"""PBT-LIFECYCLE — property-based tests for archive/restore (Task 1.14).

Covers the two REQUIRED lifecycle properties from design.md, each run at >=100
Hypothesis examples. These exercise the REAL components — VaultService archive /
restore (Task 1.10), the write-once FileStore (Task 1.6), and the
WorkspaceRepository (Task 1.2) — against disposable temporary SQLite databases
and private storage directories. Inputs are bounded synthetic images plus
randomized lifecycle action sequences, so the properties are checked across many
archive/restore interleavings rather than a single hand-picked path.

Property definitions (quoted from .kiro/specs/.../design.md):
  P2  Archive/restore round-trip integrity — *for any* stored screenshot,
      archiving (Safe Burial) then restoring (Resurrection) returns it to the
      ACTIVE state and yields bytes whose SHA-256 equals the originally stored
      checksum.
  P8  Stable Screenshot ID — *for any* stored screenshot and *any* subsequent
      sequence of operations, its Screenshot ID never changes.

Enforces INV-2 (archived images can be restored), INV-3 (restored bytes match
the stored SHA-256), INV-10 (archive/restore never permanently deletes
originals). Validates Requirements 3.1, 3.2, 3.3, 14.2, 1.7.

Safety note: these tests NEVER weaken a production safety check. The forced
checksum-corruption case (a documented safe-failure path for P2/INV-3) corrupts
the stored file out-of-band and asserts the REAL restore guard reports a failed
restore and leaves the screenshot archived with its bytes untouched.
"""

from __future__ import annotations

import hashlib
import io
import os

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st
from PIL import Image
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db import models
from app.storage.file_store import FileStore, compute_sha256
from app.services.vault_service import (
    LifecycleResult,
    ScreenshotNotFoundError,
    VaultService,
)

# Shared settings: >=100 examples per property, generous deadline because real
# Pillow encode/decode + SQLite commits are exercised per example. Mirrors the
# PBT-STORAGE module (test_pbt_storage.py) for consistency.
PBT = settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)

# ---------------------------------------------------------------- strategies

_FORMATS = ("PNG", "JPEG", "WEBP")
_FMT_EXT = {"PNG": "png", "JPEG": "jpg", "WEBP": "webp"}


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


# A randomized sequence of lifecycle actions. "archive"/"restore" may repeat
# (idempotency), "read"/"list"/"read_image" are pure reads, "bad_op" is a safe
# failure against a non-existent id (must never mutate anything).
_ACTIONS = st.lists(
    st.sampled_from(["archive", "restore", "read", "list", "read_image", "bad_op"]),
    min_size=1,
    max_size=10,
)

