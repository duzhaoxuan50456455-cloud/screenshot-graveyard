"""PostgreSQL-specific schema test (Task 1.1) — GATED / OPT-IN / ISOLATED.

Runs ONLY when BOTH are explicitly set:
  * TEST_POSTGRES_URL          — a real PostgreSQL URL, AND
  * PG_TEST_DB_IS_DISPOSABLE=1 — operator affirmation that the target database is
                                 a disposable test database safe for DDL.

If either is missing the test is SKIPPED, so it never silently "passes" and never
runs destructive DDL unless the operator has explicitly designated a throwaway DB.
A URL keyword heuristic is an ADDITIONAL defense, not the primary guarantee: even
with the disposable flag, a production-looking URL still fails fast unless
ALLOW_PG_TEST_ON_RISKY_URL=1 is also set.

Isolation strategy (no mutation of shared global metadata):
  * All DDL happens inside a uniquely-named, freshly-created schema.
  * The tables are reflected into a SEPARATE, disposable MetaData bound to that
    schema via `Table.to_metadata(..., schema=...)`. The global `Base.metadata`
    is never mutated, so foreign-key resolution and all other tests are
    unaffected even if this test fails midway.

As of Task 1.1 this is unverified in our environment (no local Postgres/driver),
so the test SKIPs until the two env vars are provided.
"""

from __future__ import annotations

import os
import secrets

import pytest
from sqlalchemy import MetaData, create_engine, inspect, text

from app.db.base import Base
from app.db import models  # noqa: F401

from app.db.pg_test_safety import is_disposable_opt_in, looks_like_production

POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL")
ALLOW_RISKY = os.environ.get("ALLOW_PG_TEST_ON_RISKY_URL") == "1"

EXPECTED_TABLES = {
    "workspace", "content_blob", "screenshot", "artifact",
    "artifact_version", "source_reference", "consent_record", "operation",
}

pytestmark = pytest.mark.skipif(
    not (POSTGRES_URL and is_disposable_opt_in(os.environ)),
    reason=(
        "PostgreSQL schema test skipped: requires TEST_POSTGRES_URL and "
        "PG_TEST_DB_IS_DISPOSABLE=1 (explicit disposable-DB opt-in)."
    ),
)


def _isolated_metadata(schema: str) -> MetaData:
    """Reflect the app's tables into a fresh MetaData bound to `schema`.

    The global `Base.metadata` is never touched; we copy each Table into a new
    MetaData with the target schema, so FK references resolve within the copy.
    """
    target = MetaData(schema=schema)
    for table in Base.metadata.tables.values():
        table.to_metadata(target, schema=schema)
    return target


def test_schema_creates_on_isolated_postgres_schema():
    assert POSTGRES_URL is not None

    if looks_like_production(POSTGRES_URL) and not ALLOW_RISKY:
        pytest.fail(
            "TEST_POSTGRES_URL looks like a production/shared database. Refusing "
            "to run DDL. Use a disposable local/CI Postgres, or set "
            "ALLOW_PG_TEST_ON_RISKY_URL=1 only if you are certain it is safe."
        )

    schema = f"sg_test_{secrets.token_hex(6)}"
    md = _isolated_metadata(schema)
    eng = create_engine(POSTGRES_URL)
    try:
        with eng.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        md.create_all(eng)
        tables = set(inspect(eng).get_table_names(schema=schema))
        assert EXPECTED_TABLES.issubset(tables), tables
    finally:
        with eng.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        eng.dispose()
