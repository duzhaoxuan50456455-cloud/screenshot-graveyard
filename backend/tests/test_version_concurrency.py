"""Concurrent artifact-version completion (Task 3.7; Req 7.3-7.6, 14.3; INV-5/6; P15).

Concurrent Redo + Manual Edit and Redo + Redo on one artifact must BOTH complete,
with distinct strictly-increasing version numbers, every earlier version retained,
and a current pointer on the newest committed version.

How these tests stay honest and deterministic
  * Real threads, each with its OWN SQLAlchemy Session/connection (file-backed
    SQLite, NullPool). No Session is ever shared between threads.
  * Interleavings are placed with threading.Barrier / Event, never with sleeps.
    Barriers sit at the ENTRY of the persistence stage, before a worker takes the
    workspace lock (a barrier inside the lock would only deadlock the test).
  * Conflicts are real: a stale `max` makes the REAL database reject the REAL
    INSERT through UNIQUE(artifact_id, version_number). One test makes the race
    fully genuine (a second session commits between a writer's read and insert).
  * Synthetic images; FakeAIAdapter-based recording doubles; no network.

Scope: SQLite, one process. PostgreSQL and multi-instance behaviour are NOT
verified here; the PostgreSQL error-shape branch is covered by shape-only tests.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import threading
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from PIL import Image
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool, StaticPool

from app import config
import app.db.session as sess_mod
import app.services.artifact_version_service as avs
from app.ai import provider
from app.ai.fake_adapter import FakeAIAdapter
from app.db import models
from app.db.base import Base
from app.db.repository import CrossWorkspaceError, WorkspaceRepository
from app.services.analysis_service import AnalysisService, AnalysisUnavailableError
from app.services.artifact_version_service import (
    ArtifactVersionService,
    VersionConflictError,
    is_version_number_conflict,
)
from app.services.classification_service import ClassificationService
from app.services.consent_service import ConsentService
from app.services.operation_service import OperationService
from app.services.study_note_artifact_service import (
    StudyNoteArtifactService,
    StudyNoteArtifactUnavailableError,
)
from app.services.study_note_service import StudyNoteService
from app.services.vault_service import VaultService
from app.storage.file_store import FileStore

JOIN_TIMEOUT = 30


def png(color) -> bytes:
    b = io.BytesIO()
    Image.new("RGB", (10, 10), color).save(b, format="PNG")
    return b.getvalue()


def doc(**kw) -> str:
    return json.dumps({"k": "v", **kw}, ensure_ascii=False)


# ================================================================== infrastructure

class Recording:
    """Counts provider calls; wraps the network-free FakeAIAdapter."""

    def __init__(self):
        self._fake = FakeAIAdapter()
        self.study_calls, self.class_calls = [], []
        self._lock = threading.Lock()

    def classify_and_epitaph(self, image_bytes, mime, **kw):
        with self._lock:
            self.class_calls.append(kw)
        return self._fake.classify_and_epitaph(image_bytes, mime, **kw)

    def study_note(self, images, **kw):
        with self._lock:
            self.study_calls.append(kw)
        return self._fake.study_note(images, **kw)


class Blocking(Recording):
    """Holds the first study_note call at the provider boundary until released."""

    def __init__(self):
        super().__init__()
        self.entered, self.release = threading.Event(), threading.Event()

    def study_note(self, images, **kw):
        with self._lock:
            self.study_calls.append(kw)
        self.entered.set()
        assert self.release.wait(JOIN_TIMEOUT)
        return self._fake.study_note(images, **kw)


@pytest.fixture()
def env(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'versions.sqlite'}",
        connect_args={"check_same_thread": False, "timeout": 10},
        poolclass=NullPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        a, b = models.Workspace(token_hash="a" * 64), models.Workspace(token_hash="b" * 64)
        s.add_all([a, b])
        s.commit()
        ids = (a.id, b.id)
    yield SimpleNamespace(engine=engine, store=FileStore(tmp_path / "storage"), a=ids[0], b=ids[1])
    for p in (tmp_path / "storage").rglob("*"):
        try:
            os.chmod(p, 0o700)
        except OSError:
            pass
    engine.dispose()


def run_parallel(*jobs):
    """Run each callable in its own thread (names job-0, job-1, ...). Re-raises the
    first worker error; fails (not hangs) if any worker is still alive."""
    results, errors = [None] * len(jobs), []

    def wrap(i, fn):
        try:
            results[i] = fn()
        except BaseException as exc:  # noqa: BLE001 - re-raised in the main thread
            errors.append(exc)

    threads = [threading.Thread(target=wrap, args=(i, fn), name=f"job-{i}") for i, fn in enumerate(jobs)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(JOIN_TIMEOUT)
    assert not [t for t in threads if t.is_alive()], "worker still running: deadlock"
    if errors:
        raise errors[0]
    return results


@contextmanager
def stale_max(monkeypatch, artifact_ids, *, times=None, per_thread=None, stale_value=0):
    """Make `max_version_number` return `stale_value` (so the NEXT number is already
    taken) for a bounded number of reads. The INSERT that follows is rejected by the
    real UNIQUE constraint. `times` is a global budget per artifact; `per_thread`
    gives every thread its own budget. Yields a log of (artifact, returned, actual).
    """
    real = WorkspaceRepository.max_version_number
    targets, lock, budget, log = set(artifact_ids), threading.Lock(), {}, []

    def wrapper(self, artifact_id):
        actual = real(self, artifact_id)
        if artifact_id in targets:
            key = (artifact_id, threading.current_thread().name) if per_thread else artifact_id
            with lock:
                left = budget.setdefault(key, per_thread if per_thread else times)
                if left > 0:
                    budget[key] = left - 1
                    log.append((artifact_id, stale_value, actual))
                    return stale_value
        log.append((artifact_id, actual, actual))
        return actual

    monkeypatch.setattr(WorkspaceRepository, "max_version_number", wrapper)
    try:
        yield log
    finally:
        monkeypatch.setattr(WorkspaceRepository, "max_version_number", real)


@contextmanager
def count_max_reads(monkeypatch):
    """Count every ENTRY into `max_version_number`, including ones that are refused."""
    real, entries = WorkspaceRepository.max_version_number, []

    def counting(self, artifact_id):
        entries.append(artifact_id)
        return real(self, artifact_id)

    monkeypatch.setattr(WorkspaceRepository, "max_version_number", counting)
    try:
        yield entries
    finally:
        monkeypatch.setattr(WorkspaceRepository, "max_version_number", real)


def barrier_at(monkeypatch, cls, name, barrier):
    """Make callers of `cls.name` rendezvous on `barrier` at the method ENTRY, before
    any lock is taken. Pass the SAME barrier to every method taking part in one
    rendezvous (separate barriers would each wait forever for the other side).
    """
    real = getattr(cls, name)

    def wrapper(self, *a, **k):
        barrier.wait(JOIN_TIMEOUT)
        return real(self, *a, **k)

    monkeypatch.setattr(cls, name, wrapper)


# ----- data helpers (each opens its own short-lived Session) -----------------------

def _grant_all(s, wid, ids):
    for sid in ids:
        ConsentService(s, wid).record_consent(sid, True)


def make_note(env, wid, n=3):
    with Session(env.engine) as s:
        v = VaultService(s, wid, file_store=env.store)
        ids = [v.upload(png((wid * 20 + i * 40, 9, 9)), filename=f"{i}.png").screenshot_id for i in range(n)]
        _grant_all(s, wid, ids)
        op = OperationService(s, wid).create_pending("study_note").operation_id
        out = StudyNoteService(s, wid, adapter=Recording(), vault=v).run_generation(ids, op)
        assert out.operation.status == "successful"
        return out.artifact_id, ids


def make_analysis(env, wid):
    with Session(env.engine) as s:
        v = VaultService(s, wid, file_store=env.store)
        sid = v.upload(png((wid, 50, 60)), filename="a.png").screenshot_id
        _grant_all(s, wid, [sid])
        op = OperationService(s, wid).create_pending("classify").operation_id
        out = ClassificationService(s, wid, adapter=Recording(), vault=v).run_classification(sid, op)
        assert out.operation.status == "successful"
        return sid, out.classification_artifact_id, out.epitaph_artifact_id


def new_op(env, wid, kind="redo"):
    with Session(env.engine) as s:
        return OperationService(s, wid).create_pending(kind).operation_id


def versions(env, aid):
    with Session(env.engine) as s:
        return [
            (v.version_number, v.origin, v.content_json, v.id)
            for v in s.scalars(select(models.ArtifactVersion).where(models.ArtifactVersion.artifact_id == aid)
                               .order_by(models.ArtifactVersion.version_number))
        ]


def origins(env, aid):
    return [(n, o) for n, o, _c, _i in versions(env, aid)]


def pointer(env, aid):
    with Session(env.engine) as s:
        cur = s.get(models.Artifact, aid).current_version_id
        return cur, s.get(models.ArtifactVersion, cur).version_number


def assert_valid_pointer(env, aid):
    """Pointer targets a retained version of THIS artifact and is the newest one."""
    rows = versions(env, aid)
    cur, number = pointer(env, aid)
    assert cur in {r[3] for r in rows}
    assert number == max(r[0] for r in rows)
    assert [r[0] for r in rows] == list(range(1, len(rows) + 1))  # no gaps, no duplicates


def op_row(env, wid, op):
    with Session(env.engine) as s:
        o = s.get(models.Operation, op)
        return o.kind, o.status, o.error_detail, o.artifact_id


def op_count(env, wid):
    with Session(env.engine) as s:
        return s.scalar(select(func.count()).select_from(models.Operation).where(models.Operation.workspace_id == wid))


def provenance(env, wid):
    with Session(env.engine) as s:
        return [(r.id, r.artifact_id, r.screenshot_id) for r in s.scalars(
            select(models.SourceReference).where(models.SourceReference.workspace_id == wid)
            .order_by(models.SourceReference.id))]


def originals(env, wid):
    """(path, bytes, sha) of every stored original, to prove nothing touched them."""
    out = []
    with Session(env.engine) as s:
        for shot in s.scalars(select(models.Screenshot).where(models.Screenshot.workspace_id == wid)):
            blob = s.get(models.ContentBlob, shot.content_blob_id)
            p = env.store.root / blob.storage_path
            out.append((p, p.read_bytes(), blob.sha256, os.stat(p).st_mtime_ns))
    return out


def assert_originals_intact(snapshot):
    for p, data, sha, mtime in snapshot:
        assert p.read_bytes() == data and hashlib.sha256(data).hexdigest() == sha
        assert os.stat(p).st_mode & 0o777 == 0o444 and os.stat(p).st_mtime_ns == mtime


def workspace_snapshot(env, wid):
    with Session(env.engine) as s:
        q = lambda m: s.scalars(select(m).where(m.workspace_id == wid).order_by(m.id)).all()  # noqa: E731
        return (
            [(a.id, a.kind, a.current_version_id) for a in q(models.Artifact)],
            [(v.id, v.artifact_id, v.version_number, v.origin, v.content_json) for v in q(models.ArtifactVersion)],
            [(o.id, o.kind, o.status, o.error_detail, o.artifact_id) for o in q(models.Operation)],
            [(r.id, r.artifact_id, r.screenshot_id) for r in q(models.SourceReference)],
        )


def redo_note(env, wid, aid, op, adapter, instructions=None):
    with Session(env.engine) as s:
        v = VaultService(s, wid, file_store=env.store)
        return StudyNoteService(s, wid, adapter=adapter, vault=v).run_redo(aid, op, instructions)


def edit_note(env, wid, aid, topic="Manual topic"):
    with Session(env.engine) as s:
        return StudyNoteArtifactService(s, wid).save_edit(aid, {
            "topic": topic, "main_concepts": ["mine"], "extracted_code": ["a\n\tb  "],
            "code_explanations": [], "pitfalls": [], "uncertain_content": []})


def redo_analysis(env, wid, sid, op, adapter, instructions=None):
    with Session(env.engine) as s:
        v = VaultService(s, wid, file_store=env.store)
        return ClassificationService(s, wid, adapter=adapter, vault=v).run_redo(sid, op, instructions)


def edit_analysis(env, wid, sid):
    with Session(env.engine) as s:
        return AnalysisService(s, wid).save_edit(
            sid, {"category": "Memory/Entertainment", "epitaph_text": "my own caption"})


# ===================================================== 1. conflict classification

@pytest.fixture()
def mem():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    with Session(eng) as s:
        w = models.Workspace(token_hash="m" * 64)
        s.add(w)
        s.commit()
        art = WorkspaceRepository(s, w.id).create_artifact(kind="study_note")
        s.commit()
        yield s, w.id, art.id
    eng.dispose()


def _violation(session, make):
    session.add(make())
    with pytest.raises(IntegrityError) as caught:
        session.flush()
    session.rollback()
    return caught.value


def test_a_real_version_number_collision_is_recognised(mem):
    s, wid, aid = mem
    row = lambda: models.ArtifactVersion(workspace_id=wid, artifact_id=aid, version_number=1,  # noqa: E731
                                         origin="ai_generate", content_json="{}")
    s.add(row())
    s.commit()
    assert is_version_number_conflict(_violation(s, row))


def test_other_real_integrity_errors_are_never_treated_as_a_version_conflict(mem):
    s, wid, aid = mem
    # NOT NULL on the very same table.
    null_content = _violation(s, lambda: models.ArtifactVersion(
        workspace_id=wid, artifact_id=aid, version_number=9, origin="ai_redo", content_json=None))
    assert not is_version_number_conflict(null_content)
    # A different table's UNIQUE constraint.
    blob = WorkspaceRepository(s, wid).create_content_blob(sha256="c" * 64, byte_size=1, mime_type="image/png", storage_path="p")
    shot = WorkspaceRepository(s, wid).create_screenshot(content_blob_id=blob.id, original_filename="x.png")
    s.add(models.SourceReference(workspace_id=wid, artifact_id=aid, screenshot_id=shot.id))
    s.commit()
    dup_ref = _violation(s, lambda: models.SourceReference(workspace_id=wid, artifact_id=aid, screenshot_id=shot.id))
    assert not is_version_number_conflict(dup_ref)


class _Diag:
    def __init__(self, constraint_name):
        self.constraint_name = constraint_name


class _PgError(Exception):
    """Shape of a psycopg error. SHAPE ONLY: no PostgreSQL server is involved."""

    def __init__(self, message, constraint=None, sqlstate="23505", with_diag=True):
        super().__init__(message)
        self.sqlstate = sqlstate
        if with_diag:
            self.diag = _Diag(constraint)


def _wrap(orig):
    return IntegrityError("INSERT ...", {}, orig)


@pytest.mark.parametrize("orig,expected", [
    (_PgError("dup", constraint="artifact_version_artifact_version"), True),
    (_PgError("dup", constraint="source_reference_artifact_screenshot"), False),
    (_PgError('duplicate key value violates unique constraint "artifact_version_artifact_version"', with_diag=False), True),
    (_PgError('duplicate key value violates unique constraint "artifact_version_artifact_version"', sqlstate="23503", with_diag=False), False),
    (_PgError('duplicate key value violates unique constraint "some_other_index"', with_diag=False), False),
    (_PgError("null value in column", with_diag=False), False),
    (Exception("UNIQUE constraint failed: artifact_version.version_number, artifact_version.artifact_id"), True),
    (Exception("UNIQUE constraint failed: artifact_version.artifact_id, artifact_version.version_number"), True),
    (Exception("UNIQUE constraint failed: artifact_version.artifact_id"), False),
    (Exception("UNIQUE constraint failed: artifact.id"), False),
    (Exception("UNIQUE constraint failed: source_reference.artifact_id, source_reference.screenshot_id"), False),
    (Exception("FOREIGN KEY constraint failed"), False),
    (Exception(""), False),
])
def test_conflict_classifier_message_and_constraint_shapes(orig, expected):
    assert is_version_number_conflict(_wrap(orig)) is expected


def test_classifier_tolerates_a_missing_driver_error():
    assert is_version_number_conflict(IntegrityError("s", {}, None)) is False


# ============================================= 2. bounded retry mechanics (1 session)

def _seed(s, wid, aid, extra=0):
    svc = ArtifactVersionService(s, wid)
    svc.create_first_version(aid, doc(n=1))
    for i in range(extra):
        svc.append_version(aid, doc(n=i + 2), "manual_edit")
    s.commit()
    return svc


def _rows(s, aid):
    return [(v.version_number, v.origin) for v in s.scalars(
        select(models.ArtifactVersion).where(models.ArtifactVersion.artifact_id == aid)
        .order_by(models.ArtifactVersion.version_number))]


def test_a_stale_number_conflicts_in_the_database_then_the_retry_rereads_max_and_succeeds(mem, monkeypatch):
    s, wid, aid = mem
    svc = _seed(s, wid, aid, extra=2)               # v1..v3
    with stale_max(monkeypatch, [aid], times=1, stale_value=1) as log:   # believes next=2 (taken)
        v = svc.append_version(aid, doc(x="new"), "ai_redo")
    s.commit()
    assert [(returned, actual) for _a, returned, actual in log] == [(1, 3), (3, 3)]  # stale, then fresh
    assert v.version_number == 4
    assert _rows(s, aid) == [(1, "ai_generate"), (2, "manual_edit"), (3, "manual_edit"), (4, "ai_redo")]
    assert s.get(models.Artifact, aid).current_version_id == v.id


def test_recovers_after_attempts_minus_one_conflicts_and_exhausts_at_attempts(mem, monkeypatch):
    s, wid, aid = mem
    svc = _seed(s, wid, aid, extra=1)               # v1, v2
    n = avs.MAX_VERSION_CONFLICT_ATTEMPTS
    with stale_max(monkeypatch, [aid], times=n - 1) as log:
        svc.append_version(aid, doc(), "manual_edit")
    s.commit()
    assert len(log) == n and _rows(s, aid)[-1] == (3, "manual_edit")

    before_ptr = s.get(models.Artifact, aid).current_version_id
    with stale_max(monkeypatch, [aid], times=10**6) as log:
        with pytest.raises(VersionConflictError):
            svc.append_version(aid, doc(lost="yes"), "ai_redo")
    assert len(log) == n                              # EXACTLY the bound, never more
    s.rollback()
    assert _rows(s, aid) == [(1, "ai_generate"), (2, "manual_edit"), (3, "manual_edit")]
    assert s.get(models.Artifact, aid).current_version_id == before_ptr


def test_exhaustion_error_is_sanitised_and_leaves_the_session_usable(mem, monkeypatch):
    s, wid, aid = mem
    svc = _seed(s, wid, aid)
    other = WorkspaceRepository(s, wid).create_artifact(kind="classification")   # unrelated PENDING work
    with stale_max(monkeypatch, [aid], times=10**6):
        with pytest.raises(VersionConflictError) as exc:
            svc.append_version(aid, doc(secret="PRIVATE-NOTE-TEXT"), "manual_edit")
    text = str(exc.value)
    assert exc.value.__cause__ is None and exc.value.__suppress_context__
    for leaked in ("PRIVATE-NOTE-TEXT", "UNIQUE", "artifact_version", "INSERT", "version_number"):
        assert leaked not in text
    # The savepoints rolled back, the outer transaction is still valid and usable.
    assert s.get(models.Artifact, other.id) is not None
    v = svc.append_version(aid, doc(), "manual_edit")                  # works right away
    s.commit()
    assert v.version_number == 2 and _rows(s, aid)[-1] == (2, "manual_edit")


def test_unrelated_pending_work_survives_a_recovered_conflict(mem, monkeypatch):
    s, wid, aid = mem
    svc = _seed(s, wid, aid)
    other = WorkspaceRepository(s, wid).create_artifact(kind="epitaph")
    with stale_max(monkeypatch, [aid], times=2):
        svc.append_version(aid, doc(), "ai_redo")
    s.commit()
    assert s.get(models.Artifact, other.id) is not None and _rows(s, aid)[-1] == (2, "ai_redo")


def test_lock_is_taken_once_and_not_dropped_by_a_retry(mem, monkeypatch):
    s, wid, aid = mem
    svc = _seed(s, wid, aid)
    locks = []
    real = WorkspaceRepository.lock_workspace
    monkeypatch.setattr(WorkspaceRepository, "lock_workspace", lambda self: locks.append(1) or real(self))
    with stale_max(monkeypatch, [aid], times=3):
        svc.append_version(aid, doc(), "ai_redo")
    assert len(locks) == 1


def test_only_a_recognised_conflict_is_retried(mem, monkeypatch):
    s, wid, aid = mem
    svc = _seed(s, wid, aid)
    calls = []

    def other_integrity(self, **kw):
        calls.append(1)
        raise IntegrityError("INSERT", {}, Exception("NOT NULL constraint failed: artifact_version.content_json"))

    monkeypatch.setattr(WorkspaceRepository, "add_artifact_version", other_integrity)
    with pytest.raises(IntegrityError):
        svc.append_version(aid, doc(), "manual_edit")
    assert len(calls) == 1                           # no blind retry
    monkeypatch.undo()
    s.rollback()
    assert _rows(s, aid) == [(1, "ai_generate")]


@pytest.mark.parametrize("error", [
    OperationalError("INSERT", {}, Exception("database is locked")),
    RuntimeError("boom"),
])
def test_lock_timeouts_and_other_failures_are_not_retried(mem, monkeypatch, error):
    s, wid, aid = mem
    svc = _seed(s, wid, aid)
    calls = []

    def fail(self, **kw):
        calls.append(1)
        raise error

    monkeypatch.setattr(WorkspaceRepository, "add_artifact_version", fail)
    with pytest.raises(type(error)):
        svc.append_version(aid, doc(), "manual_edit")
    assert len(calls) == 1
    monkeypatch.undo()
    s.rollback()
    assert _rows(s, aid) == [(1, "ai_generate")]


@pytest.mark.parametrize("bad", [0, -1, 2.5, True, "3"])
def test_retry_bound_must_be_a_positive_integer(mem, bad):
    s, wid, _ = mem
    with pytest.raises(ValueError):
        ArtifactVersionService(s, wid, max_conflict_attempts=bad)


def test_the_bound_is_explicit_per_instance_and_defaults_to_the_module_constant(mem, monkeypatch):
    s, wid, aid = mem
    _seed(s, wid, aid)
    with stale_max(monkeypatch, [aid], times=1):
        with pytest.raises(VersionConflictError):       # 1 attempt: the single conflict is final
            ArtifactVersionService(s, wid, max_conflict_attempts=1).append_version(aid, doc(), "manual_edit")
    s.rollback()
    monkeypatch.setattr(avs, "MAX_VERSION_CONFLICT_ATTEMPTS", 2)
    with stale_max(monkeypatch, [aid], times=1):        # default now 2: one conflict is recoverable
        assert ArtifactVersionService(s, wid).append_version(aid, doc(), "manual_edit").version_number == 2
    s.commit()


def test_a_foreign_artifact_is_refused_without_retrying_or_writing(mem, monkeypatch):
    s, wid, aid = mem
    other = models.Workspace(token_hash="z" * 64)
    s.add(other)
    s.commit()
    with count_max_reads(monkeypatch) as reads:
        with pytest.raises(CrossWorkspaceError):
            ArtifactVersionService(s, other.id).append_version(aid, doc(), "manual_edit")
    assert len(reads) == 1                                # refused on the first read: no retry loop
    s.rollback()
    assert _rows(s, aid) == []


# ============================ 3. a genuine cross-session conflict (no injected error)

def test_a_second_sessions_commit_between_read_and_insert_is_recovered(env, monkeypatch):
    """Writer A computes its number from a committed state, B then commits that very
    number, and A's INSERT is rejected by the database. A is modelled as a writer that
    does NOT hold the serializing lock (the case the optimistic layer exists for).
    Everything after that point - the rejection, the rollback to the SAVEPOINT, the
    re-read and the retry - is the real mechanism."""
    aid, _ids = make_note(env, env.a)
    a_has_read, b_done, a_calls = threading.Event(), threading.Event(), []
    real_max, real_lock = WorkspaceRepository.max_version_number, WorkspaceRepository.lock_workspace

    def lock(self):
        return True if threading.current_thread().name == "job-0" else real_lock(self)

    def max_version(self, artifact_id):
        if threading.current_thread().name != "job-0":
            return real_max(self, artifact_id)
        a_calls.append(1)
        if len(a_calls) == 1:
            with Session(env.engine) as reader:           # a real, earlier, committed read
                value = real_max(WorkspaceRepository(reader, env.a), artifact_id)
            a_has_read.set()
            assert b_done.wait(JOIN_TIMEOUT)               # B commits that same number now
            return value
        return real_max(self, artifact_id)

    monkeypatch.setattr(WorkspaceRepository, "lock_workspace", lock)
    monkeypatch.setattr(WorkspaceRepository, "max_version_number", max_version)

    def writer_a():
        with Session(env.engine) as s:
            v = ArtifactVersionService(s, env.a).append_version(aid, doc(who="A"), "ai_redo")
            number = v.version_number
            s.commit()
            return number

    def writer_b():
        assert a_has_read.wait(JOIN_TIMEOUT)
        with Session(env.engine) as s:
            v = ArtifactVersionService(s, env.a).append_version(aid, doc(who="B"), "manual_edit")
            number = v.version_number
            s.commit()
        b_done.set()
        return number

    a_num, b_num = run_parallel(writer_a, writer_b)
    assert (b_num, a_num) == (2, 3)                       # A lost the race for 2, won 3 on retry
    assert len(a_calls) == 2                              # exactly one conflict, one retry
    assert origins(env, aid) == [(1, "ai_generate"), (2, "manual_edit"), (3, "ai_redo")]
    assert json.loads(versions(env, aid)[1][2])["who"] == "B"      # B's version never overwritten
    assert_valid_pointer(env, aid)


# ============================================= 4. many writers, real threads, Barrier

@pytest.mark.parametrize("forced_conflicts", [False, True])
def test_many_concurrent_appends_all_complete_with_distinct_numbers(env, monkeypatch, forced_conflicts):
    aid, _ = make_note(env, env.a)
    n = 8
    barrier = threading.Barrier(n)

    def writer(i):
        def run():
            with Session(env.engine) as s:
                barrier.wait(JOIN_TIMEOUT)
                v = ArtifactVersionService(s, env.a).append_version(
                    aid, doc(i=i), "ai_redo" if i % 2 else "manual_edit")
                s.commit()
                return v.version_number
        return run

    ctx = stale_max(monkeypatch, [aid], per_thread=1) if forced_conflicts else stale_max(monkeypatch, [], times=0)
    with ctx as log:
        numbers = run_parallel(*[writer(i) for i in range(n)])
    assert sorted(numbers) == list(range(2, n + 2))       # distinct, gap-free
    assert len(versions(env, aid)) == 1 + n
    assert_valid_pointer(env, aid)
    if forced_conflicts:
        assert sum(1 for _a, ret, act in log if ret != act) == n    # every writer conflicted once


# ============================================== 5. end-to-end Redo + Manual Edit

def test_redo_blocked_at_the_provider_while_a_manual_edit_commits(env):
    aid, _ = make_note(env, env.a)
    op, adapter = new_op(env, env.a), Blocking()

    def redo():
        return redo_note(env, env.a, aid, op, adapter, "focus on pitfalls").operation.status

    def edit():
        assert adapter.entered.wait(JOIN_TIMEOUT)         # Redo is now at the provider
        edit_note(env, env.a, aid, "Edited during redo")
        adapter.release.set()
        return "edited"

    status, _ = run_parallel(redo, edit)
    assert status == "successful"
    assert origins(env, aid) == [(1, "ai_generate"), (2, "manual_edit"), (3, "ai_redo")]
    assert json.loads(versions(env, aid)[1][2])["topic"] == "Edited during redo"   # edit retained
    assert_valid_pointer(env, aid)
    assert len(adapter.study_calls) == 1
    assert adapter.study_calls[0] == {"instructions": "focus on pitfalls"}
    # The provider never saw the concurrent edit (it is not part of the request).
    assert "Edited during redo" not in repr(adapter.study_calls)


@pytest.mark.parametrize("forced_conflict", [False, True])
def test_redo_and_manual_edit_enter_persistence_together_and_both_complete(env, monkeypatch, forced_conflict):
    for _round in range(4):                               # different scheduler orders
        aid, order = make_note(env, env.a)
        op, adapter = new_op(env, env.a), Recording()
        ops_before, refs_before, bytes_before = op_count(env, env.a), provenance(env, env.a), originals(env, env.a)
        with monkeypatch.context() as m:
            rendezvous = threading.Barrier(2)
            barrier_at(m, OperationService, "complete_after_persistence", rendezvous)
            barrier_at(m, StudyNoteArtifactService, "save_edit", rendezvous)
            ctx = stale_max(m, [aid], per_thread=1) if forced_conflict else stale_max(m, [], times=0)
            with ctx as log:
                redo_out, edit_out = run_parallel(
                    lambda: redo_note(env, env.a, aid, op, adapter, "be brief"),
                    lambda: edit_note(env, env.a, aid, "Concurrent manual edit"),
                )
        assert redo_out.operation.status == "successful"
        assert edit_out.version_number in (2, 3)
        rows = versions(env, aid)
        assert [r[0] for r in rows] == [1, 2, 3]
        assert sorted(o for _n, o in origins(env, aid)[1:]) == ["ai_redo", "manual_edit"]
        assert len({r[2] for r in rows}) == 3             # three different retained payloads
        manual = next(r for r in rows if r[1] == "manual_edit")
        assert json.loads(manual[2])["topic"] == "Concurrent manual edit"   # never discarded
        assert_valid_pointer(env, aid)
        assert len(adapter.study_calls) == 1              # ONE provider call
        assert op_count(env, env.a) == ops_before         # no extra operation / quota
        assert op_row(env, env.a, op) == ("redo", "successful", None, aid)
        assert provenance(env, env.a)[: len(refs_before)] == refs_before
        assert_originals_intact(bytes_before)
        if forced_conflict:
            assert sum(1 for _a, ret, act in log if ret != act) == 2   # both writers conflicted once


# ================================================================ 6. Redo + Redo

@pytest.mark.parametrize("forced_conflict", [False, True])
def test_two_distinct_redos_both_complete_with_their_own_version(env, monkeypatch, forced_conflict):
    aid, _ = make_note(env, env.a)
    op1, op2, adapter = new_op(env, env.a), new_op(env, env.a), Recording()
    ops_before = op_count(env, env.a)
    refs_before, bytes_before = provenance(env, env.a), originals(env, env.a)
    barrier_at(monkeypatch, OperationService, "complete_after_persistence", threading.Barrier(2))
    ctx = stale_max(monkeypatch, [aid], per_thread=1) if forced_conflict else stale_max(monkeypatch, [], times=0)
    with ctx:
        o1, o2 = run_parallel(
            lambda: redo_note(env, env.a, aid, op1, adapter, "first instructions"),
            lambda: redo_note(env, env.a, aid, op2, adapter, "second instructions"),
        )
    assert (o1.operation.status, o2.operation.status) == ("successful", "successful")
    rows = versions(env, aid)
    assert [(r[0], r[1]) for r in rows] == [(1, "ai_generate"), (2, "ai_redo"), (3, "ai_redo")]
    assert len({r[2] for r in rows}) == 3                 # each redo kept its OWN payload
    assert_valid_pointer(env, aid)
    assert sorted(c["instructions"] for c in adapter.study_calls) == ["first instructions", "second instructions"]
    assert op_count(env, env.a) == ops_before             # retries created no operation
    assert op_row(env, env.a, op1)[:2] == op_row(env, env.a, op2)[:2] == ("redo", "successful")
    assert provenance(env, env.a) == refs_before          # provenance untouched
    assert_originals_intact(bytes_before)


def test_two_redos_and_a_manual_edit_together_keep_every_version(env, monkeypatch):
    aid, _ = make_note(env, env.a)
    op1, op2, adapter = new_op(env, env.a), new_op(env, env.a), Recording()
    rendezvous = threading.Barrier(3)                      # both redos and the edit, together
    barrier_at(monkeypatch, OperationService, "complete_after_persistence", rendezvous)
    barrier_at(monkeypatch, StudyNoteArtifactService, "save_edit", rendezvous)
    redo1, redo2, edit = run_parallel(
        lambda: redo_note(env, env.a, aid, op1, adapter, "one").operation.status,
        lambda: redo_note(env, env.a, aid, op2, adapter, "two").operation.status,
        lambda: edit_note(env, env.a, aid, "Edited among redos").version_number,
    )
    assert (redo1, redo2) == ("successful", "successful")
    assert edit in (2, 3, 4)
    assert [r[0] for r in versions(env, aid)] == [1, 2, 3, 4]
    assert sorted(o for _n, o in origins(env, aid)[1:]) == ["ai_redo", "ai_redo", "manual_edit"]
    assert len({r[2] for r in versions(env, aid)}) == 4    # four distinct retained payloads
    assert_valid_pointer(env, aid)
    assert len(adapter.study_calls) == 2


# ================================ 7. forced recovery / exhaustion through real services

def test_a_conflict_during_redo_persistence_reuses_the_ai_result(env, monkeypatch):
    aid, _ = make_note(env, env.a)
    edit_note(env, env.a, aid)
    op, adapter = new_op(env, env.a), Recording()
    ops_before = op_count(env, env.a)
    with stale_max(monkeypatch, [aid], times=2) as log:     # two conflicts in a row
        out = redo_note(env, env.a, aid, op, adapter, "x")
    assert out.operation.status == "successful"
    assert len(adapter.study_calls) == 1                    # provider NOT re-invoked
    assert op_count(env, env.a) == ops_before               # no new operation => no new quota use
    assert [n for n, _o in origins(env, aid)] == [1, 2, 3]
    assert sum(1 for _a, ret, act in log if ret != act) == 2
    assert_valid_pointer(env, aid)
    with Session(env.engine) as s:                          # quota accounting itself is unchanged
        assert WorkspaceRepository(s, env.a).count_ai_operations() == ops_before


def test_redo_retry_exhaustion_fails_safely_and_a_new_operation_then_succeeds(env, monkeypatch):
    aid, order = make_note(env, env.a)
    edit_note(env, env.a, aid, "Keep me")
    snapshot_versions, ptr = versions(env, aid), pointer(env, aid)
    refs, bytes_before = provenance(env, env.a), originals(env, env.a)
    op, adapter = new_op(env, env.a), Recording()
    ops_before = op_count(env, env.a)
    with stale_max(monkeypatch, [aid], times=10**6) as log:
        out = redo_note(env, env.a, aid, op, adapter, "x")
    assert len(log) == avs.MAX_VERSION_CONFLICT_ATTEMPTS
    assert out.operation.status == "failed" and out.operation.error_detail == "Processing failed"
    for leaked in ("UNIQUE", "artifact_version", "version_number", "INSERT"):
        assert leaked not in (out.operation.error_detail or "")
    assert len(adapter.study_calls) == 1                    # the provider was called once, never again
    assert versions(env, aid) == snapshot_versions and pointer(env, aid) == ptr   # nothing partial
    assert provenance(env, env.a) == refs
    assert_originals_intact(bytes_before)
    assert op_count(env, env.a) == ops_before
    # A safe retry is a NEW operation and appends a NEW version after the retained ones.
    retry = new_op(env, env.a)
    again = redo_note(env, env.a, aid, retry, adapter, "x")
    assert again.operation.status == "successful"
    assert [n for n, _o in origins(env, aid)] == [1, 2, 3]
    assert op_row(env, env.a, op)[1] == "failed" and op_row(env, env.a, retry)[1] == "successful"
    assert_valid_pointer(env, aid)


def test_same_operation_delivered_twice_while_a_manual_edit_commits_calls_the_provider_once(env):
    aid, _ = make_note(env, env.a)
    op, adapter = new_op(env, env.a), Blocking()
    second_started = threading.Event()

    def first():
        return redo_note(env, env.a, aid, op, adapter, "x").operation.status

    def duplicate():
        assert adapter.entered.wait(JOIN_TIMEOUT)
        second_started.set()
        return redo_note(env, env.a, aid, op, adapter, "x").operation.status

    def editor():
        assert second_started.wait(JOIN_TIMEOUT)
        edit_note(env, env.a, aid, "During duplicate delivery")
        adapter.release.set()
        return "edited"

    s1, s2, _ = run_parallel(first, duplicate, editor)
    assert (s1, s2) == ("successful", "successful")
    assert len(adapter.study_calls) == 1                   # ONE provider call for ONE operation
    assert origins(env, aid) == [(1, "ai_generate"), (2, "manual_edit"), (3, "ai_redo")]  # one redo only
    assert_valid_pointer(env, aid)


# ============================================ 8. Classification + Epitaph pair atomicity

def _pair_origins(env, cid, eid):
    return origins(env, cid), origins(env, eid)


def test_analysis_redo_and_edit_under_contention_keep_both_artifacts_in_step(env, monkeypatch):
    for _round in range(3):
        sid, cid, eid = make_analysis(env, env.a)
        op, adapter = new_op(env, env.a), Recording()
        with monkeypatch.context() as m:
            rendezvous = threading.Barrier(2)
            barrier_at(m, OperationService, "complete_after_persistence", rendezvous)
            barrier_at(m, AnalysisService, "save_edit", rendezvous)
            redo_out, _view = run_parallel(
                lambda: redo_analysis(env, env.a, sid, op, adapter, "brief"),
                lambda: edit_analysis(env, env.a, sid),
            )
        assert redo_out.operation.status == "successful"
        class_o, epi_o = _pair_origins(env, cid, eid)
        assert class_o == epi_o                                    # same order on BOTH artifacts
        assert sorted(o for _n, o in class_o[1:]) == ["ai_redo", "manual_edit"]
        assert_valid_pointer(env, cid)
        assert_valid_pointer(env, eid)
        assert len(adapter.class_calls) == 1


def test_a_conflict_on_only_the_epitaph_is_recovered_and_the_pair_still_commits_together(env, monkeypatch):
    sid, cid, eid = make_analysis(env, env.a)
    op, adapter = new_op(env, env.a), Recording()
    with stale_max(monkeypatch, [eid], times=1) as log:
        out = redo_analysis(env, env.a, sid, op, adapter, "x")
    assert out.operation.status == "successful"
    assert _pair_origins(env, cid, eid) == ([(1, "ai_generate"), (2, "ai_redo")],) * 2
    assert sum(1 for a, ret, act in log if ret != act and a == eid) == 1
    assert len(adapter.class_calls) == 1
    assert_valid_pointer(env, cid)
    assert_valid_pointer(env, eid)


def test_epitaph_retry_exhaustion_rolls_the_classification_version_back_too(env, monkeypatch):
    sid, cid, eid = make_analysis(env, env.a)
    snapshot = (versions(env, cid), versions(env, eid), pointer(env, cid), pointer(env, eid))
    op, adapter = new_op(env, env.a), Recording()
    with stale_max(monkeypatch, [eid], times=10**6):
        out = redo_analysis(env, env.a, sid, op, adapter, "x")
    assert out.operation.status == "failed" and out.operation.error_detail == "Processing failed"
    assert (versions(env, cid), versions(env, eid), pointer(env, cid), pointer(env, eid)) == snapshot
    assert len(adapter.class_calls) == 1                           # the classification was NOT re-requested
    # Manual edit of the pair hits the same wall: nothing half-saved, safe error.
    with stale_max(monkeypatch, [eid], times=10**6):
        with Session(env.engine) as s:
            with pytest.raises(AnalysisUnavailableError):
                AnalysisService(s, env.a).save_edit(sid, {"category": "Memory/Entertainment", "epitaph_text": "x"})
    assert (versions(env, cid), versions(env, eid)) == snapshot[:2]


# ========================================================= 9. workspace isolation

def test_contention_in_one_workspace_never_touches_another(env, monkeypatch):
    aid_a, _ = make_note(env, env.a)
    aid_b, _ = make_note(env, env.b)
    sid_b, cid_b, eid_b = make_analysis(env, env.b)
    edit_note(env, env.b, aid_b, "B's own edit")
    b_before, b_bytes = workspace_snapshot(env, env.b), originals(env, env.b)
    b_ops = op_count(env, env.b)

    op1, op2, adapter = new_op(env, env.a), new_op(env, env.a), Recording()
    barrier_at(monkeypatch, OperationService, "complete_after_persistence", threading.Barrier(2))
    with stale_max(monkeypatch, [aid_a], per_thread=1):
        run_parallel(
            lambda: redo_note(env, env.a, aid_a, op1, adapter, "1"),
            lambda: redo_note(env, env.a, aid_a, op2, adapter, "2"),
        )
    assert origins(env, aid_a) == [(1, "ai_generate"), (2, "ai_redo"), (3, "ai_redo")]
    assert workspace_snapshot(env, env.b) == b_before             # B is byte-for-byte unchanged
    assert op_count(env, env.b) == b_ops
    assert_originals_intact(b_bytes)
    # B cannot write into A's artifact, even mid-contention, and nothing is retried or written.
    a_before = workspace_snapshot(env, env.a)
    with count_max_reads(monkeypatch) as reads:
        with Session(env.engine) as s:
            with pytest.raises(CrossWorkspaceError):
                ArtifactVersionService(s, env.b).append_version(aid_a, doc(evil=1), "manual_edit")
            s.rollback()
    assert len(reads) == 1                                # refused immediately, never retried
    assert workspace_snapshot(env, env.a) == a_before


# ===================================================================== 10. HTTP

TRUSTED = config.TRUSTED_ORIGINS[0]
HDRS = {"Origin": TRUSTED, "X-Requested-With": "fetch"}


@pytest.fixture()
def http(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.ratelimit import TokenBucketRateLimiter

    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    factory = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False, class_=Session)
    monkeypatch.setattr(sess_mod, "engine", eng)
    monkeypatch.setattr(sess_mod, "SessionLocal", factory)
    monkeypatch.setattr(config, "COOKIE_SECURE", False)
    monkeypatch.setattr(config, "STORAGE_ROOT", str(tmp_path / "storage"))
    lim = TokenBucketRateLimiter(capacity=100_000, refill_per_sec=100_000.0)
    c = TestClient(create_app(workspace_session_factory=factory, rate_limiter=lim, workspace_rate_limiter=lim))
    c.get("/api/workspace")
    saved, adapter = provider.get_ai_adapter(), Recording()
    provider.set_ai_adapter(adapter)
    yield c, eng, adapter
    provider.set_ai_adapter(saved)
    for p in (tmp_path / "storage").rglob("*"):
        try:
            os.chmod(p, 0o700)
        except OSError:
            pass


def _http_note(c, n=2):
    ids = []
    for i in range(n):
        r = c.post("/api/screenshots", headers=HDRS, files=[("files", (f"{i}.png", png((i * 30, 5, 5)), "image/png"))])
        sid = r.json()["results"][0]["screenshotId"]
        c.post(f"/api/screenshots/{sid}/consent", headers=HDRS, json={"granted": True})
        ids.append(sid)
    op = c.post("/api/study-notes", headers=HDRS, json={"screenshotIds": ids}).json()["operationId"]
    assert c.get(f"/api/operations/{op}").json()["status"] == "successful"
    return c.get("/api/study-notes").json()["notes"][0]["artifactId"], ids


SAFE_503 = {"error": {"code": "service_unavailable", "message": "Service temporarily unavailable"}}


def test_http_note_edit_reports_a_generic_503_when_retries_are_exhausted(http, monkeypatch):
    c, eng, _ = http
    aid, _ = _http_note(c)
    with stale_max(monkeypatch, [aid], times=10**6):
        r = c.post(f"/api/study-notes/{aid}/edits", headers=HDRS, json={"content": {"topic": "PRIVATE-TOPIC"}})
    assert r.status_code == 503 and r.json() == SAFE_503
    assert "PRIVATE-TOPIC" not in r.text and "UNIQUE" not in r.text
    versions_now = c.get(f"/api/study-notes/{aid}/versions").json()["versions"]
    assert [(v["versionNumber"], v["isCurrent"]) for v in versions_now] == [(1, True)]
    ok = c.post(f"/api/study-notes/{aid}/edits", headers=HDRS, json={"content": {"topic": "After"}})
    assert ok.status_code == 200 and ok.json()["versionNumber"] == 2        # service recovered


def test_http_analysis_edit_reports_a_generic_503_and_changes_nothing(http, monkeypatch):
    c, eng, _ = http
    r = c.post("/api/screenshots", headers=HDRS, files=[("files", ("a.png", png((9, 9, 9)), "image/png"))])
    sid = r.json()["results"][0]["screenshotId"]
    c.post(f"/api/screenshots/{sid}/consent", headers=HDRS, json={"granted": True})
    op = c.post(f"/api/screenshots/{sid}/classify", headers=HDRS).json()["operationId"]
    assert c.get(f"/api/operations/{op}").json()["status"] == "successful"
    with Session(eng) as s:
        eid = s.scalars(select(models.Artifact).where(models.Artifact.kind == "epitaph")).first().id
    with stale_max(monkeypatch, [eid], times=10**6):
        resp = c.post(f"/api/screenshots/{sid}/analysis/edits", headers=HDRS,
                      json={"category": "Memory/Entertainment", "epitaphText": "x"})
    assert resp.status_code == 503 and resp.json() == SAFE_503
    now = c.get(f"/api/screenshots/{sid}/analysis").json()
    assert now["classification"]["versionNumber"] == now["epitaph"]["versionNumber"] == 1


def test_http_redo_that_exhausts_retries_polls_as_failed_with_a_sanitised_detail(http, monkeypatch):
    c, eng, adapter = http
    aid, _ = _http_note(c)
    calls = len(adapter.study_calls)
    with stale_max(monkeypatch, [aid], times=10**6):
        r = c.post(f"/api/study-notes/{aid}/redo", headers=HDRS, json={"instructions": "SECRET-GUIDANCE"})
    assert r.status_code == 202
    poll = c.get(f"/api/operations/{r.json()['operationId']}").json()
    assert poll["status"] == "failed" and poll["errorDetail"] == "Processing failed"
    assert "SECRET-GUIDANCE" not in json.dumps(poll) and "UNIQUE" not in json.dumps(poll)
    assert len(adapter.study_calls) == calls + 1                          # one provider call, no re-invocation
    assert [(v["versionNumber"]) for v in c.get(f"/api/study-notes/{aid}/versions").json()["versions"]] == [1]
    # A new operation succeeds afterwards.
    r2 = c.post(f"/api/study-notes/{aid}/redo", headers=HDRS, json={"instructions": "again"})
    assert c.get(f"/api/operations/{r2.json()['operationId']}").json()["status"] == "successful"
    assert [(v["versionNumber"], v["origin"]) for v in c.get(f"/api/study-notes/{aid}/versions").json()["versions"]] == [
        (1, "ai_generate"), (2, "ai_redo")]
