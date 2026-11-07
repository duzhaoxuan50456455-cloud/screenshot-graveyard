"""PBT-STORAGE — property-based tests for the write-once storage stack (Task 1.13).

Covers the eight REQUIRED storage properties from design.md, each run at >=100
Hypothesis examples. These exercise the REAL components — FileStore (Task 1.6),
the content-authoritative image validator (Task 1.7), VaultService (Task 1.8),
and the WorkspaceRepository (Task 1.2) — against disposable temporary SQLite
databases and private storage directories. Inputs are bounded synthetic images
generated with meaningful variation (format, size, colour, dimensions) plus
deliberately invalid / decompression-bomb inputs.

Property definitions (quoted from .kiro/specs/.../design.md):
  P1  Source immutability under any operation.
  P3  Persisted checksum equals content hash.
  P4  Content-addressed deduplication within a workspace.
  P5  Consolidated image acceptance policy (extension-independent).
  P6  Decompression-bomb rejection.
  P9  Exclusive, concurrency-safe write-once publication.
  P10 Interrupted write leaves no published artifact; cleanup is crash-safe.
  P11 Exclusive create never overwrites existing bytes.

Enforces INV-1 (originals never overwritten), INV-3 (restored bytes match the
stored SHA-256), INV-10 (archive/review never deletes originals).

Safety note: these tests NEVER weaken a production safety check. Where a property
needs an "interrupted" write (P10), it injects the interruption at the real
exclusive-publication boundary in FileStore.write_once.
"""

from __future__ import annotations

import hashlib
import io
import os
import struct
import threading

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st
from PIL import Image
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app import config
from app.db.base import Base
from app.db import models
from app.storage.file_store import (
    FileStore,
    IntegrityError as FileStoreIntegrityError,
    compute_sha256,
)
from app.services.vault_service import VaultService
from app.upload_validation import (
    PayloadTooLargeError,
    UploadValidationError,
    validate_image_bytes,
    validate_image_stream,
)

# Shared settings: >=100 examples per property, generous deadline because real
# Pillow encode/decode + SQLite commits are exercised per example.
PBT = settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)

# ---------------------------------------------------------------- strategies

_FORMATS = ("PNG", "JPEG", "WEBP")
_FMT_MIME = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}
_FMT_EXT = {"PNG": "png", "JPEG": "jpg", "WEBP": "webp"}


def _encode(fmt: str, width: int, height: int, color: tuple[int, int, int]) -> bytes:
    """Encode a small RGB image to the given format, in-memory."""
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buf, format=fmt)
    return buf.getvalue()


@st.composite
def valid_images(draw) -> dict:
    """A genuine, small, decodable PNG/JPEG/WebP with varied size/colour.

    Returns a dict with the exact bytes, true format, and its real SHA-256.
    Dimensions are kept small (<=64 px) to keep per-example cost low while still
    varying meaningfully. WebP requires dimensions >= 1 and Pillow handles tiny
    sizes fine.
    """
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
        "width": width,
        "height": height,
    }


@st.composite
def declared_hints(draw) -> dict:
    """A possibly-mismatched declared filename extension + MIME type hint."""
    ext = draw(st.sampled_from(["png", "jpg", "jpeg", "webp", "gif", "txt", "bin", ""]))
    mime = draw(
        st.sampled_from(
            [
                "image/png",
                "image/jpeg",
                "image/webp",
                "image/gif",
                "application/octet-stream",
                "text/plain",
                "",
            ]
        )
    )
    name = draw(st.text(alphabet=st.characters(min_codepoint=97, max_codepoint=122), min_size=1, max_size=8))
    filename = f"{name}.{ext}" if ext else name
    return {"filename": filename, "mime": mime or None}


# Non-image byte blobs that must never validate as an image.
non_images = st.one_of(
    st.binary(min_size=1, max_size=256),
    st.builds(lambda s: s.encode("utf-8"), st.text(min_size=1, max_size=64)),
    st.just(b"GIF89a" + b"\x00" * 16),            # real but UNSUPPORTED format sig
    st.just(b"%PDF-1.4\n%fake"),                   # pdf-ish
    st.just(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8),   # PNG magic but not decodable
)


