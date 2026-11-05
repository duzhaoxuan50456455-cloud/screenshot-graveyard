"""Alembic migration test (Task 1.1).

Runs the real Alembic migration from zero against a temporary SQLite file and
asserts every expected table, UNIQUE constraint, and workspace_id index exists.
This exercises the migration path, not just the ORM metadata. SQLite only; the
Postgres migration path is validated separately when a real URL is provided.
"""

from __future__ import annotations

import os
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

BACKEND_DIR = Path(__file__).resolve().parent.parent
EXPECTED_TABLES = {
    "workspace", "content_blob", "screenshot", "artifact",
    "artifact_version", "source_reference", "consent_record", "operation",
}


def _alembic_config(db_url: str) -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


def test_migration_creates_full_schema_from_zero(tmp_path, monkeypatch):
    db_path = tmp_path / "migtest.db"
    db_url = f"sqlite:///{db_path.as_posix()}"
    # env.py reads app.config.DATABASE_URL; point it at the temp db.
    monkeypatch.setenv("DATABASE_URL", db_url)

    cfg = _alembic_config(db_url)
    command.upgrade(cfg, "head")

    insp = inspect(create_engine(db_url))
    tables = set(insp.get_table_names())
    assert EXPECTED_TABLES.issubset(tables)

    # UNIQUE constraints / indexes carried by the migration.
    uqs = {u["name"] for u in insp.get_unique_constraints("content_blob")}
    # SQLite may surface UNIQUE as a unique index; accept either representation.
    uq_idx = {i["name"] for i in insp.get_indexes("content_blob") if i.get("unique")}
    assert any("workspace" in n and "sha256" in n for n in (uqs | uq_idx))

    for table in EXPECTED_TABLES - {"workspace"}:
        flat = {c for i in insp.get_indexes(table) for c in i["column_names"]}
        assert "workspace_id" in flat, f"{table} missing workspace_id index after migration"

    # Downgrade back to base should remove the tables.
    command.downgrade(cfg, "base")
    insp2 = inspect(create_engine(db_url))
    assert not EXPECTED_TABLES.issubset(set(insp2.get_table_names()))
