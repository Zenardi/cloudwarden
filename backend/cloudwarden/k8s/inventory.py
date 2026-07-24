"""Turn discovered Kubernetes clusters/workloads into AssetDB records (M14.12).

K8s resources are surfaced in AssetDB as their own ``provider="kubernetes"``
dimension, scoped by cluster + namespace via tags so they are queryable alongside
cloud resources. Three synthetic resource types are emitted — ``k8s.cluster``,
``k8s.namespace`` and ``k8s.workload`` — with stable ids that the right-sizing
recommendations reference (so a rec joins back to its asset).
"""

from __future__ import annotations

from ..models import KubeCluster, KubeWorkload, ResourceRecord
from ._fixtures import cluster_key

PROVIDER = "kubernetes"


def namespace_resource_id(cluster_id: str, namespace: str) -> str:
    """Stable AssetDB / recommendation id for a namespace within a cluster."""
    return f"{cluster_id}/{namespace}"


def workload_resource_id(cluster_id: str, namespace: str, name: str) -> str:
    """Stable AssetDB / recommendation id for a workload within a namespace."""
    return f"{cluster_id}/{namespace}/{name}"


def build_records(
    clusters: list[KubeCluster], workloads: list[KubeWorkload]
) -> list[ResourceRecord]:
    """Build ``ResourceRecord`` rows for clusters, namespaces and workloads."""
    records: list[ResourceRecord] = []
    by_cluster = {c.cluster_id: c for c in clusters}

    for c in clusters:
        records.append(
            ResourceRecord(
                resource_id=c.cluster_id,
                name=c.name,
                type="k8s.cluster",
                location=c.region or "",
                resource_group="",
                subscription_id=c.account_id or "",
                provider=PROVIDER,
                sku=f"nodes={c.node_count}",
                tags={"cluster": c.name, "cloud": c.provider},
                config={
                    "version": c.version,
                    "node_count": c.node_count,
                    "node_monthly_cost": c.node_monthly_cost,
                    "currency": c.currency,
                    "cloud": c.provider,
                    **c.config,
                },
            )
        )

    seen_ns: set[str] = set()
    for w in workloads:
        c = by_cluster.get(w.cluster_id)
        cname = c.name if c else cluster_key(w.cluster_id)
        region = (c.region if c else None) or ""
        account = (c.account_id if c else None) or ""

        ns_id = namespace_resource_id(w.cluster_id, w.namespace)
        if ns_id not in seen_ns:
            seen_ns.add(ns_id)
            records.append(
                ResourceRecord(
                    resource_id=ns_id,
                    name=w.namespace,
                    type="k8s.namespace",
                    location=region,
                    resource_group=cname,
                    subscription_id=account,
                    provider=PROVIDER,
                    tags={"cluster": cname, "namespace": w.namespace},
                    config={"cluster_id": w.cluster_id},
                )
            )

        records.append(
            ResourceRecord(
                resource_id=workload_resource_id(w.cluster_id, w.namespace, w.name),
                name=w.name,
                type="k8s.workload",
                location=region,
                resource_group=cname,
                subscription_id=account,
                provider=PROVIDER,
                sku=w.kind,
                tags={"cluster": cname, "namespace": w.namespace, "workload": w.name},
                config={
                    "kind": w.kind,
                    "replicas": w.replicas,
                    "cpu_request": w.cpu_request,
                    "mem_request": w.mem_request,
                    "cpu_limit": w.cpu_limit,
                    "mem_limit": w.mem_limit,
                    "cluster_id": w.cluster_id,
                    "namespace": w.namespace,
                },
            )
        )

    return records
