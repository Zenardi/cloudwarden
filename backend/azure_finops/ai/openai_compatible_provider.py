"""OpenAI-compatible provider — OpenAI, or any local server (Ollama / vLLM /
LM Studio) via AI_BASE_URL. Requests JSON output and parses tolerantly.
"""

from __future__ import annotations

import logging
from typing import Any

from ..config import get_settings
from .base import AIProvider
from .prompt import SYSTEM_PROMPT, build_user_content, extract_json
from .schemas import AIResult

logger = logging.getLogger("azure_finops.ai.openai")


class OpenAICompatibleProvider(AIProvider):
    name = "openai"

    def __init__(self, client: Any = None) -> None:
        self._client = client

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        from openai import OpenAI

        settings = get_settings()
        return OpenAI(
            api_key=settings.resolved_ai_key or "not-needed",
            base_url=settings.ai_base_url or None,
        )

    def generate(self, payload: dict[str, Any]) -> AIResult:
        settings = get_settings()
        client = self._get_client()
        response = client.chat.completions.create(
            model=settings.ai_model,
            max_tokens=settings.ai_max_tokens,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_user_content(payload)},
            ],
        )
        text = response.choices[0].message.content or "{}"
        result = AIResult.model_validate(extract_json(text))
        result.provider = self.name
        result.model = settings.ai_model
        usage = getattr(response, "usage", None)
        if usage is not None:
            result.input_tokens = getattr(usage, "prompt_tokens", None)
            result.output_tokens = getattr(usage, "completion_tokens", None)
        return result