# ---------------------------------------------------------------- fixtures / helpers


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


# ================================================================ P5
# P5: Consolidated image acceptance policy (extension-independent).
# The accept decision = valid signature AND Pillow decodes AND within the
# dimension/MP cap AND <= 15 MB, INDEPENDENT of declared extension/MIME; a valid
# image with a disagreeing declared format is ACCEPTED and persisted as the
# DETECTED true format; rejected files fail safely with a file-named error.

# Feature: screenshot-graveyard, Property 5: Consolidated image acceptance policy (extension-independent)
@given(img=valid_images(), hint=declared_hints())
@PBT
def test_p5_valid_image_accepted_regardless_of_declared_hints(img, hint):
    """P5 — a genuine image is accepted no matter how its extension/MIME lie, and
    the persisted format is the DETECTED true format, not the declared value."""
    result = validate_image_bytes(
        img["data"], filename=hint["filename"], declared_mime=hint["mime"]
    )
    # Accepted: detected true format wins over any (mis)declared hint.
    assert result.detected_format == img["format"]
    assert result.mime_type == _FMT_MIME[img["format"]]
    assert result.data == img["data"]            # exact bytes, unmodified
    assert result.byte_size == len(img["data"])


# Feature: screenshot-graveyard, Property 5: Consolidated image acceptance policy (extension-independent)
@given(blob=non_images, hint=declared_hints())
@PBT
def test_p5_non_image_rejected_safely_even_with_image_hints(blob, hint):
    """P5 — a non-image is rejected safely (no crash) even when its declared
    extension/MIME claim to be an image; the error names the file. The declared
    hint is never the SOLE reason either way."""
    # Force image-looking hints so the declared value cannot be the reject cause.
    with pytest.raises(UploadValidationError) as exc:
        validate_image_bytes(blob, filename="claims-to-be.png", declared_mime="image/png")
    # Descriptive, file-named error (Req 1.4/1.5); no crash/other exception type.
    assert "claims-to-be.png" in str(exc.value)


# Feature: screenshot-graveyard, Property 5: Consolidated image acceptance policy (extension-independent)
@given(img=valid_images())
@PBT
def test_p5_oversize_rejected_before_full_buffering(img):
    """P5 — streaming validation stops at the cap rather than buffering all of
    an oversized body. The stream represents a much larger upload without
    allocating it as a test fixture."""
    cap = config.MAX_UPLOAD_BYTES

    class OversizedStream:
        def __init__(self, prefix: bytes, total_size: int) -> None:
            self.prefix = prefix
            self.total_size = total_size
            self.position = 0

        def read(self, size: int = -1) -> bytes:
            assert size >= 0  # production passes a bounded chunk size
            remaining = self.total_size - self.position
            count = min(size, remaining)
            start = self.position
            self.position += count
            prefix_part = self.prefix[start : min(start + count, len(self.prefix))]
            return prefix_part + b"\0" * (count - len(prefix_part))

    stream = OversizedStream(img["data"], cap * 2)
    with pytest.raises(PayloadTooLargeError):
        validate_image_stream(stream, filename="big.png", declared_mime="image/png")
    assert stream.position == cap + 1
    assert stream.position < stream.total_size


# ================================================================ P6
# P6: Decompression-bomb rejection — any input whose declared/encoded dimensions
# exceed the pixel cap is rejected via the megapixel/dimension guard without a
# full decode.

def _oversized_png_header(width: int, height: int) -> bytes:
    """A syntactically valid PNG stream header declaring huge dimensions.

    The IHDR carries the (enormous) width/height so the validator's header-based
    dimension/MP guard rejects it BEFORE any attempt to allocate the raster.
    """
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr_data = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8-bit RGB
    crc = 0  # the guard triggers before CRC/decode matters
    ihdr = struct.pack(">I", len(ihdr_data)) + b"IHDR" + ihdr_data + struct.pack(">I", crc)
    return sig + ihdr


