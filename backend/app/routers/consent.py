"""Consent HTTP router (Task 2.3).

Thin adapter over `ConsentService`. Owns no business logic: it resolves the
TRUSTED server-resolved workspace id (never a client value), fails closed when
identity is missing (401) or unavailable (503), and maps service errors to safe
status codes. A foreign/missing screenshot returns the SAME generic 404
(existence non-disclosure).

Routes:
  * GET  /api/screenshots/{id}/consent  -> current consent state (safe GET)
  * POST /api/screenshots/{id}/consent  -> record/withdraw consent (CSRF-guarded)

The POST is state-changing, so the existing CSRF middleware (SameSite + Origin +
X-Requested-With) already guards it. This router never calls an AIAdapter and
never touches image bytes.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.db.session import get_session
from app.services.consent_service import (
    ConsentService,
    ConsentServiceError,
    ConsentServiceUnavailableError,
    ScreenshotNotFoundError,
)


class ConsentBody(BaseModel):
    granted: bool


def _error_body(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message}}


def _resolved_workspace_or_response(request: Request):
    """Return (workspace_id, None) or (None, JSONResponse) failing closed."""
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


def build_consent_router() -> APIRouter:
    router = APIRouter(prefix="/api/screenshots", tags=["consent"])

    @router.get("/{screenshot_id}/consent", response_model=None)
    def read_consent(request: Request, screenshot_id: int):
        workspace_id, err = _resolved_workspace_or_response(request)
        if err is not None:
            return err
        session = get_session()
        try:
            svc = ConsentService(session, workspace_id)
            try:
                state = svc.get_consent(screenshot_id)
            except ScreenshotNotFoundError:
                return JSONResponse(status_code=404, content=_error_body("not_found", "Not Found"))
            except ConsentServiceUnavailableError:
                return JSONResponse(
                    status_code=503,
                    content=_error_body("service_unavailable", "Service temporarily unavailable"),
                )
            except ConsentServiceError:
                return JSONResponse(
                    status_code=503,
                    content=_error_body("service_unavailable", "Service temporarily unavailable"),
                )
        finally:
            session.close()
        return {"screenshotId": state.screenshot_id, "granted": state.granted}

    @router.post("/{screenshot_id}/consent", response_model=None)
    def record_consent(request: Request, screenshot_id: int, body: ConsentBody):
        workspace_id, err = _resolved_workspace_or_response(request)
        if err is not None:
            return err
        session = get_session()
        try:
            svc = ConsentService(session, workspace_id)
            try:
                state = svc.record_consent(screenshot_id, body.granted)
            except ScreenshotNotFoundError:
                return JSONResponse(status_code=404, content=_error_body("not_found", "Not Found"))
            except ConsentServiceUnavailableError:
                return JSONResponse(
                    status_code=503,
                    content=_error_body("service_unavailable", "Service temporarily unavailable"),
                )
            except ConsentServiceError:
                return JSONResponse(
                    status_code=503,
                    content=_error_body("service_unavailable", "Service temporarily unavailable"),
                )
        finally:
            session.close()
        return {"screenshotId": state.screenshot_id, "granted": state.granted}

    return router
