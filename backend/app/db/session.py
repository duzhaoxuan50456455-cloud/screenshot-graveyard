"""Engine and session factory built from the environment-configured DATABASE_URL.

No credentials are embedded here; the URL comes from `app.config` (environment).
SQLite needs `check_same_thread=False` for test/dev use across threads; other
backends (PostgreSQL) use default connect args.
"""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app import config


def make_engine(url: str | None = None) -> Engine:
    database_url = url or config.DATABASE_URL
    connect_args = {}
    if database_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
    return create_engine(database_url, connect_args=connect_args, future=True)


engine: Engine = make_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, class_=Session)


def get_session() -> Session:
    """Return a new Session bound to the configured engine."""
    return SessionLocal()
