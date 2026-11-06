"""Tests for the write-once content-addressed file store (Task 1.6).

Uses temporary directories and synthetic bytes only — never real screenshots or
private data. Filesystem-level persistence is exercised here; durability on a
real mounted cloud volume across instance restarts is a SEPARATE, still-
outstanding proof (Task 1.0), not established by these tests.
"""

from __future__ import annotations

import os
import stat
import threading

import pytest

from app.storage.file_store import (
    FileStore,
    FileStoreError,
    IntegrityError,
    UnsafePathError,
    compute_sha256,
)

DATA = b"synthetic screenshot bytes \x89PNG\r\n demo payload"
SHA = compute_sha256(DATA)


@pytest.fixture()
def store(tmp_path):
    root = tmp_path / "storage"
    fs = FileStore(root)
    yield fs
    # Teardown: make everything writable so read-only published files/dirs clean
    # up with the tmp_path fixture.
    for p in root.rglob("*"):
        try:
            os.chmod(p, 0o700)
        except OSError:
            pass


def test_store_and_read_exact_bytes(store):
    rel = store.write_once(1, SHA, DATA)
    assert store.read(rel) == DATA


def test_content_addressed_layout(store):
    rel = store.write_once(7, SHA, DATA)
    assert rel == f"7/{SHA[0:2]}/{SHA[2:4]}/{SHA}"


def test_published_file_is_read_only(store):
    rel = store.write_once(1, SHA, DATA)
    mode = stat.S_IMODE((store.root / rel).stat().st_mode)
    assert mode == 0o444


def test_duplicate_write_is_idempotent_no_overwrite(store):
    rel1 = store.write_once(1, SHA, DATA)
    inode1 = (store.root / rel1).stat().st_ino
    mtime1 = (store.root / rel1).stat().st_mtime
    rel2 = store.write_once(1, SHA, DATA)
    assert rel1 == rel2
    # Same file, untouched (no overwrite / re-create).
    assert (store.root / rel2).stat().st_ino == inode1
    assert (store.root / rel2).stat().st_mtime == mtime1
    assert store.read(rel2) == DATA


def test_existing_destination_not_replaced(store):
    rel = store.write_once(1, SHA, DATA)
    # Simulate "same hash, different bytes" cannot happen for a valid sha; but
    # assert the published bytes are never replaced on a second valid call.
    before = (store.root / rel).read_bytes()
    store.write_once(1, SHA, DATA)
    assert (store.root / rel).read_bytes() == before


def test_per_workspace_physical_separation(store):
    rel_a = store.write_once(1, SHA, DATA)
    rel_b = store.write_once(2, SHA, DATA)
    assert rel_a != rel_b
    assert rel_a.startswith("1/") and rel_b.startswith("2/")
    assert store.read(rel_a) == DATA and store.read(rel_b) == DATA


def test_sha_mismatch_rejected(store):
    with pytest.raises(IntegrityError):
        store.write_once(1, "0" * 64, DATA)


def test_invalid_sha_format_rejected(store):
    with pytest.raises(FileStoreError):
        store.write_once(1, "not-a-sha", DATA)


def test_concurrent_identical_publish_single_blob(store):
    results: list[str] = []
    errors: list[BaseException] = []

    def worker():
        try:
            results.append(store.write_once(1, SHA, DATA))
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    # All logical publishes succeed and point at the one blob.
    assert len(results) == 16
    assert len(set(results)) == 1
    rel = results[0]
    assert store.read(rel) == DATA
    # Exactly one published file; no temp leftovers.
    tmp_dir = store.root / ".tmp"
    leftover = [p for p in tmp_dir.iterdir()] if tmp_dir.is_dir() else []
    assert leftover == []


def test_interrupted_write_cleans_temp_and_publishes_nothing(store, monkeypatch):
    # Force a failure AFTER the temp file is written but BEFORE publish.
    import app.storage.file_store as fs_mod

    def boom(src, dst):
        raise RuntimeError("simulated interruption before publish")

    monkeypatch.setattr(fs_mod.os, "link", boom)
    with pytest.raises(RuntimeError):
        store.write_once(1, SHA, DATA)
    # Nothing published at the final path.
    final = store.root / f"1/{SHA[0:2]}/{SHA[2:4]}/{SHA}"
    assert not final.exists()
    # Temp file cleaned up by try/finally.
    tmp_dir = store.root / ".tmp"
    leftover = [p for p in tmp_dir.iterdir()] if tmp_dir.is_dir() else []
    assert leftover == []


def test_path_traversal_rejected(store):
    for bad in ("../escape", "1/../../etc/passwd", "../../secret"):
        with pytest.raises(UnsafePathError):
            store.read(bad)
        assert store.exists(bad) is False


def test_absolute_path_rejected(store):
    with pytest.raises(UnsafePathError):
        store.read("/etc/passwd")
    assert store.exists("/etc/passwd") is False


