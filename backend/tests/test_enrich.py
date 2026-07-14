"""Cost -> inventory enrichment test."""

from __future__ import annotations

import datetime as dt

from cloudwarden.models import CostRow, ResourceRecord
from cloudwarden.orchestrator import _enrich_cost


def test_enrich_fills_type_and_location() -> None:
    resources = [
        ResourceRecord(
            resource_id="/x/vm1",
            name="vm1",
            type="microsoft.compute/virtualmachines",
            location="eastus",
            resource_group="rg",
            subscription_id="s",
        )
    ]
    cost = [CostRow(usage_date=dt.date.today(), resource_id="/x/vm1", cost=1.0)]
    _enrich_cost(cost, resources)
    assert cost[0].resource_type == "microsoft.compute/virtualmachines"
    assert cost[0].location == "eastus"
    assert cost[0].resource_group == "rg"


def test_enrich_skips_when_no_resource_id_or_unknown() -> None:
    resources = [
        ResourceRecord(
            resource_id="/x/vm1",
            name="vm",
            type="t",
            location="eastus",
            resource_group="rg",
            subscription_id="s",
        )
    ]
    rows = [
        CostRow(usage_date=dt.date.today(), resource_id=None, cost=1.0),
        CostRow(usage_date=dt.date.today(), resource_id="/x/unknown", cost=2.0),
    ]
    _enrich_cost(rows, resources)  # must not raise; unmatched rows unchanged
    assert rows[0].resource_type is None and rows[1].resource_type is None


def test_config_helpers() -> None:
    from cloudwarden.config import Settings

    s = Settings(
        exclude_tag="nocolon",
        allowed_resource_groups="a, b ,",
        ai_api_key="k1",
        anthropic_api_key="k2",
    )
    assert s.exclude_tag_kv is None
    assert s.allowed_rg_list == ["a", "b"]
    assert s.resolved_ai_key == "k1"

    s2 = Settings(exclude_tag="finops:exclude", ai_api_key=None, anthropic_api_key="ak")
    assert s2.exclude_tag_kv == ("finops", "exclude")
    assert s2.resolved_ai_key == "ak"
