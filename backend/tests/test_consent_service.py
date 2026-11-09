"""Consent_Service unit tests (Task 2.3).

Temporary SQLite DB + temporary private storage + synthetic images. Exercises the
real ConsentService, WorkspaceRepository, and VaultService (to create owned
screenshots). Proves the consent gate blocks AI paths without consent and never
invokes the adapter, that consent is scoped to the correct screenshot+workspace,
that cross-workspace access is denied generically, and that vault operations work
regardless of consent (Req 9.2, 9.4; P23).

A RecordingAdapter stub (never a real provider, no network) counts invocations so
"no consent => zero adapter calls" is provable.
"""

from __future__ import annotations

import io
import os

import pytest
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db import models
from app.storage.file_store import FileStore
from app.services.vault_service import VaultService
from app.services.consent_service import (
    ConsentRequiredError,
    ConsentService,
    ConsentServiceUnavailableError,
    ScreenshotNotFoundError,
)
from app.ai.schemas import Category, ClassificationResult, EpitaphPayload


def png(color=(3, 6, 9)) -> bytes:
    b = io.BytesIO()
    Image.new("RGB", (10, 10), color).save(b, format="PNG")
    return b.getvalue()


class RecordingAdapter:
    """A stub that counts calls so we can prove the adapter is never invoked
    without consent. Returns schema-valid output; performs no network I/O."""

    def __init__(self) -> None:
        self.calls = 0

    def classify_and_epitaph(self, image_bytes: bytes, mime: str) -> ClassificationResult:
        self.calls += 1
        return ClassificationResult(
            category=Category.REFERENCE_DOCUMENT,
            confidence=0.5,
            uncertain=False,
            epitaph=EpitaphPayload(text="recorded"),
        )


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


def _upload(s, store, wid, data=None) -> int:
    v = VaultService(s, wid, file_store=store)
    return v.upload(data or png(), filename="x.png").screenshot_id


def test_no_consent_require_raises_and_adapter_never_called(ctx):
    s, store, wa, _ = ctx
    sid = _upload(s, store, wa)
    svc = ConsentService(s, wa)
    adapter = RecordingAdapter()
    # require_consent blocks before any adapter work.
    with pytest.raises(ConsentRequiredError):
        svc.require_consent(sid)
    # run_with_consent must NOT invoke the adapter callable without consent.
    with pytest.raises(ConsentRequiredError):
        svc.run_with_consent(sid, lambda: adapter.classify_and_epitaph(png(), "image/png"))
    assert adapter.calls == 0  # P23: adapter never invoked without consent


def test_granted_consent_allows_adapter_and_is_scoped_to_screenshot(ctx):
    s, store, wa, _ = ctx
    sid = _upload(s, store, wa)
    other = _upload(s, store, wa, data=png((1, 1, 1)))
    svc = ConsentService(s, wa)
    svc.record_consent(sid, True)
    adapter = RecordingAdapter()
    # Consent on `sid` authorizes the adapter for `sid`.
    result = svc.run_with_consent(sid, lambda: adapter.classify_and_epitaph(png(), "image/png"))
    assert isinstance(result, ClassificationResult)
    assert adapter.calls == 1
    # Consent is per-screenshot: the OTHER screenshot is still blocked.
    assert svc.has_consent(sid) is True
    assert svc.has_consent(other) is False
    with pytest.raises(ConsentRequiredError):
        svc.require_consent(other)


def test_consent_withdrawal_reblocks(ctx):
    s, store, wa, _ = ctx
    sid = _upload(s, store, wa)
    svc = ConsentService(s, wa)
    svc.record_consent(sid, True)
    assert svc.has_consent(sid) is True
    svc.record_consent(sid, False)  # explicit withdrawal
    assert svc.has_consent(sid) is False
    with pytest.raises(ConsentRequiredError):
        svc.require_consent(sid)


def test_database_error_blocks_adapter_before_its_callback(ctx, monkeypatch):
    """A failed consent lookup must never become an implicit grant (P23)."""
    s, store, wa, _ = ctx
    sid = _upload(s, store, wa)
    svc = ConsentService(s, wa)
    adapter = RecordingAdapter()

    from sqlalchemy.exc import SQLAlchemyError
    from app.db.repository import WorkspaceRepository

    def unavailable(self, screenshot_id):
        raise SQLAlchemyError("database unavailable")

    monkeypatch.setattr(WorkspaceRepository, "get_consent", unavailable)
    with pytest.raises(ConsentServiceUnavailableError):
        svc.run_with_consent(sid, lambda: adapter.classify_and_epitaph(png(), "image/png"))
    assert adapter.calls == 0


def test_consent_is_workspace_scoped_no_cross_workspace_grant(ctx):
    s, store, wa, wb = ctx
    sid = _upload(s, store, wa)  # owned by A
    svc_b = ConsentService(s, wb)
    # B cannot grant consent for A's screenshot: generic not-found, nothing written.
    with pytest.raises(ScreenshotNotFoundError):
        svc_b.record_consent(sid, True)
    # B cannot read it either; has_consent fails closed to False.
    with pytest.raises(ScreenshotNotFoundError):
        svc_b.get_consent(sid)
    assert svc_b.has_consent(sid) is False
    # A still sees no consent (B's attempt wrote nothing).
    svc_a = ConsentService(s, wa)
    assert svc_a.has_consent(sid) is False
    # No consent_record row exists for this screenshot at all.
    from sqlalchemy import select, func
    assert s.scalar(
        select(func.count()).select_from(models.ConsentRecord).where(
            models.ConsentRecord.screenshot_id == sid
        )
    ) == 0


def test_foreign_and_missing_screenshot_indistinguishable(ctx):
    s, store, wa, wb = ctx
    sid = _upload(s, store, wa)
    svc_b = ConsentService(s, wb)
    with pytest.raises(ScreenshotNotFoundError) as foreign:
        svc_b.get_consent(sid)
    with pytest.raises(ScreenshotNotFoundError) as missing:
        svc_b.get_consent(sid + 9999)
    assert str(foreign.value) == str(missing.value)  # no existence disclosure


def test_vault_operations_work_without_consent(ctx):
    s, store, wa, _ = ctx
    v = VaultService(s, wa, file_store=store)
    data = png((5, 5, 5))
    r = v.upload(data, filename="x.png")   # store works, no consent
    sid = r.screenshot_id
    svc = ConsentService(s, wa)
    assert svc.has_consent(sid) is False   # still no consent
    assert v.archive(sid).state == "archived"   # archive works
    assert v.restore(sid).state == "active"      # restore works
    # Original bytes intact (INV-1) despite never consenting.
    assert v.read_original(sid).data == data
