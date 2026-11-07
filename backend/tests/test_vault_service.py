"""Vault_Service ingestion tests (Task 1.8).

Temporary SQLite DBs, temporary storage roots, synthetic images only. Covers the
validate -> dedup -> write-once -> persist flow and its concurrency/failure
safety. Deterministic where possible; concurrency tests use a shared on-disk DB
and real threads.
"""

from __future__ import annotations

import hashlib
import io
import threading

import pytest
from PIL import Image
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db import models
from app.db.repository import QuotaExceededError, WorkspaceRepository
from app.storage.file_store import FileStore
from app.services.vault_service import VaultService, VaultServiceError
from app.upload_validation import UploadValidationError


def png(color=(10, 20, 30), size=(12, 9)) -> bytes:
    b = io.BytesIO()
    Image.new("RGB", size, color).save(b, format="PNG")
    return b.getvalue()


@pytest.fixture()
def ctx(tmp_path):
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(eng)
    store = FileStore(tmp_path / "storage")
    with Session(eng) as s:
        wa = models.Workspace(token_hash="a" * 64)
        wb = models.Workspace(token_hash="b" * 64)
        s.add_all([wa, wb])
        s.commit()
        yield s, eng, store, wa.id, wb.id
    # teardown: make read-only published files writable for cleanup
    import os
    root = tmp_path / "storage"
    for p in root.rglob("*"):
        try:
            os.chmod(p, 0o700)
        except OSError:
            pass


def _count(s, model, **filters):
    stmt = select(func.count()).select_from(model)
    for k, v in filters.items():
        stmt = stmt.where(getattr(model, k) == v)
    return int(s.scalar(stmt) or 0)


def test_first_upload_success(ctx):
    s, _, store, wa, _ = ctx
    v = VaultService(s, wa, file_store=store)
    data = png()
    r = v.upload(data, filename="a.png")
    assert r.screenshot_id > 0
    assert r.is_duplicate is False
    assert r.sha256 == hashlib.sha256(data).hexdigest()
    assert r.mime_type == "image/png"
    # The content blob references the correct workspace and the file exists.
    blob = s.get(models.ContentBlob, r.content_blob_id)
    assert blob.workspace_id == wa
    assert store.read(blob.storage_path) == data


def test_identical_content_one_blob_two_screenshots(ctx):
    s, _, store, wa, _ = ctx
    v = VaultService(s, wa, file_store=store)
    data = png()
    r1 = v.upload(data, filename="1.png")
    r2 = v.upload(data, filename="2.png")
    assert r1.content_blob_id == r2.content_blob_id
    assert r1.screenshot_id != r2.screenshot_id
    assert _count(s, models.ContentBlob) == 1
    assert _count(s, models.Screenshot) == 2


def test_duplicate_flag_set_correctly(ctx):
    s, _, store, wa, _ = ctx
    v = VaultService(s, wa, file_store=store)
    data = png()
    r1 = v.upload(data)
    r2 = v.upload(data)
    assert r1.is_duplicate is False
    assert r2.is_duplicate is True
    shot2 = s.get(models.Screenshot, r2.screenshot_id)
    assert shot2.is_duplicate_of == r1.screenshot_id


def test_detected_mime_overrides_declared(ctx):
    s, _, store, wa, _ = ctx
    v = VaultService(s, wa, file_store=store)
    data = png()
    r = v.upload(data, filename="pretend.jpg", declared_mime="image/webp")
    assert r.mime_type == "image/png"
    blob = s.get(models.ContentBlob, r.content_blob_id)
    assert blob.mime_type == "image/png"


def test_checksum_is_of_exact_original_bytes(ctx):
    s, _, store, wa, _ = ctx
    v = VaultService(s, wa, file_store=store)
    data = png((99, 1, 2))
    r = v.upload(data)
    blob = s.get(models.ContentBlob, r.content_blob_id)
    assert blob.sha256 == hashlib.sha256(data).hexdigest()
    assert store.read(blob.storage_path) == data  # bytes preserved


def test_workspace_isolation(ctx):
    s, _, store, wa, wb = ctx
    va = VaultService(s, wa, file_store=store)
    vb = VaultService(s, wb, file_store=store)
    va.upload(png((1, 1, 1)))
    vb.upload(png((2, 2, 2)))
    assert WorkspaceRepository(s, wa).count_screenshots() == 1
    assert WorkspaceRepository(s, wb).count_screenshots() == 1
    # Same content in both workspaces -> separate blobs (per-workspace dedup).
    shared = png((5, 5, 5))
    ra = va.upload(shared)
    rb = vb.upload(shared)
    assert ra.content_blob_id != rb.content_blob_id


def test_quota_boundary(ctx):
    s, _, store, wa, _ = ctx
    v = VaultService(s, wa, file_store=store, screenshot_quota=2)
    v.upload(png((1, 0, 0)))
    v.upload(png((2, 0, 0)))
    with pytest.raises(QuotaExceededError):
        v.upload(png((3, 0, 0)))
    assert WorkspaceRepository(s, wa).count_screenshots() == 2


