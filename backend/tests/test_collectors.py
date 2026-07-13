"""Mock-mode collector tests (no Azure, no database)."""

from __future__ import annotations

import datetime as dt

from azure_finops.azure.cost import _parse_response, collect_cost
from azure_finops.azure.inventory import collect_inventory
from azure_finops.config import get_settings


def test_inventory_mock_shape() -> None:
    resources = collect_inventory()
    assert len(resources) == 7
    ids = {r.resource_id for r in resources}
    assert any(i.endswith("vm-web-01") for i in ids)
    disk = next(r for r in resources if r.type == "microsoft.compute/disks")
    assert disk.extra.get("diskState") == "Unattached"
    # resource ids are lower-cased for clean joins with cost rows
    assert all(r.resource_id == r.resource_id.lower() for r in resources)


def test_cost_mock_recent_and_counts() -> None:
    rows = collect_cost()
    settings = get_settings()
    assert rows, "expected mock cost rows"
    today = dt.date.today()
    assert max(r.usage_date for r in rows) == today
    assert min(r.usage_date for r in rows) == today - dt.timedelta(
        days=settings.cost_lookback_days - 1
    )
    amortized = [r for r in rows if r.cost_type == "Amortized"]
    assert len(amortized) == 6 * settings.cost_lookback_days
    assert all(r.cost >= 0 for r in rows)
    assert all(r.resource_type for r in amortized)


def test_cost_response_parser() -> None:
    payload = {
        "properties": {
            "columns": [
                {"name": "Cost"},
                {"name": "UsageDate"},
                {"name": "ResourceId"},
                {"name": "ServiceName"},
                {"name": "Currency"},
            ],
            "rows": [
                [
                    12.5,
                    20260712,
                    "/subscriptions/x/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/VM1",
                    "Virtual Machines",
                    "USD",
                ]
            ],
        }
    }
    rows = _parse_response(payload, "Amortized")
    assert len(rows) == 1
    row = rows[0]
    assert row.cost == 12.5
    assert row.usage_date == dt.date(2026, 7, 12)
    assert row.resource_id.endswith("vm1")  # lower-cased
    assert row.cost_type == "Amortized"
