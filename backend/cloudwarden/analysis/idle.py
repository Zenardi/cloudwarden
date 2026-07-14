"""Idle / orphaned resource detectors driven by inventory shape fields.

Unattached managed disks, unassociated public IPs, and empty App Service plans.
Savings come from the observed monthly cost of the resource.
"""

from __future__ import annotations

from ..models import Recommendation, ResourceRecord


def detect_idle(
    resources: list[ResourceRecord], monthly_cost: dict[str, float]
) -> list[Recommendation]:
    recs: list[Recommendation] = []
    for r in resources:
        monthly = round(monthly_cost.get(r.resource_id, 0.0), 2)
        extra = r.extra or {}
        if r.type == "microsoft.compute/disks" and extra.get("diskState") == "Unattached":
            recs.append(
                Recommendation(
                    resource_id=r.resource_id,
                    category="delete_orphan",
                    action="delete_disk",
                    current_sku=r.sku,
                    risk="low",
                    confidence=0.8,
                    est_monthly_savings=monthly,
                    source="heuristic",
                    rationale="Managed disk is Unattached — no VM references it.",
                    caveats=["confirm no snapshots or images depend on it"],
                    evidence={"diskState": "Unattached"},
                )
            )
        elif r.type == "microsoft.network/publicipaddresses" and not extra.get("ipConfig"):
            recs.append(
                Recommendation(
                    resource_id=r.resource_id,
                    category="idle_ip",
                    action="delete_public_ip",
                    current_sku=r.sku,
                    risk="low",
                    confidence=0.75,
                    est_monthly_savings=monthly,
                    source="heuristic",
                    rationale=(
                        "Public IP is not associated with any resource "
                        "(Standard SKU IPs bill even when unattached)."
                    ),
                    caveats=[],
                    evidence={"ipConfig": None},
                )
            )
        elif r.type == "microsoft.web/serverfarms" and str(extra.get("numberOfSites")) == "0":
            recs.append(
                Recommendation(
                    resource_id=r.resource_id,
                    category="empty_asp",
                    action="delete_plan",
                    current_sku=r.sku,
                    risk="medium",
                    confidence=0.6,
                    est_monthly_savings=monthly,
                    source="heuristic",
                    rationale="App Service plan hosts 0 sites but bills for its tier.",
                    caveats=["confirm no deployment slots or planned use"],
                    evidence={"numberOfSites": 0},
                )
            )
    return recs
