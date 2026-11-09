"""Classification_Service unit tests (Task 2.5).

Temporary SQLite DB + temporary private storage + synthetic images. Exercises the
real ClassificationService, ConsentService, OperationService, VaultService, and
WorkspaceRepository with injected adapters. Proves: one adapter call, atomic
classification+Epitaph co-persistence, schema-failure -> failed with no partial
artifacts, uncertainty distinguishable, consent re-check at execution, foreign
screenshot non-disclosure, Potentially Sensitive advisory, and immutable bytes
(Req 4.1-4.5, 4.7, 5.1, 5.3; INV-1, INV-4; P12/P13/P14).
"""

from __future__ import annotations

import hashlib
import io
import os

import pytest
from PIL import Image
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.ai.fake_adapter import FakeAIAdapter, FakeAIAdapterMode
from app.ai.schemas import Category, ClassificationResult, EpitaphPayload
from app.db.base import Base
from app.db import models
from app.storage.file_store import FileStore
from app.services.classification_service import ClassificationService
from app.services.consent_service import ConsentService
from app.services.operation_service import OperationService
from app.services.vault_service import VaultService


def png(color=(3, 6, 9)) -> bytes:
    b = io.BytesIO()
    Image.new("RGB", (10, 10), color).save(b, format="PNG")
    return b.getvalue()


class CountingAdapter:
    """Wraps a FakeAIAdapter and counts classify calls; optional category override."""

    def __init__(self, mode=FakeAIAdapterMode.NORMAL, category: Category | None = None):
        self._fake = FakeAIAdapter(mode=mode)
        self._category = category
        self.calls = 0

    def classify_and_epitaph(self, image_bytes: bytes, mime: str):
        self.calls += 1
        result = self._fake.classify_and_epitaph(image_bytes, mime)
        if self._category is not None and isinstance(result, ClassificationResult):
            return ClassificationResult(
                category=self._category,
                confidence=result.confidence,
                uncertain=result.uncertain,
                epitaph=result.epitaph,
            )
        return result

    def study_note(self, images):  # unused here
        return self._fake.study_note(images)


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


def _setup(s, store, wid, *, consent=True, data=None):
    """Upload a screenshot, optionally grant consent, create a pending op."""
    v = VaultService(s, wid, file_store=store)
    sid = v.upload(data or png(), filename="x.png").screenshot_id
    if consent:
        ConsentService(s, wid).record_consent(sid, True)
    op = OperationService(s, wid).create_pending("classify")
    return sid, op.operation_id, v


def _counts(s, wid):
    def c(model):
        return s.scalar(
            select(func.count()).select_from(model).where(model.workspace_id == wid)
        )
    return c(models.Artifact), c(models.ArtifactVersion), c(models.SourceReference)


def test_authorized_classify_calls_adapter_exactly_once_and_succeeds(ctx):
    s, store, wa, _ = ctx
    sid, op_id, v = _setup(s, store, wa)
    adapter = CountingAdapter()
    svc = ClassificationService(s, wa, adapter=adapter, vault=v)
    outcome = svc.run_classification(sid, op_id)
    assert adapter.calls == 1
    assert outcome.operation.status == "successful"
    # Two artifacts (classification + epitaph), two v1 versions, two source refs.
    arts, vers, refs = _counts(s, wa)
    assert (arts, vers, refs) == (2, 2, 2)
    # Both artifacts point at a current version and reference the screenshot.
    for aid in (outcome.classification_artifact_id, outcome.epitaph_artifact_id):
        art = s.get(models.Artifact, aid)
        assert art.current_version_id is not None
        srefs = s.scalars(select(models.SourceReference).where(
            models.SourceReference.artifact_id == aid)).all()
        assert [r.screenshot_id for r in srefs] == [sid]


def test_schema_failure_marks_failed_with_no_partial_artifacts(ctx):
    s, store, wa, _ = ctx
    sid, op_id, v = _setup(s, store, wa)
    adapter = CountingAdapter(mode=FakeAIAdapterMode.INVALID)
    svc = ClassificationService(s, wa, adapter=adapter, vault=v)
    outcome = svc.run_classification(sid, op_id)
    assert adapter.calls == 1
    assert outcome.operation.status == "failed"
    # NO classification/epitaph artifacts persisted (P12).
    assert _counts(s, wa) == (0, 0, 0)


