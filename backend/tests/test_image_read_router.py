"""Authorized image-read endpoint tests (Task 1.11).

Real HTTP path via TestClient, temporary SQLite DB + temporary private storage,
synthetic images. Verifies owner-only access, generic 404 for missing/foreign,
integrity-verified bytes, fail-closed identity, and that private originals are
never reachable via a static/SPA path.
"""

from __future__ import annotations

import hashlib
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

TRUSTED = config.TRUSTED_ORIGINS[0]
HDRS = {"Origin": TRUSTED, "X-Requested-With": "fetch"}


def png(color=(5, 6, 7)) -> bytes:
    b = io.BytesIO()
    Image.new("RGB", (12, 9), color).save(b, format="PNG")
    return b.getvalue()


def jpeg(color=(1, 2, 3)) -> bytes:
    b = io.BytesIO()
    Image.new("RGB", (12, 9), color).save(b, format="JPEG")
    return b.getvalue()


def webp(color=(4, 5, 6)) -> bytes:
    b = io.BytesIO()
    Image.new("RGB", (12, 9), color).save(b, format="WEBP")
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


def _upload(c, data, name="a.png", ct="image/png") -> int:
    r = c.post("/api/screenshots", headers=HDRS, files=[("files", (name, data, ct))])
    return r.json()["results"][0]["screenshotId"]


def _count_ws(eng):
    with Session(eng) as s:
        return int(s.scalar(select(func.count()).select_from(models.Workspace)) or 0)


def test_owner_read_200_byte_identical(client):
    c, _, _ = client
    data = png()
    sid = _upload(c, data)
    r = c.get(f"/api/screenshots/{sid}/image")
    assert r.status_code == 200
    assert r.content == data  # byte-for-byte identical
    assert hashlib.sha256(r.content).hexdigest() == hashlib.sha256(data).hexdigest()
    assert r.headers["cache-control"] == "private, no-store"


@pytest.mark.parametrize("maker,name,ct,expected", [
    (png, "a.png", "image/png", "image/png"),
    (jpeg, "a.jpg", "image/jpeg", "image/jpeg"),
    (webp, "a.webp", "image/webp", "image/webp"),
])
def test_content_type_from_detected_stored_format(client, maker, name, ct, expected):
    c, _, _ = client
    sid = _upload(c, maker(), name, ct)
    r = c.get(f"/api/screenshots/{sid}/image")
    assert r.status_code == 200
    assert r.headers["content-type"].split(";")[0] == expected


def test_detected_mime_used_even_when_declared_wrong(client):
    c, _, _ = client
    # PNG bytes uploaded with a misleading .jpg name + image/webp content-type.
    sid = _upload(c, png((9, 9, 9)), "pretend.jpg", "image/webp")
    r = c.get(f"/api/screenshots/{sid}/image")
    assert r.status_code == 200
    assert r.headers["content-type"].split(";")[0] == "image/png"  # detected


def test_sha_and_size_match_persisted_metadata(client):
    c, eng, _ = client
    data = png((2, 4, 6))
    sid = _upload(c, data)
    r = c.get(f"/api/screenshots/{sid}/image")
    with Session(eng) as s:
        shot = s.get(models.Screenshot, sid)
        blob = s.get(models.ContentBlob, shot.content_blob_id)
    assert len(r.content) == blob.byte_size
    assert hashlib.sha256(r.content).hexdigest() == blob.sha256


def test_nonexistent_id_404(client):
    c, _, _ = client
    r = c.get("/api/screenshots/987654/image")
    assert r.status_code == 404


def test_foreign_workspace_404_same_as_missing(client):
    c, _, _ = client
    sid = _upload(c, png())
    # New visitor (fresh cookie) must not read A's screenshot.
    c.cookies.clear()
    c.get("/api/workspace")
    r_foreign = c.get(f"/api/screenshots/{sid}/image")
    r_missing = c.get("/api/screenshots/987654/image")
    assert r_foreign.status_code == 404
    assert r_missing.status_code == 404
    # Identical response body -> no existence disclosure.
    assert r_foreign.json() == r_missing.json()


def test_spoofed_workspace_params_have_no_effect(client):
    c, _, _ = client
    sid = _upload(c, png())
    c.cookies.clear()
    c.get("/api/workspace")  # different workspace
    r = c.get(
        f"/api/screenshots/{sid}/image?workspace_id=1",
        headers={"X-Workspace-Id": "1", "X-Workspace": "owner"},
    )
    assert r.status_code == 404  # cookie identity is authoritative


