"""Contract tests for the AIAdapter Protocol + result schemas (Task 2.1).

Asserts the Protocol methods and Pydantic schemas EXIST with the agreed shape and
that well-formed sample output validates — and, as the structural basis for the
P12 schema-validation gate, that malformed output is REJECTED. No real model and
no network: a tiny in-test stub implements the Protocol so conformance is checked
without any paid provider call.

Requirements: 13.1, 13.2, 13.4, P12.
"""

from __future__ import annotations

import inspect
from typing import get_type_hints

import pytest
from pydantic import ValidationError

from app import config
from app.ai import (
    AIAdapter,
    AIAdapterError,
    AIAdapterTimeoutError,
    Category,
    ClassificationResult,
    EpitaphPayload,
    StudyNoteResult,
)
from app.ai.adapter import default_timeout_seconds


# --------------------------------------------------------------- sample output

def _sample_classification() -> dict:
    return {
        "category": "Study/Knowledge",
        "confidence": 0.82,
        "uncertain": False,
        "epitaph": {
            "text": "C++ lecture: function parameters and return values.",
            "visible_facts": "Slide titled 'Functions' with C++ code.",
            "inferred_intent": "Possibly saved to revise for an exam.",
            "uncertain": False,
        },
    }


def _sample_study_note() -> dict:
    return {
        "topic": "C++ Functions",
        "main_concepts": ["parameters", "return values"],
        "extracted_code": ["int add(int a, int b) {\n    return a + b;\n}"],
        "code_explanations": ["Adds two ints and returns the sum."],
        "pitfalls": ["Forgetting the return type."],
        "uncertain_content": [],
        "references": [1, 2, 3],
    }


# --------------------------------------------------------------- schema existence/validation

def test_six_fixed_categories_exact():
    assert {c.value for c in Category} == {
        "Study/Knowledge",
        "Reference/Document",
        "Shopping/Inspiration",
        "Memory/Entertainment",
        "Unknown/Needs Review",
        "Potentially Sensitive",
    }


def test_well_formed_classification_validates():
    result = ClassificationResult.model_validate(_sample_classification())
    assert result.category is Category.STUDY_KNOWLEDGE
    assert 0.0 <= result.confidence <= 1.0
    assert isinstance(result.epitaph, EpitaphPayload)
    assert result.epitaph.text


def test_well_formed_study_note_validates():
    note = StudyNoteResult.model_validate(_sample_study_note())
    assert note.topic == "C++ Functions"
    assert note.references == [1, 2, 3]
    # Code preserved verbatim (indentation + symbols) as a string.
    assert note.extracted_code[0].startswith("int add(")
    assert "\n    return a + b;" in note.extracted_code[0]


def test_epitaph_uncertainty_is_representable():
    payload = EpitaphPayload.model_validate(
        {"text": "Purpose unknown.", "uncertain": True}
    )
    assert payload.uncertain is True
    assert payload.inferred_intent is None  # intent not asserted when unknown


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(category="Totally Made Up"),      # invalid enum
        lambda d: d.update(confidence=1.7),                  # out of [0,1]
        lambda d: d.update(confidence=-0.1),                 # out of [0,1]
        lambda d: d.pop("epitaph"),                          # missing epitaph
        lambda d: d.update(extra_field="nope"),              # extra forbidden
    ],
)
def test_malformed_classification_rejected(mutate):
    """Basis of the P12 validation gate: non-conforming output never validates."""
    bad = _sample_classification()
    mutate(bad)
    with pytest.raises(ValidationError):
        ClassificationResult.model_validate(bad)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(topic=""),                        # empty topic
        lambda d: d.update(references=["not-an-int"]),       # bad ref type
        lambda d: d.update(unexpected="x"),                  # extra forbidden
    ],
)
def test_malformed_study_note_rejected(mutate):
    bad = _sample_study_note()
    mutate(bad)
    with pytest.raises(ValidationError):
        StudyNoteResult.model_validate(bad)


# --------------------------------------------------------------- protocol shape

def test_adapter_protocol_methods_exist_with_expected_signatures():
    # Methods exist on the Protocol.
    assert hasattr(AIAdapter, "classify_and_epitaph")
    assert hasattr(AIAdapter, "study_note")
    # Signatures match the design contract.
    ce = inspect.signature(AIAdapter.classify_and_epitaph)
    assert list(ce.parameters)[1:] == ["image_bytes", "mime"]
    assert get_type_hints(AIAdapter.classify_and_epitaph)["return"] is ClassificationResult
    sn = inspect.signature(AIAdapter.study_note)
    assert list(sn.parameters)[1:] == ["images"]
    assert get_type_hints(AIAdapter.study_note)["return"] is StudyNoteResult


def test_default_timeout_is_positive_and_configurable():
    assert default_timeout_seconds() == config.AI_ADAPTER_TIMEOUT_SECONDS
    assert default_timeout_seconds() > 0


@pytest.mark.parametrize("raw", ["0", "-1", "nan", "inf", "not-a-number"])
def test_timeout_configuration_rejects_invalid_values(raw):
    with pytest.raises(RuntimeError, match="AI_ADAPTER_TIMEOUT_SECONDS"):
        config._positive_finite_float(raw, setting="AI_ADAPTER_TIMEOUT_SECONDS")


def test_a_conforming_stub_satisfies_the_runtime_checkable_protocol():
    class _StubAdapter:
        def classify_and_epitaph(self, image_bytes: bytes, mime: str) -> ClassificationResult:
            return ClassificationResult.model_validate(_sample_classification())

        def study_note(self, images: list[tuple[int, bytes, str]]) -> StudyNoteResult:
            return StudyNoteResult.model_validate(_sample_study_note())

    stub = _StubAdapter()
    assert isinstance(stub, AIAdapter)  # runtime_checkable structural conformance
    # And the stub's outputs validate against the schemas.
    assert isinstance(stub.classify_and_epitaph(b"x", "image/png"), ClassificationResult)
    assert stub.study_note([(1, b"x", "image/png")]).references == [1, 2, 3]


def test_adapter_error_types_hierarchy():
    assert issubclass(AIAdapterTimeoutError, AIAdapterError)
    assert issubclass(AIAdapterError, Exception)
