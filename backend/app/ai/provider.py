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
from app import config

# Default to the deterministic fake until a real provider adapter is approved.
_adapter: AIAdapter = FakeAIAdapter()


def build_configured_adapter() -> AIAdapter:
    """Build the explicitly configured adapter without a silent fallback.

    Importing the real SDK is intentionally deferred until ``AI_PROVIDER=openai``
    so fake-only tests do not require a provider package, key, or network.
    """
    if config.AI_PROVIDER == "fake":
        return FakeAIAdapter()
    if config.AI_PROVIDER == "openai":
        from app.ai.openai_vision_adapter import OpenAIVisionAdapter

        return OpenAIVisionAdapter.from_config()
    # Config validates this at import, but preserve fail-closed behavior if a
    # test mutates config after import.
    raise RuntimeError("unsupported AI provider configuration")


def configure_ai_adapter() -> AIAdapter:
    """Install the environment-selected adapter during application startup."""
    adapter = build_configured_adapter()
    set_ai_adapter(adapter)
    return adapter


def get_ai_adapter() -> AIAdapter:
    """Return the currently active AI adapter."""
    return _adapter


def set_ai_adapter(adapter: AIAdapter) -> None:
    """Replace the active AI adapter (app wiring / tests only)."""
    global _adapter
    if adapter is None:
        raise ValueError("adapter is required")
    _adapter = adapter