# Feature: screenshot-graveyard, Property 6: Decompression-bomb rejection
@given(
    width=st.integers(min_value=config.MAX_IMAGE_DIMENSION_PX + 1, max_value=200_000),
    height=st.integers(min_value=1, max_value=200_000),
)
@PBT
def test_p6_oversized_dimension_png_rejected_without_decode(width, height, monkeypatch):
    """P6 — a PNG declaring a dimension beyond the cap is rejected by the guard.

    The crafted stream is only a header with giant dimensions; if the validator
    tried to fully decode it would be far slower / allocate huge memory. A fast,
    safe rejection shows the header guard fired first."""
    def fail_if_full_decode(*_args, **_kwargs):
        raise AssertionError("oversized image must be rejected before full decode")

    monkeypatch.setattr(Image.Image, "load", fail_if_full_decode)
    bomb = _oversized_png_header(width, height)
    with pytest.raises(UploadValidationError):
        validate_image_bytes(bomb, filename="bomb.png", declared_mime="image/png")


# Feature: screenshot-graveyard, Property 6: Decompression-bomb rejection
@given(
    # width*height over the ~40 MP cap but each dimension within the px cap.
    width=st.integers(min_value=7000, max_value=12000),
    height=st.integers(min_value=7000, max_value=12000),
)
@PBT
def test_p6_over_megapixel_cap_rejected(width, height, monkeypatch):
    """P6 — dimensions within the per-axis px cap but whose PRODUCT exceeds the
    megapixel cap are still rejected by the MP guard (no full decode)."""
    max_px = int(config.MAX_IMAGE_MEGAPIXELS * 1_000_000)
    assert width * height > max_px
    def fail_if_full_decode(*_args, **_kwargs):
        raise AssertionError("over-cap image must be rejected before full decode")

    monkeypatch.setattr(Image.Image, "load", fail_if_full_decode)
    bomb = _oversized_png_header(width, height)
    with pytest.raises(UploadValidationError):
        validate_image_bytes(bomb, filename="bigmp.png", declared_mime="image/png")


# ================================================================ P3
# P3: Persisted checksum equals content hash — the SHA-256 persisted with the
# metadata equals the SHA-256 of the exact stored bytes, and is the one used for
# restore verification.

# Feature: screenshot-graveyard, Property 3: Persisted checksum equals content hash
@given(img=valid_images())
@PBT
def test_p3_persisted_checksum_equals_content_hash(img, tmp_path_factory):
    """P3 — after an upload, the stored content_blob.sha256 equals both the
    hash of the validator's accepted bytes AND the hash of the bytes on disk."""
    root = tmp_path_factory.mktemp("p3")
    eng = _new_db()
    store = FileStore(root)
    try:
        with Session(eng) as s:
            wid = _make_workspace(s, "p3" + "0" * 62)
            v = VaultService(s, wid, file_store=store)
            r = v.upload(img["data"], filename="a." + _FMT_EXT[img["format"]])
            blob = s.get(models.ContentBlob, r.content_blob_id)
            # persisted checksum == hash of exact accepted bytes
            assert blob.sha256 == img["sha256"]
            assert r.sha256 == img["sha256"]
            # persisted checksum == hash of the bytes actually on disk
            on_disk = (store.root / blob.storage_path).read_bytes()
            assert compute_sha256(on_disk) == blob.sha256
            # The restore path compares a fresh content hash to this persisted
            # checksum before it returns an archived screenshot to active.
            verified = store.read(blob.storage_path, expected_sha256=blob.sha256)
            assert hashlib.sha256(verified).hexdigest() == blob.sha256
            assert v.archive(r.screenshot_id).state == "archived"
            restored = v.restore(r.screenshot_id)
            assert restored.status == "successful"
            assert restored.state == "active"
    finally:
        _chmod_cleanup(root)
        eng.dispose()


# ================================================================ P4
# P4: Content-addressed deduplication within a workspace — identical content
# produces exactly ONE content_blob but a DISTINCT screenshot record per upload.

