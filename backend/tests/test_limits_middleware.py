"""Integration tests for the rate-limit + size-gate middleware (Task 1.5).

Uses the real app factory with an injected controllable-clock limiter and a
temporary in-memory DB, so limiter behavior is deterministic (no timing flakes)
and we can assert no workspace rows are created on rejected requests.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app import config
from app.db.base import Base
from app.main import create_app
from app.ratelimit import TokenBucketRateLimiter

TRUSTED = config.TRUSTED_ORIGINS[0]
HEADER = config.CSRF_HEADER_NAME


@pytest.fixture()
def ctx(monkeypatch):
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(eng)
    factory = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False, class_=Session)
    monkeypatch.setattr(config, "COOKIE_SECURE", False)

    clock = {"now": 0.0}
    limiter = TokenBucketRateLimiter(
        capacity=3, refill_per_sec=1.0, clock=lambda: clock["now"]
    )
    app = create_app(workspace_session_factory=factory, rate_limiter=limiter)
    return TestClient(app), eng, clock


def _count_ws(eng) -> int:
    with eng.connect() as conn:
        return conn.exec_driver_sql("SELECT COUNT(*) FROM workspace").scalar()


def test_requests_within_rate_pass(ctx):
    c, _, _ = ctx
    assert [c.get("/api/health").status_code for _ in range(3)] == [200, 200, 200]


def test_requests_exceeding_rate_get_429(ctx):
    c, _, _ = ctx
    for _ in range(3):
        c.get("/api/health")
    r = c.get("/api/health")
    assert r.status_code == 429
    assert r.json() == {"error": {"code": "rate_limited", "message": "Too Many Requests"}}


def test_window_reset_allows_again(ctx):
    c, _, clock = ctx
    for _ in range(3):
        c.get("/api/health")
    assert c.get("/api/health").status_code == 429
    clock["now"] += 1.0  # refill one token
    assert c.get("/api/health").status_code == 200


def test_oversized_content_length_rejected_413_no_mint(ctx):
    c, eng, _ = ctx
    big = str(config.MAX_UPLOAD_BYTES + 1)
    # Use /api/workspace (a mint path) to also assert no workspace is created.
    r = c.get("/api/workspace", headers={"Content-Length": big})
    assert r.status_code == 413
    assert r.json() == {"error": {"code": "payload_too_large", "message": "Payload Too Large"}}
    assert _count_ws(eng) == 0


def test_rate_limit_rejection_does_not_mint_workspace(ctx):
    c, eng, _ = ctx
    # Exhaust the bucket with health (no mint), then a blocked /api/workspace
    # must not create a workspace.
    for _ in range(3):
        c.get("/api/health")
    assert _count_ws(eng) == 0
    r = c.get("/api/workspace")  # 4th request -> 429
    assert r.status_code == 429
    assert _count_ws(eng) == 0  # rejected before minting


def test_size_within_limit_passes(ctx):
    c, _, _ = ctx
    r = c.get("/api/health", headers={"Content-Length": str(config.MAX_UPLOAD_BYTES)})
    assert r.status_code == 200


def test_non_api_paths_not_rate_limited(ctx):
    c, _, _ = ctx
    # Exhaust the API bucket...
    for _ in range(5):
        c.get("/api/health")
    # ...a non-API path is unaffected (no SPA dist in tests -> 404, never 429).
    assert c.get("/some/spa/route").status_code in (200, 404)


def test_csrf_runs_before_rate_limit(ctx):
    """A forged state-changing request is 403 (CSRF) even under the limiter."""
    c, eng, _ = ctx
    r = c.post("/api/workspace", headers={"Origin": "https://evil.example", HEADER: "x"})
    assert r.status_code == 403  # CSRF rejects first (outermost)
    assert _count_ws(eng) == 0


# ---- Concern 1: per-workspace (stage 2) rate limiting ----

@pytest.fixture()
def two_stage_ctx(monkeypatch):
    """App with a generous IP limiter (stage 1) and a tight per-workspace
    limiter (stage 2), so stage-2 behavior is isolated and deterministic."""
    from sqlalchemy.orm import Session
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(eng)
    factory = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False, class_=Session)
    monkeypatch.setattr(config, "COOKIE_SECURE", False)
    clock = {"now": 0.0}
    ip_limiter = TokenBucketRateLimiter(capacity=10_000, refill_per_sec=1.0, clock=lambda: clock["now"])
    ws_limiter = TokenBucketRateLimiter(capacity=2, refill_per_sec=1.0, clock=lambda: clock["now"])
    app = create_app(
        workspace_session_factory=factory,
        rate_limiter=ip_limiter,
        workspace_rate_limiter=ws_limiter,
    )
    return app, eng, clock


def test_two_workspaces_same_ip_have_independent_workspace_limits(two_stage_ctx):
    app, _, _ = two_stage_ctx
    ca = TestClient(app)
    cb = TestClient(app)  # same client IP under TestClient, distinct cookie jar
    # Each visitor establishes its own workspace (consumes 1 ws token).
    assert ca.get("/api/workspace").status_code == 200
    assert cb.get("/api/workspace").status_code == 200
    # A exhausts its own bucket (cap 2 -> 1 left), then is limited...
    assert ca.get("/api/workspace").status_code == 200
    assert ca.get("/api/workspace").status_code == 429
    # ...while B is unaffected (independent per-workspace bucket).
    assert cb.get("/api/workspace").status_code == 200
    assert cb.get("/api/workspace").status_code == 429


def test_client_cannot_bypass_workspace_limit_by_changing_identifiers(two_stage_ctx):
    app, _, _ = two_stage_ctx
    c = TestClient(app)
    c.get("/api/workspace")          # establishes workspace, uses 1 token
    c.get("/api/workspace")          # uses the 2nd token
    # Attempt to get a fresh bucket by supplying client-controlled ids.
    r = c.get(
        "/api/workspace?workspace_id=999",
        headers={"X-Workspace-Id": "999", "X-Workspace": "other"},
    )
    assert r.status_code == 429  # still keyed by the trusted cookie identity


def test_identity_failure_does_not_disable_ip_stage(monkeypatch):
    """Even if per-workspace resolution yields no workspace, the IP stage still
    rate-limits (protection is not disabled by identity absence)."""
    from sqlalchemy.orm import Session
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    factory = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False, class_=Session)
    monkeypatch.setattr(config, "COOKIE_SECURE", False)
    clock = {"now": 0.0}
    ip_limiter = TokenBucketRateLimiter(capacity=2, refill_per_sec=1.0, clock=lambda: clock["now"])
    # Health never mints a workspace -> no per-workspace key, only IP stage applies.
    app = create_app(workspace_session_factory=factory, rate_limiter=ip_limiter)
    c = TestClient(app)
    assert c.get("/api/health").status_code == 200
    assert c.get("/api/health").status_code == 200
    assert c.get("/api/health").status_code == 429  # IP stage still enforced


def test_workspace_rate_limit_rejection_does_not_mutate_data(two_stage_ctx):
    app, eng, _ = two_stage_ctx
    c = TestClient(app)
    c.get("/api/workspace")
    c.get("/api/workspace")
    before = _count_ws(eng)
    r = c.get("/api/workspace")  # 429 from stage 2
    assert r.status_code == 429
    assert _count_ws(eng) == before  # no new workspace, no mutation


# ---- Concern 2: Content-Length is not authoritative (documented behavior) ----

def test_missing_content_length_passes_gate_documented(ctx):
    """A request with no Content-Length is NOT rejected by the early gate.
    Authoritative streaming enforcement is OUTSTANDING (upload Task 1.7/1.8)."""
    c, _, _ = ctx
    assert c.get("/api/health").status_code == 200


def test_invalid_content_length_passes_gate_documented(ctx):
    """An unparseable Content-Length is treated as absent (not a 500/413 here).
    Authoritative streaming enforcement is OUTSTANDING (upload Task 1.7/1.8)."""
    c, _, _ = ctx
    r = c.get("/api/health", headers={"Content-Length": "not-a-number"})
    assert r.status_code == 200


def test_oversized_content_length_still_blocked_early(ctx):
    c, _, _ = ctx
    r = c.get("/api/health", headers={"Content-Length": str(config.MAX_UPLOAD_BYTES + 1)})
    assert r.status_code == 413
