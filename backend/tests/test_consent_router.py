"""Consent HTTP router tests (Task 2.3).

Real HTTP path via TestClient, temporary SQLite DB + temporary private storage,
synthetic images. Verifies workspace-scoped record/read, generic 404 for
missing/foreign, fail-closed identity, and that the state-changing POST is
CSRF-guarded. Also confirms vault routes still work without consent.
"""

from __future__ import annotations

import io
import os

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app import config
import app.db.session as sess_mod
from app.db.base import Base
from app.main import create_app
from app.ratelimit import TokenBucketRateLimiter

TRUSTED = config.TRUSTED_ORIGINS[0]
HDRS = {"Origin": TRUSTED, "X-Requested-With": "fetch"}


def png(color=(5, 6, 7)) -> bytes:
    b = io.BytesIO()
    Image.new("RGB", (12, 9), color).save(b, format="PNG")
    return b.getvalue()


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
    yield c, eng, tmp_path
    for p in (tmp_path / "storage").rglob("*"):
        try:
            os.chmod(p, 0o700)
        except OSError:
            pass


def _upload(c, data=None) -> int:
    r = c.post("/api/screenshots", headers=HDRS, files=[("files", ("a.png", data or png(), "image/png"))])
    return r.json()["results"][0]["screenshotId"]


def test_consent_defaults_to_not_granted(client):
    c, _, _ = client
    sid = _upload(c)
    r = c.get(f"/api/screenshots/{sid}/consent")
    assert r.status_code == 200
    assert r.json() == {"screenshotId": sid, "granted": False}


def test_record_and_read_consent_roundtrip(client):
    c, _, _ = client
    sid = _upload(c)
    r = c.post(f"/api/screenshots/{sid}/consent", headers=HDRS, json={"granted": True})
    assert r.status_code == 200
    assert r.json() == {"screenshotId": sid, "granted": True}
    # Persisted and readable.
    assert c.get(f"/api/screenshots/{sid}/consent").json()["granted"] is True
    # Withdrawal.
    assert c.post(f"/api/screenshots/{sid}/consent", headers=HDRS, json={"granted": False}).json()["granted"] is False


def test_record_consent_requires_csrf_header(client):
    c, _, _ = client
    sid = _upload(c)
    # Missing X-Requested-With -> CSRF middleware rejects the state-changing POST.
    r = c.post(f"/api/screenshots/{sid}/consent", headers={"Origin": TRUSTED}, json={"granted": True})
    assert r.status_code == 403
    # State unchanged: still not granted.
    assert c.get(f"/api/screenshots/{sid}/consent").json()["granted"] is False


def test_read_consent_requires_no_csrf_header(client):
    c, _, _ = client
    sid = _upload(c)
    r = c.get(f"/api/screenshots/{sid}/consent")  # no CSRF headers for a safe GET
    assert r.status_code == 200


def test_missing_screenshot_generic_404(client):
    c, _, _ = client
    r_get = c.get("/api/screenshots/987654/consent")
    r_post = c.post("/api/screenshots/987654/consent", headers=HDRS, json={"granted": True})
    assert r_get.status_code == 404
    assert r_post.status_code == 404


def test_foreign_workspace_cannot_grant_or_read_consent(client):
    c, _, _ = client
    sid = _upload(c)  # workspace A
    c.cookies.clear()
    c.get("/api/workspace")  # fresh workspace B
    r_get = c.get(f"/api/screenshots/{sid}/consent")
    r_post = c.post(f"/api/screenshots/{sid}/consent", headers=HDRS, json={"granted": True})
    r_missing = c.get("/api/screenshots/987654/consent")
    assert r_get.status_code == 404
    assert r_post.status_code == 404
    # Foreign == missing (existence non-disclosure).
    assert r_get.json() == r_missing.json()


def test_spoofed_workspace_params_ignored(client):
    c, _, _ = client
    sid = _upload(c)
    c.cookies.clear()
    c.get("/api/workspace")  # different workspace
    r = c.post(
        f"/api/screenshots/{sid}/consent?workspace_id=1",
        headers={**HDRS, "X-Workspace-Id": "1"},
        json={"granted": True},
    )
    assert r.status_code == 404  # cookie identity authoritative


def test_vault_routes_still_work_without_consent(client):
    c, _, _ = client
    data = png((2, 2, 2))
    sid = _upload(c, data)
    # No consent granted. Archive, restore, and image-read all still work.
    assert c.get(f"/api/screenshots/{sid}/consent").json()["granted"] is False
    assert c.post(f"/api/screenshots/{sid}/archive", headers=HDRS).status_code == 200
    assert c.post(f"/api/screenshots/{sid}/restore", headers=HDRS).status_code == 200
    img = c.get(f"/api/screenshots/{sid}/image")
    assert img.status_code == 200 and img.content == data


def test_missing_workspace_denied_401_and_outage_503():
    from fastapi import FastAPI
    from starlette.middleware.base import BaseHTTPMiddleware
    from app.routers.consent import build_consent_router

    class NoWorkspace(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            request.state.workspace_id = None
            request.state.identity_unavailable = False
            return await call_next(request)

    app1 = FastAPI()
    app1.add_middleware(NoWorkspace)
    app1.include_router(build_consent_router())
    assert TestClient(app1).get("/api/screenshots/1/consent").status_code == 401

    class Outage(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            request.state.workspace_id = None
            request.state.identity_unavailable = True
            return await call_next(request)

    app2 = FastAPI()
    app2.add_middleware(Outage)
    app2.include_router(build_consent_router())
    r = TestClient(app2).get("/api/screenshots/1/consent")
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "identity_unavailable"


def test_consent_database_failure_is_unavailable_not_a_false_grant(client, monkeypatch):
    c, _, _ = client
    sid = _upload(c)
    from app.db.repository import WorkspaceRepository

    def unavailable(self, screenshot_id, granted):
        raise SQLAlchemyError("database unavailable")

    monkeypatch.setattr(WorkspaceRepository, "set_consent", unavailable)
    r = c.post(f"/api/screenshots/{sid}/consent", headers=HDRS, json={"granted": True})
    assert r.status_code == 503
    assert r.json() == {
        "error": {"code": "service_unavailable", "message": "Service temporarily unavailable"}
    }
    # The failed request was not converted into a grant.
    assert c.get(f"/api/screenshots/{sid}/consent").json()["granted"] is False