def test_missing_workspace_denied(tmp_path):
    from fastapi import FastAPI
    from starlette.middleware.base import BaseHTTPMiddleware
    from app.routers.screenshots import build_screenshots_router

    class NoWorkspace(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            request.state.workspace_id = None
            request.state.identity_unavailable = False
            return await call_next(request)

    app = FastAPI()
    app.add_middleware(NoWorkspace)
    app.include_router(build_screenshots_router())
    c = TestClient(app)
    r = c.get("/api/screenshots/1/image")
    assert r.status_code == 401


def test_identity_db_outage_fails_closed_503(tmp_path):
    from fastapi import FastAPI
    from starlette.middleware.base import BaseHTTPMiddleware
    from app.routers.screenshots import build_screenshots_router

    class Outage(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            request.state.workspace_id = None
            request.state.identity_unavailable = True
            return await call_next(request)

    app = FastAPI()
    app.add_middleware(Outage)
    app.include_router(build_screenshots_router())
    c = TestClient(app)
    r = c.get("/api/screenshots/1/image")
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "identity_unavailable"


def test_image_lookup_database_error_is_safe_non_200(client, monkeypatch):
    """A database failure after identity resolution never returns bytes or a
    framework error response with internal details."""
    c, _, _ = client
    sid = _upload(c, png())

    from app.db.repository import WorkspaceRepository

    def fail_lookup(self, screenshot_id):
        raise SQLAlchemyError("database unavailable")

    monkeypatch.setattr(WorkspaceRepository, "get_screenshot", fail_lookup)
    r = c.get(f"/api/screenshots/{sid}/image")
    assert r.status_code == 404
    assert r.json() == {"error": {"code": "not_found", "message": "Not Found"}}


def test_missing_original_file_safe_non_200(client):
    c, eng, tmp_path = client
    sid = _upload(c, png())
    # Delete the underlying stored file to simulate a missing original.
    with Session(eng) as s:
        shot = s.get(models.Screenshot, sid)
        blob = s.get(models.ContentBlob, shot.content_blob_id)
        path = tmp_path / "storage" / blob.storage_path
    os.chmod(path, 0o700)
    path.unlink()
    r = c.get(f"/api/screenshots/{sid}/image")
    assert r.status_code == 404  # safe non-200, no internal disclosure
    assert "/" not in r.text.replace("screenshots", "")  # no filesystem path


def test_corrupted_original_file_safe_non_200(client):
    c, eng, tmp_path = client
    sid = _upload(c, png())
    with Session(eng) as s:
        shot = s.get(models.Screenshot, sid)
        blob = s.get(models.ContentBlob, shot.content_blob_id)
        path = tmp_path / "storage" / blob.storage_path
    os.chmod(path, 0o644)
    path.write_bytes(b"corrupted bytes that do not match the checksum")
    r = c.get(f"/api/screenshots/{sid}/image")
    assert r.status_code == 404  # integrity failure -> safe non-200
    # The corrupt bytes are NOT returned.
    assert r.content != b"corrupted bytes that do not match the checksum"


def test_archived_image_still_readable_by_owner(client):
    c, eng, _ = client
    data = png((3, 3, 3))
    sid = _upload(c, data)
    # Archive via the service directly (no archive route exists yet).
    from app.services.vault_service import VaultService
    with Session(eng) as s:
        shot = s.get(models.Screenshot, sid)
        VaultService(s, shot.workspace_id).archive(sid)
    r = c.get(f"/api/screenshots/{sid}/image")
    assert r.status_code == 200
    assert r.content == data  # original bytes preserved while archived


def test_no_static_or_spa_path_serves_originals(client):
    c, eng, tmp_path = client
    data = png((7, 7, 7))
    sid = _upload(c, data)
    with Session(eng) as s:
        shot = s.get(models.Screenshot, sid)
        blob = s.get(models.ContentBlob, shot.content_blob_id)
    # Try to reach the private file via plausible static/SPA paths.
    for path in (f"/storage/{blob.storage_path}", f"/{blob.storage_path}", f"/static/{blob.storage_path}"):
        r = c.get(path)
        # Never the raw image bytes (either 404 or SPA fallback HTML/JSON).
        assert r.content != data
        ct = r.headers.get("content-type", "")
        assert not ct.startswith("image/")


def test_get_image_does_not_mutate_state_or_create_workspace(client):
    c, eng, _ = client
    sid = _upload(c, png())
    ws_before = _count_ws(eng)
    with Session(eng) as s:
        state_before = s.get(models.Screenshot, sid).state
    for _ in range(3):
        assert c.get(f"/api/screenshots/{sid}/image").status_code == 200
    with Session(eng) as s:
        assert s.get(models.Screenshot, sid).state == state_before
    assert _count_ws(eng) == ws_before  # no new workspace rows from GETs


def test_get_image_requires_no_csrf_header(client):
    """A safe GET must work WITHOUT the X-Requested-With / Origin CSRF headers
    (CSRF guards only state-changing methods)."""
    c, _, _ = client
    sid = _upload(c, png())
    r = c.get(f"/api/screenshots/{sid}/image")  # no HDRS
    assert r.status_code == 200
