"""Vault_Service — screenshot ingestion orchestration (Task 1.8).

Orchestrates the existing primitives into the first real ingestion flow:
validate (Task 1.7) -> publish write-once bytes (Task 1.6) -> persist metadata
in a workspace-scoped transaction (Task 1.2). This service is the ONLY importer
of the private FileStore (design rule); routers/other services never touch it.

Key safety properties (criteria 11-15):
  * Server-resolved workspace only: the caller passes a trusted workspace_id
    (resolved by the identity middleware). No client-supplied id is honored.
  * Validation first: invalid content raises before anything is published or
    persisted — no file, no DB row.
  * Publish-then-commit ordering: bytes are published write-once (idempotent by
    content hash) BEFORE the DB commit. Success is reported ONLY after the DB
    transaction commits. A crash/DB failure after publish leaves at most a
    harmless ORPHAN blob file (reused idempotently by a later identical upload)
    — never a lost/overwritten original and never a false success. There is no
    delete (write-once FileStore), so a failed persist preserves the original.
  * Per-workspace dedup: content_blob is unique by (workspace_id, sha256); a
    get-or-create with retry-on-conflict converges concurrent identical uploads
    on a single blob. Screenshots are NEVER deduplicated — each upload yields a
    distinct, stable Screenshot ID; a repeat of identical content is flagged.
  * Atomic quota: the workspace row is locked (SELECT ... FOR UPDATE on Postgres)
    for the count+insert, so the quota check is not an advisory TOCTOU race.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import config
from app.db import models
from app.db.repository import QuotaExceededError, WorkspaceRepository
from app.storage.file_store import (  # ONLY importer of the file store
    FileStore,
    IntegrityError as FileStoreIntegrityError,
)
from app.upload_validation import UploadValidationError, validate_image_bytes


@dataclass(frozen=True)
class UploadResult:
    screenshot_id: int
    content_blob_id: int
    sha256: str
    mime_type: str
    is_duplicate: bool
    width: int
    height: int
    byte_size: int


@dataclass(frozen=True)
class LifecycleResult:
    """Outcome of an archive/restore operation.

    `status` is "successful" or "failed" (restore integrity failure). `state`
    is the screenshot's resulting logical state ("active" | "archived").
    """

    screenshot_id: int
    status: str
    state: str


@dataclass(frozen=True)
class OriginalImage:
    """The exact original bytes of a screenshot plus its detected MIME type."""

    screenshot_id: int
    data: bytes
    mime_type: str
    sha256: str
    byte_size: int


@dataclass(frozen=True)
class ScreenshotInfo:
    """Safe, user-facing metadata for one screenshot (no storage path/token)."""

    screenshot_id: int
    state: str
    mime_type: str
    original_filename: str
    is_duplicate: bool
    duplicate_of: int | None
    byte_size: int
    uploaded_at: str


class ScreenshotNotFoundError(Exception):
    """Raised when a screenshot is absent OR owned by another workspace.

    The SAME error is used for both cases so a foreign-workspace screenshot is
    never distinguished from a missing one (existence non-disclosure). Carries a
    generic message only.
    """


class VaultServiceError(Exception):
    """Base error for vault operations (generic, no private data in messages)."""


class VaultService:
    def __init__(
        self,
        session: Session,
        workspace_id: int,
        *,
        file_store: FileStore | None = None,
        screenshot_quota: int | None = None,
        max_blob_insert_retries: int = 3,
    ) -> None:
        if workspace_id is None:
            raise ValueError("workspace_id is required")
        self._session = session
        self._workspace_id = workspace_id
        self._repo = WorkspaceRepository(session, workspace_id)
        self._store = file_store or FileStore()
        self._quota = (
            screenshot_quota
            if screenshot_quota is not None
            else config.MAX_SCREENSHOTS_PER_WORKSPACE
        )
        self._max_retries = max_blob_insert_retries

    # --------------------------------------------------------------- ingest

    def upload(
        self,
        data: bytes,
        *,
        filename: str | None = None,
        declared_mime: str | None = None,
    ) -> UploadResult:
        """Validate, publish, and persist a single screenshot. Atomic per upload.

        Raises UploadValidationError (invalid content), QuotaExceededError (quota
        reached), or VaultServiceError (storage/persistence failure). On any
        failure no screenshot row is created; on validation failure nothing is
        published either.
        """
        # 1) Validate content FIRST (Task 1.7). Failure => nothing published/persisted.
        validated = validate_image_bytes(
            data, filename=filename, declared_mime=declared_mime
        )

        # 2) Publish bytes write-once BEFORE committing metadata. Idempotent by
        #    content hash; verifies/keeps existing bytes without overwrite.
        try:
            storage_path = self._store.write_once(
                self._workspace_id, validated.sha256, validated.data
            )
        except Exception as exc:  # FileStore integrity/OS error
            # Nothing persisted; surface a generic error.
            raise VaultServiceError("failed to store original bytes") from exc

        # 3) Persist metadata in ONE workspace-scoped transaction. Success is
        #    reported only after commit (criterion 13).
        try:
            result = self._persist(validated, storage_path, filename=filename)
            self._session.commit()
            return result
        except QuotaExceededError:
            self._session.rollback()
            # The published blob may now be an unreferenced orphan. It is NOT
            # deleted (write-once, no delete); it is harmless and reused by a
            # later identical upload. We never report success.
            raise
        except Exception as exc:
            self._session.rollback()
            raise VaultServiceError("failed to persist screenshot") from exc

    # --------------------------------------------------------------- internals

    def _persist(self, validated, storage_path: str, *, filename: str | None) -> UploadResult:
        # Lock the workspace row so the quota count+insert is atomic per workspace
        # (Postgres: SELECT ... FOR UPDATE; SQLite: harmless no-op + single writer).
        self._repo.lock_workspace()

        blob = self._get_or_create_blob(validated, storage_path)

        # Atomic quota enforcement under the workspace lock: count existing
        # screenshots, reject if adding one would exceed the limit.
        if self._repo.count_screenshots() + 1 > self._quota:
            raise QuotaExceededError("screenshot", self._quota)

        # A screenshot is a "duplicate" when the blob already backs one.
        existing = self._first_screenshot_for_blob(blob.id)
        is_duplicate = existing is not None

        screenshot = self._repo.create_screenshot(
            content_blob_id=blob.id,
            original_filename=(filename or ""),
            is_duplicate_of=existing.id if existing is not None else None,
        )

        return UploadResult(
            screenshot_id=screenshot.id,
            content_blob_id=blob.id,
            sha256=validated.sha256,
            mime_type=validated.mime_type,
            is_duplicate=is_duplicate,
            width=validated.width,
            height=validated.height,
            byte_size=validated.byte_size,
        )

    def _get_or_create_blob(self, validated, storage_path: str) -> models.ContentBlob:
        """Get the existing per-workspace blob, or create it.

        Because `_persist` holds the per-workspace write lock (see
        `lock_workspace`) before calling this, concurrent uploads of IDENTICAL
        content for the same workspace serialize here: the first commits the
        blob, and the next one — once it acquires the lock — finds the existing
        blob via `get_content_blob_by_sha256` and never attempts a conflicting
        insert.

        The UNIQUE(workspace_id, sha256) constraint remains a correctness
        backstop. If a concurrent INSERT still races to a unique violation, we
        use a SAVEPOINT (nested transaction) so only the failed INSERT is rolled
        back — NOT the whole transaction and its workspace lock — then re-fetch
        the winning row. An IntegrityError that is NOT our dedup conflict (the
        row is still absent afterward) is re-raised rather than silently
        swallowed, so unexpected integrity errors are never mistaken for an
        ordinary dedup conflict.
        """
        existing = self._repo.get_content_blob_by_sha256(validated.sha256)
        if existing is not None:
            return existing
        try:
            # Nested transaction (SAVEPOINT): an INSERT failure rolls back ONLY
            # this savepoint, preserving the outer transaction + workspace lock.
            with self._session.begin_nested():
                blob = self._repo.create_content_blob(
                    sha256=validated.sha256,
                    byte_size=validated.byte_size,
                    mime_type=validated.mime_type,  # detected format, not declared
                    storage_path=storage_path,
                )
            return blob
        except IntegrityError:
            # Could be our dedup conflict (a concurrent insert of the same
            # (workspace, sha256)) OR an unexpected integrity violation. Only
            # treat it as dedup if the winning row is now present; otherwise
            # re-raise so a genuine bug is not masked.
            winner = self._repo.get_content_blob_by_sha256(validated.sha256)
            if winner is not None:
                return winner
            raise

    def _first_screenshot_for_blob(self, blob_id: int) -> models.Screenshot | None:
        from sqlalchemy import select

        stmt = (
            select(models.Screenshot)
            .where(
                models.Screenshot.workspace_id == self._workspace_id,
                models.Screenshot.content_blob_id == blob_id,
            )
            .order_by(models.Screenshot.id.asc())
            .limit(1)
        )
        return self._session.scalars(stmt).first()

    # --------------------------------------------------------------- lifecycle

    _ACTIVE = "active"
    _ARCHIVED = "archived"

    def _owned_screenshot_or_raise(self, screenshot_id: int):
        shot = self._repo.get_screenshot(screenshot_id)
        if shot is None:
            # Missing or foreign-workspace: identical generic error, no disclosure.
            raise ScreenshotNotFoundError("screenshot not found")
        return shot

    def archive(self, screenshot_id: int) -> LifecycleResult:
        """Safe Burial: move an active screenshot into The Crypt (archived).

        Workspace-scoped. Idempotent: archiving an already-archived screenshot is
        a no-op success. Never touches the content_blob, its bytes, or checksum.
        Commits only after the state change; rolls back on any error.
        """
        shot = self._owned_screenshot_or_raise(screenshot_id)
        try:
            if shot.state != self._ARCHIVED:
                self._repo.set_screenshot_state(screenshot_id, self._ARCHIVED)
            self._session.commit()
        except ScreenshotNotFoundError:
            raise
        except Exception as exc:
            self._session.rollback()
            raise VaultServiceError("failed to archive screenshot") from exc
        return LifecycleResult(
            screenshot_id=screenshot_id, status="successful", state=self._ARCHIVED
        )

    def restore(self, screenshot_id: int) -> LifecycleResult:
        """Resurrection: return an archived screenshot to Active, verifying bytes.

        Reads the original bytes via the write-once file store, recomputes the
        SHA-256, and compares it to the stored content_blob checksum. Only on a
        match is the screenshot marked Active and the change committed. On a
        mismatch the restore is reported `failed`, the screenshot remains
        archived, and the original bytes are left untouched (no delete/rewrite).
        Workspace-scoped; idempotent (restoring an already-active screenshot is a
        no-op success — its bytes are still verified first).
        """
        shot = self._owned_screenshot_or_raise(screenshot_id)
        blob = self._repo.get_content_blob(shot.content_blob_id)
        if blob is None:
            # Should not happen for an owned screenshot; treat as a safe failure
            # without disclosing internals.
            raise VaultServiceError("failed to restore screenshot")

        # Integrity verification: recompute SHA-256 from the stored bytes and
        # compare to the checksum captured at upload. This never modifies bytes.
        import hashlib

        try:
            data = self._store.read(blob.storage_path)
            matches = hashlib.sha256(data).hexdigest() == blob.sha256
        except FileStoreIntegrityError:
            # The file store itself detected a content-address mismatch (corrupt
            # bytes). That IS a checksum mismatch -> a safe `failed` restore, not
            # an error. Keep the screenshot archived; bytes are left untouched.
            matches = False
        except Exception as exc:
            # A non-integrity read error (missing file / OS error) -> safe generic
            # failure; state and bytes retained, nothing disclosed.
            self._session.rollback()
            raise VaultServiceError("failed to restore screenshot") from exc

        if not matches:
            # DO NOT activate. Keep archived; bytes untouched. No state change.
            self._session.rollback()
            return LifecycleResult(
                screenshot_id=screenshot_id, status="failed", state=shot.state
            )

        # Verified: return to active and commit.
        try:
            if shot.state != self._ACTIVE:
                self._repo.set_screenshot_state(screenshot_id, self._ACTIVE)
            self._session.commit()
        except Exception as exc:
            self._session.rollback()
            raise VaultServiceError("failed to restore screenshot") from exc
        return LifecycleResult(
            screenshot_id=screenshot_id, status="successful", state=self._ACTIVE
        )

    # --------------------------------------------------------------- read

    def read_original(self, screenshot_id: int) -> OriginalImage:
        """Return the exact, unmodified original bytes for an OWNED screenshot.

        Workspace-scoped: a missing OR foreign-workspace screenshot raises the
        same generic ScreenshotNotFoundError (no existence disclosure). The bytes
        are read only through the write-once FileStore boundary and are verified
        (size + SHA-256) against the persisted content_blob metadata before
        return; a missing/corrupt original raises VaultServiceError rather than
        returning unverified bytes. This is a pure read — it never mutates state,
        publishes files, or creates records.
        """
        try:
            shot = self._owned_screenshot_or_raise(screenshot_id)
            blob = self._repo.get_content_blob(shot.content_blob_id)
        except ScreenshotNotFoundError:
            # Preserve the identical missing/foreign-workspace result.
            raise
        except Exception as exc:
            # Database/query failures must not escape the service boundary as
            # framework errors or be mistaken for an authorized read.
            self._session.rollback()
            raise VaultServiceError("failed to read original") from exc
        if blob is None:
            # An owned screenshot always has a blob; a missing one is a safe,
            # non-disclosing failure.
            raise VaultServiceError("failed to read original")
        try:
            # The FileStore verifies size + content-address SHA-256 on read; we
            # pass the persisted metadata for explicit defense-in-depth checks.
            data = self._store.read(
                blob.storage_path,
                expected_sha256=blob.sha256,
                expected_size=blob.byte_size,
            )
        except Exception as exc:
            # Missing file / integrity failure -> safe failure, nothing leaked.
            raise VaultServiceError("failed to read original") from exc
        return OriginalImage(
            screenshot_id=screenshot_id,
            data=data,
            mime_type=blob.mime_type,  # detected true format persisted at upload
            sha256=blob.sha256,
            byte_size=blob.byte_size,
        )

    # --------------------------------------------------------------- listing

    def list_screenshots(self, state: str | None = None) -> list[ScreenshotInfo]:
        """Return safe metadata for this workspace's screenshots.

        Workspace-scoped via the repository. Exposes only user-facing fields —
        never the storage path, workspace id/token, or other internals. `state`
        optionally filters to "active" or "archived".
        """
        rows = self._repo.list_screenshots(state=state)
        infos: list[ScreenshotInfo] = []
        for shot in rows:
            blob = self._repo.get_content_blob(shot.content_blob_id)
            infos.append(
                ScreenshotInfo(
                    screenshot_id=shot.id,
                    state=shot.state,
                    mime_type=(blob.mime_type if blob is not None else "application/octet-stream"),
                    original_filename=shot.original_filename or "",
                    is_duplicate=shot.is_duplicate_of is not None,
                    duplicate_of=shot.is_duplicate_of,
                    byte_size=(blob.byte_size if blob is not None else 0),
                    uploaded_at=shot.uploaded_at.isoformat() if shot.uploaded_at else "",
                )
            )
        return infos
