"""Task 2.8: retained Study Note edits, source reopening, and Markdown export."""

from __future__ import annotations

import io
import json
import threading

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app import config
import app.db.session as sess_mod
from app.ai import provider
from app.ai.fake_adapter import FakeAIAdapter
from app.db import models
from app.db.base import Base
from app.db.repository import WorkspaceRepository
from app.main import create_app
from app.ratelimit import TokenBucketRateLimiter
from app.services.study_note_artifact_service import (
    StudyNoteArtifactService,
    StudyNoteArtifactUnavailableError,
    StudyNoteEditValidationError,
)

TRUSTED = config.TRUSTED_ORIGINS[0]
HDRS = {"Origin": TRUSTED, "X-Requested-With": "fetch"}


def png(color: tuple[int, int, int]) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (12, 9), color).save(out, format="PNG")
    return out.getvalue()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, class_=Session)
    monkeypatch.setattr(sess_mod, "engine", engine)
    monkeypatch.setattr(sess_mod, "SessionLocal", factory)
    monkeypatch.setattr(config, "COOKIE_SECURE", False)
    monkeypatch.setattr(config, "STORAGE_ROOT", str(tmp_path / "storage"))
    limiter = TokenBucketRateLimiter(capacity=10_000, refill_per_sec=1000.0)
    app = create_app(
        workspace_session_factory=factory,
        rate_limiter=limiter,
        workspace_rate_limiter=TokenBucketRateLimiter(capacity=10_000, refill_per_sec=1000.0),
    )
    old_adapter = provider.get_ai_adapter()
    provider.set_ai_adapter(FakeAIAdapter())
    test_client = TestClient(app)
    test_client.get("/api/workspace")
    yield test_client, engine
    provider.set_ai_adapter(old_adapter)


def _make_note(client: TestClient, count: int = 2) -> tuple[int, list[int]]:
    ids: list[int] = []
    for index in range(count):
        response = client.post(
            "/api/screenshots",
            headers=HDRS,
            files=[("files", (f"{index}.png", png((index * 30, 1, 2)), "image/png"))],
        )
        assert response.status_code == 201
        sid = response.json()["results"][0]["screenshotId"]
        ids.append(sid)
        assert client.post(
            f"/api/screenshots/{sid}/consent", headers=HDRS, json={"granted": True}
        ).status_code == 200
    generated = client.post("/api/study-notes", headers=HDRS, json={"screenshotIds": ids})
    assert generated.status_code == 202
    op = client.get(f"/api/operations/{generated.json()['operationId']}")
    assert op.json()["status"] in {"successful", "uncertain"}
    note = client.get("/api/study-notes/1")
    assert note.status_code == 200
    return note.json()["artifactId"], ids


def _edited_content(note: dict, topic: str = "Edited \u03bb topic") -> dict:
    content = note["content"].copy()
    content.update(
        {
            "topic": topic,
            "main_concepts": ["  preserve whitespace  ", "Unicode \u03bb"],
            "extracted_code": ["def f():\r\n\treturn `\u2713`  \r\n"],
            "code_explanations": ["A fenced explanation."],
            "pitfalls": ["Trailing whitespace matters.  "],
            "uncertain_content": ["May vary."],
        }
    )
    content.pop("references")
    return content


def test_manual_edit_inserts_new_immutable_version_and_preserves_sources(client):
    c, engine = client
    artifact_id, ids = _make_note(c)
    original = c.get(f"/api/study-notes/{artifact_id}").json()
    old_json = json.dumps(original["content"], ensure_ascii=False)

    saved = c.post(
        f"/api/study-notes/{artifact_id}/edits",
        headers=HDRS,
        json={"content": _edited_content(original)},
    )
    assert saved.status_code == 200
    current = saved.json()
    assert current["versionNumber"] == original["versionNumber"] + 1
    assert current["origin"] == "manual_edit"
    assert current["sourceScreenshotIds"] == ids
    assert current["content"]["references"] == ids

    historical = c.get(f"/api/study-notes/{artifact_id}?versionId={original['versionId']}")
    assert historical.status_code == 200
    assert json.dumps(historical.json()["content"], ensure_ascii=False) == old_json
    with Session(engine) as session:
        artifact = session.get(models.Artifact, artifact_id)
        assert artifact.current_version_id == current["versionId"]
        versions = session.scalars(
            select(models.ArtifactVersion).where(models.ArtifactVersion.artifact_id == artifact_id)
        ).all()
        assert len(versions) == 2
        refs = session.scalars(
            select(models.SourceReference)
            .where(models.SourceReference.artifact_id == artifact_id)
            .order_by(models.SourceReference.id)
        ).all()
        assert [ref.screenshot_id for ref in refs] == ids


