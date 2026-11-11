"""Append-only manual Study Note editing and export (Task 2.8).

This is deliberately a narrow companion to ``StudyNoteService``: it operates
only on an already-generated ``study_note`` artifact.  It never reads image
bytes, invokes an AI adapter, creates operations, or changes provenance.

The generic concurrent redo/versioning workflow is Task 3.1.  Task 2.8 still
needs safe manual edits, so this service uses the existing workspace lock and
the existing INSERT-only ``artifact_version`` table to atomically insert one
manual version and repoint ``current_version_id``.  Prior version rows and
source-reference rows are never updated.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.ai.schemas import StudyNoteResult
from app.db.repository import CrossWorkspaceError, WorkspaceRepository

_KIND_STUDY_NOTE = "study_note"
_ORIGIN_MANUAL_EDIT = "manual_edit"


class StudyNoteArtifactError(Exception):
    """Base error with safe, non-private messages."""


class StudyNoteArtifactNotFoundError(StudyNoteArtifactError):
    """Missing, foreign, and wrong-kind artifacts are intentionally identical."""


class StudyNoteArtifactUnavailableError(StudyNoteArtifactError):
    """The database could not safely complete a read or write."""


class StudyNoteEditValidationError(StudyNoteArtifactError):
    """The client supplied malformed manual note content."""


class ManualStudyNoteContent(BaseModel):
    """Editable Study Note fields, excluding immutable source references.

    References are artifact-level provenance.  They are retained server-side on
    every manual version rather than accepted from a client that could replace
    or fabricate them.
    """

    model_config = ConfigDict(extra="forbid")

    topic: str = Field(min_length=1)
    main_concepts: list[str] = Field(default_factory=list)
    extracted_code: list[str] = Field(default_factory=list)
    code_explanations: list[str] = Field(default_factory=list)
    pitfalls: list[str] = Field(default_factory=list)
    uncertain_content: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class StudyNoteVersion:
    artifact_id: int
    version_id: int
    version_number: int
    origin: str
    content: StudyNoteResult
    source_screenshot_ids: list[int]


class StudyNoteArtifactService:
    """Workspace-scoped retained-version reads, edits, sources, and export."""

    def __init__(self, session: Session, workspace_id: int) -> None:
        if workspace_id is None:
            raise ValueError("workspace_id is required")
        self._session = session
        self._repo = WorkspaceRepository(session, workspace_id)

    def get_version(
        self, artifact_id: int, version_id: int | None = None
    ) -> StudyNoteVersion:
        """Return a retained owned Study Note version, defaulting to current."""
        try:
            artifact = self._study_note(artifact_id)
            selected_id = artifact.current_version_id if version_id is None else version_id
            if selected_id is None:
                raise StudyNoteArtifactNotFoundError("study note not found")
            version = self._repo.get_artifact_version(selected_id)
            if version is None or version.artifact_id != artifact.id:
                raise StudyNoteArtifactNotFoundError("study note not found")
            content = self._parse_content(version.content_json)
            return StudyNoteVersion(
                artifact_id=artifact.id,
                version_id=version.id,
                version_number=version.version_number,
                origin=version.origin,
                content=content,
                source_screenshot_ids=self._source_ids(artifact.id),
            )
        except CrossWorkspaceError as exc:
            raise StudyNoteArtifactNotFoundError("study note not found") from exc
        except SQLAlchemyError as exc:
            raise StudyNoteArtifactUnavailableError("study note service unavailable") from exc

    def save_edit(self, artifact_id: int, raw_content: object) -> StudyNoteVersion:
        """Insert a retained manual version and repoint current atomically.

        The callback neither updates nor deletes a historical version.  If
        validation, flushing, or commit fails, rollback leaves the original
        current pointer and every prior version unchanged.
        """
        try:
            try:
                editable = ManualStudyNoteContent.model_validate(raw_content)
            except (ValidationError, TypeError, ValueError) as exc:
                raise StudyNoteEditValidationError("Invalid study note content") from exc

            # Serialize number assignment and pointer movement.  This is a
            # narrow single-instance/DB transaction aid, not Task 3.1's full
            # concurrent redo conflict/retry implementation.
            if self._repo.lock_workspace() is None:
                raise StudyNoteArtifactNotFoundError("study note not found")
            artifact = self._study_note(artifact_id)
            source_ids = self._source_ids(artifact.id)
            versions = self._repo.list_artifact_versions(artifact.id)
            next_number = max((v.version_number for v in versions), default=0) + 1
            content = StudyNoteResult(
                **editable.model_dump(), references=list(source_ids)
            )
            version = self._repo.add_artifact_version(
                artifact_id=artifact.id,
                version_number=next_number,
                origin=_ORIGIN_MANUAL_EDIT,
                content_json=json.dumps(content.model_dump(), ensure_ascii=False),
            )
            self._repo.set_current_version(artifact.id, version.id)
            self._session.commit()
            return StudyNoteVersion(
                artifact_id=artifact.id,
                version_id=version.id,
                version_number=version.version_number,
                origin=version.origin,
                content=content,
                source_screenshot_ids=source_ids,
            )
        except StudyNoteArtifactError:
            self._session.rollback()
            raise
        except CrossWorkspaceError as exc:
            self._session.rollback()
            raise StudyNoteArtifactNotFoundError("study note not found") from exc
        except SQLAlchemyError as exc:
            self._session.rollback()
            raise StudyNoteArtifactUnavailableError("study note service unavailable") from exc

    @staticmethod
    def to_markdown(version: StudyNoteVersion) -> str:
        """Render one selected retained payload as portable CommonMark.

        Text is never normalized.  In particular, code strings are inserted
        verbatim between a fence longer than any backtick run they contain, so
        CRLF, indentation, Unicode and embedded fences remain valid Markdown.
        """
        content = version.content
        lines = [f"# {content.topic}", "", "## Main concepts", ""]
        lines.extend(f"- {item}" for item in content.main_concepts)
        lines.extend(["", "## Extracted code", ""])
        for code in content.extracted_code:
            fence = "`" * (max((len(run) for run in re.findall(r"`+", code)), default=2) + 1)
            lines.extend([fence, code, fence, ""])
        lines.extend(["## Code explanations", ""])
        lines.extend(f"- {item}" for item in content.code_explanations)
        lines.extend(["", "## Important pitfalls", ""])
        lines.extend(f"- {item}" for item in content.pitfalls)
        lines.extend(["", "## Uncertain/ambiguous content", ""])
        lines.extend(f"- {item}" for item in content.uncertain_content)
        lines.extend(["", "## References", ""])
        lines.extend(f"- Screenshot {sid}" for sid in version.source_screenshot_ids)
        return "\n".join(lines).rstrip() + "\n"

    def _study_note(self, artifact_id: int):
        artifact = self._repo.get_artifact(artifact_id)
        if artifact is None or artifact.kind != _KIND_STUDY_NOTE:
            raise StudyNoteArtifactNotFoundError("study note not found")
        return artifact

    def _source_ids(self, artifact_id: int) -> list[int]:
        # SourceReference insertion follows manual selection order.  The
        # repository deliberately has no implicit display order, so retain that
        # order here for provenance and Markdown attribution.
        return [ref.screenshot_id for ref in self._repo.list_source_references(artifact_id)]

    @staticmethod
    def _parse_content(content_json: str) -> StudyNoteResult:
        try:
            return StudyNoteResult.model_validate(json.loads(content_json))
        except (json.JSONDecodeError, ValidationError, TypeError, ValueError) as exc:
            raise StudyNoteArtifactUnavailableError("study note service unavailable") from exc
