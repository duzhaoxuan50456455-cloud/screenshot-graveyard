"""AI Redo HTTP-path tests (Task 3.3).

Real routes via TestClient (BackgroundTasks run after the response), disposable
SQLite + private storage, synthetic images, and an injected recording adapter
around the network-free FakeAIAdapter. Proves 202 + poll, instruction handling,
consent / ownership / CSRF / quota gates with zero provider calls on refusal, and
that no sockets are opened in fake mode. Concurrency is Task 3.7 (not claimed).
"""

from __future__ import annotations

import io
import json
import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.middleware.base import BaseHTTPMiddleware

from app import config
import app.db.session as sess_mod
import app.routers.redo as redo_router
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


class Recording:
    def __init__(self):
        self._fake = FakeAIAdapter()
        self.study_calls, self.class_calls = [], []

    def classify_and_epitaph(self, image_bytes, mime, **kw):
        self.class_calls.append(kw)
        return self._fake.classify_and_epitaph(image_bytes, mime, **kw)

    def study_note(self, images, **kw):
        self.study_calls.append(kw)
        return self._fake.study_note(images, **kw)

    @property
    def total(self):
        return len(self.study_calls) + len(self.class_calls)


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
    wlim = TokenBucketRateLimiter(capacity=100_000, refill_per_sec=100_000.0)
    c = TestClient(create_app(workspace_session_factory=factory, rate_limiter=lim, workspace_rate_limiter=wlim))
    c.get("/api/workspace")
    saved, adapter = provider.get_ai_adapter(), Recording()
    provider.set_ai_adapter(adapter)
    yield c, eng, adapter
    provider.set_ai_adapter(saved)
    for p in (tmp_path / "storage").rglob("*"):
        try:
            os.chmod(p, 0o700)
        except OSError:
            pass


def _shots(c, n=3):
    ids = []
    for i in range(n):
        r = c.post("/api/screenshots", headers=HDRS,
                   files=[("files", (f"{i}.png", png((i * 30, 5, 5)), "image/png"))])
        sid = r.json()["results"][0]["screenshotId"]
        c.post(f"/api/screenshots/{sid}/consent", headers=HDRS, json={"granted": True})
        ids.append(sid)
    return ids


def _note(c, n=3):
    ids = _shots(c, n)
    op = c.post("/api/study-notes", headers=HDRS, json={"screenshotIds": ids}).json()["operationId"]
    assert c.get(f"/api/operations/{op}").json()["status"] == "successful"
    return c.get("/api/study-notes").json()["notes"][0]["artifactId"], ids


def _analysed(c):
    sid = _shots(c, 1)[0]
    op = c.post(f"/api/screenshots/{sid}/classify", headers=HDRS).json()["operationId"]
    assert c.get(f"/api/operations/{op}").json()["status"] == "successful"
    return sid


def _redo_note(c, aid, body=None, headers=HDRS):
    return c.post(f"/api/study-notes/{aid}/redo", headers=headers, json=body) if body is not None \
        else c.post(f"/api/study-notes/{aid}/redo", headers=headers)


def _ops(eng):
    with Session(eng) as s:
        return s.scalar(select(func.count()).select_from(models.Operation))


def _versions(eng, kind):
    with Session(eng) as s:
        return [(v.version_number, v.origin) for v in s.scalars(
            select(models.ArtifactVersion).join(models.Artifact, models.Artifact.id == models.ArtifactVersion.artifact_id)
            .where(models.Artifact.kind == kind).order_by(models.ArtifactVersion.artifact_id, models.ArtifactVersion.version_number))]


# ============================================================ Study Note redo

