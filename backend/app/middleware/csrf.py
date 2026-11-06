"""CSRF / Origin protection middleware (Task 1.4).

Implements the approved three-layer defense for state-changing requests
(POST/PUT/PATCH/DELETE) to the API. Layer 1 (SameSite=Strict cookie) is set by
the Task 1.3 identity middleware; layers 2 and 3 are enforced here:

  Layer 2 — Origin/Referer check: the request's Origin (or Referer origin as a
            fallback) must exactly match one of the explicitly configured
            TRUSTED_ORIGINS. A state-changing request with NO Origin and NO
            Referer is REJECTED (fail closed — a missing Origin is never assumed
            trustworthy). As a complementary Fetch Metadata signal, an explicit
            `Sec-Fetch-Site: cross-site` (the only cross-site standard value)
            is also rejected.
  Layer 3 — Custom-header requirement: the request must carry a non-empty custom
            header (default `X-Requested-With`). Browsers forbid setting custom
            headers on simple cross-site form submissions, so a cross-site
            attacker cannot forge it.

This middleware runs BEFORE the workspace identity middleware on the request
path (it is added last / outermost in the app factory), so a rejected request
never reaches workspace minting or any database mutation. Violations return a
generic 403 that discloses no workspace token, id, or other sensitive data.

Safe-method requests (GET/HEAD/OPTIONS) and non-API paths are not checked here.
Reverse-proxy note: this middleware trusts only the browser-set Origin/Referer/
Sec-Fetch-* headers; it does NOT trust X-Forwarded-* or Host for the security
decision, so a spoofed forwarded header cannot bypass it.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app import config

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})
_FORBIDDEN = {"error": {"code": "forbidden", "message": "Forbidden"}}


def _origin_of(url: str | None) -> str | None:
    """Return the scheme://host[:port] origin of a URL, or None if unparseable."""
    if not url:
        return None
    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        return None
    # Lowercase scheme+host for case-insensitive exact matching; no wildcard or
    # subdomain/prefix logic is ever applied.
    return f"{parts.scheme}://{parts.netloc}".rstrip("/").lower()


class CSRFMiddleware(BaseHTTPMiddleware):
    """Enforce Origin + custom-header CSRF defenses on state-changing API calls."""

    def __init__(self, app, trusted_origins=None, header_name: str | None = None):
        super().__init__(app)
        self._trusted = tuple(trusted_origins) if trusted_origins is not None else None
        self._header_name = header_name

    @property
    def trusted_origins(self) -> tuple[str, ...]:
        return self._trusted if self._trusted is not None else config.TRUSTED_ORIGINS

    @property
    def header_name(self) -> str:
        return self._header_name or config.CSRF_HEADER_NAME

    def _reject(self) -> JSONResponse:
        # Generic 403; no token/id/detail leaked.
        return JSONResponse(status_code=403, content=_FORBIDDEN)

    async def dispatch(self, request: Request, call_next):
        method = request.method.upper()
        path = request.url.path

        # Only guard state-changing API requests. Safe methods and non-API paths
        # pass through untouched.
        if method in _SAFE_METHODS or not path.startswith("/api/"):
            return await call_next(request)

        # --- Layer 3: required custom header (presence, any non-empty value) ---
        if not request.headers.get(self.header_name):
            return self._reject()

        # --- Fetch Metadata (complementary): reject explicit cross-site ---
        # Standard Sec-Fetch-Site values are: same-origin, same-site, cross-site,
        # none. Only "cross-site" indicates a cross-site initiator and is
        # rejected here. We do NOT key off any nonstandard value.
        if request.headers.get("sec-fetch-site") == "cross-site":
            return self._reject()

        # --- Layer 2: Origin / Referer must match a trusted origin ---
        origin = request.headers.get("origin")
        if origin is not None:
            candidate = _origin_of(origin)
        else:
            # Fall back to the Referer's origin when Origin is absent.
            candidate = _origin_of(request.headers.get("referer"))

        # Fail closed: a state-changing request with neither a usable Origin nor
        # Referer is rejected (a missing Origin is never assumed trustworthy).
        if candidate is None or candidate not in self.trusted_origins:
            return self._reject()

        return await call_next(request)
