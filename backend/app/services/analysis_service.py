"""Read and edit a screenshot's persisted classification + Epitaph (Task 2.10).

Integration work required by Task 2.10's acceptance criterion ("category/Epitaph
are editable", Req 4.5/5.5). Reads the CURRENT version of the newest
`classification` and `epitaph` artifacts that reference an owned screenshot. An
edit is APPEND-ONLY: it inserts a new `manual_edit` version, repoints
`current_version_id`, and never updates or deletes a prior version, source
reference, or the screenshot's original bytes (INV-1/INV-5/INV-6).

Both artifacts of an edit commit in ONE transaction or not at all. As in Task
2.8 this relies on the single-database workspace lock; the generic concurrent
versioning/retry design remains Task 3.1.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.ai.schemas import Category
from app.db.repository import CrossWorkspaceError, WorkspaceRepository

_KIND_CLASSIFICATION = "classification"
_KIND_EPITAPH = "epitaph"
_ORIGIN_MANUAL_EDIT = "manual_edit"


class AnalysisError(Exception):
    """Base error with safe, non-private messages."""


class AnalysisNotFoundError(AnalysisError):
    """Missing/foreign screenshot or nothing analysed yet (non-disclosing)."""


class AnalysisUnavailableError(AnalysisError):
    """Database failure; callers fail closed."""


class AnalysisEditValidationError(AnalysisError):
    """The client supplied an invalid category or empty Epitaph."""


class AnalysisEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: Category | None = None
    epitaph_text: str | None = Field(default=None, min_length=1, max_length=500)


@dataclass(frozen=True)
class AnalysisView:
    screenshot_id: int
    classification_version_number: int
    classification_origin: str
    category: str
    confidence: float
    uncertain: bool
    epitaph_version_number: int
    epitaph_origin: str
    epitaph_text: str
    visible_facts: str
    inferred_intent: str | None
    epitaph_uncertain: bool


class AnalysisService:
    def __init__(self, session: Session, workspace_id: int) -> None:
        if workspace_id is None:
            raise ValueError("workspace_id is required")
        self._session = session
        self._repo = WorkspaceRepository(session, workspace_id)

    # ------------------------------------------------------------------ reads

    def get_analysis(self, screenshot_id: int) -> AnalysisView:
        try:
            return self._load(screenshot_id)
        except CrossWorkspaceError as exc:
            raise AnalysisNotFoundError("analysis not found") from exc
        except SQLAlchemyError as exc:
            raise AnalysisUnavailableError("analysis service unavailable") from exc

    # ------------------------------------------------------------------ edits

    def save_edit(self, screenshot_id: int, raw_edit: object) -> AnalysisView:
        try:
            try:
                edit = AnalysisEdit.model_validate(raw_edit)
            except (ValidationError, TypeError, ValueError) as exc:
                raise AnalysisEditValidationError("Invalid analysis edit") from exc
            if edit.category is None and edit.epitaph_text is None:
                raise AnalysisEditValidationError("Nothing to edit")

            if self._repo.lock_workspace() is None:
                raise AnalysisNotFoundError("analysis not found")
            class_art, epi_art = self._artifacts(screenshot_id)
            current = self._load(screenshot_id)

            if edit.category is not None:
                content = {
                    "category": edit.category.value,
                    "confidence": current.confidence,
                    # A person made this decision; it is not an uncertain AI guess.
                    "uncertain": False,
                }
                self._append_version(class_art, content)
            if edit.epitaph_text is not None:
                content = {
                    "text": edit.epitaph_text,
                    "visible_facts": current.visible_facts,
                    "inferred_intent": current.inferred_intent,
                    "uncertain": False,
                }
                self._append_version(epi_art, content)
            self._session.commit()
            return self._load(screenshot_id)
        except AnalysisError:
            self._session.rollback()
            raise
        except CrossWorkspaceError as exc:
            self._session.rollback()
            raise AnalysisNotFoundError("analysis not found") from exc
        except SQLAlchemyError as exc:
            self._session.rollback()
            raise AnalysisUnavailableError("analysis service unavailable") from exc

    # --------------------------------------------------------------- internals

    def _artifacts(self, screenshot_id: int):
        classes = self._repo.list_artifacts_for_screenshot(screenshot_id, _KIND_CLASSIFICATION)
        epis = self._repo.list_artifacts_for_screenshot(screenshot_id, _KIND_EPITAPH)
        if not classes or not epis:
            raise AnalysisNotFoundError("analysis not found")
        return classes[-1], epis[-1]  # newest of each kind

    def _current(self, artifact):
        if artifact.current_version_id is None:
            raise AnalysisNotFoundError("analysis not found")
        version = self._repo.get_artifact_version(artifact.current_version_id)
        if version is None:
            raise AnalysisNotFoundError("analysis not found")
        try:
            return version, json.loads(version.content_json)
        except (json.JSONDecodeError, TypeError) as exc:
            raise AnalysisUnavailableError("analysis service unavailable") from exc

    def _load(self, screenshot_id: int) -> AnalysisView:
        class_art, epi_art = self._artifacts(screenshot_id)
        cv, c = self._current(class_art)
        ev, e = self._current(epi_art)
        try:
            return AnalysisView(
                screenshot_id=screenshot_id,
                classification_version_number=cv.version_number,
                classification_origin=cv.origin,
                category=Category(c["category"]).value,
                confidence=float(c["confidence"]),
                uncertain=bool(c["uncertain"]),
                epitaph_version_number=ev.version_number,
                epitaph_origin=ev.origin,
                epitaph_text=str(e["text"]),
                visible_facts=str(e.get("visible_facts", "")),
                inferred_intent=e.get("inferred_intent"),
                epitaph_uncertain=bool(e.get("uncertain", False)),
            )
        except (KeyError, ValueError, TypeError) as exc:
            raise AnalysisUnavailableError("analysis service unavailable") from exc

    def _append_version(self, artifact, content: dict) -> None:
        versions = self._repo.list_artifact_versions(artifact.id)
        next_number = max((v.version_number for v in versions), default=0) + 1
        version = self._repo.add_artifact_version(
            artifact_id=artifact.id,
            version_number=next_number,
            origin=_ORIGIN_MANUAL_EDIT,
            content_json=json.dumps(content, ensure_ascii=False),
        )
        self._repo.set_current_version(artifact.id, version.id)
