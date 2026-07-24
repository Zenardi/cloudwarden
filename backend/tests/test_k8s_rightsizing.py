"""Kubernetes right-sizing, idle-namespace detection, node-cost allocation (M14.12).

Positive: over-provisioned workload -> right-size rec; idle namespace -> flag.
Negative: usage absent -> no flag; balanced requests -> no rec; allocation reconciles
to the cluster node cost. Pure functions over injected fixture inventory/usage.
"""

from __future__ import annotations

from cloudwarden.analysis import k8s_rightsizing as krs
from cloudwarden.k8s import discovery, usage, workloads
from cloudwarden.models import KubeCluster, KubeUsage, KubeWorkload, NamespaceCost

_EKS = "arn:aws:eks:us-east-1:123456789012:cluster/prod-eks"


def _eks() -> tuple[KubeCluster, list[KubeWorkload], list[KubeUsage]]:
    cluster = next(c for c in discovery.discover_clusters(provider="aws") if c.cluster_id == _EKS)
    return cluster, workloads.collect_workloads(cluster), usage.collect_usage(cluster)


def test_namespace_allocation_sums_to_node_cost() -> None:
    cluster, wls, _ = _eks()
    # Act
    alloc = krs.allocate_namespace_cost(cluster, wls)
    # Assert — a partition of the cluster node cost, by requested resources.
    assert alloc and all(isinstance(a, NamespaceCost) for a in alloc)
    assert {a.namespace for a in alloc} == {"web", "batch", "data"}
    total = round(sum(a.cost for a in alloc), 2)
    assert abs(total - round(cluster.node_monthly_cost, 2)) < 0.01  # reconciles to node cost
    assert abs(sum(a.share for a in alloc) - 1.0) < 1e-6


def test_overprovisioned_workload_rightsized() -> None:
    cluster, wls, us = _eks()
    recs = krs.detect_rightsizing([cluster], wls, us)
    api = [r for r in recs if r.category == "k8s_rightsize" and r.resource_id.endswith("/web/api")]
    assert len(api) == 1
    rec = api[0]
    assert rec.action == "resize" and rec.recommended_sku
    assert rec.est_monthly_savings > 0 and rec.caveats  # advisory, quantified, caveated
    assert rec.evidence["cpu_used"] == 0.3


def test_idle_namespace_flagged() -> None:
    cluster, wls, us = _eks()
    recs = krs.detect_rightsizing([cluster], wls, us)
    idle = [r for r in recs if r.category == "k8s_idle_namespace"]
    assert any(r.resource_id.endswith("/batch") for r in idle)
    batch = next(r for r in idle if r.resource_id.endswith("/batch"))
    assert batch.est_monthly_savings > 0 and batch.caveats


def test_no_flag_without_usage_data() -> None:
    cluster, wls, us = _eks()
    recs = krs.detect_rightsizing([cluster], wls, us)
    # data/cache has NO usage row -> never flagged (right-size or idle) on absence of data.
    assert not any(r.resource_id.endswith("/data/cache") for r in recs)
    assert not any(
        r.category == "k8s_idle_namespace" and r.resource_id.endswith("/data") for r in recs
    )


def test_balanced_workload_not_rightsized() -> None:
    cluster, wls, us = _eks()
    recs = krs.detect_rightsizing([cluster], wls, us)
    # web/frontend uses ~80% of its requests -> balanced, no right-size rec.
    assert not any(
        r.category == "k8s_rightsize" and r.resource_id.endswith("/web/frontend") for r in recs
    )


def test_no_recs_when_usage_absent_everywhere() -> None:
    cluster, wls, _ = _eks()
    # No usage observed anywhere -> nothing flagged (signal-gated).
    assert krs.detect_rightsizing([cluster], wls, []) == []


def test_allocation_empty_cluster_returns_nothing() -> None:
    cluster, _, _ = _eks()
    assert krs.allocate_namespace_cost(cluster, []) == []


def test_namespace_cost_rows_are_allocated_kubernetes_rows() -> None:
    from datetime import date

    cluster, wls, _ = _eks()
    rows = krs.namespace_cost_rows([cluster], wls, date(2026, 7, 20))
    assert rows and all(r.provider == "kubernetes" and r.cost_type == "Allocated" for r in rows)
    assert all(r.tags.get("cluster") == "prod-eks" for r in rows)
    # Daily rows reconcile (×30.4) back to the cluster monthly node cost.
    monthly = round(sum(r.cost for r in rows) * 30.4, 0)
    assert abs(monthly - cluster.node_monthly_cost) < 5.0


# --- edge cases (constructed inline, no fixtures) ---
def _cluster(node_cost: float, cid: str = "c") -> KubeCluster:
    return KubeCluster(cluster_id=cid, name="c", provider="aws", node_monthly_cost=node_cost)


def _wl(ns: str, name: str, cpu: float, mem: float, cid: str = "c") -> KubeWorkload:
    return KubeWorkload(cluster_id=cid, namespace=ns, name=name, cpu_request=cpu, mem_request=mem)


def test_allocation_cpu_only_when_memory_unrequested() -> None:
    alloc = krs.allocate_namespace_cost(
        _cluster(100.0), [_wl("a", "x", 3.0, 0.0), _wl("b", "y", 1.0, 0.0)]
    )
    by = {a.namespace: a.cost for a in alloc}
    assert round(by["a"], 2) == 75.0 and round(by["b"], 2) == 25.0  # split by cpu only


def test_allocation_memory_only_when_cpu_unrequested() -> None:
    alloc = krs.allocate_namespace_cost(
        _cluster(100.0), [_wl("a", "x", 0.0, 3.0), _wl("b", "y", 0.0, 1.0)]
    )
    by = {a.namespace: a.cost for a in alloc}
    assert round(by["a"], 2) == 75.0 and round(by["b"], 2) == 25.0  # split by mem only


def test_allocation_even_split_when_no_requests() -> None:
    alloc = krs.allocate_namespace_cost(
        _cluster(100.0), [_wl("a", "x", 0.0, 0.0), _wl("b", "y", 0.0, 0.0)]
    )
    by = {a.namespace: a.cost for a in alloc}
    assert round(by["a"], 2) == 50.0 and round(by["b"], 2) == 50.0  # even split, no signal


def test_allocation_residual_lands_on_largest_namespace() -> None:
    # three equal namespaces -> 33.33 each, 0.01 rounding residual reconciled onto one.
    wls = [_wl("a", "x", 1.0, 1.0), _wl("b", "y", 1.0, 1.0), _wl("d", "z", 1.0, 1.0)]
    alloc = krs.allocate_namespace_cost(_cluster(100.0), wls)
    assert round(sum(a.cost for a in alloc), 2) == 100.0  # reconciles exactly
    assert max(a.cost for a in alloc) == 33.34


def test_workload_cost_empty_returns_empty() -> None:
    assert krs.allocate_workload_cost(_cluster(100.0), []) == {}


def test_detect_rightsizing_skips_cluster_without_workloads() -> None:
    assert krs.detect_rightsizing([_cluster(100.0, cid="empty")], [], []) == []
