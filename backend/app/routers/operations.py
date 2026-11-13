"""HTTP acceptance and polling endpoints for AI operation lifecycle records.

Task 2.4 accepts an operation and returns its durable ``operationId`` with HTTP
202.  It intentionally does not invoke an adapter: classify, study-note, and
redo services are later tasks and will attach their own BackgroundTask workers.
Those workers must use the consent gate immediately before any adapter call.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.db.session import get_session
from app.services.operation_service import (
    OperationNotFoundError,
    OperationService,
    OperationServiceUnavailableError,
)


class OperationAcceptanceBody(BaseModel):
    """Kind only: sources and AI execution are owned by later services."""

    kind: Literal["classify", "study_note", "redo"]


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


def _operation_dict(state) -> dict:
    result = {
        "operationId": state.operation_id,
        "kind": state.kind,
        "status": state.status,
    }
    if state.error_detail is not None:
        result["errorDetail"] = state.error_detail
    if state.artifact_id is not None:
        result["artifactId"] = state.artifact_id
    return result


def build_operations_router() -> APIRouter:
    router = APIRouter(prefix="/api/operations", tags=["operations"])

    @router.post("", response_model=None)
    def accept_operation(request: Request, body: OperationAcceptanceBody):
        """Create a durable pending operation; CSRF middleware guards this POST."""
        workspace_id, err = _resolved_workspace_or_response(request)
        if err is not None:
            return err
        session = get_session()
        try:
            service = OperationService(session, workspace_id)
            try:
                operation = service.create_pending(body.kind)
            except OperationServiceUnavailableError:
                return JSONResponse(
                    status_code=503,
                    content=_error_body("service_unavailable", "Service temporarily unavailable"),
                )
        finally:
            session.close()
        return JSONResponse(status_code=status.HTTP_202_ACCEPTED, content=_operation_dict(operation))

    @router.get("/{operation_id}", response_model=None)
    def poll_operation(request: Request, operation_id: int):
        """Poll an owned operation; foreign and missing identifiers both return 404."""
        workspace_id, err = _resolved_workspace_or_response(request)
        if err is not None:
            return err
        session = get_session()
        try:
            service = OperationService(session, workspace_id)
            try:
                operation = service.get(operation_id)
            except OperationNotFoundError:
                return JSONResponse(
                    status_code=404, content=_error_body("not_found", "Not Found")
                )
            except OperationServiceUnavailableError:
                return JSONResponse(
                    status_code=503,
                    content=_error_body("service_unavailable", "Service temporarily unavailable"),
                )
        finally:
            session.close()
        return _operation_dict(operation)

    return router
