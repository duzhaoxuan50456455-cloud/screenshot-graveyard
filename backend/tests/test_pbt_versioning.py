"""PBT-VERSIONING (Task 3.8).

The properties use actual Vault, consent, operation, study-note, version, and
comparison services against disposable SQLite databases.  Provider doubles are
strictly deterministic and network-free; they only make the real persistence
and provenance boundaries observable.
"""

from __future__ import annotations

import io
import json
import os
import threading
from types import SimpleNamespace

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st
from PIL import Image
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.ai.fake_adapter import FakeAIAdapter, FakeAIAdapterMode
from app.db import models
from app.db.base import Base
from app.services.analysis_service import AnalysisService
from app.services.artifact_comparison_service import (
    ArtifactComparisonNotFoundError,
    ArtifactComparisonService,
)
from app.services.classification_service import ClassificationService
from app.services.consent_service import ConsentService
from app.services.operation_service import OperationService
from app.services.study_note_artifact_service import StudyNoteArtifactService
from app.services.study_note_service import StudyNoteService
from app.services.vault_service import VaultService
from app.storage.file_store import FileStore


PBT = settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
_JOIN_TIMEOUT = 30


def _png(color: tuple[int, int, int]) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(out, format="PNG")
    return out.getvalue()


class _RecordingAdapter:
    def __init__(self, mode: FakeAIAdapterMode = FakeAIAdapterMode.NORMAL) -> None:
        self._fake, self.calls, self.classification_calls, self._lock = (
            FakeAIAdapter(mode), 0, 0, threading.Lock()
        )

    def study_note(self, images, **kwargs):
        with self._lock:
            self.calls += 1
        return self._fake.study_note(images, **kwargs)

    def classify_and_epitaph(self, image_bytes, mime, **kwargs):
        with self._lock:
            self.classification_calls += 1
        return self._fake.classify_and_epitaph(image_bytes, mime, **kwargs)


