"""Analysis read/edit + study-note list/versions route tests (Task 2.10 integration).

Real HTTP path, temp SQLite + private storage, synthetic images, FakeAIAdapter
(deterministic fixtures; it does not understand screenshots, no network).
"""

from __future__ import annotations

import hashlib
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
from app.ai import provider
from app.ai.fake_adapter import FakeAIAdapter
from app.db import models
from app.db.base import Base
from app.main import create_app
from app.ratelimit import TokenBucketRateLimiter

TRUSTED = config.TRUSTED_ORIGINS[0]
HDRS = {"Origin": TRUSTED, "X-Requested-With": "fetch"}


def png(color=(5, 6, 7)) -> bytes:
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
    lim = TokenBucketRateLimiter(capacity=10_000, refill_per_sec=1000.0)
    wlim = TokenBucketRateLimiter(capacity=10_000, refill_per_sec=1000.0)
    app = create_app(workspace_session_factory=factory, rate_limiter=lim, workspace_rate_limiter=wlim)
    c = TestClient(app)
    c.get("/api/workspace")
    saved = provider.get_ai_adapter()
    provider.set_ai_adapter(FakeAIAdapter())
    yield c, eng, tmp_path
    provider.set_ai_adapter(saved)
    for p in (tmp_path / "storage").rglob("*"):
        try:
            os.chmod(p, 0o700)
        except OSError:
            pass


def _classified(c, color=(5, 6, 7)):
    r = c.post("/api/screenshots", headers=HDRS, files=[("files", ("a.png", png(color), "image/png"))])
    sid = r.json()["results"][0]["screenshotId"]
    c.post(f"/api/screenshots/{sid}/consent", headers=HDRS, json={"granted": True})
    op = c.post(f"/api/screenshots/{sid}/classify", headers=HDRS).json()["operationId"]
    assert c.get(f"/api/operations/{op}").json()["status"] == "successful"
    return sid


def test_read_analysis_returns_category_epitaph_and_visible_facts(client):
    c, _, _ = client
    sid = _classified(c)
    r = c.get(f"/api/screenshots/{sid}/analysis")
    assert r.status_code == 200
    body = r.json()
    assert body["classification"]["category"] == "Reference/Document"
    assert body["classification"]["origin"] == "ai_generate"
    assert body["classification"]["versionNumber"] == 1
    assert body["epitaph"]["text"] and "visibleFacts" in body["epitaph"]
    assert body["epitaph"]["origin"] == "ai_generate"


def test_unanalysed_missing_and_foreign_are_identical_404(client):
    c, _, _ = client
    r = c.post("/api/screenshots", headers=HDRS, files=[("files", ("a.png", png(), "image/png"))])
    fresh = r.json()["results"][0]["screenshotId"]
    sid = _classified(c, (9, 9, 9))
    unanalysed = c.get(f"/api/screenshots/{fresh}/analysis")
    missing = c.get("/api/screenshots/987654/analysis")
    c.cookies.clear()
    c.get("/api/workspace")
    foreign = c.get(f"/api/screenshots/{sid}/analysis")
    foreign_edit = c.post(f"/api/screenshots/{sid}/analysis/edits", headers=HDRS, json={"category": "Unknown/Needs Review"})
    assert unanalysed.status_code == missing.status_code == foreign.status_code == 404
    assert unanalysed.json() == missing.json() == foreign.json()
    assert foreign_edit.status_code == 404


def test_edit_appends_new_versions_and_retains_prior(client):
    c, eng, _ = client
    sid = _classified(c)
    r = c.post(f"/api/screenshots/{sid}/analysis/edits", headers=HDRS,
               json={"category": "Study/Knowledge", "epitaphText": "My own caption"})
    assert r.status_code == 200
    body = r.json()
    assert body["classification"]["category"] == "Study/Knowledge"
    assert body["classification"]["origin"] == "manual_edit"
    assert body["classification"]["versionNumber"] == 2
    assert body["epitaph"]["text"] == "My own caption"
    assert body["epitaph"]["origin"] == "manual_edit" and body["epitaph"]["versionNumber"] == 2
    # AI-observed visible facts are carried forward, not editable/erased.
    assert body["epitaph"]["visibleFacts"]
    with Session(eng) as s:
        rows = s.scalars(select(models.ArtifactVersion).order_by(models.ArtifactVersion.id)).all()
        assert [(v.version_number, v.origin) for v in rows] == [
            (1, "ai_generate"), (1, "ai_generate"), (2, "manual_edit"), (2, "manual_edit"),
        ]
        first = json.loads(rows[0].content_json)
        assert first["category"] == "Reference/Document"  # v1 untouched


