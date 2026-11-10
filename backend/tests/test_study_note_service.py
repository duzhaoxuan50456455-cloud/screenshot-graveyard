"""Study_Note_Service unit tests (Task 2.7).

Real StudyNoteService, ConsentService, OperationService, StudySelectionService,
VaultService and WorkspaceRepository against a temporary SQLite DB and private
temp storage with synthetic images. Adapters are in-test scripted doubles (never
a real provider, no network). The FakeAIAdapter does NOT understand screenshots;
it only supplies deterministic, schema-shaped fixtures.
"""

from __future__ import annotations

import hashlib
import io
import json
import os

import pytest
from PIL import Image
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.ai.adapter import AIAdapterTimeoutError
from app.ai.fake_adapter import FakeAIAdapter, FakeAIAdapterMode
from app.ai.schemas import StudyNoteResult
from app.db import models
from app.db.base import Base
from app.db.repository import WorkspaceRepository
from app.services.consent_service import ConsentService
from app.services.operation_service import OperationService
from app.services.study_note_service import StudyNoteService
from app.services.vault_service import VaultService
from app.storage.file_store import FileStore

SECTIONS = {
    "topic", "main_concepts", "extracted_code", "code_explanations",
    "pitfalls", "uncertain_content", "references",
}

# Deliberately awkward code: tabs, CRLF, trailing spaces, unicode, quotes, backslashes.
TRICKY_CODE = "def f(x):\r\n\treturn  x  # \u03bb \"q\" 'q' \\n {}  \n    pass   "


def png(color) -> bytes:
    b = io.BytesIO()
    Image.new("RGB", (10, 10), color).save(b, format="PNG")
    return b.getvalue()


class ScriptedAdapter:
    """Counts study_note calls; returns a scripted raw value or raises."""

    def __init__(self, make=None, raises=None):
        self._make = make
        self._raises = raises
        self.calls = 0
        self.seen_ids: list[int] = []

    def classify_and_epitaph(self, image_bytes, mime):  # not used
        raise AssertionError("classify must not be called")

    def study_note(self, images):
        self.calls += 1
        self.seen_ids = [i for i, _b, _m in images]
        if self._raises:
            raise self._raises
        return self._make(images)


def good_note(images, **over):
    data = {
        "topic": "C++ functions",
        "main_concepts": ["parameters", "return values"],
        "extracted_code": [TRICKY_CODE],
        "code_explanations": ["Returns its argument."],
        "pitfalls": ["Missing return type."],
        "uncertain_content": [],
        "references": [i for i, _b, _m in images],
    }
    data.update(over)
    return StudyNoteResult.model_validate(data)


@pytest.fixture()
def ctx(tmp_path):
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    store = FileStore(tmp_path / "storage")
    with Session(eng) as s:
        wa = models.Workspace(token_hash="a" * 64)
        wb = models.Workspace(token_hash="b" * 64)
        s.add_all([wa, wb])
        s.commit()
        yield s, store, wa.id, wb.id
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


def _op(s, wid, kind="study_note"):
    return OperationService(s, wid).create_pending(kind).operation_id


def _svc(s, wid, adapter, v):
    return StudyNoteService(s, wid, adapter=adapter, vault=v)


def _counts(s, wid):
    def c(m):
        return s.scalar(select(func.count()).select_from(m).where(m.workspace_id == wid))
    return c(models.Artifact), c(models.ArtifactVersion), c(models.SourceReference)


def test_valid_selection_generates_structurally_valid_note(ctx):
    s, store, wa, _ = ctx
    ids, v = _shots(s, store, wa)
    adapter = ScriptedAdapter(good_note)
    out = _svc(s, wa, adapter, v).run_generation(ids, _op(s, wa))
    assert adapter.calls == 1
    assert out.operation.status == "successful"
    art = s.get(models.Artifact, out.artifact_id)
    assert art.kind == "study_note"
    ver = s.get(models.ArtifactVersion, art.current_version_id)
    content = json.loads(ver.content_json)
    assert set(content) == SECTIONS            # P18: all sections present
    StudyNoteResult.model_validate(content)    # and structurally valid
    assert ver.version_number == 1 and ver.origin == "ai_generate"


