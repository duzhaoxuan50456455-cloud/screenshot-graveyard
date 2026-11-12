"""Safe recovery of stale in-process AI operations (Task 2.9).

FastAPI BackgroundTasks disappear on a process restart.  This service performs
the intentionally small recovery mechanism from the approved design: on
startup, and later at the app lifespan's periodic boundary, transition only
pending operations whose ``updated_at`` is strictly older than the cutoff to
``failed``.  It never invokes an adapter, creates artifacts, reads originals,
or exposes workspace details.

The existing per-workspace lock serializes this sweep with in-process terminal
transitions.  It is not a distributed lock and does not claim multi-instance
cloud safety.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.db import models
from app.services.operation_service import (
    OperationService,
    OperationServiceUnavailableError,
)


@dataclass(frozen=True)
class RecoverySweepResult:
    """Non-sensitive result suitable for tests and internal observability."""

    recovered_count: int
    unavailable_workspace_count: int


class OperationRecoveryService:
    """Run one all-workspace stale-pending recovery sweep using fresh sessions."""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        *,
        stale_after_seconds: float,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if stale_after_seconds <= 0:
            raise ValueError("stale_after_seconds must be positive")
        self._session_factory = session_factory
        self._stale_after_seconds = stale_after_seconds
        self._now = now or (lambda: datetime.now(timezone.utc))

    def sweep(self) -> RecoverySweepResult:
        """Recover eligible rows without failing application startup on DB loss.

        If workspace discovery or a workspace-specific recovery transaction is
        unavailable, no status is fabricated.  Those pending rows remain
        pending for a later periodic/startup sweep.
        """
        now = self._now()
        if now.tzinfo is None:
            raise ValueError("recovery clock must return a timezone-aware datetime")

        try:
            session = self._session_factory()
        except Exception:
            return RecoverySweepResult(0, 1)
        try:
            try:
                workspace_ids = list(session.scalars(select(models.Workspace.id)))
            except SQLAlchemyError:
                session.rollback()
                return RecoverySweepResult(0, 1)
        finally:
            session.close()

        recovered_count = 0
        unavailable_count = 0
        for workspace_id in workspace_ids:
            try:
                workspace_session = self._session_factory()
            except Exception:
                unavailable_count += 1
                continue
            try:
                recovered_count += OperationService(
                    workspace_session, workspace_id
                ).fail_stale_pending(
                    older_than_seconds=self._stale_after_seconds,
                    now=now,
                )
            except (OperationServiceUnavailableError, SQLAlchemyError):
                # Fail closed: a DB error cannot claim a recovered/successful
                # operation.  The row remains as it was for the next sweep.
                workspace_session.rollback()
                unavailable_count += 1
            finally:
                workspace_session.close()
        return RecoverySweepResult(recovered_count, unavailable_count)
