"""Workspace-scoped repository layer (Task 1.2).

Every workspace-owned database operation goes through a `WorkspaceRepository`
bound to a single trusted `workspace_id`. The repository is the ONLY sanctioned
data-access path for workspace-owned tables, and it guarantees:

* Reads/lists are always filtered by the bound workspace. A lookup by object ID
  returns None when the row belongs to a different workspace (safe not-found;
  no existence disclosure, Req 9.9/9.10).
* Creates always stamp the bound workspace_id; a caller cannot inject a
  different workspace_id as "proof" of authorization (Req 9.9).
* Before writing any foreign key, the referenced parent is verified to belong to
  the same workspace; otherwise a CrossWorkspaceError is raised and nothing is
  written. This blocks cross-workspace links for screenshots, content blobs,
  artifacts, artifact versions, source references, consent records, operations,
  and duplicate-screenshot references (safety review Finding 2).

IMPORTANT SCOPE NOTE: this layer assumes `workspace_id` has already been
resolved from a trusted server-side source (the cookie/session middleware in
Task 1.3). The repository does not parse cookies or HTTP; it enforces scoping
GIVEN a trusted workspace id. End-to-end request privacy also depends on the
Task 1.3/1.4 identity + CSRF layers, which are out of scope here.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import models


class QuotaExceededError(Exception):
    """Raised when a per-workspace quota would be exceeded.

    The message is generic and never includes other workspaces' data; the
    `limit` and `kind` describe which quota was hit for a safe, helpful error.
    """

    def __init__(self, kind: str, limit: int) -> None:
        self.kind = kind
        self.limit = limit
        super().__init__(f"{kind} quota exceeded (limit {limit})")


class CrossWorkspaceError(Exception):
    """Raised when an operation would read or link data across workspaces.

    The message is deliberately generic and never includes the other
    workspace's id or any record contents, to avoid leaking private data.
    """

    def __init__(self, detail: str = "resource not available in this workspace") -> None:
        super().__init__(detail)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class WorkspaceRepository:
    """All data access for a single, trusted workspace.

    Construct with a `workspace_id` that was resolved server-side (never taken
    directly from client input). Every method is scoped to that workspace.
    """

    def __init__(self, session: Session, workspace_id: int) -> None:
        if workspace_id is None:
            raise ValueError("workspace_id is required; there is no unscoped repository")
        self._session = session
        self._workspace_id = workspace_id

    @property
    def workspace_id(self) -> int:
        return self._workspace_id

    # ---- internal, workspace-scoped primitives (no public unscoped path) ----

    def _get_owned(self, model, obj_id: int):
        """Fetch a row by id ONLY if it belongs to this workspace, else None.

        Uses a single query filtered by both id and workspace_id so a foreign
        id never returns another workspace's row.
        """
        stmt = select(model).where(
            model.id == obj_id, model.workspace_id == self._workspace_id
        )
        return self._session.scalars(stmt).one_or_none()

    def _require_owned(self, model, obj_id: int):
        row = self._get_owned(model, obj_id)
        if row is None:
            # Same response whether the row is missing or owned by another
            # workspace -> no existence disclosure.
            raise CrossWorkspaceError()
        return row

    # ------------------------------- workspace -------------------------------

    def get_workspace(self) -> models.Workspace | None:
        stmt = select(models.Workspace).where(models.Workspace.id == self._workspace_id)
        return self._session.scalars(stmt).one_or_none()

    # ------------------------------ content_blob -----------------------------

    def create_content_blob(
        self, *, sha256: str, byte_size: int, mime_type: str, storage_path: str
    ) -> models.ContentBlob:
        blob = models.ContentBlob(
            workspace_id=self._workspace_id,
            sha256=sha256,
            byte_size=byte_size,
            mime_type=mime_type,
            storage_path=storage_path,
        )
        self._session.add(blob)
        self._session.flush()
        return blob

    def get_content_blob(self, blob_id: int) -> models.ContentBlob | None:
        return self._get_owned(models.ContentBlob, blob_id)

    def get_content_blob_by_sha256(self, sha256: str) -> models.ContentBlob | None:
        stmt = select(models.ContentBlob).where(
            models.ContentBlob.workspace_id == self._workspace_id,
            models.ContentBlob.sha256 == sha256,
        )
        return self._session.scalars(stmt).one_or_none()

    def list_content_blobs(self) -> list[models.ContentBlob]:
        stmt = select(models.ContentBlob).where(
            models.ContentBlob.workspace_id == self._workspace_id
        )
        return list(self._session.scalars(stmt))

    # ------------------------------- screenshot ------------------------------

    def create_screenshot(
        self,
        *,
        content_blob_id: int,
        original_filename: str,
        state: str = "active",
        is_duplicate_of: int | None = None,
    ) -> models.Screenshot:
        # Verify the referenced blob belongs to this workspace before linking.
        self._require_owned(models.ContentBlob, content_blob_id)
        # Verify the duplicate-of target (if any) is in this workspace too.
        if is_duplicate_of is not None:
            self._require_owned(models.Screenshot, is_duplicate_of)
        shot = models.Screenshot(
            workspace_id=self._workspace_id,
            content_blob_id=content_blob_id,
            original_filename=original_filename,
            state=state,
            is_duplicate_of=is_duplicate_of,
        )
        self._session.add(shot)
        self._session.flush()
        return shot

    def get_screenshot(self, screenshot_id: int) -> models.Screenshot | None:
        return self._get_owned(models.Screenshot, screenshot_id)

    def list_screenshots(self, state: str | None = None) -> list[models.Screenshot]:
        stmt = select(models.Screenshot).where(
            models.Screenshot.workspace_id == self._workspace_id
        )
        if state is not None:
            stmt = stmt.where(models.Screenshot.state == state)
        return list(self._session.scalars(stmt))

    def set_screenshot_state(self, screenshot_id: int, state: str) -> models.Screenshot:
        shot = self._require_owned(models.Screenshot, screenshot_id)
        shot.state = state
        self._session.flush()
        return shot

    # -------------------------------- artifact -------------------------------

    def create_artifact(self, *, kind: str) -> models.Artifact:
        art = models.Artifact(workspace_id=self._workspace_id, kind=kind)
        self._session.add(art)
        self._session.flush()
        return art

    def get_artifact(self, artifact_id: int) -> models.Artifact | None:
        return self._get_owned(models.Artifact, artifact_id)

    def list_artifacts(self, kind: str | None = None) -> list[models.Artifact]:
        stmt = select(models.Artifact).where(
            models.Artifact.workspace_id == self._workspace_id
        )
        if kind is not None:
            stmt = stmt.where(models.Artifact.kind == kind)
        return list(self._session.scalars(stmt))

    def set_current_version(
        self, artifact_id: int, version_id: int
    ) -> models.Artifact:
        art = self._require_owned(models.Artifact, artifact_id)
        version = self._require_owned(models.ArtifactVersion, version_id)
        if version.artifact_id != art.id:
            raise CrossWorkspaceError()
        art.current_version_id = version.id
        self._session.flush()
        return art

    # ---------------------------- artifact_version ---------------------------

    def add_artifact_version(
        self, *, artifact_id: int, version_number: int, origin: str, content_json: str
    ) -> models.ArtifactVersion:
        # Verify the parent artifact belongs to this workspace before linking.
        self._require_owned(models.Artifact, artifact_id)
        version = models.ArtifactVersion(
            workspace_id=self._workspace_id,
            artifact_id=artifact_id,
            version_number=version_number,
            origin=origin,
            content_json=content_json,
        )
        self._session.add(version)
        self._session.flush()
        return version

    def get_artifact_version(self, version_id: int) -> models.ArtifactVersion | None:
        return self._get_owned(models.ArtifactVersion, version_id)

    def list_artifact_versions(self, artifact_id: int) -> list[models.ArtifactVersion]:
        # Ensure the artifact is owned before listing its versions.
        self._require_owned(models.Artifact, artifact_id)
        stmt = select(models.ArtifactVersion).where(
            models.ArtifactVersion.workspace_id == self._workspace_id,
            models.ArtifactVersion.artifact_id == artifact_id,
        )
        return list(self._session.scalars(stmt))

    # ---------------------------- source_reference ---------------------------

    def add_source_reference(
        self, *, artifact_id: int, screenshot_id: int
    ) -> models.SourceReference:
        # Both the artifact and the screenshot must belong to this workspace.
        self._require_owned(models.Artifact, artifact_id)
        self._require_owned(models.Screenshot, screenshot_id)
        ref = models.SourceReference(
            workspace_id=self._workspace_id,
            artifact_id=artifact_id,
            screenshot_id=screenshot_id,
        )
        self._session.add(ref)
        self._session.flush()
        return ref

    def list_source_references(self, artifact_id: int) -> list[models.SourceReference]:
        self._require_owned(models.Artifact, artifact_id)
        stmt = select(models.SourceReference).where(
            models.SourceReference.workspace_id == self._workspace_id,
            models.SourceReference.artifact_id == artifact_id,
        )
        return list(self._session.scalars(stmt))

    # ----------------------------- consent_record ----------------------------

    def set_consent(
        self, *, screenshot_id: int, granted: bool
    ) -> models.ConsentRecord:
        self._require_owned(models.Screenshot, screenshot_id)
        stmt = select(models.ConsentRecord).where(
            models.ConsentRecord.workspace_id == self._workspace_id,
            models.ConsentRecord.screenshot_id == screenshot_id,
        )
        record = self._session.scalars(stmt).one_or_none()
        if record is None:
            record = models.ConsentRecord(
                workspace_id=self._workspace_id,
                screenshot_id=screenshot_id,
                granted=granted,
                granted_at=_utcnow() if granted else None,
            )
            self._session.add(record)
        else:
            record.granted = granted
            record.granted_at = _utcnow() if granted else None
        self._session.flush()
        return record

    def get_consent(self, screenshot_id: int) -> models.ConsentRecord | None:
        self._require_owned(models.Screenshot, screenshot_id)
        stmt = select(models.ConsentRecord).where(
            models.ConsentRecord.workspace_id == self._workspace_id,
            models.ConsentRecord.screenshot_id == screenshot_id,
        )
        return self._session.scalars(stmt).one_or_none()

    # ------------------------------- operation -------------------------------

    def create_operation(
        self, *, kind: str, status: str = "pending", artifact_id: int | None = None
    ) -> models.Operation:
        if artifact_id is not None:
            self._require_owned(models.Artifact, artifact_id)
        op = models.Operation(
            workspace_id=self._workspace_id,
            kind=kind,
            status=status,
            artifact_id=artifact_id,
        )
        self._session.add(op)
        self._session.flush()
        return op

    def get_operation(self, operation_id: int) -> models.Operation | None:
        return self._get_owned(models.Operation, operation_id)

    def set_operation_status(
        self, operation_id: int, status: str, error_detail: str | None = None
    ) -> models.Operation:
        op = self._require_owned(models.Operation, operation_id)
        op.status = status
        if error_detail is not None:
            op.error_detail = error_detail
        op.updated_at = _utcnow()
        self._session.flush()
        return op

    # ------------------------------- quotas ----------------------------------

    def count_screenshots(self) -> int:
        """Number of screenshots owned by this workspace (quota counter)."""
        from sqlalchemy import func

        stmt = select(func.count()).select_from(models.Screenshot).where(
            models.Screenshot.workspace_id == self._workspace_id
        )
        return int(self._session.scalar(stmt) or 0)

    def count_ai_operations(self) -> int:
        """Number of AI operations (classify/study_note/redo) for this workspace.

        Counts the operation rows that represent metered AI calls, used to
        enforce the per-workspace AI-call quota.
        """
        from sqlalchemy import func

        ai_kinds = ("classify", "study_note", "redo")
        stmt = select(func.count()).select_from(models.Operation).where(
            models.Operation.workspace_id == self._workspace_id,
            models.Operation.kind.in_(ai_kinds),
        )
        return int(self._session.scalar(stmt) or 0)

    def ensure_screenshot_quota(self, limit: int, adding: int = 1) -> None:
        """Raise QuotaExceededError if adding `adding` screenshots exceeds limit."""
        if self.count_screenshots() + adding > limit:
            raise QuotaExceededError("screenshot", limit)

    def ensure_ai_quota(self, limit: int, adding: int = 1) -> None:
        """Raise QuotaExceededError if adding `adding` AI calls exceeds limit."""
        if self.count_ai_operations() + adding > limit:
            raise QuotaExceededError("ai_call", limit)
