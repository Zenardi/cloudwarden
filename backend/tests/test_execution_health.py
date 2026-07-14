"""Policy execution health (M9.2): ``v_execution_health`` (+ by-binding) + API.

Written test-first (TDD). DB-backed (the ``db`` testcontainers fixture) against
seeded ``PolicyExecution`` rows. Execution health is the governance *engine's* own
health — per policy and per binding: how many runs succeeded/failed, the success
rate, the average wall-clock duration, and the last run. A policy/binding with no
executions is absent (empty state = empty lists, never an error). ``GET
/api/governance/execution-health`` is a thin read over the same helper.

Executions are seeded with explicit ``started_at``/``finished_at`` so durations are
deterministic (creating + finishing back-to-back would otherwise be ~0s).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from cloudwarden.api.main import app
from cloudwarden.storage import repository as repo
from cloudwarden.storage import schema
from cloudwarden.storage.db import session_scope

_BASE = datetime(2026, 1, 1, tzinfo=UTC)


def _make_policy(session, name: str) -> int:
    return repo.create_policy(
        session,
        name=name,
        resource_type="azure.vm",
        spec={"policies": [{"name": name, "resource": "azure.vm"}]},
    )["id"]


def _seed(
    session,
    *,
    eid: str,
    pid: int,
    status: str = "succeeded",
    binding_id: int | None = None,
    subscription_id: str = "sub-a",
    start_offset: int = 0,
    duration: int | None = None,
) -> None:
    """Insert a fully-formed execution. ``start_offset`` (minutes) orders runs in
    time; ``duration`` (seconds) sets ``finished_at`` — ``None`` leaves it running."""
    started = _BASE + timedelta(minutes=start_offset)
    finished = None if duration is None else started + timedelta(seconds=duration)
    session.add(
        schema.PolicyExecution(
            execution_id=eid,
            policy_id=pid,
            subscription_id=subscription_id,
            binding_id=binding_id,
            status=status,
            started_at=started,
            finished_at=finished,
            resources_matched=0,
        )
    )
    session.flush()


# --------------------------------------------------------------------------- #
# Repo helper
# --------------------------------------------------------------------------- #
def test_execution_health_empty(db) -> None:
    with session_scope() as s:
        health = repo.execution_health(s)
    assert health == {"by_policy": [], "by_binding": [], "by_provider": []}


def test_execution_health_success_rate(db) -> None:
    with session_scope() as s:
        pid = _make_policy(s, "p1")
        _seed(s, eid="e1", pid=pid, status="succeeded", start_offset=0, duration=5)
        _seed(s, eid="e2", pid=pid, status="succeeded", start_offset=1, duration=5)
        _seed(s, eid="e3", pid=pid, status="failed", start_offset=2, duration=5)

    with session_scope() as s:
        (row,) = repo.execution_health(s)["by_policy"]

    assert row["policy_name"] == "p1"
    assert row["total_executions"] == 3
    assert row["succeeded"] == 2
    assert row["failed"] == 1
    assert row["success_rate"] == 0.6667


def test_execution_health_avg_duration(db) -> None:
    with session_scope() as s:
        pid = _make_policy(s, "p1")
        _seed(s, eid="e1", pid=pid, status="succeeded", start_offset=0, duration=10)
        _seed(s, eid="e2", pid=pid, status="succeeded", start_offset=1, duration=30)

    with session_scope() as s:
        (row,) = repo.execution_health(s)["by_policy"]

    assert row["avg_duration_seconds"] == 20.0


def test_execution_health_counts_failures(db) -> None:
    with session_scope() as s:
        pid = _make_policy(s, "p1")
        _seed(s, eid="e1", pid=pid, status="succeeded", start_offset=0, duration=5)
        _seed(s, eid="e2", pid=pid, status="failed", start_offset=1, duration=5)
        _seed(s, eid="e3", pid=pid, status="failed", start_offset=2, duration=5)
        _seed(s, eid="e4", pid=pid, status="failed", start_offset=3, duration=5)

    with session_scope() as s:
        (row,) = repo.execution_health(s)["by_policy"]

    assert row["failed"] == 3
    assert row["succeeded"] == 1
    assert row["success_rate"] == 0.25
    assert row["last_status"] == "failed"


def test_execution_health_per_binding(db) -> None:
    with session_scope() as s:
        pid = _make_policy(s, "p1")
        cid = repo.create_collection(s, name="c1")["id"]
        gid = repo.create_account_group(s, name="g1")["id"]
        bid = repo.create_binding(s, collection_id=cid, account_group_id=gid)["id"]
        _seed(s, eid="e1", pid=pid, status="succeeded", binding_id=bid, start_offset=0, duration=5)
        _seed(s, eid="e2", pid=pid, status="failed", binding_id=bid, start_offset=1, duration=5)
        # A pull-mode run (no binding) must NOT appear in by_binding.
        _seed(s, eid="e3", pid=pid, status="succeeded", binding_id=None, start_offset=2, duration=5)

    with session_scope() as s:
        health = repo.execution_health(s)

    by_binding = {r["binding_id"]: r for r in health["by_binding"]}
    assert list(by_binding) == [bid]
    assert by_binding[bid]["total_executions"] == 2
    assert by_binding[bid]["succeeded"] == 1
    assert by_binding[bid]["failed"] == 1
    assert by_binding[bid]["success_rate"] == 0.5
    # by_policy still counts all three (binding + pull).
    (prow,) = health["by_policy"]
    assert prow["total_executions"] == 3


# --------------------------------------------------------------------------- #
# API endpoint
# --------------------------------------------------------------------------- #
def test_execution_health_api_empty(db) -> None:
    resp = TestClient(app).get("/api/governance/execution-health")
    assert resp.status_code == 200
    assert resp.json() == {"by_policy": [], "by_binding": [], "by_provider": []}


def test_execution_health_api_returns_health(db) -> None:
    with session_scope() as s:
        pid = _make_policy(s, "p1")
        _seed(s, eid="e1", pid=pid, status="succeeded", start_offset=0, duration=12)

    body = TestClient(app).get("/api/governance/execution-health").json()
    assert body["by_policy"][0]["policy_name"] == "p1"
    assert body["by_policy"][0]["avg_duration_seconds"] == 12.0
    assert body["by_policy"][0]["success_rate"] == 1.0
