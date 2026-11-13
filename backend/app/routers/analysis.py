"""Classification + Epitaph read/edit routes (Task 2.10 integration).

  GET  /api/screenshots/{id}/analysis        -> current category + Epitaph (safe GET)
  POST /api/screenshots/{id}/analysis/edits  -> append a manual_edit version (CSRF-guarded)

Trusted server-resolved workspace only (401/503 fail closed); a foreign, missing
or never-analysed screenshot returns the same generic 404. These routes never
read image bytes, call an AI adapter, or touch consent.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.db.session import get_session
from app.services.analysis_service import (
    AnalysisEditValidationError,
    AnalysisNotFoundError,
    AnalysisService,
    AnalysisUnavailableError,
)


def _error_body(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message}}


def _workspace(request: Request):
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


def _view_dict(v) -> dict:
    return {
        "screenshotId": v.screenshot_id,
        "classification": {
            "category": v.category,
            "confidence": v.confidence,
            "uncertain": v.uncertain,
            "versionNumber": v.classification_version_number,
            "origin": v.classification_origin,
        },
        "epitaph": {
            "text": v.epitaph_text,
            "visibleFacts": v.visible_facts,
            "inferredIntent": v.inferred_intent,
            "uncertain": v.epitaph_uncertain,
            "versionNumber": v.epitaph_version_number,
            "origin": v.epitaph_origin,
        },
    }


def build_analysis_router() -> APIRouter:
    router = APIRouter(prefix="/api/screenshots", tags=["analysis"])

    @router.get("/{screenshot_id}/analysis", response_model=None)
    def read_analysis(request: Request, screenshot_id: int):
        workspace_id, err = _workspace(request)
        if err is not None:
            return err
        session = get_session()
        try:
            return _view_dict(AnalysisService(session, workspace_id).get_analysis(screenshot_id))
        except AnalysisNotFoundError:
            return JSONResponse(status_code=404, content=_error_body("not_found", "Not Found"))
        except AnalysisUnavailableError:
            return JSONResponse(status_code=503, content=_error_body("service_unavailable", "Service temporarily unavailable"))
        finally:
            session.close()

    @router.post("/{screenshot_id}/analysis/edits", response_model=None)
    async def save_analysis_edit(request: Request, screenshot_id: int):
        workspace_id, err = _workspace(request)
        if err is not None:
            return err
        try:
            raw = await request.json()
        except Exception:
            return JSONResponse(status_code=400, content=_error_body("invalid_edit", "Invalid analysis edit"))
        if not isinstance(raw, dict):
            return JSONResponse(status_code=400, content=_error_body("invalid_edit", "Invalid analysis edit"))
        # camelCase wire format -> service fields; unknown keys stay and are rejected.
        mapped = {("epitaph_text" if k == "epitaphText" else k): v for k, v in raw.items()}
        session = get_session()
        try:
            return _view_dict(AnalysisService(session, workspace_id).save_edit(screenshot_id, mapped))
        except AnalysisEditValidationError:
            return JSONResponse(status_code=400, content=_error_body("invalid_edit", "Invalid analysis edit"))
        except AnalysisNotFoundError:
            return JSONResponse(status_code=404, content=_error_body("not_found", "Not Found"))
        except AnalysisUnavailableError:
            return JSONResponse(status_code=503, content=_error_body("service_unavailable", "Service temporarily unavailable"))
        finally:
            session.close()

    return router
