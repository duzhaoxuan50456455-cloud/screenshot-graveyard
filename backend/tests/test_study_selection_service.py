"""Task 2.6 unit tests for the read-only manual selection policy."""

from __future__ import annotations

import io

import pytest
from PIL import Image
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import models
from app.db.base import Base
from app.services.study_selection_service import (
    StudySelectionNotFoundError,
    StudySelectionService,
    StudySelectionValidationError,
)
from app.services.vault_service import VaultService
from app.storage.file_store import FileStore


def png(color=(3, 6, 9)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (10, 10), color).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture()
def ctx(tmp_path):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    store = FileStore(tmp_path / "storage")
    with Session(engine) as session:
        a = models.Workspace(token_hash="a" * 64)
        b = models.Workspace(token_hash="b" * 64)
        session.add_all((a, b))
        session.commit()
        yield session, store, a.id, b.id


def _upload(session, store, workspace_id, color):
    return VaultService(session, workspace_id, file_store=store).upload(
        png(color), filename="selection.png"
    ).screenshot_id


@pytest.mark.parametrize(
    ("count", "expected_warning"),
    ((1, True), (3, True), (4, False), (8, False), (9, True), (12, True)),
)
def test_count_policy_accepts_all_valid_counts_and_warns_only_outside_recommendation(
    ctx, count, expected_warning
):
    session, store, workspace_a, _ = ctx
    ids = [_upload(session, store, workspace_a, (i, i + 1, i + 2)) for i in range(count)]

    selected = StudySelectionService(session, workspace_a).validate_selection(ids)

    assert selected.screenshot_ids == tuple(ids)
    assert (selected.warning is not None) is expected_warning


@pytest.mark.parametrize("ids", ([], list(range(13))))
def test_count_policy_rejects_zero_and_more_than_twelve_with_descriptive_error(ctx, ids):
    session, _, workspace_a, _ = ctx
    with pytest.raises(StudySelectionValidationError, match="1 and 12"):
        StudySelectionService(session, workspace_a).validate_selection(ids)


def test_selection_preserves_order_without_reading_or_changing_original_bytes(ctx, monkeypatch):
    session, store, workspace_a, _ = ctx
    ids = [_upload(session, store, workspace_a, (i, i, i)) for i in range(3)]
    shot = session.scalar(select(models.Screenshot).where(models.Screenshot.id == ids[0]))
    assert shot is not None
    blob = session.get(models.ContentBlob, shot.content_blob_id)
    assert blob is not None
    before = store.read(blob.storage_path, expected_sha256=blob.sha256)
    read_original = store.read

    def original_must_not_be_read(*_args, **_kwargs):
        raise AssertionError("selection validation must not read original bytes")

    monkeypatch.setattr(store, "read", original_must_not_be_read)

    selected = StudySelectionService(session, workspace_a).validate_selection(
        [ids[2], ids[0], ids[1]]
    )

    assert selected.screenshot_ids == (ids[2], ids[0], ids[1])
    assert read_original(blob.storage_path, expected_sha256=blob.sha256) == before


def test_missing_or_foreign_id_fails_without_disclosing_ownership(ctx):
    session, store, workspace_a, workspace_b = ctx
    foreign_id = _upload(session, store, workspace_a, (1, 2, 3))

    selection_b = StudySelectionService(session, workspace_b)
    with pytest.raises(StudySelectionNotFoundError):
        selection_b.validate_selection([foreign_id])
    with pytest.raises(StudySelectionNotFoundError):
        selection_b.validate_selection([987654])


def test_duplicate_ids_are_rejected_instead_of_silently_changing_user_selection(ctx):
    """The specification is silent on duplicates; reject rather than de-duplicate."""
    session, store, workspace_a, _ = ctx
    screenshot_id = _upload(session, store, workspace_a, (1, 2, 3))
    with pytest.raises(StudySelectionValidationError, match="only once"):
        StudySelectionService(session, workspace_a).validate_selection([screenshot_id, screenshot_id])
