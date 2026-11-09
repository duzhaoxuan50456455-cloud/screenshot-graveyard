"""Task 2.4 operation-lifecycle tests using the real repository and SQLite."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from threading import Barrier, Lock, Thread

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool, StaticPool

from app.db import models
from app.db.base import Base
from app.db.repository import WorkspaceRepository
from app.services.operation_service import (
    OperationNotFoundError,
    OperationPersistenceError,
    OperationService,
    OperationServiceUnavailableError,
    OperationTransitionError,
)


@pytest.fixture()
def ctx():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        a = models.Workspace(token_hash="a" * 64)
        b = models.Workspace(token_hash="b" * 64)
        session.add_all([a, b])
        session.commit()
        yield session, a.id, b.id


def test_pending_operation_has_only_an_approved_status(ctx):
    session, workspace_a, _ = ctx
    state = OperationService(session, workspace_a).create_pending("classify")
    assert state.status == "pending"
    assert state.status in {"pending", "successful", "failed", "uncertain"}


def test_success_and_related_persistence_commit_together(ctx):
    session, workspace_a, _ = ctx
    service = OperationService(session, workspace_a)
    operation = service.create_pending("classify")

    def persist():
        WorkspaceRepository(session, workspace_a).create_artifact(kind="classification")

    state = service.complete_after_persistence(operation.operation_id, persist)
    assert state.status == "successful"
    assert session.scalar(select(models.Artifact).where(models.Artifact.workspace_id == workspace_a))
    assert service.get(operation.operation_id).status == "successful"


def test_persistence_failure_never_reports_success_and_rolls_back(ctx):
    session, workspace_a, _ = ctx
    service = OperationService(session, workspace_a)
    operation = service.create_pending("classify")

    def fail_after_writing():
        WorkspaceRepository(session, workspace_a).create_artifact(kind="classification")
        raise RuntimeError("simulated persistence failure")

    with pytest.raises(OperationPersistenceError):
        service.complete_after_persistence(operation.operation_id, fail_after_writing)

    assert session.scalars(
        select(models.Artifact).where(models.Artifact.workspace_id == workspace_a)
    ).all() == []
    failed = service.get(operation.operation_id)
    assert failed.status == "failed"
    assert failed.error_detail == "Processing failed"


def test_commit_failure_rolls_back_persistence_before_marking_failed(ctx, monkeypatch):
    """A failed commit cannot expose a successful operation (Property 22)."""
    session, workspace_a, _ = ctx
    service = OperationService(session, workspace_a)
    operation = service.create_pending("classify")
    original_commit = session.commit
    calls = 0

    def fail_success_commit_once():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise SQLAlchemyError("commit failed")
        return original_commit()

    monkeypatch.setattr(session, "commit", fail_success_commit_once)

    with pytest.raises(OperationPersistenceError):
        service.complete_after_persistence(
            operation.operation_id,
            lambda: WorkspaceRepository(session, workspace_a).create_artifact(
                kind="classification"
            ),
        )

    assert session.scalars(
        select(models.Artifact).where(models.Artifact.workspace_id == workspace_a)
    ).all() == []
    assert service.get(operation.operation_id).status == "failed"


def test_foreign_and_missing_operations_are_indistinguishable(ctx):
    session, workspace_a, workspace_b = ctx
    operation = OperationService(session, workspace_a).create_pending("classify")
    foreign = OperationService(session, workspace_b)
    with pytest.raises(OperationNotFoundError) as foreign_error:
        foreign.get(operation.operation_id)
    with pytest.raises(OperationNotFoundError) as missing_error:
        foreign.get(operation.operation_id + 9999)
    assert str(foreign_error.value) == str(missing_error.value)
    assert OperationService(session, workspace_a).get(operation.operation_id).status == "pending"


def test_database_error_does_not_create_an_operation(ctx, monkeypatch):
    session, workspace_a, _ = ctx

    def unavailable(self, **kwargs):
        raise SQLAlchemyError("database unavailable")

    monkeypatch.setattr(WorkspaceRepository, "create_operation", unavailable)
    with pytest.raises(OperationServiceUnavailableError):
        OperationService(session, workspace_a).create_pending("classify")
    assert session.scalars(select(models.Operation)).all() == []


def test_terminal_operation_cannot_be_transitioned_again(ctx):
    session, workspace_a, _ = ctx
    service = OperationService(session, workspace_a)
    operation = service.create_pending("classify")
    assert service.set_terminal_status(operation.operation_id, "failed").status == "failed"
    with pytest.raises(OperationTransitionError):
        service.set_terminal_status(operation.operation_id, "uncertain")


def test_completion_locks_workspace_before_persistence_callback(ctx, monkeypatch):
    """A duplicate worker is serialized before it can produce duplicate output."""
    session, workspace_a, _ = ctx
    service = OperationService(session, workspace_a)
    operation = service.create_pending("classify")
    calls = []
    original_lock = WorkspaceRepository.lock_workspace

    def track_lock(self):
        calls.append("locked")
        return original_lock(self)

    monkeypatch.setattr(WorkspaceRepository, "lock_workspace", track_lock)

    def persist():
        assert calls == ["locked"]
        WorkspaceRepository(session, workspace_a).create_artifact(kind="classification")

    assert service.complete_after_persistence(operation.operation_id, persist).status == "successful"
    assert calls == ["locked"]


def test_concurrent_completion_executes_exactly_one_persistence_callback(tmp_path):
    """Two workers for one operation cannot both persist output.

    SQLite can report a transient lock error to the loser while PostgreSQL
    serializes it as a terminal-state rejection.  Both are safe; the invariant
    here is that only the winning worker reaches its persistence callback.
    """
    engine = create_engine(
        f"sqlite:///{tmp_path / 'operations.sqlite'}",
        connect_args={"check_same_thread": False, "timeout": 5},
        poolclass=NullPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine) as setup:
        workspace = models.Workspace(token_hash="c" * 64)
        setup.add(workspace)
        setup.commit()
        workspace_id = workspace.id
        operation = OperationService(setup, workspace_id).create_pending("classify")

    barrier = Barrier(2)
    callback_calls: list[str] = []
    callback_lock = Lock()
    outcomes: list[str] = []

    def worker(name: str) -> None:
        with Session(engine) as session:
            service = OperationService(session, workspace_id)
            barrier.wait()
            try:
                def persist() -> None:
                    with callback_lock:
                        callback_calls.append(name)
                    WorkspaceRepository(session, workspace_id).create_artifact(
                        kind="classification"
                    )

                service.complete_after_persistence(operation.operation_id, persist)
                outcomes.append("successful")
            except OperationTransitionError:
                outcomes.append("already_terminal")
            except OperationServiceUnavailableError:
                outcomes.append("unavailable")

    first = Thread(target=worker, args=("first",))
    second = Thread(target=worker, args=("second",))
    first.start()
    second.start()
    first.join(timeout=10)
    second.join(timeout=10)
    assert not first.is_alive() and not second.is_alive()
    assert callback_calls in (["first"], ["second"])
    assert outcomes.count("successful") == 1
    assert len(outcomes) == 2

    with Session(engine) as verify:
        state = OperationService(verify, workspace_id).get(operation.operation_id)
        assert state.status == "successful"
        assert len(verify.scalars(select(models.Artifact)).all()) == 1


def test_repository_rejects_invalid_operation_statuses_and_transitions(ctx):
    session, workspace_a, _ = ctx
    repo = WorkspaceRepository(session, workspace_a)
    from app.db.repository import InvalidOperationStatusError, InvalidOperationTransitionError

    with pytest.raises(InvalidOperationStatusError):
        repo.create_operation(kind="classify", status="running")
    with pytest.raises(InvalidOperationTransitionError):
        repo.create_operation(kind="classify", status="successful")
    operation = repo.create_operation(kind="classify")
    repo.set_operation_status(operation.id, "failed")
    with pytest.raises(InvalidOperationTransitionError):
        repo.set_operation_status(operation.id, "successful")


def test_stale_pending_recovery_marks_only_pending_operation_failed(ctx):
    session, workspace_a, _ = ctx
    service = OperationService(session, workspace_a)
    operation = service.create_pending("classify")
    row = session.get(models.Operation, operation.operation_id)
    assert row is not None
    row.updated_at = datetime.now(timezone.utc) - timedelta(minutes=10)
    session.commit()

    assert service.fail_stale_pending(older_than_seconds=60) == 1
    recovered = service.get(operation.operation_id)
    assert recovered.status == "failed"
    assert recovered.error_detail == "Processing interrupted; please retry"
