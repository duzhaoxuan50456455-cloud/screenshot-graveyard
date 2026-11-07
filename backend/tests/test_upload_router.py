"""HTTP multipart upload router tests (Task 1.9).

These exercise the REAL HTTP multipart path via FastAPI TestClient (distinct from
the direct validator unit tests in test_upload_validation.py). A temporary
SQLite DB + temporary storage root are wired in by pointing app.db.session and
STORAGE_ROOT at the fixtures; identity/CSRF/limits middleware run for real.
"""

from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine, func, select
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


def png(color=(1, 2, 3), size=(10, 10)) -> bytes:
    b = io.BytesIO()
    Image.new("RGB", size, color).save(b, format="PNG")
    return b.getvalue()


def jpeg(color=(4, 5, 6)) -> bytes:
    b = io.BytesIO()
    Image.new("RGB", (10, 10), color).save(b, format="JPEG")
    return b.getvalue()


def webp(color=(7, 8, 9)) -> bytes:
    b = io.BytesIO()
    Image.new("RGB", (10, 10), color).save(b, format="WEBP")
    return b.getvalue()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(eng)
    factory = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False, class_=Session)
    # Point the module-level session factory (used by get_session) at the test DB.
    monkeypatch.setattr(sess_mod, "engine", eng)
    monkeypatch.setattr(sess_mod, "SessionLocal", factory)
    monkeypatch.setattr(config, "COOKIE_SECURE", False)
    monkeypatch.setattr(config, "STORAGE_ROOT", str(tmp_path / "storage"))

    # Generous limiter so rate limits don't interfere with functional tests.
    limiter = TokenBucketRateLimiter(capacity=10_000, refill_per_sec=1000.0)
    ws_limiter = TokenBucketRateLimiter(capacity=10_000, refill_per_sec=1000.0)
    app = create_app(
        workspace_session_factory=factory,
        rate_limiter=limiter,
        workspace_rate_limiter=ws_limiter,
    )
    c = TestClient(app)
    c.get("/api/workspace")  # establish workspace cookie
    yield c, eng
    import os
    for p in (tmp_path / "storage").rglob("*"):
        try:
            os.chmod(p, 0o700)
        except OSError:
            pass


def _files(*items):
    # items: (filename, bytes, content_type)
    return [("files", (n, b, ct)) for (n, b, ct) in items]


def _count(eng, model):
    with Session(eng) as s:
        return int(s.scalar(select(func.count()).select_from(model)) or 0)


def test_single_valid_upload_201(client):
    c, eng = client
    r = c.post("/api/screenshots", headers=HDRS, files=_files(("a.png", png(), "image/png")))
    assert r.status_code == 201
    body = r.json()["results"]
    assert len(body) == 1 and body[0]["status"] == "accepted"
    assert body[0]["screenshotId"] > 0
    assert _count(eng, models.Screenshot) == 1


def test_multiple_valid_uploads_201(client):
    c, eng = client
    r = c.post(
        "/api/screenshots",
        headers=HDRS,
        files=_files(
            ("a.png", png((1, 1, 1)), "image/png"),
            ("b.jpg", jpeg(), "image/jpeg"),
            ("c.webp", webp(), "image/webp"),
        ),
    )
    assert r.status_code == 201
    assert all(x["status"] == "accepted" for x in r.json()["results"])
    assert _count(eng, models.Screenshot) == 3


def test_mixed_batch_returns_207(client):
    c, eng = client
    r = c.post(
        "/api/screenshots",
        headers=HDRS,
        files=_files(
            ("good.png", png((2, 2, 2)), "image/png"),
            ("bad.txt", b"not an image", "text/plain"),
        ),
    )
    assert r.status_code == 207
    results = {x["filename"]: x for x in r.json()["results"]}
    assert results["good.png"]["status"] == "accepted"
    assert results["bad.txt"]["status"] == "rejected"
    assert results["bad.txt"]["error"]  # named + message present
    # The valid file in the mixed batch was still stored (independence).
    assert _count(eng, models.Screenshot) == 1


def test_all_invalid_batch_returns_400(client):
    c, eng = client
    r = c.post(
        "/api/screenshots",
        headers=HDRS,
        files=_files(
            ("x.png", b"xxx", "image/png"),
            ("y.png", b"yyy", "image/png"),
        ),
    )
    assert r.status_code == 400
    assert all(x["status"] == "rejected" for x in r.json()["results"])
    assert _count(eng, models.Screenshot) == 0