def test_note_redo_202_polls_to_successful_and_keeps_the_manual_edit(client):
    c, eng, ad = client
    aid, ids = _note(c)
    c.post(f"/api/study-notes/{aid}/edits", headers=HDRS, json={"content": {"topic": "My edit"}})
    r = _redo_note(c, aid, {"instructions": "focus on pitfalls"})
    assert r.status_code == 202
    body = r.json()
    assert (body["kind"], body["status"]) == ("redo", "pending")
    poll = c.get(f"/api/operations/{body['operationId']}").json()
    assert (poll["status"], poll["kind"], poll["artifactId"]) == ("successful", "redo", aid)
    assert ad.study_calls[-1] == {"instructions": "focus on pitfalls"}
    versions = c.get(f"/api/study-notes/{aid}/versions").json()["versions"]
    assert [(v["versionNumber"], v["origin"], v["isCurrent"]) for v in versions] == [
        (1, "ai_generate", False), (2, "manual_edit", False), (3, "ai_redo", True)]
    # The edited version is still readable AND exportable, unchanged.
    edited = next(v for v in versions if v["origin"] == "manual_edit")
    got = c.get(f"/api/study-notes/{aid}?versionId={edited['versionId']}").json()
    assert got["content"]["topic"] == "My edit"
    exp = c.get(f"/api/study-notes/{aid}/export?versionId={edited['versionId']}")
    assert exp.status_code == 200 and exp.text.startswith("# My edit")
    assert c.get(f"/api/study-notes/{aid}").json()["origin"] == "ai_redo"
    assert c.get(f"/api/study-notes/{aid}/sources").json()["sources"][0]["screenshotId"] == ids[0]


@pytest.mark.parametrize("make", [
    lambda c, aid: c.post(f"/api/study-notes/{aid}/redo", headers=HDRS),                       # no body
    lambda c, aid: c.post(f"/api/study-notes/{aid}/redo", headers=HDRS, json={}),
    lambda c, aid: c.post(f"/api/study-notes/{aid}/redo", headers=HDRS, json={"instructions": None}),
    lambda c, aid: c.post(f"/api/study-notes/{aid}/redo", headers=HDRS, json={"instructions": "  "}),
])
def test_instructions_are_optional_plain_regenerate_sends_none(client, make):
    c, _, ad = client
    aid, _ = _note(c)
    r = make(c, aid)
    assert r.status_code == 202
    assert ad.study_calls[-1] == {}                      # one-argument call, no instructions


@pytest.mark.parametrize("payload,raw", [
    ({"instructions": "x" * 1001}, None),
    ({"instructions": "bad\u0000char"}, None),
    ({"instructions": 12345}, None),
    ({"instructions": ["a"]}, None),
    ({"instructions": "ok", "workspaceId": 1}, None),     # smuggled field
    ({"unknown": 1}, None),
    (None, b"{not json"),
    (None, b'"just a string"'),
    (None, b"[1,2,3]"),
    (None, b'{"instructions": "' + b"a" * 20_000 + b'"}'),  # over the 16 KB read cap
])
def test_invalid_instructions_rejected_400_without_echo_or_side_effects(client, payload, raw):
    c, eng, ad = client
    aid, _ = _note(c)
    ops_before = _ops(eng)
    versions_before = _versions(eng, "study_note")
    calls_before = len(ad.study_calls)
    if raw is None:
        r = c.post(f"/api/study-notes/{aid}/redo", headers=HDRS, json=payload)
    else:
        r = c.post(f"/api/study-notes/{aid}/redo", headers={**HDRS, "Content-Type": "application/json"}, content=raw)
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_instructions"
    assert "bad" not in r.text and "aaaa" not in r.text    # submitted text never echoed
    assert len(ad.study_calls) == calls_before                # provider never reached
    assert _ops(eng) == ops_before and _versions(eng, "study_note") == versions_before


def test_redo_requires_the_csrf_header(client):
    c, eng, ad = client
    aid, _ = _note(c)
    calls = len(ad.study_calls)
    r = c.post(f"/api/study-notes/{aid}/redo", headers={"Origin": TRUSTED}, json={"instructions": "x"})
    assert r.status_code == 403
    assert len(ad.study_calls) == calls and _versions(eng, "study_note") == [(1, "ai_generate")]


