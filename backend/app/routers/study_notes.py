"""Study Note generation HTTP route (Task 2.7): 202 + operationId + polling.

`POST /api/study-notes` accepts an explicit, ordered `screenshotIds` list,
validates it with the Task 2.6 policy, pre-checks consent for EVERY selected
screenshot (403 with zero adapter calls if any lacks it), creates a pending
`study_note` operation, and schedules the work as a BackgroundTask. The worker
opens its OWN session and re-checks selection + consent for all screenshots
immediately before any bytes are read or the adapter is called. Poll the result
with the existing `GET /api/operations/{id}`.

Identity is the trusted server-resolved workspace (401/503 fail closed); CSRF
middleware guards this POST. Responses never contain image bytes or model output.
"""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StrictInt

from app.ai.provider import get_ai_adapter
from app.db.session import get_session
from app.services.consent_service import (
    ConsentRequiredError,
    ConsentService,
    ConsentServiceUnavailableError,
    ScreenshotNotFoundError as ConsentScreenshotNotFoundError,
)
from app.services.operation_service import (
    OperationQuotaExceededError,
    OperationService,
    OperationServiceUnavailableError,
)
from app.services.study_note_service import StudyNoteService
from app.services.study_selection_service import (
    StudySelectionError,
    StudySelectionNotFoundError,
    StudySelectionService,
    StudySelectionUnavailableError,
    StudySelectionValidationError,
)


class StudyNoteBody(BaseModel):
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


def _run_study_note_job(workspace_id: int, screenshot_ids: list[int], operation_id: int) -> None:
    """BackgroundTask body: own session + injected adapter. Never raises out."""
    session = get_session()
    try:
        StudyNoteService(session, workspace_id, adapter=get_ai_adapter()).run_generation(
            screenshot_ids, operation_id
        )
    except Exception:
        # An unexpected worker error must not silently strand a pollable
        # operation in pending. Roll back this session, then make one
        # best-effort terminal failure transition in a fresh session. If the
        # database remains unavailable, Task 2.9's stale-pending recovery is
        # still required; no success is ever reported here.
        session.rollback()
        try:
            failure_session = get_session()
            try:
                OperationService(failure_session, workspace_id).set_terminal_status(
                    operation_id, "failed", error_detail="AI analysis failed"
                )
            finally:
                failure_session.close()
        except Exception:
            pass
    finally:
        session.close()


def build_study_notes_router() -> APIRouter:
    router = APIRouter(prefix="/api/study-notes", tags=["study-notes"])

    @router.post("", response_model=None)
    def generate_study_note(
        request: Request, body: StudyNoteBody, background: BackgroundTasks
    ):
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
                    status_code=400, content=_error_body("invalid_selection", str(exc))
                )
            except StudySelectionNotFoundError:
                return JSONResponse(
                    status_code=404, content=_error_body("not_found", "Not Found")
                )
            except (StudySelectionUnavailableError, StudySelectionError):
                return JSONResponse(
                    status_code=503,
                    content=_error_body("service_unavailable", "Service temporarily unavailable"),
                )

            consent = ConsentService(session, workspace_id)
            try:
                for sid in selection.screenshot_ids:
                    consent.require_consent(sid)
            except ConsentScreenshotNotFoundError:
                return JSONResponse(
                    status_code=404, content=_error_body("not_found", "Not Found")
                )
            except ConsentRequiredError:
                # Any single unauthorized screenshot blocks the whole batch.
                return JSONResponse(
                    status_code=403,
                    content=_error_body("consent_required", "AI processing consent required"),
                )
            except ConsentServiceUnavailableError:
                return JSONResponse(
                    status_code=503,
                    content=_error_body("service_unavailable", "Service temporarily unavailable"),
                )

            try:
                operation = OperationService(session, workspace_id).create_pending("study_note")
            except OperationQuotaExceededError:
                return JSONResponse(
                    status_code=429,
                    content=_error_body("quota_exceeded", "AI operation quota exceeded"),
                )
            except OperationServiceUnavailableError:
                return JSONResponse(
                    status_code=503,
                    content=_error_body("service_unavailable", "Service temporarily unavailable"),
                )
        finally:
            session.close()

        background.add_task(
            _run_study_note_job,
            workspace_id,
            list(selection.screenshot_ids),
            operation.operation_id,
        )
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content={
                "operationId": operation.operation_id,
                "kind": operation.kind,
                "status": operation.status,
                "warning": selection.warning,
            },
        )

    return router
