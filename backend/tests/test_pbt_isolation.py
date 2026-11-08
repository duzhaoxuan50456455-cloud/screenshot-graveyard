"""PBT-ISOLATION (seed) — workspace isolation property tests (Task 1.15).

Covers the REQUIRED Property P24 for SCREENSHOTS and IMAGE BYTES (the scope of
this seed task; consent/artifacts/versions/compare/export arrive in later
phases). Each required property runs at >=100 Hypothesis examples.

These exercise the REAL components — the workspace-scoped WorkspaceRepository
(Task 1.2), VaultService archive/restore/read (Tasks 1.8/1.10/1.11), and the
actual FastAPI HTTP routes (list / image / archive / restore) through a
TestClient — against disposable temporary SQLite databases and private storage
directories. Inputs are bounded synthetic images plus randomized cross-workspace
ownership and access sequences.

Property definition (quoted from .kiro/specs/.../design.md):
  P24  Workspace isolation and existence non-disclosure — *for any* resource
       created in workspace A, every read or mutation attempt carrying workspace
       B's resolved identity returns `404 Not Found` (never `403`, never the
       data) and leaves A's data unchanged.

Enforces INV-1 (originals never modified) and INV-10 (archive/review never
deletes originals). Validates Requirements 9.9, 9.10, 12.6.

Naming + settings mirror the PBT-STORAGE / PBT-LIFECYCLE modules.

Safety note: these tests NEVER weaken an authorization check. Spoofing attempts
(client-supplied workspace id via query/header/body) are ASSERTED to be ignored;
the server-resolved cookie identity remains authoritative.
"""

from __future__ import annotations

import hashlib
import io
import os
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings, strategies as st
from PIL import Image
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app import config
import app.db.session as sess_mod
from app.db.base import Base
from app.db import models
from app.db.repository import WorkspaceRepository
from app.main import create_app
from app.ratelimit import TokenBucketRateLimiter
from app.storage.file_store import FileStore
from app.services.vault_service import (
    ScreenshotNotFoundError,
    VaultService,
    VaultServiceError,
)

PBT = settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)

TRUSTED = config.TRUSTED_ORIGINS[0]
HDRS = {"Origin": TRUSTED, "X-Requested-With": "fetch"}

# ---------------------------------------------------------------- strategies

_FORMATS = ("PNG", "JPEG", "WEBP")
_FMT_EXT = {"PNG": "png", "JPEG": "jpg", "WEBP": "webp"}
_FMT_CT = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}


def _encode(fmt: str, width: int, height: int, color: tuple[int, int, int]) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buf, format=fmt)
    return buf.getvalue()


@st.composite
def valid_images(draw) -> dict:
    """A genuine, small, decodable PNG/JPEG/WebP with varied size/colour."""
    fmt = draw(st.sampled_from(_FORMATS))
    width = draw(st.integers(min_value=1, max_value=64))
    height = draw(st.integers(min_value=1, max_value=64))
    color = (
        draw(st.integers(min_value=0, max_value=255)),
        draw(st.integers(min_value=0, max_value=255)),
        draw(st.integers(min_value=0, max_value=255)),
    )
    data = _encode(fmt, width, height, color)
    return {
        "data": data,
        "format": fmt,
        "sha256": hashlib.sha256(data).hexdigest(),
    }


# Mutation/read actions workspace B will attempt against A's screenshot.
_FOREIGN_ACTIONS = st.lists(
    st.sampled_from(["archive", "restore", "read_image", "get_screenshot", "list"]),
    min_size=1,
    max_size=8,
)

# ---------------------------------------------------------------- helpers


def _new_db():
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(eng)
    return eng


def _make_workspace(session: Session, token: str) -> int:
    ws = models.Workspace(token_hash=token)
    session.add(ws)
    session.commit()
    return ws.id


def _chmod_cleanup(root):
    for p in root.rglob("*"):
        try:
            os.chmod(p, 0o700)
        except OSError:
            pass


def _screenshot_snapshot(session: Session, sid: int) -> tuple:
    shot = session.get(models.Screenshot, sid)
    blob = session.get(models.ContentBlob, shot.content_blob_id)
    return (shot.state, shot.content_blob_id, blob.sha256, shot.is_duplicate_of)


