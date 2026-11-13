"""Study_Note_Service generation (Task 2.7).

Executes one `study_note` operation for a user-selected, ordered set of
screenshots, reusing the earlier building blocks:

  selection policy (2.6) -> consent for EVERY screenshot (2.3) -> read originals
  -> ONE adapter call (2.1/2.2) -> strict runtime validation -> atomic
  persistence of the study_note artifact + version 1 + one source_reference per
  screenshot + current-version pointer + operation status (2.4).

Contract (Req 6.4-6.7, 6.12; INV-1, INV-4, INV-7; P13, P16, P18, P19):

  * Consent and ownership are RE-CHECKED for ALL selected screenshots at
    execution time, immediately before bytes are read or the adapter is called.
    If any one is missing/revoked/foreign the whole batch fails closed and the
    adapter is never invoked (a partially authorized batch never proceeds).
  * Exactly ONE `AIAdapter.study_note` call per operation. An operation that is
    not `pending` (already executed/terminal) never reaches the adapter again.
  * Adapter output is re-validated at runtime. A BaseModel instance is dumped
    and re-validated (pydantic does not re-validate instances by default) so a
    `model_construct`-style invalid object cannot slip through.
  * `references` must equal the selected Screenshot IDs exactly (no fabricated,
    missing, duplicate, or foreign IDs). Mismatches FAIL the operation; nothing
    is silently dropped or "fixed".
  * Content is stored with `json.dumps`, which round-trips strings exactly, so
    extracted code keeps its indentation, tabs, CRLFs and symbols with no
    normalization (P19).
  * Uncertainty (P13/Req 6.6): the uncertain entries are stored in the note's
    Uncertain/ambiguous section and the operation ends `uncertain`, committed in
    the same transaction as the note. Otherwise the operation ends `successful`.
  * Originals are only read, never modified (INV-1).

The adapter is injected; this module never builds a provider or touches a
network. Per-workspace serialization is a single-database write lock, not a
distributed lock.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import config
from app.ai.adapter import AIAdapter, AIAdapterError
from app.ai.schemas import StudyNoteResult
from app.db.repository import WorkspaceRepository
from app.services.consent_service import (
    ConsentRequiredError,
    ConsentService,
    ConsentServiceUnavailableError,
    ScreenshotNotFoundError as ConsentScreenshotNotFoundError,
)
from app.services.operation_service import (
    OperationPersistenceError,
    OperationService,
    OperationState,
    OperationTransitionError,
)
from app.services.study_selection_service import (
    StudySelectionError,
    StudySelectionService,
)
from app.services.vault_service import VaultService, VaultServiceError
from app.services.vault_service import ScreenshotNotFoundError as VaultScreenshotNotFoundError

_KIND_STUDY_NOTE = "study_note"
_ORIGIN_AI_GENERATE = "ai_generate"
_FIRST_VERSION = 1


@dataclass(frozen=True)
class StudyNoteOutcome:
    operation: OperationState
    artifact_id: int | None = None


class StudyNoteService:
    def __init__(
        self,
        session: Session,
        workspace_id: int,
        *,
        adapter: AIAdapter,
        vault: VaultService | None = None,
    ) -> None:
        if workspace_id is None:
            raise ValueError("workspace_id is required")
        if adapter is None:
            raise ValueError("adapter is required")
        self._session = session
        self._adapter = adapter
        self._selection = StudySelectionService(session, workspace_id)
        self._consent = ConsentService(session, workspace_id)
        self._operations = OperationService(session, workspace_id)
        self._vault = vault or VaultService(session, workspace_id)
        self._repo = WorkspaceRepository(session, workspace_id)

    def run_generation(
        self, screenshot_ids: list[int], operation_id: int
    ) -> StudyNoteOutcome:
        """Execute one study_note operation. Intended as the BackgroundTask body."""
        ids = list(screenshot_ids)

        # 0) Idempotence: only a pending study_note operation may execute. A
        #    repeated/duplicate run never reaches consent, bytes, or the adapter.
        current = self._operations.get(operation_id)
        if current.kind != _KIND_STUDY_NOTE or current.status != "pending":
            return StudyNoteOutcome(operation=current)

        # 1) Re-validate the selection (count/duplicates/ownership) in the
        #    trusted workspace; never trust IDs validated at acceptance time.
        try:
            self._selection.validate_selection(ids)
        except StudySelectionError:
            return self._fail(operation_id, "Invalid selection")

        # 2) Consent for EVERY selected screenshot, before any bytes are read.
        try:
            for sid in ids:
                self._consent.require_consent(sid)
        except ConsentRequiredError:
            return self._fail(operation_id, "AI processing consent required")
        except ConsentScreenshotNotFoundError:
            return self._fail(operation_id, "Not found")
        except ConsentServiceUnavailableError:
            return self._fail(operation_id, "AI processing unavailable")

        # 3) Read originals (pure reads) in selection order. Stop before a
        # provider-bound aggregate would be accumulated in memory; the adapter
        # repeats these checks before Base64 expansion as defense in depth.
        images: list[tuple[int, bytes, str]] = []
        total_input_bytes = 0
        try:
            for sid in ids:
                original = self._vault.read_original(sid)
                if (
                    original.byte_size > config.AI_MAX_INPUT_IMAGE_BYTES
                    or total_input_bytes + original.byte_size
                    > config.AI_MAX_STUDY_INPUT_BYTES
                ):
                    images.clear()
                    return self._fail(operation_id, "AI input exceeds configured limits")
                total_input_bytes += original.byte_size
                images.append((sid, original.data, original.mime_type))
        except (VaultScreenshotNotFoundError, VaultServiceError):
            return self._fail(operation_id, "Not found")

        # Reading up to twelve originals can take enough time for a user to
        # revoke consent. Re-check EVERY selected screenshot immediately before
        # the only external-facing boundary; otherwise revocation during the
        # final read could still reach the adapter.
        try:
            for sid in ids:
                self._consent.require_consent(sid)
        except ConsentRequiredError:
            return self._fail(operation_id, "AI processing consent required")
        except ConsentScreenshotNotFoundError:
            return self._fail(operation_id, "Not found")
        except ConsentServiceUnavailableError:
            return self._fail(operation_id, "AI processing unavailable")

        # 4) EXACTLY ONE adapter call.
        # Refresh the operation heartbeat after the potentially lengthy source
        # reads and immediately before the adapter boundary.  The stale sweep
        # can then distinguish queued/active work from a crashed worker.
        try:
            self._operations.heartbeat_pending(operation_id)
        except OperationTransitionError:
            return StudyNoteOutcome(operation=self._operations.get(operation_id))
        try:
            raw = self._adapter.study_note(images)
        except AIAdapterError:
            return self._fail(operation_id, "AI analysis failed")
        except Exception:
            return self._fail(operation_id, "AI analysis failed")
        finally:
            images.clear()  # drop references to image bytes promptly

        # 5) Strict runtime validation (never trust the return annotation).
        try:
            payload = raw.model_dump() if isinstance(raw, BaseModel) else raw
            result = StudyNoteResult.model_validate(payload)
        except Exception:
            return self._fail(operation_id, "AI output failed validation")

        # 6) References must be exactly the selected IDs: no fabricated,
        #    missing, duplicate or foreign IDs; nothing silently discarded.
        if result.references != ids:
            return self._fail(operation_id, "AI output references invalid")

        # 7) Atomic persistence: note + v1 + source refs + pointer + status.
        uncertain = len(result.uncertain_content) > 0
        created: dict[str, int] = {}

        def _persist() -> None:
            art = self._repo.create_artifact(kind=_KIND_STUDY_NOTE)
            ver = self._repo.add_artifact_version(
                artifact_id=art.id,
                version_number=_FIRST_VERSION,
                origin=_ORIGIN_AI_GENERATE,
                content_json=self._note_json(result, ids),
            )
            for sid in ids:  # selection order
                self._repo.add_source_reference(artifact_id=art.id, screenshot_id=sid)
            self._repo.set_current_version(art.id, ver.id)
            created["artifact_id"] = art.id

        try:
            op = self._operations.complete_after_persistence(
                operation_id,
                _persist,
                terminal_status="uncertain" if uncertain else "successful",
            )
        except OperationPersistenceError:
            return StudyNoteOutcome(operation=self._operations.get(operation_id))
        except OperationTransitionError:
            # A concurrent worker finished first; its result stands.
            return StudyNoteOutcome(operation=self._operations.get(operation_id))
        return StudyNoteOutcome(operation=op, artifact_id=created.get("artifact_id"))

    # ----------------------------------------------------------------- helpers

    def _fail(self, operation_id: int, detail: str) -> StudyNoteOutcome:
        try:
            op = self._operations.set_terminal_status(
                operation_id, "failed", error_detail=detail
            )
        except OperationTransitionError:
            op = self._operations.get(operation_id)
        return StudyNoteOutcome(operation=op)

    @staticmethod
    def _note_json(result: StudyNoteResult, ordered_ids: list[int]) -> str:
        """Serialize all required sections; strings are stored unmodified."""
        return json.dumps(
            {
                "topic": result.topic,
                "main_concepts": result.main_concepts,
                "extracted_code": result.extracted_code,
                "code_explanations": result.code_explanations,
                "pitfalls": result.pitfalls,
                "uncertain_content": result.uncertain_content,
                "references": list(ordered_ids),
            },
            ensure_ascii=False,
        )