def _env(tmp_path_factory):
    root = tmp_path_factory.mktemp("pbt-versioning")
    engine = create_engine(
        f"sqlite:///{root / 'test.sqlite'}",
        connect_args={"check_same_thread": False, "timeout": 10},
        poolclass=NullPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        workspace = models.Workspace(token_hash="v" * 64)
        session.add(workspace)
        session.commit()
        workspace_id = workspace.id
    return SimpleNamespace(root=root, engine=engine, store=FileStore(root / "storage"), workspace_id=workspace_id)


def _close(env):
    for path in (env.root / "storage").rglob("*") if (env.root / "storage").exists() else ():
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass
    env.engine.dispose()


def _new_operation(env) -> int:
    with Session(env.engine) as session:
        return OperationService(session, env.workspace_id).create_pending("redo").operation_id


def _make_note(env, ordered_colors: list[int]) -> tuple[int, list[int]]:
    adapter = _RecordingAdapter()
    with Session(env.engine) as session:
        vault = VaultService(session, env.workspace_id, file_store=env.store)
        ids = [
            vault.upload(_png((color, 20, 80)), filename=f"{index}.png").screenshot_id
            for index, color in enumerate(ordered_colors)
        ]
        for screenshot_id in ids:
            ConsentService(session, env.workspace_id).record_consent(screenshot_id, True)
        operation_id = OperationService(session, env.workspace_id).create_pending("study_note").operation_id
        outcome = StudyNoteService(session, env.workspace_id, adapter=adapter, vault=vault).run_generation(ids, operation_id)
        assert outcome.operation.status == "successful" and outcome.artifact_id is not None
        return outcome.artifact_id, ids


def _make_analysis(env) -> tuple[int, int, int]:
    with Session(env.engine) as session:
        vault = VaultService(session, env.workspace_id, file_store=env.store)
        screenshot_id = vault.upload(_png((33, 44, 55)), filename="analysis.png").screenshot_id
        ConsentService(session, env.workspace_id).record_consent(screenshot_id, True)
        operation_id = OperationService(session, env.workspace_id).create_pending("classify").operation_id
        outcome = ClassificationService(session, env.workspace_id, adapter=_RecordingAdapter(), vault=vault).run_classification(screenshot_id, operation_id)
        assert outcome.operation.status == "successful"
        assert outcome.classification_artifact_id is not None and outcome.epitaph_artifact_id is not None
        return screenshot_id, outcome.classification_artifact_id, outcome.epitaph_artifact_id


def _redo(env, artifact_id: int, adapter: _RecordingAdapter, instruction: str):
    return _redo_with_operation(env, artifact_id, _new_operation(env), adapter, instruction)


def _redo_with_operation(
    env, artifact_id: int, operation_id: int, adapter: _RecordingAdapter, instruction: str
):
    with Session(env.engine) as session:
        vault = VaultService(session, env.workspace_id, file_store=env.store)
        return StudyNoteService(session, env.workspace_id, adapter=adapter, vault=vault).run_redo(
            artifact_id, operation_id, instruction
        )


def _edit(env, artifact_id: int, topic: str):
    content = {
        "topic": topic,
        "main_concepts": ["manual"],
        "extracted_code": [],
        "code_explanations": [],
        "pitfalls": [],
        "uncertain_content": [],
    }
    with Session(env.engine) as session:
        return StudyNoteArtifactService(session, env.workspace_id).save_edit(artifact_id, content)


def _redo_analysis(env, screenshot_id: int, adapter: _RecordingAdapter, instruction: str):
    with Session(env.engine) as session:
        vault = VaultService(session, env.workspace_id, file_store=env.store)
        return ClassificationService(session, env.workspace_id, adapter=adapter, vault=vault).run_redo(
            screenshot_id, _new_operation(env), instruction
        )


def _edit_analysis(env, screenshot_id: int, value: str):
    with Session(env.engine) as session:
        return AnalysisService(session, env.workspace_id).save_edit(
            screenshot_id,
            {"category": "Memory/Entertainment", "epitaph_text": f"manual-{value}"},
        )


def _versions(env, artifact_id: int):
    with Session(env.engine) as session:
        return [
            (row.id, row.version_number, row.origin, row.content_json)
            for row in session.scalars(
                select(models.ArtifactVersion)
                .where(models.ArtifactVersion.artifact_id == artifact_id)
                .order_by(models.ArtifactVersion.version_number)
            )
        ]


def _pointer(env, artifact_id: int) -> int:
    with Session(env.engine) as session:
        return session.get(models.Artifact, artifact_id).current_version_id


def _references(env, artifact_id: int) -> list[int]:
    with Session(env.engine) as session:
        return [ref.screenshot_id for ref in session.scalars(
            select(models.SourceReference)
            .where(models.SourceReference.artifact_id == artifact_id)
            .order_by(models.SourceReference.id)
        )]


def _foreign_version(env) -> tuple[int, int]:
    """A separately owned retained version for non-disclosure assertions."""
    with Session(env.engine) as session:
        workspace = models.Workspace(token_hash="f" * 64)
        session.add(workspace)
        session.flush()
        artifact = models.Artifact(workspace_id=workspace.id, kind="study_note")
        session.add(artifact)
        session.flush()
        version = models.ArtifactVersion(
            workspace_id=workspace.id,
            artifact_id=artifact.id,
            version_number=1,
            origin="ai_generate",
            content_json='{"topic":"foreign"}',
        )
        session.add(version)
        session.flush()
        artifact.current_version_id = version.id
        session.commit()
        return artifact.id, version.id


def _originals(env) -> list[tuple[bytes, str]]:
    with Session(env.engine) as session:
        rows = session.scalars(select(models.Screenshot).order_by(models.Screenshot.id)).all()
        return [
            ((env.store.root / session.get(models.ContentBlob, row.content_blob_id).storage_path).read_bytes(),
             session.get(models.ContentBlob, row.content_blob_id).sha256)
            for row in rows
        ]


def _assert_history(env, artifact_id: int, old: list[tuple[int, int, str, str]] | None = None):
    rows = _versions(env, artifact_id)
    assert [row[1] for row in rows] == list(range(1, len(rows) + 1))
    assert _pointer(env, artifact_id) == rows[-1][0]
    if old is not None:
        assert rows[:len(old)] == old
    return rows


def _parallel(*jobs):
    results, errors = [None] * len(jobs), []

    def worker(index, job):
        try:
            results[index] = job()
        except BaseException as exc:  # re-raised by the parent test thread
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i, job)) for i, job in enumerate(jobs)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(_JOIN_TIMEOUT)
    assert not any(thread.is_alive() for thread in threads), "concurrent version operation deadlocked"
    if errors:
        raise errors[0]
    return results


