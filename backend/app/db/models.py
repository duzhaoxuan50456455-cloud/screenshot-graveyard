"""SQLAlchemy 2.x ORM models for Screenshot Graveyard.

Schema follows the approved design exactly. Every non-`workspace` table carries a
`workspace_id` FK and is indexed on it (Req 12.6, 9.9). Append-only history and
dedup are enforced by UNIQUE constraints (INV-5/INV-6 structural support, A3).

This module defines schema only. Business logic and invariant enforcement live in
the service layer (later tasks); there is no query/CRUD logic here.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Workspace(Base):
    __tablename__ = "workspace"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Only sha256(token) is stored; the raw token lives only in the cookie.
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


class ContentBlob(Base):
    """Deduplicated original-bytes record; one row per unique content per workspace."""

    __tablename__ = "content_blob"
    __table_args__ = (
        UniqueConstraint("workspace_id", "sha256", name="content_blob_workspace_sha256"),
        Index("ix_content_blob_workspace_id", "workspace_id"),
        Index("ix_content_blob_workspace_sha256_lookup", "workspace_id", "sha256"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspace.id"), nullable=False
    )
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    mime_type: Mapped[str] = mapped_column(String(64), nullable=False)
    # Opaque content-addressed path on the private volume (never web-served).
    storage_path: Mapped[str] = mapped_column(String(512), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


class Screenshot(Base):
    __tablename__ = "screenshot"
    __table_args__ = (
        Index("ix_screenshot_workspace_id", "workspace_id"),
        Index("ix_screenshot_workspace_state", "workspace_id", "state"),
    )

    # Stable Screenshot ID — never changes for the life of the record.
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspace.id"), nullable=False
    )
    content_blob_id: Mapped[int] = mapped_column(
        ForeignKey("content_blob.id"), nullable=False
    )
    original_filename: Mapped[str] = mapped_column(String(512), nullable=False)
    # state ∈ {active, archived}
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    is_duplicate_of: Mapped[int | None] = mapped_column(
        ForeignKey("screenshot.id"), nullable=True
    )
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


class Artifact(Base):
    __tablename__ = "artifact"
    __table_args__ = (Index("ix_artifact_workspace_id", "workspace_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspace.id"), nullable=False
    )
    # kind ∈ {classification, epitaph, study_note}
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    # Pointer to the current version. Nullable until the first version exists.
    # use_alter avoids a circular-dependency cycle with artifact_version at DDL time.
    current_version_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "artifact_version.id",
            use_alter=True,
            name="fk_artifact_current_version_id_artifact_version",
        ),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


class ArtifactVersion(Base):
    """Append-only (INSERT-only) version history. No UPDATE path to content_json."""

    __tablename__ = "artifact_version"
    __table_args__ = (
        UniqueConstraint(
            "artifact_id", "version_number", name="artifact_version_artifact_version"
        ),
        Index("ix_artifact_version_workspace_id", "workspace_id"),
        Index("ix_artifact_version_artifact_id", "artifact_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspace.id"), nullable=False
    )
    artifact_id: Mapped[int] = mapped_column(
        ForeignKey("artifact.id"), nullable=False
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    # origin ∈ {ai_generate, ai_redo, manual_edit}
    origin: Mapped[str] = mapped_column(String(16), nullable=False)
    # Versioned payload (classification / epitaph / study_note). Stored as text
    # JSON for SQLite/Postgres portability; shape enforced by Pydantic at the
    # service boundary (later tasks).
    content_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


class SourceReference(Base):
    """Provenance attached to the artifact so it survives every version/redo (INV-7)."""

    __tablename__ = "source_reference"
    __table_args__ = (
        UniqueConstraint(
            "artifact_id", "screenshot_id", name="source_reference_artifact_screenshot"
        ),
        Index("ix_source_reference_workspace_id", "workspace_id"),
        Index("ix_source_reference_artifact_id", "artifact_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspace.id"), nullable=False
    )
    artifact_id: Mapped[int] = mapped_column(
        ForeignKey("artifact.id"), nullable=False
    )
    screenshot_id: Mapped[int] = mapped_column(
        ForeignKey("screenshot.id"), nullable=False
    )


class ConsentRecord(Base):
    __tablename__ = "consent_record"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "screenshot_id", name="consent_record_workspace_screenshot"
        ),
        Index("ix_consent_record_workspace_id", "workspace_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspace.id"), nullable=False
    )
    screenshot_id: Mapped[int] = mapped_column(
        ForeignKey("screenshot.id"), nullable=False
    )
    granted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    granted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class Operation(Base):
    __tablename__ = "operation"
    __table_args__ = (
        Index("ix_operation_workspace_id", "workspace_id"),
        Index("ix_operation_workspace_status", "workspace_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspace.id"), nullable=False
    )
    # kind ∈ {classify, study_note, restore, delivery(P1)}
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    # status ∈ {pending, successful, failed, uncertain}
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    artifact_id: Mapped[int | None] = mapped_column(
        ForeignKey("artifact.id"), nullable=True
    )
    # Idempotency key for external writes (P1); unused stub in P0.
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )
