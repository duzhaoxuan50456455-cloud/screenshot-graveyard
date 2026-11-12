"""Task 2.9 stale-pending recovery and lifespan integration tests."""

from __future__ import annotations

import hashlib
import io
import asyncio
from datetime import datetime, timedelta, timezone
from threading import Event, Thread

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from app import config
import app.db.session as session_module
from app.db import models
from app.db.base import Base
from app.main import create_app
import app.main as main_module
from app.services.operation_recovery_service import OperationRecoveryService
from app.services.operation_service import OperationService
from app.services.vault_service import VaultService
from app.storage.file_store import FileStore


NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)


@pytest.fixture()
def factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'recovery.db'}",
        connect_args={"check_same_thread": False, "timeout": 10},
        poolclass=NullPool,
    )
    Base.metadata.create_all(engine)
    result = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, class_=Session)
    with result() as session:
        a = models.Workspace(token_hash="a" * 64)
        b = models.Workspace(token_hash="b" * 64)
        session.add_all([a, b])
        session.commit()
    return result


def _operation(factory, workspace_id: int, status: str = "pending", age_seconds: int = 0) -> int:
    with factory() as session:
        operation = OperationService(session, workspace_id).create_pending("classify")
        if status == "successful":
            OperationService(session, workspace_id).complete_after_persistence(
                operation.operation_id, lambda: None
            )
        elif status != "pending":
            OperationService(session, workspace_id).set_terminal_status(operation.operation_id, status)
        if age_seconds:
            row = session.get(models.Operation, operation.operation_id)
            row.updated_at = NOW - timedelta(seconds=age_seconds)
            session.commit()
        return operation.operation_id


def _workspace_ids(factory) -> tuple[int, int]:
    with factory() as session:
        return tuple(session.scalars(select(models.Workspace.id).order_by(models.Workspace.id)))


def _status(factory, operation_id: int) -> tuple[str, str | None]:
    with factory() as session:
        row = session.get(models.Operation, operation_id)
        return row.status, row.error_detail


def test_recovery_marks_only_strictly_stale_pending_and_is_idempotent(factory):
    workspace_a, workspace_b = _workspace_ids(factory)
    stale = _operation(factory, workspace_a, age_seconds=61)
    boundary = _operation(factory, workspace_a, age_seconds=60)
    fresh = _operation(factory, workspace_b, age_seconds=1)
    successful = _operation(factory, workspace_b, status="successful", age_seconds=999)
    failed = _operation(factory, workspace_b, status="failed", age_seconds=999)
    uncertain = _operation(factory, workspace_b, status="uncertain", age_seconds=999)

    recovery = OperationRecoveryService(
        factory, stale_after_seconds=60, now=lambda: NOW
    )
    assert recovery.sweep().recovered_count == 1
    assert _status(factory, stale) == ("failed", "Processing interrupted; please retry")
    assert _status(factory, boundary)[0] == "pending"  # strict older-than cutoff
    assert _status(factory, fresh)[0] == "pending"
    assert _status(factory, successful)[0] == "successful"
    assert _status(factory, failed)[0] == "failed"
    assert _status(factory, uncertain)[0] == "uncertain"
    assert recovery.sweep().recovered_count == 0


def test_recovery_database_error_leaves_operation_pending(factory, monkeypatch):
    workspace_a, _ = _workspace_ids(factory)
    pending = _operation(factory, workspace_a, age_seconds=61)

    def unavailable(*_args, **_kwargs):
        raise SQLAlchemyError("database unavailable")

    monkeypatch.setattr(OperationService, "fail_stale_pending", unavailable)
    result = OperationRecoveryService(factory, stale_after_seconds=60, now=lambda: NOW).sweep()
    assert result.recovered_count == 0
    assert result.unavailable_workspace_count >= 1
    assert _status(factory, pending)[0] == "pending"


def test_adapter_boundary_heartbeat_keeps_active_pending_operation_fresh(factory):
    workspace_a, _ = _workspace_ids(factory)
    pending = _operation(factory, workspace_a, age_seconds=61)
    with factory() as session:
        OperationService(session, workspace_a).heartbeat_pending(pending, now=NOW)
    result = OperationRecoveryService(factory, stale_after_seconds=60, now=lambda: NOW).sweep()
    assert result.recovered_count == 0
    assert _status(factory, pending)[0] == "pending"


def test_periodic_loop_survives_one_unexpected_sweep_failure(monkeypatch):
    """A broken interval does not silently stop all later recovery attempts."""
    stop = asyncio.Event()
    sweeps: list[str] = []
    waits = 0

    class FlakyRecovery:
        def __init__(self, *_args, **_kwargs):
            pass

        def sweep(self):
            sweeps.append("sweep")
            if len(sweeps) == 1:
                raise RuntimeError("injected failure")

    async def immediate_timeout(awaitable, *_args, **_kwargs):
        nonlocal waits
        # Our injected wait never awaits Event.wait(); close its coroutine so
        # the deterministic timeout simulation does not leak it.
        awaitable.close()
        waits += 1
        if waits == 2:
            stop.set()
        raise TimeoutError

    monkeypatch.setattr(main_module, "OperationRecoveryService", FlakyRecovery)
    monkeypatch.setattr(main_module.asyncio, "wait_for", immediate_timeout)
    asyncio.run(
        main_module._recovery_loop(
            stop, lambda: None, stale_after_seconds=60, interval_seconds=60
        )
    )
    assert sweeps == ["sweep", "sweep"]


