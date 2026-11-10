"""Manual Study Note screenshot selection policy (Task 2.6).

This deliberately narrow service validates a *user-supplied* ordered list of
Screenshot IDs for the future Study_Note_Service.  It does not read originals,
create an operation or artifact, inspect consent, or invoke an AI adapter;
those execution concerns belong to Tasks 2.7 and 2.10.

The approved policy is:

* require an explicit list containing 1--12 screenshots;
* retain its order exactly;
* warn, without rejecting, when the count is outside the recommended 4--8;
* authorise every ID through the repository bound to the trusted workspace.

The specification does not define duplicate-ID behavior.  We reject duplicates
rather than silently de-duplicating them: silently changing the supplied list
would change user intent, and later provenance has one source-reference per
Screenshot ID.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.db.repository import WorkspaceRepository

MIN_SELECTION = 1
MAX_SELECTION = 12
RECOMMENDED_MIN_SELECTION = 4
RECOMMENDED_MAX_SELECTION = 8


class StudySelectionError(Exception):
    """Base error for the selection-only boundary."""


class StudySelectionValidationError(StudySelectionError):
    """The explicit selection violates the count or duplicate-ID policy."""


class StudySelectionNotFoundError(StudySelectionError):
    """An ID is missing or belongs to another workspace (non-disclosing)."""


class StudySelectionUnavailableError(StudySelectionError):
    """Database access failed; callers must fail closed rather than accept."""


@dataclass(frozen=True)
class StudySelection:
    """Validated ordered IDs and a non-blocking sizing advisory, if needed."""

    screenshot_ids: tuple[int, ...]
    warning: str | None


class StudySelectionService:
    """Validate manual selection without reading private image bytes."""

    def __init__(self, session: Session, workspace_id: int) -> None:
        if workspace_id is None:
            raise ValueError("workspace_id is required")
        self._repo = WorkspaceRepository(session, workspace_id)

    def validate_selection(self, screenshot_ids: list[int]) -> StudySelection:
        """Return an owned, ordered selection or a safe domain error.

        This is intentionally read-only.  `WorkspaceRepository.get_screenshot`
        filters by the service's trusted workspace, so missing and foreign IDs
        are indistinguishable and no client workspace field is ever considered.
        """
        count = len(screenshot_ids)
        if count < MIN_SELECTION or count > MAX_SELECTION:
            raise StudySelectionValidationError(
                "Select between 1 and 12 screenshots to create a study note"
            )
        if len(set(screenshot_ids)) != count:
            raise StudySelectionValidationError(
                "Select each screenshot only once for a study note"
            )

        try:
            # Do not sort or de-duplicate: order is future provenance order.
            for screenshot_id in screenshot_ids:
                if self._repo.get_screenshot(screenshot_id) is None:
                    raise StudySelectionNotFoundError("Not found")
        except StudySelectionNotFoundError:
            raise
        except SQLAlchemyError as exc:
            raise StudySelectionUnavailableError("Service temporarily unavailable") from exc

        warning = None
        if count < RECOMMENDED_MIN_SELECTION or count > RECOMMENDED_MAX_SELECTION:
            warning = (
                "Study notes work best with 4 to 8 related screenshots; "
                "you can still continue with this selection."
            )
        return StudySelection(screenshot_ids=tuple(screenshot_ids), warning=warning)
