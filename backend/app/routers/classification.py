"""Classification HTTP router (Task 2.5): async 202 + operationId + polling.

`POST /api/screenshots/{id}/classify` accepts a Revive request and returns
`202 Accepted` with the durable `operationId` (poll via the existing
`GET /api/operations/{id}` from Task 2.4). The real classification runs in a
FastAPI BackgroundTask via `ClassificationService.run_classification`.

Enforcement at the request boundary (defense in depth; the AUTHORITATIVE gate is
the re-check inside the background worker):
  * Trusted server-resolved workspace only; fail closed 401/503.
  * A foreign/missing screenshot returns a generic 404 (non-disclosure).
  * Consent is pre-checked here for fast 403 feedback, AND re-checked inside the
    worker immediately before the adapter call, so consent revoked after
    acceptance still blocks the adapter.

The background worker opens its OWN session (the request session is closed when
the 202 response returns) and uses the injected adapter from the DI provider.
This router never transmits bytes to an external service.
"""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Request, status
from fastapi.responses import JSONResponse

from app.ai.provider import get_ai_adapter
from app.db.session import get_session
from app.services.classification_service import ClassificationService
from app.services.consent_service import (
    ConsentRequiredError,
    ConsentService,
    ConsentServiceUnavailableError,
    ScreenshotNotFoundError as ConsentScreenshotNotFoundError,
)
from app.services.operation_service import (
    OperationService,
    OperationServiceUnavailableError,
)


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


def _run_classification_job(workspace_id: int, screenshot_id: int, operation_id: int) -> None:
    """BackgroundTask body: own session + injected adapter. Never raises out."""
    session = get_session()
    try:
        service = ClassificationService(
            session, workspace_id, adapter=get_ai_adapter()
        )
        service.run_classification(screenshot_id, operation_id)
    except Exception:
        # Unexpected errors (including DB/adapter implementation bugs) may
        # occur before ClassificationService reaches one of its normal terminal
        # paths. Roll back this worker session, then make one best-effort,
        # separate failure transition so polling does not silently remain
        # pending. If the database is itself unavailable, Task 2.9's stale
        # sweep is the remaining safe recovery path.
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
            # No image bytes, provider details, or traceback are logged here.
            pass
    finally:
        session.close()


def build_classification_router() -> APIRouter:
    router = APIRouter(prefix="/api/screenshots", tags=["classification"])

    @router.post("/{screenshot_id}/classify", response_model=None)
    def classify(request: Request, screenshot_id: int, background: BackgroundTasks):
        """Revive a screenshot for classification. CSRF middleware guards this POST."""
        workspace_id, err = _resolved_workspace_or_response(request)
        if err is not None:
            return err

        session = get_session()
        try:
            # Foreign/missing -> generic 404; no-consent -> 403 (fast feedback).
            consent = ConsentService(session, workspace_id)
            try:
                consent.require_consent(screenshot_id)
            except ConsentScreenshotNotFoundError:
                return JSONResponse(status_code=404, content=_error_body("not_found", "Not Found"))
            except ConsentRequiredError:
                return JSONResponse(
                    status_code=403,
                    content=_error_body("consent_required", "AI processing consent required"),
                )
            except ConsentServiceUnavailableError:
                return JSONResponse(
                    status_code=503,
                    content=_error_body("service_unavailable", "Service temporarily unavailable"),
                )

            operations = OperationService(session, workspace_id)
            try:
                operation = operations.create_pending("classify")
            except OperationServiceUnavailableError:
                return JSONResponse(
                    status_code=503,
                    content=_error_body("service_unavailable", "Service temporarily unavailable"),
                )
        finally:
            session.close()

        # Schedule the real work AFTER the pending operation is committed. The
        # worker re-checks consent immediately before the single adapter call.
        background.add_task(
            _run_classification_job, workspace_id, screenshot_id, operation.operation_id
        )
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content={
                "operationId": operation.operation_id,
                "kind": operation.kind,
                "status": operation.status,
            },
        )

    return router