def _workspace_counts(session: Session, workspace_id: int) -> tuple[int, int]:
    """Return the screenshot/blob counts for one specific workspace."""
    screenshots = session.scalar(
        select(func.count()).select_from(models.Screenshot).where(
            models.Screenshot.workspace_id == workspace_id
        )
    )
    blobs = session.scalar(
        select(func.count()).select_from(models.ContentBlob).where(
            models.ContentBlob.workspace_id == workspace_id
        )
    )
    return screenshots, blobs


# ================================================================ P24 (service/repo layer)
# P24: a resource created in workspace A is invisible and immutable to workspace
# B's identity — foreign access raises the SAME generic not-found used for a
# truly missing id, and A's data is unchanged.

# Feature: screenshot-graveyard, Property 24: Workspace isolation and existence non-disclosure
@given(a_img=valid_images(), actions=_FOREIGN_ACTIONS, token_salt=st.integers(0, 10_000))
@PBT
def test_p24_foreign_workspace_cannot_read_or_mutate_a_screenshot(a_img, actions, token_salt, tmp_path_factory):
    """P24 — B's VaultService/repository cannot read metadata, read bytes, or
    archive/restore A's screenshot; every attempt raises the generic
    ScreenshotNotFoundError and A's state/blob/bytes are unchanged."""
    root = tmp_path_factory.mktemp("p24svc")
    eng = _new_db()
    store = FileStore(root)
    try:
        with Session(eng) as s:
            wa = _make_workspace(s, f"a{token_salt:04d}" + "0" * 59)
            wb = _make_workspace(s, f"b{token_salt:04d}" + "0" * 59)
            va = VaultService(s, wa, file_store=store)
            r = va.upload(a_img["data"], filename="a." + _FMT_EXT[a_img["format"]])
            sid = r.screenshot_id

            before = _screenshot_snapshot(s, sid)
            counts_before = _workspace_counts(s, wa), _workspace_counts(s, wb)
            blob = s.get(models.ContentBlob, r.content_blob_id)
            on_disk_before = (store.root / blob.storage_path).read_bytes()

            vb = VaultService(s, wb, file_store=store)
            repo_b = WorkspaceRepository(s, wb)

            for action in actions:
                if action == "archive":
                    with pytest.raises(ScreenshotNotFoundError):
                        vb.archive(sid)
                elif action == "restore":
                    with pytest.raises(ScreenshotNotFoundError):
                        vb.restore(sid)
                elif action == "read_image":
                    with pytest.raises(ScreenshotNotFoundError):
                        vb.read_original(sid)
                elif action == "get_screenshot":
                    # Repository scoping: B sees A's id as simply absent (None).
                    assert repo_b.get_screenshot(sid) is None
                elif action == "list":
                    # B's listing never contains A's screenshot.
                    assert all(sh.id != sid for sh in vb.list_screenshots())

            # A's data is completely unchanged after every foreign attempt.
            after = _screenshot_snapshot(s, sid)
            assert after == before
            assert (_workspace_counts(s, wa), _workspace_counts(s, wb)) == counts_before
            assert (store.root / blob.storage_path).read_bytes() == on_disk_before
            # A can still read its own screenshot normally.
            assert va.read_original(sid).data == a_img["data"]
    finally:
        _chmod_cleanup(root)
        eng.dispose()


# Feature: screenshot-graveyard, Property 24: Workspace isolation and existence non-disclosure
@given(img=valid_images(), token_salt=st.integers(0, 10_000))
@PBT
def test_p24_foreign_id_and_missing_id_are_indistinguishable(img, token_salt, tmp_path_factory):
    """P24 — from B's perspective, A's (existing but foreign) id and a truly
    nonexistent id both raise the SAME generic error with the SAME message, so
    existence is never disclosed."""
    root = tmp_path_factory.mktemp("p24nd")
    eng = _new_db()
    store = FileStore(root)
    try:
        with Session(eng) as s:
            wa = _make_workspace(s, f"a{token_salt:04d}" + "1" * 59)
            wb = _make_workspace(s, f"b{token_salt:04d}" + "1" * 59)
            va = VaultService(s, wa, file_store=store)
            sid = va.upload(img["data"], filename="a.img").screenshot_id
            missing_id = sid + 10_000  # certainly not present

            vb = VaultService(s, wb, file_store=store)
            with pytest.raises(ScreenshotNotFoundError) as foreign_exc:
                vb.read_original(sid)
            with pytest.raises(ScreenshotNotFoundError) as missing_exc:
                vb.read_original(missing_id)
            # Identical generic message -> no existence disclosure.
            assert str(foreign_exc.value) == str(missing_exc.value)
            # And the message leaks neither the workspace id nor the sid.
            assert str(wa) not in str(foreign_exc.value)
            assert str(sid) not in str(foreign_exc.value)
    finally:
        _chmod_cleanup(root)
        eng.dispose()


