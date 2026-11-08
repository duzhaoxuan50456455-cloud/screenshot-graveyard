"""Application configuration, sourced from the environment (no hardcoded secrets).

`DATABASE_URL` selects the relational store. In local development it defaults to
a SQLite file; in production it is set to a managed PostgreSQL URL via the
environment. `STORAGE_ROOT` is the private file-volume path for original bytes.
Nothing here contains credentials; secrets are provided only via the environment.
"""

from __future__ import annotations

import math
import os
from pathlib import Path

# Repo root: backend/app/config.py -> parents[2] is the project root.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# Default to a local SQLite file for dev; override with a Postgres URL in prod
# by setting DATABASE_URL in the environment (e.g. postgresql+psycopg://...).
DATABASE_URL: str = os.environ.get(
    "DATABASE_URL",
    f"sqlite:///{(_REPO_ROOT / 'backend' / 'local.db').as_posix()}",
)

# Private file volume for original image bytes (never web-served). Overridable.
STORAGE_ROOT: str = os.environ.get(
    "STORAGE_ROOT",
    (_REPO_ROOT / "backend" / "var" / "storage").as_posix(),
)


def is_sqlite(url: str | None = None) -> bool:
    """Return True if the given (or configured) database URL targets SQLite."""
    return (url or DATABASE_URL).startswith("sqlite")


# ---- Workspace identity cookie (Task 1.3) ----
# Name of the cookie carrying the raw Workspace Identifier token.
WORKSPACE_COOKIE_NAME: str = os.environ.get("WORKSPACE_COOKIE_NAME", "sg_workspace")

# `Secure` flag for the workspace cookie. Defaults to True (production, same-origin
# HTTPS per the architecture steering). Set COOKIE_SECURE=0 ONLY for local HTTP
# development, where a Secure cookie would not be returned by the browser.
COOKIE_SECURE: bool = os.environ.get("COOKIE_SECURE", "1") != "0"

# Cookie max-age in seconds (~180 days). The workspace is tied to this cookie;
# there is intentionally no token-bearing recovery URL (design CORRECTION 1).
WORKSPACE_COOKIE_MAX_AGE: int = int(
    os.environ.get("WORKSPACE_COOKIE_MAX_AGE", str(180 * 24 * 60 * 60))
)


# ---- CSRF / Origin protection (Task 1.4) ----
# APP_ENV selects development vs production behavior for origin configuration.
# In production, TRUSTED_ORIGINS MUST be set explicitly; the app refuses to fall
# back to the insecure localhost development defaults (fail fast at startup).
APP_ENV: str = os.environ.get("APP_ENV", "development").strip().lower()

# Dev-only default trusted origins (localhost same-origin). NEVER used in prod.
_DEV_DEFAULT_ORIGINS = (
    "http://localhost:5173,http://127.0.0.1:5173,"
    "http://localhost:8000,http://127.0.0.1:8000"
)


def _parse_origins(raw: str) -> tuple[str, ...]:
    # Normalize to lowercase scheme://host[:port], strip trailing slash. Exact
    # matching only — no wildcard, no subdomain/prefix matching.
    return tuple(o.strip().rstrip("/").lower() for o in raw.split(",") if o.strip())


def _resolve_trusted_origins() -> tuple[str, ...]:
    raw = os.environ.get("TRUSTED_ORIGINS")
    if raw is None or not raw.strip():
        if APP_ENV == "production":
            raise RuntimeError(
                "TRUSTED_ORIGINS must be set explicitly in production (APP_ENV="
                "production). Refusing to fall back to insecure localhost "
                "development defaults."
            )
        raw = _DEV_DEFAULT_ORIGINS
    origins = _parse_origins(raw)
    # Defense in depth: never allow a wildcard entry.
    if any(o == "*" or "*" in o for o in origins):
        raise RuntimeError("TRUSTED_ORIGINS must not contain a wildcard.")
    return origins


TRUSTED_ORIGINS: tuple[str, ...] = _resolve_trusted_origins()

# Required custom header on state-changing requests. Browsers forbid setting
# custom headers on simple cross-site form submissions, so a cross-site attacker
# cannot forge it. Value is checked for presence only (any non-empty value).
CSRF_HEADER_NAME: str = os.environ.get("CSRF_HEADER_NAME", "X-Requested-With")


