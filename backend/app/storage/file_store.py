"""Write-once, content-addressed file store (Task 1.6).

Stores original screenshot bytes privately and immutably beneath STORAGE_ROOT,
addressed by their SHA-256. Three operations only — no delete, no overwrite:

    write_once(workspace_id, sha256, data) -> storage_path (relative)
    read(storage_path) -> bytes
    exists(storage_path) -> bool

Layout (private volume, never web-served):
    <STORAGE_ROOT>/<workspace_id>/<ab>/<cd>/<sha256>
where ab = sha256[0:2], cd = sha256[2:4]. Physical deduplication is by content
hash per workspace; Workspace *metadata* lives in the relational DB (separation
of concerns, design Req 12.2). The returned storage_path is RELATIVE to
STORAGE_ROOT so DB rows stay portable across storage roots/mounts.

Immutability (INV-1/INV-3/INV-10):
  * Publication is atomic via an EXCLUSIVE create (os.link / O_CREAT|O_EXCL) that
    fails if the destination already exists; an existing destination is treated
    as success WITHOUT replacing bytes.
  * There is NO delete or overwrite operation.
  * Published files are set read-only (0o444).

Crash tolerance:
  * Bytes are written to a unique temp file under a reserved `.tmp/` subdirectory
    on the same volume, flushed + fsync'd, then published by exclusive create.
  * try/finally removes the temp file on in-process errors and on the
    already-exists path. A hard crash/SIGKILL may leave an orphan temp file under
    `.tmp/` (harmless; removable by the optional sweep) but can NEVER corrupt or
    delete a published original.

NOTE: filesystem persistence here is local. Durability on a real mounted cloud
volume across instance restarts is a SEPARATE, still-outstanding proof (Task 1.0,
targeted Nov 12); this module does not establish that.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import time
from pathlib import Path

from app import config

_SHA256_HEX_LEN = 64
_TMP_DIRNAME = ".tmp"
# Published files: read-only for owner/group/other (private + immutable).
_PUBLISHED_MODE = 0o444
# Directories: owner-only access (private screenshots).
_DIR_MODE = 0o700


class FileStoreError(Exception):
    """Base error for file-store operations."""


class IntegrityError(FileStoreError):
    """Raised when stored bytes fail a size or SHA-256 integrity check."""


class UnsafePathError(FileStoreError):
    """Raised when a storage_path would escape the private storage root."""


def compute_sha256(data: bytes) -> str:
    """Return the hex SHA-256 of the exact bytes."""
    return hashlib.sha256(data).hexdigest()


def _is_valid_sha256(sha256: str) -> bool:
    return (
        isinstance(sha256, str)
        and len(sha256) == _SHA256_HEX_LEN
        and all(c in "0123456789abcdef" for c in sha256)
    )


class FileStore:
    def __init__(self, root: str | os.PathLike[str] | None = None) -> None:
        self._root = Path(root if root is not None else config.STORAGE_ROOT).resolve()
        self._tmp_dir = self._root / _TMP_DIRNAME

    @property
    def root(self) -> Path:
        return self._root

    # ------------------------------------------------------------------ paths

    def _ensure_dirs(self, *dirs: Path) -> None:
        for d in dirs:
            d.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(d, _DIR_MODE)
            except OSError:
                pass  # best-effort on platforms/filesystems that disallow chmod

    def _relative_storage_path(self, workspace_id: int, sha256: str) -> str:
        # POSIX-style relative path stored in the DB; join uses os.sep on read.
        return f"{int(workspace_id)}/{sha256[0:2]}/{sha256[2:4]}/{sha256}"

    def _resolve_within_root(self, storage_path: str) -> Path:
        """Resolve a RELATIVE storage_path under the root, rejecting any escape.

        Defends against path traversal (`..`), absolute-path injection, and
        symlink escapes: the fully-resolved real path must stay within the
        fully-resolved real storage root.
        """
        if storage_path is None or storage_path == "":
            raise UnsafePathError("empty storage path")
        p = Path(storage_path)
        if p.is_absolute() or p.drive or p.anchor:
            raise UnsafePathError("absolute storage path is not allowed")
        if any(part == ".." for part in p.parts):
            raise UnsafePathError("parent-directory segments are not allowed")

        candidate = (self._root / p)
        # Resolve symlinks/.. fully, then confirm containment within the real root.
        real_root = os.path.realpath(self._root)
        real_candidate = os.path.realpath(candidate)
        # Containment check using commonpath on realpaths.
        try:
            common = os.path.commonpath([real_root, real_candidate])
        except ValueError:
            raise UnsafePathError("storage path is not within the storage root")
        if common != real_root:
            raise UnsafePathError("storage path escapes the storage root")
        return Path(real_candidate)

    def _assert_within_root(self, path: Path) -> None:
        """Raise UnsafePathError unless `path`'s real location is within root.

        Used on the WRITE path to ensure an attacker-controlled symlink in a
        path component cannot redirect publication outside STORAGE_ROOT.
        """
        real_root = os.path.realpath(self._root)
        real_path = os.path.realpath(path)
        try:
            common = os.path.commonpath([real_root, real_path])
        except ValueError:
            raise UnsafePathError("path is not within the storage root")
        if common != real_root:
            raise UnsafePathError("path escapes the storage root")

    def _verify_published(self, final: Path, sha256: str, expected_size: int) -> None:
        """Verify an already-published file matches the expected content.

        A duplicate write is only "successful" if the existing bytes are intact:
        their size and SHA-256 must match. A corrupted/mismatched existing
        original raises IntegrityError and is NEVER overwritten or silently
        accepted.
        """
        existing = final.read_bytes()
        if len(existing) != expected_size:
            raise IntegrityError(
                "existing published file size does not match expected size"
            )
        if compute_sha256(existing) != sha256:
            raise IntegrityError(
                "existing published file does not match its content address"
            )

    # --------------------------------------------------------------- core ops

    def write_once(self, workspace_id: int, sha256: str, data: bytes) -> str:
        """Atomically, idempotently publish `data` under its content hash.

        Verifies the provided sha256 matches the data, writes to a temp file,
        fsyncs, and publishes by exclusive create. Returns the RELATIVE
        storage_path. If the destination already exists, returns it WITHOUT
        replacing the existing bytes.
        """
        if not _is_valid_sha256(sha256):
            raise FileStoreError("sha256 must be 64 lowercase hex chars")
        actual = compute_sha256(data)
        if actual != sha256:
            raise IntegrityError("provided sha256 does not match data")

        rel = self._relative_storage_path(workspace_id, sha256)
        final = self._root / rel
        expected_size = len(data)
        self._ensure_dirs(final.parent, self._tmp_dir)

        # Write-path containment: ensure neither the destination directory nor
        # the temp dir has been redirected OUTSIDE the storage root via a
        # symlinked path component (defends the write/link against symlink
        # escape, complementing the read-path check).
        self._assert_within_root(final.parent)
        self._assert_within_root(self._tmp_dir)

        # If already published, it is only a successful duplicate when the
        # existing bytes are intact. A corrupted/mismatched original raises
        # IntegrityError and is never overwritten or silently accepted.
        if final.exists():
            self._verify_published(final, sha256, expected_size)
            return rel

        # Unique temp name under the reserved .tmp/ dir (same volume as final).
        tmp = self._tmp_dir / f"{sha256}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
        try:
            fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            # Publish by EXCLUSIVE create: os.link fails if `final` exists.
            try:
                os.link(tmp, final)
            except FileExistsError:
                # A concurrent writer won the race. Keep ITS bytes, but only
                # after verifying they are intact (never accept a corrupt
                # existing original silently).
                self._verify_published(final, sha256, expected_size)
                return rel
            # Make the published original read-only (immutable, private).
            try:
                os.chmod(final, _PUBLISHED_MODE)
            except OSError:
                pass
            return rel
        finally:
            # Best-effort temp cleanup for in-process paths. A hard crash may
            # leave an orphan temp under .tmp/ — harmless, never a published file.
            # This only ever removes OUR uniquely-named temp file, never a
            # published original.
            try:
                if tmp.exists():
                    os.unlink(tmp)
            except OSError:
                pass

    def exists(self, storage_path: str) -> bool:
        try:
            resolved = self._resolve_within_root(storage_path)
        except UnsafePathError:
            return False
        return resolved.is_file()

    def read(self, storage_path: str, *, expected_sha256: str | None = None,
             expected_size: int | None = None) -> bytes:
        """Read bytes back, verifying integrity.

        Always re-verifies the SHA-256 against the content-addressed filename
        (the last path segment) so silent corruption is detected. Optional
        explicit expected_sha256 / expected_size add defense in depth.
        """
        resolved = self._resolve_within_root(storage_path)
        if not resolved.is_file():
            raise FileStoreError("storage path does not exist")
        data = resolved.read_bytes()

        # Size check (if provided).
        if expected_size is not None and len(data) != expected_size:
            raise IntegrityError("stored byte size does not match expected size")

        # Content-address self-check: filename is the sha256 of the bytes.
        name_hash = resolved.name
        actual = compute_sha256(data)
        if _is_valid_sha256(name_hash) and actual != name_hash:
            raise IntegrityError("stored bytes do not match content address")
        if expected_sha256 is not None and actual != expected_sha256:
            raise IntegrityError("stored bytes do not match expected sha256")
        return data

    # ------------------------------------------------------------- housekeeping

    def sweep_stale_temp_files(self, older_than_seconds: float = 3600) -> int:
        """Remove ONLY stale temp files under `.tmp/`. Never touches published
        originals. Returns the count removed. Safe and idempotent."""
        if not self._tmp_dir.is_dir():
            return 0
        now = time.time()
        removed = 0
        for entry in self._tmp_dir.iterdir():
            if not entry.is_file():
                continue
            try:
                if now - entry.stat().st_mtime >= older_than_seconds:
                    os.unlink(entry)
                    removed += 1
            except OSError:
                pass
        return removed
