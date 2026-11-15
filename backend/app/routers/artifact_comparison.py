"""Workspace-scoped read-only retained-version comparison route (Task 3.4)."""

from __future__ import annotations

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from app.db.session import get_session
from app.services.artifact_comparison_service import (
    ArtifactComparisonNotFoundError,
    ArtifactComparisonService,
    ArtifactComparisonUnavailableError,
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


def build_artifact_comparison_router() -> APIRouter:
    router = APIRouter(prefix="/api/artifacts", tags=["artifacts"])

    @router.get("/{artifact_id}/compare", response_model=None)
    def compare_versions(
        request: Request,
        artifact_id: int,
        leftVersionId: int = Query(gt=0),
        rightVersionId: int = Query(gt=0),
    ):
        workspace_id, err = _workspace(request)
        if err is not None:
            return err
        # Reject duplicate parameters rather than silently accepting whichever
        # value the framework happens to select.  This is a read-only endpoint,
        # but explicit input handling keeps the comparison contract deterministic.
        if (
            len(request.query_params.getlist("leftVersionId")) != 1
            or len(request.query_params.getlist("rightVersionId")) != 1
        ):
            return JSONResponse(
                status_code=400,
                content=_error_body("invalid_version", "Invalid version identifier"),
            )
        session = get_session()
        try:
            return ArtifactComparisonService(session, workspace_id).compare(
                artifact_id, leftVersionId, rightVersionId
            ).as_dict()
        except ArtifactComparisonNotFoundError:
            return JSONResponse(
                status_code=404, content=_error_body("not_found", "Not Found")
            )
        except ArtifactComparisonUnavailableError:
            return JSONResponse(
                status_code=503,
                content=_error_body("service_unavailable", "Service temporarily unavailable"),
            )
        finally:
            session.close()

    return router