# Feature: screenshot-graveyard, Property 15: append-only retained versions.
@PBT
@given(
    actions=st.lists(st.sampled_from(("edit", "redo", "uncertain", "failed")), min_size=0, max_size=3),
    suffixes=st.lists(st.text(alphabet="abc123", min_size=1, max_size=12), min_size=5, max_size=5),
)
def test_p15_append_only_versions_and_concurrent_completion(tmp_path_factory, actions, suffixes):
    """Manual edits/Redos retain all history; concurrent completions both append."""
    env = _env(tmp_path_factory)
    try:
        artifact_id, _ = _make_note(env, [10])
        retained = _assert_history(env, artifact_id)
        for index, action in enumerate(actions):
            before = retained
            if action == "edit":
                _edit(env, artifact_id, f"manual-{suffixes[index]}")
            else:
                mode = {
                    "redo": FakeAIAdapterMode.NORMAL,
                    "uncertain": FakeAIAdapterMode.UNCERTAIN,
                    "failed": FakeAIAdapterMode.INVALID,
                }[action]
                adapter = _RecordingAdapter(mode)
                outcome = _redo(env, artifact_id, adapter, suffixes[index])
                assert outcome.operation.status == {
                    "redo": "successful", "uncertain": "uncertain", "failed": "failed"
                }[action]
                assert adapter.calls == 1
            retained = _assert_history(env, artifact_id, before)

        # Every generated example exercises BOTH required concurrent patterns.
        # The barrier releases two independent sessions together before either
        # service reaches its workspace-scoped transaction boundary.
        for redo_edit in (True, False):
            before = retained
            barrier = threading.Barrier(2, timeout=_JOIN_TIMEOUT)
            adapter = _RecordingAdapter()
            if redo_edit:
                redo_operation = _new_operation(env)
                jobs = (
                    lambda: (barrier.wait(), _redo_with_operation(env, artifact_id, redo_operation, adapter, suffixes[3]))[1],
                    lambda: (barrier.wait(), _edit(env, artifact_id, f"parallel-{suffixes[4]}"))[1],
                )
            else:
                first_operation, second_operation = _new_operation(env), _new_operation(env)
                jobs = (
                    lambda: (barrier.wait(), _redo_with_operation(env, artifact_id, first_operation, adapter, suffixes[3]))[1],
                    lambda: (barrier.wait(), _redo_with_operation(env, artifact_id, second_operation, adapter, suffixes[4]))[1],
                )
            outcomes = _parallel(*jobs)
            assert all(getattr(outcome, "operation", None) is None or outcome.operation.status in {"successful", "uncertain"} for outcome in outcomes)
            assert adapter.calls == (1 if redo_edit else 2)
            retained = _assert_history(env, artifact_id, before)
            assert len(retained) == len(before) + 2
    finally:
        _close(env)


# Feature: screenshot-graveyard, Property 15: append-only versions for paired classification artifacts.
@PBT
@given(
    actions=st.lists(st.sampled_from(("edit", "redo")), min_size=1, max_size=4),
    token=st.text(alphabet="abcdef012345", min_size=1, max_size=10),
)
def test_p15_classification_and_epitaph_versions_remain_append_only(tmp_path_factory, actions, token):
    """A classification edit/Redo advances both paired artifacts without overwriting either."""
    env = _env(tmp_path_factory)
    try:
        screenshot_id, classification_id, epitaph_id = _make_analysis(env)
        class_history, epitaph_history = _assert_history(env, classification_id), _assert_history(env, epitaph_id)
        for index, action in enumerate(actions):
            if action == "edit":
                _edit_analysis(env, screenshot_id, f"{token}-{index}")
            else:
                adapter = _RecordingAdapter()
                assert _redo_analysis(env, screenshot_id, adapter, f"classification-{token}-{index}").operation.status == "successful"
                assert adapter.classification_calls == 1
            class_history = _assert_history(env, classification_id, class_history)
            epitaph_history = _assert_history(env, epitaph_id, epitaph_history)
        assert _references(env, classification_id) == [screenshot_id]
        assert _references(env, epitaph_id) == [screenshot_id]
    finally:
        _close(env)


