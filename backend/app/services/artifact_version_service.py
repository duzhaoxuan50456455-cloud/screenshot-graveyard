"""Append-only artifact version boundary (Task 3.1).

The ONE place that assigns a version number, inserts an `artifact_version` row,
and moves an artifact's `current_version_id`. Classification/Epitaph generation,
Study Note generation, and manual edits all go through it, so the append-only
history rules (INV-5, INV-6; Property 15) are enforced in a single spot instead
of being re-implemented per service.

Guarantees
  * INSERT-only. Nothing here updates or deletes a version row; the ORM also
    refuses to flush an UPDATE/DELETE of an `ArtifactVersion`
    (`models.AppendOnlyViolationError`).
  * Every version is stamped with the trusted workspace and verified to belong
    to an artifact owned by that workspace. A foreign or missing artifact raises
    the repository's generic `CrossWorkspaceError` (existence is not disclosed).
  * Version numbers start at 1 and each append is `max + 1`, so numbers are
    strictly increasing with no gaps. `UNIQUE(artifact_id, version_number)` is the
    database backstop against duplicates.
  * The version INSERT and the pointer update run inside one SAVEPOINT: if either
    fails, neither is applied, so no orphaned version or dangling pointer is left
    behind even if the caller later chooses to keep the outer transaction.

Transactions: this service NEVER commits or rolls back the outer transaction.
The caller owns it, which is what lets `OperationService.complete_after_persistence`
commit artifacts + versions + references + operation status atomically.

Concurrent completion (Task 3.7), in two layers:

  1. SERIALIZE. `append_version` first takes the existing per-workspace lock
     (`lock_workspace`). On SQLite that is a real database write lock; on
     PostgreSQL a row lock. Writers that use this service therefore queue behind
     one another and read a fresh `max`, so ordinary Redo + Manual Edit and
     Redo + Redo contention never produces a conflict at all.
  2. OPTIMISTIC BACKSTOP. If a writer nevertheless computes a number that another
     committed transaction already used (a writer that did not hold the lock, a
     lock that did not serialize, a stale read), the database's
     `UNIQUE(artifact_id, version_number)` rejects the INSERT. That one failure,
     and only that one, is recognised (`is_version_number_conflict`); the
     SAVEPOINT is rolled back, `max` is re-read, and the append is retried up to
     `MAX_VERSION_CONFLICT_ATTEMPTS` times. Each conflict means some other writer
     committed a version, so the loop makes progress. No sleeps, no randomness.

The retry lives INSIDE the caller's transaction and reuses the content the caller
already produced, so a conflict can never re-invoke an AI provider, create another
operation, or consume AI quota. When the bound is exhausted a sanitized
`VersionConflictError` is raised; the caller's transaction rolls back as a whole
(so a Classification + Epitaph pair stays all-or-nothing).

What this does NOT claim: it is not a distributed lock; it has not been run
against PostgreSQL or several app instances; it does not retry SQLite
"database is locked" (lock-wait timeout), serialization failures (SQLSTATE
40001) or any other IntegrityError; and persistent database outages still fail
the operation. Those are infrastructure failures, not version-number contention.
"""

from __future__ import annotations

import json

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import models
from app.db.repository import CrossWorkspaceError, WorkspaceRepository

ORIGIN_AI_GENERATE = "ai_generate"
ORIGIN_AI_REDO = "ai_redo"
ORIGIN_MANUAL_EDIT = "manual_edit"
VALID_ORIGINS = frozenset({ORIGIN_AI_GENERATE, ORIGIN_AI_REDO, ORIGIN_MANUAL_EDIT})
FIRST_VERSION = 1

# Bounded optimistic retry (Task 3.7). Read at call time so tests can tighten it.
MAX_VERSION_CONFLICT_ATTEMPTS = 5

# The one constraint whose violation means "that version number is taken".
_VERSION_UNIQUE_CONSTRAINT = "artifact_version_artifact_version"
_VERSION_UNIQUE_COLUMNS = frozenset(
    {"artifact_version.artifact_id", "artifact_version.version_number"}
)
_SQLSTATE_UNIQUE_VIOLATION = "23505"


class ArtifactVersionError(Exception):
    """Invalid use of the version boundary (bad origin or content)."""


class VersionConflictError(ArtifactVersionError):
    """Version-number conflicts persisted past the retry bound.

    Carries a fixed, safe message only: no SQL, parameters, content or ids. The
    caller's transaction must be rolled back; nothing was written.
    """


