"""Idle / orphaned resource detectors driven by inventory shape fields.

Unattached managed disks, disks reserved by a deallocated VM, unassociated public
IPs, and empty App Service plans. Savings come from the observed monthly cost of
the resource.
"""

from __future__ import annotations

import re

from ..models import ActivitySignal, Recommendation, ResourceRecord

# DevCenter dev-box definition SKUs encode the VM size, e.g.
# "general_i_16c64gb512ssd_v2" → 16 vCPU / 64 GB. Pools whose definition is a large
# size are right-sizing candidates. 16 vCPU is the largest common dev-box tier.
_DEVBOX_VCPU_RE = re.compile(r"_(\d+)c\d+gb")
_OVERSIZED_DEVBOX_VCPU = 16


def _devbox_vcpu(sku: str | None) -> int | None:
    """Parse the vCPU count out of a dev-box definition SKU name (None if unknown)."""
    if not sku:
        return None
    m = _DEVBOX_VCPU_RE.search(sku)
    return int(m.group(1)) if m else None


# ML compute-instance states that mean "not usefully running": a failed provision
# never became usable but can still leave a residual OS disk behind.
_ML_FAILED_STATES = {"CreateFailed", "Failed"}


def _ml_compute_rec(r: ResourceRecord, ws_monthly: float) -> Recommendation | None:
    """Advisory rec for one ML compute target, or None if it's in a fine state.

    ML compute bills per its VM size but Cost Management rolls that spend up to the
    owning *workspace* (there is no per-compute cost row), so we can't isolate a
    savings figure — every case is advisory (``est_monthly_savings=0``) with the
    workspace's monthly cost carried as evidence context. Three wasteful shapes:
    a running Compute Instance (no scale-to-zero — bills continuously), a failed
    Compute Instance (delete the wreckage), and an AmlCompute cluster pinned to a
    non-zero minimum node count (idle nodes bill between jobs).
    """
    cfg = r.config or {}
    ctype = cfg.get("compute_type")
    state = str(cfg.get("state") or "")
    vm_size = cfg.get("vm_size") or r.sku or "its VM size"
    evidence = {
        "compute_type": ctype,
        "state": state,
        "vm_size": cfg.get("vm_size"),
        "workspace_monthly": ws_monthly,
    }
    base = dict(
        resource_id=r.resource_id,
        category="idle_ml_compute",
        current_sku=cfg.get("vm_size") or r.sku,
        risk="medium",
        est_monthly_savings=0.0,
        source="heuristic",
    )
    if ctype == "ComputeInstance" and state in _ML_FAILED_STATES:
        return Recommendation(
            **base,
            action="review_idle_resource",
            confidence=0.6,
            rationale=(
                f"ML compute instance failed to provision (state {state}). It is not usable; "
                f"delete it to clear the failed resource and any residual OS disk."
            ),
            caveats=["confirm it isn't mid-retry before deleting"],
            evidence=evidence,
        )
    if ctype == "ComputeInstance" and state == "Running":
        return Recommendation(
            **base,
            action="review_idle_resource",
            confidence=0.4,
            rationale=(
                f"ML compute instance is running ({vm_size}) and bills continuously — compute "
                f"instances do not scale to zero. If it isn't in active use, stop it or enable "
                f"idle shutdown."
            ),
            caveats=["may be in active interactive use — confirm before stopping"],
            evidence=evidence,
        )
    if ctype == "AmlCompute":
        min_nodes = int(cfg.get("min_node_count") or 0)
        if min_nodes > 0:
            return Recommendation(
                **base,
                action="review_rightsizing",
                confidence=0.5,
                rationale=(
                    f"ML compute cluster keeps a minimum of {min_nodes} node(s) always allocated "
                    f"({vm_size}), which bills even when no job is running. Set the minimum node "
                    f"count to 0 to scale to zero between jobs."
                ),
                caveats=["a warm minimum may be intentional to cut job start latency"],
                evidence={**evidence, "min_node_count": min_nodes},
            )
    return None


