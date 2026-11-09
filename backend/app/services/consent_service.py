"""Consent_Service and the explicit AI consent gate (Task 2.3).

Records and reads per-screenshot, per-workspace consent for cloud AI processing,
and provides the single enforcement boundary every AI path must pass through
(Req 9.2, 9.4; Property P23). The contract:

  * No recorded, granted consent for a screenshot  => the AI path is BLOCKED and
    the AIAdapter is NEVER invoked (fail closed).
  * Store / archive / restore are entirely independent of consent — they never
    call this service, so a user can skip AI and still use the vault (Req 9.4).

Workspace scoping (INV via Req 9.9/12.6): every operation goes through the
workspace-scoped `WorkspaceRepository`, which stamps the TRUSTED server-resolved
`workspace_id` and treats a foreign/missing screenshot identically
(`CrossWorkspaceError`) — surfaced here as the SAME generic
`ScreenshotNotFoundError` the vault uses, so existence is never disclosed and a
client cannot grant/read consent for another workspace's screenshot. The service
never accepts a client-supplied workspace id; it is constructed with the id the
identity middleware resolved.

Privacy: this service records only a boolean grant + timestamp. It never reads,
stores, logs, or transmits image bytes, and it never contacts a network. The
`run_with_consent` helper is the guarded AI-invocation boundary: it verifies
consent BEFORE calling the supplied adapter callable, so an unauthorized request
cannot reach the adapter at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, TypeVar

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.db.repository import CrossWorkspaceError, WorkspaceRepository

T = TypeVar("T")


class ConsentServiceError(Exception):
    """Base error for consent operations (generic, no private data in messages)."""


class ScreenshotNotFoundError(ConsentServiceError):
    """Raised when a screenshot is absent OR owned by another workspace.

    The SAME error is used for both cases so a foreign-workspace screenshot is
    never distinguished from a missing one (existence non-disclosure). Mirrors
    the vault service's error of the same name.
    """


class ConsentRequiredError(ConsentServiceError):
    """Raised when an AI path is attempted without granted consent.

    Routers map this to HTTP 403. It is raised BEFORE any adapter call, so the
    adapter is never invoked without consent (P23).
    """


class ConsentServiceUnavailableError(ConsentServiceError):
    """Raised when consent state cannot be read or recorded safely.

    Callers must fail closed: a database failure is never treated as consent and
    therefore can never authorize an AI-adapter invocation.
    """


@dataclass(frozen=True)
class ConsentState:
    """A safe, user-facing view of a screenshot's consent state."""

    screenshot_id: int
    granted: bool


class ConsentService:
    def __init__(self, session: Session, workspace_id: int) -> None:
        if workspace_id is None:
            raise ValueError("workspace_id is required")
        self._session = session
        self._workspace_id = workspace_id
        self._repo = WorkspaceRepository(session, workspace_id)

    # ------------------------------------------------------------- record/read

    def record_consent(self, screenshot_id: int, granted: bool) -> ConsentState:
        """Record explicit consent (or an explicit withdrawal) for a screenshot.

        Workspace-scoped: a foreign/missing screenshot raises the generic
        ScreenshotNotFoundError and nothing is written. Commits on success.
        """
        try:
            record = self._repo.set_consent(
                screenshot_id=screenshot_id, granted=bool(granted)
            )
            self._session.commit()
        except CrossWorkspaceError:
            # Missing OR foreign-workspace screenshot: identical generic result.
            self._session.rollback()
            raise ScreenshotNotFoundError("screenshot not found")
        except SQLAlchemyError as exc:
            self._session.rollback()
            raise ConsentServiceUnavailableError("consent service unavailable") from exc
        return ConsentState(screenshot_id=screenshot_id, granted=record.granted)

    def get_consent(self, screenshot_id: int) -> ConsentState:
        """Return the consent state for an OWNED screenshot.

        A foreign/missing screenshot raises the generic ScreenshotNotFoundError.
        A screenshot with no consent record yet reports granted=False.
        """
        try:
            record = self._repo.get_consent(screenshot_id)
        except CrossWorkspaceError:
            raise ScreenshotNotFoundError("screenshot not found")
        except SQLAlchemyError as exc:
            raise ConsentServiceUnavailableError("consent service unavailable") from exc
        granted = bool(record.granted) if record is not None else False
        return ConsentState(screenshot_id=screenshot_id, granted=granted)

    def has_consent(self, screenshot_id: int) -> bool:
        """True only if explicit, granted consent is recorded for the screenshot.

        Fails closed: a foreign/missing screenshot or any absent record yields
        False (no exception leaks to callers that only want a boolean gate for an
        owned screenshot; foreign access is still non-disclosing because the repo
        returns the generic cross-workspace error, mapped to False here)."""
        try:
            record = self._repo.get_consent(screenshot_id)
        except (CrossWorkspaceError, SQLAlchemyError):
            return False
        return record is not None and bool(record.granted)

    # --------------------------------------------------------------- the gate

    def require_consent(self, screenshot_id: int) -> None:
        """Raise unless explicit, granted consent exists for an OWNED screenshot.

        A foreign/missing screenshot raises ScreenshotNotFoundError (404-worthy,
        non-disclosing); an owned screenshot without granted consent raises
        ConsentRequiredError (403-worthy). Returns None only when the AI path is
        authorized to proceed.
        """
        try:
            record = self._repo.get_consent(screenshot_id)
        except CrossWorkspaceError:
            raise ScreenshotNotFoundError("screenshot not found")
        except SQLAlchemyError as exc:
            raise ConsentServiceUnavailableError("consent service unavailable") from exc
        if record is None or not record.granted:
            raise ConsentRequiredError("AI processing consent required")

    def run_with_consent(
        self, screenshot_id: int, adapter_call: Callable[[], T]
    ) -> T:
        """The guarded AI-invocation boundary.

        Verifies consent FIRST; only if consent is granted is `adapter_call`
        invoked. If consent is missing the adapter callable is NEVER called —
        ConsentRequiredError is raised before it can run (P23). `adapter_call`
        is a zero-arg callable the caller has bound to its adapter + image bytes;
        this service never sees or logs the bytes.
        """
        self.require_consent(screenshot_id)
        return adapter_call()
