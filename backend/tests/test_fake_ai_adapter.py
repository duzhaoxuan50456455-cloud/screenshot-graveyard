"""Focused behavior tests for the deterministic FakeAIAdapter (Task 2.2)."""

from __future__ import annotations

import inspect
from typing import get_type_hints

import pytest
from pydantic import ValidationError

from app.ai import (
    AIAdapter,
    AIAdapterTimeoutError,
    Category,
    ClassificationResult,
    FakeAIAdapter,
    FakeAIAdapterMode,
    StudyNoteResult,
)


_IMAGES = [(7, b"not-an-image", "image/png"), (11, b"also-not-an-image", "image/jpeg")]


def test_fake_method_signatures_and_return_annotations_match_the_protocol():
    classify = inspect.signature(FakeAIAdapter.classify_and_epitaph)
    assert list(classify.parameters) == ["self", "image_bytes", "mime"]
    assert get_type_hints(FakeAIAdapter.classify_and_epitaph)["return"] is ClassificationResult

    study_note = inspect.signature(FakeAIAdapter.study_note)
    assert list(study_note.parameters) == ["self", "images"]
    assert get_type_hints(FakeAIAdapter.study_note)["return"] is StudyNoteResult


def test_default_mode_is_schema_valid_deterministic_and_protocol_conforming():
    adapter = FakeAIAdapter()
    assert isinstance(adapter, AIAdapter)

    first = adapter.classify_and_epitaph(b"one", "image/png")
    second = adapter.classify_and_epitaph(b"different", "image/webp")
    assert isinstance(first, ClassificationResult)
    assert first == second
    assert first.category is Category.REFERENCE_DOCUMENT
    assert first.epitaph.visible_facts == "Synthetic adapter does not interpret image content."
    assert first.epitaph.inferred_intent is None

    note = adapter.study_note(_IMAGES)
    assert isinstance(note, StudyNoteResult)
    assert note.references == [7, 11]
    assert note.main_concepts == ["Deterministic test fixture"]


def test_default_study_note_references_are_deterministic_and_preserve_input_order():
    adapter = FakeAIAdapter()
    assert adapter.study_note(_IMAGES).references == [7, 11]
    assert adapter.study_note(list(reversed(_IMAGES))).references == [11, 7]


@pytest.mark.parametrize("method", ["classify", "study_note"])
def test_invalid_mode_returns_malformed_provider_output_for_service_validation(method):
    adapter = FakeAIAdapter(FakeAIAdapterMode.INVALID)
    if method == "classify":
        raw = adapter.classify_and_epitaph(b"fixture", "image/png")
        model = ClassificationResult
    else:
        raw = adapter.study_note(_IMAGES)
        model = StudyNoteResult

    assert isinstance(raw, dict)
    with pytest.raises(ValidationError):
        model.model_validate(raw)


@pytest.mark.parametrize("method", ["classify", "study_note"])
def test_timeout_mode_raises_established_timeout_error_without_sleep(method):
    adapter = FakeAIAdapter(FakeAIAdapterMode.TIMEOUT)
    with pytest.raises(AIAdapterTimeoutError, match="simulated AI adapter timeout"):
        if method == "classify":
            adapter.classify_and_epitaph(b"fixture", "image/png")
        else:
            adapter.study_note(_IMAGES)


def test_uncertain_mode_explicitly_represents_uncertainty_without_claiming_intent():
    adapter = FakeAIAdapter(FakeAIAdapterMode.UNCERTAIN)
    classification = adapter.classify_and_epitaph(b"fixture", "image/png")
    note = adapter.study_note(_IMAGES)

    assert classification.category is Category.UNKNOWN_NEEDS_REVIEW
    assert classification.confidence == 0.0
    assert classification.uncertain is True
    assert classification.epitaph.uncertain is True
    assert classification.epitaph.inferred_intent is None
    assert note.uncertain_content == [
        "Synthetic adapter does not interpret image content."
    ]
    assert note.references == [7, 11]
