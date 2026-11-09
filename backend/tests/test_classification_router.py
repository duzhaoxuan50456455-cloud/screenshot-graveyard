"""Classification HTTP-path integration tests (Task 2.5).

Real HTTP path via TestClient (BackgroundTasks run synchronously after the
response), temporary SQLite DB + temporary private storage, synthetic images, and
an injected RecordingAdapter (never a real provider). Proves the async
202 + operationId + polling contract, that an unauthorized request returns 403
with ZERO adapter calls, that a successful run commits classification+Epitaph and
polls to `successful`, and that foreign/missing screenshots are non-disclosing.
"""

from __future__ import annotations

import io
import os

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app import config
import app.db.session as sess_mod
from app.db.base import Base
from app.db import models
from app.main import create_app
from app.ratelimit import TokenBucketRateLimiter
from app.ai import provider
from app.ai.fake_adapter import FakeAIAdapter, FakeAIAdapterMode
from app.ai.schemas import ClassificationResult

TRUSTED = config.TRUSTED_ORIGINS[0]
HDRS = {"Origin": TRUSTED, "X-Requested-With": "fetch"}


def png(color=(5, 6, 7)) -> bytes:
    b = io.BytesIO()
    Image.new("RGB", (12, 9), color).save(b, format="PNG")
    return b.getvalue()


class RecordingAdapter:
    def __init__(self, mode=FakeAIAdapterMode.NORMAL):
        self._fake = FakeAIAdapter(mode=mode)
        self.calls = 0

    def classify_and_epitaph(self, image_bytes: bytes, mime: str):
        self.calls += 1
        return self._fake.classify_and_epitaph(image_bytes, mime)

    def study_note(self, images):
        return self._fake.study_note(images)


class UnexpectedFailureAdapter:
    """Simulates a provider implementation bug outside AIAdapterError."""

    def classify_and_epitaph(self, image_bytes: bytes, mime: str):
        raise RuntimeError("unexpected adapter failure")

    def study_note(self, images):
        raise RuntimeError("unexpected adapter failure")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    factory = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False, class_=Session)
    monkeypatch.setattr(sess_mod, "engine", eng)
    monkeypatch.setattr(sess_mod, "SessionLocal", factory)
    monkeypatch.setattr(config, "COOKIE_SECURE", False)
    monkeypatch.setattr(config, "STORAGE_ROOT", str(tmp_path / "storage"))
    limiter = TokenBucketRateLimiter(capacity=10_000, refill_per_sec=1000.0)
    ws_limiter = TokenBucketRateLimiter(capacity=10_000, refill_per_sec=1000.0)
    app = create_app(workspace_session_factory=factory, rate_limiter=limiter, workspace_rate_limiter=ws_limiter)
    c = TestClient(app)
    c.get("/api/workspace")
    # Inject a recording adapter; restore the default afterward.
    saved_adapter = provider.get_ai_adapter()
    adapter = RecordingAdapter()
    provider.set_ai_adapter(adapter)
    yield c, eng, adapter
    provider.set_ai_adapter(saved_adapter)
    for p in (tmp_path / "storage").rglob("*"):
        try:
            os.chmod(p, 0o700)
        except OSError:
            pass


def _upload(c, data=None) -> int:
    r = c.post("/api/screenshots", headers=HDRS, files=[("files", ("a.png", data or png(), "image/png"))])
    return r.json()["results"][0]["screenshotId"]


def _grant(c, sid, granted=True):
    return c.post(f"/api/screenshots/{sid}/consent", headers=HDRS, json={"granted": granted})


def test_authorized_classify_202_then_polls_successful_one_adapter_call(client):
    c, eng, adapter = client
    sid = _upload(c)
    _grant(c, sid, True)
    r = c.post(f"/api/screenshots/{sid}/classify", headers=HDRS)
    assert r.status_code == 202
    op_id = r.json()["operationId"]
    assert r.json()["status"] == "pending"
    # BackgroundTask has run synchronously by now under TestClient.
    assert adapter.calls == 1
    poll = c.get(f"/api/operations/{op_id}")
    assert poll.status_code == 200
    assert poll.json()["status"] == "successful"
    # Classification + Epitaph artifacts committed.
    with Session(eng) as s:
        assert s.scalar(select(func.count()).select_from(models.Artifact)) == 2
        assert s.scalar(select(func.count()).select_from(models.ArtifactVersion)) == 2