# Feature: screenshot-graveyard, Property 4: Content-addressed deduplication within a workspace
@given(img=valid_images(), repeats=st.integers(min_value=2, max_value=5))
@PBT
def test_p4_identical_uploads_dedup_blob_distinct_screenshots(img, repeats, tmp_path_factory):
    """P4 — uploading identical bytes N times yields 1 blob + N screenshots, all
    referencing the shared blob; repeats beyond the first are flagged duplicate."""
    root = tmp_path_factory.mktemp("p4")
    eng = _new_db()
    store = FileStore(root)
    try:
        with Session(eng) as s:
            wid = _make_workspace(s, "p4" + "0" * 62)
            v = VaultService(s, wid, file_store=store)
            screenshot_ids = []
            for _ in range(repeats):
                r = v.upload(img["data"], filename="dup." + _FMT_EXT[img["format"]])
                screenshot_ids.append(r.screenshot_id)
            # Exactly ONE blob for this content in this workspace.
            blob_count = s.scalar(
                select(func.count()).select_from(models.ContentBlob).where(
                    models.ContentBlob.workspace_id == wid,
                    models.ContentBlob.sha256 == img["sha256"],
                )
            )
            assert blob_count == 1
            # A DISTINCT screenshot per upload (all unique ids).
            assert len(set(screenshot_ids)) == repeats
            shots = list(
                s.scalars(
                    select(models.Screenshot).where(
                        models.Screenshot.workspace_id == wid
                    )
                )
            )
            assert len(shots) == repeats
            # exactly one stored byte sequence on disk for this content
            blob = s.scalars(
                select(models.ContentBlob).where(
                    models.ContentBlob.workspace_id == wid,
                    models.ContentBlob.sha256 == img["sha256"],
                )
            ).one()
            assert all(sh.content_blob_id == blob.id for sh in shots)
    finally:
        _chmod_cleanup(root)
        eng.dispose()


# Feature: screenshot-graveyard, Property 4: Content-addressed deduplication within a workspace
@given(a=valid_images(), b=valid_images())
@PBT
def test_p4_distinct_content_distinct_blobs(a, b, tmp_path_factory):
    """P4 (complement) — distinct content yields distinct blobs; same content
    (even if the two draws collide) yields one. Verifies the dedup key is the
    content hash, not the upload count."""
    root = tmp_path_factory.mktemp("p4b")
    eng = _new_db()
    store = FileStore(root)
    try:
        with Session(eng) as s:
            wid = _make_workspace(s, "p4b" + "0" * 61)
            other_wid = _make_workspace(s, "p4c" + "0" * 61)
            v = VaultService(s, wid, file_store=store)
            other_v = VaultService(s, other_wid, file_store=store)
            v.upload(a["data"], filename="a.img")
            v.upload(b["data"], filename="b.img")
            other_v.upload(a["data"], filename="other-a.img")
            distinct_hashes = {a["sha256"], b["sha256"]}
            blob_count = s.scalar(
                select(func.count()).select_from(models.ContentBlob).where(
                    models.ContentBlob.workspace_id == wid
                )
            )
            assert blob_count == len(distinct_hashes)
            # Deduplication is scoped to a workspace: the same content has a
            # distinct metadata record and workspace-specific storage path.
            own_a = s.scalars(
                select(models.ContentBlob).where(
                    models.ContentBlob.workspace_id == wid,
                    models.ContentBlob.sha256 == a["sha256"],
                )
            ).one()
            other_a = s.scalars(
                select(models.ContentBlob).where(
                    models.ContentBlob.workspace_id == other_wid,
                    models.ContentBlob.sha256 == a["sha256"],
                )
            ).one()
            assert own_a.id != other_a.id
            assert own_a.storage_path != other_a.storage_path
    finally:
        _chmod_cleanup(root)
        eng.dispose()


# ================================================================ P1
# P1: Source immutability under any operation — the original bytes and persisted
# SHA-256 remain byte-for-byte unchanged and the file stays read-only across any
# sequence of operations (including a failed one).

_OPS = st.lists(
    st.sampled_from(["archive", "restore", "read", "list", "read_image", "bad_restore"]),
    min_size=1,
    max_size=8,
)