def test_selection_order_and_source_references_preserved(ctx):
    s, store, wa, _ = ctx
    ids, v = _shots(s, store, wa, n=4)
    order = [ids[2], ids[0], ids[3], ids[1]]
    adapter = ScriptedAdapter(good_note)
    out = _svc(s, wa, adapter, v).run_generation(order, _op(s, wa))
    assert out.operation.status == "successful"
    assert adapter.seen_ids == order                      # adapter got user order
    art = s.get(models.Artifact, out.artifact_id)
    content = json.loads(s.get(models.ArtifactVersion, art.current_version_id).content_json)
    assert content["references"] == order
    refs = s.scalars(
        select(models.SourceReference).where(models.SourceReference.artifact_id == art.id)
        .order_by(models.SourceReference.id)
    ).all()
    assert [r.screenshot_id for r in refs] == order        # P16/INV-7


def test_any_missing_consent_blocks_whole_batch_zero_adapter_calls(ctx):
    s, store, wa, _ = ctx
    ids, v = _shots(s, store, wa)
    ConsentService(s, wa).record_consent(ids[1], False)
    adapter = ScriptedAdapter(good_note)
    out = _svc(s, wa, adapter, v).run_generation(ids, _op(s, wa))
    assert adapter.calls == 0
    assert out.operation.status == "failed"
    assert _counts(s, wa) == (0, 0, 0)


def test_consent_revoked_before_execution_blocks_adapter(ctx):
    s, store, wa, _ = ctx
    ids, v = _shots(s, store, wa)
    op = _op(s, wa)                               # "accepted"
    ConsentService(s, wa).record_consent(ids[2], False)   # revoked before running
    adapter = ScriptedAdapter(good_note)
    out = _svc(s, wa, adapter, v).run_generation(ids, op)
    assert adapter.calls == 0
    assert out.operation.status == "failed"


def test_consent_revoked_during_final_original_read_blocks_adapter(ctx, monkeypatch):
    """The final all-source re-check closes the read-to-adapter window."""
    s, store, wa, _ = ctx
    ids, vault = _shots(s, store, wa)
    original_read = vault.read_original

    def read_then_revoke(screenshot_id):
        original = original_read(screenshot_id)
        if screenshot_id == ids[-1]:
            ConsentService(s, wa).record_consent(screenshot_id, False)
        return original

    monkeypatch.setattr(vault, "read_original", read_then_revoke)
    adapter = ScriptedAdapter(good_note)
    out = _svc(s, wa, adapter, vault).run_generation(ids, _op(s, wa))
    assert adapter.calls == 0
    assert out.operation.status == "failed"
    assert _counts(s, wa) == (0, 0, 0)


def test_foreign_or_missing_screenshot_cannot_enter_note(ctx):
    s, store, wa, wb = ctx
    ids_a, _ = _shots(s, store, wa, n=2)
    ids_b, vb = _shots(s, store, wb, n=1)
    for bad in ([ids_b[0], ids_a[0]], [ids_b[0], 987654]):
        adapter = ScriptedAdapter(good_note)
        out = _svc(s, wb, adapter, vb).run_generation(bad, _op(s, wb))
        assert adapter.calls == 0
        assert out.operation.status == "failed"
    assert _counts(s, wb) == (0, 0, 0)
    assert _counts(s, wa) == (0, 0, 0)


@pytest.mark.parametrize("label,make", [
    ("fabricated", lambda im: good_note(im, references=[i for i, _b, _m in im] + [999999])),
    ("missing", lambda im: good_note(im, references=[im[0][0]])),
    ("duplicate", lambda im: good_note(im, references=[im[0][0]] * len(im))),
    ("foreign", lambda im: good_note(im, references=[im[0][0], im[1][0], 123456])),
    ("empty", lambda im: good_note(im, references=[])),
])
def test_bad_source_references_fail_with_no_persistence(ctx, label, make):
    s, store, wa, _ = ctx
    ids, v = _shots(s, store, wa)
    adapter = ScriptedAdapter(make)
    out = _svc(s, wa, adapter, v).run_generation(ids, _op(s, wa))
    assert adapter.calls == 1
    assert out.operation.status == "failed"
    assert _counts(s, wa) == (0, 0, 0)