def test_uncertain_result_is_distinguishable_and_persists_no_artifacts(ctx):
    s, store, wa, _ = ctx
    sid, op_id, v = _setup(s, store, wa)
    adapter = CountingAdapter(mode=FakeAIAdapterMode.UNCERTAIN)
    svc = ClassificationService(s, wa, adapter=adapter, vault=v)
    outcome = svc.run_classification(sid, op_id)
    assert outcome.operation.status == "uncertain"  # not failed, not successful
    assert _counts(s, wa) == (0, 0, 0)  # P14: neither persisted


def test_no_consent_blocks_adapter_and_fails(ctx):
    s, store, wa, _ = ctx
    sid, op_id, v = _setup(s, store, wa, consent=False)
    adapter = CountingAdapter()
    svc = ClassificationService(s, wa, adapter=adapter, vault=v)
    outcome = svc.run_classification(sid, op_id)
    assert adapter.calls == 0  # adapter NEVER invoked without consent
    assert outcome.operation.status == "failed"
    assert _counts(s, wa) == (0, 0, 0)


def test_consent_revoked_before_execution_blocks_adapter(ctx):
    s, store, wa, _ = ctx
    sid, op_id, v = _setup(s, store, wa, consent=True)
    # Simulate revocation AFTER acceptance but BEFORE the worker runs.
    ConsentService(s, wa).record_consent(sid, False)
    adapter = CountingAdapter()
    svc = ClassificationService(s, wa, adapter=adapter, vault=v)
    outcome = svc.run_classification(sid, op_id)
    assert adapter.calls == 0  # re-check at execution caught the revocation
    assert outcome.operation.status == "failed"


def test_consent_revoked_during_original_read_blocks_adapter(ctx, monkeypatch):
    """The final re-check closes the read-to-adapter revocation window."""
    s, store, wa, _ = ctx
    sid, op_id, vault = _setup(s, store, wa, consent=True)
    original_read = vault.read_original

    def read_then_revoke(screenshot_id):
        original = original_read(screenshot_id)
        ConsentService(s, wa).record_consent(sid, False)
        return original

    monkeypatch.setattr(vault, "read_original", read_then_revoke)
    adapter = CountingAdapter()
    outcome = ClassificationService(s, wa, adapter=adapter, vault=vault).run_classification(
        sid, op_id
    )
    assert adapter.calls == 0
    assert outcome.operation.status == "failed"
    assert _counts(s, wa) == (0, 0, 0)


def test_foreign_screenshot_cannot_be_classified(ctx):
    s, store, wa, wb = ctx
    sid, _, v = _setup(s, store, wa, consent=True)
    # B creates its own pending op and tries to classify A's screenshot id.
    op_b = OperationService(s, wb).create_pending("classify")
    adapter = CountingAdapter()
    vault_b = VaultService(s, wb, file_store=store)
    svc_b = ClassificationService(s, wb, adapter=adapter, vault=vault_b)
    outcome = svc_b.run_classification(sid, op_b.operation_id)
    assert adapter.calls == 0  # foreign screenshot -> no adapter call
    assert outcome.operation.status == "failed"
    # Nothing persisted in B.
    assert _counts(s, wb) == (0, 0, 0)


def test_classification_and_epitaph_committed_together_content(ctx):
    s, store, wa, _ = ctx
    sid, op_id, v = _setup(s, store, wa)
    adapter = CountingAdapter()
    svc = ClassificationService(s, wa, adapter=adapter, vault=v)
    outcome = svc.run_classification(sid, op_id)
    import json
    class_art = s.get(models.Artifact, outcome.classification_artifact_id)
    epi_art = s.get(models.Artifact, outcome.epitaph_artifact_id)
    class_v = s.get(models.ArtifactVersion, class_art.current_version_id)
    epi_v = s.get(models.ArtifactVersion, epi_art.current_version_id)
    cj = json.loads(class_v.content_json)
    ej = json.loads(epi_v.content_json)
    assert cj["category"] in {c.value for c in Category}
    assert set(cj) == {"category", "confidence", "uncertain"}
    # Epitaph persists visible_facts correctly (field name per committed schema).
    assert "visible_facts" in ej and "text" in ej
    assert class_v.version_number == 1 and epi_v.version_number == 1
    assert class_v.origin == "ai_generate" and epi_v.origin == "ai_generate"


