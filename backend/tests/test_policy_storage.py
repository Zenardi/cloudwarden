"""Execution-results storage (M3.1): ``policy_executions`` + ``policy_matches``.

Written test-first (TDD). DB-backed (the ``db`` testcontainers fixture, so rows
really persist) exercising the storage foundation the M3.2 orchestrator will build
on: the two ORM tables are auto-created by ``init_db()``, an execution round-trips
its lifecycle (``running`` → ``succeeded``/``failed``) via
``create_policy_execution`` / ``finish_policy_execution``, per-resource matches
persist under the execution FK, and the read helpers filter / limit / order and
return ``None`` for an unknown id. Pure storage — no orchestration or API here.

Note: ``PolicyExecution.policy_id`` is a ``BigInteger`` FK to the real
``policies.id`` PK (the issue's ``policies.policy_id`` predates the M2 schema, whose
policy PK is the autoincrement ``id``), so every test seeds a real policy first.
"""

from __future__ import annotations

from sqlalchemy import inspect

from cloudwarden import models as m
from cloudwarden.storage import repository as repo
from cloudwarden.storage.db import session_scope


def _make_policy(session, name: str = "exec-pol") -> int:
    return repo.create_policy(
        session,
        name=name,
        resource_type="azure.vm",
        spec={"policies": [{"name": name, "resource": "azure.vm"}]},
    )["id"]


# --------------------------------------------------------------------------- #
# Schema creation
# --------------------------------------------------------------------------- #
def test_init_db_creates_policy_execution_tables(db) -> None:
    inspector = inspect(db)
    assert inspector.has_table("policy_executions")
    assert inspector.has_table("policy_matches")


# --------------------------------------------------------------------------- #
# Execution lifecycle: create → finish
# --------------------------------------------------------------------------- #
def test_create_policy_execution_defaults_to_running_status(db) -> None:
    with session_scope() as s:
        pid = _make_policy(s)
    with session_scope() as s:
        repo.create_policy_execution(
            s, execution_id="exec-1", policy_id=pid, subscription_id="sub-1"
        )

    with session_scope() as s:
        rec = repo.get_policy_execution(s, "exec-1")
    assert rec is not None
    assert rec["status"] == "running"
    assert rec["policy_id"] == pid
    assert rec["subscription_id"] == "sub-1"
    assert rec["started_at"] is not None
    assert rec["finished_at"] is None
    assert rec["resources_matched"] == 0
    assert rec["actions_taken"] == []
    assert rec["error"] is None


def test_finish_policy_execution_sets_finished_at_and_status(db) -> None:
    with session_scope() as s:
        pid = _make_policy(s)
        repo.create_policy_execution(
            s, execution_id="exec-2", policy_id=pid, subscription_id="sub-1"
        )

    with session_scope() as s:
        repo.finish_policy_execution(
            s, "exec-2", status="succeeded", resources_matched=3, actions_taken=[{"type": "stop"}]
        )

    with session_scope() as s:
        rec = repo.get_policy_execution(s, "exec-2")
    assert rec["status"] == "succeeded"
    assert rec["finished_at"] is not None
    assert rec["resources_matched"] == 3
    assert rec["actions_taken"] == [{"type": "stop"}]
    assert rec["error"] is None


def test_finish_policy_execution_failed_records_error(db) -> None:
    with session_scope() as s:
        pid = _make_policy(s)
        repo.create_policy_execution(s, execution_id="exec-f", policy_id=pid, subscription_id=None)

    with session_scope() as s:
        repo.finish_policy_execution(s, "exec-f", status="failed", error="boom")

    with session_scope() as s:
        rec = repo.get_policy_execution(s, "exec-f")
    assert rec["status"] == "failed"
    assert rec["error"] == "boom"
    assert rec["finished_at"] is not None
    assert rec["resources_matched"] == 0
    assert rec["actions_taken"] == []


def test_finish_policy_execution_unknown_id_is_noop(db) -> None:
    # Must return quietly (not raise) for an unknown execution id.
    with session_scope() as s:
        repo.finish_policy_execution(s, "ghost", status="succeeded")
    with session_scope() as s:
        assert repo.get_policy_execution(s, "ghost") is None


