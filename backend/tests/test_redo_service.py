"""AI Redo service tests (Task 3.3; Req 7.3, 7.6, 7.7, 6.10; INV-5, INV-6; P15).

Real StudyNoteService / ClassificationService / OperationService / ConsentService /
ArtifactVersionService / VaultService against a disposable SQLite DB and private temp
storage, with synthetic images. Adapters are in-test recording doubles around the
network-free FakeAIAdapter (no provider, no network). Concurrency (Redo+Redo,
Redo+Edit retry-on-conflict) is Task 3.7 and is NOT claimed here.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
from itertools import product
from threading import Event, Lock, Thread

import pytest
from PIL import Image
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool, StaticPool

from app.ai.adapter import AIAdapterError, AIAdapterTimeoutError
from app.ai.fake_adapter import FakeAIAdapter, FakeAIAdapterMode
from app.ai.schemas import StudyNoteResult
from app.db import models
from app.db.base import Base
from app.db.repository import CrossWorkspaceError, WorkspaceRepository
from app.services.analysis_service import AnalysisService
from app.services.artifact_version_service import ArtifactVersionService
from app.services.classification_service import ClassificationService
from app.services.consent_service import ConsentService
from app.services.operation_service import OperationService
from app.services.study_note_artifact_service import StudyNoteArtifactService
from app.services.study_note_service import StudyNoteService
from app.services.vault_service import VaultService
from app.storage.file_store import FileStore

CODE = "def f(x):\r\n\treturn  x  # \u03bb   "


def png(color) -> bytes:
    b = io.BytesIO()
    Image.new("RGB", (10, 10), color).save(b, format="PNG")
    return b.getvalue()


class Recording:
    """Records every call; delegates to FakeAIAdapter unless told otherwise."""

    def __init__(self, mode=FakeAIAdapterMode.NORMAL, raises=None, study=None):
        self._fake = FakeAIAdapter(mode=mode)
        self._raises, self._study = raises, study
        self.study_calls: list[tuple[list, dict]] = []
        self.class_calls: list[tuple[tuple, dict]] = []

    def classify_and_epitaph(self, image_bytes, mime, **kw):
        self.class_calls.append(((image_bytes, mime), kw))
        if self._raises:
            raise self._raises
        return self._fake.classify_and_epitaph(image_bytes, mime, **kw)

    def study_note(self, images, **kw):
        self.study_calls.append((list(images), kw))
        if self._raises:
            raise self._raises
        if self._study:
            return self._study(images)
        return self._fake.study_note(images, **kw)


@pytest.fixture()
def ctx(tmp_path):
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    store = FileStore(tmp_path / "storage")
    with Session(eng) as s:
        a, b = models.Workspace(token_hash="a" * 64), models.Workspace(token_hash="b" * 64)
        s.add_all([a, b])
        s.commit()
        yield s, store, a.id, b.id
    for p in (tmp_path / "storage").rglob("*"):
        try:
            os.chmod(p, 0o700)
        except OSError:
            pass


def _shots(s, store, wid, n=3, consent=True):
    v = VaultService(s, wid, file_store=store)
    ids = [v.upload(png((i * 40, 9, 9)), filename=f"{i}.png").screenshot_id for i in range(n)]
    if consent:
        for sid in ids:
            ConsentService(s, wid).record_consent(sid, True)
    return ids, v


def _op(s, wid, kind="redo") -> int:
    return OperationService(s, wid).create_pending(kind).operation_id


def _versions(s, aid):
    return s.scalars(select(models.ArtifactVersion).where(models.ArtifactVersion.artifact_id == aid)
                     .order_by(models.ArtifactVersion.version_number)).all()


def _make_note(s, store, wid, order=None, n=3):
    ids, v = _shots(s, store, wid, n)
    order = order or ids
    out = StudyNoteService(s, wid, adapter=Recording(), vault=v).run_generation(order, _op(s, wid, "study_note"))
    assert out.operation.status == "successful"
    return out.artifact_id, order, v


def _edit_note(s, wid, aid, topic="My manual topic"):
    return StudyNoteArtifactService(s, wid).save_edit(
        aid, {"topic": topic, "main_concepts": ["mine"], "extracted_code": [CODE],
              "code_explanations": [], "pitfalls": [], "uncertain_content": []})


def _redo(s, wid, v, aid, adapter, instructions=None, op=None):
    op = op or _op(s, wid)
    return StudyNoteService(s, wid, adapter=adapter, vault=v).run_redo(aid, op, instructions), op


def _counts(s, wid):
    def c(m):
        return s.scalar(select(func.count()).select_from(m).where(m.workspace_id == wid))
    return c(models.Artifact), c(models.ArtifactVersion), c(models.SourceReference)


# =============================================================== Study Notes

def test_redo_after_manual_edit_appends_ai_redo_and_retains_the_edit(ctx):
    s, store, wa, _ = ctx
    aid, order, v = _make_note(s, store, wa)
    v1 = _versions(s, aid)[0]
    edit = _edit_note(s, wa, aid)
    snapshot = [(r.id, r.version_number, r.origin, r.content_json) for r in _versions(s, aid)]
    assert [x[2] for x in snapshot] == ["ai_generate", "manual_edit"]

    out, op = _redo(s, wa, v, aid, Recording(), "focus on pitfalls")

    assert out.operation.status == "successful" and out.artifact_id == aid
    rows = _versions(s, aid)
    assert [(r.version_number, r.origin) for r in rows] == [(1, "ai_generate"), (2, "manual_edit"), (3, "ai_redo")]
    # Earlier versions, INCLUDING the manual edit, are byte-for-byte unchanged.
    assert [(r.id, r.version_number, r.origin, r.content_json) for r in rows[:2]] == snapshot
    assert json.loads(rows[1].content_json)["topic"] == "My manual topic"
    assert json.loads(rows[1].content_json)["extracted_code"] == [CODE]
    # The manual edit stays separately retrievable through the public read path.
    svc = StudyNoteArtifactService(s, wa)
    assert svc.get_version(aid, edit.version_id).content.topic == "My manual topic"
    assert svc.get_version(aid, v1.id).origin == "ai_generate"
    # Pointer moved to the new version, and only that one.
    assert s.get(models.Artifact, aid).current_version_id == rows[2].id
    assert svc.get_version(aid).origin == "ai_redo"
    # The redo regenerated rather than copying the manual edit.
    assert json.loads(rows[2].content_json)["topic"] != "My manual topic"


def test_redo_is_a_recorded_operation_linked_to_the_artifact(ctx):
    s, store, wa, _ = ctx
    aid, _, v = _make_note(s, store, wa)
    op = _op(s, wa)
    assert s.get(models.Operation, op).status == "pending"
    assert s.get(models.Operation, op).kind == "redo"
    _redo(s, wa, v, aid, Recording(), None, op)
    row = s.get(models.Operation, op)
    assert (row.kind, row.status, row.artifact_id) == ("redo", "successful", aid)


def test_modified_instructions_reach_the_adapter_and_change_the_result(ctx):
    s, store, wa, _ = ctx
    aid, order, v = _make_note(s, store, wa)
    ad = Recording()
    _redo(s, wa, v, aid, ad, "  focus on pitfalls  ")
    _redo(s, wa, v, aid, ad, "summarise in three bullets")
    assert [kw for _, kw in ad.study_calls] == [
        {"instructions": "focus on pitfalls"}, {"instructions": "summarise in three bullets"}]  # trimmed, exact
    rows = _versions(s, aid)
    c2, c3 = (json.loads(r.content_json) for r in rows[1:])
    assert c2["main_concepts"] != c3["main_concepts"]  # honoured: distinct instructions -> distinct output
    assert [r.origin for r in rows] == ["ai_generate", "ai_redo", "ai_redo"]


def test_redo_without_instructions_makes_the_plain_one_argument_call(ctx):
    s, store, wa, _ = ctx
    aid, _, v = _make_note(s, store, wa)
    ad = Recording()
    for blank in (None, "", "   "):
        _redo(s, wa, v, aid, ad, blank)
    assert [kw for _, kw in ad.study_calls] == [{}, {}, {}]


def test_provider_receives_only_authorised_originals_and_instructions(ctx):
    """Current content / prior versions / manual edits are NEVER sent to the provider."""
    s, store, wa, _ = ctx
    aid, order, v = _make_note(s, store, wa, order=None)
    _edit_note(s, wa, aid, topic="PRIVATE-MANUAL-TOPIC-XYZ")
    ad = Recording()
    _redo(s, wa, v, aid, ad, "be brief")
    images, kw = ad.study_calls[0]
    assert [i for i, _b, _m in images] == order
    for sid, data, mime in images:
        assert data == v.read_original(sid).data and mime == "image/png"
    assert set(kw) == {"instructions"}
    blob = repr(images) + repr(kw)
    assert "PRIVATE-MANUAL-TOPIC-XYZ" not in blob and "mine" not in blob


def test_source_references_and_selection_order_survive_redo(ctx):
    s, store, wa, _ = ctx
    ids, v = _shots(s, store, wa, 4)
    order = [ids[2], ids[0], ids[3], ids[1]]
    aid = StudyNoteService(s, wa, adapter=Recording(), vault=v).run_generation(order, _op(s, wa, "study_note")).artifact_id
    repo = WorkspaceRepository(s, wa)
    before = [(r.id, r.screenshot_id) for r in repo.list_source_references(aid)]
    ad = Recording()
    for _ in range(2):
        out, _ = _redo(s, wa, v, aid, ad, "x")
        assert out.operation.status == "successful"
    assert [(r.id, r.screenshot_id) for r in repo.list_source_references(aid)] == before
    assert [sid for _, sid in before] == order
    assert [[i for i, _b, _m in imgs] for imgs, _ in ad.study_calls] == [order, order]  # order preserved
    assert json.loads(_versions(s, aid)[-1].content_json)["references"] == order
    assert _counts(s, wa)[0] == 1  # no new artifact


@pytest.mark.parametrize(
    "steps",
    list(product(("edit", "redo"), repeat=3)),
)
def test_study_note_provenance_survives_mixed_manual_edits_and_redos(ctx, steps):
    """P16: artifact-level references outlive every retained note version.

    The complete set of three-step edit/Redo sequences covers each transition
    in both directions. The references are an ordered artifact-level
    relationship, not client-editable version content.
    """
    s, store, wa, wb = ctx
    ids, vault = _shots(s, store, wa, 4)
    order = [ids[2], ids[0], ids[3], ids[1]]
    aid = StudyNoteService(
        s, wa, adapter=Recording(), vault=vault
    ).run_generation(order, _op(s, wa, "study_note")).artifact_id
    repo = WorkspaceRepository(s, wa)
    refs_before = [(ref.id, ref.screenshot_id) for ref in repo.list_source_references(aid)]
    originals_before = {
        sid: (vault.read_original(sid).data, vault.read_original(sid).sha256)
        for sid in order
    }

    for number, step in enumerate(steps, start=1):
        if step == "edit":
            StudyNoteArtifactService(s, wa).save_edit(
                aid,
                {
                    "topic": f"Manual topic {number}",
                    "main_concepts": ["manual"],
                    "extracted_code": [CODE],
                    "code_explanations": [],
                    "pitfalls": [],
                    "uncertain_content": [],
                },
            )
        else:
            outcome, _ = _redo(s, wa, vault, aid, Recording(), "keep sources")
            assert outcome.operation.status == "successful"

        assert [(ref.id, ref.screenshot_id) for ref in repo.list_source_references(aid)] == refs_before

    # Every historical and current version receives the same durable ordered
    # provenance at read/export time, even though its JSON payload differs.
    artifact_service = StudyNoteArtifactService(s, wa)
    for version in _versions(s, aid):
        retained = artifact_service.get_version(aid, version.id)
        assert retained.source_screenshot_ids == order
        markdown = StudyNoteArtifactService.to_markdown(retained)
        assert [f"- Screenshot {sid}" for sid in order] == [
            line for line in markdown.splitlines() if line.startswith("- Screenshot ")
        ]

    # Failed and uncertain Redo attempts cannot disturb a completed artifact's
    # provenance, and foreign workspaces cannot inspect it.
    for adapter, status in (
        (Recording(raises=AIAdapterError("synthetic failure")), "failed"),
        (Recording(mode=FakeAIAdapterMode.UNCERTAIN), "uncertain"),
    ):
        outcome, _ = _redo(s, wa, vault, aid, adapter, "x")
        assert outcome.operation.status == status
        assert [(ref.id, ref.screenshot_id) for ref in repo.list_source_references(aid)] == refs_before
    with pytest.raises(CrossWorkspaceError):
        WorkspaceRepository(s, wb).list_source_references(aid)

    # Archive/restore is a logical screenshot state change only; it leaves the
    # artifact relationship and immutable original bytes intact.
    assert vault.archive(order[0]).state == "archived"
    assert vault.restore(order[0]).state == "active"
    assert [(ref.id, ref.screenshot_id) for ref in repo.list_source_references(aid)] == refs_before
    for sid, expected in originals_before.items():
        original = vault.read_original(sid)
        assert (original.data, original.sha256) == expected


def test_original_bytes_are_untouched_by_redo(ctx):
    s, store, wa, _ = ctx
    aid, order, v = _make_note(s, store, wa)
    snaps = []
    for sid in order:
        blob = s.get(models.ContentBlob, s.get(models.Screenshot, sid).content_blob_id)
        p = store.root / blob.storage_path
        snaps.append((p, p.read_bytes(), blob.sha256, os.stat(p).st_mtime_ns))
    _redo(s, wa, v, aid, Recording(), "x")
    for p, data, sha, mtime in snaps:
        assert p.read_bytes() == data and hashlib.sha256(data).hexdigest() == sha
        assert os.stat(p).st_mode & 0o777 == 0o444 and os.stat(p).st_mtime_ns == mtime


# ---- consent (re-checked at execution) --------------------------------------

def test_consent_revoked_after_acceptance_blocks_the_provider(ctx):
    s, store, wa, _ = ctx
    aid, order, v = _make_note(s, store, wa)
    op = _op(s, wa)                                        # accepted...
    ConsentService(s, wa).record_consent(order[1], False)  # ...then one source revoked
    ad = Recording()
    out, _ = _redo(s, wa, v, aid, ad, "x", op)
    assert ad.study_calls == []
    assert out.operation.status == "failed" and out.operation.error_detail == "AI processing consent required"
    assert [r.origin for r in _versions(s, aid)] == ["ai_generate"]


def test_never_consented_screenshots_block_the_provider(ctx):
    s, store, wa, _ = ctx
    ids, v = _shots(s, store, wa, 2, consent=False)
    art = WorkspaceRepository(s, wa).create_artifact(kind="study_note")
    for sid in ids:
        WorkspaceRepository(s, wa).add_source_reference(artifact_id=art.id, screenshot_id=sid)
    ArtifactVersionService(s, wa).create_first_version(art.id, json.dumps({"topic": "t"}))
    s.commit()
    ad = Recording()
    out, _ = _redo(s, wa, v, art.id, ad, "x")
    assert ad.study_calls == [] and out.operation.status == "failed"


# ---- isolation / targeting ---------------------------------------------------

def test_foreign_missing_and_wrong_kind_artifacts_cannot_be_redone(ctx):
    s, store, wa, wb = ctx
    aid, order, _ = _make_note(s, store, wa)
    other_ids, vb = _shots(s, store, wb, 1)
    b_class = WorkspaceRepository(s, wb).create_artifact(kind="classification")
    s.commit()
    before_a = _counts(s, wa)
    for target in (aid, 987654, b_class.id):
        ad = Recording()
        out, _ = _redo(s, wb, vb, target, ad, "x")
        assert ad.study_calls == []
        assert out.operation.status == "failed" and out.operation.error_detail == "Not found"
    assert _counts(s, wa) == before_a                       # A untouched
    assert [r.origin for r in _versions(s, aid)] == ["ai_generate"]
    assert _counts(s, wb)[1] == 0                           # nothing written for B either


# ---- invalid input / provider failures ---------------------------------------

@pytest.mark.parametrize("bad", ["x" * 5000, "a\x00b", 123])
def test_invalid_instructions_at_execution_fail_without_a_provider_call(ctx, bad):
    s, store, wa, _ = ctx
    aid, _, v = _make_note(s, store, wa)
    ad = Recording()
    out, _ = _redo(s, wa, v, aid, ad, bad)
    assert ad.study_calls == [] and out.operation.status == "failed"
    assert out.operation.error_detail == "Invalid instructions"
    assert [r.origin for r in _versions(s, aid)] == ["ai_generate"]


@pytest.mark.parametrize("raises", [AIAdapterError("boom"), AIAdapterTimeoutError("slow"), RuntimeError("weird")])
def test_provider_errors_and_timeouts_create_no_version(ctx, raises):
    s, store, wa, _ = ctx
    aid, _, v = _make_note(s, store, wa)
    cur = s.get(models.Artifact, aid).current_version_id
    ad = Recording(raises=raises)
    out, _ = _redo(s, wa, v, aid, ad, "x")
    assert len(ad.study_calls) == 1                         # exactly one call, no retry
    assert out.operation.status == "failed" and out.operation.error_detail == "AI analysis failed"
    assert [r.origin for r in _versions(s, aid)] == ["ai_generate"]
    assert s.get(models.Artifact, aid).current_version_id == cur


def test_malformed_model_output_is_rejected_with_no_version(ctx):
    s, store, wa, _ = ctx
    aid, _, v = _make_note(s, store, wa)
    out, _ = _redo(s, wa, v, aid, Recording(mode=FakeAIAdapterMode.INVALID), "x")
    assert out.operation.status == "failed" and out.operation.error_detail == "AI output failed validation"
    assert len(_versions(s, aid)) == 1


@pytest.mark.parametrize("label,make", [
    ("fabricated", lambda im: StudyNoteResult(topic="T", references=[i for i, _b, _m in im] + [999999])),
    ("missing", lambda im: StudyNoteResult(topic="T", references=[im[0][0]])),
    ("reordered", lambda im: StudyNoteResult(topic="T", references=list(reversed([i for i, _b, _m in im])))),
    ("empty", lambda im: StudyNoteResult(topic="T", references=[])),
])
def test_wrong_source_references_in_output_are_rejected(ctx, label, make):
    s, store, wa, _ = ctx
    aid, _, v = _make_note(s, store, wa)
    out, _ = _redo(s, wa, v, aid, Recording(study=make), "x")
    assert out.operation.status == "failed" and out.operation.error_detail == "AI output references invalid"
    assert len(_versions(s, aid)) == 1


def test_uncertain_redo_is_persisted_and_flagged_like_initial_generation(ctx):
    s, store, wa, _ = ctx
    aid, _, v = _make_note(s, store, wa)
    out, _ = _redo(s, wa, v, aid, Recording(mode=FakeAIAdapterMode.UNCERTAIN), "x")
    assert out.operation.status == "uncertain"
    rows = _versions(s, aid)
    assert [r.origin for r in rows] == ["ai_generate", "ai_redo"]
    assert json.loads(rows[1].content_json)["uncertain_content"]


# ---- atomicity ---------------------------------------------------------------

@pytest.mark.parametrize("target", ["append", "pointer"])
def test_persistence_failure_rolls_back_the_version_and_keeps_the_pointer(ctx, monkeypatch, target):
    s, store, wa, _ = ctx
    aid, _, v = _make_note(s, store, wa)
    _edit_note(s, wa, aid)
    cur = s.get(models.Artifact, aid).current_version_id
    snap = [(r.id, r.content_json) for r in _versions(s, aid)]
    if target == "append":
        monkeypatch.setattr(ArtifactVersionService, "append_version",
                            lambda self, *a, **k: (_ for _ in ()).throw(RuntimeError("insert failed")))
    else:
        monkeypatch.setattr(WorkspaceRepository, "set_current_version",
                            lambda self, a, b: (_ for _ in ()).throw(RuntimeError("pointer failed")))
    out, _ = _redo(s, wa, v, aid, Recording(), "x")
    monkeypatch.undo()
    assert out.operation.status == "failed"                 # never a false success
    assert [(r.id, r.content_json) for r in _versions(s, aid)] == snap
    assert s.get(models.Artifact, aid).current_version_id == cur


def test_commit_failure_rolls_back_everything(ctx, monkeypatch):
    s, store, wa, _ = ctx
    aid, _, v = _make_note(s, store, wa)
    real, state = s.commit, {"fail": True}

    def flaky():
        # Fail ONLY the atomic completion commit: the one that would carry the new
        # (still uncommitted) version. Earlier heartbeat commits pass through.
        pending = s.scalar(select(func.count()).select_from(models.ArtifactVersion)
                           .where(models.ArtifactVersion.artifact_id == aid))
        if state["fail"] and pending > 1:
            state["fail"] = False
            raise RuntimeError("commit failed")
        return real()

    monkeypatch.setattr(s, "commit", flaky)
    out, _ = _redo(s, wa, v, aid, Recording(), "x")
    monkeypatch.setattr(s, "commit", real)
    assert out.operation.status == "failed" and len(_versions(s, aid)) == 1


def test_a_safe_retry_after_failure_is_a_new_operation_and_new_version(ctx):
    s, store, wa, _ = ctx
    aid, _, v = _make_note(s, store, wa)
    bad, op1 = _redo(s, wa, v, aid, Recording(raises=AIAdapterError("x")), "x")
    good, op2 = _redo(s, wa, v, aid, Recording(), "x")
    assert op1 != op2 and bad.operation.status == "failed" and good.operation.status == "successful"
    assert [r.origin for r in _versions(s, aid)] == ["ai_generate", "ai_redo"]


def test_duplicate_delivery_of_one_redo_operation_calls_the_provider_once(ctx):
    s, store, wa, _ = ctx
    aid, _, v = _make_note(s, store, wa)
    op, ad = _op(s, wa), Recording()
    svc = StudyNoteService(s, wa, adapter=ad, vault=v)
    first, second = svc.run_redo(aid, op, "x"), svc.run_redo(aid, op, "x")
    assert len(ad.study_calls) == 1 and first.operation.status == second.operation.status == "successful"
    assert len(_versions(s, aid)) == 2


def test_concurrent_duplicate_study_redo_delivery_calls_provider_once(tmp_path):
    """A second worker waits before the provider boundary, not only persistence.

    The blocking adapter keeps the first worker at the exact external-call
    boundary. Without the operation execution gate the second worker would also
    enter ``study_note`` while the first is blocked; with it, it observes the
    first worker's terminal operation after the gate is released.
    """
    engine = create_engine(
        f"sqlite:///{tmp_path / 'redo-concurrency.sqlite'}",
        connect_args={"check_same_thread": False, "timeout": 5},
        poolclass=NullPool,
    )
    Base.metadata.create_all(engine)
    store = FileStore(tmp_path / "storage")
    with Session(engine) as setup:
        workspace = models.Workspace(token_hash="d" * 64)
        setup.add(workspace)
        setup.commit()
        workspace_id = workspace.id
        artifact_id, _, _ = _make_note(setup, store, workspace_id)
        operation_id = _op(setup, workspace_id)

    class BlockingAdapter:
        def __init__(self) -> None:
            self._fake = FakeAIAdapter()
            self.calls = 0
            self.lock = Lock()
            self.first_entered = Event()
            self.second_entered = Event()
            self.release = Event()

        def study_note(self, images, **kwargs):
            with self.lock:
                self.calls += 1
                if self.calls == 1:
                    self.first_entered.set()
                else:
                    self.second_entered.set()
            assert self.release.wait(timeout=5)
            return self._fake.study_note(images, **kwargs)

        def classify_and_epitaph(self, *args, **kwargs):  # pragma: no cover - wrong path
            raise AssertionError("classification was not requested")

    adapter = BlockingAdapter()
    second_started = Event()
    outcomes = []

    def worker(mark_second: bool) -> None:
        with Session(engine) as session:
            if mark_second:
                second_started.set()
            vault = VaultService(session, workspace_id, file_store=store)
            outcome = StudyNoteService(
                session, workspace_id, adapter=adapter, vault=vault
            ).run_redo(artifact_id, operation_id, "x")
            outcomes.append(outcome.operation.status)

    first = Thread(target=worker, args=(False,))
    first.start()
    assert adapter.first_entered.wait(timeout=5)
    second = Thread(target=worker, args=(True,))
    second.start()
    assert second_started.wait(timeout=5)
    assert not adapter.second_entered.wait(timeout=0.5)
    adapter.release.set()
    first.join(timeout=10)
    second.join(timeout=10)
    assert not first.is_alive() and not second.is_alive()
    assert adapter.calls == 1
    assert outcomes == ["successful", "successful"]

    with Session(engine) as verify:
        assert [(v.version_number, v.origin) for v in _versions(verify, artifact_id)] == [
            (1, "ai_generate"),
            (2, "ai_redo"),
        ]


@pytest.mark.parametrize("kind", ["study_note", "classify"])
def test_a_non_redo_operation_never_reaches_the_provider(ctx, kind):
    s, store, wa, _ = ctx
    aid, _, v = _make_note(s, store, wa)
    ad = Recording()
    out = StudyNoteService(s, wa, adapter=ad, vault=v).run_redo(aid, _op(s, wa, kind), "x")
    assert ad.study_calls == [] and out.operation.status == "pending"
    assert len(_versions(s, aid)) == 1


# ======================================================= Classification + Epitaph

def _classified(s, store, wid, color=(5, 6, 7)):
    v = VaultService(s, wid, file_store=store)
    sid = v.upload(png(color), filename="a.png").screenshot_id
    ConsentService(s, wid).record_consent(sid, True)
    out = ClassificationService(s, wid, adapter=Recording(), vault=v).run_classification(
        sid, _op(s, wid, "classify"))
    assert out.operation.status == "successful"
    return sid, out.classification_artifact_id, out.epitaph_artifact_id, v


def _redo_analysis(s, wid, v, sid, adapter, instructions=None, op=None):
    op = op or _op(s, wid)
    return ClassificationService(s, wid, adapter=adapter, vault=v).run_redo(sid, op, instructions), op


def test_analysis_redo_after_manual_edit_appends_to_both_and_retains_the_edit(ctx):
    s, store, wa, _ = ctx
    sid, cid, eid, v = _classified(s, store, wa)
    AnalysisService(s, wa).save_edit(sid, {"category": "Memory/Entertainment", "epitaph_text": "my caption"})
    snap = {a: [(r.id, r.origin, r.content_json) for r in _versions(s, a)] for a in (cid, eid)}

    ad = Recording()
    out, op = _redo_analysis(s, wa, v, sid, ad, "focus on code")

    assert out.operation.status == "successful" and len(ad.class_calls) == 1  # ONE call, both artifacts
    assert ad.class_calls[0][1] == {"instructions": "focus on code"}
    for a in (cid, eid):
        rows = _versions(s, a)
        assert [r.origin for r in rows] == ["ai_generate", "manual_edit", "ai_redo"]
        assert [(r.id, r.origin, r.content_json) for r in rows[:2]] == snap[a]  # edit untouched
        assert s.get(models.Artifact, a).current_version_id == rows[2].id
    assert json.loads(_versions(s, cid)[1].content_json)["category"] == "Memory/Entertainment"
    assert json.loads(_versions(s, eid)[1].content_json)["text"] == "my caption"
    view = AnalysisService(s, wa).get_analysis(sid)
    assert view.classification_origin == view.epitaph_origin == "ai_redo"
    assert "custom instructions" in view.epitaph_text            # instructions honoured (synthetic marker)
    row = s.get(models.Operation, op)
    assert (row.kind, row.status, row.artifact_id) == ("redo", "successful", cid)


def test_analysis_redo_keeps_provenance_and_adds_no_artifacts(ctx):
    s, store, wa, _ = ctx
    sid, cid, eid, v = _classified(s, store, wa)
    before = _counts(s, wa)
    refs = [(r.artifact_id, r.screenshot_id) for r in s.scalars(select(models.SourceReference)).all()]
    for _ in range(2):
        _redo_analysis(s, wa, v, sid, Recording(), None)
    assert _counts(s, wa)[0] == before[0] and _counts(s, wa)[2] == before[2]
    assert [(r.artifact_id, r.screenshot_id) for r in s.scalars(select(models.SourceReference)).all()] == refs


def test_analysis_and_epitaph_provenance_survive_manual_edit_and_redo(ctx):
    """P16 for both classification artifact families, including a failed Redo."""
    s, store, wa, wb = ctx
    sid, classification_id, epitaph_id, vault = _classified(s, store, wa)
    repo = WorkspaceRepository(s, wa)
    refs_before = {
        artifact_id: [(ref.id, ref.screenshot_id) for ref in repo.list_source_references(artifact_id)]
        for artifact_id in (classification_id, epitaph_id)
    }
    original_before = vault.read_original(sid)

    AnalysisService(s, wa).save_edit(
        sid, {"category": "Memory/Entertainment", "epitaph_text": "A manual caption"}
    )
    success, _ = _redo_analysis(s, wa, vault, sid, Recording(), "try another reading")
    assert success.operation.status == "successful"
    failed, _ = _redo_analysis(
        s, wa, vault, sid, Recording(raises=AIAdapterError("synthetic failure")), "x"
    )
    assert failed.operation.status == "failed"

    for artifact_id, expected in refs_before.items():
        assert [(ref.id, ref.screenshot_id) for ref in repo.list_source_references(artifact_id)] == expected
        assert expected == [(expected[0][0], sid)]
        with pytest.raises(CrossWorkspaceError):
            WorkspaceRepository(s, wb).list_source_references(artifact_id)

    assert vault.archive(sid).state == "archived"
    assert vault.restore(sid).state == "active"
    original_after = vault.read_original(sid)
    assert (original_after.data, original_after.sha256) == (
        original_before.data,
        original_before.sha256,
    )


def test_analysis_redo_provider_sees_only_the_original_and_instructions(ctx):
    s, store, wa, _ = ctx
    sid, cid, eid, v = _classified(s, store, wa)
    AnalysisService(s, wa).save_edit(sid, {"epitaph_text": "PRIVATE-EDIT-TEXT"})
    ad = Recording()
    _redo_analysis(s, wa, v, sid, ad, "x")
    (data, mime), kw = ad.class_calls[0]
    assert data == v.read_original(sid).data and set(kw) == {"instructions"}
    assert "PRIVATE-EDIT-TEXT" not in repr(ad.class_calls)


def test_analysis_redo_consent_revoked_blocks_the_provider(ctx):
    s, store, wa, _ = ctx
    sid, cid, eid, v = _classified(s, store, wa)
    op = _op(s, wa)
    ConsentService(s, wa).record_consent(sid, False)
    ad = Recording()
    out, _ = _redo_analysis(s, wa, v, sid, ad, "x", op)
    assert ad.class_calls == [] and out.operation.status == "failed"
    assert len(_versions(s, cid)) == len(_versions(s, eid)) == 1


def test_analysis_redo_foreign_or_unanalysed_screenshot_fails_generically(ctx):
    s, store, wa, wb = ctx
    sid, cid, eid, _ = _classified(s, store, wa)
    other, vb = _shots(s, store, wb, 1)
    for ws, target, vault in ((wb, sid, vb), (wb, other[0], vb), (wb, 987654, vb)):
        ad = Recording()
        out, _ = _redo_analysis(s, ws, vault, target, ad, "x")
        assert ad.class_calls == [] and out.operation.status == "failed"
        assert out.operation.error_detail == "Not found"
    assert len(_versions(s, cid)) == len(_versions(s, eid)) == 1


def test_analysis_redo_invalid_instructions_and_provider_failures(ctx):
    s, store, wa, _ = ctx
    sid, cid, eid, v = _classified(s, store, wa)
    ad = Recording()
    out, _ = _redo_analysis(s, wa, v, sid, ad, "a\x00b")
    assert ad.class_calls == [] and out.operation.error_detail == "Invalid instructions"
    for raises in (AIAdapterError("x"), AIAdapterTimeoutError("t")):
        ad = Recording(raises=raises)
        out, _ = _redo_analysis(s, wa, v, sid, ad, "x")
        assert len(ad.class_calls) == 1 and out.operation.status == "failed"
    out, _ = _redo_analysis(s, wa, v, sid, Recording(mode=FakeAIAdapterMode.INVALID), "x")
    assert out.operation.error_detail == "AI output failed validation"
    assert len(_versions(s, cid)) == len(_versions(s, eid)) == 1


def test_analysis_uncertain_redo_persists_nothing_and_keeps_current_versions(ctx):
    s, store, wa, _ = ctx
    sid, cid, eid, v = _classified(s, store, wa)
    cur = [s.get(models.Artifact, a).current_version_id for a in (cid, eid)]
    out, _ = _redo_analysis(s, wa, v, sid, Recording(mode=FakeAIAdapterMode.UNCERTAIN), "x")
    assert out.operation.status == "uncertain"
    assert len(_versions(s, cid)) == len(_versions(s, eid)) == 1
    assert [s.get(models.Artifact, a).current_version_id for a in (cid, eid)] == cur


def test_analysis_redo_second_append_failure_rolls_back_the_first(ctx, monkeypatch):
    """Classification + Epitaph are atomic (P14): a failure after the first append
    must leave NEITHER artifact with a new version."""
    s, store, wa, _ = ctx
    sid, cid, eid, v = _classified(s, store, wa)
    cur = [s.get(models.Artifact, a).current_version_id for a in (cid, eid)]
    real, n = ArtifactVersionService.append_version, {"c": 0}

    def second_fails(self, *a, **k):
        n["c"] += 1
        if n["c"] == 2:
            raise RuntimeError("epitaph insert failed")
        return real(self, *a, **k)

    monkeypatch.setattr(ArtifactVersionService, "append_version", second_fails)
    out, _ = _redo_analysis(s, wa, v, sid, Recording(), "x")
    monkeypatch.undo()
    assert n["c"] == 2 and out.operation.status == "failed"
    assert len(_versions(s, cid)) == len(_versions(s, eid)) == 1
    assert [s.get(models.Artifact, a).current_version_id for a in (cid, eid)] == cur


def test_analysis_redo_duplicate_delivery_calls_the_provider_once(ctx):
    s, store, wa, _ = ctx
    sid, cid, eid, v = _classified(s, store, wa)
    op, ad = _op(s, wa), Recording()
    svc = ClassificationService(s, wa, adapter=ad, vault=v)
    svc.run_redo(sid, op, "x")
    svc.run_redo(sid, op, "x")
    assert len(ad.class_calls) == 1 and len(_versions(s, cid)) == 2


def test_concurrent_duplicate_analysis_redo_delivery_calls_provider_once(tmp_path):
    """The execution gate covers Classification + Epitaph Redo too.

    The first worker remains inside the actual adapter call while the second
    enters ``run_redo``. A second adapter entry would set ``second_entered``;
    the gate keeps it blocked until the first worker commits its terminal
    status, after which it returns that completed state without another call.
    """
    engine = create_engine(
        f"sqlite:///{tmp_path / 'analysis-redo-concurrency.sqlite'}",
        connect_args={"check_same_thread": False, "timeout": 5},
        poolclass=NullPool,
    )
    Base.metadata.create_all(engine)
    store = FileStore(tmp_path / "storage")
    with Session(engine) as setup:
        workspace = models.Workspace(token_hash="e" * 64)
        setup.add(workspace)
        setup.commit()
        workspace_id = workspace.id
        screenshot_id, classification_id, epitaph_id, _ = _classified(
            setup, store, workspace_id
        )
        operation_id = _op(setup, workspace_id)

    class BlockingAdapter:
        def __init__(self) -> None:
            self._fake = FakeAIAdapter()
            self.calls = 0
            self.lock = Lock()
            self.first_entered = Event()
            self.second_entered = Event()
            self.release = Event()

        def classify_and_epitaph(self, image_bytes, mime, **kwargs):
            with self.lock:
                self.calls += 1
                if self.calls == 1:
                    self.first_entered.set()
                else:
                    self.second_entered.set()
            assert self.release.wait(timeout=5)
            return self._fake.classify_and_epitaph(image_bytes, mime, **kwargs)

        def study_note(self, *args, **kwargs):  # pragma: no cover - wrong path
            raise AssertionError("study note was not requested")

    adapter = BlockingAdapter()
    second_started = Event()
    outcomes = []

    def worker(mark_second: bool) -> None:
        with Session(engine) as session:
            if mark_second:
                second_started.set()
            vault = VaultService(session, workspace_id, file_store=store)
            outcome = ClassificationService(
                session, workspace_id, adapter=adapter, vault=vault
            ).run_redo(screenshot_id, operation_id, "x")
            outcomes.append(outcome.operation.status)

    first = Thread(target=worker, args=(False,))
    first.start()
    assert adapter.first_entered.wait(timeout=5)
    second = Thread(target=worker, args=(True,))
    second.start()
    assert second_started.wait(timeout=5)
    assert not adapter.second_entered.wait(timeout=0.5)
    adapter.release.set()
    first.join(timeout=10)
    second.join(timeout=10)
    assert not first.is_alive() and not second.is_alive()
    assert adapter.calls == 1
    assert outcomes == ["successful", "successful"]

    with Session(engine) as verify:
        for artifact_id in (classification_id, epitaph_id):
            assert [(v.version_number, v.origin) for v in _versions(verify, artifact_id)] == [
                (1, "ai_generate"),
                (2, "ai_redo"),
            ]


def test_analysis_redo_leaves_original_bytes_untouched(ctx):
    s, store, wa, _ = ctx
    sid, cid, eid, v = _classified(s, store, wa)
    blob = s.get(models.ContentBlob, s.get(models.Screenshot, sid).content_blob_id)
    p = store.root / blob.storage_path
    before = p.read_bytes()
    _redo_analysis(s, wa, v, sid, Recording(), "x")
    assert p.read_bytes() == before and hashlib.sha256(before).hexdigest() == blob.sha256
