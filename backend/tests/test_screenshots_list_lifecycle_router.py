"""List / archive / restore router tests (Task 1.12 integration).

Real HTTP path via TestClient, temporary SQLite DB + temporary private storage,
synthetic images. Verifies:
  * GET /api/screenshots lists only the caller's workspace (safe metadata),
  * ?state= filter,
  * POST archive (Safe Burial) and restore (Resurrection) round-trip + state
    persistence across a fresh list,
  * stable screenshot id through the lifecycle,
  * duplicate flag surfaced in the list,
  * foreign/missing -> identical generic 404,
  * CSRF: state-changing POST without the custom header is rejected (403),
  * restore integrity failure -> honest 200 failed/archived (no false success),
  * no storage path / workspace token leaked in list responses.
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
from app.db import models
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


def _upload(c, data, name="a.png", ct="image/png") -> dict:
    r = c.post("/api/screenshots", headers=HDRS, files=[("files", (name, data, ct))])
    return r.json()["results"][0]


def test_list_returns_only_own_workspace_safe_metadata(client):
    c, _, _ = client
    r1 = _upload(c, png((1, 1, 1)))
    r2 = _upload(c, png((2, 2, 2)))
    r = c.get("/api/screenshots")
    assert r.status_code == 200
    body = r.json()
    ids = {s["screenshotId"] for s in body["screenshots"]}
    assert ids == {r1["screenshotId"], r2["screenshotId"]}
    # Safe metadata only: no storage path / workspace id / token fields.
    for s in body["screenshots"]:
        assert set(s.keys()) == {
            "screenshotId", "state", "mimeType", "originalFilename",
            "isDuplicate", "duplicateOf", "byteSize", "uploadedAt",
        }
    assert "storage" not in r.text and "token" not in r.text


def test_list_is_empty_for_fresh_workspace(client):
    c, _, _ = client
    r = c.get("/api/screenshots")
    assert r.status_code == 200
    assert r.json() == {"screenshots": []}


def test_duplicate_flag_surfaced_in_list(client):
    c, _, _ = client
    data = png((3, 3, 3))
    first = _upload(c, data)
    second = _upload(c, data)  # identical bytes -> duplicate
    by_id = {s["screenshotId"]: s for s in c.get("/api/screenshots").json()["screenshots"]}
    assert by_id[first["screenshotId"]]["isDuplicate"] is False
    assert by_id[second["screenshotId"]]["isDuplicate"] is True
    assert by_id[second["screenshotId"]]["duplicateOf"] == first["screenshotId"]


def test_archive_then_restore_roundtrip_and_state_filter(client):
    c, _, _ = client
    sid = _upload(c, png())["screenshotId"]

    # Safe Burial -> archived.
    ra = c.post(f"/api/screenshots/{sid}/archive", headers=HDRS)
    assert ra.status_code == 200
    assert ra.json() == {"screenshotId": sid, "status": "successful", "state": "archived"}

    # A fresh list reflects the new state (persistence across requests).
    active = c.get("/api/screenshots?state=active").json()["screenshots"]
    archived = c.get("/api/screenshots?state=archived").json()["screenshots"]
    assert sid not in {s["screenshotId"] for s in active}
    assert sid in {s["screenshotId"] for s in archived}

    # Resurrection -> active, same stable id.
    rr = c.post(f"/api/screenshots/{sid}/restore", headers=HDRS)
    assert rr.status_code == 200
    assert rr.json() == {"screenshotId": sid, "status": "successful", "state": "active"}
    active2 = c.get("/api/screenshots?state=active").json()["screenshots"]
    assert sid in {s["screenshotId"] for s in active2}


def test_invalid_state_filter_rejected(client):
    c, _, _ = client
    r = c.get("/api/screenshots?state=bogus")
    assert r.status_code == 400


def test_list_database_failure_fails_closed_503(client, monkeypatch):
    c, _, _ = client
    from app.db.repository import WorkspaceRepository

    def fail_list(self, state=None):
        raise SQLAlchemyError("database unavailable")

    monkeypatch.setattr(WorkspaceRepository, "list_screenshots", fail_list)
    r = c.get("/api/screenshots")
    assert r.status_code == 503
    assert r.json() == {
        "error": {"code": "service_unavailable", "message": "Service temporarily unavailable"}
    }


def test_archive_missing_id_generic_404(client):
    c, _, _ = client
    r = c.post("/api/screenshots/987654/archive", headers=HDRS)
    assert r.status_code == 404


def test_archive_database_failure_fails_closed_503(client, monkeypatch):
    c, _, _ = client
    sid = _upload(c, png())["screenshotId"]
    from app.db.repository import WorkspaceRepository

    def fail_lookup(self, screenshot_id):
        raise SQLAlchemyError("database unavailable")

    monkeypatch.setattr(WorkspaceRepository, "get_screenshot", fail_lookup)
    r = c.post(f"/api/screenshots/{sid}/archive", headers=HDRS)
    assert r.status_code == 503
    assert r.json() == {
        "error": {"code": "service_unavailable", "message": "Service temporarily unavailable"}
    }


def test_foreign_workspace_cannot_archive_or_list(client):
    c, _, _ = client
    sid = _upload(c, png())["screenshotId"]
    # New visitor with a fresh cookie.
    c.cookies.clear()
    c.get("/api/workspace")
    # Foreign list is empty.
    assert c.get("/api/screenshots").json() == {"screenshots": []}
    # Foreign archive -> same generic 404 as a missing id.
    r_foreign = c.post(f"/api/screenshots/{sid}/archive", headers=HDRS)
    r_missing = c.post("/api/screenshots/987654/archive", headers=HDRS)
    assert r_foreign.status_code == 404
    assert r_missing.status_code == 404
    assert r_foreign.json() == r_missing.json()


def test_spoofed_workspace_params_ignored_on_archive(client):
    c, _, _ = client
    sid = _upload(c, png())["screenshotId"]
    c.cookies.clear()
    c.get("/api/workspace")  # different workspace
    r = c.post(
        f"/api/screenshots/{sid}/archive?workspace_id=1",
        headers={**HDRS, "X-Workspace-Id": "1"},
    )
    assert r.status_code == 404  # cookie identity authoritative


def test_archive_requires_csrf_header(client):
    c, _, _ = client
    sid = _upload(c, png())["screenshotId"]
    # Missing X-Requested-With -> CSRF middleware rejects state-changing POST.
    r = c.post(f"/api/screenshots/{sid}/archive", headers={"Origin": TRUSTED})
    assert r.status_code == 403
    # State unchanged: still active.
    active = c.get("/api/screenshots?state=active").json()["screenshots"]
    assert sid in {s["screenshotId"] for s in active}


def test_restore_requires_csrf_header(client):
    c, _, _ = client
    sid = _upload(c, png())["screenshotId"]
    c.post(f"/api/screenshots/{sid}/archive", headers=HDRS)
    r = c.post(f"/api/screenshots/{sid}/restore", headers={"Origin": TRUSTED})
    assert r.status_code == 403


def test_list_requires_no_csrf_header(client):
    c, _, _ = client
    _upload(c, png())
    r = c.get("/api/screenshots")  # no CSRF headers for a safe GET
    assert r.status_code == 200


def test_restore_integrity_failure_is_honest_not_false_success(client):
    c, eng, tmp_path = client
    sid = _upload(c, png())["screenshotId"]
    c.post(f"/api/screenshots/{sid}/archive", headers=HDRS)
    # Corrupt the stored original so the SHA-256 verification fails.
    with Session(eng) as s:
        shot = s.get(models.Screenshot, sid)
        blob = s.get(models.ContentBlob, shot.content_blob_id)
        path = tmp_path / "storage" / blob.storage_path
    os.chmod(path, 0o644)
    path.write_bytes(b"tampered bytes that do not match the checksum")
    r = c.post(f"/api/screenshots/{sid}/restore", headers=HDRS)
    assert r.status_code == 200
    # Honest: failed + still archived (NOT a false 'active').
    assert r.json() == {"screenshotId": sid, "status": "failed", "state": "archived"}
    archived = c.get("/api/screenshots?state=archived").json()["screenshots"]
    assert sid in {s["screenshotId"] for s in archived}
