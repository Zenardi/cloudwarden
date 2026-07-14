"""AI factory selection/fallback, provider client construction, OpenAI parsing."""

from __future__ import annotations

from cloudwarden.ai import factory
from cloudwarden.ai.base import AIProvider, StubProvider
from cloudwarden.ai.openai_compatible_provider import OpenAICompatibleProvider
from cloudwarden.config import get_settings


def _payload() -> dict:
    return {
        "subscription": {"currency": "USD"},
        "totals": {"monthly_cost_estimate": 100.0},
        "recommendations": [
            {
                "resource_id": "/x/a",
                "category": "shutdown",
                "action": "deallocate",
                "est_monthly_savings": 40.0,
            },
        ],
    }


def test_factory_anthropic_with_key(monkeypatch) -> None:
    monkeypatch.setenv("AI_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    get_settings.cache_clear()
    from cloudwarden.ai.anthropic_provider import AnthropicProvider

    assert isinstance(factory.get_provider(), AnthropicProvider)
    get_settings.cache_clear()


def test_factory_anthropic_without_key(monkeypatch) -> None:
    monkeypatch.setenv("AI_PROVIDER", "anthropic")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("AI_API_KEY", raising=False)
    get_settings.cache_clear()
    assert isinstance(factory.get_provider(), StubProvider)
    get_settings.cache_clear()


def test_factory_openai_with_base_url(monkeypatch) -> None:
    monkeypatch.setenv("AI_PROVIDER", "openai")
    monkeypatch.setenv("AI_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.delenv("AI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    get_settings.cache_clear()
    assert isinstance(factory.get_provider(), OpenAICompatibleProvider)
    get_settings.cache_clear()


def test_factory_openai_without_creds(monkeypatch) -> None:
    monkeypatch.setenv("AI_PROVIDER", "openai")
    monkeypatch.delenv("AI_BASE_URL", raising=False)
    monkeypatch.delenv("AI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    get_settings.cache_clear()
    assert isinstance(factory.get_provider(), StubProvider)
    get_settings.cache_clear()


def test_factory_unknown_provider(monkeypatch) -> None:
    monkeypatch.setenv("AI_PROVIDER", "weird")
    get_settings.cache_clear()
    assert isinstance(factory.get_provider(), StubProvider)
    get_settings.cache_clear()


def test_generate_falls_back_on_error(monkeypatch) -> None:
    class _Boom(AIProvider):
        name = "boom"

        def generate(self, payload):
            raise RuntimeError("nope")

    monkeypatch.setattr(factory, "get_provider", lambda: _Boom())
    result = factory.generate(_payload())
    assert result.provider == "stub"
    assert result.total_potential_monthly_savings == 40.0


def test_anthropic_get_client(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-x")
    get_settings.cache_clear()
    from cloudwarden.ai.anthropic_provider import AnthropicProvider

    assert AnthropicProvider()._get_client() is not None
    fake = object()
    assert AnthropicProvider(client=fake)._get_client() is fake
    get_settings.cache_clear()


def test_openai_get_client(monkeypatch) -> None:
    monkeypatch.setenv("AI_BASE_URL", "http://x/v1")
    get_settings.cache_clear()
    assert OpenAICompatibleProvider()._get_client() is not None
    get_settings.cache_clear()


class _Msg:
    def __init__(self, content: str) -> None:
        self.content = content


class _Choice:
    def __init__(self, content: str) -> None:
        self.message = _Msg(content)


class _Usage:
    prompt_tokens = 5
    completion_tokens = 7


class _OAIResp:
    def __init__(self, content: str) -> None:
        self.choices = [_Choice(content)]
        self.usage = _Usage()


class _Completions:
    def __init__(self, content: str) -> None:
        self._c = content

    def create(self, **kwargs):
        return _OAIResp(self._c)


class _Chat:
    def __init__(self, content: str) -> None:
        self.completions = _Completions(content)


class _FakeOpenAI:
    def __init__(self, content: str) -> None:
        self.chat = _Chat(content)


def test_openai_provider_parses() -> None:
    get_settings.cache_clear()
    fake = _FakeOpenAI(
        '{"executive_summary": "ok", "total_potential_monthly_savings": 55, '
        '"currency": "USD", "recommendations": []}'
    )
    result = OpenAICompatibleProvider(client=fake).generate(_payload())
    assert result.executive_summary == "ok"
    assert result.total_potential_monthly_savings == 55
    assert result.provider == "openai"
    assert result.input_tokens == 5 and result.output_tokens == 7
    get_settings.cache_clear()
