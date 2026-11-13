"""Study Note retained-version edit, source listing, and Markdown export routes.

Task 2.8 uses the existing private ``/api/screenshots/{id}/image`` endpoint to
open originals.  This router only exposes the owning artifact's source IDs and
safe relative image URLs; it never reads or returns original image bytes.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from app.db.session import get_session
from app.services.study_note_artifact_service import (
    ManualStudyNoteContent,
    StudyNoteArtifactNotFoundError,
    StudyNoteArtifactService,
    StudyNoteArtifactUnavailableError,
    StudyNoteEditValidationError,
)


class StudyNoteEditBody(BaseModel):
    content: ManualStudyNoteContent


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


def _version_dict(version) -> dict:
    return {
        "artifactId": version.artifact_id,
        "versionId": version.version_id,
        "versionNumber": version.version_number,
        "origin": version.origin,
        "content": version.content.model_dump(),
        "sourceScreenshotIds": version.source_screenshot_ids,
    }


def build_study_note_artifacts_router() -> APIRouter:
    router = APIRouter(prefix="/api/study-notes", tags=["study-notes"])

    @router.get("", response_model=None)
    def list_study_notes(request: Request):
        workspace_id, err = _workspace(request)
        if err is not None:
            return err
        session = get_session()
        try:
            return {"notes": StudyNoteArtifactService(session, workspace_id).list_notes()}
        except StudyNoteArtifactUnavailableError:
            return JSONResponse(status_code=503, content=_error_body("service_unavailable", "Service temporarily unavailable"))
        finally:
            session.close()

    @router.get("/{artifact_id}/versions", response_model=None)
    def list_study_note_versions(request: Request, artifact_id: int):
        workspace_id, err = _workspace(request)
        if err is not None:
            return err
        session = get_session()
        try:
            return {"versions": StudyNoteArtifactService(session, workspace_id).list_versions(artifact_id)}
        except StudyNoteArtifactNotFoundError:
            return JSONResponse(status_code=404, content=_error_body("not_found", "Not Found"))
        except StudyNoteArtifactUnavailableError:
            return JSONResponse(status_code=503, content=_error_body("service_unavailable", "Service temporarily unavailable"))
        finally:
            session.close()

    @router.get("/{artifact_id}", response_model=None)
    def get_study_note(request: Request, artifact_id: int, versionId: int | None = None):
        workspace_id, err = _workspace(request)
        if err is not None:
            return err
        session = get_session()
        try:
            version = StudyNoteArtifactService(session, workspace_id).get_version(artifact_id, versionId)
            return _version_dict(version)
        except StudyNoteArtifactNotFoundError:
            return JSONResponse(status_code=404, content=_error_body("not_found", "Not Found"))
        except StudyNoteArtifactUnavailableError:
            return JSONResponse(status_code=503, content=_error_body("service_unavailable", "Service temporarily unavailable"))
        finally:
            session.close()

    @router.post("/{artifact_id}/edits", response_model=None)
    def save_study_note_edit(request: Request, artifact_id: int, body: StudyNoteEditBody):
        """CSRF middleware protects this state-changing append-only save."""
        workspace_id, err = _workspace(request)
        if err is not None:
            return err
        session = get_session()
        try:
            version = StudyNoteArtifactService(session, workspace_id).save_edit(
                artifact_id, body.content.model_dump()
            )
            return _version_dict(version)
        except StudyNoteEditValidationError:
            return JSONResponse(status_code=400, content=_error_body("invalid_content", "Invalid study note content"))
        except StudyNoteArtifactNotFoundError:
            return JSONResponse(status_code=404, content=_error_body("not_found", "Not Found"))
        except StudyNoteArtifactUnavailableError:
            return JSONResponse(status_code=503, content=_error_body("service_unavailable", "Service temporarily unavailable"))
        finally:
            session.close()

    @router.get("/{artifact_id}/sources", response_model=None)
    def list_sources(request: Request, artifact_id: int):
        workspace_id, err = _workspace(request)
        if err is not None:
            return err
        session = get_session()
        try:
            version = StudyNoteArtifactService(session, workspace_id).get_version(artifact_id)
            return {"sources": [
                {"screenshotId": sid, "imageUrl": f"/api/screenshots/{sid}/image"}
                for sid in version.source_screenshot_ids
            ]}
        except StudyNoteArtifactNotFoundError:
            return JSONResponse(status_code=404, content=_error_body("not_found", "Not Found"))
        except StudyNoteArtifactUnavailableError:
            return JSONResponse(status_code=503, content=_error_body("service_unavailable", "Service temporarily unavailable"))
        finally:
            session.close()

    @router.get("/{artifact_id}/export", response_model=None)
    def export_markdown(request: Request, artifact_id: int, versionId: int | None = None):
        workspace_id, err = _workspace(request)
        if err is not None:
            return err
        session = get_session()
        try:
            version = StudyNoteArtifactService(session, workspace_id).get_version(artifact_id, versionId)
            markdown = StudyNoteArtifactService.to_markdown(version)
            # The filename is server-generated from validated integer IDs only;
            # no title/client filename can become a header/path injection.
            return Response(
                content=markdown.encode("utf-8"),
                media_type="text/markdown; charset=utf-8",
                headers={
                    "Content-Disposition": (
                        f'attachment; filename="study-note-{version.artifact_id}-v{version.version_number}.md"'
                    ),
                    "Cache-Control": "private, no-store",
                },
            )
        except StudyNoteArtifactNotFoundError:
            return JSONResponse(status_code=404, content=_error_body("not_found", "Not Found"))
        except StudyNoteArtifactUnavailableError:
            return JSONResponse(status_code=503, content=_error_body("service_unavailable", "Service temporarily unavailable"))
        finally:
            session.close()

    return router
