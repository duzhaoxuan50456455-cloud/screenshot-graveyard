"""Workspace-scoped lifecycle records for asynchronous AI operations (Task 2.4).

This service owns the durable status boundary shared by future classify,
study-note, and redo services.  It does not invoke an adapter or read screenshot
bytes.  Those later services provide their persistence work through
``complete_after_persistence``; that method commits the produced artifacts and
the ``successful`` operation state in one transaction, so a poller cannot
observe a successful operation whose related persistence failed.

Task 2.4 deliberately creates lifecycle records only.  It does not simulate
Task 2.5 AI work with the fake adapter.  A future background worker must
re-check consent immediately before adapter invocation (and for every selected
study-note screenshot) using :class:`ConsentService`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.db import models
from app.db.repository import CrossWorkspaceError, WorkspaceRepository


AI_OPERATION_KINDS = frozenset({"classify", "study_note", "redo"})


class OperationServiceError(Exception):
    """Base lifecycle error with only safe, non-private messages."""


class OperationNotFoundError(OperationServiceError):
    """Missing and foreign operation IDs intentionally share this error."""


class OperationServiceUnavailableError(OperationServiceError):
    """The database could not safely create, read, or update an operation."""


class OperationPersistenceError(OperationServiceError):
    """Related artifact persistence failed; the operation was not successful."""


class OperationTransitionError(OperationServiceError):
    """A terminal operation cannot be transitioned again."""


@dataclass(frozen=True)
class OperationState:
    """Safe polling representation; never exposes workspace or storage details."""

    operation_id: int
    kind: str
    status: str
    error_detail: str | None = None


class OperationService:
    """Manage operation lifecycle records for one trusted workspace."""

    def __init__(self, session: Session, workspace_id: int) -> None:
        if workspace_id is None:
            raise ValueError("workspace_id is required")
        self._session = session
        self._repo = WorkspaceRepository(session, workspace_id)

    @staticmethod
    def _state(operation: models.Operation) -> OperationState:
        return OperationState(
            operation_id=operation.id,
            kind=operation.kind,
            status=operation.status,
            error_detail=operation.error_detail,
        )

    def create_pending(self, kind: str) -> OperationState:
        """Persist a new accepted AI operation in its initial ``pending`` state."""
        if kind not in AI_OPERATION_KINDS:
            raise ValueError("invalid operation kind")
        try:
            operation = self._repo.create_operation(kind=kind, status="pending")
            self._session.commit()
        except SQLAlchemyError as exc:
            self._session.rollback()
            raise OperationServiceUnavailableError("operation service unavailable") from exc
        return self._state(operation)

    def get(self, operation_id: int) -> OperationState:
        """Read an owned operation, treating absent and foreign IDs identically."""
        try:
            operation = self._repo.get_operation(operation_id)
        except SQLAlchemyError as exc:
            raise OperationServiceUnavailableError("operation service unavailable") from exc
        if operation is None:
            raise OperationNotFoundError("operation not found")
        return self._state(operation)

    def set_terminal_status(
        self, operation_id: int, status: str, error_detail: str | None = None
    ) -> OperationState:
        """Mark an owned pending operation failed or uncertain after safe handling.

        ``successful`` is intentionally excluded: callers must use
        :meth:`complete_after_persistence` so success is committed atomically
        with the related artifact persistence.
        """
        if status not in {"failed", "uncertain"}:
            raise ValueError("terminal status must be failed or uncertain")
        try:
            # Serialize terminal transitions for this workspace on both SQLite
            # and PostgreSQL.  Without this lock two workers could both observe
            # ``pending`` and execute conflicting lifecycle work.
            if self._repo.lock_workspace() is None:
                raise OperationNotFoundError("operation not found")
            current = self._repo.get_operation(operation_id)
            if current is None:
                raise OperationNotFoundError("operation not found")
            if current.status != "pending":
                raise OperationTransitionError("operation is already terminal")
            operation = self._repo.set_operation_status(
                operation_id, status, error_detail=error_detail
            )
            self._session.commit()
        except CrossWorkspaceError:
            self._session.rollback()
            raise OperationNotFoundError("operation not found")
        except SQLAlchemyError as exc:
            self._session.rollback()
            raise OperationServiceUnavailableError("operation service unavailable") from exc
        return self._state(operation)

    def complete_after_persistence(
        self, operation_id: int, persist: Callable[[], None]
    ) -> OperationState:
        """Atomically persist related work and make the operation successful.

        ``persist`` must only flush changes in this service's session; it must
        not commit.  The related writes and the pending→successful state change
        become externally visible together on the commit below.  On failure the
        transaction rolls back, then a fresh transaction records ``failed``.
        """
        try:
            # Serialize completion before executing the callback.  This is
            # deliberately before ``persist`` so a duplicate worker sees the
            # first worker's terminal status and cannot duplicate its writes.
            if self._repo.lock_workspace() is None:
                raise OperationNotFoundError("operation not found")
            # Confirm ownership before executing any persistence callback.
            current = self._repo.get_operation(operation_id)
            if current is None:
                raise OperationNotFoundError("operation not found")
            if current.status != "pending":
                raise OperationTransitionError("operation is already terminal")
            persist()
            operation = self._repo.set_operation_status(operation_id, "successful")
            self._session.commit()
        except (OperationNotFoundError, OperationTransitionError):
            self._session.rollback()
            raise
        except Exception as exc:
            self._session.rollback()
            try:
                # Fresh, separately committed failure transaction.  It locks
                # again so it cannot overwrite a completion by another worker.
                self.set_terminal_status(
                    operation_id, "failed", error_detail="Processing failed"
                )
            except OperationNotFoundError:
                raise OperationNotFoundError("operation not found") from exc
            except OperationServiceUnavailableError as status_exc:
                raise OperationServiceUnavailableError(
                    "operation service unavailable"
                ) from status_exc
            except OperationTransitionError:
                # Another serialized worker completed first.  Its terminal
                # result remains authoritative and is never overwritten.
                pass
            raise OperationPersistenceError("operation persistence failed") from exc
        return self._state(operation)

    def fail_stale_pending(self, older_than_seconds: float) -> int:
        """Recover stale in-process work after a restart without touching sources.

        The design's background tasks are intentionally non-durable.  This
        bounded, idempotent sweep converts only stale ``pending`` rows to
        ``failed``; it never retries work or invokes an adapter.
        """
        if older_than_seconds <= 0:
            raise ValueError("older_than_seconds must be positive")
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=older_than_seconds)
        try:
            # Use the same per-workspace serialization as completion so a
            # recovery sweep cannot race an actively committing worker.
            if self._repo.lock_workspace() is None:
                return 0
            stale = list(
                self._session.scalars(
                    select(models.Operation).where(
                        models.Operation.workspace_id == self._repo.workspace_id,
                        models.Operation.status == "pending",
                        models.Operation.updated_at < cutoff,
                    )
                )
            )
            for operation in stale:
                operation.status = "failed"
                operation.error_detail = "Processing interrupted; please retry"
                operation.updated_at = datetime.now(timezone.utc)
            self._session.commit()
        except SQLAlchemyError as exc:
            self._session.rollback()
            raise OperationServiceUnavailableError("operation service unavailable") from exc
        return len(stale)
