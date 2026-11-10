"""HTTP integration tests for Task 2.6's manual Study Note selection boundary."""

from __future__ import annotations

import io
import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.middleware.base import BaseHTTPMiddleware

from app import config
import app.db.session as sess_mod
from app.ai import provider
from app.db import models
from app.db.base import Base
from app.main import create_app
from app.ratelimit import TokenBucketRateLimiter
from app.routers.study_selection import build_study_selection_router

TRUSTED = config.TRUSTED_ORIGINS[0]
HDRS = {"Origin": TRUSTED, "X-Requested-With": "fetch"}


def png(color=(5, 6, 7)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (12, 9), color).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, class_=Session)
    monkeypatch.setattr(sess_mod, "engine", engine)
    monkeypatch.setattr(sess_mod, "SessionLocal", factory)
    monkeypatch.setattr(config, "COOKIE_SECURE", False)
    monkeypatch.setattr(config, "STORAGE_ROOT", str(tmp_path / "storage"))
    app = create_app(
        workspace_session_factory=factory,
        rate_limiter=TokenBucketRateLimiter(capacity=10_000, refill_per_sec=1000.0),
        workspace_rate_limiter=TokenBucketRateLimiter(capacity=10_000, refill_per_sec=1000.0),
    )
    test_client = TestClient(app)
    test_client.get("/api/workspace")
    yield test_client
    for path in (tmp_path / "storage").rglob("*"):
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass


def _upload(client, color) -> int:
    response = client.post(
        "/api/screenshots",
        headers=HDRS,
        files=[("files", ("study.png", png(color), "image/png"))],
    )
    assert response.status_code == 201
    return response.json()["results"][0]["screenshotId"]


@pytest.mark.parametrize(
    ("count", "has_warning"),
    ((1, True), (3, True), (4, False), (8, False), (9, True), (12, True)),
)
def test_selection_count_policy_returns_non_blocking_warning_exactly_outside_4_to_8(
    client, count, has_warning
):
    ids = [_upload(client, (index, index + 10, index + 20)) for index in range(count)]
    response = client.post("/api/study-notes/selection", headers=HDRS, json={"screenshotIds": ids})

    assert response.status_code == 200
    assert response.json()["screenshotIds"] == ids
    assert (response.json()["warning"] is not None) is has_warning


@pytest.mark.parametrize("ids", ([], list(range(1, 14))))
def test_selection_count_policy_rejects_zero_or_thirteen_descriptively(client, ids):
    response = client.post("/api/study-notes/selection", headers=HDRS, json={"screenshotIds": ids})
    assert response.status_code == 400
    assert response.json() == {
        "error": {
            "code": "invalid_selection",
            "message": "Select between 1 and 12 screenshots to create a study note",
        }
    }


def test_selection_preserves_client_order_and_never_invokes_adapter(client, monkeypatch):
    ids = [_upload(client, (index, index, index)) for index in range(3)]

    def adapter_must_not_be_requested():
        raise AssertionError("selection validation must not invoke an AI adapter")

    monkeypatch.setattr(provider, "get_ai_adapter", adapter_must_not_be_requested)
    response = client.post(
        "/api/study-notes/selection",
        headers=HDRS,
        json={"screenshotIds": [ids[2], ids[0], ids[1]]},
    )

    assert response.status_code == 200
    assert response.json()["screenshotIds"] == [ids[2], ids[0], ids[1]]


def test_foreign_and_missing_selection_ids_are_identical_404_and_spoofing_is_ignored(client):
    screenshot_id = _upload(client, (1, 2, 3))  # workspace A
    client.cookies.clear()
    client.get("/api/workspace")  # workspace B
    owned_by_b = _upload(client, (4, 5, 6))

    foreign = client.post(
        "/api/study-notes/selection?workspace_id=1",
        headers={**HDRS, "X-Workspace-Id": "1"},
        json={
            "screenshotIds": [owned_by_b, screenshot_id],
            # Extra, client-supplied workspace values cannot affect the
            # server-resolved cookie identity.
            "workspaceId": 1,
        },
    )
    missing = client.post(
        "/api/study-notes/selection",
        headers=HDRS,
        json={"screenshotIds": [987654]},
    )

    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json() == {
        "error": {"code": "not_found", "message": "Not Found"}
    }


@pytest.mark.parametrize(
    "payload",
    (
        {"screenshotIds": ["1"]},
        {"screenshotIds": [True]},
        {"screenshotIds": [1.0]},
        {"screenshotIds": 1},
        {"screenshotIds": [{"id": 1}]},
        {},
    ),
)
def test_malformed_selection_shapes_and_id_types_never_report_success(client, payload):
    response = client.post("/api/study-notes/selection", headers=HDRS, json=payload)
    assert response.status_code == 422


def test_selection_validation_creates_no_operations_or_artifacts(client):
    screenshot_id = _upload(client, (1, 2, 3))
    with sess_mod.SessionLocal() as session:
        before = (
            session.scalar(select(func.count()).select_from(models.Operation)),
            session.scalar(select(func.count()).select_from(models.Artifact)),
            session.scalar(select(func.count()).select_from(models.ArtifactVersion)),
        )

    response = client.post(
        "/api/study-notes/selection", headers=HDRS, json={"screenshotIds": [screenshot_id]}
    )

    assert response.status_code == 200
    with sess_mod.SessionLocal() as session:
        after = (
            session.scalar(select(func.count()).select_from(models.Operation)),
            session.scalar(select(func.count()).select_from(models.Artifact)),
            session.scalar(select(func.count()).select_from(models.ArtifactVersion)),
        )
    assert after == before == (0, 0, 0)


def test_selection_requires_csrf_headers(client):
    screenshot_id = _upload(client, (1, 2, 3))
    response = client.post(
        "/api/study-notes/selection",
        headers={"Origin": TRUSTED},
        json={"screenshotIds": [screenshot_id]},
    )
    assert response.status_code == 403


def test_selection_database_failure_fails_closed(client, monkeypatch):
    screenshot_id = _upload(client, (1, 2, 3))
    from app.db.repository import WorkspaceRepository

    def unavailable(self, _screenshot_id):
        raise SQLAlchemyError("database unavailable")

    monkeypatch.setattr(WorkspaceRepository, "get_screenshot", unavailable)
    response = client.post(
        "/api/study-notes/selection", headers=HDRS, json={"screenshotIds": [screenshot_id]}
    )
    assert response.status_code == 503
    assert response.json() == {
        "error": {"code": "service_unavailable", "message": "Service temporarily unavailable"}
    }


def test_selection_identity_failures_fail_closed():
    class NoWorkspace(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            request.state.workspace_id = None
            request.state.identity_unavailable = False
            return await call_next(request)

    missing = FastAPI()
    missing.add_middleware(NoWorkspace)
    missing.include_router(build_study_selection_router())
    assert TestClient(missing).post(
        "/api/study-notes/selection", json={"screenshotIds": [1]}
    ).status_code == 401

    class Outage(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            request.state.workspace_id = None
            request.state.identity_unavailable = True
            return await call_next(request)

    unavailable = FastAPI()
    unavailable.add_middleware(Outage)
    unavailable.include_router(build_study_selection_router())
    assert TestClient(unavailable).post(
        "/api/study-notes/selection", json={"screenshotIds": [1]}
    ).status_code == 503
