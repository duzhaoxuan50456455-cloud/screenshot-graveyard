"""Per-workspace quota counter tests (Task 1.5).

Covers the DB-backed quota counters (stored screenshots, AI calls) and their
boundary conditions, scoped per workspace. Synthetic data, in-memory DB.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db import models
from app.db.repository import WorkspaceRepository, QuotaExceededError


@pytest.fixture()
def repos():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    with Session(eng) as s:
        ws_a = models.Workspace(token_hash="a" * 64)
        ws_b = models.Workspace(token_hash="b" * 64)
        s.add_all([ws_a, ws_b])
        s.commit()
        yield WorkspaceRepository(s, ws_a.id), WorkspaceRepository(s, ws_b.id), s
    eng.dispose()


def _add_screenshot(repo, s, n):
    blob = repo.create_content_blob(
        sha256=f"{n:064d}", byte_size=1, mime_type="image/png", storage_path=f"p{n}"
    )
    return repo.create_screenshot(content_blob_id=blob.id, original_filename=f"x{n}.png")


def test_screenshot_count_scoped_per_workspace(repos):
    repo_a, repo_b, s = repos
    for i in range(3):
        _add_screenshot(repo_a, s, i)
    s.commit()
    assert repo_a.count_screenshots() == 3
    assert repo_b.count_screenshots() == 0  # isolated


def test_screenshot_quota_boundary(repos):
    repo_a, _, s = repos
    for i in range(2):
        _add_screenshot(repo_a, s, i)
    s.commit()
    # At limit 3, adding 1 (total 3) is allowed; adding 2 (total 4) exceeds.
    repo_a.ensure_screenshot_quota(limit=3, adding=1)  # ok
    with pytest.raises(QuotaExceededError) as exc:
        repo_a.ensure_screenshot_quota(limit=3, adding=2)
    assert exc.value.kind == "screenshot" and exc.value.limit == 3


def test_ai_operation_count_and_quota(repos):
    repo_a, _, s = repos
    repo_a.create_operation(kind="classify")
    repo_a.create_operation(kind="study_note")
    repo_a.create_operation(kind="restore")  # NOT an AI-metered kind
    s.commit()
    assert repo_a.count_ai_operations() == 2  # restore excluded
    repo_a.ensure_ai_quota(limit=3, adding=1)  # total would be 3 -> ok
    with pytest.raises(QuotaExceededError) as exc:
        repo_a.ensure_ai_quota(limit=2, adding=1)  # total 3 > 2
    assert exc.value.kind == "ai_call"


def test_quota_error_message_leaks_no_private_data(repos):
    repo_a, _, s = repos
    _add_screenshot(repo_a, s, 0)
    s.commit()
    with pytest.raises(QuotaExceededError) as exc:
        repo_a.ensure_screenshot_quota(limit=1, adding=1)
    msg = str(exc.value)
    assert "token" not in msg.lower()
    assert "p0" not in msg  # no storage_path leaked


def test_quota_check_is_count_then_insert_not_atomic(repos):
    """Documents a known limitation: ensure_*_quota COUNTS then the caller
    INSERTS, so two concurrent callers could both pass at the boundary and
    exceed the quota by a small margin (TOCTOU). The authoritative fix
    (transactional count+insert or a DB constraint) belongs to the upload/AI
    endpoint tasks. This test pins the current non-atomic contract so a future
    change is intentional.
    """
    repo_a, _, s = repos
    _add_screenshot(repo_a, s, 0)
    s.commit()
    # Two "concurrent" checks both see count==1 and both pass at limit==2...
    repo_a.ensure_screenshot_quota(limit=2, adding=1)
    repo_a.ensure_screenshot_quota(limit=2, adding=1)
    # ...demonstrating the check alone does not serialize inserts. (No insert is
    # performed here; this asserts the check is advisory, not a hard gate.)
    assert repo_a.count_screenshots() == 1