def test_persistence_failure_rolls_back_related_changes(ctx, monkeypatch):
    s, store, wa, _ = ctx
    sid, op_id, v = _setup(s, store, wa)
    adapter = CountingAdapter()
    svc = ClassificationService(s, wa, adapter=adapter, vault=v)
    # Force ONLY the atomic completion commit to fail; the fresh failure-commit
    # in the error handler is allowed through so the operation records `failed`.
    real_commit = s.commit
    state = {"fail_next": True}

    def flaky_commit():
        if state["fail_next"]:
            state["fail_next"] = False
            raise RuntimeError("simulated commit failure")
        return real_commit()

    monkeypatch.setattr(s, "commit", flaky_commit)
    outcome = svc.run_classification(sid, op_id)
    monkeypatch.setattr(s, "commit", real_commit)
    # No partial classification/epitaph artifacts survived the rollback (P14).
    assert _counts(s, wa) == (0, 0, 0)
    # Operation did not falsely succeed.
    assert outcome.operation.status == "failed"


@pytest.mark.parametrize(
    "repository_method",
    [
        "create_artifact",
        "add_artifact_version",
        "add_source_reference",
        "set_current_version",
    ],
)
def test_each_atomic_persistence_stage_rolls_back_all_artifacts(
    ctx, monkeypatch, repository_method
):
    """No intermediate classification/Epitaph state survives a stage failure."""
    s, store, wa, _ = ctx
    sid, op_id, vault = _setup(s, store, wa)
    adapter = CountingAdapter()
    from app.db.repository import WorkspaceRepository

    def fail_stage(*args, **kwargs):
        raise RuntimeError(f"simulated {repository_method} failure")

    monkeypatch.setattr(WorkspaceRepository, repository_method, fail_stage)
    outcome = ClassificationService(s, wa, adapter=adapter, vault=vault).run_classification(
        sid, op_id
    )
    assert adapter.calls == 1
    assert outcome.operation.status == "failed"
    assert _counts(s, wa) == (0, 0, 0)


def test_potentially_sensitive_is_advisory_only(ctx):
    s, store, wa, _ = ctx
    sid, op_id, v = _setup(s, store, wa)
    adapter = CountingAdapter(category=Category.POTENTIALLY_SENSITIVE)
    svc = ClassificationService(s, wa, adapter=adapter, vault=v)
    outcome = svc.run_classification(sid, op_id)
    # Persisted as an ordinary successful classification -> advisory, not an
    # automatic deletion/suppression. The screenshot remains active & readable.
    assert outcome.operation.status == "successful"
    import json
    class_art = s.get(models.Artifact, outcome.classification_artifact_id)
    cj = json.loads(s.get(models.ArtifactVersion, class_art.current_version_id).content_json)
    assert cj["category"] == "Potentially Sensitive"
    shot = s.get(models.Screenshot, sid)
    assert shot.state == "active"  # not archived/deleted
    assert VaultService(s, wa, file_store=store).read_original(sid) is not None


def test_original_bytes_unchanged_before_and_after(ctx):
    s, store, wa, _ = ctx
    data = png((11, 22, 33))
    sid, op_id, v = _setup(s, store, wa, data=data)
    blob = s.get(models.ContentBlob, s.get(models.Screenshot, sid).content_blob_id)
    path = store.root / blob.storage_path
    before = path.read_bytes()
    before_sha = hashlib.sha256(before).hexdigest()
    svc = ClassificationService(s, wa, adapter=CountingAdapter(), vault=v)
    svc.run_classification(sid, op_id)
    after = path.read_bytes()
    assert after == before == data
    assert hashlib.sha256(after).hexdigest() == before_sha == blob.sha256
    assert os.stat(path).st_mode & 0o777 == 0o444  # still read-only (INV-1)
