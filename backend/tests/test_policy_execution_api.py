"""Execution-history API (M3.3): ``/api/policy-executions`` + drill-down.

Written test-first (TDD). FastAPI ``TestClient`` against the mock-mode ``db``
fixture (real Postgres), exercising the read/query surface an operator uses to
review scheduled policy runs: list executions newest-first, filter by
``policy_id`` / ``subscription_id`` / ``status`` (alone and combined), respect
``limit``, 404 on an unknown execution id, and drill into one execution's matched
resources. Thin endpoints over the M3.1 repository read helpers — no orchestration
here.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from azure_finops import models as m
from azure_finops.api.main import app
from azure_finops.storage import repository as repo
from azure_finops.storage.db import session_scope


def _make_policy(session, name: str = "exec-pol") -> int:
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
    subscription_id: str | None,
    status: str = "succeeded",
    matches: list[m.PolicyMatch] | None = None,
) -> None:
    repo.create_policy_execution(
        session,
        execution_id=execution_id,
        policy_id=policy_id,
        subscription_id=subscription_id,
    )
    if matches:
        repo.insert_policy_matches(session, execution_id, matches)
    repo.finish_policy_execution(
        session, execution_id, status=status, resources_matched=len(matches or [])
    )


# --------------------------------------------------------------------------- #
# GET /api/policy-executions — list + filters
# --------------------------------------------------------------------------- #
def test_policy_executions_empty_db_returns_empty_list(db) -> None:
    resp = TestClient(app).get("/api/policy-executions")
    assert resp.status_code == 200
    assert resp.json() == []


def test_policy_executions_filters_by_policy_id(db) -> None:
    with session_scope() as s:
        p1 = _make_policy(s, "p1")
        p2 = _make_policy(s, "p2")
        _seed_execution(s, execution_id="e1", policy_id=p1, subscription_id="sub-a")
        _seed_execution(s, execution_id="e2", policy_id=p2, subscription_id="sub-a")

    rows = TestClient(app).get(f"/api/policy-executions?policy_id={p1}").json()
    assert [r["execution_id"] for r in rows] == ["e1"]


def test_policy_executions_filters_by_subscription_id(db) -> None:
    with session_scope() as s:
        pid = _make_policy(s)
        _seed_execution(s, execution_id="e1", policy_id=pid, subscription_id="sub-a")
        _seed_execution(s, execution_id="e2", policy_id=pid, subscription_id="sub-b")

    rows = TestClient(app).get("/api/policy-executions?subscription_id=sub-b").json()
    assert [r["execution_id"] for r in rows] == ["e2"]


def test_policy_executions_filters_by_status(db) -> None:
    with session_scope() as s:
        pid = _make_policy(s)
        _seed_execution(
            s, execution_id="ok", policy_id=pid, subscription_id="s", status="succeeded"
        )
        _seed_execution(s, execution_id="bad", policy_id=pid, subscription_id="s", status="failed")

    rows = TestClient(app).get("/api/policy-executions?status=failed").json()
    assert [r["execution_id"] for r in rows] == ["bad"]


def test_policy_executions_filters_combined(db) -> None:
    with session_scope() as s:
        p1 = _make_policy(s, "p1")
        p2 = _make_policy(s, "p2")
        _seed_execution(s, execution_id="e1", policy_id=p1, subscription_id="sub-a")
        _seed_execution(s, execution_id="e2", policy_id=p1, subscription_id="sub-b")
        _seed_execution(s, execution_id="e3", policy_id=p2, subscription_id="sub-a")

    rows = (
        TestClient(app).get(f"/api/policy-executions?policy_id={p1}&subscription_id=sub-a").json()
    )
    assert [r["execution_id"] for r in rows] == ["e1"]


def test_policy_executions_respects_limit_param(db) -> None:
    with session_scope() as s:
        pid = _make_policy(s)
        for i in range(5):
            _seed_execution(s, execution_id=f"e{i}", policy_id=pid, subscription_id="s")

    rows = TestClient(app).get("/api/policy-executions?limit=2").json()
    assert len(rows) == 2


def test_policy_executions_blank_filter_is_ignored(db) -> None:
    # A blank query-string filter (an "all" dropdown) must not filter everything out.
    with session_scope() as s:
        pid = _make_policy(s)
        _seed_execution(s, execution_id="e1", policy_id=pid, subscription_id="sub-a")

    rows = TestClient(app).get("/api/policy-executions?subscription_id=&status=").json()
    assert [r["execution_id"] for r in rows] == ["e1"]


# --------------------------------------------------------------------------- #
# GET /api/policy-executions/{execution_id}
# --------------------------------------------------------------------------- #
def test_get_policy_execution_returns_404_for_unknown_id(db) -> None:
    resp = TestClient(app).get("/api/policy-executions/does-not-exist")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "execution not found"


def test_get_policy_execution_returns_execution_for_known_id(db) -> None:
    with session_scope() as s:
        pid = _make_policy(s)
        _seed_execution(
            s,
            execution_id="known",
            policy_id=pid,
            subscription_id="sub-a",
            status="succeeded",
            matches=[m.PolicyMatch(resource_id="/r/1")],
        )

    body = TestClient(app).get("/api/policy-executions/known").json()
    assert body["execution_id"] == "known"
    assert body["policy_id"] == pid
    assert body["status"] == "succeeded"
    assert body["resources_matched"] == 1


# --------------------------------------------------------------------------- #
# GET /api/policy-executions/{execution_id}/matches — drill-down
# --------------------------------------------------------------------------- #
def test_policy_execution_matches_returns_404_for_unknown_execution(db) -> None:
    resp = TestClient(app).get("/api/policy-executions/ghost/matches")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "execution not found"


def test_policy_execution_matches_returns_matched_resources(db) -> None:
    with session_scope() as s:
        pid = _make_policy(s)
        _seed_execution(
            s,
            execution_id="withmatches",
            policy_id=pid,
            subscription_id="sub-a",
            matches=[
                m.PolicyMatch(
                    resource_id="/r/1",
                    resource_type="Microsoft.Compute/virtualMachines",
                    action_taken="stop",
                ),
                m.PolicyMatch(resource_id="/r/2", resource_type="Microsoft.Compute/disks"),
            ],
        )

    rows = TestClient(app).get("/api/policy-executions/withmatches/matches").json()
    assert {r["resource_id"] for r in rows} == {"/r/1", "/r/2"}
    first = next(r for r in rows if r["resource_id"] == "/r/1")
    assert first["resource_type"] == "Microsoft.Compute/virtualMachines"
    assert first["action_taken"] == "stop"
