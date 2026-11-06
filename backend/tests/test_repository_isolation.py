"""Workspace-scoped repository tests (Task 1.2).

These are REPOSITORY-LAYER tests: they verify that, GIVEN a trusted workspace_id,
the repository scopes every operation to that workspace and refuses cross-workspace
reads, writes, and foreign-key links. They use two independent synthetic
workspaces and a temporary in-memory SQLite database. No real screenshots or
private user data are used.

Scope boundary: these tests do NOT cover how workspace_id is resolved from a
cookie/session, nor CSRF/Origin/HTTP auth — those belong to Tasks 1.3/1.4. Passing
these tests does NOT by itself prove end-to-end user privacy.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db import models
from app.db.repository import WorkspaceRepository, CrossWorkspaceError


@pytest.fixture()
def session():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    with Session(eng) as s:
        yield s
    eng.dispose()


@pytest.fixture()
def two_workspaces(session):
    """Create two independent workspaces A and B; return (repo_a, repo_b, ids)."""
    ws_a = models.Workspace(token_hash="a" * 64)
    ws_b = models.Workspace(token_hash="b" * 64)
    session.add_all([ws_a, ws_b])
    session.commit()
    return (
        WorkspaceRepository(session, ws_a.id),
        WorkspaceRepository(session, ws_b.id),
        (ws_a.id, ws_b.id),
    )


# --------------------------- construction / scoping --------------------------

def test_repository_requires_workspace_id(session):
    with pytest.raises(ValueError):
        WorkspaceRepository(session, None)  # type: ignore[arg-type]


def test_create_stamps_bound_workspace(two_workspaces, session):
    repo_a, _, (ws_a_id, _) = two_workspaces
    blob = repo_a.create_content_blob(
        sha256="c" * 64, byte_size=10, mime_type="image/png", storage_path="p"
    )
    assert blob.workspace_id == ws_a_id


# ------------------------------ successful path ------------------------------

def test_operations_within_correct_workspace_succeed(two_workspaces):
    repo_a, _, _ = two_workspaces
    blob = repo_a.create_content_blob(
        sha256="d" * 64, byte_size=10, mime_type="image/png", storage_path="p"
    )
    shot = repo_a.create_screenshot(content_blob_id=blob.id, original_filename="x.png")
    art = repo_a.create_artifact(kind="classification")
    ver = repo_a.add_artifact_version(
        artifact_id=art.id, version_number=1, origin="ai_generate", content_json="{}"
    )
    repo_a.set_current_version(art.id, ver.id)
    ref = repo_a.add_source_reference(artifact_id=art.id, screenshot_id=shot.id)
    rec = repo_a.set_consent(screenshot_id=shot.id, granted=True)
    op = repo_a.create_operation(kind="classify", artifact_id=art.id)

    assert repo_a.get_screenshot(shot.id) is shot
    assert repo_a.get_artifact(art.id) is art
    assert [v.id for v in repo_a.list_artifact_versions(art.id)] == [ver.id]
    assert [r.id for r in repo_a.list_source_references(art.id)] == [ref.id]
    assert repo_a.get_consent(shot.id).granted is True
    assert repo_a.get_operation(op.id) is op


# --------------------------- cross-workspace reads ---------------------------

def test_cross_workspace_read_by_id_returns_none(two_workspaces):
    repo_a, repo_b, _ = two_workspaces
    blob = repo_a.create_content_blob(
        sha256="e" * 64, byte_size=1, mime_type="image/png", storage_path="p"
    )
    shot = repo_a.create_screenshot(content_blob_id=blob.id, original_filename="x.png")
    art = repo_a.create_artifact(kind="study_note")

    # B uses A's known object IDs -> must NOT see A's rows.
    assert repo_b.get_screenshot(shot.id) is None
    assert repo_b.get_content_blob(blob.id) is None
    assert repo_b.get_artifact(art.id) is None
    assert repo_b.list_screenshots() == []
    assert repo_b.list_artifacts() == []


def test_cross_workspace_list_versions_is_rejected(two_workspaces):
    repo_a, repo_b, _ = two_workspaces
    art = repo_a.create_artifact(kind="study_note")
    repo_a.add_artifact_version(
        artifact_id=art.id, version_number=1, origin="ai_generate", content_json="{}"
    )
    # B cannot enumerate A's artifact versions even with the known artifact id.
    with pytest.raises(CrossWorkspaceError):
        repo_b.list_artifact_versions(art.id)


# --------------------- cross-workspace updates / deletes ---------------------

def test_cross_workspace_state_change_rejected_and_no_mutation(two_workspaces, session):
    repo_a, repo_b, _ = two_workspaces
    blob = repo_a.create_content_blob(
        sha256="f" * 64, byte_size=1, mime_type="image/png", storage_path="p"
    )
    shot = repo_a.create_screenshot(content_blob_id=blob.id, original_filename="x.png")
    session.commit()

    with pytest.raises(CrossWorkspaceError):
        repo_b.set_screenshot_state(shot.id, "archived")

    # A's record is unchanged.
    session.rollback()
    refetched = repo_a.get_screenshot(shot.id)
    assert refetched.state == "active"


def test_cross_workspace_operation_status_change_rejected(two_workspaces):
    repo_a, repo_b, _ = two_workspaces
    op = repo_a.create_operation(kind="classify")
    with pytest.raises(CrossWorkspaceError):
        repo_b.set_operation_status(op.id, "failed")
    assert repo_a.get_operation(op.id).status == "pending"


# --------------------- cross-workspace foreign-key links ---------------------

def test_screenshot_cannot_link_foreign_content_blob(two_workspaces):
    repo_a, repo_b, _ = two_workspaces
    blob_a = repo_a.create_content_blob(
        sha256="1" * 64, byte_size=1, mime_type="image/png", storage_path="p"
    )
    # B tries to create a screenshot referencing A's blob.
    with pytest.raises(CrossWorkspaceError):
        repo_b.create_screenshot(content_blob_id=blob_a.id, original_filename="x.png")


def test_screenshot_cannot_mark_duplicate_of_foreign_screenshot(two_workspaces):
    repo_a, repo_b, _ = two_workspaces
    blob_a = repo_a.create_content_blob(
        sha256="2" * 64, byte_size=1, mime_type="image/png", storage_path="p"
    )
    shot_a = repo_a.create_screenshot(content_blob_id=blob_a.id, original_filename="x.png")
    blob_b = repo_b.create_content_blob(
        sha256="3" * 64, byte_size=1, mime_type="image/png", storage_path="p"
    )
    with pytest.raises(CrossWorkspaceError):
        repo_b.create_screenshot(
            content_blob_id=blob_b.id, original_filename="y.png",
            is_duplicate_of=shot_a.id,
        )


def test_artifact_version_cannot_link_foreign_artifact(two_workspaces):
    repo_a, repo_b, _ = two_workspaces
    art_a = repo_a.create_artifact(kind="classification")
    with pytest.raises(CrossWorkspaceError):
        repo_b.add_artifact_version(
            artifact_id=art_a.id, version_number=1,
            origin="ai_generate", content_json="{}",
        )


def test_source_reference_cannot_link_foreign_artifact_or_screenshot(two_workspaces):
    repo_a, repo_b, _ = two_workspaces
    art_a = repo_a.create_artifact(kind="study_note")
    blob_b = repo_b.create_content_blob(
        sha256="4" * 64, byte_size=1, mime_type="image/png", storage_path="p"
    )
    shot_b = repo_b.create_screenshot(content_blob_id=blob_b.id, original_filename="y.png")
    art_b = repo_b.create_artifact(kind="study_note")

    # B artifact cannot reference A artifact's... B can't even address art_a.
    with pytest.raises(CrossWorkspaceError):
        repo_b.add_source_reference(artifact_id=art_a.id, screenshot_id=shot_b.id)

    # A cannot reference B's screenshot via A's own artifact.
    blob_a = repo_a.create_content_blob(
        sha256="5" * 64, byte_size=1, mime_type="image/png", storage_path="p"
    )
    art_a2 = repo_a.create_artifact(kind="study_note")
    with pytest.raises(CrossWorkspaceError):
        repo_a.add_source_reference(artifact_id=art_a2.id, screenshot_id=shot_b.id)


def test_set_current_version_rejects_foreign_or_mismatched_version(two_workspaces):
    repo_a, repo_b, _ = two_workspaces
    art_a = repo_a.create_artifact(kind="classification")
    ver_a = repo_a.add_artifact_version(
        artifact_id=art_a.id, version_number=1, origin="ai_generate", content_json="{}"
    )
    # B cannot point its (nonexistent-to-it) artifact at A's version.
    with pytest.raises(CrossWorkspaceError):
        repo_b.set_current_version(art_a.id, ver_a.id)

    # A version belonging to a different artifact is rejected.
    art_a2 = repo_a.create_artifact(kind="classification")
    with pytest.raises(CrossWorkspaceError):
        repo_a.set_current_version(art_a2.id, ver_a.id)


def test_consent_cannot_target_foreign_screenshot(two_workspaces):
    repo_a, repo_b, _ = two_workspaces
    blob_a = repo_a.create_content_blob(
        sha256="6" * 64, byte_size=1, mime_type="image/png", storage_path="p"
    )
    shot_a = repo_a.create_screenshot(content_blob_id=blob_a.id, original_filename="x.png")
    with pytest.raises(CrossWorkspaceError):
        repo_b.set_consent(screenshot_id=shot_a.id, granted=True)


# --------------------------- error-leak safety ------------------------------

def test_cross_workspace_error_does_not_leak_private_data(two_workspaces):
    repo_a, repo_b, (ws_a_id, _) = two_workspaces
    blob_a = repo_a.create_content_blob(
        sha256="secretsha" + "7" * 55, byte_size=1,
        mime_type="image/png", storage_path="private/secret/path",
    )
    shot_a = repo_a.create_screenshot(
        content_blob_id=blob_a.id, original_filename="private-filename.png"
    )
    with pytest.raises(CrossWorkspaceError) as exc:
        repo_b.set_screenshot_state(shot_a.id, "archived")
    msg = str(exc.value)
    # The error must not leak the owner workspace id, filename, path, or sha.
    assert str(ws_a_id) not in msg
    assert "private-filename.png" not in msg
    assert "private/secret/path" not in msg
    assert "secretsha" not in msg
