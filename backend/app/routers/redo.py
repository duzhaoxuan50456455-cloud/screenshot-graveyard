"""AI Redo routes (Task 3.3): 202 + operationId + polling.

  POST /api/study-notes/{artifact_id}/redo          -> redo a Study Note
  POST /api/screenshots/{screenshot_id}/analysis/redo -> redo Classification + Epitaph

Both accept an OPTIONAL JSON body ``{"instructions": "..."}`` (Req 7.7). A Redo is
always explicit and user-initiated, always creates a NEW ``ai_redo`` version, and
never touches earlier versions (manual edits included), provenance, or original
bytes. Poll the result with the existing ``GET /api/operations/{id}``.

Acceptance (defense in depth; the worker re-checks everything before any bytes are
read or the adapter is called):
  trusted workspace (401/503 fail closed) -> bounded body + instruction validation
  (400, never echoing the text) -> owned target (generic 404) -> consent for EVERY
  source (403, zero adapter calls) -> quota (429) -> 202.

The instruction text lives only in this request and the in-memory background task;
it is not persisted, logged, or returned in any response.
"""

from __future__ import annotations

import json
from collections.abc import Callable

from fastapi import APIRouter, BackgroundTasks, Depends, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.ai.provider import get_ai_adapter
from app.ai.redo_instructions import RedoInstructionsError, instructions_from_body
from app.db.session import get_session
from app.services.analysis_service import (
    AnalysisNotFoundError,
    AnalysisService,
    AnalysisUnavailableError,
)
from app.services.classification_service import ClassificationService
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
from app.services.study_note_artifact_service import (
    StudyNoteArtifactNotFoundError,
    StudyNoteArtifactService,
    StudyNoteArtifactUnavailableError,
)
from app.services.study_note_service import StudyNoteService
from app.services.study_selection_service import (
    StudySelectionError,
    StudySelectionNotFoundError,
    StudySelectionService,
    StudySelectionUnavailableError,
)

# Instructions are <= ~1,000 characters; even fully \u-escaped that is far below
# this. Reading is capped so an oversized body is rejected without buffering it.
_MAX_BODY_BYTES = 16 * 1024
_INVALID = object()


def _error_body(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message}}


def _unavailable() -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content=_error_body("service_unavailable", "Service temporarily unavailable"),
    )


def _not_found() -> JSONResponse:
    return JSONResponse(status_code=404, content=_error_body("not_found", "Not Found"))


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


async def _read_body(request: Request) -> object:
    """Parse the optional JSON body, reading at most `_MAX_BODY_BYTES`."""
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > _MAX_BODY_BYTES:
            return _INVALID
        chunks.append(chunk)
    raw = b"".join(chunks).strip()
    if not raw:
        return None  # no body == regenerate without extra guidance
    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return _INVALID


def _parse_instructions(body: object) -> tuple[str | None, JSONResponse | None]:
    try:
        if body is _INVALID:
            raise RedoInstructionsError("Invalid request body")
        return instructions_from_body(body), None
    except RedoInstructionsError as exc:
        # Fixed, safe messages only; the submitted text is never echoed.
        return None, JSONResponse(
            status_code=400, content=_error_body("invalid_instructions", str(exc))
        )


def _consent_and_operation(session: Session, workspace_id: int, screenshot_ids: list[int]):
    """Consent for EVERY source, then a metered pending `redo` operation."""
    consent = ConsentService(session, workspace_id)
    try:
        for sid in screenshot_ids:
            consent.require_consent(sid)
    except ConsentScreenshotNotFoundError:
        return None, _not_found()
    except ConsentRequiredError:
        return None, JSONResponse(
            status_code=403,
            content=_error_body("consent_required", "AI processing consent required"),
        )
    except ConsentServiceUnavailableError:
        return None, _unavailable()
    try:
        return OperationService(session, workspace_id).create_pending("redo"), None
    except OperationQuotaExceededError:
        return None, JSONResponse(
            status_code=429,
            content=_error_body("quota_exceeded", "AI operation quota exceeded"),
        )
    except OperationServiceUnavailableError:
        return None, _unavailable()


