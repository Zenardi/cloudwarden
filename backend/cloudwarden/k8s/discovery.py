"""Managed-Kubernetes cluster discovery per cloud provider (M14.12).

Discovery is provider-dispatched: each cloud's control plane lists its managed
clusters (EKS / AKS / GKE). The client is injectable — tests pass a fake shaped
like the live control-plane client; live builds a real one lazily. In mock mode
the recorded ``k8s/clusters.json`` fixture is replayed, filtered to the provider,
and priced with each cluster's node bill (the pool the namespace allocation
splits). Placeholder account ids are retargeted to the onboarded account.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from ..config import get_settings
from ..models import KubeCluster
from ..resilience import with_retry
from ._fixtures import load_k8s_fixture

# Placeholder ids embedded in the fixture, retargeted to the onboarded account.
_PLACEHOLDERS = {
    "aws": "123456789012",
    "azure": "00000000-0000-0000-0000-000000000000",
    "gcp": "example-project-123456",
}
_FIXTURE = "clusters"


def discover_clusters(
    *,
    provider: str,
    account: Any = None,
    client: Any = None,
    settings: Any = None,
    sleep: Callable[[float], None] = time.sleep,
) -> list[KubeCluster]:
    """Discover ``provider``'s managed clusters as normalized :class:`KubeCluster`."""
    settings = settings or get_settings()
    account_id = account.account_id if account is not None else None
    if client is None:
        client = _mock_client() if settings.finops_mock else _live_client(provider, account)
    fetch = with_retry(max_attempts=6, base_delay=2.0, max_delay=90.0, sleep=sleep)(
        client.list_clusters
    )
    raw = fetch(provider=provider)
    return [_parse_cluster(row, provider, account_id) for row in raw]


def _retarget(value: str | None, provider: str, account_id: str | None) -> str | None:
    placeholder = _PLACEHOLDERS.get(provider)
    if not value or not account_id or not placeholder or account_id == placeholder:
        return value
    return value.replace(placeholder, account_id)


def _parse_cluster(row: dict, provider: str, account_id: str | None) -> KubeCluster:
    return KubeCluster(
        cluster_id=_retarget(row.get("cluster_id"), provider, account_id),
        name=row.get("name"),
        provider=row.get("provider") or provider,
        region=row.get("region"),
        version=row.get("version"),
        node_count=int(row.get("node_count") or 0),
        node_monthly_cost=float(row.get("node_monthly_cost") or 0.0),
        currency=row.get("currency") or "USD",
        account_id=_retarget(row.get("account_id"), provider, account_id),
        config=dict(row.get("config") or {}),
    )


class _FixtureControlPlane:
    """Offline stand-in for a cloud control-plane client (single page)."""

    def list_clusters(self, *, provider: str) -> list[dict]:
        data = load_k8s_fixture(_FIXTURE)
        return [c for c in data.get("clusters", []) if c.get("provider") == provider]


def _mock_client() -> _FixtureControlPlane:
    return _FixtureControlPlane()


def _live_client(provider: str, account: Any) -> Any:  # pragma: no cover - requires live cloud
    return _LiveControlPlane(provider, account)


class _LiveControlPlane:  # pragma: no cover - requires live cloud control plane
    """Live managed-Kubernetes discovery (EKS/AKS/GKE). Out of scope for mock mode
    (M14.12 verifies end-to-end with ``FINOPS_MOCK=1`` and no live cluster); wire the
    cloud SDK list-clusters call here when running against real control planes."""

    def __init__(self, provider: str, account: Any) -> None:
        self._provider = provider
        self._account = account

    def list_clusters(self, *, provider: str) -> list[dict]:
        raise NotImplementedError(
            "live managed-Kubernetes discovery requires the cloud SDK (eks/aks/gke); "
            "run with FINOPS_MOCK=1 for the recorded fixtures"
        )
