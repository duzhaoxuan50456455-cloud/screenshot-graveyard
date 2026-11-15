"""Task 3.4 retained artifact-version comparison tests.

The tests create only synthetic JSON payloads in disposable SQLite databases.
They exercise the HTTP route and service against the real workspace-scoped
repository; comparison must remain a pure read (no operations, adapters, or
historical mutations).
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import app.db.session as session_module
from app import config
from app.db import models
from app.db.base import Base
from app.db.repository import WorkspaceRepository
from app.main import create_app
from app.ratelimit import TokenBucketRateLimiter
from app.services.artifact_comparison_service import (
    ArtifactComparisonNotFoundError,
    ArtifactComparisonService,
    ArtifactComparisonUnavailableError,
)
from app.services.artifact_version_service import ArtifactVersionService


@pytest.fixture()
def client(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, class_=Session)
    monkeypatch.setattr(session_module, "engine", engine)
    monkeypatch.setattr(session_module, "SessionLocal", factory)
    monkeypatch.setattr(config, "COOKIE_SECURE", False)
    limiter = TokenBucketRateLimiter(capacity=100_000, refill_per_sec=100_000.0)
    app = create_app(workspace_session_factory=factory, rate_limiter=limiter, workspace_rate_limiter=limiter)
    c = TestClient(app)
    assert c.get("/api/workspace").json() == {"workspaceActive": True}
    yield c, engine


def _workspace_id(engine: object) -> int:
    with Session(engine) as session:
        return session.scalars(select(models.Workspace.id).order_by(models.Workspace.id)).first()


def _artifact(engine, workspace_id: int, kind: str, *payloads: dict) -> tuple[int, list[int]]:
    with Session(engine) as session:
        repo = WorkspaceRepository(session, workspace_id)
        artifact = repo.create_artifact(kind=kind)
        versions = ArtifactVersionService(session, workspace_id)
        created = [
            versions.create_first_version(
                artifact.id, json.dumps(payloads[0], ensure_ascii=False), "ai_generate"
            )
        ]
        for payload in payloads[1:]:
            created.append(
                versions.append_version(
                    artifact.id, json.dumps(payload, ensure_ascii=False), "manual_edit"
                )
            )
        session.commit()
        return artifact.id, [version.id for version in created]


def _comparison_url(artifact_id: int, left_id: int, right_id: int) -> str:
    return f"/api/artifacts/{artifact_id}/compare?leftVersionId={left_id}&rightVersionId={right_id}"


def test_service_reports_deterministic_added_removed_and_changed_fields(client):
    _, engine = client
    workspace_id = _workspace_id(engine)
    artifact_id, (v1, v2) = _artifact(
        engine,
        workspace_id,
        "study_note",
        {"topic": "First", "legacy": "gone", "same": True, "concepts": ["one"]},
        {"topic": "Second", "added": "new", "same": True, "concepts": ["two"]},
    )
    with Session(engine) as session:
        result = ArtifactComparisonService(session, workspace_id).compare(artifact_id, v1, v2)

    assert result.left_version["versionNumber"] == 1
    assert result.right_version["versionNumber"] == 2
    assert result.differences["added"] == [
        {"field": "added", "after": {"json": '"new"', "truncated": False}}
    ]
    assert result.differences["removed"] == [
        {"field": "legacy", "before": {"json": '"gone"', "truncated": False}}
    ]
    assert [item["field"] for item in result.differences["changed"]] == ["concepts", "topic"]
    assert result.differences["changed"][1] == {
        "field": "topic",
        "before": {"json": '"First"', "truncated": False},
        "after": {"json": '"Second"', "truncated": False},
    }


def test_route_compares_historical_study_note_and_classification_versions(client):
    c, engine = client
    workspace_id = _workspace_id(engine)
    note_id, (note_v1, note_v2) = _artifact(
        engine,
        workspace_id,
        "study_note",
        {
            "topic": "Loops",
            "main_concepts": ["for"],
            "extracted_code": [],
            "code_explanations": [],
            "pitfalls": [],
            "uncertain_content": [],
            "references": [1],
        },
        {
            "topic": "Loops revised",
            "main_concepts": ["for", "while"],
            "extracted_code": [],
            "code_explanations": [],
            "pitfalls": [],
            "uncertain_content": [],
            "references": [1],
        },
    )
    classification_id, (classification_v1, classification_v2) = _artifact(
        engine,
        workspace_id,
        "classification",
        {"category": "Reference/Document", "confidence": 0.75, "uncertain": False},
        {"category": "Study/Knowledge", "confidence": 0.75, "uncertain": False},
    )
    epitaph_id, (epitaph_v1, epitaph_v2) = _artifact(
        engine,
        workspace_id,
        "epitaph",
        {
            "text": "Original caption",
            "visible_facts": "Visible text",
            "inferred_intent": None,
            "uncertain": False,
        },
        {
            "text": "Edited caption",
            "visible_facts": "Visible text",
            "inferred_intent": None,
            "uncertain": False,
        },
    )

    note = c.get(_comparison_url(note_id, note_v1, note_v2))
    classification = c.get(_comparison_url(classification_id, classification_v1, classification_v2))
    epitaph = c.get(_comparison_url(epitaph_id, epitaph_v1, epitaph_v2))
    assert note.status_code == classification.status_code == epitaph.status_code == 200
    assert note.json()["artifactKind"] == "study_note"
    assert [item["field"] for item in note.json()["differences"]["changed"]] == [
        "main_concepts",
        "topic",
    ]
    assert classification.json()["artifactKind"] == "classification"
    assert classification.json()["differences"]["changed"] == [
        {
            "field": "category",
            "before": {"json": '"Reference/Document"', "truncated": False},
            "after": {"json": '"Study/Knowledge"', "truncated": False},
        }
    ]
    assert epitaph.json()["artifactKind"] == "epitaph"
    assert epitaph.json()["differences"]["changed"][0]["field"] == "text"


def test_identical_versions_have_an_empty_deterministic_comparison(client):
    c, engine = client
    workspace_id = _workspace_id(engine)
    artifact_id, (version_id,) = _artifact(engine, workspace_id, "epitaph", {"text": "Kept"})
    response = c.get(_comparison_url(artifact_id, version_id, version_id))
    assert response.status_code == 200
    assert response.json()["differences"] == {"added": [], "removed": [], "changed": []}
    assert response.json()["truncated"] is False


def test_long_values_with_an_identical_preview_are_still_changed(client):
    c, engine = client
    workspace_id = _workspace_id(engine)
    prefix = "same" * 600
    artifact_id, (v1, v2) = _artifact(
        engine,
        workspace_id,
        "epitaph",
        {"text": prefix + "-left"},
        {"text": prefix + "-right"},
    )
    changed = c.get(_comparison_url(artifact_id, v1, v2)).json()["differences"]["changed"]
    assert len(changed) == 1 and changed[0]["field"] == "text"
    assert changed[0]["before"]["truncated"] is True
    assert changed[0]["after"]["truncated"] is True
    assert changed[0]["before"]["json"] == changed[0]["after"]["json"]
    assert changed[0]["before"]["sha256"] != changed[0]["after"]["sha256"]


def test_missing_null_and_json_types_are_distinguished_deterministically(client):
    c, engine = client
    workspace_id = _workspace_id(engine)
    artifact_id, (v1, v2) = _artifact(
        engine,
        workspace_id,
        "study_note",
        {"removed_null": None, "typed": True, "empty": "", "array": [1], "nested": {"a": 1}},
        {"added_null": None, "typed": 1, "empty": "", "array": [1, 2], "nested": {"a": 2}},
    )
    differences = c.get(_comparison_url(artifact_id, v1, v2)).json()["differences"]
    assert [entry["field"] for entry in differences["added"]] == ["added_null"]
    assert [entry["field"] for entry in differences["removed"]] == ["removed_null"]
    assert [entry["field"] for entry in differences["changed"]] == ["array", "nested", "typed"]
    assert differences["changed"][-1]["before"]["json"] == "true"
    assert differences["changed"][-1]["after"]["json"] == "1"


def test_long_field_names_are_bounded_with_unambiguous_metadata(client):
    c, engine = client
    workspace_id = _workspace_id(engine)
    field = "field-" * 40
    artifact_id, (v1, v2) = _artifact(
        engine, workspace_id, "study_note", {field: "left"}, {field: "right"}
    )
    change = c.get(_comparison_url(artifact_id, v1, v2)).json()["differences"]["changed"][0]
    assert len(change["field"]) == 128
    assert change["fieldTruncated"] is True
    assert change["originalFieldCharacterCount"] == len(field)
    assert len(change["fieldSha256"]) == 64


def test_compare_is_read_only_and_never_creates_an_operation_or_ai_call(client, monkeypatch):
    c, engine = client
    workspace_id = _workspace_id(engine)
    artifact_id, (v1, v2) = _artifact(
        engine, workspace_id, "epitaph", {"text": "old"}, {"text": "new"}
    )
    with Session(engine) as session:
        artifact = session.get(models.Artifact, artifact_id)
        before_pointer = artifact.current_version_id
        before_versions = [
            (v.id, v.content_json) for v in session.scalars(
                select(models.ArtifactVersion).where(models.ArtifactVersion.artifact_id == artifact_id)
            )
        ]
        before_operations = session.scalar(select(func.count()).select_from(models.Operation))

    import app.ai.provider as provider
    monkeypatch.setattr(provider, "get_ai_adapter", lambda: (_ for _ in ()).throw(AssertionError("AI called")))
    assert c.get(_comparison_url(artifact_id, v1, v2)).status_code == 200

    with Session(engine) as session:
        artifact = session.get(models.Artifact, artifact_id)
        assert artifact.current_version_id == before_pointer
        assert [
            (v.id, v.content_json) for v in session.scalars(
                select(models.ArtifactVersion).where(models.ArtifactVersion.artifact_id == artifact_id)
            )
        ] == before_versions
        assert session.scalar(select(func.count()).select_from(models.Operation)) == before_operations


def test_foreign_missing_and_wrong_artifact_versions_are_indistinguishable(client):
    c, engine = client
    workspace_a = _workspace_id(engine)
    artifact_a, (a1, a2) = _artifact(engine, workspace_a, "study_note", {"topic": "a"}, {"topic": "b"})
    other_a, (other_v,) = _artifact(engine, workspace_a, "epitaph", {"text": "other"})

    c.cookies.clear()
    assert c.get("/api/workspace").json() == {"workspaceActive": True}
    foreign = c.get(_comparison_url(artifact_a, a1, a2))
    missing = c.get(_comparison_url(999999, a1, a2))
    wrong_version = c.get(_comparison_url(other_a, a1, other_v))
    assert foreign.status_code == missing.status_code == wrong_version.status_code == 404
    assert foreign.json() == missing.json() == wrong_version.json()


@pytest.mark.parametrize(
    "suffix,expected",
    [
        ("?leftVersionId=not-an-int&rightVersionId=1", 422),
        ("?leftVersionId=-1&rightVersionId=1", 422),
        ("?leftVersionId=1", 422),
        ("?leftVersionId=1&leftVersionId=2&rightVersionId=1", 400),
    ],
)
def test_invalid_version_identifiers_fail_without_side_effects(client, suffix, expected):
    c, engine = client
    workspace_id = _workspace_id(engine)
    artifact_id, _ = _artifact(engine, workspace_id, "epitaph", {"text": "safe"})
    with Session(engine) as session:
        before = session.scalar(select(func.count()).select_from(models.Operation))
    response = c.get(f"/api/artifacts/{artifact_id}/compare{suffix}")
    assert response.status_code == expected
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(models.Operation)) == before


def test_comparison_response_is_bounded(client):
    c, engine = client
    workspace_id = _workspace_id(engine)
    left = {f"field_{i:03d}": "x" * 4_000 for i in range(70)}
    right = {f"field_{i:03d}": "y" * 4_000 for i in range(70)}
    artifact_id, (v1, v2) = _artifact(engine, workspace_id, "study_note", left, right)
    body = c.get(_comparison_url(artifact_id, v1, v2)).json()
    assert body["truncated"] is True and body["omittedFieldCount"] == 6
    assert len(body["differences"]["changed"]) == 64
    assert all(item["before"]["truncated"] for item in body["differences"]["changed"])


def test_oversized_persisted_payload_fails_closed_before_parsing(client, monkeypatch):
    _, engine = client
    workspace_id = _workspace_id(engine)
    artifact_id, (v1, v2) = _artifact(
        engine, workspace_id, "epitaph", {"text": "safe"}, {"text": "also-safe"}
    )
    monkeypatch.setattr(
        "app.services.artifact_comparison_service._MAX_CONTENT_JSON_CHARS", 10
    )
    with Session(engine) as session:
        with pytest.raises(ArtifactComparisonUnavailableError):
            ArtifactComparisonService(session, workspace_id).compare(artifact_id, v1, v2)


def test_service_rejects_a_version_from_another_artifact_in_the_same_workspace(client):
    _, engine = client
    workspace_id = _workspace_id(engine)
    first, (first_version,) = _artifact(engine, workspace_id, "epitaph", {"text": "one"})
    _, (second_version,) = _artifact(engine, workspace_id, "epitaph", {"text": "two"})
    with Session(engine) as session:
        with pytest.raises(ArtifactComparisonNotFoundError):
            ArtifactComparisonService(session, workspace_id).compare(
                first, first_version, second_version
            )
