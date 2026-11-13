"""Opt-in OpenAI Responses API adapter (Task 2.9a).

This module is deliberately narrow: it converts already-authorized, in-memory
original bytes into temporary Base64 data URLs and returns the existing Pydantic
contracts.  It does not know about workspaces, Vault paths, consent records, or
operations; the services enforce those boundaries immediately before calling it.

``store=False`` asks the Responses API not to retain response application state.
It does *not* eliminate provider abuse-monitoring retention, which must remain
accurately represented in user-facing consent material.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel, ValidationError

from app import config
from app.ai.adapter import AIAdapterError, AIAdapterTimeoutError, default_timeout_seconds
from app.ai.schemas import ClassificationResult, StudyNoteResult


class _ResponsesClient(Protocol):
    """Minimal SDK surface, making ordinary tests fully network-free."""

    class responses:  # pragma: no cover - structural documentation only
        @staticmethod
        def parse(**kwargs: Any) -> Any: ...


class AIInputLimitError(AIAdapterError):
    """Authorized input exceeds the configured provider safety bound."""


class AIProviderConfigurationError(AIAdapterError):
    """Real mode was selected without a safe provider configuration."""


_CLASSIFICATION_PROMPT = """Analyze this screenshot conservatively.
Return only the requested structured result. Classify into exactly one approved
category. Put only directly observable content in visible_facts. Put any purpose
guess in inferred_intent using tentative wording, or null if unsupported. Mark
uncertain true for unreadable or ambiguous content. Never invent text, URLs,
prices, code, or the user's reason for saving the image."""

_STUDY_NOTE_PROMPT = """Create one conservative structured study note from these
screenshots in their supplied order. References MUST equal the supplied screenshot
IDs in exactly that order. Preserve code character-for-character only where it is
legible; do not guess unreadable code. Put ambiguity and unreadable content in
uncertain_content. Return only the requested structured result."""