# Feature: screenshot-graveyard, Property 24: Workspace isolation and existence non-disclosure
@given(shared=valid_images(), token_salt=st.integers(0, 10_000))
@PBT
def test_p24_identical_content_across_workspaces_stays_isolated(shared, token_salt, tmp_path_factory):
    """P24 + dedup isolation — identical bytes uploaded in BOTH A and B produce
    SEPARATE per-workspace blobs and screenshots; neither workspace can see or
    reach the other's record even though the content hash matches."""
    root = tmp_path_factory.mktemp("p24dup")
    eng = _new_db()
    store = FileStore(root)
    try:
        with Session(eng) as s:
            wa = _make_workspace(s, f"a{token_salt:04d}" + "2" * 59)
            wb = _make_workspace(s, f"b{token_salt:04d}" + "2" * 59)
            va = VaultService(s, wa, file_store=store)
            vb = VaultService(s, wb, file_store=store)
            ra = va.upload(shared["data"], filename="a.img")
            rb = vb.upload(shared["data"], filename="b.img")

            # Distinct screenshots and distinct per-workspace content_blobs.
            assert ra.screenshot_id != rb.screenshot_id
            assert ra.content_blob_id != rb.content_blob_id
            # Each workspace has exactly one blob for this content, owned by it.
            for wid, rid in ((wa, ra.content_blob_id), (wb, rb.content_blob_id)):
                blob = s.get(models.ContentBlob, rid)
                assert blob.workspace_id == wid
                assert blob.sha256 == shared["sha256"]
            # B cannot reach A's screenshot and vice versa.
            with pytest.raises(ScreenshotNotFoundError):
                vb.read_original(ra.screenshot_id)
            with pytest.raises(ScreenshotNotFoundError):
                va.read_original(rb.screenshot_id)
            # Neither screenshot is marked a duplicate of the other (no accidental link).
            assert s.get(models.Screenshot, ra.screenshot_id).is_duplicate_of is None
            assert s.get(models.Screenshot, rb.screenshot_id).is_duplicate_of is None
            # A's listing shows only A's screenshot; B's only B's.
            assert {sh.screenshot_id for sh in va.list_screenshots()} == {ra.screenshot_id}
            assert {sh.screenshot_id for sh in vb.list_screenshots()} == {rb.screenshot_id}
    finally:
        _chmod_cleanup(root)
        eng.dispose()


# ================================================================ P24 (HTTP path)
# The acceptance criterion requires the ACTUAL HTTP routes return 404 (never 403,
# never the data) for a foreign workspace, and that client-supplied identity
# hints never override the server-resolved cookie identity.


def _build_http_app(tmp_path):
    """Create a real FastAPI app wired to an isolated SQLite DB + private store.

    Returned helpers mint fresh workspaces by clearing the cookie (the middleware
    mints a new workspace on the next request), so one TestClient can drive many
    distinct workspaces across Hypothesis examples."""
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(eng)
    factory = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False, class_=Session)
    # Point the app's session + storage at the isolated test resources.
    sess_mod.engine = eng
    sess_mod.SessionLocal = factory
    config.COOKIE_SECURE = False
    config.STORAGE_ROOT = str(tmp_path / "storage")
    limiter = TokenBucketRateLimiter(capacity=100_000, refill_per_sec=100_000.0)
    ws_limiter = TokenBucketRateLimiter(capacity=100_000, refill_per_sec=100_000.0)
    app = create_app(
        workspace_session_factory=factory, rate_limiter=limiter, workspace_rate_limiter=ws_limiter
    )
    return TestClient(app), eng


