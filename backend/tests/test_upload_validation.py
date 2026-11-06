"""Content-authoritative upload validation tests (Task 1.7).

Uses synthetic images generated in-memory (never real screenshots) and bounded
synthetic streams for oversize cases (no large allocations). Confirms the accept
decision tracks actual content (signature + decode + caps + size) and is
independent of declared extension/MIME, that the 15 MB actual-byte cap cannot be
bypassed by omitting Content-Length, and that rejected inputs never reach the
FileStore.
"""

from __future__ import annotations

import hashlib
import io

import pytest
from PIL import Image

from app import config
from app.upload_validation import (
    PayloadTooLargeError,
    UploadValidationError,
    ValidatedImage,
    validate_image_bytes,
    validate_image_stream,
)


def make_image(fmt: str, size=(16, 12), color=(120, 60, 160)) -> bytes:
    bio = io.BytesIO()
    Image.new("RGB", size, color).save(bio, format=fmt)
    return bio.getvalue()


# ---- valid images, all approved formats ----

@pytest.mark.parametrize("fmt,mime", [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp")])
def test_valid_formats_accepted(fmt, mime):
    data = make_image(fmt)
    v = validate_image_bytes(data)
    assert isinstance(v, ValidatedImage)
    assert v.detected_format == fmt
    assert v.mime_type == mime
    assert v.width == 16 and v.height == 12


def test_sha256_correct_and_bytes_preserved():
    data = make_image("PNG")
    v = validate_image_bytes(data)
    assert v.data == data  # exact original bytes, no recompression
    assert v.sha256 == hashlib.sha256(data).hexdigest()
    assert v.byte_size == len(data)


# ---- content-authoritative: extension/MIME are hints only ----

def test_extension_mismatch_accepted_detected_format_used():
    png = make_image("PNG")
    v = validate_image_bytes(png, filename="actually_a_png.jpg", declared_mime="image/webp")
    assert v.detected_format == "PNG"
    assert v.mime_type == "image/png"  # detected, not declared


def test_misleading_mime_does_not_cause_reject_or_wrong_format():
    jpeg = make_image("JPEG")
    v = validate_image_bytes(jpeg, filename="x.png", declared_mime="image/png")
    assert v.detected_format == "JPEG"
    assert v.mime_type == "image/jpeg"


# ---- unsupported / malformed ----

def test_unsupported_format_rejected():
    gif = make_image("GIF")
    with pytest.raises(UploadValidationError):
        validate_image_bytes(gif)


def test_truncated_image_rejected():
    png = make_image("PNG")
    with pytest.raises(UploadValidationError):
        validate_image_bytes(png[:24])


def test_fake_signature_non_image_rejected():
    # Correct PNG magic bytes but garbage payload -> decode fails.
    fake = b"\x89PNG\r\n\x1a\n" + b"not a real png body" * 4
    with pytest.raises(UploadValidationError):
        validate_image_bytes(fake)


def test_empty_file_rejected():
    with pytest.raises(UploadValidationError):
        validate_image_bytes(b"")


def test_plain_text_rejected():
    with pytest.raises(UploadValidationError):
        validate_image_bytes(b"just some text, definitely not an image")


# ---- dimension / decompression-bomb caps ----

def test_excessive_dimension_rejected():
    oversized = io.BytesIO()
    Image.new("RGB", (config.MAX_IMAGE_DIMENSION_PX + 1, 2), (0, 0, 0)).save(oversized, format="PNG")
    with pytest.raises(UploadValidationError):
        validate_image_bytes(oversized.getvalue())


def test_megapixel_cap_rejected(monkeypatch):
    # Lower the MP cap so a modest synthetic image trips it without huge memory.
    monkeypatch.setattr(config, "MAX_IMAGE_MEGAPIXELS", 0.0001)  # 100 px cap
    data = make_image("PNG", size=(64, 64))  # 4096 px > 100 px
    with pytest.raises(UploadValidationError):
        validate_image_bytes(data)


# ---- size cap: authoritative on actual bytes ----

def test_payload_one_byte_over_cap_rejected(monkeypatch):
    # Use a tiny cap so we don't allocate 15 MB in tests.
    monkeypatch.setattr(config, "MAX_UPLOAD_BYTES", 64)
    over = b"x" * 65
    with pytest.raises(PayloadTooLargeError):
        validate_image_stream(io.BytesIO(over), filename="big.bin")


def test_payload_exactly_at_cap_reads_but_then_format_checked(monkeypatch):
    # At exactly the cap, the size gate passes; content checks then apply.
    monkeypatch.setattr(config, "MAX_UPLOAD_BYTES", 64)
    exactly = b"x" * 64  # not an image -> rejected by signature, NOT by size
    with pytest.raises(UploadValidationError) as exc:
        validate_image_stream(io.BytesIO(exactly))
    assert not isinstance(exc.value, PayloadTooLargeError)


def test_missing_content_length_cannot_bypass_cap(monkeypatch):
    monkeypatch.setattr(config, "MAX_UPLOAD_BYTES", 1024)

    class Endless(io.RawIOBase):
        def readable(self):
            return True
        def readinto(self, b):
            b[:] = b"\x00" * len(b)
            return len(b)

    # No content_length provided at all -> still rejected by actual-byte cap,
    # and the stream is never fully buffered (reads at most cap+1 bytes).
    with pytest.raises(PayloadTooLargeError):
        validate_image_stream(Endless(), filename="endless.bin", content_length=None)


def test_lying_small_content_length_cannot_bypass_cap(monkeypatch):
    monkeypatch.setattr(config, "MAX_UPLOAD_BYTES", 1024)

    class Endless(io.RawIOBase):
        def readable(self):
            return True
        def readinto(self, b):
            b[:] = b"\x00" * len(b)
            return len(b)

    # Attacker lies that it's tiny; the actual-byte gate still catches it.
    with pytest.raises(PayloadTooLargeError):
        validate_image_stream(Endless(), content_length=10)


def test_oversized_content_length_early_rejected(monkeypatch):
    monkeypatch.setattr(config, "MAX_UPLOAD_BYTES", 1024)
    # Early-reject optimization: honest oversized Content-Length is rejected
    # before reading the body.
    with pytest.raises(PayloadTooLargeError):
        validate_image_stream(io.BytesIO(b"small"), content_length=10_000)


def test_bounded_read_does_not_overbuffer(monkeypatch):
    """The capped reader never reads more than cap+1 bytes total."""
    monkeypatch.setattr(config, "MAX_UPLOAD_BYTES", 100)
    read_total = {"n": 0}

    class Counting(io.RawIOBase):
        def readable(self):
            return True
        def readinto(self, b):
            n = len(b)
            read_total["n"] += n
            b[:] = b"\x00" * n
            return n

    with pytest.raises(PayloadTooLargeError):
        validate_image_stream(Counting())
    # Never buffers much more than the cap (cap + one chunk at most).
    assert read_total["n"] <= 100 + 64 * 1024


# ---- streamed valid image round-trips ----

def test_valid_image_via_stream():
    png = make_image("PNG")
    v = validate_image_stream(io.BytesIO(png), filename="a.png", content_length=len(png))
    assert v.detected_format == "PNG"
    assert v.data == png


# ---- rejected inputs never reach the FileStore ----

def test_rejected_input_never_written_to_filestore(tmp_path):
    from app.storage.file_store import FileStore
    fs = FileStore(tmp_path / "storage")
    bad = b"not an image at all"
    with pytest.raises(UploadValidationError):
        validate_image_bytes(bad)
    # The validator never touches the store; confirm nothing was published.
    root = tmp_path / "storage"
    published = [p for p in root.rglob("*") if p.is_file()] if root.exists() else []
    assert published == []


def test_error_messages_do_not_leak_paths():
    try:
        validate_image_bytes(b"xx", filename="user-provided-name.png")
    except UploadValidationError as e:
        msg = str(e)
        assert "/" not in msg.replace("user-provided-name.png", "")  # no fs paths
        assert "user-provided-name.png" in msg  # filename ok (not a path)


# ---- Review corrections: Pillow decompression thread-safety ----

def test_global_max_image_pixels_not_mutated():
    """Validation must not mutate Pillow's process-global MAX_IMAGE_PIXELS."""
    from PIL import Image
    before = Image.MAX_IMAGE_PIXELS
    validate_image_bytes(make_image("PNG"))
    # Also after a rejection path.
    with pytest.raises(UploadValidationError):
        validate_image_bytes(b"not an image")
    assert Image.MAX_IMAGE_PIXELS == before


def test_concurrent_validation_mixed_valid_and_bomb(monkeypatch):
    """Concurrent validations (valid + oversized) each get the correct result;
    no cross-talk from shared global state."""
    import threading
    monkeypatch.setattr(config, "MAX_IMAGE_DIMENSION_PX", 100)
    good = make_image("PNG", size=(10, 10))
    bad = io.BytesIO()
    Image.new("RGB", (200, 10), (0, 0, 0)).save(bad, format="PNG")
    bad_bytes = bad.getvalue()

    results: dict[str, list] = {"good": [], "bad": []}
    lock = threading.Lock()

    def run_good():
        try:
            v = validate_image_bytes(good)
            with lock:
                results["good"].append(v.detected_format == "PNG")
        except Exception:  # noqa: BLE001
            with lock:
                results["good"].append(False)

    def run_bad():
        try:
            validate_image_bytes(bad_bytes)
            with lock:
                results["bad"].append(False)  # should have raised
        except UploadValidationError:
            with lock:
                results["bad"].append(True)

    threads = []
    for _ in range(8):
        threads.append(threading.Thread(target=run_good))
        threads.append(threading.Thread(target=run_bad))
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(results["good"]) and len(results["good"]) == 8
    assert all(results["bad"]) and len(results["bad"]) == 8


def test_bomb_warning_range_image_rejected(monkeypatch):
    """An image whose pixels exceed our cap (but within Pillow's 2x warning band)
    is rejected via our explicit header check / the warning-as-error backstop."""
    monkeypatch.setattr(config, "MAX_IMAGE_MEGAPIXELS", 0.0001)  # 100 px cap
    monkeypatch.setattr(config, "MAX_IMAGE_DIMENSION_PX", 100000)  # don't trip dim cap
    data = make_image("PNG", size=(40, 40))  # 1600 px > 100 px cap
    with pytest.raises(UploadValidationError) as exc:
        validate_image_bytes(data)
    assert "pixel count" in str(exc.value)


def test_oversized_rejected_before_full_decode(monkeypatch):
    """Dimension cap is applied from the header before load(); assert load() is
    not reached for an oversized image."""
    monkeypatch.setattr(config, "MAX_IMAGE_DIMENSION_PX", 50)
    oversized = io.BytesIO()
    Image.new("RGB", (4000, 10), (0, 0, 0)).save(oversized, format="PNG")
    called = {"load": False}
    real_load = Image.Image.load

    def spy_load(self, *a, **k):
        called["load"] = True
        return real_load(self, *a, **k)

    monkeypatch.setattr(Image.Image, "load", spy_load)
    with pytest.raises(UploadValidationError):
        validate_image_bytes(oversized.getvalue())
    assert called["load"] is False  # rejected before any raster decode