@dataclass(frozen=True)
class OpenAIVisionAdapter:
    """A bounded, non-retrying real adapter for the existing synchronous Protocol.

    The existing FastAPI background task invokes this synchronous adapter outside
    the request coroutine.  The SDK timeout is a real network timeout; it cannot
    forcibly undo a provider request that completed after a client-side timeout,
    so the adapter never retries ambiguous requests.
    """

    client: _ResponsesClient
    model: str
    timeout_seconds: float = default_timeout_seconds()
    max_input_image_bytes: int = config.AI_MAX_INPUT_IMAGE_BYTES
    max_study_input_bytes: int = config.AI_MAX_STUDY_INPUT_BYTES
    max_study_image_count: int = config.AI_MAX_STUDY_IMAGE_COUNT
    classification_max_output_tokens: int = config.AI_CLASSIFICATION_MAX_OUTPUT_TOKENS
    study_note_max_output_tokens: int = config.AI_STUDY_NOTE_MAX_OUTPUT_TOKENS

    def __post_init__(self) -> None:
        if not self.model.strip():
            raise AIProviderConfigurationError("AI provider configuration invalid")
        for value in (
            self.timeout_seconds,
            self.max_input_image_bytes,
            self.max_study_input_bytes,
            self.max_study_image_count,
            self.classification_max_output_tokens,
            self.study_note_max_output_tokens,
        ):
            if value <= 0:
                raise AIProviderConfigurationError("AI provider configuration invalid")

    @classmethod
    def from_config(cls) -> "OpenAIVisionAdapter":
        """Create real mode lazily and fail closed if its prerequisites are absent."""
        if not config.OPENAI_API_KEY or not config.OPENAI_API_KEY.strip() or not config.OPENAI_MODEL:
            raise AIProviderConfigurationError("AI provider configuration invalid")
        try:
            # Imported only in explicit real mode.  No fake-mode import, API key,
            # or network activity is needed for tests/local development.
            from openai import OpenAI

            client = OpenAI(
                api_key=config.OPENAI_API_KEY,
                timeout=config.AI_ADAPTER_TIMEOUT_SECONDS,
                max_retries=0,
            )
        except AIAdapterError:
            raise
        except Exception as exc:
            raise AIProviderConfigurationError("AI provider configuration invalid") from exc
        return cls(client=client, model=config.OPENAI_MODEL)

    def classify_and_epitaph(self, image_bytes: bytes, mime: str) -> ClassificationResult:
        self._validate_image(image_bytes, mime)
        response = self._request(
            prompt=_CLASSIFICATION_PROMPT,
            content=[self._image_content(image_bytes, mime)],
            schema=ClassificationResult,
            max_output_tokens=self.classification_max_output_tokens,
        )
        return self._validated_output(response, ClassificationResult)

    def study_note(self, images: list[tuple[int, bytes, str]]) -> StudyNoteResult:
        if not images or len(images) > self.max_study_image_count:
            raise AIInputLimitError("AI input exceeds configured limits")
        total = 0
        content: list[dict[str, Any]] = []
        ordered_ids: list[int] = []
        for screenshot_id, image_bytes, mime in images:
            self._validate_image(image_bytes, mime)
            total += len(image_bytes)
            if total > self.max_study_input_bytes:
                raise AIInputLimitError("AI input exceeds configured limits")
            ordered_ids.append(screenshot_id)
            content.append({"type": "input_text", "text": f"Screenshot ID: {screenshot_id}"})
            content.append(self._image_content(image_bytes, mime))

        response = self._request(
            prompt=_STUDY_NOTE_PROMPT,
            content=content,
            schema=StudyNoteResult,
            max_output_tokens=self.study_note_max_output_tokens,
        )
        result = self._validated_output(response, StudyNoteResult)
        if result.references != ordered_ids:
            raise AIAdapterError("AI output failed validation")
        return result

    def _request(
        self,
        *,
        prompt: str,
        content: list[dict[str, Any]],
        schema: type[BaseModel],
        max_output_tokens: int,
    ) -> Any:
        try:
            # Responses API conventions only: structured parsing via text_format,
            # direct data URLs, no provider Files, no implicit storage, and zero
            # SDK retries. Keep the complete call inside the configured deadline.
            return self.client.responses.parse(
                model=self.model,
                input=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": content},
                ],
                text_format=schema,
                max_output_tokens=max_output_tokens,
                store=False,
                timeout=self.timeout_seconds,
            )
        except Exception as exc:
            # Avoid importing provider exception classes at module import time;
            # SDK/http client timeout class names are stable enough for the
            # boundary and tests inject those exact failure modes.
            if type(exc).__name__ in {"APITimeoutError", "TimeoutException", "ReadTimeout", "ConnectTimeout"}:
                raise AIAdapterTimeoutError("AI provider timed out") from exc
            raise AIAdapterError("AI provider request failed") from exc

    @staticmethod
    def _validated_output(response: Any, schema: type[BaseModel]):
        # Refusals, incomplete responses, and malformed parsed output are all
        # rejected without exposing provider payload details.
        if getattr(response, "status", None) == "incomplete":
            raise AIAdapterError("AI provider response incomplete")
        parsed = getattr(response, "output_parsed", None)
        if parsed is None:
            raise AIAdapterError("AI provider response unavailable")
        try:
            payload = parsed.model_dump() if isinstance(parsed, BaseModel) else parsed
            return schema.model_validate(payload)
        except (ValidationError, TypeError, ValueError) as exc:
            raise AIAdapterError("AI output failed validation") from exc

    def _validate_image(self, image_bytes: bytes, mime: str) -> None:
        if not isinstance(image_bytes, bytes) or not image_bytes or len(image_bytes) > self.max_input_image_bytes:
            raise AIInputLimitError("AI input exceeds configured limits")
        if mime not in {"image/png", "image/jpeg", "image/webp"}:
            raise AIInputLimitError("AI input exceeds configured limits")

    @staticmethod
    def _image_content(image_bytes: bytes, mime: str) -> dict[str, str]:
        encoded = base64.b64encode(image_bytes).decode("ascii")
        return {
            "type": "input_image",
            "image_url": f"data:{mime};base64,{encoded}",
            # High preserves ordinary screenshot text without requiring a local
            # derivative. The byte/aggregate caps bound request memory and cost.
            "detail": "high",
        }
