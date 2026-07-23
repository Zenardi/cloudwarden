"""Prompt construction: build the aggregated payload and the system/user text.

We feed the model aggregated data only (cost totals + the rule engine's candidate
recommendations), never raw metric samples. Free-text from Azure (resource ids,
rationales) is sanitized to blunt prompt-injection.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import Any

from ..models import CostRow, Recommendation

SYSTEM_PROMPT = (
    "You are a FinOps analyst. You receive an Azure subscription's cost totals and a list of "
    "candidate cost-optimization recommendations from a rules engine. Reconcile and prioritize "
    "them, and write a concise executive summary for engineering leadership. Ground every "
    "statement strictly in the provided data — never invent resources, SKUs, or numbers. If a "
    "recommendation notes missing memory data or low confidence, reflect that honestly. "
    "Some candidates have category 'commitment' — Reservation/Savings-Plan coverage: "
    "under-utilized existing commitments (advisory waste) and purchase recommendations sized to "
    "steady-state usage. Treat their savings as caveated ESTIMATES (blended discount) and never "
    "over-state them; the 'commitment_coverage' block gives current coverage/utilization context. "
    "Respond with ONLY a JSON object (no prose, no code fences) matching this schema: "
    '{"executive_summary": string, "total_potential_monthly_savings": number, '
    '"currency": string, "recommendations": [{"resource_id": string, "action": string, '
    '"priority": integer, "risk": string, "confidence": number, '
    '"est_monthly_savings": number, "rationale": string}]}'
)


def _sanitize(text: str | None) -> str:
    if not text:
        return ""
    return " ".join(str(text).split())[:200]


def build_payload(
    recommendations: list[Recommendation],
    cost_rows: list[CostRow],
    currency: str = "USD",
    max_candidates: int = 40,
    commitment_coverage: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    amortized = [c for c in cost_rows if c.cost_type == "Amortized"]
    total = sum(float(c.cost) for c in amortized)
    by_type: dict[str, float] = defaultdict(float)
    by_region: dict[str, float] = defaultdict(float)
    for c in amortized:
        by_type[c.resource_type or "unknown"] += float(c.cost)
        by_region[c.location or "unknown"] += float(c.cost)

    top = sorted(recommendations, key=lambda r: r.est_monthly_savings, reverse=True)[
        :max_candidates
    ]
    return {
        "subscription": {"currency": currency},
        # Aggregated commitment coverage/utilization per SKU family/region (M14.1) —
        # already rolled up (never raw), so it adds negligible token cost.
        "commitment_coverage": commitment_coverage or [],
        "totals": {
            "monthly_cost_estimate": round(total, 2),
            "by_type": [
                {"resource_type": k, "cost": round(v, 2)}
                for k, v in sorted(by_type.items(), key=lambda kv: -kv[1])
            ],
            "by_region": [
                {"location": k, "cost": round(v, 2)}
                for k, v in sorted(by_region.items(), key=lambda kv: -kv[1])
            ],
        },
        "recommendations": [
            {
                "resource_id": _sanitize(r.resource_id),
                "category": r.category,
                "action": r.action,
                "current_sku": r.current_sku,
                "recommended_sku": r.recommended_sku,
                "risk": r.risk,
                "confidence": r.confidence,
                "est_monthly_savings": r.est_monthly_savings,
                "source": r.source,
                "rationale": _sanitize(r.rationale),
            }
            for r in top
        ],
    }


def build_user_content(payload: dict[str, Any]) -> str:
    return "Here is the FinOps data:\n\n" + json.dumps(payload, indent=2, default=str)


def extract_json(text: str) -> dict[str, Any]:
    """Tolerant JSON extraction: strips code fences, falls back to the first {...}."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            return json.loads(match.group(0))
        raise
