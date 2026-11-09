"""AI boundary package (Task 2.1).

Exposes the single replaceable `AIAdapter` Protocol and the Pydantic result
schemas every adapter implementation (real or fake) must satisfy. Business
services depend ONLY on these abstractions, never on a concrete model SDK
(Req 13.1, 13.4).
"""

from app.ai.schemas import (
    Category,
    ClassificationResult,
    EpitaphPayload,
    StudyNoteResult,
)
from app.ai.adapter import AIAdapter, AIAdapterError, AIAdapterTimeoutError
from app.ai.fake_adapter import FakeAIAdapter, FakeAIAdapterMode

__all__ = [
    "Category",
    "ClassificationResult",
    "EpitaphPayload",
    "StudyNoteResult",
    "AIAdapter",
    "AIAdapterError",
    "AIAdapterTimeoutError",
    "FakeAIAdapter",
    "FakeAIAdapterMode",
]
