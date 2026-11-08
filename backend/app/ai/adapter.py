"""The replaceable `AIAdapter` Protocol (Task 2.1).

A SINGLE interface isolates all multimodal AI functionality so services never
import or depend on a concrete model SDK (Req 13.1, 13.4). Any implementation —
the deterministic `fake_adapter` used by every test (Task 2.2) or a real
multimodal model (Task 2.9) — must satisfy this Protocol and return output that
validates against the Pydantic schemas in `app.ai.schemas` (Req 13.2).

Timeout (design CORRECTION 4): an adapter enforces a bounded wall-clock timeout
per call; on timeout it raises `AIAdapterTimeoutError`, which the calling service
(Task 2.4/2.5) maps to a `failed`/`uncertain` operation WITHOUT touching source
bytes (INV-4). The default timeout is `config.AI_ADAPTER_TIMEOUT_SECONDS`.

This module is pure contract: it defines the Protocol, the default timeout
accessor, and the adapter error types. It performs no network I/O and holds no
secrets. API keys for a real adapter are read server-side from the environment
by that adapter's own implementation (Task 2.9), never here and never from the
client.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app import config
from app.ai.schemas import ClassificationResult, StudyNoteResult


class AIAdapterError(Exception):
    """Base error for any AI adapter failure.

    Carries a generic, user-safe message only; adapters must not leak image
    bytes, prompts, credentials, or provider internals through it.
    """


class AIAdapterTimeoutError(AIAdapterError):
    """Raised when an adapter call exceeds its configured timeout.

    The calling service maps this to a `failed`/`uncertain` operation with
    sources preserved (INV-4).
    """


def default_timeout_seconds() -> float:
    """The default per-call adapter timeout, from server configuration."""
    return config.AI_ADAPTER_TIMEOUT_SECONDS


@runtime_checkable
class AIAdapter(Protocol):
    """The one AI boundary. Implementations are swappable without changing any
    calling service (Req 13.4).

    Both methods accept raw ORIGINAL image bytes plus the detected MIME type and
    return schema-validated structured output. Implementations MUST enforce a
    bounded timeout (defaulting to `default_timeout_seconds()`), raising
    `AIAdapterTimeoutError` on expiry and `AIAdapterError` on any other failure,
    so no call can hang the request path. Implementations never mutate, persist,
    or delete the input bytes (INV-1).
    """

    def classify_and_epitaph(
        self, image_bytes: bytes, mime: str
    ) -> ClassificationResult:
        """Classify a single screenshot into one of the six categories AND
        produce its Epitaph in the SAME call (Req 4.1, 5.1)."""
        ...

    def study_note(
        self, images: list[tuple[int, bytes, str]]
    ) -> StudyNoteResult:
        """Generate one structured study note from the selected screenshots.

        `images` is a list of (screenshot_id, image_bytes, mime) tuples; the
        screenshot ids flow through to `StudyNoteResult.references` to preserve
        provenance (INV-7).
        """
        ...
