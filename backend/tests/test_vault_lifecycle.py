"""Archive (Safe Burial) / Restore (Resurrection) tests (Task 1.10).

Temporary SQLite DB + temporary private storage + synthetic images. Covers the
active<->archived state machine, SHA-256 restore verification, ID stability,
workspace isolation, idempotency, duplicate relationships, failure rollback, and
the no-delete / no-rewrite guarantees.
"""

from __future__ import annotations

import hashlib
import io
import os

import pytest
from PIL import Image
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db import models
from app.storage.file_store import FileStore
from app.services.vault_service import (
    LifecycleResult,
    ScreenshotNotFoundError,
    VaultService,
    VaultServiceError,
)


def png(color=(3, 6, 9)) -> bytes:
    b = io.BytesIO()
    Image.new("RGB", (10, 10), color).save(b, format="PNG")
    return b.getvalue()


@pytest.fixture()
def ctx(tmp_path):
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    store = FileStore(tmp_path / "storage")
    with Session(eng) as s:
        wa = models.Workspace(token_hash="a" * 64)
        wb = models.Workspace(token_hash="b" * 64)
        s.add_all([wa, wb])
        s.commit()
        yield s, eng, store, wa.id, wb.id
    for p in (tmp_path / "storage").rglob("*"):
        try:
            os.chmod(p, 0o700)
        except OSError:
            pass


def _blob_path(store, s, blob_id):
    blob = s.get(models.ContentBlob, blob_id)
    return store.root / blob.storage_path, blob


def test_archive_then_restore_round_trip(ctx):
    s, eng, store, wa, _ = ctx
    v = VaultService(s, wa, file_store=store)
    data = png()
    r = v.upload(data, filename="x.png")
    sid = r.screenshot_id

    ar = v.archive(sid)
    assert isinstance(ar, LifecycleResult)
    assert ar.status == "successful" and ar.state == "archived"
    assert s.get(models.Screenshot, sid).state == "archived"

    rr = v.restore(sid)
    assert rr.status == "successful" and rr.state == "active"
    assert s.get(models.Screenshot, sid).state == "active"


def test_bytes_and_checksum_unchanged_throughout(ctx):
    s, eng, store, wa, _ = ctx
    v = VaultService(s, wa, file_store=store)
    data = png((1, 2, 3))
    r = v.upload(data, filename="x.png")
    path, blob = _blob_path(store, s, r.content_blob_id)
    inode = path.stat().st_ino
    sha = hashlib.sha256(data).hexdigest()

    v.archive(r.screenshot_id)
    v.restore(r.screenshot_id)

    assert path.stat().st_ino == inode  # same file, never moved/rewritten
    assert path.read_bytes() == data
    assert s.get(models.ContentBlob, r.content_blob_id).sha256 == sha


def test_screenshot_id_stable_across_transitions(ctx):
    s, _, store, wa, _ = ctx
    v = VaultService(s, wa, file_store=store)
    sid = v.upload(png(), filename="x.png").screenshot_id
    assert v.archive(sid).screenshot_id == sid
    assert v.restore(sid).screenshot_id == sid
    assert s.get(models.Screenshot, sid).id == sid


def test_archived_screenshot_still_retrievable(ctx):
    s, _, store, wa, _ = ctx
    v = VaultService(s, wa, file_store=store)
    sid = v.upload(png(), filename="x.png").screenshot_id
    v.archive(sid)
    # Retrievable via the authorized data layer, listed under 'archived'.
    assert v._repo.get_screenshot(sid) is not None
    archived = v._repo.list_screenshots(state="archived")
    assert any(sh.id == sid for sh in archived)


def test_forced_checksum_mismatch_restore_fails_and_retains(ctx):
    s, eng, store, wa, _ = ctx
    v = VaultService(s, wa, file_store=store)
    r = v.upload(png(), filename="x.png")
    sid = r.screenshot_id
    v.archive(sid)
    path, _ = _blob_path(store, s, r.content_blob_id)
    os.chmod(path, 0o644)
    path.write_bytes(b"corrupted bytes not matching the checksum")

    rr = v.restore(sid)
    assert rr.status == "failed"
    assert rr.state == "archived"
    # State unchanged; corrupt bytes retained (never deleted or rewritten).
    assert s.get(models.Screenshot, sid).state == "archived"
    assert path.read_bytes() == b"corrupted bytes not matching the checksum"


