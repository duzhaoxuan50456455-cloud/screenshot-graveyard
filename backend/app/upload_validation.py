"""Content-authoritative upload validation (Task 1.7).

Validates that an uploaded file is a genuine, supported, safely-decodable image
BEFORE its bytes may enter the write-once FileStore. The decision is driven by
the ACTUAL bytes, never by the client-declared filename extension or MIME type
(those are hints only and are never the sole reason to reject).

Ordered policy (design CORRECTION 3):
  1. Size gate (authoritative, on actual bytes): read in bounded chunks and abort
     as soon as the accumulated size exceeds MAX_UPLOAD_BYTES (15 MB). Arbitrary
     payloads are NEVER fully buffered. Content-Length, if present, is only an
     early-reject optimization — never the security boundary.
  2. File signature (magic bytes) = authoritative format gate: PNG/JPEG/WebP.
  3. Pillow verify() then a fresh load() = confirm a real, decodable image.
  4. Dimension / megapixel cap = decompression-bomb guard (MAX_IMAGE_DIMENSION_PX,
     MAX_IMAGE_MEGAPIXELS), with Pillow's MAX_IMAGE_PIXELS as a hard backstop.

On success returns the EXACT accepted original bytes (unmodified, no
recompression), the detected true format, pixel dimensions, and the SHA-256 of
those exact bytes. On failure raises UploadValidationError with a SAFE, generic
message (optionally naming the client filename, never a filesystem path or other
private data). Rejected uploads produce no stored bytes and no persistent temp
files (this module does not write to the FileStore; it only validates in memory
within the bounded cap).
"""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass

from app import config

# Map Pillow format names to canonical stored MIME types.
_FORMAT_TO_MIME = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "WEBP": "image/webp",
}

# Magic-byte signatures for the supported formats.
def _sniff_format(data: bytes) -> str | None:
    if len(data) >= 8 and data[:8] == b"\x89PNG\r\n\x1a\n":
        return "PNG"
    if len(data) >= 3 and data[:3] == b"\xff\xd8\xff":
        return "JPEG"
    # WebP: 'RIFF' .... 'WEBP'
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "WEBP"
    return None


class UploadValidationError(Exception):
    """Raised when an upload fails validation.

    Carries a generic, user-safe message. May name the client-supplied filename
    (not a filesystem path) for a helpful error, per Req 1.4/1.5.
    """

    def __init__(self, message: str, *, filename: str | None = None) -> None:
        self.filename = filename
        if filename:
            super().__init__(f"{filename}: {message}")
        else:
            super().__init__(message)


class PayloadTooLargeError(UploadValidationError):
    """The actual byte size exceeded the 15 MB cap."""


@dataclass(frozen=True)
class ValidatedImage:
    data: bytes          # exact accepted original bytes (unmodified)
    sha256: str          # SHA-256 of `data`
    detected_format: str # Pillow format name, e.g. "PNG"
    mime_type: str       # canonical MIME for the detected format
    width: int
    height: int
    byte_size: int


def _read_capped(stream: io.RawIOBase | io.BufferedIOBase, cap: int,
                 filename: str | None, chunk_size: int = 64 * 1024) -> bytes:
    """Read a stream into memory in bounded chunks, aborting once `cap` is
    exceeded. Reads at most cap+1 bytes total, so an oversized payload is never
    fully buffered. Raises PayloadTooLargeError if the stream exceeds `cap`."""
    buf = bytearray()
    limit = cap + 1  # one byte past the cap lets us detect "over by one byte"
    while len(buf) < limit:
        want = min(chunk_size, limit - len(buf))
        chunk = stream.read(want)
        if not chunk:
            break
        buf.extend(chunk)
        if len(buf) > cap:
            raise PayloadTooLargeError(
                f"file exceeds the maximum size of {cap} bytes", filename=filename
            )
    return bytes(buf)


def validate_image_bytes(data: bytes, *, filename: str | None = None,
                         declared_mime: str | None = None) -> ValidatedImage:
    """Validate already-in-memory bytes (must already be within the size cap)."""
    return _validate(data, filename=filename, declared_mime=declared_mime)