@contextmanager
def _isolated_http_env(tmp_path):
    """Yield one real app with fresh DB/storage for exactly one generated case.

    The HTTP app uses module-level session/storage settings, so restore them in
    ``finally`` even when a generated assertion fails. Keeping this scope per
    Hypothesis example prevents workspace rows, cookies, rate-limit buckets, or
    original files from contaminating a later example.
    """
    saved = (sess_mod.engine, sess_mod.SessionLocal, config.COOKIE_SECURE, config.STORAGE_ROOT)
    client = None
    eng = None
    try:
        client, eng = _build_http_app(tmp_path)
        yield client, eng, tmp_path
    finally:
        sess_mod.engine, sess_mod.SessionLocal, config.COOKIE_SECURE, config.STORAGE_ROOT = saved
        for p in (tmp_path / "storage").rglob("*"):
            try:
                os.chmod(p, 0o700)
            except OSError:
                pass
        if client is not None:
            client.close()
        if eng is not None:
            eng.dispose()


def _mint_workspace(client: TestClient, eng) -> tuple[str, int]:
    """Mint a fresh cookie identity and prove its persisted workspace exists."""
    client.cookies.clear()
    r = client.get("/api/workspace")
    assert r.status_code == 200 and r.json()["workspaceActive"] is True
    token = client.cookies.get(config.WORKSPACE_COOKIE_NAME)
    assert token
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    with Session(eng) as s:
        workspace = s.scalar(
            select(models.Workspace).where(models.Workspace.token_hash == token_hash)
        )
        assert workspace is not None
        return token, workspace.id


def _fs_root():
    from pathlib import Path
    return Path(config.STORAGE_ROOT).resolve()

# Feature: screenshot-graveyard, Property 24: Workspace isolation and existence non-disclosure
@given(img=valid_images())
@PBT
def test_p24_http_foreign_workspace_gets_404_and_a_unchanged(img, tmp_path_factory):
    """P24 (HTTP) — A uploads; a different workspace B gets 404 (never 403, never
    bytes) on image read / archive / restore, and A's screenshot is unchanged and
    still readable afterward."""
    root = tmp_path_factory.mktemp("p24http")
    with _isolated_http_env(root) as (client, eng, _):
        # Workspace A uploads a screenshot.
        token_a, workspace_a = _mint_workspace(client, eng)
        up = client.post("/api/screenshots", headers=HDRS, files=[("files", ("a.png", img["data"], "image/png"))])
        assert up.status_code in (201, 207)
        sid = up.json()["results"][0]["screenshotId"]

        # Record A's persistent state before any foreign attempt.
        with Session(eng) as s:
            before = _screenshot_snapshot(s, sid)
            counts_before = _workspace_counts(s, workspace_a)

        # Switch to a genuinely independent server-side workspace B.
        token_b, workspace_b = _mint_workspace(client, eng)
        assert token_b != token_a
        assert workspace_b != workspace_a
        with Session(eng) as s:
            assert s.get(models.Workspace, workspace_a) is not None
            assert s.get(models.Workspace, workspace_b) is not None
            assert _workspace_counts(s, workspace_b) == (0, 0)

        r_img = client.get(f"/api/screenshots/{sid}/image")
        r_arch = client.post(f"/api/screenshots/{sid}/archive", headers=HDRS)
        r_rest = client.post(f"/api/screenshots/{sid}/restore", headers=HDRS)
        missing = client.get("/api/screenshots/987654321/image")
        missing_arch = client.post("/api/screenshots/987654321/archive", headers=HDRS)
        missing_rest = client.post("/api/screenshots/987654321/restore", headers=HDRS)

        assert r_img.status_code == 404
        assert r_arch.status_code == 404
        assert r_rest.status_code == 404
        # Never the bytes.
        assert r_img.content != img["data"]
        assert not r_img.headers.get("content-type", "").startswith("image/")
        # Foreign == missing: identical generic bodies (existence non-disclosure).
        assert r_img.json() == missing.json()
        assert r_arch.json() == missing_arch.json()
        assert r_rest.json() == missing_rest.json()
        # B's listing does not contain A's screenshot.
        lst = client.get("/api/screenshots")
        assert all(item["screenshotId"] != sid for item in lst.json()["screenshots"])

        # A's data is unchanged; no unexpected records were created for either workspace.
        with Session(eng) as s:
            assert _screenshot_snapshot(s, sid) == before
            assert _workspace_counts(s, workspace_a) == counts_before
            assert _workspace_counts(s, workspace_b) == (0, 0)
        with Session(eng) as s:
            shot = s.get(models.Screenshot, sid)
            blob = s.get(models.ContentBlob, shot.content_blob_id)
            assert shot.state == "active"
            assert hashlib.sha256((_fs_root() / blob.storage_path).read_bytes()).hexdigest() == blob.sha256



