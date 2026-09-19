"""Language-model providers.

`get_provider()` returns the live Gemini backend when a key is configured and
the deterministic offline baseline otherwise, so the application runs end to end
either way and always reports which it used.
"""

from __future__ import annotations

import logging
from functools import lru_cache

from app.core.config import Settings, get_settings
from app.services.llm.base import LLMProvider, LLMResponse, LLMUsage
from app.services.llm.offline import OfflineProvider

logger = logging.getLogger(__name__)

__all__ = [
    "LLMProvider",
    "LLMResponse",
    "LLMUsage",
    "OfflineProvider",
    "build_provider",
    "get_provider",
    "reset_provider",
]


def build_provider(settings: Settings | None = None) -> LLMProvider:
    """Construct a provider for the given settings. Not cached.

    Split from `get_provider` deliberately. `get_provider` is `lru_cache`d, and
    an lru_cache hashes its arguments — `Settings` is a mutable Pydantic model
    and therefore unhashable, so a cached function accepting one advertises an
    injection point that raises `TypeError` the moment anybody uses it. Keeping
    the factory uncached makes the injection real and testable.
    """
    settings = settings or get_settings()

    if not settings.llm_configured:
        logger.warning(
            "GEMINI_API_KEY not configured - using the deterministic offline "
            "baseline. Generated SQL will be rule-based and insights will not "
            "be written."
        )
        return OfflineProvider()

    # Imported lazily so the google-genai client is only constructed when a key
    # actually exists.
    from app.services.llm.gemini import GeminiProvider

    logger.info("using Gemini provider (model=%s)", settings.gemini_model)
    return GeminiProvider(settings)


@lru_cache(maxsize=1)
def _cached_provider() -> LLMProvider:
    return build_provider()


def get_provider(settings: Settings | None = None) -> LLMProvider:
    """The provider for this process.

    With no argument, returns a process-wide singleton — the normal path, so the
    client and its connection pool are built once. With explicit settings,
    builds a fresh provider, which is what tests and the evaluation harness need.
    """
    if settings is not None:
        return build_provider(settings)
    return _cached_provider()


def reset_provider() -> None:
    """Clear the cached provider. Used by tests that swap configuration."""
    _cached_provider.cache_clear()
