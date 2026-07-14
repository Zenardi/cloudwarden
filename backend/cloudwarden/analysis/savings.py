"""Savings helpers: per-resource monthly cost from collected cost rows."""

from __future__ import annotations

from collections import defaultdict

from ..models import CostRow

DAYS_PER_MONTH = 30.4


def monthly_cost_map(cost_rows: list[CostRow], cost_type: str = "Amortized") -> dict[str, float]:
    """Estimate each resource's monthly cost from the observed daily rows.

    Normalizes by the number of distinct days actually seen (handles partial
    windows), then scales to a 30.4-day month.
    """
    totals: dict[str, float] = defaultdict(float)
    days: dict[str, set] = defaultdict(set)
    for c in cost_rows:
        if c.cost_type != cost_type or not c.resource_id:
            continue
        totals[c.resource_id] += float(c.cost)
        days[c.resource_id].add(c.usage_date)
    return {rid: totals[rid] / (len(days[rid]) or 1) * DAYS_PER_MONTH for rid in totals}
