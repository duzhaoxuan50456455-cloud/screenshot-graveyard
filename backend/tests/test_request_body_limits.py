"""Aggregate request-body size limit tests (Task 1.9 review).

Covers the per-file vs whole-request distinction (Concern 1) and the ASGI
actual-byte aggregate cap that bounds parsing/spooling even without an honest
Content-Length (Concern 2). Uses bounded synthetic streams — no huge allocations
beyond a few hundred KB.
"""

from __future__ import annotations

import asyncio
import io
import json
import random

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
from app.middleware.limits import MaxBodySizeMiddleware
from app.ratelimit import TokenBucketRateLimiter

TRUSTED = config.TRUSTED_ORIGINS[0]
HDRS = {"Origin": TRUSTED, "X-Requested-With": "fetch"}


def noise_png(seed, w, h) -> bytes:
    random.seed(seed)
    img = Image.frombytes("RGB", (w, h), bytes(random.getrandbits(8) for _ in range(w * h * 3)))
    b = io.BytesIO()
    img.save(b, format="PNG")
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
    yield c, eng
    import os
    for p in (tmp_path / "storage").rglob("*"):
        try:
            os.chmod(p, 0o700)
        except OSError:
            pass


def _count(eng, model):
    with Session(eng) as s:
        return int(s.scalar(select(func.count()).select_from(model)) or 0)


def test_multi_file_batch_over_per_file_cap_but_under_request_cap_succeeds(client):
    """Concern 1: two valid files each < 15MB, combined > 15MB, under the 301MB
    aggregate cap -> the batch SUCCEEDS (not wrongly 413'd)."""
    c, eng = client
    f1 = noise_png(1, 1800, 1700)
    f2 = noise_png(2, 1800, 1700)
    assert len(f1) < config.MAX_UPLOAD_BYTES and len(f2) < config.MAX_UPLOAD_BYTES
    assert len(f1) + len(f2) > config.MAX_UPLOAD_BYTES  # combined exceeds per-file cap
    r = c.post(
        "/api/screenshots",
        headers=HDRS,
        files=[("files", ("a.png", f1, "image/png")), ("files", ("b.png", f2, "image/png"))],
    )
    assert r.status_code == 201
    assert _count(eng, models.Screenshot) == 2


def test_single_file_over_per_file_cap_rejected(client, monkeypatch):
    """A single file over the 15MB per-file cap is rejected (not stored). With a
    lowered per-file cap but a higher request cap, the per-file gate is what
    rejects it."""
    c, eng = client
    monkeypatch.setattr(config, "MAX_UPLOAD_BYTES", 100_000)   # per-file cap 100KB
    monkeypatch.setattr(config, "MAX_REQUEST_BYTES", 10_000_000)  # request cap high
    data = noise_png(5, 400, 400)  # > 100KB
    assert len(data) > 100_000
    r = c.post("/api/screenshots", headers=HDRS, files=[("files", ("big.png", data, "image/png"))])
    # Per-file streaming cap rejects this one file -> all-rejected 400.
    assert r.status_code == 400
    assert r.json()["results"][0]["status"] == "rejected"
    assert "maximum size" in r.json()["results"][0]["error"]
    assert _count(eng, models.Screenshot) == 0


def test_aggregate_request_cap_rejects_413(client, monkeypatch):
    c, eng = client
    monkeypatch.setattr(config, "MAX_REQUEST_BYTES", 50_000)
    big = noise_png(6, 400, 400)  # > 50KB
    assert len(big) > 50_000
    r = c.post("/api/screenshots", headers=HDRS, files=[("files", ("big.png", big, "image/png"))])
    assert r.status_code == 413
    assert _count(eng, models.Screenshot) == 0


def test_mixed_batch_still_207_after_changes(client):
    c, eng = client
    r = c.post(
        "/api/screenshots",
        headers=HDRS,
        files=[
            ("files", ("good.png", noise_png(7, 50, 50), "image/png")),
            ("files", ("bad.txt", b"nope", "text/plain")),
        ],
    )
    assert r.status_code == 207
    assert _count(eng, models.Screenshot) == 1


# ---- ASGI actual-byte enforcement (Concern 2), independent of Content-Length ----

def _drive(headers, chunks, cap):
    """Drive MaxBodySizeMiddleware with a downstream that reads the whole body."""
    async def downstream(scope, receive, send):
        total = 0
        while True:
            msg = await receive()
            if msg["type"] == "http.disconnect":
                break
            if msg["type"] == "http.request":
                total += len(msg.get("body", b""))
                if not msg.get("more_body"):
                    break
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": json.dumps({"read": total}).encode()})

    app = MaxBodySizeMiddleware(downstream, max_bytes=cap)

    async def run():
        scope = {"type": "http", "path": "/api/screenshots", "headers": headers, "method": "POST"}
        sent = []
        it = iter(chunks)

        async def receive():
            try:
                return {"type": "http.request", "body": next(it), "more_body": True}
            except StopIteration:
                return {"type": "http.request", "body": b"", "more_body": False}

        async def send(m):
            sent.append(m)

        await app(scope, receive, send)
        return sent

    return asyncio.run(run())


def test_asgi_cap_enforced_without_content_length():
    sent = _drive([], [b"x" * 400] * 5, cap=1000)  # 2000 bytes, no CL
    start = next(m for m in sent if m["type"] == "http.response.start")
    assert start["status"] == 413


def test_asgi_cap_enforced_with_false_content_length():
    sent = _drive([(b"content-length", b"10")], [b"x" * 400] * 5, cap=1000)
    start = next(m for m in sent if m["type"] == "http.response.start")
    assert start["status"] == 413


def test_asgi_cap_allows_under_limit_body():
    sent = _drive([], [b"x" * 300] * 2, cap=1000)  # 600 bytes
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = next(m for m in sent if m["type"] == "http.response.body")["body"]
    assert start["status"] == 200
    assert json.loads(body) == {"read": 600}


def test_asgi_cap_ignores_non_api_paths():
    sent = _drive_path("/not-api", [b"x" * 400] * 5, cap=1000)
    start = next(m for m in sent if m["type"] == "http.response.start")
    assert start["status"] == 200  # passed through, not capped


def _drive_path(path, chunks, cap):
    async def downstream(scope, receive, send):
        while True:
            msg = await receive()
            if msg["type"] == "http.disconnect":
                break
            if msg["type"] == "http.request" and not msg.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    app = MaxBodySizeMiddleware(downstream, max_bytes=cap)

    async def run():
        scope = {"type": "http", "path": path, "headers": [], "method": "POST"}
        sent = []
        it = iter(chunks)

        async def receive():
            try:
                return {"type": "http.request", "body": next(it), "more_body": True}
            except StopIteration:
                return {"type": "http.request", "body": b"", "more_body": False}

        async def send(m):
            sent.append(m)

        await app(scope, receive, send)
        return sent

    return asyncio.run(run())
