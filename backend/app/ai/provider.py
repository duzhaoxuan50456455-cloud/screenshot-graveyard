"""AI adapter provider — the single dependency-injection seam (Task 2.5).

Services and routers obtain the active `AIAdapter` through `get_ai_adapter()` so
no business code imports a concrete adapter. Until a real vision-capable provider
adapter is implemented (see the reported future P0 integration gap), the default
is the deterministic, network-free `FakeAIAdapter`; it NEVER transmits image
bytes to an external service.

`set_ai_adapter()` lets app wiring or tests inject an alternative adapter (e.g. a
recording stub). This module holds no credentials and performs no network I/O.
"""

from __future__ import annotations

from app.ai.adapter import AIAdapter
from app.ai.fake_adapter import FakeAIAdapter

# Default to the deterministic fake until a real provider adapter is approved.
_adapter: AIAdapter = FakeAIAdapter()


def get_ai_adapter() -> AIAdapter:
    """Return the currently active AI adapter."""
    return _adapter


def set_ai_adapter(adapter: AIAdapter) -> None:
    """Replace the active AI adapter (app wiring / tests only)."""
    global _adapter
    if adapter is None:
        raise ValueError("adapter is required")
    _adapter = adapter
