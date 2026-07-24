"""Kubernetes inventory (M14.12): cluster discovery, workload enumeration, AssetDB ingest.

Kube/metrics clients are injected or fixture-backed — no live cluster. Each test
asserts one behaviour, Arrange-Act-Assert.
"""

from __future__ import annotations

from cloudwarden.azure.context import AccountContext
from cloudwarden.k8s import discovery, inventory, usage, workloads
from cloudwarden.models import KubeCluster, KubeUsage, KubeWorkload

_EKS = "arn:aws:eks:us-east-1:123456789012:cluster/prod-eks"


def _eks_cluster() -> KubeCluster:
    return next(c for c in discovery.discover_clusters(provider="aws") if c.cluster_id == _EKS)


# --- injected fakes, shaped like the live control-plane client (never unittest.mock) ---
class _FakeControlPlane:
    """Returns preset cluster rows, recording the provider it was asked for."""

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.calls: list[str] = []

    def list_clusters(self, *, provider: str) -> list[dict]:
        self.calls.append(provider)
        return self.rows


class _Throttle(Exception):
    """A retryable control-plane throttle (HTTP 429) — resilience.with_retry retries it."""

    status_code = 429


class _ThrottleThenClusters:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.attempts = 0

    def list_clusters(self, *, provider: str) -> list[dict]:
        self.attempts += 1
        if self.attempts == 1:
            raise _Throttle("Too Many Requests")
        return self.rows


def test_clusters_discovered_per_provider() -> None:
    # Act / Assert — mock discovery replays the fixture, one managed cluster per cloud.
    for provider, expected in (("aws", "prod-eks"), ("azure", "prod-aks"), ("gcp", "prod-gke")):
        clusters = discovery.discover_clusters(provider=provider)
        assert clusters and all(isinstance(c, KubeCluster) for c in clusters)
        assert all(c.provider == provider for c in clusters)  # only that cloud's clusters
        assert all(c.node_monthly_cost > 0 for c in clusters)  # priced for allocation
        assert any(c.name == expected for c in clusters)


def test_workload_requests_enumerated() -> None:
    cluster = _eks_cluster()
    # Act
    wls = workloads.collect_workloads(cluster)
    # Assert — namespace -> workload with per-pod requests/limits.
    assert wls and all(isinstance(w, KubeWorkload) for w in wls)
    assert {w.namespace for w in wls} >= {"web", "batch", "data"}
    api = next(w for w in wls if w.namespace == "web" and w.name == "api")
    assert api.cpu_request == 1.0 and api.mem_request == 2.0
    assert api.replicas == 2 and api.kind == "Deployment"


def test_usage_signal_gated_on_samples() -> None:
    cluster = _eks_cluster()
    us = usage.collect_usage(cluster)
    assert us and all(isinstance(u, KubeUsage) for u in us)
    assert all(u.samples > 0 for u in us)  # observed usage carries a sample count
    # data/cache is intentionally absent from the usage fixture (unknown, not idle).
    assert not any(u.namespace == "data" for u in us)


def test_discovery_injected_client_takes_precedence() -> None:
    fake = _FakeControlPlane(
        [{"cluster_id": "c1", "name": "injected", "provider": "aws", "node_monthly_cost": 10.0}]
    )
    clusters = discovery.discover_clusters(provider="aws", client=fake)
    assert [c.name for c in clusters] == ["injected"]  # fixture bypassed
    assert fake.calls == ["aws"]


def test_discovery_throttle_retried() -> None:
    fake = _ThrottleThenClusters(
        [{"cluster_id": "c1", "name": "n", "provider": "aws", "node_monthly_cost": 1.0}]
    )
    clusters = discovery.discover_clusters(provider="aws", client=fake, sleep=lambda _s: None)
    assert fake.attempts == 2 and len(clusters) == 1


def test_placeholder_account_retargeted() -> None:
    ctx = AccountContext(account_id="999988887777", provider="aws")
    clusters = discovery.discover_clusters(provider="aws", account=ctx)
    eks = next(c for c in clusters if c.name == "prod-eks")
    assert "999988887777" in eks.cluster_id and eks.account_id == "999988887777"


