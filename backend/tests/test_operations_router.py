"""HTTP acceptance and poll tests for Task 2.4 operation lifecycle."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app import config
import app.db.session as sess_mod
from app.db import models
from app.db.base import Base
from app.main import create_app
from app.ratelimit import TokenBucketRateLimiter
from app.services.operation_service import OperationService


TRUSTED = config.TRUSTED_ORIGINS[0]
HDRS = {"Origin": TRUSTED, "X-Requested-With": "fetch"}


@pytest.fixture()
def client(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, class_=Session)
    monkeypatch.setattr(sess_mod, "engine", engine)
    monkeypatch.setattr(sess_mod, "SessionLocal", factory)
    monkeypatch.setattr(config, "COOKIE_SECURE", False)
    app = create_app(
        workspace_session_factory=factory,
        rate_limiter=TokenBucketRateLimiter(capacity=10_000, refill_per_sec=1000.0),
        workspace_rate_limiter=TokenBucketRateLimiter(capacity=10_000, refill_per_sec=1000.0),
    )
    test_client = TestClient(app)
    test_client.get("/api/workspace")
    yield test_client, factory


def _accept(client, kind="classify"):
    return client.post("/api/operations", headers=HDRS, json={"kind": kind})


def test_accept_returns_202_pending_operation_id_and_poll_reads_it(client):
    test_client, _ = client
    accepted = _accept(test_client)
    assert accepted.status_code == 202
    payload = accepted.json()
    assert payload["kind"] == "classify"
    assert payload["status"] == "pending"
    assert isinstance(payload["operationId"], int)

    polled = test_client.get(f"/api/operations/{payload['operationId']}")
    assert polled.status_code == 200
    assert polled.json() == payload


def test_acceptance_post_is_csrf_protected(client):
    test_client, _ = client
    response = test_client.post(
        "/api/operations", headers={"Origin": TRUSTED}, json={"kind": "classify"}
    )
    assert response.status_code == 403


def test_foreign_and_missing_operation_polls_are_generic_404(client):
    test_client, _ = client
    operation_id = _accept(test_client).json()["operationId"]
    test_client.cookies.clear()
    test_client.get("/api/workspace")
    foreign = test_client.get(f"/api/operations/{operation_id}")
    missing = test_client.get("/api/operations/987654")
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json()


def test_poll_observes_terminal_status_after_service_commits(client):
    test_client, factory = client
    operation_id = _accept(test_client).json()["operationId"]
    with factory() as session:
        workspace_id = session.scalar(select(models.Workspace.id))
        assert workspace_id is not None
        OperationService(session, workspace_id).set_terminal_status(
            operation_id, "uncertain", "Processing needs review"
        )
    polled = test_client.get(f"/api/operations/{operation_id}")
    assert polled.status_code == 200
    assert polled.json()["status"] == "uncertain"
    assert polled.json()["errorDetail"] == "Processing needs review"


def test_database_failure_returns_generic_503(client, monkeypatch):
    test_client, _ = client
    from app.db.repository import WorkspaceRepository

    def unavailable(self, **kwargs):
        raise SQLAlchemyError("database unavailable")

    monkeypatch.setattr(WorkspaceRepository, "create_operation", unavailable)
    response = _accept(test_client)
    assert response.status_code == 503
    assert response.json() == {
        "error": {"code": "service_unavailable", "message": "Service temporarily unavailable"}
    }
