"""Mocked contract tests for the opt-in real vision adapter (Task 2.9a).

No test here imports a real SDK client or sends a request.  The final smoke test
is opt-in and uses a generated image only.
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest

from app import config
from app.ai.adapter import AIAdapterError, AIAdapterTimeoutError
from app.ai.fake_adapter import FakeAIAdapter
from app.ai.openai_vision_adapter import (
    AIInputLimitError,
    AIProviderConfigurationError,
    OpenAIVisionAdapter,
)
from app.ai.provider import build_configured_adapter
from app.ai.schemas import Category, ClassificationResult, EpitaphPayload, StudyNoteResult


class _Response:
    def __init__(self, parsed=None, *, status="completed"):
        self.output_parsed = parsed
        self.status = status


class _Responses:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.response


class _Client:
    def __init__(self, response=None, error=None):
        self.responses = _Responses(response, error)


def _classification():
    return ClassificationResult(
        category=Category.STUDY_KNOWLEDGE,
        confidence=0.8,
        uncertain=False,
        epitaph=EpitaphPayload(
            text="A lecture slide.", visible_facts="A title and bullet points.", inferred_intent=None
        ),
    )


def _adapter(response=None, error=None, **kwargs):
    client = _Client(response, error)
    return OpenAIVisionAdapter(client=client, model="gpt-4.1-mini", **kwargs), client


def test_fake_default_remains_network_free(monkeypatch):
    monkeypatch.setattr(config, "AI_PROVIDER", "fake")
    assert isinstance(build_configured_adapter(), FakeAIAdapter)


def test_openai_mode_fails_closed_without_credentials(monkeypatch):
    monkeypatch.setattr(config, "OPENAI_API_KEY", None)
    with pytest.raises(AIProviderConfigurationError, match="configuration invalid"):
        OpenAIVisionAdapter.from_config()


def test_provider_seam_never_falls_back_to_fake_in_real_mode(monkeypatch):
    monkeypatch.setattr(config, "AI_PROVIDER", "openai")
    monkeypatch.setattr(config, "OPENAI_API_KEY", "")
    with pytest.raises(AIProviderConfigurationError, match="configuration invalid"):
        build_configured_adapter()


def test_real_sdk_factory_enforces_zero_retries_and_configured_deadline(monkeypatch):
    recorded = {}

    class SDKClient:
        def __init__(self, **kwargs):
            recorded.update(kwargs)

    monkeypatch.setattr(config, "OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(config, "OPENAI_MODEL", "gpt-4.1-mini")
    monkeypatch.setattr(config, "AI_ADAPTER_TIMEOUT_SECONDS", 17.5)
    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=SDKClient))
    adapter = OpenAIVisionAdapter.from_config()
    assert isinstance(adapter.client, SDKClient)
    assert recorded == {"api_key": "test-key", "timeout": 17.5, "max_retries": 0}


def test_classification_uses_responses_parse_with_bounded_structured_request():
    adapter, client = _adapter(_Response(_classification()), classification_max_output_tokens=37)
    assert adapter.classify_and_epitaph(b"png-bytes", "image/png") == _classification()
    call = client.responses.calls[0]
    assert call["model"] == "gpt-4.1-mini"
    assert call["text_format"] is ClassificationResult
    assert call["store"] is False
    assert call["max_output_tokens"] == 37
    image = call["input"][1]["content"][0]
    assert image["image_url"].startswith("data:image/png;base64,")
    assert b"png-bytes" not in repr(call).encode()


def test_study_note_preserves_prompt_image_and_reference_order():
    result = StudyNoteResult(topic="Loops", references=[9, 2], extracted_code=["for x in y:\n    pass"])
    adapter, client = _adapter(_Response(result))
    assert adapter.study_note([(9, b"first", "image/png"), (2, b"second", "image/jpeg")]) == result
    content = client.responses.calls[0]["input"][1]["content"]
    assert [part["text"] for part in content if part["type"] == "input_text"] == [
        "Screenshot ID: 9", "Screenshot ID: 2"
    ]
    assert [part["image_url"].split(";", 1)[0] for part in content if part["type"] == "input_image"] == [
        "data:image/png", "data:image/jpeg"
    ]


def test_study_note_rejects_provider_reference_reordering():
    adapter, _ = _adapter(_Response(StudyNoteResult(topic="x", references=[2, 9])))
    with pytest.raises(AIAdapterError, match="output failed validation"):
        adapter.study_note([(9, b"first", "image/png"), (2, b"second", "image/png")])


def test_image_and_aggregate_bounds_reject_without_provider_call():
    adapter, client = _adapter(_Response(_classification()), max_input_image_bytes=3, max_study_input_bytes=4)
    with pytest.raises(AIInputLimitError):
        adapter.classify_and_epitaph(b"four", "image/png")
    with pytest.raises(AIInputLimitError):
        adapter.study_note([(1, b"abc", "image/png"), (2, b"def", "image/png")])
    assert client.responses.calls == []


@pytest.mark.parametrize("status, parsed", [("incomplete", _classification()), ("completed", None)])
def test_incomplete_or_refused_provider_response_is_safely_rejected(status, parsed):
    adapter, _ = _adapter(_Response(parsed, status=status))
    with pytest.raises(AIAdapterError):
        adapter.classify_and_epitaph(b"ok", "image/png")


def test_malformed_provider_output_is_independently_validated():
    adapter, _ = _adapter(_Response({"category": "invented", "confidence": 2}))
    with pytest.raises(AIAdapterError, match="output failed validation"):
        adapter.classify_and_epitaph(b"ok", "image/png")


@pytest.mark.parametrize(
    "name, timeout",
    [
        ("APITimeoutError", True),
        ("ReadTimeout", True),
        ("AuthenticationError", False),
        ("RateLimitError", False),
        ("InternalServerError", False),
        ("APIConnectionError", False),
    ],
)
def test_provider_failures_are_sanitized_and_timeout_is_distinct(name, timeout):
    error = type(name, (Exception,), {})()
    adapter, _ = _adapter(error=error)
    expected = AIAdapterTimeoutError if timeout else AIAdapterError
    with pytest.raises(expected) as raised:
        adapter.classify_and_epitaph(b"private-image", "image/png")
    assert "private-image" not in str(raised.value)
    assert "gpt-4" not in str(raised.value)


def test_study_count_cap_rejects_without_silent_drop():
    adapter, client = _adapter(_Response(StudyNoteResult(topic="x", references=[])), max_study_image_count=1)
    with pytest.raises(AIInputLimitError):
        adapter.study_note([(1, b"a", "image/png"), (2, b"b", "image/png")])
    assert client.responses.calls == []


@pytest.mark.skipif(
    os.environ.get("RUN_LIVE_VISION_SMOKE") != "1",
    reason="opt-in live provider smoke test; never runs in normal pytest",
)
def test_live_openai_vision_smoke_uses_only_synthetic_image():
    """One bounded live request; requires explicit credentials/model and opt-in."""
    if not config.OPENAI_API_KEY or not config.OPENAI_MODEL:
        pytest.skip("OPENAI_API_KEY and OPENAI_MODEL are required for live smoke")
    # A generated 1x1 transparent PNG, not a Vault file or user screenshot.
    synthetic_png = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\x0dIDATx\x9cc\xf8\xcf\xc0\xf0\x1f\x00\x05\x00\x01\xff\x89\x99=\x1d\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    result = OpenAIVisionAdapter.from_config().classify_and_epitaph(synthetic_png, "image/png")
    assert isinstance(result, ClassificationResult)