def test_multiple_edits_keep_full_history_and_failed_edit_creates_no_version(client):
    c, engine = client
    artifact_id, _ = _make_note(c)
    first = c.get(f"/api/study-notes/{artifact_id}").json()
    second = c.post(
        f"/api/study-notes/{artifact_id}/edits", headers=HDRS,
        json={"content": _edited_content(first, "second")},
    ).json()
    third = c.post(
        f"/api/study-notes/{artifact_id}/edits", headers=HDRS,
        json={"content": _edited_content(second, "third")},
    ).json()
    assert [first["versionNumber"], second["versionNumber"], third["versionNumber"]] == [1, 2, 3]
    bad = c.post(
        f"/api/study-notes/{artifact_id}/edits", headers=HDRS,
        json={"content": {"topic": ""}},
    )
    assert bad.status_code == 422
    with Session(engine) as session:
        versions = session.scalars(
            select(models.ArtifactVersion)
            .where(models.ArtifactVersion.artifact_id == artifact_id)
            .order_by(models.ArtifactVersion.version_number)
        ).all()
        assert [v.version_number for v in versions] == [1, 2, 3]
        assert session.get(models.Artifact, artifact_id).current_version_id == third["versionId"]


def test_service_rejects_malformed_content_before_writing(client):
    c, engine = client
    artifact_id, _ = _make_note(c)
    with Session(engine) as session:
        service = StudyNoteArtifactService(session, 1)
        with pytest.raises(StudyNoteEditValidationError):
            service.save_edit(artifact_id, {"topic": "valid", "references": [1]})
        assert len(session.scalars(select(models.ArtifactVersion)).all()) == 1


def test_persistence_failure_rolls_back_new_version_and_current_pointer(client, monkeypatch):
    c, engine = client
    artifact_id, _ = _make_note(c)
    current = c.get(f"/api/study-notes/{artifact_id}").json()
    original_add = WorkspaceRepository.add_artifact_version

    def flush_then_fail(self, **kwargs):
        original_add(self, **kwargs)
        raise SQLAlchemyError("forced persistence failure")

    monkeypatch.setattr(WorkspaceRepository, "add_artifact_version", flush_then_fail)
    with Session(engine) as session:
        workspace_id = session.get(models.Artifact, artifact_id).workspace_id
        with pytest.raises(StudyNoteArtifactUnavailableError):
            StudyNoteArtifactService(session, workspace_id).save_edit(
                artifact_id, _edited_content(current)
            )
    with Session(engine) as session:
        artifact = session.get(models.Artifact, artifact_id)
        versions = session.scalars(
            select(models.ArtifactVersion).where(models.ArtifactVersion.artifact_id == artifact_id)
        ).all()
        assert len(versions) == 1
        assert artifact.current_version_id == current["versionId"]