# --------------------------------------------------------------------------- #
# Matches
# --------------------------------------------------------------------------- #
def test_insert_policy_matches_persists_rows_with_execution_fk(db) -> None:
    with session_scope() as s:
        pid = _make_policy(s)
        repo.create_policy_execution(
            s, execution_id="exec-3", policy_id=pid, subscription_id="sub-1"
        )

    with session_scope() as s:
        count = repo.insert_policy_matches(
            s,
            "exec-3",
            [
                m.PolicyMatch(
                    resource_id="/r/1",
                    resource_type="azure.vm",
                    action_taken="stop",
                    action_result={"ok": True},
                ),
                m.PolicyMatch(resource_id="/r/2", resource_type="azure.vm"),
            ],
        )
    assert count == 2

    with session_scope() as s:
        matches = repo.list_policy_matches(s, "exec-3")
    assert {mm["resource_id"] for mm in matches} == {"/r/1", "/r/2"}
    assert all(mm["execution_id"] == "exec-3" for mm in matches)
    first = next(mm for mm in matches if mm["resource_id"] == "/r/1")
    assert first["action_taken"] == "stop"
    assert first["action_result"] == {"ok": True}
    assert first["matched_at"] is not None


def test_insert_policy_matches_empty_list_returns_zero(db) -> None:
    with session_scope() as s:
        assert repo.insert_policy_matches(s, "exec-empty", []) == 0


def test_list_policy_matches_ordered_newest_first(db) -> None:
    with session_scope() as s:
        pid = _make_policy(s)
        repo.create_policy_execution(s, execution_id="exec-o", policy_id=pid, subscription_id="s1")
    # Separate transactions → distinct matched_at timestamps (Postgres now() is the
    # transaction clock), so ordering genuinely reflects insertion time.
    with session_scope() as s:
        repo.insert_policy_matches(s, "exec-o", [m.PolicyMatch(resource_id="/r/old")])
    with session_scope() as s:
        repo.insert_policy_matches(s, "exec-o", [m.PolicyMatch(resource_id="/r/new")])

    with session_scope() as s:
        matches = repo.list_policy_matches(s, "exec-o")
    assert [mm["resource_id"] for mm in matches] == ["/r/new", "/r/old"]


# --------------------------------------------------------------------------- #
# Listing: filters / limit / not-found
# --------------------------------------------------------------------------- #
def test_list_policy_executions_filters_by_policy_id(db) -> None:
    with session_scope() as s:
        p1 = _make_policy(s, "p1")
        p2 = _make_policy(s, "p2")
        repo.create_policy_execution(s, execution_id="e1", policy_id=p1, subscription_id="s1")
        repo.create_policy_execution(s, execution_id="e2", policy_id=p2, subscription_id="s1")

    with session_scope() as s:
        recs = repo.list_policy_executions(s, policy_id=p1)
    assert [r["execution_id"] for r in recs] == ["e1"]


def test_list_policy_executions_filters_by_subscription_id(db) -> None:
    with session_scope() as s:
        pid = _make_policy(s)
        repo.create_policy_execution(s, execution_id="e1", policy_id=pid, subscription_id="subA")
        repo.create_policy_execution(s, execution_id="e2", policy_id=pid, subscription_id="subB")

    with session_scope() as s:
        recs = repo.list_policy_executions(s, subscription_id="subB")
    assert [r["execution_id"] for r in recs] == ["e2"]


def test_list_policy_executions_filters_by_status(db) -> None:
    with session_scope() as s:
        pid = _make_policy(s)
        repo.create_policy_execution(s, execution_id="e1", policy_id=pid, subscription_id="s1")
        repo.create_policy_execution(s, execution_id="e2", policy_id=pid, subscription_id="s1")
        repo.finish_policy_execution(s, "e2", status="succeeded")

    with session_scope() as s:
        running = repo.list_policy_executions(s, status="running")
        succeeded = repo.list_policy_executions(s, status="succeeded")
    assert [r["execution_id"] for r in running] == ["e1"]
    assert [r["execution_id"] for r in succeeded] == ["e2"]


def test_list_policy_executions_filters_combined(db) -> None:
    with session_scope() as s:
        p1 = _make_policy(s, "p1")
        p2 = _make_policy(s, "p2")
        repo.create_policy_execution(s, execution_id="e1", policy_id=p1, subscription_id="subA")
        repo.create_policy_execution(s, execution_id="e2", policy_id=p1, subscription_id="subB")
        repo.create_policy_execution(s, execution_id="e3", policy_id=p2, subscription_id="subA")

    with session_scope() as s:
        recs = repo.list_policy_executions(s, policy_id=p1, subscription_id="subA")
    assert [r["execution_id"] for r in recs] == ["e1"]


def test_list_policy_executions_respects_limit(db) -> None:
    with session_scope() as s:
        pid = _make_policy(s)
        for i in range(5):
            repo.create_policy_execution(
                s, execution_id=f"e{i}", policy_id=pid, subscription_id="s1"
            )

    with session_scope() as s:
        recs = repo.list_policy_executions(s, limit=2)
    assert len(recs) == 2


def test_get_policy_execution_returns_none_for_unknown_id(db) -> None:
    with session_scope() as s:
        assert repo.get_policy_execution(s, "does-not-exist") is None