# Feature: screenshot-graveyard, Property 16: source provenance survives every version.
@PBT
@given(
    order=st.permutations((11, 42, 73)),
    actions=st.lists(st.sampled_from(("edit", "redo", "uncertain")), min_size=0, max_size=3),
)
def test_p16_source_references_survive_edit_and_redo(tmp_path_factory, order, actions):
    """Artifact-level sources preserve the original explicit order through every version."""
    env = _env(tmp_path_factory)
    try:
        artifact_id, expected_sources = _make_note(env, list(order))
        initial_bytes = _originals(env)
        _foreign_artifact_id, foreign_version_id = _foreign_version(env)
        for index, action in enumerate(actions):
            if action == "edit":
                _edit(env, artifact_id, f"edited-{index}")
            else:
                adapter = _RecordingAdapter(FakeAIAdapterMode.UNCERTAIN if action == "uncertain" else FakeAIAdapterMode.NORMAL)
                assert _redo(env, artifact_id, adapter, f"redo-{index}").operation.status in {"successful", "uncertain"}
            assert _references(env, artifact_id) == expected_sources
            for version_id, *_ in _versions(env, artifact_id):
                with Session(env.engine) as session:
                    retained = StudyNoteArtifactService(session, env.workspace_id).get_version(artifact_id, version_id)
                    assert retained.source_screenshot_ids == expected_sources
                    assert [line.removeprefix("- Screenshot ") for line in StudyNoteArtifactService.to_markdown(retained).splitlines() if line.startswith("- Screenshot ")] == [str(s) for s in expected_sources]
        with Session(env.engine) as session:
            # A different trusted workspace cannot turn its retained version
            # into a source of this artifact or learn its source references.
            with pytest.raises(ArtifactComparisonNotFoundError):
                ArtifactComparisonService(session, env.workspace_id).compare(
                    artifact_id, _versions(env, artifact_id)[0][0], foreign_version_id
                )
        assert _originals(env) == initial_bytes
    finally:
        _close(env)


# Feature: screenshot-graveyard, Property 17: any two retained versions compare read-only.
@PBT
@given(
    family=st.sampled_from(("study_note", "classification", "epitaph")),
    actions=st.lists(st.sampled_from(("edit", "redo")), min_size=1, max_size=4),
    pick_a=st.integers(0, 99),
    pick_b=st.integers(0, 99),
)
def test_p17_any_two_retained_versions_compare_without_mutation(
    tmp_path_factory, family, actions, pick_a, pick_b
):
    """Comparison is deterministic/read-only for arbitrary retained-version pairs."""
    env = _env(tmp_path_factory)
    try:
        if family == "study_note":
            artifact_id, expected_sources = _make_note(env, [19, 91])
            screenshot_id = None
        else:
            screenshot_id, classification_id, epitaph_id = _make_analysis(env)
            artifact_id = classification_id if family == "classification" else epitaph_id
            expected_sources = [screenshot_id]
        for index, action in enumerate(actions):
            if family == "study_note" and action == "edit":
                _edit(env, artifact_id, f"compare-edit-{index}")
            elif family != "study_note" and action == "edit":
                _edit_analysis(env, screenshot_id, f"compare-edit-{index}")
            elif family == "study_note":
                assert _redo(env, artifact_id, _RecordingAdapter(), f"compare-redo-{index}").operation.status == "successful"
            else:
                assert _redo_analysis(env, screenshot_id, _RecordingAdapter(), f"compare-redo-{index}").operation.status == "successful"
        before, before_pointer = _versions(env, artifact_id), _pointer(env, artifact_id)
        left = before[pick_a % len(before)]
        right = before[pick_b % len(before)]
        if left[0] == right[0]:
            right = before[(pick_b + 1) % len(before)]
        _foreign_artifact_id, foreign_version_id = _foreign_version(env)
        with Session(env.engine) as session:
            comparison = ArtifactComparisonService(session, env.workspace_id).compare(artifact_id, left[0], right[0])
            with pytest.raises(ArtifactComparisonNotFoundError):
                ArtifactComparisonService(session, env.workspace_id).compare(artifact_id, 0, right[0])
            with pytest.raises(ArtifactComparisonNotFoundError):
                ArtifactComparisonService(session, env.workspace_id).compare(artifact_id, foreign_version_id, right[0])
        assert comparison.artifact_id == artifact_id
        assert comparison.left_version["versionId"] == left[0]
        assert comparison.right_version["versionId"] == right[0]
        left_content, right_content = json.loads(left[3]), json.loads(right[3])
        expected = {
            field
            for field in set(left_content) | set(right_content)
            if field not in left_content
            or field not in right_content
            or left_content[field] != right_content[field]
        }
        actual = {
            change["field"]
            for changes in comparison.differences.values()
            for change in changes
        }
        assert actual == expected
        # Redo intentionally may retain identical Classification JSON while
        # changing its paired Epitaph; the Study Note branch always changes a
        # persisted field and guards against a vacuous comparison response.
        if family == "study_note":
            assert actual
        assert _versions(env, artifact_id) == before
        assert _pointer(env, artifact_id) == before_pointer
        assert _references(env, artifact_id) == expected_sources
    finally:
        _close(env)
