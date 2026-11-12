"""Classification_Service — one AI call, atomic classification + Epitaph (Task 2.5).

Orchestrates a single `classify` operation end-to-end with the pieces built in
Tasks 2.1-2.4:

  consent gate (2.3) -> read original bytes (1.8/1.11) -> ONE adapter call (2.1/2.2)
  -> schema-validate the output (2.1) -> atomic co-persistence of the
  classification + Epitaph artifact versions + source references + current-version
  repoints + operation->successful, all in ONE transaction (2.4's
  `complete_after_persistence`).

Correctness contract (Requirements 4.1-4.5, 4.7, 5.1, 5.3; INV-1, INV-4; P12/P13/P14):

  * Exactly ONE `AIAdapter.classify_and_epitaph` call per attempt (P14).
  * Consent is RE-CHECKED immediately before the adapter call — not only at
    request acceptance — so consent revoked between enqueue and execution blocks
    the adapter entirely (Req 9.2/9.4). A foreign/missing screenshot is
    non-disclosing.
  * Adapter output is validated against the Pydantic schema at this boundary
    (`ClassificationResult.model_validate`), never trusting the runtime return
    annotation. Invalid output -> operation `failed`, NO artifacts persisted (P12,
    Req 4.3, INV-4).
  * An explicitly uncertain result -> operation `uncertain`, NO artifacts
    persisted. Uncertainty is thus distinguishable from both a confident success
    and malformed data (P13, Req 4.4). (Design P14: "either both persisted and
    successful, or neither persisted and failed/uncertain.")
  * On success, the classification artifact version AND the Epitaph artifact
    version (plus their source references and current-version repoints and the
    operation->successful transition) commit together or not at all (P14, Req 4.2/5.1).
  * Original screenshot bytes are only READ, never modified (INV-1).
  * Potentially Sensitive is persisted as an ordinary advisory category — no
    deletion, suppression, or automatic action (Req 4.7).

The adapter is INJECTED (dependency injection); this service never constructs a
real provider and never transmits bytes to an external service. Tests inject the
deterministic `FakeAIAdapter`.

Concurrency note: atomic completion serializes per-workspace via
`OperationService` using a single-DB write lock. That is NOT a distributed lock
and does not by itself guarantee cross-instance PostgreSQL safety; the durable
backstops are the append-only `UNIQUE(artifact_id, version_number)` constraint
and the single-transaction commit.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from app.ai.adapter import AIAdapter, AIAdapterError
from app.ai.schemas import ClassificationResult
from app.services.consent_service import (
    ConsentRequiredError,
    ConsentService,
    ScreenshotNotFoundError as ConsentScreenshotNotFoundError,
)
from app.services.operation_service import (
    OperationPersistenceError,
    OperationService,
    OperationState,
    OperationTransitionError,
)
from app.services.vault_service import (
    ScreenshotNotFoundError as VaultScreenshotNotFoundError,
    VaultService,
    VaultServiceError,
)

# Artifact kinds (models.py: kind in {classification, epitaph, study_note}).
_KIND_CLASSIFICATION = "classification"
_KIND_EPITAPH = "epitaph"
# First AI-generated version origin (models.py: origin in {ai_generate, ai_redo, manual_edit}).
_ORIGIN_AI_GENERATE = "ai_generate"
_FIRST_VERSION = 1


class ClassificationServiceError(Exception):
    """Base error (generic, no private data in messages)."""


@dataclass(frozen=True)
class ClassificationOutcome:
    """Result of a classify attempt, including the terminal operation state."""

    operation: OperationState
    classification_artifact_id: int | None = None
    epitaph_artifact_id: int | None = None


class ClassificationService:
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
        self._workspace_id = workspace_id
        self._adapter = adapter
        self._consent = ConsentService(session, workspace_id)
        self._operations = OperationService(session, workspace_id)
        self._vault = vault or VaultService(session, workspace_id)
        self._repo = self._operations._repo  # workspace-scoped repository

    def run_classification(
        self, screenshot_id: int, operation_id: int
    ) -> ClassificationOutcome:
        """Execute one classify operation. Intended as the BackgroundTask body.

        Preconditions: `operation_id` is a pending `classify` operation owned by
        this workspace (created by the 202 acceptance route). This method owns the
        consent re-check, the single adapter call, validation, and the terminal
        operation transition.
        """
        # 1) Consent RE-CHECK immediately before any adapter work. If consent was
        #    revoked (or never granted) between enqueue and now, the adapter is
        #    never called and the operation fails safely. Foreign/missing is
        #    non-disclosing.
        try:
            self._consent.require_consent(screenshot_id)
        except ConsentRequiredError:
            op = self._operations.set_terminal_status(
                operation_id, "failed", error_detail="AI processing consent required"
            )
            return ClassificationOutcome(operation=op)
        except ConsentScreenshotNotFoundError:
            op = self._operations.set_terminal_status(
                operation_id, "failed", error_detail="Not found"
            )
            return ClassificationOutcome(operation=op)

        # 2) Read the ORIGINAL bytes (pure read; never mutates — INV-1).
        try:
            original = self._vault.read_original(screenshot_id)
        except (VaultScreenshotNotFoundError, VaultServiceError):
            op = self._operations.set_terminal_status(
                operation_id, "failed", error_detail="Not found"
            )
            return ClassificationOutcome(operation=op)

        # Reading local bytes can take enough time for a user to revoke consent.
        # Re-check immediately before the only external-facing boundary so no
        # revoked screenshot reaches an adapter after that queue/read window.
        try:
            self._consent.require_consent(screenshot_id)
        except ConsentRequiredError:
            op = self._operations.set_terminal_status(
                operation_id, "failed", error_detail="AI processing consent required"
            )
            return ClassificationOutcome(operation=op)
        except ConsentScreenshotNotFoundError:
            op = self._operations.set_terminal_status(
                operation_id, "failed", error_detail="Not found"
            )
            return ClassificationOutcome(operation=op)

        # 3) EXACTLY ONE adapter call. Timeouts/adapter errors -> failed, no artifacts.
        # Heartbeat immediately before the external-facing boundary so queued
        # work is not mistaken for a crashed worker by stale recovery.
        try:
            self._operations.heartbeat_pending(operation_id)
        except OperationTransitionError:
            return ClassificationOutcome(operation=self._operations.get(operation_id))
        try:
            raw = self._adapter.classify_and_epitaph(original.data, original.mime_type)
        except AIAdapterError:
            op = self._operations.set_terminal_status(
                operation_id, "failed", error_detail="AI analysis failed"
            )
            return ClassificationOutcome(operation=op)

        # 4) Validate at the boundary (do NOT trust the return annotation). A fake
        #    or real adapter returning malformed data is rejected here (P12).
        try:
            # Pydantic trusts an instance of the target model by default.
            # Dumping then validating closes that escape hatch for
            # `model_construct`-created, malformed provider objects, including
            # invalid nested Epitaph data and enum/confidence values.
            payload = raw.model_dump() if isinstance(raw, BaseModel) else raw
            result = ClassificationResult.model_validate(payload)
        except (ValidationError, TypeError, ValueError):
            op = self._operations.set_terminal_status(
                operation_id, "failed", error_detail="AI output failed validation"
            )
            return ClassificationOutcome(operation=op)

        # 5) Explicit uncertainty -> operation `uncertain`, NO artifacts persisted.
        #    Distinguishable from both confident success and malformed data (P13).
        if result.uncertain:
            op = self._operations.set_terminal_status(
                operation_id, "uncertain", error_detail="AI result uncertain"
            )
            return ClassificationOutcome(operation=op)

        # 6) Confident, valid result -> atomic co-persistence (P14). The callback
        #    only flushes; `complete_after_persistence` commits the artifacts AND
        #    the operation->successful transition together (or rolls both back).
        created: dict[str, int] = {}

        def _persist() -> None:
            class_art = self._repo.create_artifact(kind=_KIND_CLASSIFICATION)
            epi_art = self._repo.create_artifact(kind=_KIND_EPITAPH)
            class_ver = self._repo.add_artifact_version(
                artifact_id=class_art.id,
                version_number=_FIRST_VERSION,
                origin=_ORIGIN_AI_GENERATE,
                content_json=self._classification_json(result),
            )
            epi_ver = self._repo.add_artifact_version(
                artifact_id=epi_art.id,
                version_number=_FIRST_VERSION,
                origin=_ORIGIN_AI_GENERATE,
                content_json=self._epitaph_json(result),
            )
            # Provenance: both artifacts reference the source screenshot (INV-7).
            self._repo.add_source_reference(
                artifact_id=class_art.id, screenshot_id=screenshot_id
            )
            self._repo.add_source_reference(
                artifact_id=epi_art.id, screenshot_id=screenshot_id
            )
            # Repoint each artifact at its freshly-created current version.
            self._repo.set_current_version(class_art.id, class_ver.id)
            self._repo.set_current_version(epi_art.id, epi_ver.id)
            created["classification_artifact_id"] = class_art.id
            created["epitaph_artifact_id"] = epi_art.id

        try:
            op = self._operations.complete_after_persistence(operation_id, _persist)
        except OperationPersistenceError:
            # Atomic rollback already occurred; a fresh `failed` was recorded.
            op = self._operations.get(operation_id)
            return ClassificationOutcome(operation=op)

        return ClassificationOutcome(
            operation=op,
            classification_artifact_id=created.get("classification_artifact_id"),
            epitaph_artifact_id=created.get("epitaph_artifact_id"),
        )

    # ------------------------------------------------------------- serializers

    @staticmethod
    def _classification_json(result: ClassificationResult) -> str:
        """Serialize the classification payload for the versioned artifact.

        Stores the category (the six-value enum's value), confidence, and the
        uncertain flag. The Epitaph is persisted as its OWN artifact (P14), not
        embedded here.
        """
        return json.dumps(
            {
                "category": result.category.value,
                "confidence": result.confidence,
                "uncertain": result.uncertain,
            }
        )

    @staticmethod
    def _epitaph_json(result: ClassificationResult) -> str:
        """Serialize the Epitaph payload, preserving `visible_facts` exactly."""
        epi = result.epitaph
        return json.dumps(
            {
                "text": epi.text,
                "visible_facts": epi.visible_facts,
                "inferred_intent": epi.inferred_intent,
                "uncertain": epi.uncertain,
            }
        )