# Feature: screenshot-graveyard, Property 1: Source immutability under any operation
@given(img=valid_images(), ops=_OPS)
@PBT
def test_p1_source_immutable_under_any_operation_sequence(img, ops, tmp_path_factory):
    """P1 — after an arbitrary op sequence, the on-disk bytes, persisted SHA-256,
    and read-only (0o444) mode of the published original are all unchanged."""
    root = tmp_path_factory.mktemp("p1")
    eng = _new_db()
    store = FileStore(root)
    try:
        with Session(eng) as s:
            wid = _make_workspace(s, "p1" + "0" * 62)
            v = VaultService(s, wid, file_store=store)
            r = v.upload(img["data"], filename="orig." + _FMT_EXT[img["format"]])
            sid = r.screenshot_id
            blob = s.get(models.ContentBlob, r.content_blob_id)
            path = store.root / blob.storage_path
            original_bytes = path.read_bytes()
            original_sha = blob.sha256
            original_mode = os.stat(path).st_mode & 0o777

            for op in ops:
                if op == "archive":
                    v.archive(sid)
                elif op == "restore":
                    v.restore(sid)
                elif op == "read":
                    store.read(blob.storage_path, expected_sha256=original_sha)
                elif op == "list":
                    v.list_screenshots()
                elif op == "read_image":
                    v.read_original(sid)
                elif op == "bad_restore":
                    # A failed operation on a non-existent id must not mutate bytes.
                    from app.services.vault_service import ScreenshotNotFoundError
                    with pytest.raises(ScreenshotNotFoundError):
                        v.restore(987654321)

            # INV-1 / INV-3 / INV-10: nothing about the original changed.
            s.refresh(blob)
            assert path.read_bytes() == original_bytes
            assert blob.sha256 == original_sha
            assert original_bytes == img["data"]
            # Published original remains read-only.
            assert os.stat(path).st_mode & 0o777 == original_mode == 0o444
    finally:
        _chmod_cleanup(root)
        eng.dispose()


# ================================================================ P11
# P11: Exclusive create never overwrites existing bytes — publishing content to a
# content-addressed path that already exists succeeds logically WITHOUT replacing
# the existing bytes.

# Feature: screenshot-graveyard, Property 11: Exclusive create never overwrites existing bytes
@given(img=valid_images())
@PBT
def test_p11_republish_existing_path_never_overwrites(img, tmp_path_factory):
    """P11 — writing identical content twice returns the same path and leaves the
    already-published bytes byte-for-byte unchanged (no overwrite)."""
    root = tmp_path_factory.mktemp("p11")
    store = FileStore(root)
    try:
        wid = 7
        sha = img["sha256"]
        rel1 = store.write_once(wid, sha, img["data"])
        first_bytes = (store.root / rel1).read_bytes()
        first_mtime = os.stat(store.root / rel1).st_mtime_ns
        # Second publish of identical content: logical success, same path.
        rel2 = store.write_once(wid, sha, img["data"])
        assert rel2 == rel1
        again = (store.root / rel2).read_bytes()
        assert again == first_bytes == img["data"]
        # Bytes untouched (content identical; inode not replaced by a new file).
        assert os.stat(store.root / rel2).st_mtime_ns == first_mtime
    finally:
        _chmod_cleanup(root)


# Feature: screenshot-graveyard, Property 11: Exclusive create never overwrites existing bytes
@given(img=valid_images(), tamper=st.binary(min_size=1, max_size=32))
@PBT
def test_p11_corrupt_existing_original_is_never_silently_overwritten(img, tamper, tmp_path_factory):
    """P11 (safety) — if a published original is corrupted out-of-band, a second
    write of the TRUE content must NOT overwrite it; it raises IntegrityError so
    the write-once guard never silently 'fixes' a mismatched original."""
    root = tmp_path_factory.mktemp("p11b")
    store = FileStore(root)
    try:
        wid = 9
        sha = img["sha256"]
        if tamper == img["data"]:
            return  # degenerate: identical bytes aren't a corruption
        rel = store.write_once(wid, sha, img["data"])
        path = store.root / rel
        os.chmod(path, 0o644)
        path.write_bytes(tamper)  # corrupt it out of band
        with pytest.raises(FileStoreIntegrityError):
            store.write_once(wid, sha, img["data"])
        # The corrupt bytes are still there — we never overwrote with true bytes.
        assert path.read_bytes() == tamper
    finally:
        _chmod_cleanup(root)