def validate_image_stream(stream, *, filename: str | None = None,
                          declared_mime: str | None = None,
                          content_length: int | None = None) -> ValidatedImage:
    """Validate an image from a byte stream with a bounded, streaming size gate.

    `content_length`, if provided, is used ONLY as an early-reject optimization;
    the authoritative limit is enforced on the actual bytes read from `stream`.
    """
    cap = config.MAX_UPLOAD_BYTES
    # Early-reject optimization (not authoritative): obviously-too-large declared
    # size. A missing/invalid/lying Content-Length is simply ignored here.
    if content_length is not None and content_length > cap:
        raise PayloadTooLargeError(
            f"file exceeds the maximum size of {cap} bytes", filename=filename
        )
    data = _read_capped(stream, cap, filename)
    return _validate(data, filename=filename, declared_mime=declared_mime)


def _validate(data: bytes, *, filename: str | None,
              declared_mime: str | None) -> ValidatedImage:
    cap = config.MAX_UPLOAD_BYTES
    # Authoritative size check on actual bytes (defense if called directly).
    if len(data) > cap:
        raise PayloadTooLargeError(
            f"file exceeds the maximum size of {cap} bytes", filename=filename
        )
    if len(data) == 0:
        raise UploadValidationError("file is empty", filename=filename)

    # Signature gate (authoritative format detection from content).
    sniffed = _sniff_format(data)
    if sniffed is None or sniffed not in config.SUPPORTED_IMAGE_FORMATS:
        raise UploadValidationError(
            "unsupported or unrecognized image format", filename=filename
        )

    # Decode validation with Pillow. Import locally so the module imports even if
    # Pillow is temporarily unavailable in some tooling context.
    import warnings

    from PIL import Image, UnidentifiedImageError

    max_pixels = int(config.MAX_IMAGE_MEGAPIXELS * 1_000_000)

    # NOTE: we deliberately do NOT mutate the process-global
    # `Image.MAX_IMAGE_PIXELS` per request — that is shared mutable state and
    # would race across concurrent validations. Bomb safety is enforced by our
    # OWN explicit dimension/pixel checks on the header (read before any raster
    # decode), and Pillow's library-level DecompressionBombError/Warning are
    # caught defensively as a backstop (without changing the global limit).

    def _reject_not_image() -> None:
        raise UploadValidationError(
            "file is not a valid, decodable image", filename=filename
        )

    def _reject_too_many_pixels() -> None:
        raise UploadValidationError(
            "image pixel count exceeds the allowed maximum", filename=filename
        )

    # Treat Pillow's DecompressionBombWarning as an error WITHIN this call only,
    # using a local warnings context (restored on exit). This does not depend on
    # the global MAX_IMAGE_PIXELS and is scoped to this stack.
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)

        # Pass 1: header + structural verify(). Read dimensions from the header
        # (no raster decode) and apply our explicit caps FIRST, so an oversized
        # or bomb image is rejected before any full decode.
        try:
            with Image.open(io.BytesIO(data)) as probe:
                detected = (probe.format or "").upper()
                width, height = probe.width, probe.height
                if (
                    width > config.MAX_IMAGE_DIMENSION_PX
                    or height > config.MAX_IMAGE_DIMENSION_PX
                ):
                    raise UploadValidationError(
                        "image dimensions exceed the allowed maximum",
                        filename=filename,
                    )
                if width * height > max_pixels:
                    _reject_too_many_pixels()
                probe.verify()
        except UploadValidationError:
            raise
        except Image.DecompressionBombError:
            _reject_too_many_pixels()
        except Image.DecompressionBombWarning:
            _reject_too_many_pixels()
        except (UnidentifiedImageError, OSError, SyntaxError, ValueError):
            _reject_not_image()

        # Pass 2: fresh open + load() (verify() leaves the image unusable). The
        # caps were already enforced from the header above, bounding the work.
        try:
            with Image.open(io.BytesIO(data)) as img:
                img.load()
        except Image.DecompressionBombError:
            _reject_too_many_pixels()
        except Image.DecompressionBombWarning:
            _reject_too_many_pixels()
        except (UnidentifiedImageError, OSError, SyntaxError, ValueError):
            _reject_not_image()

    # Decoded format must match a supported format AND agree with the signature.
    if detected not in config.SUPPORTED_IMAGE_FORMATS or detected != sniffed:
        raise UploadValidationError(
            "unsupported or unrecognized image format", filename=filename
        )

    # Compute SHA-256 from the EXACT accepted original bytes (no modification).
    sha = hashlib.sha256(data).hexdigest()
    return ValidatedImage(
        data=data,
        sha256=sha,
        detected_format=detected,
        mime_type=_FORMAT_TO_MIME[detected],
        width=width,
        height=height,
        byte_size=len(data),
    )