def test_symlink_escape_rejected(store, tmp_path):
    # Publish a legit file so the root exists.
    store.write_once(1, SHA, DATA)
    # Create a secret file outside the root and a symlink inside pointing to it.
    secret = tmp_path / "secret.txt"
    secret.write_bytes(b"TOP SECRET")
    link_dir = store.root / "1"
    link = link_dir / "escape_link"
    try:
        os.symlink(secret, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported on this platform")
    # Reading through the symlink must be rejected (resolves outside root).
    with pytest.raises(UnsafePathError):
        store.read("1/escape_link")


def test_corruption_detected_on_read(store):
    rel = store.write_once(1, SHA, DATA)
    # Tamper with the stored bytes (make writable first).
    os.chmod(store.root / rel, 0o644)
    (store.root / rel).write_bytes(b"different bytes than the content address")
    with pytest.raises(IntegrityError):
        store.read(rel)


def test_size_integrity_check(store):
    rel = store.write_once(1, SHA, DATA)
    with pytest.raises(IntegrityError):
        store.read(rel, expected_size=len(DATA) + 1)


def test_expected_sha_mismatch_on_read(store):
    rel = store.write_once(1, SHA, DATA)
    with pytest.raises(IntegrityError):
        store.read(rel, expected_sha256="0" * 64)


def test_persistence_across_new_instance(store):
    rel = store.write_once(1, SHA, DATA)
    fresh = FileStore(store.root)
    assert fresh.read(rel) == DATA
    assert fresh.exists(rel) is True


def test_files_remain_inside_storage_root(store):
    rel = store.write_once(1, SHA, DATA)
    real_root = os.path.realpath(store.root)
    real_file = os.path.realpath(store.root / rel)
    assert os.path.commonpath([real_root, real_file]) == real_root


def test_sweep_removes_only_stale_temp_never_published(store):
    rel = store.write_once(1, SHA, DATA)
    tmp_dir = store.root / ".tmp"
    tmp_dir.mkdir(exist_ok=True)
    stale = tmp_dir / "stale.tmp"
    stale.write_bytes(b"junk")
    # Backdate the temp file so it counts as stale.
    old = 1_000_000.0
    os.utime(stale, (old, old))
    removed = store.sweep_stale_temp_files(older_than_seconds=1.0)
    assert removed == 1
    assert not stale.exists()
    # The published original is untouched.
    assert store.read(rel) == DATA


# ---- Review corrections (focused regressions) ----

def test_duplicate_write_verifies_existing_content(store):
    """A second write_once of valid content re-verifies the existing bytes and
    succeeds only if they are intact (Concern 1)."""
    rel = store.write_once(1, SHA, DATA)
    # Intact -> accepted.
    assert store.write_once(1, SHA, DATA) == rel


def test_corrupt_existing_destination_rejected_not_overwritten(store):
    """If the published file is corrupted, a duplicate write RAISES and never
    overwrites or silently accepts it (Concern 1)."""
    rel = store.write_once(1, SHA, DATA)
    os.chmod(store.root / rel, 0o644)
    (store.root / rel).write_bytes(b"corrupted bytes not matching the hash")
    with pytest.raises(IntegrityError):
        store.write_once(1, SHA, DATA)
    # The (corrupt) existing bytes are left untouched — never overwritten.
    assert (store.root / rel).read_bytes() == b"corrupted bytes not matching the hash"


def test_size_mismatch_existing_destination_rejected(store, monkeypatch):
    """An existing file with a different size than the incoming data is rejected
    before being treated as a duplicate (Concern 1)."""
    rel = store.write_once(1, SHA, DATA)
    os.chmod(store.root / rel, 0o644)
    # Same leading content address name, but wrong size on disk.
    (store.root / rel).write_bytes(DATA + b"EXTRA")
    with pytest.raises(IntegrityError):
        store.write_once(1, SHA, DATA)


def test_write_path_symlinked_destdir_rejected(store, tmp_path):
    """If the per-workspace destination directory is a symlink pointing OUTSIDE
    the storage root, write_once refuses to publish there (Concern 2)."""
    outside = tmp_path / "outside_target"
    outside.mkdir()
    # Pre-create the workspace dir as a symlink to an outside directory.
    ws_dir = store.root / "9"
    store.root.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(outside, ws_dir)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported on this platform")
    with pytest.raises(UnsafePathError):
        store.write_once(9, SHA, DATA)
    # Nothing was published into the outside target.
    assert not any(outside.rglob(SHA))


def test_cleanup_on_duplicate_leaves_published_original_intact(store):
    """A duplicate write exercises the temp-write + cleanup path; the previously
    published original must remain present and byte-identical, and no temp file
    may be left behind (Concern 3: cleanup never deletes a published original)."""
    rel = store.write_once(1, SHA, DATA)
    published = store.root / rel
    inode_before = published.stat().st_ino
    # Second write of identical content: verifies existing, cleans its temp.
    assert store.write_once(1, SHA, DATA) == rel
    assert published.exists()
    assert published.read_bytes() == DATA
    assert published.stat().st_ino == inode_before  # same file, not re-created
    tmp_dir = store.root / ".tmp"
    leftover = [p for p in tmp_dir.iterdir()] if tmp_dir.is_dir() else []
    assert leftover == []


def test_concurrent_writers_loser_verifies_not_overwrites(store):
    """Under the exclusive-create race, the losing writer keeps the winner's
    bytes (verified intact) and never overwrites them (Concern 3)."""
    import threading

    rels: list[str] = []
    errs: list[BaseException] = []

    def worker():
        try:
            rels.append(store.write_once(5, SHA, DATA))
        except BaseException as e:  # noqa: BLE001
            errs.append(e)

    threads = [threading.Thread(target=worker) for _ in range(12)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    assert not errs, errs
    assert len(set(rels)) == 1
    final = store.root / rels[0]
    assert final.read_bytes() == DATA
    assert stat.S_IMODE(final.stat().st_mode) == 0o444
