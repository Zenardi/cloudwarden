"""Kubernetes workload inventory per cluster (M14.12).

Enumerates namespace -> workload with aggregated per-pod resource requests/limits
(CPU cores, memory GiB). The kube client is injectable; mock mode replays
``k8s/workloads.json`` filtered to the cluster. Requests drive the namespace cost
allocation and, joined to usage, the right-sizing detector.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from ..config import get_settings
from ..models import KubeCluster, KubeWorkload
from ..resilience import with_retry
from ._fixtures import cluster_key, load_k8s_fixture

_FIXTURE = "workloads"


def collect_workloads(
    cluster: KubeCluster,
    *,
    client: Any = None,
    settings: Any = None,
    sleep: Callable[[float], None] = time.sleep,
) -> list[KubeWorkload]:
    """Enumerate ``cluster``'s workloads as normalized :class:`KubeWorkload`."""
    settings = settings or get_settings()
    if client is None:
        client = _mock_client() if settings.finops_mock else _live_client(cluster)
    fetch = with_retry(max_attempts=6, base_delay=2.0, max_delay=90.0, sleep=sleep)(
        client.list_workloads
    )
    raw = fetch(cluster_id=cluster.cluster_id)
    return [_parse_workload(row, cluster) for row in raw]


def _parse_workload(row: dict, cluster: KubeCluster) -> KubeWorkload:
    return KubeWorkload(
        cluster_id=cluster.cluster_id,
        namespace=row.get("namespace"),
        name=row.get("name"),
        kind=row.get("kind") or "Deployment",
        replicas=int(row.get("replicas") or 1),
        cpu_request=float(row.get("cpu_request") or 0.0),
        mem_request=float(row.get("mem_request") or 0.0),
        cpu_limit=float(row.get("cpu_limit") or 0.0),
        mem_limit=float(row.get("mem_limit") or 0.0),
        config=dict(row.get("config") or {}),
    )


class _FixtureKube:
    """Offline stand-in for a kube API client, matching workloads by cluster name."""

    def list_workloads(self, *, cluster_id: str) -> list[dict]:
        data = load_k8s_fixture(_FIXTURE)
        key = cluster_key(cluster_id)
        return [w for w in data.get("workloads", []) if cluster_key(w.get("cluster_id", "")) == key]


def _mock_client() -> _FixtureKube:
    return _FixtureKube()


def _live_client(cluster: KubeCluster) -> Any:  # pragma: no cover - requires live cluster
    return _LiveKube(cluster)


class _LiveKube:  # pragma: no cover - requires live cluster kube API
    """Live kube-API workload enumeration. Out of scope for mock mode (M14.12);
    wire the ``kubernetes`` client list-workloads-with-requests call here."""

    def __init__(self, cluster: KubeCluster) -> None:
        self._cluster = cluster

    def list_workloads(self, *, cluster_id: str) -> list[dict]:
        raise NotImplementedError(
            "live workload inventory requires the kubernetes client; "
            "run with FINOPS_MOCK=1 for the recorded fixtures"
        )