def is_version_number_conflict(exc: IntegrityError) -> bool:
    """True ONLY for a violation of UNIQUE(artifact_id, version_number).

    Any other IntegrityError (a foreign key, NOT NULL, another table's unique
    index, ...) is a genuine bug or data problem and must never be retried.

    SQLite reports the violated columns in its message; PostgreSQL drivers report
    the constraint name (`diag.constraint_name`, with a message fallback). The
    PostgreSQL branch is covered by shape-only unit tests, NOT by a live server.
    """
    orig = getattr(exc, "orig", None)
    if orig is None:
        return False
    diag = getattr(orig, "diag", None)
    constraint = getattr(diag, "constraint_name", None)
    if constraint is not None:
        return constraint == _VERSION_UNIQUE_CONSTRAINT
    code = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    if code is not None and code != _SQLSTATE_UNIQUE_VIOLATION:
        return False
    message = str(orig)
    if f'"{_VERSION_UNIQUE_CONSTRAINT}"' in message:
        return True
    prefix = "UNIQUE constraint failed:"
    if message.startswith(prefix):
        columns = {part.strip() for part in message[len(prefix):].split(",")}
        return columns == _VERSION_UNIQUE_COLUMNS
    return False


def _validate(origin: str, content_json: str) -> None:
    if origin not in VALID_ORIGINS:
        raise ArtifactVersionError("invalid version origin")
    if not isinstance(content_json, str):
        raise ArtifactVersionError("content_json must be a JSON string")
    try:
        json.loads(content_json)
    except (TypeError, ValueError) as exc:
        raise ArtifactVersionError("content_json must be valid JSON") from exc


class ArtifactVersionService:
    def __init__(
        self,
        session: Session,
        workspace_id: int,
        *,
        max_conflict_attempts: int | None = None,
    ) -> None:
        if workspace_id is None:
            raise ValueError("workspace_id is required")
        attempts = (
            MAX_VERSION_CONFLICT_ATTEMPTS
            if max_conflict_attempts is None
            else max_conflict_attempts
        )
        if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 1:
            raise ValueError("max_conflict_attempts must be a positive integer")
        self._session = session
        self._repo = WorkspaceRepository(session, workspace_id)
        self._max_attempts = attempts

    # ----------------------------------------------------------------- writes

    def create_first_version(
        self, artifact_id: int, content_json: str, origin: str = ORIGIN_AI_GENERATE
    ) -> models.ArtifactVersion:
        """Insert version 1 of a NEW artifact and point the artifact at it.

        Intended for the artifact's creator, inside that caller's transaction. It
        refuses an artifact that already has versions (use `append_version`).
        """
        _validate(origin, content_json)
        with self._session.begin_nested():
            if self._repo.max_version_number(artifact_id) != 0:
                raise ArtifactVersionError("artifact already has versions")
            return self._insert_and_point(artifact_id, FIRST_VERSION, origin, content_json)

    def append_version(
        self, artifact_id: int, content_json: str, origin: str
    ) -> models.ArtifactVersion:
        """Append the next version of an EXISTING artifact and make it current.

        Serializes on the workspace lock, then inserts optimistically. A recognised
        version-number conflict rolls back only this SAVEPOINT (the caller's
        transaction, its lock, and any unrelated pending work stay intact),
        re-reads `max`, and retries up to the explicit bound. Anything else
        propagates unchanged. See the module docstring for the guarantees.
        """
        _validate(origin, content_json)
        # Layer 1: serialize number assignment for this workspace.
        if self._repo.lock_workspace() is None:
            raise CrossWorkspaceError()
        # Layer 2: optimistic insert + bounded retry on a recognised conflict.
        for _attempt in range(self._max_attempts):
            try:
                with self._session.begin_nested():
                    next_number = self._repo.max_version_number(artifact_id) + 1
                    return self._insert_and_point(
                        artifact_id, next_number, origin, content_json
                    )
            except IntegrityError as exc:
                # The context manager already rolled the SAVEPOINT back, so the
                # outer transaction is valid and the failed row is gone.
                if not is_version_number_conflict(exc):
                    raise
        # `from None`: do not chain the driver error (it embeds SQL parameters).
        raise VersionConflictError(
            "version number conflict persisted; please retry"
        ) from None

    # ------------------------------------------------------------------ reads

    def get_current_version(self, artifact_id: int) -> models.ArtifactVersion | None:
        artifact = self._repo.get_artifact(artifact_id)
        if artifact is None:
            raise CrossWorkspaceError()
        if artifact.current_version_id is None:
            return None
        return self._repo.get_artifact_version(artifact.current_version_id)

    def list_versions(self, artifact_id: int) -> list[models.ArtifactVersion]:
        """Every retained version, oldest first."""
        return sorted(
            self._repo.list_artifact_versions(artifact_id),
            key=lambda v: v.version_number,
        )

    # --------------------------------------------------------------- internals

    def _insert_and_point(
        self, artifact_id: int, number: int, origin: str, content_json: str
    ) -> models.ArtifactVersion:
        version = self._repo.add_artifact_version(
            artifact_id=artifact_id,
            version_number=number,
            origin=origin,
            content_json=content_json,
        )
        self._repo.set_current_version(artifact_id, version.id)
        return version