def test_foreign_workspace_archive_denied_without_disclosure(ctx):
    s, _, store, wa, wb = ctx
    va = VaultService(s, wa, file_store=store)
    sid = va.upload(png(), filename="x.png").screenshot_id
    vb = VaultService(s, wb, file_store=store)
    with pytest.raises(ScreenshotNotFoundError) as exc:
        vb.archive(sid)
    # Generic message: no workspace id, no path, no foreign screenshot detail.
    assert str(wa) not in str(exc.value)
    # A's screenshot is unchanged.
    assert s.get(models.Screenshot, sid).state == "active"


def test_foreign_workspace_restore_denied(ctx):
    s, _, store, wa, wb = ctx
    va = VaultService(s, wa, file_store=store)
    sid = va.upload(png(), filename="x.png").screenshot_id
    va.archive(sid)
    vb = VaultService(s, wb, file_store=store)
    with pytest.raises(ScreenshotNotFoundError):
        vb.restore(sid)
    assert s.get(models.Screenshot, sid).state == "archived"


def test_idempotent_repeated_archive_and_restore(ctx):
    s, _, store, wa, _ = ctx
    v = VaultService(s, wa, file_store=store)
    sid = v.upload(png(), filename="x.png").screenshot_id
    assert v.archive(sid).state == "archived"
    assert v.archive(sid).state == "archived"  # no-op success
    assert v.restore(sid).state == "active"
    assert v.restore(sid).state == "active"    # no-op success


def test_duplicate_relationships_preserved_across_lifecycle(ctx):
    s, _, store, wa, _ = ctx
    v = VaultService(s, wa, file_store=store)
    data = png((9, 9, 9))
    r1 = v.upload(data, filename="d1.png")
    r2 = v.upload(data, filename="d2.png")  # duplicate of r1
    assert r2.is_duplicate is True
    dup_ref = s.get(models.Screenshot, r2.screenshot_id).is_duplicate_of
    shared_blob = s.get(models.Screenshot, r1.screenshot_id).content_blob_id

    # Archive + restore the duplicate; relationships must be intact.
    v.archive(r2.screenshot_id)
    v.restore(r2.screenshot_id)
    shot2 = s.get(models.Screenshot, r2.screenshot_id)
    assert shot2.is_duplicate_of == dup_ref
    assert shot2.content_blob_id == shared_blob
    # Still one shared blob.
    assert s.scalar(select(func.count()).select_from(models.ContentBlob)) == 1


def test_db_failure_rolls_back_state_transition(ctx, monkeypatch):
    s, _, store, wa, _ = ctx
    v = VaultService(s, wa, file_store=store)
    sid = v.upload(png(), filename="x.png").screenshot_id
    # Force the commit to fail during archive.
    real_commit = s.commit
    monkeypatch.setattr(s, "commit", lambda: (_ for _ in ()).throw(RuntimeError("fail")))
    with pytest.raises(VaultServiceError):
        v.archive(sid)
    monkeypatch.setattr(s, "commit", real_commit)
    s.rollback()
    # State unchanged (still active) after the rolled-back archive.
    assert s.get(models.Screenshot, sid).state == "active"


def test_no_permanent_delete_path_exists(ctx):
    s, _, store, wa, _ = ctx
    v = VaultService(s, wa, file_store=store)
    # Neither the service nor the file store exposes a delete/remove API.
    for name in ("delete", "remove", "delete_screenshot", "purge", "destroy"):
        assert not hasattr(v, name)
        assert not hasattr(store, name)


def test_no_files_published_or_rewritten_during_lifecycle(ctx):
    s, _, store, wa, _ = ctx
    v = VaultService(s, wa, file_store=store)
    r = v.upload(png(), filename="x.png")
    root = store.root
    before = {str(p): (p.stat().st_ino, p.stat().st_mtime, p.read_bytes())
              for p in root.rglob("*") if p.is_file()}
    v.archive(r.screenshot_id)
    v.restore(r.screenshot_id)
    after = {str(p): (p.stat().st_ino, p.stat().st_mtime, p.read_bytes())
             for p in root.rglob("*") if p.is_file()}
    assert before.keys() == after.keys()  # no files added or removed
    assert before == after                 # no file rewritten/moved
