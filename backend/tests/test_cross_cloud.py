"""M12.4 — cross-cloud AssetDB & dashboards.

Written test-first (TDD). DB-backed (the ``db`` testcontainers fixture) against
seeded multi-cloud rows. The provider dimension unifies Azure/AWS/GCP in a single
pane:

* **AssetDB** — ``query_assets`` filters by the allow-listed ``provider`` column so
  a query returns only one cloud's assets (or all clouds by default).
* **Posture** (M9.1) & **execution-health** (M9.2) — each grows a ``by_provider``
  rollup, and both repo helpers + their governance endpoints accept an optional
  ``provider`` filter that defaults to *all providers*.

Provider is intrinsic to the account: an execution's provider is its
subscription's ``provider`` (an un-onboarded subscription defaults to ``azure``,
mirroring the ``server_default='azure'`` backfill throughout M12).
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from azure_finops.api.main import app
from azure_finops.models import AssetFilter, AssetQuery, ResourceRecord
from azure_finops.storage import repository as repo
from azure_finops.storage.db import session_scope

# --------------------------------------------------------------------------- #
# Seed helpers
# --------------------------------------------------------------------------- #


def _asset(resource_id: str, *, provider: str, subscription_id: str) -> ResourceRecord:
    return ResourceRecord(
        resource_id=resource_id,
        name=resource_id.rsplit("/", 1)[-1],
        type=f"{provider}.vm",
        location="r1",
        resource_group="",
        subscription_id=subscription_id,
        provider=provider,
    )


def _make_policy(session, name: str) -> int:
    return repo.create_policy(
        session,
        name=name,
        resource_type="azure.vm",
        spec={"policies": [{"name": name, "resource": "azure.vm"}]},
    )["id"]


def _seed_execution(
    session,
    *,
    execution_id: str,
    policy_id: int,
    subscription_id: str,
    resources_matched: int = 0,
    status: str = "succeeded",
) -> None:
    repo.create_policy_execution(
        session,
        execution_id=execution_id,
        policy_id=policy_id,
        subscription_id=subscription_id,
    )
    repo.finish_policy_execution(
        session, execution_id, status=status, resources_matched=resources_matched
    )


def _seed_multi_cloud(session) -> int:
    """Onboard one subscription per cloud and run a policy against each. Returns the policy id."""
    repo.upsert_subscription(
        session, subscription_id="az-1", display_name="Azure", provider="azure"
    )
    repo.upsert_subscription(session, subscription_id="aws-1", display_name="AWS", provider="aws")
    repo.upsert_subscription(session, subscription_id="gcp-1", display_name="GCP", provider="gcp")
    pid = _make_policy(session, "p1")
    _seed_execution(
        session, execution_id="e1", policy_id=pid, subscription_id="az-1", resources_matched=0
    )
    _seed_execution(
        session, execution_id="e2", policy_id=pid, subscription_id="aws-1", resources_matched=2
    )
    _seed_execution(
        session, execution_id="e3", policy_id=pid, subscription_id="gcp-1", resources_matched=5
    )
    return pid


# --------------------------------------------------------------------------- #
# AssetDB — filter by provider
# --------------------------------------------------------------------------- #
def test_asset_query_filter_by_provider(db) -> None:
    # Arrange — one asset per cloud.
    with session_scope() as s:
        repo.upsert_assets(
            s,
            [
                _asset("/az/vm-1", provider="azure", subscription_id="az-1"),
                _asset("arn:aws:ec2:vm-2", provider="aws", subscription_id="aws-1"),
                _asset("//compute/vm-3", provider="gcp", subscription_id="gcp-1"),
            ],
        )
    # Act — filter to AWS only.
    with session_scope() as s:
        rows = repo.query_assets(
            s, AssetQuery(filters=[AssetFilter(column="provider", op="eq", value="aws")])
        )
    # Assert — only the AWS asset comes back, tagged provider='aws'.
    assert [r["resource_id"] for r in rows] == ["arn:aws:ec2:vm-2"]
    assert rows[0]["provider"] == "aws"


def test_asset_query_default_returns_all_providers(db) -> None:
    with session_scope() as s:
        repo.upsert_assets(
            s,
            [
                _asset("/az/vm-1", provider="azure", subscription_id="az-1"),
                _asset("arn:aws:ec2:vm-2", provider="aws", subscription_id="aws-1"),
                _asset("//compute/vm-3", provider="gcp", subscription_id="gcp-1"),
            ],
        )
    with session_scope() as s:
        rows = repo.query_assets(s, AssetQuery(filters=[]))
    assert {r["provider"] for r in rows} == {"azure", "aws", "gcp"}


def test_asset_query_filter_by_provider_via_api(db) -> None:
    with session_scope() as s:
        repo.upsert_assets(
            s,
            [
                _asset("/az/vm-1", provider="azure", subscription_id="az-1"),
                _asset("//compute/vm-3", provider="gcp", subscription_id="gcp-1"),
            ],
        )
    body = {"filters": [{"column": "provider", "op": "eq", "value": "gcp"}]}
    resp = TestClient(app).post("/api/assets/query", json=body)
    assert resp.status_code == 200
    rows = resp.json()
    assert [r["resource_id"] for r in rows] == ["//compute/vm-3"]
    assert rows[0]["provider"] == "gcp"


# --------------------------------------------------------------------------- #
# Posture — group by / filter by provider
# --------------------------------------------------------------------------- #
def test_posture_group_by_provider(db) -> None:
    # Arrange
    with session_scope() as s:
        _seed_multi_cloud(s)
    # Act
    with session_scope() as s:
        by_provider = {r["provider"]: r for r in repo.governance_posture(s)["by_provider"]}
    # Assert — each cloud is its own row with the right posture.
    assert set(by_provider) == {"azure", "aws", "gcp"}
    assert by_provider["azure"]["compliant"] == 1
    assert by_provider["azure"]["non_compliant"] == 0
    assert by_provider["aws"]["non_compliant"] == 1
    assert by_provider["aws"]["violations"] == 2
    assert by_provider["gcp"]["violations"] == 5


def test_posture_defaults_provider_azure_for_unonboarded_subscription(db) -> None:
    # A subscription that was never onboarded (no subscriptions row) counts as azure.
    with session_scope() as s:
        pid = _make_policy(s, "p1")
        _seed_execution(
            s, execution_id="e1", policy_id=pid, subscription_id="ghost", resources_matched=3
        )
    with session_scope() as s:
        by_provider = {r["provider"]: r for r in repo.governance_posture(s)["by_provider"]}
    assert list(by_provider) == ["azure"]
    assert by_provider["azure"]["violations"] == 3


def test_posture_filter_by_provider(db) -> None:
    # Arrange
    with session_scope() as s:
        _seed_multi_cloud(s)
    # Act — filter the whole posture to AWS.
    with session_scope() as s:
        posture = repo.governance_posture(s, provider="aws")
    # Assert — totals & every rollup reflect AWS alone.
    assert posture["totals"] == {
        "compliant": 0,
        "non_compliant": 1,
        "violations": 2,
        "evaluated": 1,
    }
    assert [r["provider"] for r in posture["by_provider"]] == ["aws"]
    assert {r["subscription_id"] for r in posture["by_subscription"]} == {"aws-1"}


def test_posture_default_all_providers(db) -> None:
    # No provider filter → totals aggregate across all three clouds.
    with session_scope() as s:
        _seed_multi_cloud(s)
    with session_scope() as s:
        totals = repo.governance_posture(s)["totals"]
    assert totals["evaluated"] == 3
    assert totals["compliant"] == 1
    assert totals["non_compliant"] == 2
    assert totals["violations"] == 7  # 0 + 2 + 5


def test_posture_by_provider_sums_to_totals(db) -> None:
    # Mixed-cloud aggregation: the per-provider rollup reconciles with the totals.
    with session_scope() as s:
        _seed_multi_cloud(s)
    with session_scope() as s:
        posture = repo.governance_posture(s)
    assert sum(r["violations"] for r in posture["by_provider"]) == posture["totals"]["violations"]
    assert sum(r["evaluated"] for r in posture["by_provider"]) == posture["totals"]["evaluated"]


def test_posture_api_accepts_provider_param(db) -> None:
    with session_scope() as s:
        _seed_multi_cloud(s)
    body = TestClient(app).get("/api/governance/posture?provider=gcp").json()
    assert body["totals"]["violations"] == 5
    assert [r["provider"] for r in body["by_provider"]] == ["gcp"]


def test_posture_api_provider_all_is_unfiltered(db) -> None:
    # provider=all behaves exactly like omitting the param.
    with session_scope() as s:
        _seed_multi_cloud(s)
    body = TestClient(app).get("/api/governance/posture?provider=all").json()
    assert body["totals"]["evaluated"] == 3
    assert {r["provider"] for r in body["by_provider"]} == {"azure", "aws", "gcp"}


# --------------------------------------------------------------------------- #
# Execution health — group by / filter by provider
# --------------------------------------------------------------------------- #
def test_execution_health_group_by_provider(db) -> None:
    # Arrange — az succeeds, aws succeeds, gcp fails.
    with session_scope() as s:
        repo.upsert_subscription(s, subscription_id="az-1", display_name="Azure", provider="azure")
        repo.upsert_subscription(s, subscription_id="aws-1", display_name="AWS", provider="aws")
        repo.upsert_subscription(s, subscription_id="gcp-1", display_name="GCP", provider="gcp")
        pid = _make_policy(s, "p1")
        _seed_execution(
            s, execution_id="e1", policy_id=pid, subscription_id="az-1", status="succeeded"
        )
        _seed_execution(
            s, execution_id="e2", policy_id=pid, subscription_id="aws-1", status="succeeded"
        )
        _seed_execution(
            s, execution_id="e3", policy_id=pid, subscription_id="gcp-1", status="failed"
        )
    # Act
    with session_scope() as s:
        by_provider = {r["provider"]: r for r in repo.execution_health(s)["by_provider"]}
    # Assert
    assert set(by_provider) == {"azure", "aws", "gcp"}
    assert by_provider["azure"]["succeeded"] == 1
    assert by_provider["azure"]["failed"] == 0
    assert by_provider["gcp"]["failed"] == 1
    assert by_provider["gcp"]["success_rate"] == 0.0


def test_execution_health_default_all_providers(db) -> None:
    # by_provider present and totals across clouds by default; empty state is a list.
    with session_scope() as s:
        health = repo.execution_health(s)
    assert health["by_provider"] == []
    with session_scope() as s:
        _seed_multi_cloud(s)
    with session_scope() as s:
        providers = {r["provider"] for r in repo.execution_health(s)["by_provider"]}
    assert providers == {"azure", "aws", "gcp"}


def test_execution_health_filter_by_provider(db) -> None:
    with session_scope() as s:
        repo.upsert_subscription(s, subscription_id="aws-1", display_name="AWS", provider="aws")
        repo.upsert_subscription(s, subscription_id="gcp-1", display_name="GCP", provider="gcp")
        pid = _make_policy(s, "p1")
        _seed_execution(
            s, execution_id="e1", policy_id=pid, subscription_id="aws-1", status="succeeded"
        )
        _seed_execution(
            s, execution_id="e2", policy_id=pid, subscription_id="gcp-1", status="failed"
        )
    with session_scope() as s:
        health = repo.execution_health(s, provider="aws")
    # by_policy is scoped to AWS executions only (1 run, all succeeded).
    (row,) = health["by_policy"]
    assert row["total_executions"] == 1
    assert row["succeeded"] == 1
    assert [r["provider"] for r in health["by_provider"]] == ["aws"]


def test_execution_health_mixed_cloud_aggregation(db) -> None:
    # by_policy (all providers) still counts every run; by_provider partitions them.
    with session_scope() as s:
        _seed_multi_cloud(s)
    with session_scope() as s:
        health = repo.execution_health(s)
    (prow,) = health["by_policy"]
    assert prow["total_executions"] == 3
    assert sum(r["total_executions"] for r in health["by_provider"]) == 3


def test_execution_health_api_accepts_provider_param(db) -> None:
    with session_scope() as s:
        _seed_multi_cloud(s)
    body = TestClient(app).get("/api/governance/execution-health?provider=gcp").json()
    assert [r["provider"] for r in body["by_provider"]] == ["gcp"]
    assert body["by_policy"][0]["total_executions"] == 1