# Sequences that always contain at least one full archive->restore for the P2
# round-trip property (so every example meaningfully exercises the round trip).
_ROUNDTRIP_PREFIX = st.lists(
    st.sampled_from(["archive", "restore"]),
    min_size=0,
    max_size=6,
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


def _blob_path(store: FileStore, session: Session, blob_id: int):
    blob = session.get(models.ContentBlob, blob_id)
    return store.root / blob.storage_path, blob


# ================================================================ P2
# P2: Archive/restore round-trip integrity — archiving then restoring returns the
# screenshot to ACTIVE and yields bytes whose SHA-256 equals the stored checksum.

# Feature: screenshot-graveyard, Property 2: Archive/restore round-trip integrity
@given(img=valid_images(), extra=_ROUNDTRIP_PREFIX)
@PBT
def test_p2_archive_restore_roundtrip_returns_active_and_matching_sha(img, extra, tmp_path_factory):
    """P2 — after an arbitrary archive/restore prefix followed by a final
    archive->restore, the screenshot is ACTIVE and the stored bytes still hash to
    the originally persisted checksum (INV-2, INV-3)."""
    root = tmp_path_factory.mktemp("p2")
    eng = _new_db()
    store = FileStore(root)
    try:
        with Session(eng) as s:
            wid = _make_workspace(s, "p2" + "0" * 62)
            v = VaultService(s, wid, file_store=store)
            r = v.upload(img["data"], filename="a." + _FMT_EXT[img["format"]])
            sid = r.screenshot_id
            original_blob_id = r.content_blob_id
            stored_sha = s.get(models.ContentBlob, original_blob_id).sha256
            assert stored_sha == img["sha256"]

            # Arbitrary prefix of (possibly repeated) archive/restore actions.
            for action in extra:
                if action == "archive":
                    v.archive(sid)
                else:
                    v.restore(sid)

            # The defining round trip: Safe Burial then Resurrection.
            ar = v.archive(sid)
            assert isinstance(ar, LifecycleResult)
            assert ar.status == "successful" and ar.state == "archived"
            assert s.get(models.Screenshot, sid).state == "archived"

            rr = v.restore(sid)
            assert rr.status == "successful" and rr.state == "active"
            assert s.get(models.Screenshot, sid).state == "active"

            # Bytes read back through the authorized data layer hash to the
            # originally stored checksum (INV-3), and equal the exact originals.
            original = v.read_original(sid)
            assert hashlib.sha256(original.data).hexdigest() == stored_sha
            assert original.data == img["data"]
            # The persisted checksum itself is unchanged.
            shot = s.get(models.Screenshot, sid)
            blob = s.get(models.ContentBlob, original_blob_id)
            assert shot.content_blob_id == original_blob_id
            assert blob.id == original_blob_id
            assert blob.sha256 == stored_sha
    finally:
        _chmod_cleanup(root)
        eng.dispose()


# Feature: screenshot-graveyard, Property 2: Archive/restore round-trip integrity
@given(img=valid_images(), cycles=st.integers(min_value=1, max_value=5))
@PBT
def test_p2_repeated_roundtrips_are_idempotent_and_integrity_preserving(img, cycles, tmp_path_factory):
    """P2 — repeating archive->restore N times always ends ACTIVE with matching
    SHA-256, and repeated archive/restore calls are idempotent no-op successes."""
    root = tmp_path_factory.mktemp("p2b")
    eng = _new_db()
    store = FileStore(root)
    try:
        with Session(eng) as s:
            wid = _make_workspace(s, "p2b" + "0" * 61)
            v = VaultService(s, wid, file_store=store)
            r = v.upload(img["data"], filename="r.img")
            sid = r.screenshot_id
            original_blob_id = r.content_blob_id
            stored_sha = s.get(models.ContentBlob, original_blob_id).sha256
            for _ in range(cycles):
                assert v.archive(sid).state == "archived"
                assert v.archive(sid).state == "archived"   # idempotent no-op
                assert v.restore(sid).state == "active"
                assert v.restore(sid).state == "active"     # idempotent no-op
                shot = s.get(models.Screenshot, sid)
                blob = s.get(models.ContentBlob, original_blob_id)
                assert shot.state == "active"
                assert shot.content_blob_id == original_blob_id
                assert blob.sha256 == stored_sha
            assert s.get(models.Screenshot, sid).state == "active"
            restored = v.read_original(sid)
            assert hashlib.sha256(restored.data).hexdigest() == stored_sha
            assert restored.data == img["data"]
    finally:
        _chmod_cleanup(root)
        eng.dispose()


# Feature: screenshot-graveyard, Property 2: Archive/restore round-trip integrity
@given(img=valid_images(), corrupt=st.binary(min_size=1, max_size=48))
@PBT
def test_p2_failed_restore_never_activates_and_preserves_bytes(img, corrupt, tmp_path_factory):
    """P2 safe-failure (INV-3/INV-4) — if the stored original is corrupted
    out-of-band, restore reports `failed`, the screenshot STAYS archived, and the
    (corrupt) bytes are left untouched. A failed restore never fakes success."""
    root = tmp_path_factory.mktemp("p2c")
    eng = _new_db()
    store = FileStore(root)
    try:
        with Session(eng) as s:
            wid = _make_workspace(s, "p2c" + "0" * 61)
            v = VaultService(s, wid, file_store=store)
            r = v.upload(img["data"], filename="c.img")
            sid = r.screenshot_id
            original_blob_id = r.content_blob_id
            path, blob = _blob_path(store, s, original_blob_id)
            stored_sha = blob.sha256
            v.archive(sid)
            corrupt = b"corrupt:" + corrupt  # never a valid supported image
            os.chmod(path, 0o644)
            path.write_bytes(corrupt)  # out-of-band corruption

            rr = v.restore(sid)
            assert rr.status == "failed"
            assert rr.state == "archived"
            # NOT activated, bytes untouched (never deleted/rewritten by restore).
            assert s.get(models.Screenshot, sid).state == "archived"
            assert s.get(models.Screenshot, sid).content_blob_id == original_blob_id
            assert s.get(models.ContentBlob, original_blob_id).sha256 == stored_sha
            assert path.read_bytes() == corrupt
    finally:
        _chmod_cleanup(root)
        eng.dispose()


# ================================================================ P8
# P8: Stable Screenshot ID — for any stored screenshot and any subsequent
# sequence of operations, its Screenshot ID never changes.

# Feature: screenshot-graveyard, Property 8: Stable Screenshot ID
@given(img=valid_images(), actions=_ACTIONS)
@PBT
def test_p8_screenshot_id_stable_across_any_operation_sequence(img, actions, tmp_path_factory):
    """P8 — the Screenshot ID is invariant across any sequence of archive /
    restore / read operations (and safe failures), and the content_blob identity
    the screenshot references never changes either (INV-1/INV-3 corollary)."""
    root = tmp_path_factory.mktemp("p8")
    eng = _new_db()
    store = FileStore(root)
    try:
        with Session(eng) as s:
            wid = _make_workspace(s, "p8" + "0" * 62)
            v = VaultService(s, wid, file_store=store)
            r = v.upload(img["data"], filename="s." + _FMT_EXT[img["format"]])
            sid = r.screenshot_id
            original_blob_id = r.content_blob_id

            for action in actions:
                if action == "archive":
                    assert v.archive(sid).screenshot_id == sid
                elif action == "restore":
                    assert v.restore(sid).screenshot_id == sid
                elif action == "read":
                    store.read(
                        s.get(models.ContentBlob, original_blob_id).storage_path
                    )
                elif action == "list":
                    v.list_screenshots()
                elif action == "read_image":
                    assert v.read_original(sid).screenshot_id == sid
                elif action == "bad_op":
                    with pytest.raises(ScreenshotNotFoundError):
                        v.archive(999_000_111)

                # The id and its blob reference are invariant after every step.
                shot = s.get(models.Screenshot, sid)
                assert shot.id == sid
                assert shot.content_blob_id == original_blob_id

            # Exactly one screenshot row and one blob were ever created.
            assert s.scalar(
                select(func.count()).select_from(models.Screenshot).where(
                    models.Screenshot.workspace_id == wid
                )
            ) == 1
    finally:
        _chmod_cleanup(root)
        eng.dispose()


# Feature: screenshot-graveyard, Property 8: Stable Screenshot ID
@given(uploads=st.lists(valid_images(), min_size=2, max_size=5), rounds=st.integers(min_value=1, max_value=4))
@PBT
def test_p8_ids_stable_and_distinct_across_many_screenshots(uploads, rounds, tmp_path_factory):
    """P8 — with several screenshots in one workspace, every id is stable across
    repeated archive/restore rounds and ids remain distinct (no reuse/collision)."""
    root = tmp_path_factory.mktemp("p8b")
    eng = _new_db()
    store = FileStore(root)
    try:
        with Session(eng) as s:
            wid = _make_workspace(s, "p8b" + "0" * 61)
            v = VaultService(s, wid, file_store=store)
            ids = [v.upload(u["data"], filename="m.img").screenshot_id for u in uploads]
            original_blob_ids = {
                sid: s.get(models.Screenshot, sid).content_blob_id for sid in ids
            }
            assert len(set(ids)) == len(ids)  # distinct at creation

            for _ in range(rounds):
                for sid in ids:
                    assert v.archive(sid).screenshot_id == sid
                    assert v.restore(sid).screenshot_id == sid
                    assert s.get(models.Screenshot, sid).content_blob_id == original_blob_ids[sid]

            # Same set of ids still present, unchanged and still distinct.
            present = [
                sh.id
                for sh in s.scalars(
                    select(models.Screenshot).where(
                        models.Screenshot.workspace_id == wid
                    )
                )
            ]
            assert sorted(present) == sorted(ids)
            assert all(s.get(models.Screenshot, sid).state == "active" for sid in ids)
            assert all(
                s.get(models.Screenshot, sid).content_blob_id == original_blob_ids[sid]
                for sid in ids
            )
    finally:
        _chmod_cleanup(root)
        eng.dispose()