def test_concurrent_manual_edits_allocate_distinct_retained_versions(tmp_path):
    """Two independent sessions must not race max(version_number)+1."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'versions.db'}",
        connect_args={"check_same_thread": False, "timeout": 10},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, class_=Session)
    with Session(engine) as session:
        workspace = models.Workspace(token_hash="c" * 64)
        session.add(workspace)
        session.flush()
        repo = WorkspaceRepository(session, workspace.id)
        artifact = repo.create_artifact(kind="study_note")
        v1 = repo.add_artifact_version(
            artifact_id=artifact.id,
            version_number=1,
            origin="ai_generate",
            content_json=json.dumps({
                "topic": "original", "main_concepts": [], "extracted_code": [],
                "code_explanations": [], "pitfalls": [], "uncertain_content": [], "references": [],
            }),
        )
        repo.set_current_version(artifact.id, v1.id)
        session.commit()
        workspace_id, artifact_id = workspace.id, artifact.id

    barrier = threading.Barrier(2)
    outcomes: list[int] = []
    errors: list[Exception] = []

    def save(topic: str) -> None:
        session = factory()
        try:
            barrier.wait(timeout=5)
            outcome = StudyNoteArtifactService(session, workspace_id).save_edit(
                artifact_id,
                {
                    "topic": topic, "main_concepts": [], "extracted_code": [],
                    "code_explanations": [], "pitfalls": [], "uncertain_content": [],
                },
            )
            outcomes.append(outcome.version_number)
        except Exception as exc:  # surfaced below with the real failure type
            errors.append(exc)
        finally:
            session.close()

    threads = [threading.Thread(target=save, args=(topic,)) for topic in ("one", "two")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
    assert not errors
    assert sorted(outcomes) == [2, 3]
    with Session(engine) as session:
        versions = session.scalars(
            select(models.ArtifactVersion)
            .where(models.ArtifactVersion.artifact_id == artifact_id)
            .order_by(models.ArtifactVersion.version_number)
        ).all()
        artifact = session.get(models.Artifact, artifact_id)
        assert [version.version_number for version in versions] == [1, 2, 3]
        assert artifact.current_version_id in {versions[1].id, versions[2].id}


def test_sources_reopen_through_existing_authorized_image_route_without_bytes_in_listing(client):
    c, engine = client
    artifact_id, ids = _make_note(c)
    original_rows = {}
    with Session(engine) as session:
        for sid in ids:
            screenshot = session.get(models.Screenshot, sid)
            blob = session.get(models.ContentBlob, screenshot.content_blob_id)
            original_rows[sid] = (screenshot.id, blob.sha256, blob.storage_path)
    sources = c.get(f"/api/study-notes/{artifact_id}/sources")
    assert sources.status_code == 200
    assert sources.json()["sources"] == [
        {"screenshotId": sid, "imageUrl": f"/api/screenshots/{sid}/image"} for sid in ids
    ]
    assert "storage" not in sources.text and "sha256" not in sources.text
    for sid in ids:
        image = c.get(f"/api/screenshots/{sid}/image")
        assert image.status_code == 200 and image.content
    original = c.get(f"/api/study-notes/{artifact_id}").json()
    assert c.post(
        f"/api/study-notes/{artifact_id}/edits",
        headers=HDRS,
        json={"content": _edited_content(original)},
    ).status_code == 200
    with Session(engine) as session:
        for sid, expected in original_rows.items():
            screenshot = session.get(models.Screenshot, sid)
            blob = session.get(models.ContentBlob, screenshot.content_blob_id)
            assert (screenshot.id, blob.sha256, blob.storage_path) == expected


def test_export_defaults_current_and_can_select_historical_version_with_markdown_fidelity(client):
    c, _ = client
    artifact_id, _ = _make_note(c)
    original = c.get(f"/api/study-notes/{artifact_id}").json()
    saved = c.post(
        f"/api/study-notes/{artifact_id}/edits", headers=HDRS,
        json={"content": _edited_content(original, "\u03bb heading")},
    ).json()
    current_export = c.get(f"/api/study-notes/{artifact_id}/export")
    historical_export = c.get(
        f"/api/study-notes/{artifact_id}/export?versionId={original['versionId']}"
    )
    assert current_export.status_code == 200
    assert current_export.headers["content-type"].startswith("text/markdown")
    assert current_export.headers["content-disposition"] == (
        f'attachment; filename="study-note-{artifact_id}-v{saved["versionNumber"]}.md"'
    )
    assert "# \u03bb heading" in current_export.text
    assert "\r\n\treturn `\u2713`  \r\n" in current_export.text
    assert "## References" in current_export.text
    assert historical_export.status_code == 200
    assert original["content"]["topic"] in historical_export.text
    assert "\u03bb heading" not in historical_export.text
    assert "/storage/" not in current_export.text


def test_foreign_and_missing_artifact_or_version_are_generic_404_and_spoofing_fails(client):
    c, _ = client
    artifact_id, ids = _make_note(c)
    current = c.get(f"/api/study-notes/{artifact_id}").json()
    c.cookies.clear()
    c.get("/api/workspace")
    foreign = c.get(f"/api/study-notes/{artifact_id}/export")
    missing = c.get("/api/study-notes/987654/export")
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json()
    assert c.get(f"/api/study-notes/{artifact_id}/sources").status_code == 404
    assert c.get(f"/api/screenshots/{ids[0]}/image").status_code == 404
    assert c.get(f"/api/study-notes/{artifact_id}?versionId={current['versionId']}").status_code == 404
    assert c.get(f"/api/study-notes/987654?versionId=987655").status_code == 404
    spoof = c.post(
        f"/api/study-notes/{artifact_id}/edits?workspaceId=1",
        headers={**HDRS, "X-Workspace-Id": "1"},
        json={"workspaceId": 1, "content": _edited_content(current)},
    )
    assert spoof.status_code == 404


def test_edit_requires_csrf_and_does_not_create_ai_work_or_operations(client):
    c, engine = client
    artifact_id, _ = _make_note(c)
    original = c.get(f"/api/study-notes/{artifact_id}").json()
    denied = c.post(
        f"/api/study-notes/{artifact_id}/edits",
        headers={"Origin": TRUSTED},
        json={"content": _edited_content(original)},
    )
    assert denied.status_code == 403
    with Session(engine) as session:
        assert len(session.scalars(select(models.Operation)).all()) == 1  # generation only
        assert len(session.scalars(select(models.ArtifactVersion)).all()) == 1
