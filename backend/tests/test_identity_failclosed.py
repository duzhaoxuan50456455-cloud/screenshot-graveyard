"""Task 1.3 identity error-handling boundary (reviewed for Task 1.4 integration).

Verifies that a DATABASE outage during identity resolution is distinguished from
an invalid/absent credential: the middleware sets request.state.identity_unavailable
= True (so sensitive endpoints can fail closed) rather than silently treating the
visitor as a valid anonymous "no workspace". Non-sensitive paths (health) still
work, and no workspace is created during the failure.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app import config
from app.middleware.workspace import WorkspaceMiddleware


class _BrokenSession:
    """A session whose every use raises a DB error, simulating an outage."""

    def execute(self, *a, **k):
        raise OperationalError("SELECT 1", {}, Exception("db down"))

    def scalars(self, *a, **k):
        raise OperationalError("SELECT 1", {}, Exception("db down"))

    def scalar(self, *a, **k):
        raise OperationalError("SELECT 1", {}, Exception("db down"))

    def add(self, *a, **k):
        raise OperationalError("INSERT", {}, Exception("db down"))

    def flush(self, *a, **k):
        raise OperationalError("INSERT", {}, Exception("db down"))

    def commit(self, *a, **k):
        raise OperationalError("COMMIT", {}, Exception("db down"))

    def rollback(self):
        return None

    def close(self):
        return None


@pytest.fixture()
def app():
    a = FastAPI()
    a.add_middleware(WorkspaceMiddleware, session_factory=lambda: _BrokenSession())

    @a.get("/api/health")
    def health():
        return {"status": "ok"}

    @a.get("/api/workspace")
    def workspace(request: Request):
        return {
            "workspaceActive": getattr(request.state, "workspace_id", None) is not None,
            "identityUnavailable": getattr(request.state, "identity_unavailable", False),
        }

    return a


def test_db_outage_flags_identity_unavailable_not_valid_anon(app):
    # A visitor WITH a cookie hits a mint path while the DB is down.
    c = TestClient(app)
    c.cookies.set(config.WORKSPACE_COOKIE_NAME, "some-existing-looking-token")
    r = c.get("/api/workspace")
    # Request does not 500; but identity is flagged unavailable, not "valid none".
    assert r.status_code == 200
    body = r.json()
    assert body["workspaceActive"] is False
    assert body["identityUnavailable"] is True


def test_health_still_ok_during_db_outage(app):
    r = TestClient(app).get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
