"""Study Note generation HTTP-path tests (Task 2.7).

Real HTTP path via TestClient (BackgroundTasks run synchronously after the
response), temp SQLite + private storage, synthetic images, injected scripted
adapter (never a real provider).
"""

from __future__ import annotations

import io
import json
import os

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app import config
import app.db.session as sess_mod
import app.routers.study_notes as study_router
from app.ai import provider
from app.ai.fake_adapter import FakeAIAdapter
from app.db import models
from app.db.base import Base
from app.main import create_app
from app.ratelimit import TokenBucketRateLimiter

TRUSTED = config.TRUSTED_ORIGINS[0]
HDRS = {"Origin": TRUSTED, "X-Requested-With": "fetch"}


def png(color) -> bytes:
    b = io.BytesIO()
    Image.new("RGB", (12, 9), color).save(b, format="PNG")
    return b.getvalue()


class RecordingAdapter:
    def __init__(self):
        self._fake = FakeAIAdapter()
        self.calls = 0

    def classify_and_epitaph(self, image_bytes, mime):
        raise AssertionError("not used")

    def study_note(self, images):
        self.calls += 1
        return self._fake.study_note(images)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    factory = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False, class_=Session)
    monkeypatch.setattr(sess_mod, "engine", eng)
    monkeypatch.setattr(sess_mod, "SessionLocal", factory)
    monkeypatch.setattr(config, "COOKIE_SECURE", False)
    monkeypatch.setattr(config, "STORAGE_ROOT", str(tmp_path / "storage"))
    lim = TokenBucketRateLimiter(capacity=10_000, refill_per_sec=1000.0)
    wlim = TokenBucketRateLimiter(capacity=10_000, refill_per_sec=1000.0)
    app = create_app(workspace_session_factory=factory, rate_limiter=lim, workspace_rate_limiter=wlim)
    c = TestClient(app)
    c.get("/api/workspace")
    saved = provider.get_ai_adapter()
    adapter = RecordingAdapter()
    provider.set_ai_adapter(adapter)
    yield c, eng, adapter
    provider.set_ai_adapter(saved)
    for p in (tmp_path / "storage").rglob("*"):
        try:
            os.chmod(p, 0o700)
        except OSError:
            pass


def _shots(c, n=3, consent=True):
    ids = []
    for i in range(n):
        r = c.post("/api/screenshots", headers=HDRS,
                   files=[("files", (f"{i}.png", png((i * 30, 5, 5)), "image/png"))])
        ids.append(r.json()["results"][0]["screenshotId"])
    if consent:
        for sid in ids:
            c.post(f"/api/screenshots/{sid}/consent", headers=HDRS, json={"granted": True})
    return ids


def _post(c, ids, headers=HDRS):
    return c.post("/api/study-notes", headers=headers, json={"screenshotIds": ids})


def test_authorized_generation_202_then_polls_successful(client):
    c, eng, adapter = client
    ids = _shots(c, 4)
    r = _post(c, [ids[3], ids[0], ids[2], ids[1]])
    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "pending" and body["warning"] is None
    assert adapter.calls == 1
    poll = c.get(f"/api/operations/{body['operationId']}")
    assert poll.json()["status"] == "successful"
    with Session(eng) as s:
        art = s.scalars(select(models.Artifact)).one()
        content = json.loads(s.get(models.ArtifactVersion, art.current_version_id).content_json)
        assert content["references"] == [ids[3], ids[0], ids[2], ids[1]]
        assert s.scalar(select(func.count()).select_from(models.SourceReference)) == 4


def test_nonblocking_warning_outside_recommended_range(client):
    c, _, adapter = client
    ids = _shots(c, 2)
    r = _post(c, ids)
    assert r.status_code == 202 and r.json()["warning"]
    assert adapter.calls == 1                       # warning does not block


def test_one_unconsented_screenshot_blocks_batch_403_zero_calls(client):
    c, eng, adapter = client
    ids = _shots(c, 3)
    c.post(f"/api/screenshots/{ids[1]}/consent", headers=HDRS, json={"granted": False})
    r = _post(c, ids)
    assert r.status_code == 403 and r.json()["error"]["code"] == "consent_required"
    assert adapter.calls == 0
    with Session(eng) as s:
        assert s.scalar(select(func.count()).select_from(models.Operation)) == 0
        assert s.scalar(select(func.count()).select_from(models.Artifact)) == 0


