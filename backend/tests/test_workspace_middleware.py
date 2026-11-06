"""HTTP-level tests for the workspace identity middleware (Task 1.3).

These exercise cookie minting/resolution end-to-end through FastAPI using a
temporary SQLite database injected into the middleware (no global module
reloads, so other tests are unaffected). They verify first-visit creation,
returning-visitor recognition, cookie flags, tamper rejection, distinct
identities, that a client-supplied workspace id is never honored, and that no
raw token leaks into response bodies.

Scope boundary: Task 1.3 establishes WHO the workspace is (identity). It does
NOT implement CSRF/Origin defenses (Task 1.4); these tests do not assert those.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import Session, sessionmaker

from app import config
from app.db.base import Base
from app.main import create_app

COOKIE = config.WORKSPACE_COOKIE_NAME


@pytest.fixture()
def client(monkeypatch):
    # In-memory DB shared across connections for this test engine.
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    factory = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False, class_=Session)

    # Non-secure cookie so the TestClient (http) echoes it back.
    monkeypatch.setattr(config, "COOKIE_SECURE", False)

    app = create_app(workspace_session_factory=factory)
    with TestClient(app) as c:
        yield c, eng


def _count_workspaces(eng) -> int:
    with eng.connect() as conn:
        return conn.exec_driver_sql("SELECT COUNT(*) FROM workspace").scalar()


def test_first_visit_creates_workspace_and_sets_cookie(client):
    c, eng = client
    assert c.cookies.get(COOKIE) is None
    r = c.get("/api/workspace")
    assert r.status_code == 200
    assert r.json() == {"workspaceActive": True}
    set_cookie = r.headers.get("set-cookie", "").lower()
    assert "httponly" in set_cookie
    assert "samesite=strict" in set_cookie
    assert "path=/" in set_cookie
    assert _count_workspaces(eng) == 1


def test_returning_visitor_recognized_no_new_cookie(client):
    c, eng = client
    r1 = c.get("/api/workspace")
    assert "set-cookie" in {k.lower() for k in r1.headers}
    r2 = c.get("/api/workspace")
    assert r2.status_code == 200
    assert "set-cookie" not in {k.lower() for k in r2.headers}
    assert _count_workspaces(eng) == 1


def test_tampered_cookie_not_honored_mints_fresh(client):
    c, eng = client
    c.get("/api/workspace")
    c.cookies.set(COOKIE, "tampered-garbage-token")
    r2 = c.get("/api/workspace")
    assert r2.status_code == 200
    assert "set-cookie" in {k.lower() for k in r2.headers}
    assert _count_workspaces(eng) == 2  # original + fresh


def test_two_distinct_visitors_get_distinct_workspaces(client):
    c, eng = client
    c.get("/api/workspace")
    token_a = c.cookies.get(COOKIE)
    c.cookies.clear()
    c.get("/api/workspace")
    token_b = c.cookies.get(COOKIE)
    assert token_a and token_b and token_a != token_b
    assert _count_workspaces(eng) == 2


def test_client_supplied_workspace_id_is_never_honored(client):
    c, eng = client
    r = c.get("/api/workspace?workspace_id=1", headers={"X-Workspace-Id": "1"})
    assert r.status_code == 200
    assert "workspace_id" not in r.text
    assert r.json() == {"workspaceActive": True}


def test_no_raw_token_in_response_body(client):
    c, _ = client
    r = c.get("/api/workspace")
    token = c.cookies.get(COOKIE)
    assert token
    assert token not in r.text


def test_health_never_sets_workspace_cookie(client):
    c, eng = client
    r = c.get("/api/health")
    assert r.status_code == 200
    assert "set-cookie" not in {k.lower() for k in r.headers}
    assert _count_workspaces(eng) == 0


def test_secure_flag_present_when_cookie_secure_enabled(monkeypatch):
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    factory = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False, class_=Session)
    monkeypatch.setattr(config, "COOKIE_SECURE", True)  # production default
    app = create_app(workspace_session_factory=factory)
    with TestClient(app) as c:
        r = c.get("/api/workspace")
        assert "secure" in r.headers.get("set-cookie", "").lower()


def test_unknown_api_route_does_not_mint_or_crash(client):
    """An unknown /api route must not mint a workspace and must 404 cleanly.

    Regression: minting on every /api path previously turned a 404 into a 500
    when the DB had no workspace table. Minting is now allowlisted to
    identity-establishing endpoints only.
    """
    c, eng = client
    r = c.get("/api/definitely-not-a-route")
    assert r.status_code == 404
    assert "set-cookie" not in {k.lower() for k in r.headers}
    assert _count_workspaces(eng) == 0
