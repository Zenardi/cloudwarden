"""Savings helpers: per-resource monthly cost from collected cost rows."""

from __future__ import annotations

from collections import defaultdict

from ..models import CostRow, Recommendation

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


def weight_commitment_savings(
    recs: list[Recommendation], environment: str | None
) -> list[Recommendation]:
    """Environment-weight commitment recommendation savings in place (M14.1).

    Mirrors the idle/waste reclaim weighting the orchestrator applies to heuristic
    recs: multiply each rec's estimated monthly savings by the subscription's
    reclaim factor and stamp ``environment``/``reclaim_factor`` onto evidence so the
    UI can group by subscription kind and show the discount. Keeps the executive
    total consistent across recommendation families and errs toward under-stating
    (never over-stating) commitment savings. Advisory (savings=0) recs are stamped
    but left unchanged."""
    factor = reclaim_factor(environment)
    for rec in recs:
        if factor != 1.0 and rec.est_monthly_savings:
            rec.est_monthly_savings = round(rec.est_monthly_savings * factor, 2)
        if environment:
            rec.evidence = {
                **rec.evidence,
                "environment": environment,
                "reclaim_factor": factor,
            }
    return recs


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