def test_validation_failure_leaves_db_and_store_unchanged(ctx):
    s, _, store, wa, _ = ctx
    v = VaultService(s, wa, file_store=store)
    before_blobs = _count(s, models.ContentBlob)
    before_shots = _count(s, models.Screenshot)
    with pytest.raises(UploadValidationError):
        v.upload(b"not an image", filename="bad.png")
    assert _count(s, models.ContentBlob) == before_blobs
    assert _count(s, models.Screenshot) == before_shots
    root = store.root
    published = [p for p in root.rglob("*") if p.is_file()] if root.exists() else []
    # No published original from the rejected upload.
    assert published == []


def test_stable_screenshot_ids_across_reopen(ctx, tmp_path):
    s, eng, store, wa, _ = ctx
    v = VaultService(s, wa, file_store=store)
    r = v.upload(png())
    sid = r.screenshot_id
    # Reopen a fresh session + fresh FileStore against the same root/engine.
    with Session(eng) as s2:
        shot = s2.get(models.Screenshot, sid)
        assert shot is not None and shot.id == sid
        blob = s2.get(models.ContentBlob, shot.content_blob_id)
        fresh_store = FileStore(store.root)
        assert fresh_store.read(blob.storage_path) is not None


def test_existing_original_never_overwritten(ctx):
    s, _, store, wa, _ = ctx
    v = VaultService(s, wa, file_store=store)
    data = png()
    r1 = v.upload(data)
    blob = s.get(models.ContentBlob, r1.content_blob_id)
    path = store.root / blob.storage_path
    inode = path.stat().st_ino
    mtime = path.stat().st_mtime
    # Second identical upload reuses the blob; the file is untouched.
    v.upload(data)
    assert path.stat().st_ino == inode
    assert path.stat().st_mtime == mtime


# ---- failure handling ----

def test_filestore_failure_prevents_persistence(ctx, monkeypatch):
    s, _, store, wa, _ = ctx
    v = VaultService(s, wa, file_store=store)

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(store, "write_once", boom)
    before = _count(s, models.Screenshot)
    with pytest.raises(VaultServiceError):
        v.upload(png())
    # No screenshot, no blob persisted.
    assert _count(s, models.Screenshot) == before
    assert _count(s, models.ContentBlob) == 0


def test_db_commit_failure_does_not_report_success(ctx, monkeypatch):
    s, _, store, wa, _ = ctx
    v = VaultService(s, wa, file_store=store)

    # Force the commit to fail after the file is published.
    real_commit = s.commit

    def failing_commit():
        raise RuntimeError("simulated commit failure")

    monkeypatch.setattr(s, "commit", failing_commit)
    with pytest.raises(VaultServiceError):
        v.upload(png())
    # Restore commit and confirm no screenshot persisted (rolled back).
    monkeypatch.setattr(s, "commit", real_commit)
    s.rollback()
    assert _count(s, models.Screenshot) == 0
    # The published file may remain as a harmless orphan (write-once, no delete);
    # that is acceptable and must NOT have been deleted.
    # (We don't assert its presence strictly, but assert no delete API exists.)
    assert not hasattr(store, "delete")


def test_orphan_blob_reused_by_later_identical_upload(ctx, monkeypatch):
    """After a persist failure that left a published file, a later identical
    upload reuses the bytes idempotently (no overwrite, no corruption)."""
    s, _, store, wa, _ = ctx
    v = VaultService(s, wa, file_store=store)
    data = png()

    # First attempt: publish succeeds, commit fails -> orphan file remains.
    real_commit = s.commit
    monkeypatch.setattr(s, "commit", lambda: (_ for _ in ()).throw(RuntimeError("fail")))
    with pytest.raises(VaultServiceError):
        v.upload(data)
    s.rollback()
    monkeypatch.setattr(s, "commit", real_commit)

    # Second attempt: publish is idempotent (file already there), commit succeeds.
    r = v.upload(data)
    blob = s.get(models.ContentBlob, r.content_blob_id)
    assert store.read(blob.storage_path) == data


# ---- concurrency ----

def _threaded_uploads(engine, store, workspace_id, payloads, quota=None):
    """Run N uploads concurrently, each in its own Session. Returns (results, errors)."""
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, class_=Session)
    results = []
    errors = []
    lock = threading.Lock()
    barrier = threading.Barrier(len(payloads))

    def worker(data):
        sess = factory()
        try:
            v = VaultService(sess, workspace_id, file_store=store, screenshot_quota=quota
                             if quota is not None else 10_000)
            barrier.wait()  # maximize overlap
            r = v.upload(data)
            with lock:
                results.append(r)
        except BaseException as e:  # noqa: BLE001
            with lock:
                errors.append(e)
        finally:
            sess.close()

    threads = [threading.Thread(target=worker, args=(p,)) for p in payloads]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results, errors