def test_reordered_source_references_fail_without_persistence(ctx):
    s, store, wa, _ = ctx
    ids, vault = _shots(s, store, wa)
    adapter = ScriptedAdapter(lambda images: good_note(images, references=list(reversed(ids))))
    out = _svc(s, wa, adapter, vault).run_generation(ids, _op(s, wa))
    assert adapter.calls == 1
    assert out.operation.status == "failed"
    assert _counts(s, wa) == (0, 0, 0)


def test_model_construct_invalid_instance_is_rejected(ctx):
    """A BaseModel instance is re-validated; model_construct cannot bypass it."""
    s, store, wa, _ = ctx
    ids, v = _shots(s, store, wa)
    bad = lambda im: StudyNoteResult.model_construct(topic="", references=[i for i, _b, _m in im])
    out = _svc(s, wa, ScriptedAdapter(bad), v).run_generation(ids, _op(s, wa))
    assert out.operation.status == "failed"
    assert _counts(s, wa) == (0, 0, 0)


def test_fake_invalid_output_fails_without_partial_persistence(ctx):
    s, store, wa, _ = ctx
    ids, v = _shots(s, store, wa)
    out = _svc(s, wa, FakeAIAdapter(mode=FakeAIAdapterMode.INVALID), v).run_generation(ids, _op(s, wa))
    assert out.operation.status == "failed"
    assert _counts(s, wa) == (0, 0, 0)


def test_adapter_timeout_fails_with_sources_intact(ctx):
    s, store, wa, _ = ctx
    ids, v = _shots(s, store, wa)
    out = _svc(s, wa, ScriptedAdapter(raises=AIAdapterTimeoutError("t")), v).run_generation(ids, _op(s, wa))
    assert out.operation.status == "failed"
    assert _counts(s, wa) == (0, 0, 0)
    for sid in ids:
        assert v.read_original(sid).data


def test_extracted_code_is_character_for_character_faithful(ctx):
    s, store, wa, _ = ctx
    ids, v = _shots(s, store, wa)
    out = _svc(s, wa, ScriptedAdapter(good_note), v).run_generation(ids, _op(s, wa))
    art = s.get(models.Artifact, out.artifact_id)
    content = json.loads(s.get(models.ArtifactVersion, art.current_version_id).content_json)
    assert content["extracted_code"] == [TRICKY_CODE]   # P19, exact incl. \r\n, \t, trailing spaces


def test_uncertain_content_is_explicit_and_status_uncertain(ctx):
    s, store, wa, _ = ctx
    ids, v = _shots(s, store, wa)
    out = _svc(s, wa, FakeAIAdapter(mode=FakeAIAdapterMode.UNCERTAIN), v).run_generation(ids, _op(s, wa))
    assert out.operation.status == "uncertain"          # not successful, not failed
    art = s.get(models.Artifact, out.artifact_id)
    content = json.loads(s.get(models.ArtifactVersion, art.current_version_id).content_json)
    assert content["uncertain_content"]                 # explicitly represented (Req 6.6)
    assert content["references"] == ids
    assert _counts(s, wa) == (1, 1, len(ids))           # persisted atomically with status


def test_artifact_v1_refs_pointer_and_status_commit_together(ctx):
    s, store, wa, _ = ctx
    ids, v = _shots(s, store, wa, n=5)
    op = _op(s, wa)
    out = _svc(s, wa, ScriptedAdapter(good_note), v).run_generation(ids, op)
    assert _counts(s, wa) == (1, 1, 5)
    art = s.get(models.Artifact, out.artifact_id)
    ver = s.get(models.ArtifactVersion, art.current_version_id)
    assert ver.artifact_id == art.id and ver.version_number == 1
    assert s.get(models.Operation, op).status == "successful"