def _accepted(operation) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={
            "operationId": operation.operation_id,
            "kind": operation.kind,
            "status": operation.status,
        },
    )


def _run_job(workspace_id: int, operation_id: int, work: Callable[[Session], object]) -> None:
    """BackgroundTask body: own session + injected adapter. Never raises out."""
    session = get_session()
    try:
        work(session)
    except Exception:
        # Never strand a pollable operation in `pending` after an unexpected
        # worker error; if even this fails, Task 2.9's stale recovery still applies.
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


def _run_study_note_redo_job(
    workspace_id: int, artifact_id: int, operation_id: int, instructions: str | None
) -> None:
    _run_job(
        workspace_id,
        operation_id,
        lambda s: StudyNoteService(s, workspace_id, adapter=get_ai_adapter()).run_redo(
            artifact_id, operation_id, instructions
        ),
    )


def _run_analysis_redo_job(
    workspace_id: int, screenshot_id: int, operation_id: int, instructions: str | None
) -> None:
    _run_job(
        workspace_id,
        operation_id,
        lambda s: ClassificationService(s, workspace_id, adapter=get_ai_adapter()).run_redo(
            screenshot_id, operation_id, instructions
        ),
    )


def build_redo_router() -> APIRouter:
    router = APIRouter(prefix="/api", tags=["redo"])

    @router.post("/study-notes/{artifact_id}/redo", response_model=None)
    def redo_study_note(
        request: Request,
        artifact_id: int,
        background: BackgroundTasks,
        body: object = Depends(_read_body),
    ):
        workspace_id, err = _resolved_workspace_or_response(request)
        if err is not None:
            return err
        instructions, bad = _parse_instructions(body)
        if bad is not None:
            return bad

        session = get_session()
        try:
            try:
                # Owned study note + its provenance (the user's original order).
                note = StudyNoteArtifactService(session, workspace_id).get_version(artifact_id)
            except StudyNoteArtifactNotFoundError:
                return _not_found()
            except StudyNoteArtifactUnavailableError:
                return _unavailable()
            ids = list(note.source_screenshot_ids)
            try:
                StudySelectionService(session, workspace_id).validate_selection(ids)
            except StudySelectionNotFoundError:
                return _not_found()
            except (StudySelectionUnavailableError, StudySelectionError):
                return _unavailable()
            operation, err = _consent_and_operation(session, workspace_id, ids)
            if err is not None:
                return err
        finally:
            session.close()

        background.add_task(
            _run_study_note_redo_job,
            workspace_id,
            artifact_id,
            operation.operation_id,
            instructions,
        )
        return _accepted(operation)

    @router.post("/screenshots/{screenshot_id}/analysis/redo", response_model=None)
    def redo_analysis(
        request: Request,
        screenshot_id: int,
        background: BackgroundTasks,
        body: object = Depends(_read_body),
    ):
        workspace_id, err = _resolved_workspace_or_response(request)
        if err is not None:
            return err
        instructions, bad = _parse_instructions(body)
        if bad is not None:
            return bad

        session = get_session()
        try:
            try:
                # Missing, foreign, and never-analysed are one generic answer.
                AnalysisService(session, workspace_id).get_analysis(screenshot_id)
            except AnalysisNotFoundError:
                return _not_found()
            except AnalysisUnavailableError:
                return _unavailable()
            operation, err = _consent_and_operation(session, workspace_id, [screenshot_id])
            if err is not None:
                return err
        finally:
            session.close()

        background.add_task(
            _run_analysis_redo_job,
            workspace_id,
            screenshot_id,
            operation.operation_id,
            instructions,
        )
        return _accepted(operation)

    return router
