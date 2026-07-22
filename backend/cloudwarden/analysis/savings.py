"""Savings helpers: per-resource monthly cost from collected cost rows."""

from __future__ import annotations

from collections import defaultdict

from ..models import CostRow

DAYS_PER_MONTH = 30.4

# Environment "reclaim factor": how much of a resource's raw idle/waste savings we
# count as *potential* savings, by subscription kind. Non-production waste is safe
# to reclaim (a sandbox resource can be deleted on sight); production idle needs
# investigation before it is cut, so it is discounted. This is the single source of
# truth for the valid environment names (the API validates against these keys).
ENVIRONMENT_RECLAIM_FACTORS: dict[str, float] = {
    "Sandbox": 1.0,
    "Development": 0.9,
    "QA": 0.7,
    "Prod": 0.5,
}


def reclaim_factor(environment: str | None) -> float:
    """Savings reclaim factor for a subscription environment (1.0 when unclassified)."""
    if not environment:
        return 1.0
    return ENVIRONMENT_RECLAIM_FACTORS.get(environment, 1.0)


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