# Feature: screenshot-graveyard, Property 24: Workspace isolation and existence non-disclosure
@given(img=valid_images())
@PBT
def test_p24_http_client_supplied_identity_is_ignored(img, tmp_path_factory):
    """P24 (HTTP) — client-supplied workspace hints (query param, headers, body)
    never override the server-resolved cookie identity: B still gets 404 on A's
    screenshot even while spoofing A's workspace id."""
    root = tmp_path_factory.mktemp("p24spoof")
    with _isolated_http_env(root) as (client, eng, _):
        token_a, workspace_a = _mint_workspace(client, eng)
        up = client.post("/api/screenshots", headers=HDRS, files=[("files", ("a.png", img["data"], "image/png"))])
        sid = up.json()["results"][0]["screenshotId"]
        with Session(eng) as s:
            before = _screenshot_snapshot(s, sid)
            counts_before = _workspace_counts(s, workspace_a)

        # New workspace B, now spoofing A's ACTUAL id by every client-controlled means.
        token_b, workspace_b = _mint_workspace(client, eng)
        assert token_b != token_a and workspace_b != workspace_a
        spoof_headers = {
            **HDRS,
            "X-Workspace-Id": str(workspace_a),
            "X-Workspace": "owner",
            "Workspace-Id": str(workspace_a),
        }
        r = client.get(
            f"/api/screenshots/{sid}/image?workspace_id={workspace_a}&workspaceId={workspace_a}",
            headers=spoof_headers,
        )
        r_arch = client.post(
            f"/api/screenshots/{sid}/archive?workspace_id={workspace_a}",
            headers=spoof_headers,
            json={"workspace_id": workspace_a, "workspaceId": workspace_a},
        )
        assert r.status_code == 404
        assert r_arch.status_code == 404
        assert r.content != img["data"]
        # A unchanged despite spoofing; B gained no metadata or bytes.
        with Session(eng) as s:
            assert _screenshot_snapshot(s, sid) == before
            assert _workspace_counts(s, workspace_a) == counts_before
            assert _workspace_counts(s, workspace_b) == (0, 0)


# Feature: screenshot-graveyard, Property 24: Workspace isolation and existence non-disclosure
@given(screenshot_id=st.integers(min_value=1, max_value=1_000_000))
@PBT
def test_p24_http_identity_failure_fails_closed(screenshot_id, tmp_path_factory):
    """P24 corollary — when trusted identity cannot be resolved (DB outage) the
    routes fail CLOSED with 503, never defaulting to some other workspace's data.

    Uses the real WorkspaceMiddleware with a deliberately unavailable identity
    database, rather than injecting request state directly into the router."""
    root = tmp_path_factory.mktemp("p24outage")
    missing_eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    factory = sessionmaker(bind=missing_eng, autoflush=False, expire_on_commit=False, class_=Session)
    limiter = TokenBucketRateLimiter(capacity=100_000, refill_per_sec=100_000.0)
    ws_limiter = TokenBucketRateLimiter(capacity=100_000, refill_per_sec=100_000.0)
    app = create_app(
        workspace_session_factory=factory,
        rate_limiter=limiter,
        workspace_rate_limiter=ws_limiter,
    )
    try:
        with TestClient(app) as c:
            # A token forces the production middleware to look up the missing
            # workspace table and mark identity resolution unavailable.
            c.cookies.set(config.WORKSPACE_COOKIE_NAME, "unresolvable-token")
            r_img = c.get(f"/api/screenshots/{screenshot_id}/image")
            r_list = c.get("/api/screenshots")
            assert r_img.status_code == 503
            assert r_list.status_code == 503
            assert r_img.json()["error"]["code"] == "identity_unavailable"
            assert r_list.json()["error"]["code"] == "identity_unavailable"
    finally:
        missing_eng.dispose()
