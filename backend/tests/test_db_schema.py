"""Schema + constraint tests for the ORM models (Task 1.1).

These tests run against an in-memory SQLite database (fast, no external service).
They assert table/index/constraint presence and that UNIQUE constraints reject
duplicates. PostgreSQL-specific behavior is covered separately and only when a
real Postgres URL is provided (see test_db_postgres.py); it is NOT exercised here.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db import models

EXPECTED_TABLES = {
    "workspace", "content_blob", "screenshot", "artifact",
    "artifact_version", "source_reference", "consent_record", "operation",
}
NON_WORKSPACE_TABLES = EXPECTED_TABLES - {"workspace"}


@pytest.fixture()
def engine():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


def _utc():
    return datetime.now(timezone.utc)


def _make_workspace(session: Session, token_hash: str = "h" * 64) -> models.Workspace:
    ws = models.Workspace(token_hash=token_hash, created_at=_utc())
    session.add(ws)
    session.commit()
    return ws


def test_all_tables_created(engine):
    tables = set(inspect(engine).get_table_names())
    assert EXPECTED_TABLES.issubset(tables)


def test_every_non_workspace_table_has_workspace_id_and_index(engine):
    insp = inspect(engine)
    for table in NON_WORKSPACE_TABLES:
        cols = {c["name"] for c in insp.get_columns(table)}
        assert "workspace_id" in cols, f"{table} missing workspace_id"
        indexed_cols = {
            tuple(ix["column_names"]) for ix in insp.get_indexes(table)
        }
        flat = {c for combo in indexed_cols for c in combo}
        assert "workspace_id" in flat, f"{table} has no index covering workspace_id"


def test_workspace_token_hash_unique(engine):
    with Session(engine) as s:
        _make_workspace(s, token_hash="a" * 64)
        s.add(models.Workspace(token_hash="a" * 64, created_at=_utc()))
        with pytest.raises(IntegrityError):
            s.commit()


def test_content_blob_unique_workspace_sha256(engine):
    with Session(engine) as s:
        ws = _make_workspace(s)
        common = dict(
            workspace_id=ws.id, sha256="b" * 64, byte_size=10,
            mime_type="image/png", storage_path="ab/cd/" + "b" * 64,
        )
        s.add(models.ContentBlob(**common))
        s.commit()
        s.add(models.ContentBlob(**common))
        with pytest.raises(IntegrityError):
            s.commit()


def test_artifact_version_unique_artifact_version_number(engine):
    with Session(engine) as s:
        ws = _make_workspace(s)
        art = models.Artifact(workspace_id=ws.id, kind="classification")
        s.add(art)
        s.commit()
        v1 = models.ArtifactVersion(
            workspace_id=ws.id, artifact_id=art.id, version_number=1,
            origin="ai_generate", content_json="{}",
        )
        s.add(v1)
        s.commit()
        s.add(models.ArtifactVersion(
            workspace_id=ws.id, artifact_id=art.id, version_number=1,
            origin="ai_redo", content_json="{}",
        ))
        with pytest.raises(IntegrityError):
            s.commit()


def test_source_reference_unique_artifact_screenshot(engine):
    with Session(engine) as s:
        ws = _make_workspace(s)
        blob = models.ContentBlob(
            workspace_id=ws.id, sha256="c" * 64, byte_size=1,
            mime_type="image/png", storage_path="p",
        )
        s.add(blob)
        s.commit()
        shot = models.Screenshot(
            workspace_id=ws.id, content_blob_id=blob.id,
            original_filename="x.png", state="active",
        )
        art = models.Artifact(workspace_id=ws.id, kind="study_note")
        s.add_all([shot, art])
        s.commit()
        ref = dict(workspace_id=ws.id, artifact_id=art.id, screenshot_id=shot.id)
        s.add(models.SourceReference(**ref))
        s.commit()
        s.add(models.SourceReference(**ref))
        with pytest.raises(IntegrityError):
            s.commit()


def test_consent_record_unique_workspace_screenshot(engine):
    with Session(engine) as s:
        ws = _make_workspace(s)
        blob = models.ContentBlob(
            workspace_id=ws.id, sha256="d" * 64, byte_size=1,
            mime_type="image/png", storage_path="p",
        )
        s.add(blob)
        s.commit()
        shot = models.Screenshot(
            workspace_id=ws.id, content_blob_id=blob.id,
            original_filename="x.png", state="active",
        )
        s.add(shot)
        s.commit()
        rec = dict(workspace_id=ws.id, screenshot_id=shot.id, granted=True, granted_at=_utc())
        s.add(models.ConsentRecord(**rec))
        s.commit()
        s.add(models.ConsentRecord(**rec))
        with pytest.raises(IntegrityError):
            s.commit()


def test_append_only_versions_coexist(engine):
    """Distinct version numbers for one artifact coexist (append-only, INV-5/6)."""
    with Session(engine) as s:
        ws = _make_workspace(s)
        art = models.Artifact(workspace_id=ws.id, kind="study_note")
        s.add(art)
        s.commit()
        for n in (1, 2, 3):
            s.add(models.ArtifactVersion(
                workspace_id=ws.id, artifact_id=art.id, version_number=n,
                origin="ai_generate" if n == 1 else "ai_redo", content_json="{}",
            ))
        s.commit()
        rows = s.query(models.ArtifactVersion).filter_by(artifact_id=art.id).all()
        assert sorted(r.version_number for r in rows) == [1, 2, 3]
