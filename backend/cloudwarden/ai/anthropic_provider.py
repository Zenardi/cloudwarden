"""Anthropic (Claude) provider — default AI model is claude-opus-4-8.

Uses adaptive thinking and a strict-JSON system prompt with tolerant parsing.
The Anthropic SDK is imported lazily so the package works without it installed.
"""

from __future__ import annotations

import logging
from typing import Any

from ..config import get_settings
from .base import AIProvider
from .prompt import SYSTEM_PROMPT, build_user_content, extract_json
from .schemas import AIResult

logger = logging.getLogger("cloudwarden.ai.anthropic")


class AnthropicProvider(AIProvider):
    name = "anthropic"

    def __init__(self, client: Any = None) -> None:
        self._client = client

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        import anthropic

        return anthropic.Anthropic(api_key=get_settings().resolved_ai_key)

    def generate(self, payload: dict[str, Any]) -> AIResult:
        settings = get_settings()
        client = self._get_client()
        response = client.messages.create(
            model=settings.ai_model,
            max_tokens=settings.ai_max_tokens,
            thinking={"type": "adaptive"},
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": build_user_content(payload)}],
        )
        text = "".join(
            getattr(block, "text", "")
            for block in response.content
            if getattr(block, "type", None) == "text"
        )
        result = AIResult.model_validate(extract_json(text))
        result.provider = self.name
        result.model = settings.ai_model
        usage = getattr(response, "usage", None)
        if usage is not None:
            result.input_tokens = getattr(usage, "input_tokens", None)
            result.output_tokens = getattr(usage, "output_tokens", None)
        return result
