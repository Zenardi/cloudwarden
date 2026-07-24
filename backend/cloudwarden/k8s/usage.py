"""Kubernetes workload usage per cluster (M14.12).

Joins actual observed CPU/memory usage (metrics-server / Prometheus) to workloads.
The metrics client is injectable; mock mode replays ``k8s/usage.json`` filtered to
the cluster. A workload with no usage row is *unknown* — the detectors never flag
on the absence of a usage signal (see ``KubeUsage.samples``).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from ..config import get_settings
from ..models import KubeCluster, KubeUsage
from ..resilience import with_retry
from ._fixtures import cluster_key, load_k8s_fixture

_FIXTURE = "usage"


def collect_usage(
    cluster: KubeCluster,
    *,
    client: Any = None,
    settings: Any = None,
    sleep: Callable[[float], None] = time.sleep,
) -> list[KubeUsage]:
    """Collect ``cluster``'s observed workload usage as normalized :class:`KubeUsage`."""
    settings = settings or get_settings()
    if client is None:
        client = _mock_client() if settings.finops_mock else _live_client(cluster)
    fetch = with_retry(max_attempts=6, base_delay=2.0, max_delay=90.0, sleep=sleep)(
        client.query_usage
    )
    raw = fetch(cluster_id=cluster.cluster_id)
    return [_parse_usage(row, cluster) for row in raw]


def _parse_usage(row: dict, cluster: KubeCluster) -> KubeUsage:
    return KubeUsage(
        cluster_id=cluster.cluster_id,
        namespace=row.get("namespace"),
        workload=row.get("workload"),
        cpu_used=float(row.get("cpu_used") or 0.0),
        mem_used=float(row.get("mem_used") or 0.0),
        samples=int(row.get("samples") or 0),
    )


class _FixtureMetrics:
    """Offline stand-in for a metrics client, matching usage by cluster name."""

    def query_usage(self, *, cluster_id: str) -> list[dict]:
        data = load_k8s_fixture(_FIXTURE)
        key = cluster_key(cluster_id)
        return [u for u in data.get("usage", []) if cluster_key(u.get("cluster_id", "")) == key]


def _mock_client() -> _FixtureMetrics:
    return _FixtureMetrics()


def _live_client(cluster: KubeCluster) -> Any:  # pragma: no cover - requires live metrics
    return _LiveMetrics(cluster)


class _LiveMetrics:  # pragma: no cover - requires live metrics-server / Prometheus
    """Live usage source (metrics-server / Prometheus). Out of scope for mock mode
    (M14.12); wire the range-query for per-workload cpu/mem here."""

    def __init__(self, cluster: KubeCluster) -> None:
        self._cluster = cluster

    def query_usage(self, *, cluster_id: str) -> list[dict]:
        raise NotImplementedError(
            "live usage requires metrics-server / Prometheus; "
            "run with FINOPS_MOCK=1 for the recorded fixtures"
        )