def test_one_unconsented_source_gives_403_zero_provider_calls_no_operation(client):
    c, eng, ad = client
    aid, ids = _note(c)
    c.post(f"/api/screenshots/{ids[1]}/consent", headers=HDRS, json={"granted": False})
    calls, ops = len(ad.study_calls), _ops(eng)
    r = _redo_note(c, aid, {"instructions": "x"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "consent_required"
    assert len(ad.study_calls) == calls and _ops(eng) == ops
    assert _versions(eng, "study_note") == [(1, "ai_generate")]


def test_consent_revoked_after_acceptance_but_before_the_worker_runs(client, monkeypatch):
    c, eng, ad = client
    aid, ids = _note(c)
    deferred, real = [], redo_router._run_study_note_redo_job
    monkeypatch.setattr(redo_router, "_run_study_note_redo_job", lambda *a: deferred.append(a))
    calls = len(ad.study_calls)
    r = _redo_note(c, aid, {"instructions": "x"})
    assert r.status_code == 202 and len(deferred) == 1 and len(ad.study_calls) == calls
    c.post(f"/api/screenshots/{ids[2]}/consent", headers=HDRS, json={"granted": False})  # revoke
    real(*deferred[0])                                                                    # worker runs now
    assert len(ad.study_calls) == calls                                                   # provider NOT called
    poll = c.get(f"/api/operations/{r.json()['operationId']}").json()
    assert poll["status"] == "failed" and "consent" in poll["errorDetail"].lower()
    assert _versions(eng, "study_note") == [(1, "ai_generate")]


def test_foreign_and_missing_notes_are_indistinguishable_404(client):
    c, eng, ad = client
    aid, _ = _note(c)
    c.cookies.clear()
    c.get("/api/workspace")                                   # workspace B
    foreign = _redo_note(c, aid, {"instructions": "x"})
    missing = _redo_note(c, 987654, {"instructions": "x"})
    spoofed = c.post(f"/api/study-notes/{aid}/redo?workspace_id=1", headers={**HDRS, "X-Workspace-Id": "1"},
                     json={"instructions": "x"})
    assert foreign.status_code == missing.status_code == spoofed.status_code == 404
    assert foreign.json() == missing.json() == spoofed.json()
    assert len(ad.study_calls) == 1                           # only the original generation
    assert _versions(eng, "study_note") == [(1, "ai_generate")]


def test_a_classification_artifact_id_is_not_a_study_note(client):
    c, eng, ad = client
    _analysed(c)
    with Session(eng) as s:
        cid = s.scalars(select(models.Artifact).where(models.Artifact.kind == "classification")).first().id
    assert _redo_note(c, cid, {"instructions": "x"}).status_code == 404


def test_quota_exceeded_gives_429_and_no_provider_call(client, monkeypatch):
    c, eng, ad = client
    aid, _ = _note(c)
    used = _ops(eng)
    monkeypatch.setattr(config, "MAX_AI_CALLS_PER_WORKSPACE", used)   # allowance fully consumed
    calls = len(ad.study_calls)
    r = _redo_note(c, aid, {"instructions": "x"})
    assert r.status_code == 429 and r.json()["error"]["code"] == "quota_exceeded"
    assert len(ad.study_calls) == calls and _ops(eng) == used


def test_redo_is_metered_like_any_other_ai_operation(client, monkeypatch):
    c, eng, _ = client
    aid, _ = _note(c)
    monkeypatch.setattr(config, "MAX_AI_CALLS_PER_WORKSPACE", _ops(eng) + 1)
    assert _redo_note(c, aid).status_code == 202
    assert _redo_note(c, aid).status_code == 429                     # second one is over the cap


def test_fake_mode_redo_opens_no_network_connections(client, monkeypatch):
    import socket

    def boom(*a, **k):
        raise AssertionError("network used during fake-mode redo")

    c, _, _ = client
    aid, _ = _note(c)
    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    r = _redo_note(c, aid, {"instructions": "no network please"})
    assert r.status_code == 202
    assert c.get(f"/api/operations/{r.json()['operationId']}").json()["status"] == "successful"


def test_instruction_text_never_appears_in_any_response(client):
    c, _, _ = client
    aid, _ = _note(c)
    secret = "TOPSECRET-INSTRUCTION-9f3"
    r = _redo_note(c, aid, {"instructions": secret})
    op = r.json()["operationId"]
    texts = [r.text, c.get(f"/api/operations/{op}").text, c.get(f"/api/study-notes/{aid}").text,
             c.get(f"/api/study-notes/{aid}/versions").text, c.get(f"/api/study-notes/{aid}/export").text]
    assert all(secret not in t for t in texts)


# ================================================ Classification + Epitaph redo

def test_analysis_redo_202_polls_successful_and_keeps_the_manual_edit(client):
    c, eng, ad = client
    sid = _analysed(c)
    c.post(f"/api/screenshots/{sid}/analysis/edits", headers=HDRS,
           json={"category": "Memory/Entertainment", "epitaphText": "my caption"})
    r = c.post(f"/api/screenshots/{sid}/analysis/redo", headers=HDRS, json={"instructions": "be brief"})
    assert r.status_code == 202 and r.json()["kind"] == "redo"
    assert c.get(f"/api/operations/{r.json()['operationId']}").json()["status"] == "successful"
    assert ad.class_calls[-1] == {"instructions": "be brief"}
    now = c.get(f"/api/screenshots/{sid}/analysis").json()
    assert now["classification"]["origin"] == now["epitaph"]["origin"] == "ai_redo"
    assert now["classification"]["versionNumber"] == 3
    assert _versions(eng, "classification") == [(1, "ai_generate"), (2, "manual_edit"), (3, "ai_redo")]
    with Session(eng) as s:
        edited = s.scalars(select(models.ArtifactVersion).where(models.ArtifactVersion.origin == "manual_edit")).all()
        assert {json.loads(v.content_json).get("category") or json.loads(v.content_json).get("text") for v in edited} \
            == {"Memory/Entertainment", "my caption"}


def test_analysis_redo_gates(client):
    c, eng, ad = client
    sid = _analysed(c)
    calls = ad.total
    # CSRF
    assert c.post(f"/api/screenshots/{sid}/analysis/redo", headers={"Origin": TRUSTED}).status_code == 403
    # invalid instructions
    bad = c.post(f"/api/screenshots/{sid}/analysis/redo", headers=HDRS, json={"instructions": "x" * 2000})
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "invalid_instructions"
    # consent withdrawn
    c.post(f"/api/screenshots/{sid}/consent", headers=HDRS, json={"granted": False})
    assert c.post(f"/api/screenshots/{sid}/analysis/redo", headers=HDRS).status_code == 403
    # never-analysed screenshot and missing id: identical 404
    fresh = c.post("/api/screenshots", headers=HDRS,
                   files=[("files", ("z.png", png((1, 2, 3)), "image/png"))]).json()["results"][0]["screenshotId"]
    c.post(f"/api/screenshots/{fresh}/consent", headers=HDRS, json={"granted": True})
    r_fresh = c.post(f"/api/screenshots/{fresh}/analysis/redo", headers=HDRS)
    r_missing = c.post("/api/screenshots/987654/analysis/redo", headers=HDRS)
    assert r_fresh.status_code == r_missing.status_code == 404 and r_fresh.json() == r_missing.json()
    assert ad.total == calls                                   # provider never called by any refusal
    assert _versions(eng, "classification") == [(1, "ai_generate")]


def test_analysis_redo_foreign_workspace_404(client):
    c, eng, ad = client
    sid = _analysed(c)
    calls = ad.total
    c.cookies.clear()
    c.get("/api/workspace")
    r = c.post(f"/api/screenshots/{sid}/analysis/redo", headers=HDRS, json={"instructions": "x"})
    assert r.status_code == 404 and ad.total == calls
    assert _versions(eng, "classification") == [(1, "ai_generate")]


def test_analysis_redo_consent_revoked_before_worker_runs(client, monkeypatch):
    c, eng, ad = client
    sid = _analysed(c)
    deferred, real = [], redo_router._run_analysis_redo_job
    monkeypatch.setattr(redo_router, "_run_analysis_redo_job", lambda *a: deferred.append(a))
    calls = ad.total
    r = c.post(f"/api/screenshots/{sid}/analysis/redo", headers=HDRS)
    assert r.status_code == 202
    c.post(f"/api/screenshots/{sid}/consent", headers=HDRS, json={"granted": False})
    real(*deferred[0])
    assert ad.total == calls
    assert c.get(f"/api/operations/{r.json()['operationId']}").json()["status"] == "failed"
    assert _versions(eng, "classification") == [(1, "ai_generate")]


# ==================================================== fail-closed identity

@pytest.mark.parametrize("unavailable,expected", [(False, 401), (True, 503)])
def test_identity_failures_fail_closed(unavailable, expected):
    class Mid(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            request.state.workspace_id = None
            request.state.identity_unavailable = unavailable
            return await call_next(request)

    app = FastAPI()
    app.add_middleware(Mid)
    app.include_router(redo_router.build_redo_router())
    c = TestClient(app)
    assert c.post("/api/study-notes/1/redo", json={"instructions": "x"}).status_code == expected
    assert c.post("/api/screenshots/1/analysis/redo").status_code == expected
