"""Read-only, workspace-scoped comparison of retained artifact versions (Task 3.4).

Comparison deliberately operates on the stored JSON object rather than trying to
reconstruct a product-specific document model.  That keeps Classification,
Epitaph, and Study Note versions on the same small, deterministic API while
preserving the append-only boundary: it issues no writes, never moves an
artifact's current-version pointer, and never invokes an AI adapter.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.db.repository import WorkspaceRepository

_MAX_CHANGED_FIELDS = 64
_MAX_RENDERED_VALUE_CHARS = 2_048
_MAX_RENDERED_FIELD_CHARS = 128
# Artifact payloads are normally far smaller (schema-validated Classification,
# Epitaph, and Study Note objects).  This cap prevents a malformed/legacy row
# from making a read-only comparison endpoint parse an unbounded database value.
_MAX_CONTENT_JSON_CHARS = 4 * 1024 * 1024


class ArtifactComparisonError(Exception):
    """Base comparison error with safe, non-private messages."""


class ArtifactComparisonNotFoundError(ArtifactComparisonError):
    """Missing, foreign, cross-artifact, and invalid version IDs are identical."""


class ArtifactComparisonUnavailableError(ArtifactComparisonError):
    """Stored content or the database could not safely be read."""


@dataclass(frozen=True)
class ArtifactVersionComparison:
    artifact_id: int
    artifact_kind: str
    left_version: dict[str, int | str]
    right_version: dict[str, int | str]
    differences: dict[str, list[dict]]
    truncated: bool
    omitted_field_count: int

    def as_dict(self) -> dict:
        return {
            "artifactId": self.artifact_id,
            "artifactKind": self.artifact_kind,
            "leftVersion": self.left_version,
            "rightVersion": self.right_version,
            "differences": self.differences,
            "truncated": self.truncated,
            "omittedFieldCount": self.omitted_field_count,
        }


def _reject_nonstandard_constant(value: str):
    raise ValueError(f"unsupported JSON constant: {value}")


def _load_object(content_json: str) -> dict:
    if not isinstance(content_json, str) or len(content_json) > _MAX_CONTENT_JSON_CHARS:
        raise ArtifactComparisonUnavailableError("comparison unavailable")
    try:
        value = json.loads(content_json, parse_constant=_reject_nonstandard_constant)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ArtifactComparisonUnavailableError("comparison unavailable") from exc
    if not isinstance(value, dict):
        raise ArtifactComparisonUnavailableError("comparison unavailable")
    return value


@dataclass(frozen=True)
class _RenderedValue:
    response: dict[str, object]
    digest: str


def _render_value(value: object) -> _RenderedValue:
    """Canonical, bounded representation suitable for a comparison response.

    ``JSONEncoder.iterencode`` lets comparison retain a digest of the COMPLETE
    value while retaining only a small preview for the response.  Therefore two
    long values that share a preview but differ later remain ``changed``.
    """
    try:
        chunks = json.JSONEncoder(
            ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).iterencode(value)
    except (TypeError, ValueError) as exc:
        raise ArtifactComparisonUnavailableError("comparison unavailable") from exc
    digest = hashlib.sha256()
    preview: list[str] = []
    preview_length = 0
    rendered_length = 0
    try:
        for chunk in chunks:
            digest.update(chunk.encode("utf-8"))
            rendered_length += len(chunk)
            remaining = _MAX_RENDERED_VALUE_CHARS - preview_length
            if remaining > 0:
                piece = chunk[:remaining]
                preview.append(piece)
                preview_length += len(piece)
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ArtifactComparisonUnavailableError("comparison unavailable") from exc
    truncated = rendered_length > _MAX_RENDERED_VALUE_CHARS
    response: dict[str, object] = {
        "json": "".join(preview),
        "truncated": truncated,
    }
    if truncated:
        response["originalCharacterCount"] = rendered_length
        response["sha256"] = digest.hexdigest()
    return _RenderedValue(response=response, digest=digest.hexdigest())


def _render_field(field: str) -> dict[str, object]:
    if len(field) <= _MAX_RENDERED_FIELD_CHARS:
        return {"field": field}
    return {
        "field": field[:_MAX_RENDERED_FIELD_CHARS],
        "fieldTruncated": True,
        "originalFieldCharacterCount": len(field),
        "fieldSha256": hashlib.sha256(field.encode("utf-8")).hexdigest(),
    }


class ArtifactComparisonService:
    """Compare two versions of one owned artifact without mutating any state."""

    def __init__(self, session: Session, workspace_id: int) -> None:
        if workspace_id is None:
            raise ValueError("workspace_id is required")
        self._repo = WorkspaceRepository(session, workspace_id)

    def compare(
        self, artifact_id: int, left_version_id: int, right_version_id: int
    ) -> ArtifactVersionComparison:
        if (
            type(artifact_id) is not int
            or type(left_version_id) is not int
            or type(right_version_id) is not int
            or artifact_id <= 0
            or left_version_id <= 0
            or right_version_id <= 0
        ):
            raise ArtifactComparisonNotFoundError("artifact not found")
        try:
            artifact = self._repo.get_artifact(artifact_id)
            if artifact is None:
                raise ArtifactComparisonNotFoundError("artifact not found")
            left = self._repo.get_artifact_version(left_version_id)
            right = self._repo.get_artifact_version(right_version_id)
            if (
                left is None
                or right is None
                or left.artifact_id != artifact.id
                or right.artifact_id != artifact.id
            ):
                raise ArtifactComparisonNotFoundError("artifact not found")
            return self._build(artifact, left, right)
        except ArtifactComparisonError:
            raise
        except SQLAlchemyError as exc:
            raise ArtifactComparisonUnavailableError("comparison unavailable") from exc

    @staticmethod
    def _build(artifact, left, right) -> ArtifactVersionComparison:
        left_content = _load_object(left.content_json)
        right_content = _load_object(right.content_json)
        differences: dict[str, list[dict]] = {"added": [], "removed": [], "changed": []}
        emitted = 0
        omitted = 0
        for field in sorted(set(left_content) | set(right_content)):
            if field not in left_content:
                kind = "added"
            elif field not in right_content:
                kind = "removed"
            else:
                before = _render_value(left_content[field])
                after = _render_value(right_content[field])
                if before.digest == after.digest:
                    continue
                kind = "changed"

            if emitted >= _MAX_CHANGED_FIELDS:
                omitted += 1
                continue
            item = _render_field(field)
            if kind != "added":
                item["before"] = _render_value(left_content[field]).response
            if kind != "removed":
                item["after"] = _render_value(right_content[field]).response
            differences[kind].append(item)
            emitted += 1

        return ArtifactVersionComparison(
            artifact_id=artifact.id,
            artifact_kind=artifact.kind,
            left_version={
                "versionId": left.id,
                "versionNumber": left.version_number,
                "origin": left.origin,
            },
            right_version={
                "versionId": right.id,
                "versionNumber": right.version_number,
                "origin": right.origin,
            },
            differences=differences,
            truncated=omitted > 0,
            omitted_field_count=omitted,
        )
