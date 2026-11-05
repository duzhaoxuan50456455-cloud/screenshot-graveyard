"""Unit tests for the PostgreSQL integration-test safety guards (Task 1.1).

These run WITHOUT PostgreSQL. They verify the guard logic and that reflecting
the schema into an isolated MetaData does not mutate the global Base.metadata.
"""

from __future__ import annotations

from sqlalchemy import MetaData

from app.db.base import Base
from app.db import models  # noqa: F401
from app.db.pg_test_safety import is_disposable_opt_in, looks_like_production


def test_disposable_opt_in_requires_exact_1():
    assert is_disposable_opt_in({"PG_TEST_DB_IS_DISPOSABLE": "1"}) is True
    # Anything other than exactly "1" is treated as NOT opted in (safe default).
    for val in ("0", "true", "yes", "", "2", "01"):
        assert is_disposable_opt_in({"PG_TEST_DB_IS_DISPOSABLE": val}) is False
    assert is_disposable_opt_in({}) is False  # absent -> safe default


def test_looks_like_production_flags_risky_urls():
    risky = [
        "postgresql+psycopg://u:p@my-prod-db/app",
        "postgresql://u:p@service.rds.amazonaws.com/app",
        "postgresql://u:p@ep.neon.tech/app",
        "postgresql://u:p@db.supabase.co/app",
        "postgresql://u:p@production-host/app",
    ]
    for url in risky:
        assert looks_like_production(url) is True, url


def test_looks_like_production_allows_local_disposable_urls():
    safe = [
        "postgresql+psycopg://postgres:postgres@localhost:5432/sg_test",
        "postgresql://postgres@127.0.0.1/throwaway",
    ]
    for url in safe:
        assert looks_like_production(url) is False, url


def test_reflecting_into_isolated_metadata_does_not_mutate_global():
    """to_metadata(schema=...) must leave Base.metadata tables' schema as None."""
    before = {t.name: t.schema for t in Base.metadata.tables.values()}
    assert set(before.values()) == {None}, before

    target = MetaData(schema="sg_test_isolated")
    for table in Base.metadata.tables.values():
        table.to_metadata(target, schema="sg_test_isolated")

    after = {t.name: t.schema for t in Base.metadata.tables.values()}
    assert after == before, f"global metadata mutated: {after}"
    # The isolated copy carries the target schema.
    assert all(t.schema == "sg_test_isolated" for t in target.tables.values())
