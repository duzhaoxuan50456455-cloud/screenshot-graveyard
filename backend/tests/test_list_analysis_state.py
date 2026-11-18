"""List endpoint exposes REAL analysis state so the UI can map Ghost honestly (Task 4.1).

A screenshot is a Ghost exactly when its `analysis` is null (no Classification +
Epitaph yet). Real routes, disposable SQLite, synthetic images, FakeAIAdapter (no
network; it does not understand images).
"""

from __future__ import annotations

import io
import os

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app import config
import app.db.session as sess_mod
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


@pytest.fixture()
def client(tmp_path, monkeypatch):
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    factory = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False, class_=Session)
    monkeypatch.setattr(sess_mod, "engine", eng)
    monkeypatch.setattr(sess_mod, "SessionLocal", factory)
    monkeypatch.setattr(config, "COOKIE_SECURE", False)
    monkeypatch.setattr(config, "STORAGE_ROOT", str(tmp_path / "storage"))
    lim = TokenBucketRateLimiter(capacity=100_000, refill_per_sec=100_000.0)
    c = TestClient(create_app(workspace_session_factory=factory, rate_limiter=lim, workspace_rate_limiter=lim))
    c.get("/api/workspace")
    saved = provider.get_ai_adapter()
    provider.set_ai_adapter(FakeAIAdapter())
    yield c
    provider.set_ai_adapter(saved)
    for p in (tmp_path / "storage").rglob("*"):
        try:
            os.chmod(p, 0o700)
        except OSError:
            pass


def _upload(c, n):
    r = c.post("/api/screenshots", headers=HDRS, files=[("files", (f"{n}.png", png((n * 20, 5, 5)), "image/png"))])
    return r.json()["results"][0]["screenshotId"]


def _analyse(c, sid):
    c.post(f"/api/screenshots/{sid}/consent", headers=HDRS, json={"granted": True})
    op = c.post(f"/api/screenshots/{sid}/classify", headers=HDRS).json()["operationId"]
    assert c.get(f"/api/operations/{op}").json()["status"] == "successful"


def _by_id(c, query=""):
    return {s["screenshotId"]: s for s in c.get(f"/api/screenshots{query}").json()["screenshots"]}


def test_only_analysed_screenshots_carry_analysis_and_the_rest_are_ghosts(client):
    ghost, revived = _upload(client, 1), _upload(client, 2)
    _analyse(client, revived)
    got = _by_id(client)
    assert got[ghost]["analysis"] is None
    a = got[revived]["analysis"]
    assert set(a) == {"category", "uncertain", "epitaph", "epitaphOrigin"}
    assert a["category"] == "Reference/Document" and a["epitaph"] and a["epitaphOrigin"] == "ai_generate"
    assert a["uncertain"] is False


def test_uploading_or_consenting_alone_never_makes_a_ghost_analysed(client):
    sid = _upload(client, 1)
    client.post(f"/api/screenshots/{sid}/consent", headers=HDRS, json={"granted": True})
    assert _by_id(client)[sid]["analysis"] is None      # consent is not analysis


def test_manual_edit_and_redo_are_reflected_in_the_listing(client):
    sid = _upload(client, 1)
    _analyse(client, sid)
    client.post(f"/api/screenshots/{sid}/analysis/edits", headers=HDRS, json={"epitaphText": "My own caption"})
    a = _by_id(client)[sid]["analysis"]
    assert (a["epitaph"], a["epitaphOrigin"]) == ("My own caption", "manual_edit")
    op = client.post(f"/api/screenshots/{sid}/analysis/redo", headers=HDRS).json()["operationId"]
    assert client.get(f"/api/operations/{op}").json()["status"] == "successful"
    a = _by_id(client)[sid]["analysis"]
    assert a["epitaphOrigin"] == "ai_redo" and a["epitaph"] != "My own caption"


def test_archived_screenshots_keep_their_analysis_and_filters_still_work(client):
    ghost, revived = _upload(client, 1), _upload(client, 2)
    _analyse(client, revived)
    client.post(f"/api/screenshots/{revived}/archive", headers=HDRS)
    client.post(f"/api/screenshots/{ghost}/archive", headers=HDRS)
    archived = _by_id(client, "?state=archived")
    assert set(archived) == {ghost, revived}
    assert archived[ghost]["analysis"] is None and archived[revived]["analysis"] is not None
    assert _by_id(client, "?state=active") == {}
    client.post(f"/api/screenshots/{revived}/restore", headers=HDRS)
    back = _by_id(client, "?state=active")
    assert set(back) == {revived} and back[revived]["analysis"] is not None   # analysis survives a round trip


def test_analysis_is_scoped_to_the_workspace(client):
    sid = _upload(client, 1)
    _analyse(client, sid)
    client.cookies.clear()
    client.get("/api/workspace")                       # workspace B
    other = _upload(client, 2)
    listing = _by_id(client)
    assert set(listing) == {other} and listing[other]["analysis"] is None
    assert "Synthetic" not in client.get("/api/screenshots").text   # A's Epitaph never leaks


def test_listing_is_read_only_and_calls_no_provider(client):
    class Spy(FakeAIAdapter):
        calls = 0

        def classify_and_epitaph(self, *a, **k):
            Spy.calls += 1
            return super().classify_and_epitaph(*a, **k)

    sid = _upload(client, 1)
    _analyse(client, sid)
    provider.set_ai_adapter(Spy())
    for _ in range(3):
        assert client.get("/api/screenshots").status_code == 200
    assert Spy.calls == 0


def test_unreadable_persisted_analysis_fails_closed_instead_of_becoming_a_ghost(client):
    """A corrupt retained analysis is not silently presented as unprocessed."""
    sid = _upload(client, 1)
    _analyse(client, sid)
    with sess_mod.SessionLocal() as session:
        epitaph = session.scalars(
            select(models.Artifact).where(
                models.Artifact.kind == "epitaph",
                models.Artifact.workspace_id == session.get(models.Screenshot, sid).workspace_id,
            )
        ).one()
        # Simulate a legacy/database-corruption condition. The normal ORM path
        # correctly forbids retained-version updates, so this intentionally uses
        # a direct SQL update solely to verify the list endpoint fails closed.
        session.execute(
            update(models.ArtifactVersion)
            .where(models.ArtifactVersion.id == epitaph.current_version_id)
            .values(content_json="{not valid json")
        )
        session.commit()

    response = client.get("/api/screenshots")
    assert response.status_code == 503
    assert response.json() == {
        "error": {"code": "service_unavailable", "message": "Service temporarily unavailable"}
    }
