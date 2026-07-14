"""Per-policy compliance & health metrics (M3.4): ``v_policy_health`` + API.

Written test-first (TDD). DB-backed (the ``db`` testcontainers fixture) against
seeded ``PolicyExecution`` rows: the ``v_policy_health`` view aggregates each
policy's executions into matched counts, last status and a success rate, across
every subscription the policy ran in. A policy with no executions is absent (inner
join), so the empty state is an empty list — never an error. ``GET
/api/governance/policy-health`` is a thin read over the same helper.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from cloudwarden.api.main import app
from cloudwarden.storage import repository as repo
from cloudwarden.storage.db import session_scope


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
    status: str = "succeeded",
    resources_matched: int = 0,
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


# --------------------------------------------------------------------------- #
# View / repo helper
# --------------------------------------------------------------------------- #
def test_policy_health_empty(db) -> None:
    with session_scope() as s:
        assert repo.policy_health(s) == []


def test_policy_health_after_execution(db) -> None:
    with session_scope() as s:
        pid = _make_policy(s, "p1")
        _seed_execution(
            s,
            execution_id="e1",
            policy_id=pid,
            subscription_id="sub-a",
            status="succeeded",
            resources_matched=2,
        )

    with session_scope() as s:
        (row,) = repo.policy_health(s)
    assert row["policy_id"] == pid
    assert row["policy_name"] == "p1"
    assert row["total_executions"] == 1
    assert row["succeeded_executions"] == 1
    assert row["failed_executions"] == 0
    assert row["total_matches"] == 2
    assert row["last_status"] == "succeeded"
    assert row["success_rate"] == 1.0


def test_policy_health_success_rate(db) -> None:
    with session_scope() as s:
        pid = _make_policy(s, "p1")
        _seed_execution(
            s, execution_id="e1", policy_id=pid, subscription_id="s", status="succeeded"
        )
        _seed_execution(
            s, execution_id="e2", policy_id=pid, subscription_id="s", status="succeeded"
        )
        _seed_execution(s, execution_id="e3", policy_id=pid, subscription_id="s", status="failed")

    with session_scope() as s:
        (row,) = repo.policy_health(s)
    assert row["total_executions"] == 3
    assert row["succeeded_executions"] == 2
    assert row["failed_executions"] == 1
    assert row["success_rate"] == 0.6667


def test_policy_health_aggregates_across_subscriptions(db) -> None:
    with session_scope() as s:
        pid = _make_policy(s, "p1")
        _seed_execution(
            s, execution_id="e1", policy_id=pid, subscription_id="sub-a", resources_matched=1
        )
        _seed_execution(
            s, execution_id="e2", policy_id=pid, subscription_id="sub-b", resources_matched=3
        )

    with session_scope() as s:
        (row,) = repo.policy_health(s)
    assert row["total_executions"] == 2
    assert row["subscriptions"] == 2
    assert row["total_matches"] == 4


def test_policy_health_unknown_policy_absent(db) -> None:
    # A policy that has never executed must not appear in the health list.
    with session_scope() as s:
        p1 = _make_policy(s, "has-exec")
        _make_policy(s, "no-exec")
        _seed_execution(s, execution_id="e1", policy_id=p1, subscription_id="s")

    with session_scope() as s:
        names = {r["policy_name"] for r in repo.policy_health(s)}
    assert names == {"has-exec"}


# --------------------------------------------------------------------------- #
# API endpoint
# --------------------------------------------------------------------------- #
def test_policy_health_api_empty_returns_list(db) -> None:
    resp = TestClient(app).get("/api/governance/policy-health")
    assert resp.status_code == 200
    assert resp.json() == []


def test_policy_health_api_returns_aggregates(db) -> None:
    with session_scope() as s:
        pid = _make_policy(s, "p1")
        _seed_execution(
            s,
            execution_id="e1",
            policy_id=pid,
            subscription_id="sub-a",
            status="succeeded",
            resources_matched=2,
        )

    rows = TestClient(app).get("/api/governance/policy-health").json()
    assert len(rows) == 1
    assert rows[0]["policy_name"] == "p1"
    assert rows[0]["total_matches"] == 2
    assert rows[0]["success_rate"] == 1.0