def detect_idle(
    resources: list[ResourceRecord], monthly_cost: dict[str, float]
) -> list[Recommendation]:
    recs: list[Recommendation] = []
    # Map dev-box definition name -> SKU so a pool (which references its definition by
    # name) can be assessed for right-sizing without a second lookup pass.
    devbox_skus = {
        r.name: ((r.config or {}).get("sku") or {}).get("name")
        for r in resources
        if r.type == "microsoft.devcenter/devcenters/devboxdefinitions"
    }
    for r in resources:
        monthly = round(monthly_cost.get(r.resource_id, 0.0), 2)
        extra = r.extra or {}
        cfg = r.config or {}
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
        elif r.type == "microsoft.compute/disks" and extra.get("diskState") == "Reserved":
            # "Reserved" = the disk is attached to a *deallocated* (stopped) VM.
            # Deallocating a VM stops compute charges but the disk keeps billing,
            # so a long-parked VM quietly leaks storage cost. We can't delete the
            # disk out from under its VM, so the action is advisory (delete the VM
            # and its disks if it's truly abandoned), not an auto-remediation.
            recs.append(
                Recommendation(
                    resource_id=r.resource_id,
                    category="idle_disk",
                    action="review_stopped_vm",
                    current_sku=r.sku,
                    risk="medium",
                    confidence=0.6,
                    est_monthly_savings=monthly,
                    source="heuristic",
                    rationale=(
                        "Managed disk is Reserved — attached to a deallocated (stopped) VM. "
                        "Compute charges stop while deallocated, but this disk still bills. "
                        "If the VM is no longer needed, delete it and its disks."
                    ),
                    caveats=[
                        "VM may be stopped intentionally for later use — confirm before deleting"
                    ],
                    evidence={"diskState": "Reserved"},
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
        elif r.type == "microsoft.compute/virtualmachines" and (
            r.power_state or ""
        ).lower().endswith("deallocated"):
            # A deallocated VM stops billing for compute, but its attached disks and
            # reserved public IPs keep billing. The rules engine (evaluate_vms) is
            # blind to it — a stopped VM emits no metrics, so it has no rollup — which
            # is exactly why stopped VMs went unaccounted. This is an *advisory*
            # awareness rec (savings = 0): projecting the VM's historical running cost
            # forward would overstate savings (compute no longer bills while stopped),
            # and the real residual — the disk — is already quantified by idle_disk.
            # Low confidence: many VMs are stopped on purpose.
            recs.append(
                Recommendation(
                    resource_id=r.resource_id,
                    category="stopped_vm",
                    action="review_stopped_vm",
                    current_sku=r.sku,
                    risk="medium",
                    confidence=0.4,
                    est_monthly_savings=0.0,
                    source="heuristic",
                    rationale=(
                        "VM is deallocated (stopped). Compute isn't billing, but attached "
                        "disks and reserved public IPs still do (quantified separately). If "
                        "it's abandoned, delete the VM and its disks/IPs to stop residual charges."
                    ),
                    caveats=["VM may be stopped intentionally — confirm before deleting"],
                    evidence={"power_state": r.power_state},
                )
            )
        elif r.type == "microsoft.devcenter/projects/pools":
            # A DevCenter project pool bills for the dev boxes it hosts. Zero dev boxes
            # = a pool paying for nothing; otherwise, a large dev-box definition is a
            # right-sizing candidate (dev boxes are the dominant DevCenter cost).
            count = int(cfg.get("devBoxCount") or 0)
            if count == 0:
                recs.append(
                    Recommendation(
                        resource_id=r.resource_id,
                        category="idle_pool",
                        action="review_idle_resource",
                        current_sku=r.sku,
                        risk="medium",
                        confidence=0.6,
                        est_monthly_savings=monthly,
                        source="heuristic",
                        rationale=(
                            "DevCenter project pool hosts 0 dev boxes but still bills for "
                            "its configuration. If it is unused, delete it."
                        ),
                        caveats=["confirm no planned dev-box assignments before deleting"],
                        evidence={"devBoxCount": 0},
                    )
                )
            else:
                sku = devbox_skus.get(cfg.get("devBoxDefinitionName"))
                vcpu = _devbox_vcpu(sku)
                if vcpu and vcpu >= _OVERSIZED_DEVBOX_VCPU:
                    # One dev-box size down roughly halves the per-vCPU compute rate, but
                    # storage/licensing don't shrink — so estimate conservatively (~35%)
                    # and keep it advisory (low confidence, heavy caveats).
                    est = round(monthly * 0.35, 2)
                    recs.append(
                        Recommendation(
                            resource_id=r.resource_id,
                            category="oversized_pool",
                            action="review_rightsizing",
                            current_sku=sku,
                            risk="low",
                            confidence=0.4,
                            est_monthly_savings=est,
                            source="heuristic",
                            rationale=(
                                f"Dev boxes in this pool use a large {vcpu}-vCPU definition "
                                f"({sku}). If the workload doesn't need it, a smaller size cuts "
                                f"the hourly rate across all {count} dev boxes."
                            ),
                            caveats=[
                                "estimate — validate against Dev Box pricing for the target size",
                                "confirm developers don't rely on the larger size",
                            ],
                            evidence={"devBoxCount": count, "definitionSku": sku, "vcpu": vcpu},
                        )
                    )
        elif r.type == "microsoft.documentdb/mongoclusters":
            # Make Mongo (Cosmos DB for MongoDB vCore) clusters accountable. Free-tier
            # clusters cost nothing to optimize, so only flag paid tiers for review;
            # quantified idle detection (by connections/ops) is metric-based (Phase 3).
            skus = [s.get("sku") for s in (cfg.get("nodeGroupSpecs") or []) if isinstance(s, dict)]
            paid = [s for s in skus if s and str(s).lower() != "free"]
            if paid and monthly >= 1.0:
                recs.append(
                    Recommendation(
                        resource_id=r.resource_id,
                        category="mongo_cluster",
                        action="review_idle_resource",
                        current_sku=str(paid[0]),
                        risk="medium",
                        confidence=0.4,
                        est_monthly_savings=0.0,
                        source="heuristic",
                        rationale=(
                            f"Paid Cosmos DB for MongoDB cluster (tier {paid[0]}). Review it "
                            f"for right-sizing or idle usage — a lower tier, disabling HA, or "
                            f"pausing may cut cost."
                        ),
                        caveats=["advisory — confirm workload before changing tier or deleting"],
                        evidence={"skus": skus},
                    )
                )
        elif r.type == "microsoft.machinelearningservices/workspaces/computes":
            # ML compute (instances/clusters) bills per VM size, but cost rolls up to
            # the owning workspace — so these are advisory. Pass the workspace's
            # monthly cost as context (savings stays 0; we can't isolate the compute's
            # share). See _ml_compute_rec for the three wasteful shapes it flags.
            ws_monthly = round(monthly_cost.get(str(cfg.get("workspace_id") or ""), 0.0), 2)
            rec = _ml_compute_rec(r, ws_monthly)
            if rec is not None:
                recs.append(rec)
    return recs


# resource type -> (human noun, activity noun) for the advisory rationale. Types
# must match ``azure.activity_metrics.ACTIVITY_METRICS``.
_ACTIVITY_LABELS: dict[str, tuple[str, str]] = {
    "microsoft.network/bastionhosts": ("Azure Bastion host", "connection sessions"),
    "microsoft.storage/storageaccounts": ("storage account", "transactions"),
    "microsoft.containerregistry/registries": ("container registry", "image pulls"),
}


def detect_idle_by_activity(
    resources: list[ResourceRecord],
    activity: dict[str, ActivitySignal],
    monthly_cost: dict[str, float],
    *,
    window_days: int = 14,
    threshold: float = 0.0,
    min_monthly_cost: float = 1.0,
) -> list[Recommendation]:
    """Flag resources that billed all window but recorded ~no platform activity.

    Fires only when we actually observed metric data (``datapoints > 0``) — a
    resource with no signal is *unknown*, not idle, so we never flag on absence of
    data. A monthly-cost floor keeps trivially cheap resources (e.g. near-empty
    storage accounts) off the list, and the action is advisory only (deleting these
    needs human confirmation), so it is not an auto-executable remediation.
    """
    recs: list[Recommendation] = []
    for r in resources:
        sig = activity.get(r.resource_id)
        if sig is None or sig.datapoints == 0:
            continue  # no signal observed — cannot conclude idle
        if sig.total > threshold:
            continue  # has activity — not idle
        monthly = round(monthly_cost.get(r.resource_id, 0.0), 2)
        if monthly < min_monthly_cost:
            continue  # too cheap to be worth surfacing
        noun, activity_noun = _ACTIVITY_LABELS.get(r.type, ("resource", "activity"))
        recs.append(
            Recommendation(
                resource_id=r.resource_id,
                category="idle_by_activity",
                action="review_idle_resource",
                current_sku=r.sku,
                risk="medium",
                confidence=0.5,
                est_monthly_savings=monthly,
                source="heuristic",
                rationale=(
                    f"This {noun} billed through the period but recorded no {activity_noun} "
                    f"over the last {window_days} days (0 across {sig.datapoints} samples). "
                    f"If it is no longer needed, deleting it stops the charge."
                ),
                caveats=["May be kept for occasional or standby use — confirm before deleting"],
                evidence={
                    "metric": sig.metric_name,
                    "total": sig.total,
                    "datapoints": sig.datapoints,
                    "window_days": window_days,
                },
            )
        )
    return recs