def test_lifespan_creates_one_periodic_task_and_awaits_it_on_shutdown(factory, monkeypatch):
    created = []
    real_create_task = asyncio.create_task

    def track_task(coro):
        task = real_create_task(coro)
        created.append(task)
        return task

    monkeypatch.setattr(main_module.asyncio, "create_task", track_task)
    app = create_app(
        workspace_session_factory=factory,
        operation_recovery_stale_after_seconds=60,
        operation_recovery_interval_seconds=60,
    )
    with TestClient(app):
        assert len(created) == 1
        assert not created[0].done()
    assert created[0].done()
    assert created[0].cancelled()


def test_recovery_and_completion_do_not_overwrite_success(tmp_path):
    """The existing workspace lock serializes a completion ahead of a sweep."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'race.db'}",
        connect_args={"check_same_thread": False, "timeout": 10},
        poolclass=NullPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, class_=Session)
    with factory() as session:
        workspace = models.Workspace(token_hash="z" * 64)
        session.add(workspace)
        session.commit()
        workspace_id = workspace.id
    operation_id = _operation(factory, workspace_id, age_seconds=61)
    errors: list[Exception] = []
    persistence_entered = Event()
    allow_commit = Event()

    def complete() -> None:
        try:
            with factory() as session:
                OperationService(session, workspace_id).complete_after_persistence(
                    operation_id,
                    lambda: (persistence_entered.set(), allow_commit.wait(timeout=5)),
                )
        except Exception as exc:
            errors.append(exc)

    def recover() -> None:
        try:
            OperationRecoveryService(factory, stale_after_seconds=60, now=lambda: NOW).sweep()
        except Exception as exc:
            errors.append(exc)

    completion = Thread(target=complete)
    completion.start()
    assert persistence_entered.wait(timeout=5)
    recovery = Thread(target=recover)
    recovery.start()
    allow_commit.set()
    completion.join(timeout=15)
    recovery.join(timeout=15)
    assert not completion.is_alive() and not recovery.is_alive()
    assert not errors
    # The recovery was blocked until the terminal commit; it must then observe
    # and preserve successful rather than overwrite it as stale.
    assert _status(factory, operation_id)[0] == "successful"


def test_startup_sweep_recovers_and_polling_observes_safe_failed_state(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'startup.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, class_=Session)
    monkeypatch.setattr(session_module, "engine", engine)
    monkeypatch.setattr(session_module, "SessionLocal", factory)
    token = "startup-workspace-token"
    with factory() as session:
        workspace = models.Workspace(token_hash=hashlib.sha256(token.encode()).hexdigest())
        session.add(workspace)
        session.commit()
        workspace_id = workspace.id
    operation_id = _operation(factory, workspace_id, age_seconds=301)
    monkeypatch.setattr(config, "COOKIE_SECURE", False)
    app = create_app(
        workspace_session_factory=factory,
        operation_recovery_stale_after_seconds=300,
        operation_recovery_interval_seconds=60,
        operation_recovery_now=lambda: NOW,
    )
    with TestClient(app) as client:
        client.cookies.set(config.WORKSPACE_COOKIE_NAME, token)
        poll = client.get(f"/api/operations/{operation_id}")
        assert poll.status_code == 200
        assert poll.json()["status"] == "failed"
        assert poll.json()["errorDetail"] == "Processing interrupted; please retry"


def _png() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (8, 8), (1, 2, 3)).save(output, format="PNG")
    return output.getvalue()


def test_recovery_never_creates_artifacts_or_changes_existing_versions(factory, tmp_path):
    workspace_a, _ = _workspace_ids(factory)
    stale = _operation(factory, workspace_a, age_seconds=61)
    store = FileStore(tmp_path / "storage")
    with factory() as session:
        vault = VaultService(session, workspace_a, file_store=store)
        screenshot_id = vault.upload(_png(), filename="source.png").screenshot_id
        original_bytes = vault.read_original(screenshot_id).data
        artifact = models.Artifact(workspace_id=workspace_a, kind="study_note")
        session.add(artifact)
        session.flush()
        version = models.ArtifactVersion(
            workspace_id=workspace_a,
            artifact_id=artifact.id,
            version_number=1,
            origin="ai_generate",
            content_json='{"topic":"retained"}',
        )
        session.add(version)
        session.flush()
        artifact.current_version_id = version.id
        session.commit()
        artifact_id, version_id, original_json = artifact.id, version.id, version.content_json
    OperationRecoveryService(factory, stale_after_seconds=60, now=lambda: NOW).sweep()
    with factory() as session:
        assert session.get(models.Operation, stale).status == "failed"
        assert session.get(models.Artifact, artifact_id).current_version_id == version_id
        assert session.get(models.ArtifactVersion, version_id).content_json == original_json
        assert VaultService(session, workspace_a, file_store=store).read_original(
            screenshot_id
        ).data == original_bytes
