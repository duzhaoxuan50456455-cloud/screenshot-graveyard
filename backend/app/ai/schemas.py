"""Pydantic result schemas for the AI boundary (Task 2.1).

These models are the ONLY shape the rest of the system accepts from any
`AIAdapter` implementation. Every adapter output MUST validate against them
before it may be persisted; a validation failure raises (pydantic
ValidationError) and the calling service marks the operation `failed`/`uncertain`
WITHOUT touching source bytes (Req 13.2, 13.3; INV-4). Later Phase-2 services
(2.5 classification, 2.7 study notes) enforce that persistence gate — this task
only defines the contracts.

Design source: `.kiro/specs/screenshot-graveyard/design.md`
("AIAdapter Protocol and the fake adapter"), Requirements 13.1-13.4, 4.1, 5.1-5.4.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class Category(str, Enum):
    """The six fixed classification categories (Req 4.1).

    Values are the exact human-readable labels from the requirements glossary;
    using a str-Enum makes "exactly one of six" structurally enforceable — any
    other value fails validation.
    """

    STUDY_KNOWLEDGE = "Study/Knowledge"
    REFERENCE_DOCUMENT = "Reference/Document"
    SHOPPING_INSPIRATION = "Shopping/Inspiration"
    MEMORY_ENTERTAINMENT = "Memory/Entertainment"
    UNKNOWN_NEEDS_REVIEW = "Unknown/Needs Review"
    POTENTIALLY_SENSITIVE = "Potentially Sensitive"


class EpitaphPayload(BaseModel):
    """A short, honest purpose caption produced in the SAME AI call as the
    classification (Req 5.1).

    The schema structurally separates a VISIBLE FACT from an INFERRED INTENT so
    an adapter cannot collapse the two, and carries an `uncertain` flag so an
    un-inferrable purpose is represented rather than fabricated (Req 5.2-5.4).
    The System must never assert the user's original reason as a known fact
    (Req 5.4) — `inferred_intent` is explicitly optional and advisory.
    """

    model_config = ConfigDict(extra="forbid")

    text: str = Field(
        min_length=1,
        description="The short displayed caption (editable later by the user).",
    )
    visible_facts: str = Field(
        default="",
        description="What is literally visible in the screenshot (no inference).",
    )
    inferred_intent: str | None = Field(
        default=None,
        description=(
            "An explicitly tentative guess at why it may have been saved; never "
            "stated as a known fact. None when no intent is inferred."
        ),
    )
    uncertain: bool = Field(
        default=False,
        description="True when the purpose cannot be reasonably inferred (Req 5.3).",
    )


class ClassificationResult(BaseModel):
    """Structured output of `classify_and_epitaph` (Req 4.1, 5.1, 13.2).

    `category` is constrained to the six-value enum; `confidence` is a bounded
    [0.0, 1.0] score; `uncertain` lets the adapter signal low confidence so the
    service can mark the operation `uncertain` rather than forcing success
    (INV-4; the enforcement itself lives in Task 2.5). The Epitaph is embedded
    because it is produced in the same call (Req 5.1).
    """

    model_config = ConfigDict(extra="forbid")

    category: Category
    confidence: float = Field(ge=0.0, le=1.0)
    uncertain: bool = False
    epitaph: EpitaphPayload


class StudyNoteResult(BaseModel):
    """Structured output of `study_note` (Req 6.4, 6.5, 6.7, 13.2).

    Mirrors the required Study_Note sections exactly: Topic, Main concepts,
    Extracted code, Code explanations, Important pitfalls, Uncertain/ambiguous
    content, and References to source Screenshot IDs. Code is carried verbatim
    as strings so indentation/symbols are preserved faithfully (Req 6.5); the
    service layer (Task 2.7) is responsible for actually honoring that fidelity.
    """

    model_config = ConfigDict(extra="forbid")

    topic: str = Field(min_length=1)
    main_concepts: list[str] = Field(default_factory=list)
    extracted_code: list[str] = Field(default_factory=list)
    code_explanations: list[str] = Field(default_factory=list)
    pitfalls: list[str] = Field(default_factory=list)
    uncertain_content: list[str] = Field(default_factory=list)
    references: list[int] = Field(
        default_factory=list,
        description="Source Screenshot IDs this note was generated from (INV-7).",
    )