def test_partial_edit_changes_only_supplied_field(client):
    c, eng, _ = client
    sid = _classified(c)
    before = c.get(f"/api/screenshots/{sid}/analysis").json()["epitaph"]["text"]
    r = c.post(f"/api/screenshots/{sid}/analysis/edits", headers=HDRS, json={"category": "Memory/Entertainment"})
    assert r.json()["epitaph"]["text"] == before
    assert r.json()["epitaph"]["versionNumber"] == 1
    with Session(eng) as s:
        assert s.scalar(select(func.count()).select_from(models.ArtifactVersion)) == 3


@pytest.mark.parametrize("payload", [
    {}, {"category": "Not A Category"}, {"epitaphText": ""}, {"epitaphText": "x" * 501},
    {"category": "Study/Knowledge", "extra": 1}, {"workspaceId": 1},
])
def test_invalid_edits_rejected_with_no_new_versions(client, payload):
    c, eng, _ = client
    sid = _classified(c)
    r = c.post(f"/api/screenshots/{sid}/analysis/edits", headers=HDRS, json=payload)
    assert r.status_code == 400
    with Session(eng) as s:
        assert s.scalar(select(func.count()).select_from(models.ArtifactVersion)) == 2


def test_edit_requires_csrf_header(client):
    c, eng, _ = client
    sid = _classified(c)
    r = c.post(f"/api/screenshots/{sid}/analysis/edits", headers={"Origin": TRUSTED}, json={"category": "Study/Knowledge"})
    assert r.status_code == 403
    assert c.get(f"/api/screenshots/{sid}/analysis").json()["classification"]["versionNumber"] == 1


def test_edit_never_touches_original_bytes_or_calls_ai(client):
    c, eng, tmp = client
    sid = _classified(c)
    with Session(eng) as s:
        blob = s.get(models.ContentBlob, s.get(models.Screenshot, sid).content_blob_id)
        path = tmp / "storage" / blob.storage_path
        before = path.read_bytes()
        ops_before = s.scalar(select(func.count()).select_from(models.Operation))
    c.post(f"/api/screenshots/{sid}/analysis/edits", headers=HDRS, json={"category": "Study/Knowledge"})
    assert path.read_bytes() == before
    assert hashlib.sha256(before).hexdigest() == blob.sha256
    with Session(eng) as s:
        assert s.scalar(select(func.count()).select_from(models.Operation)) == ops_before  # no new AI op


def _note(c, n=2):
    ids = []
    for i in range(n):
        r = c.post("/api/screenshots", headers=HDRS, files=[("files", (f"{i}.png", png((i * 50, 1, 1)), "image/png"))])
        sid = r.json()["results"][0]["screenshotId"]
        c.post(f"/api/screenshots/{sid}/consent", headers=HDRS, json={"granted": True})
        ids.append(sid)
    op = c.post("/api/study-notes", headers=HDRS, json={"screenshotIds": ids}).json()["operationId"]
    assert c.get(f"/api/operations/{op}").json()["status"] in {"successful", "uncertain"}
    return ids


def test_study_note_list_and_versions_are_workspace_scoped(client):
    c, _, _ = client
    ids = _note(c)
    notes = c.get("/api/study-notes").json()["notes"]
    assert len(notes) == 1 and notes[0]["sourceScreenshotIds"] == ids
    aid = notes[0]["artifactId"]
    c.post(f"/api/study-notes/{aid}/edits", headers=HDRS, json={"content": {"topic": "Edited"}})
    versions = c.get(f"/api/study-notes/{aid}/versions").json()["versions"]
    assert [(v["versionNumber"], v["origin"], v["isCurrent"]) for v in versions] == [
        (1, "ai_generate", False), (2, "manual_edit", True)]
    # Another workspace sees nothing and gets the generic 404.
    c.cookies.clear()
    c.get("/api/workspace")
    assert c.get("/api/study-notes").json() == {"notes": []}
    assert c.get(f"/api/study-notes/{aid}/versions").status_code == 404
