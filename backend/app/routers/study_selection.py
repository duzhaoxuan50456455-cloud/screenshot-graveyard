"""HTTP boundary for manual Study Note screenshot selection (Task 2.6).

This route validates a user-selected, ordered list only.  It deliberately does
not read image bytes, check consent, create artifacts or operations, or invoke
an AI adapter; Task 2.7 owns execution and must re-check consent for every
selected screenshot immediately before any adapter call.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StrictInt

from app.db.session import get_session
from app.services.study_selection_service import (
    StudySelectionError,
    StudySelectionNotFoundError,
    StudySelectionService,
    StudySelectionUnavailableError,
    StudySelectionValidationError,
)


class StudySelectionBody(BaseModel):
    """Explicit user selection; no implicit/default screenshot list exists."""

    # IDs are strict: accepting e.g. a numeric string would turn malformed
    # client input into an apparently valid manual selection.
    screenshot_ids: list[StrictInt] = Field(alias="screenshotIds")

    model_config = {"populate_by_name": True}


def _error_body(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message}}


def _resolved_workspace_or_response(request: Request):
    if getattr(request.state, "identity_unavailable", False):
        return None, JSONResponse(
            status_code=503,
            content=_error_body("identity_unavailable", "Service temporarily unavailable"),
        )
    workspace_id = getattr(request.state, "workspace_id", None)
    if workspace_id is None:
        return None, JSONResponse(
            status_code=401, content=_error_body("no_workspace", "No workspace")
        )
    return workspace_id, None


def build_study_selection_router() -> APIRouter:
    router = APIRouter(prefix="/api/study-notes", tags=["study-notes"])

    @router.post("/selection", response_model=None)
    def validate_study_selection(request: Request, body: StudySelectionBody):
        """Validate explicit Study Note sources without starting AI work.

        It is a POST so the eventual selection/generation interaction shares the
        existing CSRF protections.  The endpoint remains read-only today.
        """
        workspace_id, err = _resolved_workspace_or_response(request)
        if err is not None:
            return err

        session = get_session()
        try:
            try:
                selection = StudySelectionService(session, workspace_id).validate_selection(
                    body.screenshot_ids
                )
            except StudySelectionValidationError as exc:
                return JSONResponse(
                    status_code=400,
                    content=_error_body("invalid_selection", str(exc)),
                )
            except StudySelectionNotFoundError:
                # Missing and foreign resources intentionally have identical 404s.
                return JSONResponse(
                    status_code=404, content=_error_body("not_found", "Not Found")
                )
            except (StudySelectionUnavailableError, StudySelectionError):
                return JSONResponse(
                    status_code=503,
                    content=_error_body("service_unavailable", "Service temporarily unavailable"),
                )
        finally:
            session.close()

        return {
            "screenshotIds": list(selection.screenshot_ids),
            "warning": selection.warning,
        }

    return router
