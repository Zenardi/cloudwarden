"""AI layer: stub summary, factory selection/fallback, provider JSON parsing."""

from __future__ import annotations

import datetime as dt

from cloudwarden.ai import factory
from cloudwarden.ai.anthropic_provider import AnthropicProvider
from cloudwarden.ai.base import StubProvider
from cloudwarden.ai.prompt import build_payload, extract_json
from cloudwarden.config import get_settings
from cloudwarden.models import CostRow, Recommendation


def _payload() -> dict:
    recs = [
        Recommendation(
            resource_id="/x/vm-batch",
            category="shutdown",
            action="deallocate",
            est_monthly_savings=400.0,
            confidence=0.9,
        ),
        Recommendation(
            resource_id="/x/vm-web",
            category="downsize",
            action="resize",
            est_monthly_savings=70.0,
            confidence=0.75,
        ),
    ]
    today = dt.date.today()
    cost = [
        CostRow(
            usage_date=today,
            resource_id="/x/vm-batch",
            resource_type="microsoft.compute/virtualmachines",
            location="eastus",
            cost=13.0,
            cost_type="Amortized",
        )
    ]
    return build_payload(recs, cost, currency="USD", max_candidates=40)


def test_stub_summary_offline() -> None:
    result = StubProvider().generate(_payload())
    assert result.provider == "stub"
    assert result.total_potential_monthly_savings == 470.0
    assert "470" in result.executive_summary
    assert "shutdown" in result.executive_summary
    assert result.input_tokens == 0


def test_factory_defaults_to_stub_without_key() -> None:
    get_settings.cache_clear()
    assert isinstance(factory.get_provider(), StubProvider)


def test_generate_returns_result_offline() -> None:
    result = factory.generate(_payload())
    assert result.total_potential_monthly_savings == 470.0
    assert result.executive_summary


def test_extract_json_variants() -> None:
    assert extract_json('{"a": 1}')["a"] == 1
    assert extract_json('```json\n{"a": 2}\n```')["a"] == 2
    assert extract_json('noise {"a": 3} trailing')["a"] == 3


class _Block:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class _Usage:
    input_tokens = 11
    output_tokens = 22


class _Resp:
    def __init__(self, text: str) -> None:
        self.content = [_Block(text)]
        self.usage = _Usage()


class _Messages:
    def __init__(self, text: str) -> None:
        self._t = text

    def create(self, **kwargs):
        return _Resp(self._t)


class _FakeAnthropic:
    def __init__(self, text: str) -> None:
        self.messages = _Messages(text)


def test_anthropic_provider_parses_json() -> None:
    fake = _FakeAnthropic(
        '{"executive_summary": "ok", "total_potential_monthly_savings": 123.0, '
        '"currency": "USD", "recommendations": []}'
    )
    result = AnthropicProvider(client=fake).generate(_payload())
    assert result.executive_summary == "ok"
    assert result.total_potential_monthly_savings == 123.0
    assert result.provider == "anthropic"
    assert result.input_tokens == 11 and result.output_tokens == 22
