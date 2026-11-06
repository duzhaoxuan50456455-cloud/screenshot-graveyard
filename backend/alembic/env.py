"""Alembic environment: targets the app's models and env-based DATABASE_URL.

The connection URL comes from `app.config.DATABASE_URL` (environment), never from
a hardcoded value in alembic.ini. `target_metadata` is the app's declarative
`Base.metadata`, with `render_as_batch` enabled for SQLite compatibility.
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# Make the backend package importable when alembic runs from backend/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config as app_config  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.db import models  # noqa: E402,F401  (registers mappers)

config = context.config


def _resolve_url() -> str:
    """Resolve the database URL with correct precedence (no hardcoded secret).

    1. A URL explicitly set on the Alembic config by the caller
       (e.g. tests via `cfg.set_main_option("sqlalchemy.url", ...)`).
    2. Otherwise the live `DATABASE_URL` environment variable.
    3. Otherwise the app's configured default (import-time constant).
    """
    import os

    caller_url = config.get_main_option("sqlalchemy.url")
    if caller_url:
        return caller_url
    return os.environ.get("DATABASE_URL", app_config.DATABASE_URL)


_DB_URL = _resolve_url()
# Make the resolved URL the effective one for Alembic (alembic.ini carries no
# real URL/secret).
config.set_main_option("sqlalchemy.url", _DB_URL)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _is_sqlite() -> bool:
    return _DB_URL.startswith("sqlite")


def run_migrations_offline() -> None:
    context.configure(
        url=_DB_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=_is_sqlite(),
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=_is_sqlite(),
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
