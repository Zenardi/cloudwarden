"""Provider selection + resilient generation.

Chooses a provider from config; falls back to the deterministic StubProvider when
no credentials are configured or when the configured provider raises, so a run
always produces a summary and an AI outage never fails the pipeline.
"""

from __future__ import annotations

import logging
from typing import Any

from ..config import get_settings
from .base import AIProvider, StubProvider
from .schemas import AIResult

logger = logging.getLogger("cloudwarden.ai.factory")


def get_provider() -> AIProvider:
    settings = get_settings()
    provider = (settings.ai_provider or "stub").lower()
    if provider == "anthropic":
        if not settings.resolved_ai_key:
            return StubProvider()
        from .anthropic_provider import AnthropicProvider

        return AnthropicProvider()
    if provider in ("openai", "openai_compatible"):
        if not (settings.resolved_ai_key or settings.ai_base_url):
            return StubProvider()
        from .openai_compatible_provider import OpenAICompatibleProvider

        return OpenAICompatibleProvider()
    return StubProvider()


def generate(payload: dict[str, Any]) -> AIResult:
    provider = get_provider()
    try:
        return provider.generate(payload)
    except Exception as exc:  # noqa: BLE001 - AI is best-effort; degrade to stub
        logger.warning(
            "AI provider %s failed (%s); using deterministic summary",
            getattr(provider, "name", "?"),
            exc,
        )
        return StubProvider().generate(payload)