def test_classify_without_consent_403_and_zero_adapter_calls(client):
    c, _, adapter = client
    sid = _upload(c)  # no consent granted
    r = c.post(f"/api/screenshots/{sid}/classify", headers=HDRS)
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "consent_required"
    assert adapter.calls == 0  # adapter NEVER invoked without consent


def test_classify_requires_csrf_header(client):
    c, _, adapter = client
    sid = _upload(c)
    _grant(c, sid, True)
    # Missing X-Requested-With -> CSRF middleware rejects the state-changing POST.
    r = c.post(f"/api/screenshots/{sid}/classify", headers={"Origin": TRUSTED})
    assert r.status_code == 403
    assert adapter.calls == 0


def test_classify_foreign_screenshot_404(client):
    c, _, adapter = client
    sid = _upload(c)
    _grant(c, sid, True)
    # Fresh workspace B cannot classify A's screenshot.
    c.cookies.clear()
    c.get("/api/workspace")
    r = c.post(f"/api/screenshots/{sid}/classify", headers=HDRS)
    r_missing = c.post("/api/screenshots/987654/classify", headers=HDRS)
    assert r.status_code == 404
    assert r_missing.status_code == 404
    assert r.json() == r_missing.json()  # non-disclosure
    assert adapter.calls == 0


def test_missing_screenshot_404(client):
    c, _, _ = client
    r = c.post("/api/screenshots/987654/classify", headers=HDRS)
    assert r.status_code == 404


def test_unexpected_background_failure_marks_operation_failed(client):
    c, _, _ = client
    sid = _upload(c)
    _grant(c, sid, True)
    saved_adapter = provider.get_ai_adapter()
    provider.set_ai_adapter(UnexpectedFailureAdapter())
    try:
        accepted = c.post(f"/api/screenshots/{sid}/classify", headers=HDRS)
        assert accepted.status_code == 202
        polled = c.get(f"/api/operations/{accepted.json()['operationId']}")
        assert polled.status_code == 200
        assert polled.json()["status"] == "failed"
    finally:
        provider.set_ai_adapter(saved_adapter)


def test_consent_database_failure_returns_503_and_never_calls_adapter(client, monkeypatch):
    c, _, adapter = client
    sid = _upload(c)
    from app.db.repository import WorkspaceRepository

    def unavailable(self, screenshot_id):
        raise SQLAlchemyError("database unavailable")

    monkeypatch.setattr(WorkspaceRepository, "get_consent", unavailable)
    response = c.post(f"/api/screenshots/{sid}/classify", headers=HDRS)
    assert response.status_code == 503
    assert response.json() == {
        "error": {"code": "service_unavailable", "message": "Service temporarily unavailable"}
    }
    assert adapter.calls == 0


def test_classification_identity_failures_fail_closed():
    from fastapi import FastAPI
    from starlette.middleware.base import BaseHTTPMiddleware
    from app.routers.classification import build_classification_router

    class NoWorkspace(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            request.state.workspace_id = None
            request.state.identity_unavailable = False
            return await call_next(request)

    missing = FastAPI()
    missing.add_middleware(NoWorkspace)
    missing.include_router(build_classification_router())
    assert TestClient(missing).post("/api/screenshots/1/classify", headers=HDRS).status_code == 401

    class Unavailable(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            request.state.workspace_id = None
            request.state.identity_unavailable = True
            return await call_next(request)

    unavailable = FastAPI()
    unavailable.add_middleware(Unavailable)
    unavailable.include_router(build_classification_router())
    assert TestClient(unavailable).post(
        "/api/screenshots/1/classify", headers=HDRS
    ).status_code == 503
