"""Rate-limit + payload-size middleware (Task 1.5).

Two cheap, in-process protections applied to API requests, enforced BEFORE any
expensive processing, workspace minting, or body buffering:

  * Payload size gate — a CHEAP EARLY check only. If the `Content-Length`
    header exceeds MAX_UPLOAD_BYTES, the request is rejected with `413 Payload
    Too Large` WITHOUT reading the body. Content-Length is NOT authoritative:
    a request may omit it, send an invalid value, or use chunked transfer
    encoding, and thus pass this gate while carrying an oversized body. The
    AUTHORITATIVE byte-limit enforcement MUST be a streaming check that counts
    bytes as the body is read and aborts past MAX_UPLOAD_BYTES. That streaming
    enforcement belongs to the upload endpoint (approved Task 1.7/1.8, where the
    body is actually consumed) and is an OUTSTANDING REQUIREMENT tracked by the
    regression tests in test_limits_middleware.py. This middleware intentionally
    allows missing/invalid Content-Length through so non-upload API calls are
    not blocked; it never implies the body size has been validated.

  * Rate limit — a TWO-STAGE token-bucket design:
      Stage 1 (IP, this middleware) runs early/outer, keyed by the client IP.
        It needs no database, so anonymous floods are throttled BEFORE the
        workspace middleware mints anything. A DB identity-resolution failure
        can never disable this stage (it does not depend on identity).
      Stage 2 (per-workspace, WorkspaceRateLimitMiddleware) runs AFTER the
        workspace identity middleware, keyed strictly by the trusted
        server-resolved `request.state.workspace_id` (never a client-supplied
        id). It enforces the approved per-workspace token-bucket. When no
        workspace is resolved (anonymous / identity unavailable) it does NOT
        apply a per-workspace limit — Stage 1 already bounded that request —
        so identity failures never disable protection and there is no
        double-counting across stages (distinct limiter instances).
    Per-workspace DB quota counters (stored screenshots, AI calls) are enforced
    separately at the endpoints that create those rows.

Ordering: this middleware is added so it runs OUTER to the workspace identity
middleware (added after it in the factory), i.e. early on the request path, so
rejections happen before minting/DB work. It is INNER to CSRF so obviously
forged requests are rejected first.

Multi-worker note: the limiter is in-process and not shared across workers or
instances (see TokenBucketRateLimiter docstring).

Rejections return a generic JSON error and never mutate workspace data.
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app import config
from app.ratelimit import TokenBucketRateLimiter

_SAFE_PREFIX = "/api/"
_TOO_LARGE = {"error": {"code": "payload_too_large", "message": "Payload Too Large"}}
_RATE_LIMITED = {"error": {"code": "rate_limited", "message": "Too Many Requests"}}


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, limiter: TokenBucketRateLimiter | None = None):
        super().__init__(app)
        self._limiter = limiter or TokenBucketRateLimiter(
            capacity=config.RATE_LIMIT_CAPACITY,
            refill_per_sec=config.RATE_LIMIT_REFILL_PER_SEC,
        )

    @property
    def limiter(self) -> TokenBucketRateLimiter:
        return self._limiter

    def _client_key(self, request: Request) -> str:
        # Use the direct peer address only. We deliberately do NOT trust
        # X-Forwarded-For / X-Real-IP here, since those are client-settable and
        # could be spoofed to evade or poison the limiter. (A trusted reverse
        # proxy integration would require explicit, configured proxy handling.)
        client = request.client
        return client.host if client is not None else "unknown"

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if not path.startswith(_SAFE_PREFIX):
            return await call_next(request)

        # --- Aggregate request-body size gate (early Content-Length reject) ---
        # Compares the DECLARED Content-Length against the whole-request cap
        # (MAX_REQUEST_BYTES), NOT the per-file cap: a valid multi-file batch may
        # legitimately exceed the per-file 15 MB limit in total. This is only an
        # early optimization; the authoritative actual-byte aggregate limit is
        # enforced by MaxBodySizeMiddleware (handles missing/false/chunked CL),
        # and the per-file 15 MB cap is enforced per image by the validator.
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                declared = int(content_length)
            except ValueError:
                declared = None
            if declared is not None and declared > config.MAX_REQUEST_BYTES:
                return JSONResponse(status_code=413, content=_TOO_LARGE)

        # --- Rate limit (keyed by client IP; no DB dependency) ---
        if not self._limiter.allow(self._client_key(request)):
            return JSONResponse(status_code=429, content=_RATE_LIMITED)

        return await call_next(request)


class WorkspaceRateLimitMiddleware(BaseHTTPMiddleware):
    """Stage 2: per-workspace token-bucket keyed by trusted workspace_id.

    Added to the app BEFORE WorkspaceMiddleware so it runs AFTER it on the
    request path and can read `request.state.workspace_id`. The key is ALWAYS
    the server-resolved id; a client-supplied workspace id (path/query/body/
    header) is never consulted, so a caller cannot change identifiers to get a
    fresh bucket. Requests with no resolved workspace are not limited here
    (Stage 1 already applied), so identity failures do not disable protection.
    """

    def __init__(self, app, limiter: TokenBucketRateLimiter | None = None):
        super().__init__(app)
        self._limiter = limiter or TokenBucketRateLimiter(
            capacity=config.RATE_LIMIT_CAPACITY,
            refill_per_sec=config.RATE_LIMIT_REFILL_PER_SEC,
        )

    @property
    def limiter(self) -> TokenBucketRateLimiter:
        return self._limiter

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if not path.startswith(_SAFE_PREFIX):
            return await call_next(request)

        workspace_id = getattr(request.state, "workspace_id", None)
        if workspace_id is not None:
            # Namespace the key so it can never collide with Stage 1 IP keys.
            if not self._limiter.allow(f"ws:{workspace_id}"):
                return JSONResponse(status_code=429, content=_RATE_LIMITED)

        return await call_next(request)


class BatchTooLargeError(Exception):
    """Raised when an upload batch exceeds the max-files-per-batch cap.

    A request-shape limit (not a middleware) used by the upload router (later
    task) to reject oversized batches with a safe, descriptive error before
    storing anything.
    """

    def __init__(self, count: int, limit: int) -> None:
        self.count = count
        self.limit = limit
        super().__init__(f"too many files in one batch: {count} (max {limit})")


def enforce_max_files_per_batch(count: int, limit: int | None = None) -> None:
    """Raise BatchTooLargeError if `count` exceeds the configured batch cap."""
    cap = config.MAX_FILES_PER_BATCH if limit is None else limit
    if count > cap:
        raise BatchTooLargeError(count, cap)


class MaxBodySizeMiddleware:
    """Bound the ACTUAL number of request-body bytes received (ASGI level).

    Wraps the ASGI ``receive`` callable and counts bytes as they arrive. Once the
    running total exceeds MAX_REQUEST_BYTES, the middleware sends a 413 response
    itself and feeds the downstream app an ``http.disconnect`` so it stops
    reading — the app's own (late) response is then suppressed. This bounds
    parsing/spooling even when Content-Length is ABSENT, FALSE, or the request
    uses chunked transfer encoding, because the limit is on real received bytes.

    Smallest safe receive-layer guard: no custom multipart parser, no change to
    other request handling. Only ``http`` requests under the API prefix are
    guarded; everything else passes straight through.
    """

    def __init__(self, app, max_bytes: int | None = None) -> None:
        self.app = app
        self._max_bytes = max_bytes

    @property
    def max_bytes(self) -> int:
        return self._max_bytes if self._max_bytes is not None else config.MAX_REQUEST_BYTES

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or not scope.get("path", "").startswith(_SAFE_PREFIX):
            await self.app(scope, receive, send)
            return

        import json

        max_bytes = self.max_bytes
        state = {"received": 0, "exceeded": False, "we_responded": False}

        async def bounded_receive():
            # Once over the limit, tell the app the client disconnected so it
            # stops trying to read the (rejected) body.
            if state["exceeded"]:
                return {"type": "http.disconnect"}
            message = await receive()
            if message.get("type") == "http.request":
                state["received"] += len(message.get("body", b""))
                if state["received"] > max_bytes:
                    state["exceeded"] = True
                    if not state["we_responded"]:
                        state["we_responded"] = True
                        body = json.dumps(_TOO_LARGE).encode()
                        await send({
                            "type": "http.response.start",
                            "status": 413,
                            "headers": [(b"content-type", b"application/json")],
                        })
                        await send({"type": "http.response.body", "body": body})
                    return {"type": "http.disconnect"}
            return message

        async def guarded_send(message):
            # Suppress the app's response once we've sent our own 413.
            if state["we_responded"]:
                return
            await send(message)

        await self.app(scope, bounded_receive, guarded_send)

