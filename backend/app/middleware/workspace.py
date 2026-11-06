"""Workspace identity middleware (Task 1.3).

On every request this middleware resolves the visitor's trusted `workspace_id`
from the httpOnly cookie and attaches it to `request.state.workspace_id`
(or None). For endpoints that establish identity, a new workspace is minted on
first visit and the cookie is set on the response with the approved flags.

Security properties enforced here:
* The authoritative workspace id comes ONLY from the cookie token, resolved by
  hash lookup server-side. A `workspace_id` in a path, query, or body is never
  consulted — `request.state.workspace_id` is the single source of truth.
* The raw token is written only to the Set-Cookie header (httpOnly), never to
  logs, the response body, or a URL.
* Invalid/tampered/unknown cookie tokens resolve to None (treated as "no
  workspace"); no distinction is surfaced to the client.

Minting is restricted to an explicit allowlist of identity-establishing paths
(currently `/api/workspace`). Other paths — including unknown `/api/*` routes
and the SPA/static routes — never trigger a workspace INSERT, so routing/404
behavior is unaffected and an unseeded database cannot turn an unrelated request
into a 500. Resolution is also defensive: a transient DB error degrades to "no
workspace" rather than failing the request.

This middleware does NOT implement CSRF/Origin defenses — those are Task 1.4.
"""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app import config
from app.db import session as session_module
from app.workspace_identity import create_workspace, resolve_workspace_id

# Exact API paths that establish identity and may MINT a new workspace on first
# visit. Everything else only resolves an existing cookie (never mints).
_MINT_PATHS = {"/api/workspace"}


class WorkspaceMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, session_factory: Callable[[], Session] | None = None):
        super().__init__(app)
        # Injectable for tests; defaults to the app's real session factory.
        self._session_factory = session_factory

    def _new_session(self) -> Session:
        if self._session_factory is not None:
            return self._session_factory()
        return session_module.get_session()

    async def dispatch(self, request: Request, call_next):
        token = request.cookies.get(config.WORKSPACE_COOKIE_NAME)
        may_mint = request.url.path in _MINT_PATHS

        workspace_id: int | None = None
        new_token: str | None = None
        # Distinguishes "trusted identity resolution failed (DB outage)" from
        # "no/invalid identity". Private or state-changing endpoints MUST fail
        # closed when this is True rather than proceeding as an anonymous
        # visitor (enforced per-endpoint in later tasks).
        identity_unavailable = False

        session = self._new_session()
        try:
            try:
                workspace_id = resolve_workspace_id(session, token)
                if workspace_id is None and may_mint:
                    workspace_id, new_token = create_workspace(session)
                    session.commit()
            except SQLAlchemyError:
                # A DB error during identity resolution is NOT the same as an
                # invalid credential. We do not 500 unrelated requests (health,
                # SPA, safe reads keep working), but we flag that identity could
                # not be trusted so sensitive endpoints can fail closed.
                session.rollback()
                workspace_id = None
                new_token = None
                identity_unavailable = True
        finally:
            session.close()

        # Single source of truth for downstream code; never from path/body.
        request.state.workspace_id = workspace_id
        request.state.identity_unavailable = identity_unavailable

        response: Response = await call_next(request)

        # Set the cookie only when a brand-new workspace was minted. The raw
        # token appears ONLY here (httpOnly Set-Cookie), never in a body/log.
        if new_token is not None:
            response.set_cookie(
                key=config.WORKSPACE_COOKIE_NAME,
                value=new_token,
                max_age=config.WORKSPACE_COOKIE_MAX_AGE,
                httponly=True,
                secure=config.COOKIE_SECURE,
                samesite="strict",
                path="/",
            )
        return response
