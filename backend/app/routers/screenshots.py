"""Screenshot upload router (Task 1.9).

Thin HTTP adapter for multi-file screenshot upload. It owns NO business logic:
validation, hashing, dedup, write-once publication, quota enforcement, and DB
writes all happen in `VaultService` (Task 1.8). The router only:

  * resolves the TRUSTED workspace id from request.state (never a client value),
  * fails closed when identity is missing or unavailable (DB outage),
  * streams each uploaded file through the authoritative actual-byte validator
    (Task 1.7) — Content-Length is NOT the boundary,
  * processes files with INDEPENDENT per-file outcomes (one bad file never rolls
    back previously-stored files), and
  * maps outcomes to the approved status codes (201 all-ok, 207 mixed, 400
    all-rejected / bad request), returning safe per-file errors.

Multipart buffering note (criterion 7): FastAPI/Starlette parse each multipart
part into a `SpooledTemporaryFile` (in-memory up to a threshold, then spilled to
a temp file on disk) as the request body is read. Starlette caps the whole
request body via its own limits, and the Task 1.5 middleware rejects an oversized
declared Content-Length early (413). The AUTHORITATIVE per-file 15 MB cap is
enforced here by reading each `UploadFile` through `validate_image_stream`, which
reads in bounded 64 KB chunks and aborts at cap+1 bytes — so a single part is
never fully buffered into memory beyond the cap during validation. Each
`UploadFile` is closed in a finally block so its spooled temp file is released.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response, UploadFile, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app import config
from app.db.session import get_session
from app.middleware.limits import BatchTooLargeError, enforce_max_files_per_batch
from app.db.repository import QuotaExceededError
from app.services.vault_service import (
    ScreenshotNotFoundError,
    VaultService,
    VaultServiceError,
)
from app.upload_validation import (
    PayloadTooLargeError,
    UploadValidationError,
    validate_image_stream,
)


class PerFileResult(BaseModel):
    filename: str
    status: str  # "accepted" | "rejected"
    screenshotId: int | None = None
    sha256: str | None = None
    mimeType: str | None = None
    isDuplicate: bool | None = None
    error: str | None = None


class UploadResponse(BaseModel):
    results: list[PerFileResult]


def _error_body(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message}}


def build_screenshots_router() -> APIRouter:
    router = APIRouter(prefix="/api/screenshots", tags=["screenshots"])

    @router.post("", response_model=None)
    def upload_screenshots(request: Request, response: Response, files: list[UploadFile]):
        # --- fail closed on identity (criterion 12, 13) ---
        if getattr(request.state, "identity_unavailable", False):
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return _error_body("identity_unavailable", "Service temporarily unavailable")
        workspace_id = getattr(request.state, "workspace_id", None)
        if workspace_id is None:
            response.status_code = status.HTTP_401_UNAUTHORIZED
            return _error_body("no_workspace", "No workspace")

        # --- batch shape checks (criterion 6) ---
        if not files:
            response.status_code = status.HTTP_400_BAD_REQUEST
            return _error_body("no_files", "No files provided")
        try:
            enforce_max_files_per_batch(len(files))
        except BatchTooLargeError:
            response.status_code = status.HTTP_400_BAD_REQUEST
            return _error_body(
                "too_many_files",
                f"Too many files in one batch (max {config.MAX_FILES_PER_BATCH})",
            )

        results: list[PerFileResult] = []
        session = get_session()
        try:
            for upload in files:
                # Client filename is for display only; never a path or format hint.
                display_name = (upload.filename or "unnamed").rsplit("/", 1)[-1].rsplit(
                    "\\", 1
                )[-1]
                try:
                    # Authoritative streaming validation on actual bytes.
                    validated = validate_image_stream(
                        upload.file,
                        filename=display_name,
                        declared_mime=upload.content_type,
                    )
                    # Persist via the service (its own per-file transaction).
                    vault = VaultService(session, workspace_id)
                    r = vault.upload(
                        validated.data,
                        filename=display_name,
                        declared_mime=upload.content_type,
                    )
                    results.append(
                        PerFileResult(
                            filename=display_name,
                            status="accepted",
                            screenshotId=r.screenshot_id,
                            sha256=r.sha256,
                            mimeType=r.mime_type,
                            isDuplicate=r.is_duplicate,
                        )
                    )
                except PayloadTooLargeError:
                    results.append(
                        PerFileResult(
                            filename=display_name,
                            status="rejected",
                            error=f"File exceeds the maximum size of {config.MAX_UPLOAD_BYTES} bytes",
                        )
                    )
                except UploadValidationError:
                    results.append(
                        PerFileResult(
                            filename=display_name,
                            status="rejected",
                            error="File is not a valid, supported image",
                        )
                    )
                except QuotaExceededError:
                    results.append(
                        PerFileResult(
                            filename=display_name,
                            status="rejected",
                            error="Workspace screenshot quota exceeded",
                        )
                    )
                except VaultServiceError:
                    # Generic — no internal paths / ids / stack traces leaked.
                    results.append(
                        PerFileResult(
                            filename=display_name,
                            status="rejected",
                            error="Could not store this file; please try again",
                        )
                    )
                finally:
                    # Release the spooled temp file / handle for this part.
                    try:
                        upload.file.close()
                    except Exception:
                        pass
        finally:
            session.close()

        # --- status mapping (criterion 10) ---
        accepted = sum(1 for r in results if r.status == "accepted")
        rejected = len(results) - accepted
        if accepted and rejected:
            response.status_code = status.HTTP_207_MULTI_STATUS
        elif accepted and not rejected:
            response.status_code = status.HTTP_201_CREATED
        else:  # all rejected
            response.status_code = status.HTTP_400_BAD_REQUEST
        return UploadResponse(results=results).model_dump()

    @router.get("/{screenshot_id}/image", response_model=None)
    def read_screenshot_image(request: Request, response: Response, screenshot_id: int):
        """Return the exact original bytes of an OWNED screenshot.

        Authorization is by the SERVER-RESOLVED workspace only; a client-supplied
        workspace id (query/header/body) is never consulted. A missing OR
        foreign-workspace screenshot returns the SAME generic 404 (existence
        non-disclosure). Bytes are read only through the VaultService/FileStore
        boundary and verified (size + SHA-256) before a 200. This is a safe GET:
        it never creates or mutates private data. Private originals are returned
        ONLY here, never via a static/SPA path.
        """
        # Fail closed if trusted identity could not be resolved (DB outage).
        if getattr(request.state, "identity_unavailable", False):
            return JSONResponse(
                status_code=503,
                content=_error_body("identity_unavailable", "Service temporarily unavailable"),
            )
        workspace_id = getattr(request.state, "workspace_id", None)
        if workspace_id is None:
            return JSONResponse(
                status_code=401, content=_error_body("no_workspace", "No workspace")
            )

        session = get_session()
        try:
            vault = VaultService(session, workspace_id)
            try:
                original = vault.read_original(screenshot_id)
            except ScreenshotNotFoundError:
                # Missing OR foreign-workspace: identical generic 404.
                return JSONResponse(
                    status_code=404, content=_error_body("not_found", "Not Found")
                )
            except VaultServiceError:
                # Missing/corrupt original: safe non-200 without disclosure. We
                # use 404 so a corrupt/absent file is indistinguishable from a
                # non-existent resource (no internal detail leaked).
                return JSONResponse(
                    status_code=404, content=_error_body("not_found", "Not Found")
                )
        finally:
            session.close()

        # Return exact bytes with the detected (persisted) MIME. No storage path,
        # workspace token, filename, or checksum is exposed in headers.
        return Response(
            content=original.data,
            media_type=original.mime_type,
            headers={"Cache-Control": "private, no-store"},
        )

    return router
