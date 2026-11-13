"""Public AI provider-mode route tests (Task 2.10 privacy accuracy)."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app import config
from app.db.base import Base
from app.main import create_app
from app.ratelimit import TokenBucketRateLimiter


def _client():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, class_=Session)
    limiter = TokenBucketRateLimiter(capacity=100, refill_per_sec=100.0)
    app = create_app(
        workspace_session_factory=factory,
        rate_limiter=limiter,
        workspace_rate_limiter=limiter,
    )
    return TestClient(app)


def test_provider_mode_exposes_only_the_allowlisted_public_field(monkeypatch):
    client = _client()
    monkeypatch.setattr(config, "AI_PROVIDER", "fake")

    response = client.get("/api/ai/provider-mode")

    assert response.status_code == 200
    assert response.json() == {"mode": "fake"}
    assert "key" not in response.text.lower()
    assert "model" not in response.text.lower()


def test_provider_mode_contract_can_report_openai_without_configuration_details(monkeypatch):
    client = _client()
    monkeypatch.setattr(config, "AI_PROVIDER", "openai")

    response = client.get("/api/ai/provider-mode")

    assert response.status_code == 200
    assert response.json() == {"mode": "openai"}