@pytest.mark.parametrize("fail_on_call", [1, 2, 3])
def test_failure_at_intermediate_stage_rolls_back_everything(ctx, monkeypatch, fail_on_call):
    s, store, wa, _ = ctx
    ids, v = _shots(s, store, wa)
    op = _op(s, wa)
    real = WorkspaceRepository.add_source_reference
    n = {"c": 0}

    def flaky(self, **kw):
        n["c"] += 1
        if n["c"] == fail_on_call:
            raise RuntimeError("boom")
        return real(self, **kw)

    monkeypatch.setattr(WorkspaceRepository, "add_source_reference", flaky)
    out = _svc(s, wa, ScriptedAdapter(good_note), v).run_generation(ids, op)
    assert out.operation.status == "failed"             # never false success
    assert _counts(s, wa) == (0, 0, 0)                  # artifact, version AND refs gone


def test_commit_failure_rolls_back_and_never_succeeds(ctx, monkeypatch):
    s, store, wa, _ = ctx
    ids, v = _shots(s, store, wa)
    op = _op(s, wa)
    real_commit = s.commit
    state = {"fail": True}

    def flaky_commit():
        if state["fail"]:
            state["fail"] = False
            raise RuntimeError("commit failed")
        return real_commit()

    monkeypatch.setattr(s, "commit", flaky_commit)
    out = _svc(s, wa, ScriptedAdapter(good_note), v).run_generation(ids, op)
    monkeypatch.setattr(s, "commit", real_commit)
    assert out.operation.status == "failed"
    assert _counts(s, wa) == (0, 0, 0)


def test_original_bytes_and_hashes_unchanged(ctx):
    s, store, wa, _ = ctx
    ids, v = _shots(s, store, wa)
    paths, before = [], []
    for sid in ids:
        blob = s.get(models.ContentBlob, s.get(models.Screenshot, sid).content_blob_id)
        p = store.root / blob.storage_path
        paths.append((p, blob.sha256))
        before.append(p.read_bytes())
    _svc(s, wa, ScriptedAdapter(good_note), v).run_generation(ids, _op(s, wa))
    for (p, sha), b in zip(paths, before):
        assert p.read_bytes() == b
        assert hashlib.sha256(b).hexdigest() == sha
        assert os.stat(p).st_mode & 0o777 == 0o444


def test_duplicate_execution_of_same_operation_is_a_noop(ctx):
    s, store, wa, _ = ctx
    ids, v = _shots(s, store, wa)
    op = _op(s, wa)
    adapter = ScriptedAdapter(good_note)
    svc = _svc(s, wa, adapter, v)
    first = svc.run_generation(ids, op)
    second = svc.run_generation(ids, op)               # duplicate delivery
    assert adapter.calls == 1                          # adapter not re-invoked
    assert second.artifact_id is None
    assert second.operation.status == "successful"
    assert _counts(s, wa) == (1, 1, 3)
    assert first.artifact_id is not None


def test_separate_operations_never_overwrite_prior_notes(ctx):
    s, store, wa, _ = ctx
    ids, v = _shots(s, store, wa)
    a1 = _svc(s, wa, ScriptedAdapter(good_note), v).run_generation(ids, _op(s, wa))
    ver1 = s.get(models.ArtifactVersion, s.get(models.Artifact, a1.artifact_id).current_version_id)
    snapshot = ver1.content_json
    a2 = _svc(s, wa, ScriptedAdapter(lambda im: good_note(im, topic="Second")), v).run_generation(ids, _op(s, wa))
    assert a1.artifact_id != a2.artifact_id
    assert _counts(s, wa) == (2, 2, 6)
    assert s.get(models.ArtifactVersion, ver1.id).content_json == snapshot


def test_non_study_note_operation_never_reaches_adapter(ctx):
    s, store, wa, _ = ctx
    ids, v = _shots(s, store, wa)
    adapter = ScriptedAdapter(good_note)
    out = _svc(s, wa, adapter, v).run_generation(ids, _op(s, wa, kind="classify"))
    assert adapter.calls == 0
    assert out.operation.status == "pending"