def test_concurrent_identical_uploads_single_blob(tmp_path):
    # Shared on-disk DB so multiple sessions see the same data with real locking.
    db = tmp_path / "concurrent.db"
    eng = create_engine(f"sqlite:///{db.as_posix()}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    store = FileStore(tmp_path / "storage")
    with Session(eng) as s:
        ws = models.Workspace(token_hash="c" * 64)
        s.add(ws)
        s.commit()
        wid = ws.id

    data = png((7, 7, 7))
    results, errors = _threaded_uploads(eng, store, wid, [data] * 8)

    # All uploads succeed (or converge); no corruption.
    assert not errors, errors
    with Session(eng) as s:
        assert _count(s, models.ContentBlob) == 1  # exactly one blob
        assert _count(s, models.Screenshot) == 8   # distinct screenshot per upload
    import os
    for p in (tmp_path / "storage").rglob("*"):
        try:
            os.chmod(p, 0o700)
        except OSError:
            pass


def test_concurrent_uploads_respect_quota(tmp_path):
    db = tmp_path / "quota.db"
    eng = create_engine(f"sqlite:///{db.as_posix()}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    store = FileStore(tmp_path / "storage")
    with Session(eng) as s:
        ws = models.Workspace(token_hash="d" * 64)
        s.add(ws)
        s.commit()
        wid = ws.id

    # 6 concurrent DISTINCT uploads, quota = 3. At most 3 may persist.
    payloads = [png((i, 0, 0)) for i in range(6)]
    results, errors = _threaded_uploads(eng, store, wid, payloads, quota=3)

    quota_errors = [e for e in errors if isinstance(e, QuotaExceededError)]
    # Every error must be a quota rejection (no corruption / unexpected errors).
    assert len(quota_errors) == len(errors), errors
    with Session(eng) as s:
        persisted = _count(s, models.Screenshot)
    # Quota is never exceeded, even under concurrency (criterion 11).
    assert persisted <= 3
    assert persisted + len(quota_errors) == 6
    import os
    for p in (tmp_path / "storage").rglob("*"):
        try:
            os.chmod(p, 0o700)
        except OSError:
            pass


# ---- review regressions: dedup-conflict discrimination & error hygiene ----

def test_unexpected_integrity_error_not_masked_as_dedup(ctx, monkeypatch):
    """An IntegrityError that is NOT our (workspace, sha256) dedup conflict must
    propagate (surfaced as VaultServiceError), not be silently swallowed."""
    s, _, store, wa, _ = ctx
    v = VaultService(s, wa, file_store=store)

    from app.db import repository as repo_mod
    from sqlalchemy.exc import IntegrityError

    real_create = repo_mod.WorkspaceRepository.create_content_blob

    def raise_unrelated(self, **kwargs):
        raise IntegrityError("unrelated constraint", {}, Exception("boom"))

    monkeypatch.setattr(repo_mod.WorkspaceRepository, "create_content_blob", raise_unrelated)
    # The blob never gets created, so the post-conflict re-fetch finds nothing
    # -> the IntegrityError is re-raised (wrapped as VaultServiceError), NOT
    # treated as a successful dedup.
    with pytest.raises(VaultServiceError):
        v.upload(png())
    monkeypatch.setattr(repo_mod.WorkspaceRepository, "create_content_blob", real_create)
    s.rollback()
    assert _count(s, models.Screenshot) == 0


def test_duplicate_reference_points_to_same_workspace_screenshot(ctx):
    s, _, store, wa, wb = ctx
    va = VaultService(s, wa, file_store=store)
    data = png((3, 3, 3))
    r1 = va.upload(data)
    r2 = va.upload(data)
    shot2 = s.get(models.Screenshot, r2.screenshot_id)
    ref = s.get(models.Screenshot, shot2.is_duplicate_of)
    assert ref is not None
    assert ref.workspace_id == wa  # duplicate ref is within the same workspace
    assert ref.id == r1.screenshot_id


def test_error_messages_do_not_leak_paths_or_workspace_ids(ctx, monkeypatch):
    s, _, store, wa, _ = ctx
    v = VaultService(s, wa, file_store=store)

    def boom(*a, **k):
        raise OSError(f"/private/storage/{wa}/secret/path failed")

    monkeypatch.setattr(store, "write_once", boom)
    with pytest.raises(VaultServiceError) as exc:
        v.upload(png())
    msg = str(exc.value)
    assert "/private/storage" not in msg
    assert str(wa) not in msg
    assert "secret" not in msg


def test_commit_failure_then_successful_retry_reuses_bytes(ctx, monkeypatch):
    """Concern 3: after a commit failure that leaves a published file, a retry
    reuses the original bytes safely and succeeds."""
    s, _, store, wa, _ = ctx
    v = VaultService(s, wa, file_store=store)
    data = png((4, 2, 0))
    real_commit = s.commit
    monkeypatch.setattr(s, "commit", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    with pytest.raises(VaultServiceError):
        v.upload(data)
    s.rollback()
    monkeypatch.setattr(s, "commit", real_commit)
    r = v.upload(data)  # retry
    blob = s.get(models.ContentBlob, r.content_blob_id)
    assert store.read(blob.storage_path) == data
    assert _count(s, models.Screenshot) == 1
