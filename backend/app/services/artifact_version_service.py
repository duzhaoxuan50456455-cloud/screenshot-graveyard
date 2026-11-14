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

Concurrency boundary (be precise): `append_version` takes the existing
per-workspace lock (`lock_workspace`) before reading `max`. On SQLite that is a
real database write lock; on PostgreSQL it is a row lock. That serializes writers
that all use this service, in one database. It is NOT a distributed lock and
is not verified here against PostgreSQL or several app instances. If two writers
still race to the same number, the UNIQUE constraint rejects the loser with an
`IntegrityError` (surfaced to the caller as a SQLAlchemyError); the optimistic
insert + bounded retry-on-conflict that lets both complete is Task 3.7.
"""

from __future__ import annotations

import json

from sqlalchemy.orm import Session

from app.db import models
from app.db.repository import CrossWorkspaceError, WorkspaceRepository

ORIGIN_AI_GENERATE = "ai_generate"
ORIGIN_AI_REDO = "ai_redo"
ORIGIN_MANUAL_EDIT = "manual_edit"
VALID_ORIGINS = frozenset({ORIGIN_AI_GENERATE, ORIGIN_AI_REDO, ORIGIN_MANUAL_EDIT})
FIRST_VERSION = 1


class ArtifactVersionError(Exception):
    """Invalid use of the version boundary (bad origin or content)."""


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
    def __init__(self, session: Session, workspace_id: int) -> None:
        if workspace_id is None:
            raise ValueError("workspace_id is required")
        self._session = session
        self._repo = WorkspaceRepository(session, workspace_id)

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
        """Append the next version of an EXISTING artifact and make it current."""
        _validate(origin, content_json)
        # Serialize number assignment for this workspace (see module docstring).
        if self._repo.lock_workspace() is None:
            raise CrossWorkspaceError()
        with self._session.begin_nested():
            next_number = self._repo.max_version_number(artifact_id) + 1
            return self._insert_and_point(artifact_id, next_number, origin, content_json)

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
