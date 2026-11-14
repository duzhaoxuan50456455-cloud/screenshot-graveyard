"""PBT-AI / completed PBT-ISOLATION (Task 2.11).

Every property below uses real services, a disposable SQLite schema, and the
private FileStore.  Adapters are deliberately scripted, network-free doubles:
the properties exercise the mandatory service boundary validation rather than a
provider.  Each property runs at least 100 Hypothesis examples.
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import base64

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st
from PIL import Image
from pydantic import BaseModel
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.ai.adapter import AIAdapterError
from app.ai.schemas import Category, ClassificationResult, EpitaphPayload, StudyNoteResult
from app.db import models
from app.db.base import Base
from app.services.classification_service import ClassificationService
from app.services.consent_service import ConsentService, ScreenshotNotFoundError as ConsentScreenshotNotFoundError
from app.services.operation_service import OperationNotFoundError, OperationService, OperationTransitionError
from app.services.study_note_service import StudyNoteService
from app.services.vault_service import VaultService
from app.storage.file_store import FileStore

PBT = settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture])


def _image(color: tuple[int, int, int]) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(out, format="PNG")
    return out.getvalue()


def _env(tmp_path_factory):
    root = tmp_path_factory.mktemp("pbtai")
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return root, engine, FileStore(root / "storage")


def _workspace(session: Session, token: str) -> int:
    ws = models.Workspace(token_hash=token)
    session.add(ws)
    session.commit()
    return ws.id


def _counts(session: Session, wid: int) -> tuple[int, int, int]:
    return tuple(
        session.scalar(select(func.count()).select_from(m).where(m.workspace_id == wid))
        for m in (models.Artifact, models.ArtifactVersion, models.SourceReference)
    )


def _cleanup(root, engine):
    for path in root.rglob("*"):
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass
    engine.dispose()


class ClassificationAdapter:
    def __init__(self, raw):
        self.raw, self.calls = raw, 0

    def classify_and_epitaph(self, image_bytes, mime):
        self.calls += 1
        return self.raw

    def study_note(self, images):
        raise AssertionError("not used")


class NoteAdapter:
    def __init__(self, make):
        self.make, self.calls, self.seen = make, 0, []

    def classify_and_epitaph(self, image_bytes, mime):
        raise AssertionError("not used")

    def study_note(self, images):
        self.calls += 1
        self.seen = [sid for sid, _, _ in images]
        return self.make(images)


class RaisingAdapter:
    def __init__(self, detail: str):
        self.detail, self.calls = detail, 0

    def classify_and_epitaph(self, image_bytes, mime):
        self.calls += 1
        raise AIAdapterError(self.detail)

    def study_note(self, images):
        raise AssertionError("not used")


def _classification(category: Category, uncertain: bool = False):
    return ClassificationResult(
        category=category,
        confidence=0.0 if uncertain else 0.7,
        uncertain=uncertain,
        epitaph=EpitaphPayload(text="fixture", visible_facts="fixture", inferred_intent=None, uncertain=uncertain),
    )


def _note(images, *, references=None, uncertain=False):
    return StudyNoteResult(
        topic="fixture", main_concepts=["x"], extracted_code=["\tcode\n"],
        code_explanations=["x"], pitfalls=["x"],
        uncertain_content=["ambiguous"] if uncertain else [],
        references=[sid for sid, _, _ in images] if references is None else references,
    )


def _class_setup(session, store, wid, color, *, consent=True):
    vault = VaultService(session, wid, file_store=store)
    data = _image(color)
    sid = vault.upload(data, filename="fixture.png").screenshot_id
    if consent:
        ConsentService(session, wid).record_consent(sid, True)
    op = OperationService(session, wid).create_pending("classify")
    return vault, sid, op.operation_id, data


# Feature: screenshot-graveyard, Property 12: AI output schema validation gate
@given(category=st.sampled_from(list(Category)), color=st.tuples(st.integers(0, 255), st.integers(0, 255), st.integers(0, 255)), invalid=st.booleans())
@PBT
def test_p12_schema_gate_persists_only_valid_classification(category, color, invalid, tmp_path_factory):
    """P12 — malformed runtime output fails with no artifacts; all six valid
    enum categories pass the real schema boundary."""
    root, engine, store = _env(tmp_path_factory)
    try:
        with Session(engine) as s:
            wid = _workspace(s, "a" * 64)
            vault, sid, opid, original = _class_setup(s, store, wid, color)
            raw = (
                {"category": "invalid", "confidence": 2.0, "uncertain": False, "epitaph": {}}
                if invalid else _classification(category)
            )
            adapter = ClassificationAdapter(raw)
            out = ClassificationService(s, wid, adapter=adapter, vault=vault).run_classification(sid, opid)
            assert adapter.calls == 1
            assert vault.read_original(sid).data == original
            assert out.operation.status == ("failed" if invalid else "successful")
            assert _counts(s, wid) == ((0, 0, 0) if invalid else (2, 2, 2))
    finally:
        _cleanup(root, engine)


# Feature: screenshot-graveyard, Property 12: AI output schema validation gate
@given(malformed=st.sampled_from(["empty_topic", "bad_reference", "extra_field"]), color=st.tuples(st.integers(0, 255), st.integers(0, 255), st.integers(0, 255)))
@PBT
def test_p12_schema_gate_rejects_malformed_study_note_content(malformed, color, tmp_path_factory):
    """P12 — Study Note output also crosses the real runtime schema gate;
    malformed structured content cannot become a persisted artifact."""
    root, engine, store = _env(tmp_path_factory)
    try:
        with Session(engine) as s:
            wid = _workspace(s, "k" * 64)
            vault = VaultService(s, wid, file_store=store)
            sid = vault.upload(_image(color), filename="fixture.png").screenshot_id
            ConsentService(s, wid).record_consent(sid, True)
            raw = {
                "empty_topic": {"topic": "", "references": [sid]},
                "bad_reference": {"topic": "x", "references": ["not-an-id"]},
                "extra_field": {"topic": "x", "references": [sid], "surprise": True},
            }[malformed]
            adapter = NoteAdapter(lambda _images: raw)
            out = StudyNoteService(s, wid, adapter=adapter, vault=vault).run_generation(
                [sid], OperationService(s, wid).create_pending("study_note").operation_id
            )
            assert adapter.calls == 1 and out.operation.status == "failed"
            assert _counts(s, wid) == (0, 0, 0)
    finally:
        _cleanup(root, engine)


# Feature: screenshot-graveyard, Property 13: Uncertainty is represented, never forced to success
@given(kind=st.sampled_from(["classification", "study_note"]), color=st.tuples(st.integers(0, 255), st.integers(0, 255), st.integers(0, 255)))
@PBT
def test_p13_uncertainty_is_persisted_or_terminally_represented(kind, color, tmp_path_factory):
    """P13 — classification uncertainty stores nothing and is terminally
    uncertain; study-note uncertainty is retained with an uncertain terminal."""
    root, engine, store = _env(tmp_path_factory)
    try:
        with Session(engine) as s:
            wid = _workspace(s, "b" * 64)
            if kind == "classification":
                vault, sid, opid, original = _class_setup(s, store, wid, color)
                out = ClassificationService(s, wid, adapter=ClassificationAdapter(_classification(Category.UNKNOWN_NEEDS_REVIEW, True)), vault=vault).run_classification(sid, opid)
                assert out.operation.status == "uncertain" and _counts(s, wid) == (0, 0, 0)
                assert vault.read_original(sid).data == original
            else:
                vault = VaultService(s, wid, file_store=store)
                sid = vault.upload(_image(color), filename="fixture.png").screenshot_id
                ConsentService(s, wid).record_consent(sid, True)
                adapter = NoteAdapter(lambda images: _note(images, uncertain=True))
                out = StudyNoteService(s, wid, adapter=adapter, vault=vault).run_generation([sid], OperationService(s, wid).create_pending("study_note").operation_id)
                assert out.operation.status == "uncertain" and out.artifact_id is not None
                version = s.get(models.ArtifactVersion, s.get(models.Artifact, out.artifact_id).current_version_id)
                assert "ambiguous" in version.content_json and adapter.calls == 1
    finally:
        _cleanup(root, engine)


# Feature: screenshot-graveyard, Property 14: Atomic co-persistence of classification and Epitaph from one AI call
@given(fail_at=st.sampled_from([None, 1, 2]), color=st.tuples(st.integers(0, 255), st.integers(0, 255), st.integers(0, 255)))
@PBT
def test_p14_classification_and_epitaph_are_atomic(fail_at, color, tmp_path_factory):
    """P14 — one adapter call yields exactly both artifacts or neither; source
    bytes and operation evidence remain consistent on a schema failure."""
    root, engine, store = _env(tmp_path_factory)
    try:
        with Session(engine) as s:
            wid = _workspace(s, "c" * 64)
            vault, sid, opid, original = _class_setup(s, store, wid, color)
            adapter = ClassificationAdapter(_classification(Category.REFERENCE_DOCUMENT))
            service = ClassificationService(s, wid, adapter=adapter, vault=vault)
            original_add = service._repo.add_source_reference
            calls = 0
            def fail_selected_reference(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == fail_at:
                    raise RuntimeError("simulated persistence interruption")
                return original_add(*args, **kwargs)
            service._repo.add_source_reference = fail_selected_reference
            out = service.run_classification(sid, opid)
            assert adapter.calls == 1 and vault.read_original(sid).data == original
            assert _counts(s, wid) == ((2, 2, 2) if fail_at is None else (0, 0, 0))
            assert out.operation.status == ("successful" if fail_at is None else "failed")
    finally:
        _cleanup(root, engine)


# Feature: screenshot-graveyard, Property 22: Operation status lifecycle
@given(status=st.sampled_from(["failed", "uncertain", "successful"]))
@PBT
def test_p22_terminal_states_are_allowlisted_and_never_overwritten(status, tmp_path_factory):
    root, engine, store = _env(tmp_path_factory)
    try:
        with Session(engine) as s:
            wid = _workspace(s, "d" * 64)
            ops = OperationService(s, wid)
            op = ops.create_pending("classify")
            assert op.status == "pending"
            terminal = (
                ops.complete_after_persistence(op.operation_id, lambda: None)
                if status == "successful"
                else ops.set_terminal_status(op.operation_id, status)
            )
            assert terminal.status in {"pending", "successful", "failed", "uncertain"}
            with pytest.raises(OperationTransitionError):
                ops.set_terminal_status(op.operation_id, "failed")
            with pytest.raises(ValueError):
                ops.set_terminal_status(op.operation_id, "pending")
            assert ops.get(op.operation_id).status == status
    finally:
        _cleanup(root, engine)


# Feature: screenshot-graveyard, Property 23: Consent gate for AI processing
@given(color=st.tuples(st.integers(0, 255), st.integers(0, 255), st.integers(0, 255)))
@PBT
def test_p23_without_consent_adapter_is_never_called_and_vault_still_works(color, tmp_path_factory):
    root, engine, store = _env(tmp_path_factory)
    try:
        with Session(engine) as s:
            wid = _workspace(s, "e" * 64)
            vault, sid, opid, original = _class_setup(s, store, wid, color, consent=False)
            adapter = ClassificationAdapter(_classification(Category.REFERENCE_DOCUMENT))
            out = ClassificationService(s, wid, adapter=adapter, vault=vault).run_classification(sid, opid)
            assert out.operation.status == "failed" and adapter.calls == 0 and _counts(s, wid) == (0, 0, 0)
            assert vault.archive(sid).state == "archived"
            assert vault.restore(sid).state == "active"
            assert vault.read_original(sid).data == original
    finally:
        _cleanup(root, engine)


# Feature: screenshot-graveyard, Property 24: Workspace isolation and existence non-disclosure
@given(variant=st.sampled_from(["valid", "foreign", "reordered", "duplicate", "missing"]), salt=st.integers(0, 9999))
@PBT
def test_p24_study_note_references_must_be_exact_owned_selection(variant, salt, tmp_path_factory):
    """P24 completion — a foreign/reordered source reference never persists,
    even when A has consent and B owns a real screenshot with that ID."""
    root, engine, store = _env(tmp_path_factory)
    try:
        with Session(engine) as s:
            wa, wb = _workspace(s, f"a{salt:063d}"), _workspace(s, f"b{salt:063d}")
            va, vb = VaultService(s, wa, file_store=store), VaultService(s, wb, file_store=store)
            a1 = va.upload(_image((1, 2, 3)), filename="a1.png").screenshot_id
            a2 = va.upload(_image((4, 5, 6)), filename="a2.png").screenshot_id
            foreign = vb.upload(_image((7, 8, 9)), filename="b.png").screenshot_id
            for sid in (a1, a2): ConsentService(s, wa).record_consent(sid, True)
            selected = [a1, a2]
            refs = {
                "valid": selected,
                "foreign": [foreign, a2],
                "reordered": [a2, a1],
                "duplicate": [a1, a1],
                "missing": [a1],
            }[variant]
            adapter = NoteAdapter(lambda images: _note(images, references=refs))
            out = StudyNoteService(s, wa, adapter=adapter, vault=va).run_generation(selected, OperationService(s, wa).create_pending("study_note").operation_id)
            if variant != "valid":
                assert out.operation.status == "failed" and _counts(s, wa) == (0, 0, 0)
            else:
                assert out.operation.status == "successful" and adapter.seen == selected
                refs_db = s.scalars(select(models.SourceReference).where(models.SourceReference.artifact_id == out.artifact_id).order_by(models.SourceReference.id)).all()
                assert [r.screenshot_id for r in refs_db] == selected
            assert vb.read_original(foreign).data == _image((7, 8, 9))
    finally:
        _cleanup(root, engine)


# Feature: screenshot-graveyard, Property 24: Workspace isolation and existence non-disclosure
@given(color=st.tuples(st.integers(0, 255), st.integers(0, 255), st.integers(0, 255)))
@PBT
def test_p24_foreign_operation_consent_and_classification_cannot_touch_a_records(color, tmp_path_factory):
    """P24 completion — B cannot poll A's operation, inspect A's consent, or
    classify A's screenshot; A's artifact versions and original remain intact."""
    root, engine, store = _env(tmp_path_factory)
    try:
        with Session(engine) as s:
            wa, wb = _workspace(s, "i" * 64), _workspace(s, "j" * 64)
            vault_a, sid, opid, original = _class_setup(s, store, wa, color)
            first = ClassificationService(s, wa, adapter=ClassificationAdapter(_classification(Category.REFERENCE_DOCUMENT)), vault=vault_a).run_classification(sid, opid)
            assert first.operation.status == "successful"
            snapshot = (_counts(s, wa), vault_a.read_original(sid).data)
            with pytest.raises(OperationNotFoundError):
                OperationService(s, wb).get(opid)
            with pytest.raises(ConsentScreenshotNotFoundError):
                ConsentService(s, wb).get_consent(sid)
            foreign_op = OperationService(s, wb).create_pending("classify")
            adapter_b = ClassificationAdapter(_classification(Category.REFERENCE_DOCUMENT))
            out = ClassificationService(s, wb, adapter=adapter_b, vault=VaultService(s, wb, file_store=store)).run_classification(sid, foreign_op.operation_id)
            assert out.operation.status == "failed" and adapter_b.calls == 0
            assert (_counts(s, wa), vault_a.read_original(sid).data) == snapshot
            assert original == snapshot[1]
    finally:
        _cleanup(root, engine)


# Feature: screenshot-graveyard, Property 25: No image bytes or credentials in logs
@given(
    secret=st.text(alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd")), min_size=8, max_size=24),
    color=st.tuples(st.integers(0, 255), st.integers(0, 255), st.integers(0, 255)),
)
@PBT
def test_p25_provider_failures_do_not_log_generated_secret_or_source_bytes(secret, color, tmp_path_factory, caplog):
    """P25 — provider-shaped errors are converted to a safe terminal result;
    neither their credential-like detail nor original image bytes are logged."""
    root, engine, store = _env(tmp_path_factory)
    try:
        with Session(engine) as s:
            wid = _workspace(s, "h" * 64)
            vault, sid, opid, original = _class_setup(s, store, wid, color)
            caplog.set_level(logging.DEBUG)
            encoded = base64.b64encode(original).decode("ascii")
            out = ClassificationService(
                s, wid, adapter=RaisingAdapter(f"api_key={secret}; image_base64={encoded}"), vault=vault
            ).run_classification(sid, opid)
            assert out.operation.status == "failed"
            assert secret not in (out.operation.error_detail or "")
            assert encoded not in (out.operation.error_detail or "")
            assert secret not in caplog.text
            assert encoded not in caplog.text
            assert original.hex() not in caplog.text
    finally:
        _cleanup(root, engine)