# ---- Rate limiting, size, and quota gates (Task 1.5) ----
# All in-process (no distributed limiter); values are env-overridable defaults,
# chosen conservatively for a solo-dev public hobby deployment. They are NOT
# shared across workers/instances (see RateLimiter docstring).

# Token-bucket rate limit per workspace: capacity (burst) and refill rate.
RATE_LIMIT_CAPACITY: int = int(os.environ.get("RATE_LIMIT_CAPACITY", "60"))
RATE_LIMIT_REFILL_PER_SEC: float = float(
    os.environ.get("RATE_LIMIT_REFILL_PER_SEC", "1.0")
)

# Maximum accepted request payload size in bytes (15 MB per file, Req 1.5 / A5).
# Applied as a request-body cap; per-file validation also enforces 15 MB later.
MAX_UPLOAD_BYTES: int = int(os.environ.get("MAX_UPLOAD_BYTES", str(15 * 1024 * 1024)))

# Maximum number of files accepted in a single upload batch.
MAX_FILES_PER_BATCH: int = int(os.environ.get("MAX_FILES_PER_BATCH", "20"))

# Aggregate maximum size of an ENTIRE request body (all multipart parts +
# envelope), distinct from the per-file 15 MB cap. Sized for a full batch:
# MAX_FILES_PER_BATCH * MAX_UPLOAD_BYTES plus a margin for multipart boundaries
# and headers. This bounds parsing/spooling even when Content-Length is absent,
# false, or the request is chunked. Enforced on ACTUAL bytes received.
_MULTIPART_OVERHEAD_MARGIN = 1 * 1024 * 1024  # 1 MB for boundaries/headers
MAX_REQUEST_BYTES: int = int(
    os.environ.get(
        "MAX_REQUEST_BYTES",
        str(MAX_FILES_PER_BATCH * MAX_UPLOAD_BYTES + _MULTIPART_OVERHEAD_MARGIN),
    )
)

# Per-workspace DB quota counters.
MAX_SCREENSHOTS_PER_WORKSPACE: int = int(
    os.environ.get("MAX_SCREENSHOTS_PER_WORKSPACE", "500")
)
MAX_AI_CALLS_PER_WORKSPACE: int = int(
    os.environ.get("MAX_AI_CALLS_PER_WORKSPACE", "1000")
)

def _positive_finite_float(raw: str, *, setting: str) -> float:
    """Parse a required-positive finite duration at startup.

    A zero, negative, NaN, or infinite adapter timeout would silently disable
    the bounded-call safety contract, so configuration fails fast instead.
    """
    try:
        value = float(raw)
    except ValueError as exc:
        raise RuntimeError(f"{setting} must be a positive finite number") from exc
    if not math.isfinite(value) or value <= 0:
        raise RuntimeError(f"{setting} must be a positive finite number")
    return value


# Per-call wall-clock timeout (seconds) for any AIAdapter implementation
# (design CORRECTION 4). On timeout the adapter raises AIAdapterTimeoutError and
# the calling service marks the operation failed/uncertain with sources intact.
AI_ADAPTER_TIMEOUT_SECONDS: float = _positive_finite_float(
    os.environ.get("AI_ADAPTER_TIMEOUT_SECONDS", "30"),
    setting="AI_ADAPTER_TIMEOUT_SECONDS",
)


# ---- Upload image validation (Task 1.7) ----
# Supported image formats (content-authoritative; A5 / Req 1.2-1.3). Declared
# filename extension and MIME type are HINTS only, never the sole reject reason.
SUPPORTED_IMAGE_FORMATS: tuple[str, ...] = ("PNG", "JPEG", "WEBP")

# Decompression-bomb guard (design CORRECTION 3): max single dimension and total
# megapixels. 40 MP comfortably exceeds legitimate screenshots while blocking bombs.
MAX_IMAGE_DIMENSION_PX: int = int(os.environ.get("MAX_IMAGE_DIMENSION_PX", "12000"))
MAX_IMAGE_MEGAPIXELS: float = float(os.environ.get("MAX_IMAGE_MEGAPIXELS", "40"))