def test_absent_files_field_rejected(client):
    c, _ = client
    # The 'files' field is required; omitting it -> FastAPI 422 (never a crash,
    # never a write).
    r = c.post("/api/screenshots", headers=HDRS)
    assert r.status_code == 422


def test_empty_file_part_rejected(client, eng=None):
    c, eng = client
    # A present-but-empty file part is a rejected image (all-rejected -> 400).
    r = c.post(
        "/api/screenshots",
        headers=HDRS,
        files=[("files", ("empty.png", b"", "image/png"))],
    )
    assert r.status_code == 400
    assert r.json()["results"][0]["status"] == "rejected"


def test_batch_count_limit(client, monkeypatch):
    c, eng = client
    monkeypatch.setattr(config, "MAX_FILES_PER_BATCH", 2)
    r = c.post(
        "/api/screenshots",
        headers=HDRS,
        files=_files(
            ("a.png", png((1, 0, 0)), "image/png"),
            ("b.png", png((2, 0, 0)), "image/png"),
            ("c.png", png((3, 0, 0)), "image/png"),
        ),
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "too_many_files"
    assert _count(eng, models.Screenshot) == 0  # nothing stored


def test_deceptive_extension_and_mime_accepted_detected_format(client, eng=None):
    c, eng = client
    # PNG bytes with .jpg name + image/webp content-type -> accepted as PNG.
    r = c.post(
        "/api/screenshots",
        headers=HDRS,
        files=_files(("photo.jpg", png((3, 3, 3)), "image/webp")),
    )
    assert r.status_code == 201
    res = r.json()["results"][0]
    assert res["status"] == "accepted"
    assert res["mimeType"] == "image/png"  # detected, not declared


def test_duplicate_images_distinct_ids_shared_blob(client):
    c, eng = client
    data = png((9, 9, 9))
    r1 = c.post("/api/screenshots", headers=HDRS, files=_files(("d1.png", data, "image/png")))
    r2 = c.post("/api/screenshots", headers=HDRS, files=_files(("d2.png", data, "image/png")))
    a = r1.json()["results"][0]
    b = r2.json()["results"][0]
    assert a["screenshotId"] != b["screenshotId"]
    assert a["sha256"] == b["sha256"]
    assert b["isDuplicate"] is True
    assert _count(eng, models.ContentBlob) == 1
    assert _count(eng, models.Screenshot) == 2


# ---- CSRF / Origin ----

def test_foreign_origin_rejected_403_no_mutation(client):
    c, eng = client
    r = c.post(
        "/api/screenshots",
        headers={"Origin": "https://evil.example", "X-Requested-With": "fetch"},
        files=_files(("a.png", png(), "image/png")),
    )
    assert r.status_code == 403
    assert _count(eng, models.Screenshot) == 0


def test_missing_custom_header_rejected_403(client):
    c, eng = client
    r = c.post("/api/screenshots", headers={"Origin": TRUSTED}, files=_files(("a.png", png(), "image/png")))
    assert r.status_code == 403
    assert _count(eng, models.Screenshot) == 0


# ---- workspace isolation / client-ID spoofing ----

def test_client_supplied_workspace_id_is_ignored(client):
    c, eng = client
    # Try to spoof another workspace via query/body/header; server uses the cookie.
    r = c.post(
        "/api/screenshots?workspace_id=9999",
        headers={**HDRS, "X-Workspace-Id": "9999"},
        files=_files(("a.png", png(), "image/png")),
    )
    assert r.status_code == 201
    # The screenshot is owned by the cookie-resolved workspace, not 9999.
    with Session(eng) as s:
        shot = s.scalars(select(models.Screenshot)).first()
        assert shot.workspace_id != 9999


def test_two_workspaces_isolated(client):
    c, eng = client
    # Visitor A uploads.
    c.post("/api/screenshots", headers=HDRS, files=_files(("a.png", png((1, 1, 1)), "image/png")))
    # Visitor B: fresh cookie jar.
    c.cookies.clear()
    c.get("/api/workspace")
    c.post("/api/screenshots", headers=HDRS, files=_files(("b.png", png((2, 2, 2)), "image/png")))
    with Session(eng) as s:
        ws_ids = [w.id for w in s.scalars(select(models.Workspace)).all()]
        assert len(ws_ids) == 2
        for wid in ws_ids:
            n = s.scalar(
                select(func.count()).select_from(models.Screenshot).where(
                    models.Screenshot.workspace_id == wid
                )
            )
            assert n == 1  # each workspace sees only its own upload


# ---- identity fail-closed ----

def test_router_fails_closed_when_identity_unavailable(tmp_path, monkeypatch):
    # Build an app whose workspace middleware always flags identity_unavailable,
    # to exercise the router's fail-closed branch deterministically.
    from fastapi import FastAPI
    from starlette.middleware.base import BaseHTTPMiddleware
    from app.routers.screenshots import build_screenshots_router

    class ForceUnavailable(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            request.state.workspace_id = None
            request.state.identity_unavailable = True
            return await call_next(request)

    app = FastAPI()
    app.add_middleware(ForceUnavailable)
    app.include_router(build_screenshots_router())
    c = TestClient(app)
    r = c.post("/api/screenshots", files=_files(("a.png", png(), "image/png")))
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "identity_unavailable"


def test_router_401_when_no_workspace(tmp_path):
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
    r = c.post("/api/screenshots", files=_files(("a.png", png(), "image/png")))
    assert r.status_code == 401


# ---- size boundary ----

def test_request_body_over_aggregate_cap_413(client, monkeypatch):
    c, eng = client
    # Lower the AGGREGATE request cap; a body over it is rejected with 413 before
    # storage. (Per-file 15MB cap is separate and unchanged.)
    monkeypatch.setattr(config, "MAX_REQUEST_BYTES", 5000)
    # A single incompressible (noise) PNG whose bytes exceed the 5000-byte
    # aggregate request cap -> rejected with 413 before storage.
    import random
    random.seed(99)
    from PIL import Image as _Image
    w, h = 120, 120
    noise = _Image.frombytes("RGB", (w, h), bytes(random.getrandbits(8) for _ in range(w * h * 3)))
    import io as _io
    _b = _io.BytesIO(); noise.save(_b, format="PNG"); big = _b.getvalue()
    assert len(big) > 5000
    r = c.post("/api/screenshots", headers=HDRS, files=_files(("big.png", big, "image/png")))
    assert r.status_code == 413
    assert _count(eng, models.Screenshot) == 0


def test_oversized_single_file_rejected_no_mutation(client, monkeypatch):
    """With a lowered cap, an over-cap request is rejected and nothing stored.

    At the HTTP layer the Task 1.5 Content-Length middleware (whole-request gate)
    fires first and returns 413 for an over-cap request — which is correct
    layered behaviour. The AUTHORITATIVE per-file actual-byte streaming cap
    (independent of Content-Length) is unit-tested directly in
    test_upload_validation.py; here we assert the end-to-end result: an over-cap
    upload is rejected and writes nothing.
    """
    c, eng = client
    # Per-file cap below the file size, but the AGGREGATE request cap kept high,
    # so the PER-FILE streaming gate (not the request gate) is what rejects it.
    monkeypatch.setattr(config, "MAX_UPLOAD_BYTES", 100)
    monkeypatch.setattr(config, "MAX_REQUEST_BYTES", 10_000_000)
    data = png(size=(64, 64))
    assert len(data) > 100
    r = c.post("/api/screenshots", headers=HDRS, files=_files(("big.png", data, "image/png")))
    assert r.status_code == 400  # all-rejected: the single over-cap file
    res = r.json()["results"][0]
    assert res["status"] == "rejected" and "maximum size" in res["error"]
    assert _count(eng, models.Screenshot) == 0


# ---- error hygiene ----

def test_per_file_errors_do_not_leak_internal_details(client):
    c, _ = client
    r = c.post("/api/screenshots", headers=HDRS, files=_files(("bad.txt", b"nope", "text/plain")))
    res = r.json()["results"][0]
    msg = res["error"]
    assert "/" not in msg.replace("bad.txt", "")  # no filesystem path
    assert "Traceback" not in msg and "Error(" not in msg
    assert res["filename"] == "bad.txt"  # filename echoed for the user


def test_filename_not_used_as_path(client, eng=None):
    c, eng = client
    # A path-like filename is reduced to a basename; never used as a path.
    r = c.post(
        "/api/screenshots",
        headers=HDRS,
        files=_files(("../../etc/evil.png", png((4, 4, 4)), "image/png")),
    )
    assert r.status_code == 201
    res = r.json()["results"][0]
    assert "/" not in res["filename"]  # basename only
    assert res["filename"] == "evil.png"
