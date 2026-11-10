"""FastAPI application factory for the Screenshot Graveyard backend.

This module exposes :func:`create_app`, an app factory that builds and returns
a configured :class:`fastapi.FastAPI` instance, plus a module-level ``app``
object so the server can be launched with ``app.main:app``.

In addition to the API, the app serves the built React single-page app from
``frontend/dist`` at the same origin as ``/api`` (Task 0.4). Serving the SPA
same-origin is what makes same-origin cookies and the dev proxy's production
equivalent work. Static serving is guarded: if ``frontend/dist`` is absent
(backend-only development, CI, pytest), the app still starts and the API still
works — only the SPA routes are skipped.

The scaffold is otherwise minimal. The database layer, services, repositories,
the file store, and feature endpoints are added by later tasks.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.middleware.csrf import CSRFMiddleware
from app.middleware.limits import (
    MaxBodySizeMiddleware,
    RateLimitMiddleware,
    WorkspaceRateLimitMiddleware,
)
from app.middleware.workspace import WorkspaceMiddleware

APP_TITLE = "Screenshot Graveyard API"
APP_VERSION = "0.1.0"

# backend/app/main.py -> repo root is two parents up from this file's dir.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_FRONTEND_DIST = _REPO_ROOT / "frontend" / "dist"

_NOT_FOUND = {"error": {"code": "not_found", "message": "Not Found"}}


def _build_api_router() -> APIRouter:
    """Build the API router. All API paths live under ``/api``."""
    router = APIRouter(prefix="/api")

    @router.get("/health")
    def health() -> dict[str, str]:
        """Liveness check. No auth, no database dependency."""
        return {"status": "ok"}

    @router.get("/workspace", response_model=None)
    def workspace(request: Request) -> dict[str, bool] | JSONResponse:
        """Bootstrap/confirm the visitor's Workspace.

        The WorkspaceMiddleware has already resolved (or minted) the trusted
        workspace id and attached it to request.state. This endpoint returns a
        non-sensitive confirmation only — it NEVER returns the raw token or the
        workspace id, so nothing identity-bearing leaks into the response body.
        """
        # A failed database lookup/mint is not the same as an anonymous visitor.
        # The frontend must not interpret an unavailable identity store as a
        # valid-but-inactive workspace and retry forever.
        if getattr(request.state, "identity_unavailable", False):
            return JSONResponse(
                status_code=503,
                content={
                    "error": {
                        "code": "identity_unavailable",
                        "message": "Service temporarily unavailable",
                    }
                },
            )
        return {"workspaceActive": getattr(request.state, "workspace_id", None) is not None}

    return router


def _mount_spa(application: FastAPI, dist_dir: Path) -> None:
    """Serve the built SPA from ``dist_dir`` at the same origin as the API.

    Hashed asset files under ``dist/assets`` are served as static files. Any
    other non-API GET path returns ``index.html`` so client-side routing works.
    Unknown ``/api`` paths are never served the SPA (the ``/api`` catch-all in
    :func:`create_app` is registered first and takes precedence).
    """
    assets_dir = dist_dir / "assets"
    if assets_dir.is_dir():
        application.mount(
            "/assets",
            StaticFiles(directory=assets_dir),
            name="assets",
        )

    index_file = dist_dir / "index.html"

    @application.get("/", include_in_schema=False)
    def spa_root() -> FileResponse:
        return FileResponse(index_file)

    @application.get("/{full_path:path}", include_in_schema=False, response_model=None)
    def spa_catch_all(full_path: str) -> JSONResponse | FileResponse:
        # Defensive: never serve the SPA for an /api path.
        if full_path == "api" or full_path.startswith("api/"):
            return JSONResponse(status_code=404, content=_NOT_FOUND)
        return FileResponse(index_file)


def create_app(
    workspace_session_factory=None,
    rate_limiter=None,
    workspace_rate_limiter=None,
    max_request_bytes=None,
) -> FastAPI:
    """Build and return a configured FastAPI application instance.

    Returns:
        A :class:`fastapi.FastAPI` instance with the API router registered and,
        when ``frontend/dist`` exists, the built SPA served at the same origin.
    """
    application = FastAPI(title=APP_TITLE, version=APP_VERSION)

    # Middleware is added inner-to-outer; on the request path it runs in REVERSE
    # add order. Target request-path order:
    #   CSRF -> IP rate limit (stage 1) -> workspace identity
    #        -> per-workspace rate limit (stage 2) -> route
    #
    # Stage 2 (per-workspace) is added FIRST so it runs LAST (after identity is
    # resolved and attached to request.state).
    application.add_middleware(
        WorkspaceRateLimitMiddleware, limiter=workspace_rate_limiter
    )

    # Resolve/mint the trusted workspace identity from the httpOnly cookie.
    application.add_middleware(
        WorkspaceMiddleware, session_factory=workspace_session_factory
    )

    # Stage 1: IP-keyed rate limit + payload-size gate. Runs OUTER to identity,
    # so anonymous floods and oversized requests are rejected (429/413) BEFORE
    # any workspace minting or body buffering; needs no DB, so a DB identity
    # failure cannot disable it.
    application.add_middleware(RateLimitMiddleware, limiter=rate_limiter)

    # CSRF/Origin protection.
    application.add_middleware(CSRFMiddleware)

    # Aggregate request-body byte cap at the ASGI receive layer. Added LAST so
    # it is the OUTERMOST middleware and bounds ACTUAL received bytes before any
    # buffering/parsing/CSRF — even with missing/false/chunked Content-Length.
    application.add_middleware(MaxBodySizeMiddleware, max_bytes=max_request_bytes)

    # Register the API first so real API routes always win over the SPA.
    application.include_router(_build_api_router())
    from app.routers.screenshots import build_screenshots_router
    application.include_router(build_screenshots_router())
    from app.routers.consent import build_consent_router
    application.include_router(build_consent_router())
    from app.routers.operations import build_operations_router
    application.include_router(build_operations_router())
    from app.routers.classification import build_classification_router
    application.include_router(build_classification_router())
    from app.routers.study_selection import build_study_selection_router
    application.include_router(build_study_selection_router())

    # Explicit guard: any unmatched /api path returns a JSON API error, never
    # the SPA. Registered before the SPA catch-all so it takes precedence for
    # /api paths.
    @application.get("/api/{rest:path}", include_in_schema=False, response_model=None)
    def unknown_api_route(rest: str) -> JSONResponse:
        return JSONResponse(status_code=404, content=_NOT_FOUND)

    # Serve the SPA only if the build output exists. Backend-only dev, CI, and
    # pytest run fine without it.
    if _FRONTEND_DIST.is_dir() and (_FRONTEND_DIST / "index.html").is_file():
        _mount_spa(application, _FRONTEND_DIST)

    return application


# Module-level ASGI application for `app.main:app`.
app = create_app()
