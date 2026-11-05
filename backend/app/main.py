"""FastAPI application factory for the Screenshot Graveyard backend.

This module exposes :func:`create_app`, an app factory that builds and returns
a configured :class:`fastapi.FastAPI` instance, plus a module-level ``app``
object so the server can be launched with ``app.main:app``.

The scaffold is intentionally minimal. The database layer, services, the
repository layer, the file store, and all feature endpoints are added by later
tasks and are out of scope here. The only route registered is the health
check (Task 0.3), which has no authentication and no database dependency.
"""

from __future__ import annotations

from fastapi import FastAPI

APP_TITLE = "Screenshot Graveyard API"
APP_VERSION = "0.1.0"


def create_app() -> FastAPI:
    """Build and return a configured FastAPI application instance.

    Returns:
        A :class:`fastapi.FastAPI` instance with the health route registered.
    """
    application = FastAPI(title=APP_TITLE, version=APP_VERSION)

    @application.get("/api/health")
    def health() -> dict[str, str]:
        """Liveness check. No auth, no database dependency."""
        return {"status": "ok"}

    return application


# Module-level ASGI application for `app.main:app`.
app = create_app()