def test_placeholder_account_not_retargeted_when_default() -> None:
    # No account -> the recorded placeholder id is kept verbatim (no retarget).
    eks = _eks_cluster()
    assert "123456789012" in eks.cluster_id


def test_k8s_resources_ingested_into_assetdb(db) -> None:
    from sqlalchemy import select

    from cloudwarden.storage import repository as repo
    from cloudwarden.storage import schema
    from cloudwarden.storage.db import session_scope

    cluster = _eks_cluster()
    wls = workloads.collect_workloads(cluster)
    records = inventory.build_records([cluster], wls)

    with session_scope() as s:
        repo.upsert_assets(s, records)

    with session_scope() as s:
        rows = [
            {"id": r.resource_id, "type": r.type, "name": r.name, "tags": dict(r.tags)}
            for r in s.execute(
                select(schema.Asset).where(schema.Asset.provider == "kubernetes")
            ).scalars()
        ]

    types = {r["type"] for r in rows}
    assert {"k8s.cluster", "k8s.namespace", "k8s.workload"} <= types
    # Namespaces are scoped by cluster + namespace via tags.
    web_ns = next(
        r for r in rows if r["type"] == "k8s.namespace" and r["tags"].get("namespace") == "web"
    )
    assert web_ns["tags"]["cluster"] == "prod-eks"
    api_wl = next(r for r in rows if r["type"] == "k8s.workload" and r["name"] == "api")
    assert api_wl["tags"]["cluster"] == "prod-eks" and api_wl["tags"]["namespace"] == "web"


def test_k8s_api_endpoints(db) -> None:
    from fastapi.testclient import TestClient

    from cloudwarden.api.main import app

    c = TestClient(app)
    clusters = c.get("/api/k8s/clusters").json()
    assert clusters and any(x["name"] == "prod-eks" for x in clusters)
    assert {x["provider"] for x in clusters} == {"aws", "azure", "gcp"}

    aws_only = c.get("/api/k8s/clusters?provider=aws").json()
    assert aws_only and all(x["provider"] == "aws" for x in aws_only)

    ns = c.get("/api/k8s/namespaces?provider=aws").json()
    assert ns and any(x["namespace"] == "batch" for x in ns)

    recs = c.get("/api/k8s/recommendations?provider=aws").json()
    assert any(r["category"] == "k8s_idle_namespace" for r in recs)

    assert c.get("/api/k8s/clusters?provider=xyz").status_code == 400


def test_k8s_collect_persists_assets_and_allocated_cost(db) -> None:
    from cloudwarden.orchestrator import run_kubernetes
    from cloudwarden.storage import repository as repo
    from cloudwarden.storage.db import session_scope

    counts = run_kubernetes()
    assert counts["k8s_clusters"] == 3  # aws + azure + gcp
    assert counts["k8s_assets"] > 0 and counts["k8s_cost_rows"] > 0

    with session_scope() as s:
        n_assets = repo._rows(s, "SELECT COUNT(*) AS n FROM assets WHERE provider = 'kubernetes'")[
            0
        ]["n"]
        n_cost = repo._rows(
            s,
            "SELECT COUNT(*) AS n FROM cost_snapshots "
            "WHERE provider = 'kubernetes' AND cost_type = 'Allocated'",
        )[0]["n"]
        # Allocated K8s cost is excluded from the Amortized cloud-cost queries.
        amortized = repo.total_cost(s, days=3650, provider="kubernetes")
    assert n_assets > 0 and n_cost > 0 and amortized == 0.0


def test_k8s_collect_endpoint(db) -> None:
    from fastapi.testclient import TestClient

    from cloudwarden.api.main import app

    c = TestClient(app)
    body = c.post("/api/k8s/collect?provider=aws").json()
    assert body["k8s_clusters"] == 1 and body["k8s_assets"] > 0
    assert c.post("/api/k8s/collect?provider=xyz").status_code == 400
