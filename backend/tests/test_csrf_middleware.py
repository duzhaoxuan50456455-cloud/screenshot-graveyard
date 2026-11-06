"""CSRF / Origin protection tests (Task 1.4).

Exercises the three-layer defense on state-changing API requests using a test
app that mounts the real CSRFMiddleware + WorkspaceMiddleware (same order as the
app factory) plus synthetic GET/POST routes. A temporary in-memory SQLite DB is
injected so we can also assert that a rejected request performs NO DB mutation
(no workspace is minted).

Scope note: these verify CSRF/Origin behavior. Workspace identity itself is
covered by test_workspace_middleware.py.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app import config
from app.db.base import Base
from app.middleware.csrf import CSRFMiddleware
from app.middleware.workspace import WorkspaceMiddleware

TRUSTED = config.TRUSTED_ORIGINS[0]  # http://localhost:5173
HEADER = config.CSRF_HEADER_NAME


@pytest.fixture()
def app_and_engine(monkeypatch):
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(eng)
    factory = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False, class_=Session)
    monkeypatch.setattr(config, "COOKIE_SECURE", False)

    app = FastAPI()
    # Same order as the real factory: WorkspaceMiddleware first (inner),
    # CSRFMiddleware added last (outermost, runs first).
    app.add_middleware(WorkspaceMiddleware, session_factory=factory)
    app.add_middleware(CSRFMiddleware)

    # /api/workspace is a mint path -> lets us detect DB mutation on POSTs too.
    @app.post("/api/workspace")
    def mutate(request: Request):
        return {"workspaceActive": getattr(request.state, "workspace_id", None) is not None}

    @app.get("/api/workspace")
    def read(request: Request):
        return {"ok": True}

    return app, eng


def _count_ws(eng) -> int:
    with eng.connect() as conn:
        return conn.exec_driver_sql("SELECT COUNT(*) FROM workspace").scalar()


def _client(app) -> TestClient:
    return TestClient(app)


def test_safe_get_passes_without_origin_or_header(app_and_engine):
    app, _ = app_and_engine
    r = _client(app).get("/api/workspace")
    assert r.status_code == 200


def test_valid_same_origin_post_with_header_passes(app_and_engine):
    app, eng = app_and_engine
    r = _client(app).post("/api/workspace", headers={"Origin": TRUSTED, HEADER: "fetch"})
    assert r.status_code == 200
    assert _count_ws(eng) == 1  # the mutation proceeded


def test_foreign_origin_is_rejected_and_no_mutation(app_and_engine):
    app, eng = app_and_engine
    r = _client(app).post(
        "/api/workspace", headers={"Origin": "https://evil.example", HEADER: "fetch"}
    )
    assert r.status_code == 403
    assert _count_ws(eng) == 0  # rejected before minting


def test_missing_custom_header_is_rejected(app_and_engine):
    app, eng = app_and_engine
    r = _client(app).post("/api/workspace", headers={"Origin": TRUSTED})
    assert r.status_code == 403
    assert _count_ws(eng) == 0


def test_missing_origin_and_referer_fails_closed(app_and_engine):
    app, eng = app_and_engine
    r = _client(app).post("/api/workspace", headers={HEADER: "fetch"})
    assert r.status_code == 403
    assert _count_ws(eng) == 0


def test_malformed_origin_is_rejected(app_and_engine):
    app, _ = app_and_engine
    r = _client(app).post("/api/workspace", headers={"Origin": "not-a-url", HEADER: "fetch"})
    assert r.status_code == 403


def test_referer_fallback_when_origin_absent(app_and_engine):
    app, eng = app_and_engine
    r = _client(app).post(
        "/api/workspace", headers={"Referer": TRUSTED + "/some/path", HEADER: "fetch"}
    )
    assert r.status_code == 200
    assert _count_ws(eng) == 1


def test_sec_fetch_site_cross_site_is_rejected(app_and_engine):
    app, eng = app_and_engine
    r = _client(app).post(
        "/api/workspace",
        headers={"Origin": TRUSTED, HEADER: "fetch", "Sec-Fetch-Site": "cross-site"},
    )
    assert r.status_code == 403
    assert _count_ws(eng) == 0


def test_sec_fetch_site_same_origin_passes(app_and_engine):
    app, _ = app_and_engine
    r = _client(app).post(
        "/api/workspace",
        headers={"Origin": TRUSTED, HEADER: "fetch", "Sec-Fetch-Site": "same-origin"},
    )
    assert r.status_code == 200


def test_cross_site_post_with_cookie_still_rejected(app_and_engine):
    """A cross-site POST that carries the workspace cookie is still blocked.

    SameSite=Strict already prevents the cookie on real cross-site navigations;
    this asserts the server-side Origin layer independently rejects even if a
    cookie were somehow attached (defense in depth, not SameSite alone).
    """
    app, eng = app_and_engine
    c = _client(app)
    # Establish a workspace (valid first request).
    c.post("/api/workspace", headers={"Origin": TRUSTED, HEADER: "fetch"})
    assert _count_ws(eng) == 1
    # Now a forged cross-site POST with the cookie present + header, foreign origin.
    r = c.post("/api/workspace", headers={"Origin": "https://evil.example", HEADER: "fetch"})
    assert r.status_code == 403
    assert _count_ws(eng) == 1  # unchanged


def test_rejection_body_leaks_no_sensitive_data(app_and_engine):
    app, _ = app_and_engine
    r = _client(app).post(
        "/api/workspace", headers={"Origin": "https://evil.example", HEADER: "fetch"}
    )
    body = r.text
    assert r.json() == {"error": {"code": "forbidden", "message": "Forbidden"}}
    assert "sg_workspace" not in body and "token" not in body.lower()


def test_production_default_origins_are_not_wildcard():
    # No wildcard / arbitrary origin is trusted by default.
    assert "*" not in config.TRUSTED_ORIGINS
    assert all(o.startswith("http://") or o.startswith("https://") for o in config.TRUSTED_ORIGINS)


def test_sec_fetch_site_same_site_passes(app_and_engine):
    """`same-site` is a standard value and must not be rejected on its own."""
    app, _ = app_and_engine
    r = _client(app).post(
        "/api/workspace",
        headers={"Origin": TRUSTED, HEADER: "fetch", "Sec-Fetch-Site": "same-site"},
    )
    assert r.status_code == 200


def test_sec_fetch_site_none_passes(app_and_engine):
    """`none` (user-initiated, e.g. typed URL) is standard and not rejected."""
    app, _ = app_and_engine
    r = _client(app).post(
        "/api/workspace",
        headers={"Origin": TRUSTED, HEADER: "fetch", "Sec-Fetch-Site": "none"},
    )
    assert r.status_code == 200


def test_nonstandard_sec_fetch_site_value_is_not_special_cased(app_and_engine):
    """A nonstandard value like 'cross-origin' is NOT used as a signal; the
    decision falls through to the Origin check (trusted origin -> allowed)."""
    app, _ = app_and_engine
    r = _client(app).post(
        "/api/workspace",
        headers={"Origin": TRUSTED, HEADER: "fetch", "Sec-Fetch-Site": "cross-origin"},
    )
    assert r.status_code == 200


def test_deceptive_subdomain_origin_is_rejected(app_and_engine):
    app, eng = app_and_engine
    for evil in (
        "http://localhost:5173.evil.com",
        "http://evil.localhost:5173",
        "http://localhost:5173.attacker.test",
    ):
        r = _client(app).post("/api/workspace", headers={"Origin": evil, HEADER: "fetch"})
        assert r.status_code == 403, evil
    assert _count_ws(eng) == 0


def test_origin_match_is_case_insensitive_on_scheme_host(app_and_engine):
    app, _ = app_and_engine
    r = _client(app).post(
        "/api/workspace", headers={"Origin": "HTTP://LOCALHOST:5173", HEADER: "fetch"}
    )
    assert r.status_code == 200


def test_production_requires_explicit_trusted_origins(monkeypatch):
    """APP_ENV=production with no TRUSTED_ORIGINS must fail fast, not fall back
    to dev defaults. Tested via the resolver directly to avoid reloading the
    shared config module (no global contamination)."""
    import app.config as cfg
    monkeypatch.setattr(cfg, "APP_ENV", "production")
    monkeypatch.delenv("TRUSTED_ORIGINS", raising=False)
    with pytest.raises(RuntimeError):
        cfg._resolve_trusted_origins()


def test_wildcard_trusted_origin_is_rejected(monkeypatch):
    import app.config as cfg
    monkeypatch.setattr(cfg, "APP_ENV", "development")
    monkeypatch.setenv("TRUSTED_ORIGINS", "https://good.example,*")
    with pytest.raises(RuntimeError):
        cfg._resolve_trusted_origins()


def test_dev_resolver_uses_localhost_defaults(monkeypatch):
    import app.config as cfg
    monkeypatch.setattr(cfg, "APP_ENV", "development")
    monkeypatch.delenv("TRUSTED_ORIGINS", raising=False)
    origins = cfg._resolve_trusted_origins()
    assert "http://localhost:5173" in origins
