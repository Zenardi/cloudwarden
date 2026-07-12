"""AI provider interface + a deterministic offline stub.

`StubProvider` produces a data-grounded executive summary without any network
call — the default whenever no AI credentials are configured, so the pipeline
always yields a summary.
"""

from __future__ import annotations

import abc
from typing import Any

from .schemas import AIRecommendation, AIResult

_REC_FIELDS = (
    "resource_id",
    "action",
    "priority",
    "risk",
    "confidence",
    "est_monthly_savings",
    "rationale",
)


class AIProvider(abc.ABC):
    name: str = "base"

    @abc.abstractmethod
    def generate(self, payload: dict[str, Any]) -> AIResult: ...


def _stub_summary(
    recs: list[dict[str, Any]], total_savings: float, monthly: float, pct: float, currency: str
) -> str:
    if not recs:
        return "No optimization opportunities were identified in this run."
    by_cat: dict[str, int] = {}
    for r in recs:
        cat = r.get("category", "other")
        by_cat[cat] = by_cat.get(cat, 0) + 1
    mix = ", ".join(
        f"{count}× {cat}" for cat, count in sorted(by_cat.items(), key=lambda kv: -kv[1])
    )
    top = max(recs, key=lambda r: r.get("est_monthly_savings") or 0.0)
    top_res = str(top.get("resource_id", "")).rsplit("/", 1)[-1]
    spend_clause = (
        f" (~{pct:.0f}% of ~{currency} {monthly:,.0f} current 30-day spend)" if monthly else ""
    )
    return (
        f"{len(recs)} optimization opportunities identified, an estimated "
        f"{currency} {total_savings:,.0f}/month in potential savings{spend_clause}. "
        f"Highest impact: {top.get('action', 'act on')} {top_res} "
        f"(~{currency} {top.get('est_monthly_savings') or 0:,.0f}/mo). Mix: {mix}. "
        "Figures are estimates — validate low-confidence and memory-caveated items before acting."
    )


class StubProvider(AIProvider):
    """Deterministic, offline summary — used when no AI key/endpoint is configured."""

    name = "stub"

    def generate(self, payload: dict[str, Any]) -> AIResult:
        recs = payload.get("recommendations", [])
        totals = payload.get("totals", {})
        currency = payload.get("subscription", {}).get("currency", "USD")
        total_savings = round(sum(r.get("est_monthly_savings") or 0.0 for r in recs), 2)
        monthly = float(totals.get("monthly_cost_estimate", 0.0) or 0.0)
        pct = (total_savings / monthly * 100.0) if monthly else 0.0
        return AIResult(
            executive_summary=_stub_summary(recs, total_savings, monthly, pct, currency),
            total_potential_monthly_savings=total_savings,
            currency=currency,
            recommendations=[
                AIRecommendation(**{k: r.get(k) for k in _REC_FIELDS if k in r}) for r in recs
            ],
            provider=self.name,
            model="deterministic",
            input_tokens=0,
            output_tokens=0,
        )
