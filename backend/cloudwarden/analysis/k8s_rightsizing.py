"""Kubernetes cost allocation + workload right-sizing / idle detection (M14.12).

Three capabilities, all pure over injected inventory/usage:

* **Namespace cost allocation** — split a cluster's node cost across its namespaces
  by requested resources (CPU + memory, weighted equally). The partition
  *reconciles*: ``sum(cost) == node_monthly_cost`` and ``sum(share) == 1``.
* **Right-sizing** — a workload whose observed usage sits well under *both* its CPU
  and memory requests is over-provisioned; propose lower requests toward usage
  (with headroom). Savings are advisory (K8s cost rolls up to the node) and caveated.
* **Idle namespace** — a namespace whose *observed* workloads recorded ~0 usage is
  flagged, with its allocated node cost as the advisory saving.

Everything is **signal-gated**: a workload/namespace with no observed usage is
*unknown*, never flagged on the absence of data.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date

from ..k8s import inventory
from ..models import (
    CostRow,
    KubeCluster,
    KubeUsage,
    KubeWorkload,
    NamespaceCost,
    Recommendation,
)

# Floor requests never go below (a workload always needs a sliver of CPU/mem).
_MIN_CPU = 0.05
_MIN_MEM = 0.05
# Average days per month, matching analysis.savings — daily allocation × this ≈ monthly.
_DAYS_PER_MONTH = 30.4


def _blended_weights(items: list[tuple]) -> dict:
    """Normalized weight per item from ``(key, cpu, mem)``: CPU-share and mem-share
    blended 50/50 (so the weights sum to 1 whenever any resource is requested)."""
    total_cpu = sum(c for _, c, _ in items)
    total_mem = sum(m for _, _, m in items)
    weights: dict = {}
    for key, cpu, mem in items:
        if total_cpu > 0 and total_mem > 0:
            weights[key] = 0.5 * (cpu / total_cpu) + 0.5 * (mem / total_mem)
        elif total_cpu > 0:
            weights[key] = cpu / total_cpu
        elif total_mem > 0:
            weights[key] = mem / total_mem
        else:
            weights[key] = 1.0 / len(items)
    return weights


def allocate_namespace_cost(
    cluster: KubeCluster, workloads: list[KubeWorkload]
) -> list[NamespaceCost]:
    """Partition ``cluster``'s node cost across namespaces by requested resources.

    Reconciles exactly: the rounded per-namespace costs sum to the (rounded) cluster
    ``node_monthly_cost`` — any cent of rounding residual lands on the largest bucket.
    """
    cws = [w for w in workloads if w.cluster_id == cluster.cluster_id]
    ns_cpu: dict[str, float] = defaultdict(float)
    ns_mem: dict[str, float] = defaultdict(float)
    for w in cws:
        ns_cpu[w.namespace] += w.cpu_request * w.replicas
        ns_mem[w.namespace] += w.mem_request * w.replicas
    namespaces = sorted(ns_cpu)
    if not namespaces:
        return []

    weights = _blended_weights([(ns, ns_cpu[ns], ns_mem[ns]) for ns in namespaces])
    node_cost = round(cluster.node_monthly_cost, 2)
    costs = {ns: round(weights[ns] * node_cost, 2) for ns in namespaces}
    residual = round(node_cost - sum(costs.values()), 2)
    if residual:
        biggest = max(namespaces, key=lambda n: costs[n])
        costs[biggest] = round(costs[biggest] + residual, 2)

    return [
        NamespaceCost(
            cluster_id=cluster.cluster_id,
            namespace=ns,
            cpu_request=round(ns_cpu[ns], 3),
            mem_request=round(ns_mem[ns], 3),
            cost=costs[ns],
            share=(costs[ns] / node_cost) if node_cost else 0.0,
            currency=cluster.currency,
        )
        for ns in namespaces
    ]


def allocate_workload_cost(
    cluster: KubeCluster, workloads: list[KubeWorkload]
) -> dict[tuple[str, str], float]:
    """Allocate node cost to ``(namespace, workload)`` by requested resources."""
    cws = [w for w in workloads if w.cluster_id == cluster.cluster_id]
    items = [
        ((w.namespace, w.name), w.cpu_request * w.replicas, w.mem_request * w.replicas) for w in cws
    ]
    if not items:
        return {}
    weights = _blended_weights(items)
    return {key: weights[key] * cluster.node_monthly_cost for key in weights}


def detect_rightsizing(
    clusters: list[KubeCluster],
    workloads: list[KubeWorkload],
    usage: list[KubeUsage],
    *,
    settings=None,
) -> list[Recommendation]:
    """Right-size over-provisioned workloads and flag idle namespaces (signal-gated)."""
    if settings is None:
        from ..config import get_settings

        settings = get_settings()
    over = settings.k8s_overprovision_threshold
    headroom = settings.k8s_rightsize_headroom
    idle_thr = settings.k8s_idle_threshold

    usage_by = {(u.cluster_id, u.namespace, u.workload): u for u in usage if u.samples > 0}
    recs: list[Recommendation] = []

    for cluster in clusters:
        cws = [w for w in workloads if w.cluster_id == cluster.cluster_id]
        if not cws:
            continue
        wl_cost = allocate_workload_cost(cluster, cws)
        ns_cost = {a.namespace: a.cost for a in allocate_namespace_cost(cluster, cws)}

        by_ns: dict[str, list[KubeWorkload]] = defaultdict(list)
        for w in cws:
            by_ns[w.namespace].append(w)

        for ns, ns_workloads in by_ns.items():
            observed = [
                (w, usage_by[(cluster.cluster_id, ns, w.name)])
                for w in ns_workloads
                if (cluster.cluster_id, ns, w.name) in usage_by
            ]
            if not observed:
                continue  # no usage observed in this namespace -> unknown, never flag

            total_cpu = sum(u.cpu_used for _, u in observed)
            total_mem = sum(u.mem_used for _, u in observed)
            if total_cpu <= idle_thr and total_mem <= idle_thr:
                recs.append(_idle_namespace_rec(cluster, ns, ns_cost.get(ns, 0.0), observed))
                continue  # idle namespace supersedes per-workload right-sizing here

            for w, u in observed:
                rec = _rightsize_rec(cluster, w, u, wl_cost.get((ns, w.name), 0.0), over, headroom)
                if rec is not None:
                    recs.append(rec)

    return recs


def _rightsize_rec(
    cluster: KubeCluster,
    w: KubeWorkload,
    u: KubeUsage,
    workload_cost: float,
    over: float,
    headroom: float,
) -> Recommendation | None:
    cpu_req = w.cpu_request * w.replicas
    mem_req = w.mem_request * w.replicas
    ratios = []
    if cpu_req > 0:
        ratios.append(u.cpu_used / cpu_req)
    if mem_req > 0:
        ratios.append(u.mem_used / mem_req)
    if not ratios or max(ratios) >= over:
        return None  # balanced (or nothing to shrink) — not over-provisioned

    new_cpu_total = max(round(u.cpu_used * headroom, 3), _MIN_CPU)
    new_mem_total = max(round(u.mem_used * headroom, 3), _MIN_MEM)
    cpu_red = (cpu_req - new_cpu_total) / cpu_req if cpu_req > 0 else 0.0
    mem_red = (mem_req - new_mem_total) / mem_req if mem_req > 0 else 0.0
    reduction = max(0.0, 0.5 * cpu_red + 0.5 * mem_red)
    savings = round(workload_cost * reduction, 2)
    new_cpu_pp = round(new_cpu_total / w.replicas, 3)
    new_mem_pp = round(new_mem_total / w.replicas, 3)

    return Recommendation(
        resource_id=inventory.workload_resource_id(cluster.cluster_id, w.namespace, w.name),
        category="k8s_rightsize",
        action="resize",
        current_sku=f"cpu={w.cpu_request},mem={w.mem_request}Gi",
        recommended_sku=f"cpu={new_cpu_pp},mem={new_mem_pp}Gi",
        risk="low",
        confidence=0.6,
        est_monthly_savings=savings,
        currency=cluster.currency,
        source="heuristic",
        rationale=(
            f"Workload {w.namespace}/{w.name} requests {cpu_req:.2f} vCPU / {mem_req:.2f} GiB "
            f"across {w.replicas} replica(s) but used only {u.cpu_used:.2f} vCPU / "
            f"{u.mem_used:.2f} GiB. Lower requests toward observed usage "
            f"(+{int(round((headroom - 1) * 100))}% headroom) to reclaim node capacity."
        ),
        caveats=[
            "requests-vs-usage estimate — validate against peak usage and limits before applying",
            "K8s cost rolls up to the node; savings realize only if freed capacity is scaled in",
        ],
        evidence={
            "cpu_request": cpu_req,
            "mem_request": mem_req,
            "cpu_used": u.cpu_used,
            "mem_used": u.mem_used,
            "samples": u.samples,
            "recommended_cpu_per_pod": new_cpu_pp,
            "recommended_mem_per_pod": new_mem_pp,
            "workload_monthly_cost": round(workload_cost, 2),
        },
    )


def _idle_namespace_rec(
    cluster: KubeCluster, namespace: str, allocated: float, observed: list[tuple]
) -> Recommendation:
    total_cpu = sum(u.cpu_used for _, u in observed)
    total_mem = sum(u.mem_used for _, u in observed)
    samples = sum(u.samples for _, u in observed)
    return Recommendation(
        resource_id=inventory.namespace_resource_id(cluster.cluster_id, namespace),
        category="k8s_idle_namespace",
        action="review_idle_namespace",
        risk="medium",
        confidence=0.5,
        est_monthly_savings=round(allocated, 2),
        currency=cluster.currency,
        source="heuristic",
        rationale=(
            f"Namespace {namespace} on cluster {cluster.name} was allocated ~{allocated:.2f} "
            f"{cluster.currency}/mo of node capacity but its workloads recorded ~0 usage "
            f"({total_cpu:.2f} vCPU / {total_mem:.2f} GiB across {samples} samples). If it is "
            f"abandoned, deleting it (or scaling to zero) reclaims the allocated node capacity."
        ),
        caveats=[
            "allocated (not metered) cost — savings realize only if the node pool is scaled in",
            "namespace may be a warm standby — confirm before deleting",
        ],
        evidence={
            "cpu_used": round(total_cpu, 3),
            "mem_used": round(total_mem, 3),
            "samples": samples,
            "allocated_monthly_cost": round(allocated, 2),
        },
    )


def namespace_cost_rows(
    clusters: list[KubeCluster], workloads: list[KubeWorkload], usage_date: date
) -> list[CostRow]:
    """Namespace allocation as persistable daily ``CostRow`` rows (M14.12).

    Emitted with ``provider="kubernetes"`` and ``cost_type="Allocated"`` so they are
    self-describing and stay out of the Amortized cloud-cost queries/analytics while
    still feeding the K8s namespace-cost dashboard panel. Monthly node cost is spread
    to a daily figure (``/30.4``)."""
    rows: list[CostRow] = []
    for c in clusters:
        for a in allocate_namespace_cost(c, workloads):
            rows.append(
                CostRow(
                    usage_date=usage_date,
                    resource_id=inventory.namespace_resource_id(c.cluster_id, a.namespace),
                    subscription_id=c.account_id,
                    provider="kubernetes",
                    resource_type="k8s.namespace",
                    resource_group=c.name,
                    location=c.region,
                    service_name="Kubernetes",
                    meter_category="node-allocation",
                    cost=round(a.cost / _DAYS_PER_MONTH, 6),
                    currency=c.currency,
                    cost_type="Allocated",
                    tags={"cluster": c.name, "namespace": a.namespace, "cloud": c.provider},
                )
            )
    return rows
