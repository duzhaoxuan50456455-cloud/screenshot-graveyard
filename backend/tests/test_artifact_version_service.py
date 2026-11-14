"""Append-only artifact version boundary tests (Task 3.1; INV-5/INV-6; P15).

Synthetic data, disposable in-memory SQLite, no AI provider. These tests prove the
SINGLE-WRITER semantics of the shared boundary. They do not claim PostgreSQL or
multi-instance concurrency; that is Task 3.7.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import models
from app.db.base import Base
from app.db.models import AppendOnlyViolationError
from app.db.repository import CrossWorkspaceError, WorkspaceRepository
from app.services.artifact_version_service import (
    ArtifactVersionError,
    ArtifactVersionService,
)
from app.services.study_note_artifact_service import StudyNoteArtifactService


def doc(**kw) -> str:
    return json.dumps({"k": "v", **kw}, ensure_ascii=False)


@pytest.fixture()
def ctx():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    with Session(eng) as s:
        wa = models.Workspace(token_hash="a" * 64)
        wb = models.Workspace(token_hash="b" * 64)
        s.add_all([wa, wb])
        s.commit()
        yield s, wa.id, wb.id
    eng.dispose()


def _artifact(s, wid, kind="study_note") -> int:
    art = WorkspaceRepository(s, wid).create_artifact(kind=kind)
    s.commit()
    return art.id


def _rows(s, aid):
    return s.scalars(
        select(models.ArtifactVersion).where(models.ArtifactVersion.artifact_id == aid)
        .order_by(models.ArtifactVersion.version_number)
    ).all()


# ------------------------------------------------------------- creation / append

def test_first_version_is_number_one_stamped_and_pointed(ctx):
    s, wa, _ = ctx
    aid = _artifact(s, wa)
    v = ArtifactVersionService(s, wa).create_first_version(aid, doc(a=1))
    s.commit()
    assert (v.version_number, v.origin, v.workspace_id, v.artifact_id) == (1, "ai_generate", wa, aid)
    assert s.get(models.Artifact, aid).current_version_id == v.id


def test_sequential_appends_are_strictly_increasing_without_gaps(ctx):
    s, wa, _ = ctx
    aid = _artifact(s, wa)
    svc = ArtifactVersionService(s, wa)
    svc.create_first_version(aid, doc(n=1))
    for n in range(2, 8):
        v = svc.append_version(aid, doc(n=n), "manual_edit" if n % 2 else "ai_redo")
        s.commit()
        assert v.version_number == n
        assert s.get(models.Artifact, aid).current_version_id == v.id  # newest
    assert [r.version_number for r in _rows(s, aid)] == [1, 2, 3, 4, 5, 6, 7]
    assert [v.version_number for v in svc.list_versions(aid)] == [1, 2, 3, 4, 5, 6, 7]
    assert svc.get_current_version(aid).version_number == 7


def test_numbering_is_per_artifact(ctx):
    s, wa, _ = ctx
    a1, a2 = _artifact(s, wa), _artifact(s, wa)
    svc = ArtifactVersionService(s, wa)
    svc.create_first_version(a1, doc())
    svc.create_first_version(a2, doc())
    svc.append_version(a1, doc(), "manual_edit")
    svc.append_version(a1, doc(), "manual_edit")
    v = svc.append_version(a2, doc(), "ai_redo")
    s.commit()
    assert v.version_number == 2  # unaffected by a1's three versions


def test_first_version_refuses_an_artifact_that_already_has_versions(ctx):
    s, wa, _ = ctx
    aid = _artifact(s, wa)
    svc = ArtifactVersionService(s, wa)
    svc.create_first_version(aid, doc())
    with pytest.raises(ArtifactVersionError):
        svc.create_first_version(aid, doc())
    assert len(_rows(s, aid)) == 1


@pytest.mark.parametrize("origin,content", [("bogus", doc()), ("manual_edit", "not json"), ("manual_edit", None)])
def test_invalid_origin_or_content_writes_nothing(ctx, origin, content):
    s, wa, _ = ctx
    aid = _artifact(s, wa)
    svc = ArtifactVersionService(s, wa)
    svc.create_first_version(aid, doc())
    with pytest.raises(ArtifactVersionError):
        svc.append_version(aid, content, origin)
    assert len(_rows(s, aid)) == 1


def test_service_never_commits_the_callers_transaction(ctx):
    s, wa, _ = ctx
    aid = _artifact(s, wa)
    svc = ArtifactVersionService(s, wa)
    svc.create_first_version(aid, doc())
    s.commit()
    svc.append_version(aid, doc(), "manual_edit")
    s.rollback()  # caller aborts: the append must vanish with it
    assert [r.version_number for r in _rows(s, aid)] == [1]
    assert s.get(models.Artifact, aid).current_version_id == _rows(s, aid)[0].id


# ------------------------------------------------------------ uniqueness backstop

def test_unique_artifact_version_number_is_enforced_by_the_database(ctx):
    s, wa, _ = ctx
    aid = _artifact(s, wa)
    svc = ArtifactVersionService(s, wa)
    svc.create_first_version(aid, doc())
    s.commit()
    # A stale writer that computed the same number must be rejected, not silently
    # overwrite or duplicate (Task 3.7 adds the retry that lets it complete).
    original = WorkspaceRepository.max_version_number
    # Simulate a writer working from a stale view: it always believes max == 1.
    WorkspaceRepository.max_version_number = lambda self, artifact_id: 1
    try:
        svc.append_version(aid, doc(x=2), "manual_edit")  # believes next=2: correct
        s.commit()
        with pytest.raises(IntegrityError):
            svc.append_version(aid, doc(x=3), "ai_redo")  # believes next=2 again: duplicate
    finally:
        WorkspaceRepository.max_version_number = original
    s.rollback()
    assert [r.version_number for r in _rows(s, aid)] == [1, 2]
    assert json.loads(_rows(s, aid)[1].content_json)["x"] == 2  # first writer's data intact


# ------------------------------------------------------ immutability of history

def test_orm_refuses_to_update_a_retained_version(ctx):
    s, wa, _ = ctx
    aid = _artifact(s, wa)
    v = ArtifactVersionService(s, wa).create_first_version(aid, doc(a="original"))
    s.commit()
    v.content_json = doc(a="tampered")
    with pytest.raises(AppendOnlyViolationError):
        s.flush()
    s.rollback()
    assert json.loads(_rows(s, aid)[0].content_json)["a"] == "original"


@pytest.mark.parametrize("column,value", [("origin", "ai_redo"), ("version_number", 99)])
def test_no_column_of_a_retained_version_can_be_updated(ctx, column, value):
    s, wa, _ = ctx
    aid = _artifact(s, wa)
    v = ArtifactVersionService(s, wa).create_first_version(aid, doc())
    s.commit()
    setattr(v, column, value)
    with pytest.raises(AppendOnlyViolationError):
        s.flush()
    s.rollback()


def test_orm_refuses_to_delete_a_retained_version(ctx):
    s, wa, _ = ctx
    aid = _artifact(s, wa)
    v = ArtifactVersionService(s, wa).create_first_version(aid, doc())
    s.commit()
    s.delete(v)
    with pytest.raises(AppendOnlyViolationError):
        s.flush()
    s.rollback()
    assert len(_rows(s, aid)) == 1


def test_appending_never_changes_earlier_content(ctx):
    s, wa, _ = ctx
    aid = _artifact(s, wa)
    svc = ArtifactVersionService(s, wa)
    svc.create_first_version(aid, doc(body="one"))
    s.commit()
    before = [(r.id, r.version_number, r.origin, r.content_json) for r in _rows(s, aid)]
    for i in range(4):
        svc.append_version(aid, doc(body=f"later{i}"), "manual_edit")
        s.commit()
    after = [(r.id, r.version_number, r.origin, r.content_json) for r in _rows(s, aid)]
    assert after[: len(before)] == before


def test_no_application_code_path_updates_or_deletes_version_rows():
    """Source scan: nothing under app/ assigns .content_json on an existing row or
    issues a bulk UPDATE/DELETE of artifact_version (which would bypass the ORM
    guard). The only writers are the repository INSERT and the model constructor."""
    root = Path(__file__).resolve().parents[1] / "app"
    offenders = []
    patterns = [
        r"\.content_json\s*=(?!=)",
        r"\bupdate\(\s*(models\.)?ArtifactVersion\b",
        r"\bdelete\(\s*(models\.)?ArtifactVersion\b",
        r"UPDATE\s+artifact_version",
        r"DELETE\s+FROM\s+artifact_version",
        r"session\.delete\(\s*\w*version",
    ]
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for pat in patterns:
            for m in re.finditer(pat, text, flags=re.IGNORECASE):
                line = text.count("\n", 0, m.start()) + 1
                offenders.append(f"{path.relative_to(root)}:{line}: {m.group(0)}")
    # The one legitimate hit: the repository constructor keyword `content_json=`
    # is NOT an assignment to an existing row and does not match `.content_json =`.
    assert offenders == []


# ------------------------------------------------------------------ rollback

def test_failed_insert_leaves_no_version_and_keeps_pointer(ctx, monkeypatch):
    s, wa, _ = ctx
    aid = _artifact(s, wa)
    svc = ArtifactVersionService(s, wa)
    first = svc.create_first_version(aid, doc())
    s.commit()

    def boom(self, **kw):
        raise RuntimeError("insert failed")

    monkeypatch.setattr(WorkspaceRepository, "add_artifact_version", boom)
    with pytest.raises(RuntimeError):
        svc.append_version(aid, doc(), "manual_edit")
    monkeypatch.undo()
    s.rollback()
    assert [r.version_number for r in _rows(s, aid)] == [1]
    assert s.get(models.Artifact, aid).current_version_id == first.id


def test_failed_pointer_update_rolls_back_the_inserted_version_atomically(ctx, monkeypatch):
    """The savepoint makes insert+pointer atomic even if the caller does NOT roll
    back the outer transaction: the new row must disappear on its own."""
    s, wa, _ = ctx
    aid = _artifact(s, wa)
    svc = ArtifactVersionService(s, wa)
    first = svc.create_first_version(aid, doc())
    s.commit()
    other = WorkspaceRepository(s, wa).create_artifact(kind="classification")  # unrelated pending work

    def boom(self, artifact_id, version_id):
        raise RuntimeError("pointer failed")

    monkeypatch.setattr(WorkspaceRepository, "set_current_version", boom)
    with pytest.raises(RuntimeError):
        svc.append_version(aid, doc(lost="no"), "manual_edit")
    monkeypatch.undo()
    # NO outer rollback here: only the savepoint protects us.
    assert [r.version_number for r in _rows(s, aid)] == [1]
    assert s.get(models.Artifact, aid).current_version_id == first.id
    assert s.get(models.Artifact, other.id) is not None  # unrelated outer work survives
    # ...and the very next append reuses number 2 (no gap, no orphan).
    v = svc.append_version(aid, doc(), "manual_edit")
    s.commit()
    assert v.version_number == 2


def test_failed_first_version_pointer_leaves_no_orphan(ctx, monkeypatch):
    s, wa, _ = ctx
    aid = _artifact(s, wa)
    monkeypatch.setattr(WorkspaceRepository, "set_current_version",
                        lambda self, a, v: (_ for _ in ()).throw(RuntimeError("x")))
    with pytest.raises(RuntimeError):
        ArtifactVersionService(s, wa).create_first_version(aid, doc())
    monkeypatch.undo()
    assert _rows(s, aid) == []
    assert s.get(models.Artifact, aid).current_version_id is None


def test_manual_edit_service_pointer_failure_leaves_history_unchanged(ctx, monkeypatch):
    s, wa, _ = ctx
    repo = WorkspaceRepository(s, wa)
    aid = _artifact(s, wa)
    note = {"topic": "T", "main_concepts": [], "extracted_code": ["a\n\tb"],
            "code_explanations": [], "pitfalls": [], "uncertain_content": [], "references": []}
    first = ArtifactVersionService(s, wa).create_first_version(aid, json.dumps(note))
    s.commit()
    monkeypatch.setattr(WorkspaceRepository, "set_current_version",
                        lambda self, a, v: (_ for _ in ()).throw(RuntimeError("x")))
    with pytest.raises(RuntimeError):
        StudyNoteArtifactService(s, wa).save_edit(aid, {"topic": "Edited"})
    monkeypatch.undo()
    s.rollback()
    assert [r.version_number for r in _rows(s, aid)] == [1]
    assert repo.get_artifact(aid).current_version_id == first.id


# --------------------------------------------------------------- isolation

def test_foreign_workspace_cannot_read_or_append(ctx):
    s, wa, wb = ctx
    aid = _artifact(s, wa)
    ArtifactVersionService(s, wa).create_first_version(aid, doc(secret="a-only"))
    s.commit()
    b = ArtifactVersionService(s, wb)
    with pytest.raises(CrossWorkspaceError):
        b.append_version(aid, doc(evil=1), "manual_edit")
    with pytest.raises(CrossWorkspaceError):
        b.create_first_version(aid, doc())
    with pytest.raises(CrossWorkspaceError):
        b.list_versions(aid)
    with pytest.raises(CrossWorkspaceError):
        b.get_current_version(aid)
    s.rollback()
    rows = _rows(s, aid)
    assert [(r.version_number, r.workspace_id) for r in rows] == [(1, wa)]
    # missing and foreign ids are indistinguishable
    with pytest.raises(CrossWorkspaceError) as missing:
        b.append_version(987654, doc(), "manual_edit")
    with pytest.raises(CrossWorkspaceError) as foreign:
        b.append_version(aid, doc(), "manual_edit")
    assert str(missing.value) == str(foreign.value)


def test_each_workspace_numbers_and_points_independently(ctx):
    s, wa, wb = ctx
    a, b = _artifact(s, wa), _artifact(s, wb)
    sa, sb = ArtifactVersionService(s, wa), ArtifactVersionService(s, wb)
    sa.create_first_version(a, doc())
    sb.create_first_version(b, doc())
    for _ in range(3):
        sa.append_version(a, doc(), "manual_edit")
    sb.append_version(b, doc(), "ai_redo")
    s.commit()
    assert sa.get_current_version(a).version_number == 4
    assert sb.get_current_version(b).version_number == 2
    assert {r.workspace_id for r in _rows(s, a)} == {wa}
    assert {r.workspace_id for r in _rows(s, b)} == {wb}


# --------------------------------------------------- provenance + regressions

def test_source_references_are_untouched_by_appends(ctx):
    s, wa, _ = ctx
    repo = WorkspaceRepository(s, wa)
    blob = repo.create_content_blob(sha256="c" * 64, byte_size=1, mime_type="image/png", storage_path="p")
    shots = [repo.create_screenshot(content_blob_id=blob.id, original_filename=f"{i}.png").id for i in range(3)]
    aid = _artifact(s, wa)
    for sid in (shots[2], shots[0], shots[1]):
        repo.add_source_reference(artifact_id=aid, screenshot_id=sid)
    svc = ArtifactVersionService(s, wa)
    svc.create_first_version(aid, doc())
    s.commit()
    refs_before = [(r.id, r.screenshot_id) for r in repo.list_source_references(aid)]
    for _ in range(3):
        svc.append_version(aid, doc(), "ai_redo")
        s.commit()
    assert [(r.id, r.screenshot_id) for r in repo.list_source_references(aid)] == refs_before


def test_study_note_edits_append_through_the_shared_boundary_and_history_exports(ctx):
    s, wa, _ = ctx
    aid = _artifact(s, wa)
    code = "def f():\r\n\treturn  1   "
    note = {"topic": "Original", "main_concepts": ["c"], "extracted_code": [code],
            "code_explanations": [], "pitfalls": [], "uncertain_content": [], "references": []}
    ArtifactVersionService(s, wa).create_first_version(aid, json.dumps(note))
    s.commit()
    svc = StudyNoteArtifactService(s, wa)
    v2 = svc.save_edit(aid, {"topic": "Second", "extracted_code": [code]})
    v3 = svc.save_edit(aid, {"topic": "Third", "extracted_code": [code]})
    assert (v2.version_number, v3.version_number) == (2, 3)
    assert [(v["versionNumber"], v["origin"], v["isCurrent"]) for v in svc.list_versions(aid)] == [
        (1, "ai_generate", False), (2, "manual_edit", False), (3, "manual_edit", True)]
    first_id = svc.list_versions(aid)[0]["versionId"]
    old = svc.get_version(aid, first_id)
    assert old.content.topic == "Original" and old.content.extracted_code == [code]
    assert StudyNoteArtifactService.to_markdown(old).startswith("# Original")
