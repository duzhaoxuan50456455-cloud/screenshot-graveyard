"""Application configuration, sourced from the environment (no hardcoded secrets).

`DATABASE_URL` selects the relational store. In local development it defaults to
a SQLite file; in production it is set to a managed PostgreSQL URL via the
environment. `STORAGE_ROOT` is the private file-volume path for original bytes.
Nothing here contains credentials; secrets are provided only via the environment.
"""

from __future__ import annotations

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
