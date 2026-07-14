"""FinOps rules engine for VMs: shutdown / downsize heuristics.

Pure functions over `UtilizationRollup` + `ResourceRecord` + a monthly-cost map,
so they are deterministic and unit-testable. Thresholds come from Settings.
Advisor agreement upgrades a recommendation to source='combined' and boosts
confidence. `prioritize()` renumbers the final list by estimated savings.
"""

from __future__ import annotations

from ..config import Settings
from ..models import Recommendation, ResourceRecord, UtilizationRollup
from . import pricing

_VM_TYPE = "microsoft.compute/virtualmachines"
_IDLE_NET_BYTES_DAY = 5 * 1024 * 1024  # 5 MB/day


def _evidence(roll: UtilizationRollup) -> dict:
    return {
        "cpu_avg": roll.cpu_avg,
        "cpu_p95": roll.cpu_p95,
        "cpu_max": roll.cpu_max,
        "mem_p95": roll.mem_p95,
        "mem_available": roll.mem_available,
        "net_bytes_day": roll.net_bytes_day,
        "disk_iops_avg": roll.disk_iops_avg,
        "data_completeness": roll.data_completeness,
    }


def _shutdown(res: ResourceRecord, roll: UtilizationRollup, monthly: float, s: Settings):
    cpu_p95 = roll.cpu_p95 or 0.0
    cpu_max = roll.cpu_max or 0.0
    net_day = roll.net_bytes_day or 0.0
    running = (res.power_state or "").lower().endswith("running")
    if not (
        running
        and cpu_p95 < s.shutdown_cpu_p95
        and cpu_max < s.shutdown_cpu_max
        and net_day < _IDLE_NET_BYTES_DAY
    ):
        return None
    return Recommendation(
        resource_id=res.resource_id,
        category="shutdown",
        action="deallocate",
        current_sku=res.sku,
        risk="medium",
        confidence=0.8,
        est_monthly_savings=round(monthly, 2),
        source="heuristic",
        rationale=(
            f"CPU p95 {cpu_p95:.1f}% / max {cpu_max:.1f}%, network ~{net_day / 1e6:.1f} MB/day "
            "over the window — idle while running. Deallocating stops compute charges "
            "(attached disks continue to bill)."
        ),
        caveats=["savings exclude still-billing attached disks"],
        evidence=_evidence(roll),
    )


def _downsize(res: ResourceRecord, roll: UtilizationRollup, monthly: float, s: Settings):
    cpu_p95 = roll.cpu_p95 or 0.0
    cpu_max = roll.cpu_max or 0.0
    if not (cpu_p95 < s.downsize_cpu_p95 and cpu_max < s.downsize_cpu_max):
        return None
    caveats: list[str] = []
    if roll.mem_available:
        if (roll.mem_p95 or 0.0) >= s.downsize_mem_p95:
            return None
        confidence = 0.75
    else:
        caveats.append("memory data unavailable — CPU-only assessment")
        confidence = 0.55
    target = pricing.smaller_sku(res.sku)
    if not target:
        return None
    current_price = pricing.vm_monthly_price(res.sku)
    target_price = pricing.vm_monthly_price(target)
    if current_price is None or target_price is None:
        savings = max(monthly * 0.5, 0.0)  # fallback: assume ~half on one step down
    else:
        savings = max(current_price - target_price, 0.0)
    mem_note = f", memory p95 {roll.mem_p95:.0f}%" if roll.mem_available else ""
    return Recommendation(
        resource_id=res.resource_id,
        category="downsize",
        action="resize",
        current_sku=res.sku,
        recommended_sku=target,
        risk="low",
        confidence=confidence,
        est_monthly_savings=round(savings, 2),
        source="heuristic",
        rationale=(
            f"CPU p95 {cpu_p95:.1f}% / max {cpu_max:.1f}%{mem_note} — headroom to move "
            f"{res.sku} → {target}."
        ),
        caveats=caveats,
        evidence=_evidence(roll),
    )


def evaluate_vms(
    resources: list[ResourceRecord],
    rollups: dict[str, UtilizationRollup],
    monthly_cost: dict[str, float],
    advisor_ids: set[str],
    settings: Settings,
) -> list[Recommendation]:
    by_id = {r.resource_id: r for r in resources}
    recs: list[Recommendation] = []
    for rid, roll in rollups.items():
        res = by_id.get(rid)
        if res is None or res.type != _VM_TYPE:
            continue
        monthly = monthly_cost.get(rid, 0.0)
        if roll.data_completeness < settings.min_data_completeness:
            recs.append(
                Recommendation(
                    resource_id=rid,
                    category="investigate",
                    action="review",
                    current_sku=res.sku,
                    risk="low",
                    confidence=0.3,
                    est_monthly_savings=0.0,
                    source="heuristic",
                    rationale=(
                        f"Insufficient metric coverage (data_completeness "
                        f"{roll.data_completeness:.0%}) — enable diagnostics before deciding."
                    ),
                    caveats=["low telemetry coverage"],
                    evidence=_evidence(roll),
                )
            )
            continue
        rec = _shutdown(res, roll, monthly, settings) or _downsize(res, roll, monthly, settings)
        if rec is None:
            continue
        if rid in advisor_ids:
            rec.source = "combined"
            rec.confidence = min(1.0, rec.confidence + 0.1)
        recs.append(rec)
    return recs


def prioritize(recs: list[Recommendation]) -> list[Recommendation]:
    ordered = sorted(recs, key=lambda r: (r.est_monthly_savings, r.confidence), reverse=True)
    for index, rec in enumerate(ordered, start=1):
        rec.priority = index
    return ordered
