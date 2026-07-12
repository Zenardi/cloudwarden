"""Azure Advisor cost recommendations (mock-backed).

Used as a ground-truth signal: when Advisor and our heuristics agree on a
resource, the recommendation is marked source='combined' and its confidence is
boosted. Resource ids are lower-cased to join with the inventory/rules.
"""

from __future__ import annotations

import logging
from typing import Any

from ..config import Settings, get_settings
from ..resilience import REGISTRY, with_retry
from ._fixtures import load_fixture, retarget
from .context import SubscriptionContext

logger = logging.getLogger("azure_finops.azure.advisor")


def _normalize(recs: list[dict[str, Any]], subscription_id: str) -> list[dict[str, Any]]:
    for r in recs:
        if r.get("resource_id"):
            r["resource_id"] = retarget(str(r["resource_id"]).lower(), subscription_id)
    return recs


def collect_advisor(
    client: Any = None, subscription: SubscriptionContext | None = None
) -> list[dict[str, Any]]:
    settings = get_settings()
    sub_id = subscription.subscription_id if subscription else settings.azure_subscription_id
    if settings.finops_mock:
        REGISTRY.set("advisor", ok=True)
        return _normalize(load_fixture("advisor"), sub_id)
    cred = subscription.credential if subscription else None
    return _collect_live(settings, client, sub_id, cred)


@with_retry()
def _collect_live(
    settings: Settings, client: Any, subscription_id: str, credential: Any = None
) -> list[dict[str, Any]]:
    from azure.mgmt.advisor import AdvisorManagementClient

    from ..auth import read_credential

    advisor = client or AdvisorManagementClient(credential or read_credential(), subscription_id)
    out: list[dict[str, Any]] = []
    for rec in advisor.recommendations.list(filter="Category eq 'Cost'"):
        props = getattr(rec, "extended_properties", None) or {}
        short = getattr(rec, "short_description", None)
        out.append(
            {
                "resource_id": (
                    getattr(rec, "resource_metadata", None).resource_id.lower()
                    if getattr(rec, "resource_metadata", None) and rec.resource_metadata.resource_id
                    else None
                ),
                "category": getattr(rec, "category", "Cost"),
                "impact": getattr(rec, "impact", None),
                "problem": getattr(short, "problem", None) if short else None,
                "solution": getattr(short, "solution", None) if short else None,
                "recommended_sku": props.get("targetSku"),
                "annual_savings": _to_float(props.get("annualSavingsAmount")),
                "extended_properties": dict(props),
            }
        )
    REGISTRY.set("advisor", ok=True)
    return out


def _to_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