def test_study_note_quota_rejection_prevents_pending_operation_and_adapter_call(client, monkeypatch):
    c, eng, adapter = client
    ids = _shots(c, 1)
    monkeypatch.setattr(config, "MAX_AI_CALLS_PER_WORKSPACE", 0)
    response = _post(c, ids)
    assert response.status_code == 429
    assert response.json() == {
        "error": {"code": "quota_exceeded", "message": "AI operation quota exceeded"}
    }
    assert adapter.calls == 0
    with Session(eng) as session:
        assert session.scalar(select(func.count()).select_from(models.Operation)) == 0


def test_consent_revoked_after_acceptance_before_execution(client, monkeypatch):
    c, eng, adapter = client
    ids = _shots(c, 3)
    deferred = []
    real_job = study_router._run_study_note_job
    monkeypatch.setattr(study_router, "_run_study_note_job", lambda *a: deferred.append(a))
    r = _post(c, ids)
    assert r.status_code == 202 and len(deferred) == 1 and adapter.calls == 0
    op_id = r.json()["operationId"]
    # Revoke consent for one screenshot AFTER acceptance, BEFORE the worker runs.
    c.post(f"/api/screenshots/{ids[2]}/consent", headers=HDRS, json={"granted": False})
    real_job(*deferred[0])                           # worker finally runs
    assert adapter.calls == 0
    assert c.get(f"/api/operations/{op_id}").json()["status"] == "failed"
    with Session(eng) as s:
        assert s.scalar(select(func.count()).select_from(models.Artifact)) == 0


def test_unexpected_worker_failure_records_failed_operation(client, monkeypatch):
    c, _, adapter = client
    ids = _shots(c, 1)

    def unexpected_failure(*_args, **_kwargs):
        raise RuntimeError("unexpected worker failure")

    monkeypatch.setattr(study_router.StudyNoteService, "run_generation", unexpected_failure)
    accepted = _post(c, ids)
    assert accepted.status_code == 202
    assert adapter.calls == 0
    assert c.get(f"/api/operations/{accepted.json()['operationId']}").json()["status"] == "failed"


def test_foreign_and_missing_screenshots_indistinguishable_404(client):
    c, _, adapter = client
    ids = _shots(c, 2)
    c.cookies.clear()
    c.get("/api/workspace")                          # workspace B
    foreign = _post(c, ids)
    missing = _post(c, [987654, 987655])
    assert foreign.status_code == 404 and missing.status_code == 404
    assert foreign.json() == missing.json()
    assert adapter.calls == 0


def test_invalid_selection_counts_rejected_400(client):
    c, _, adapter = client
    ids = _shots(c, 1)
    assert _post(c, []).status_code == 400
    assert _post(c, [ids[0], ids[0]]).status_code == 400
    assert _post(c, list(range(1, 14))).status_code == 400
    assert adapter.calls == 0


def test_requires_csrf_header(client):
    c, _, adapter = client
    ids = _shots(c, 1)
    assert _post(c, ids, headers={"Origin": TRUSTED}).status_code == 403
    assert adapter.calls == 0


def test_spoofed_workspace_params_ignored(client):
    c, _, adapter = client
    ids = _shots(c, 1)
    c.cookies.clear()
    c.get("/api/workspace")
    r = c.post("/api/study-notes?workspace_id=1", headers={**HDRS, "X-Workspace-Id": "1"},
               json={"screenshotIds": ids, "workspaceId": 1})
    assert r.status_code == 404 and adapter.calls == 0


def test_consent_database_failure_returns_503_without_adapter_call(client, monkeypatch):
    c, _, adapter = client
    ids = _shots(c, 1)
    from app.db.repository import WorkspaceRepository
    from sqlalchemy.exc import SQLAlchemyError

    def unavailable(self, _screenshot_id):
        raise SQLAlchemyError("database unavailable")

    monkeypatch.setattr(WorkspaceRepository, "get_consent", unavailable)
    response = _post(c, ids)
    assert response.status_code == 503
    assert response.json() == {
        "error": {"code": "service_unavailable", "message": "Service temporarily unavailable"}
    }
    assert adapter.calls == 0