# ================================================================ P9
# P9: Exclusive, concurrency-safe write-once publication — for any number of
# concurrent uploads of identical content, exactly one set of bytes is published,
# every upload record is created, bytes are never replaced, and no temp file
# remains.

# Feature: screenshot-graveyard, Property 9: Exclusive, concurrency-safe write-once publication
@given(img=valid_images(), workers=st.integers(min_value=2, max_value=6))
@PBT
def test_p9_concurrent_identical_writes_publish_once_no_temp_left(img, workers, tmp_path_factory):
    """P9 — N threads publishing identical content via the real FileStore all
    succeed, exactly one file exists at the content path (identical bytes), and
    the .tmp/ area holds no leftover temp file afterward."""
    root = tmp_path_factory.mktemp("p9")
    store = FileStore(root)
    try:
        wid = 3
        sha = img["sha256"]
        results: list[str] = []
        errors: list[BaseException] = []
        barrier = threading.Barrier(workers)

        def publish():
            try:
                barrier.wait()  # maximize overlap
                rel = store.write_once(wid, sha, img["data"])
                results.append(rel)
            except BaseException as exc:  # noqa: BLE001 - recorded for assert
                errors.append(exc)

        threads = [threading.Thread(target=publish) for _ in range(workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []                       # every writer succeeded
        assert len(results) == workers            # every record created
        assert len(set(results)) == 1             # same content path for all
        final = store.root / results[0]
        assert final.read_bytes() == img["data"]  # published bytes intact
        # No leftover temp files under .tmp/.
        tmp_dir = store.root / ".tmp"
        leftover = [p for p in tmp_dir.iterdir() if p.is_file()] if tmp_dir.is_dir() else []
        assert leftover == []
    finally:
        _chmod_cleanup(root)


# Feature: screenshot-graveyard, Property 9: Exclusive, concurrency-safe write-once publication
@given(img=valid_images(), workers=st.integers(min_value=2, max_value=5))
@PBT
def test_p9_concurrent_uploads_one_blob_distinct_screenshots(img, workers, tmp_path_factory):
    """P9 + P4 — concurrent VaultService uploads of identical content in one
    workspace converge on a single blob while each creates its own screenshot.

    Uses a FILE-BASED SQLite DB so each thread opens its OWN connection (a shared
    in-memory StaticPool connection is not thread-safe for concurrent C-level
    writes). WAL + a busy timeout let the workspace write lock + the
    UNIQUE(workspace_id, sha256) constraint serialize the critical section, so
    all uploads complete with one blob and distinct screenshots."""
    root = tmp_path_factory.mktemp("p9b")
    db_path = tmp_path_factory.mktemp("p9b_db") / "concurrent.db"
    store = FileStore(root)
    # One engine; each thread checks out its own pooled connection. A real
    # busy timeout prevents spurious 'database is locked' under contention.
    eng = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )

    # WAL + a busy timeout let concurrent connections serialize cleanly instead
    # of failing fast with "database is locked" under the workspace write lock.
    @event.listens_for(eng, "connect")
    def _set_sqlite_pragma(dbapi_conn, _rec):  # pragma: no cover - setup hook
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.close()

    try:
        Base.metadata.create_all(eng)
        with Session(eng) as s:
            wid = _make_workspace(s, "p9b" + "0" * 61)
        errors: list[BaseException] = []
        sids: list[int] = []
        barrier = threading.Barrier(workers)

        def do_upload():
            # Each thread gets its OWN session/connection (file-based SQLite is
            # safe across connections; VaultService serializes via the workspace
            # write lock and the UNIQUE(workspace_id, sha256) backstop).
            local = Session(eng)
            try:
                barrier.wait()
                v = VaultService(local, wid, file_store=store)
                r = v.upload(img["data"], filename="c.img")
                sids.append(r.screenshot_id)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                local.close()

        threads = [threading.Thread(target=do_upload) for _ in range(workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        with Session(eng) as s:
            blob_count = s.scalar(
                select(func.count()).select_from(models.ContentBlob).where(
                    models.ContentBlob.workspace_id == wid
                )
            )
            shot_count = s.scalar(
                select(func.count()).select_from(models.Screenshot).where(
                    models.Screenshot.workspace_id == wid
                )
            )
        assert blob_count == 1
        assert shot_count == workers
        assert len(set(sids)) == workers
        blob = s.scalars(
            select(models.ContentBlob).where(
                models.ContentBlob.workspace_id == wid
            )
        ).one()
        shots = list(
            s.scalars(
                select(models.Screenshot).where(
                    models.Screenshot.workspace_id == wid
                )
            )
        )
        assert {shot.content_blob_id for shot in shots} == {blob.id}
        assert blob.sha256 == img["sha256"]
        assert store.read(blob.storage_path, expected_sha256=blob.sha256) == img["data"]
        tmp_dir = store.root / ".tmp"
        leftover = [p for p in tmp_dir.iterdir() if p.is_file()] if tmp_dir.is_dir() else []
        assert leftover == []
    finally:
        _chmod_cleanup(root)
        eng.dispose()


# ================================================================ P10
# P10: Interrupted write leaves no published artifact, and cleanup is crash-safe.
# An upload interrupted AFTER the temp write but BEFORE exclusive publication
# publishes nothing; in-process interruption removes the temp via try/finally;
# the optional sweep removes only stale temp files and NEVER touches a published
# original.

# Feature: screenshot-graveyard, Property 10: Interrupted write leaves no published artifact, and cleanup is crash-safe
@given(img=valid_images())
@PBT
def test_p10_interrupted_write_publishes_nothing_and_cleans_temp(img, tmp_path_factory, monkeypatch):
    """P10 — an in-process interruption before publication leaves no file at the
    content path and no temp file under .tmp/ (try/finally cleanup)."""
    root = tmp_path_factory.mktemp("p10")
    store = FileStore(root)
    try:
        wid = 5
        sha = img["sha256"]
        rel = store._relative_storage_path(wid, sha)  # noqa: SLF001
        def interrupt_before_publication(*_args, **_kwargs):
            raise RuntimeError("simulated in-process interruption before publish")

        # Exercise FileStore.write_once itself: the temp bytes have been fsync'd,
        # but publication has not happened because the exclusive link fails.
        monkeypatch.setattr("app.storage.file_store.os.link", interrupt_before_publication)
        with pytest.raises(RuntimeError):
            store.write_once(wid, sha, img["data"])
        # Nothing published at the final content-addressed path.
        assert not (store.root / rel).exists()
        # In-process try/finally removed the temp file.
        tmp_dir = store.root / ".tmp"
        leftover = [p for p in tmp_dir.iterdir() if p.is_file()] if tmp_dir.is_dir() else []
        assert leftover == []
    finally:
        _chmod_cleanup(root)


# Feature: screenshot-graveyard, Property 10: Interrupted write leaves no published artifact, and cleanup is crash-safe
@given(
    img=valid_images(),
    orphans=st.integers(min_value=0, max_value=4),
)
@PBT
def test_p10_sweep_removes_only_stale_temp_never_published(img, orphans, tmp_path_factory):
    """P10 — the crash-safe sweep removes stale orphan temp files but leaves every
    published content-addressed original byte-for-byte unchanged (INV-1/INV-10)."""
    root = tmp_path_factory.mktemp("p10b")
    store = FileStore(root)
    try:
        wid = 11
        sha = img["sha256"]
        rel = store.write_once(wid, sha, img["data"])
        published = store.root / rel
        before = published.read_bytes()

        # Simulate hard-crash orphans: stale temp files under .tmp/.
        tmp_dir = store.root / ".tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        import time as _time
        made = []
        for i in range(orphans):
            p = tmp_dir / f"{sha}.{i}.orphan.tmp"
            p.write_bytes(b"orphan" + bytes([i % 256]))
            # Backdate so the sweep considers them stale.
            old = _time.time() - 7200
            os.utime(p, (old, old))
            made.append(p)

        removed = store.sweep_stale_temp_files(older_than_seconds=3600)
        assert removed == orphans                    # only the stale temps
        for p in made:
            assert not p.exists()
        # Published original is completely untouched.
        assert published.exists()
        assert published.read_bytes() == before == img["data"]
        assert os.stat(published).st_mode & 0o777 == 0o444
    finally:
        _chmod_cleanup(root)
