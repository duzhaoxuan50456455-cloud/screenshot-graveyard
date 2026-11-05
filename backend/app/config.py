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
