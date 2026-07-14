"""Binding execution engine (M5.3): run a binding's policies across its accounts.

Written test-first (TDD). DB-backed (the ``db`` fixture) with an **injected**
``FakeCustodianRunner`` so no c7n/Azure call is ever made. Invariants under test
(Arrange–Act–Assert):

* running a binding produces **one PolicyExecution per policy × per subscription**;
* every execution is **tagged with the originating ``binding_id``**;
* a **disabled** binding does not run (returns a skipped result);
* the binding's **``dry_run``** is honoured (passed through to every policy run);
* an **unknown** binding is ``None`` at the engine / ``404`` at the API.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from cloudwarden.api.main import app, get_custodian_runner
from cloudwarden.custodian.bindings import run_binding
from cloudwarden.storage import repository as repo
from cloudwarden.storage.db import session_scope

_SPEC = {"policies": [{"name": "stopped-vms", "resource": "azure.vm", "actions": ["stop"]}]}


class FakeCustodianRunner:
    """Records ``run`` args (subscription, dry_run, policy) and returns 1 match. No c7n."""

    def __init__(self) -> None:
        self.run_calls: list[dict] = []

    def validate(self, spec: dict) -> dict:
        return {"valid": True, "errors": []}

    def run(self, spec: dict, subscription_id: str, credential, dry_run: bool) -> dict:
        self.run_calls.append(
            {
                "subscription_id": subscription_id,
                "dry_run": dry_run,
                "policy": (spec.get("policies") or [{}])[0].get("name"),
            }
        )
        return {
            "resources": [{"id": f"/subscriptions/{subscription_id}/vm-1", "type": "azure.vm"}],
            "matched": 1,
            "dry_run": dry_run,
        }

    def schema(self, resource_type: str | None = None) -> dict:
        return {"resource_types": []}


def _seed_binding(
    *,
    name: str = "b",
    n_policies: int = 2,
    n_subs: int = 2,
    enabled: bool = True,
    dry_run: bool = True,
    schedule: str | None = None,
) -> int:
    """Seed a collection (n policies), an account group (n subscriptions) and a binding."""
    with session_scope() as s:
        cid = repo.create_collection(s, name=f"col-{name}")["id"]
        for i in range(n_policies):
            pid = repo.create_policy(
                s, name=f"pol-{name}-{i}", resource_type="azure.vm", spec=_SPEC
            )["id"]
            repo.add_policy_to_collection(s, cid, pid)
        gid = repo.create_account_group(s, name=f"grp-{name}")["id"]
        for i in range(n_subs):
            sid = f"sub-{name}-{i}"
            repo.upsert_subscription(s, subscription_id=sid, display_name=f"S-{name}-{i}")
            repo.add_subscription_to_group(s, gid, sid)
        return repo.create_binding(
            s,
            collection_id=cid,
            account_group_id=gid,
            schedule=schedule,
            dry_run=dry_run,
            enabled=enabled,
        )["id"]


@pytest.fixture
def client():
    runner = FakeCustodianRunner()
    app.dependency_overrides[get_custodian_runner] = lambda: runner
    yield TestClient(app)
    app.dependency_overrides.clear()


# --------------------------------------------------------------------------- #
# Engine — run_binding
# --------------------------------------------------------------------------- #
def test_run_binding_executes_policy_per_subscription(db) -> None:
    bid = _seed_binding(n_policies=2, n_subs=3)
    runner = FakeCustodianRunner()
    result = run_binding(bid, runner=runner, mock=True)
    assert result["status"] == "completed"
    assert len(result["executions"]) == 6  # 2 policies × 3 subscriptions
    assert len(runner.run_calls) == 6
    with session_scope() as s:
        n = repo._rows(
            s, "SELECT count(*) AS n FROM policy_executions WHERE binding_id = :b", b=bid
        )[0]["n"]
    assert n == 6


def test_run_binding_tags_execution_with_binding_id(db) -> None:
    bid = _seed_binding(n_policies=1, n_subs=2)
    run_binding(bid, runner=FakeCustodianRunner())
    with session_scope() as s:
        rows = repo._rows(s, "SELECT DISTINCT binding_id FROM policy_executions")
    assert [r["binding_id"] for r in rows] == [bid]


def test_run_binding_disabled_is_skipped(db) -> None:
    bid = _seed_binding(enabled=False)
    runner = FakeCustodianRunner()
    result = run_binding(bid, runner=runner)
    assert result["status"] == "skipped"
    assert result["executions"] == []
    assert runner.run_calls == []
    with session_scope() as s:
        assert repo._rows(s, "SELECT count(*) AS n FROM policy_executions")[0]["n"] == 0


def test_run_binding_honours_dry_run_true(db) -> None:
    bid = _seed_binding(dry_run=True)
    runner = FakeCustodianRunner()
    run_binding(bid, runner=runner)
    assert runner.run_calls and all(c["dry_run"] is True for c in runner.run_calls)


def test_run_binding_honours_dry_run_false(db) -> None:
    bid = _seed_binding(dry_run=False)
    runner = FakeCustodianRunner()
    run_binding(bid, runner=runner)
    assert runner.run_calls and all(c["dry_run"] is False for c in runner.run_calls)


def test_run_unknown_binding_returns_none(db) -> None:
    assert run_binding(999999, runner=FakeCustodianRunner()) is None


class _RaisingRunner(FakeCustodianRunner):
    def run(self, spec: dict, subscription_id: str, credential, dry_run: bool) -> dict:
        raise RuntimeError("boom")


def test_run_binding_records_policy_failure_in_isolation(db) -> None:
    bid = _seed_binding(n_policies=1, n_subs=1)
    result = run_binding(bid, runner=_RaisingRunner())
    assert result["status"] == "completed"  # the sweep completes despite the failure
    assert result["executions"][0]["status"] == "failed"
    assert "boom" in result["executions"][0]["error"]
    with session_scope() as s:
        row = repo._rows(
            s, "SELECT status, error FROM policy_executions WHERE binding_id = :b", b=bid
        )[0]
    assert row["status"] == "failed" and "boom" in row["error"]


def test_run_enabled_bindings_runs_only_enabled(db) -> None:
    from cloudwarden.custodian.bindings import run_enabled_bindings

    _seed_binding(name="on", n_policies=1, n_subs=1, enabled=True)
    _seed_binding(name="off", n_policies=1, n_subs=1, enabled=False)
    summary = run_enabled_bindings(runner=FakeCustodianRunner())
    assert summary["bindings"] == 1
    assert summary["runs"][0]["status"] == "completed"


# --------------------------------------------------------------------------- #
# API — POST /api/bindings/{id}/run
# --------------------------------------------------------------------------- #
def test_api_run_binding(db, client) -> None:
    bid = _seed_binding(n_policies=1, n_subs=2)
    resp = client.post(f"/api/bindings/{bid}/run")
    assert resp.status_code == 200
    body = resp.json()
    assert body["binding_id"] == bid
    assert len(body["executions"]) == 2


def test_api_run_unknown_binding_404(db, client) -> None:
    assert client.post("/api/bindings/999999/run").status_code == 404


# --------------------------------------------------------------------------- #
# Scheduler — wire enabled bindings by cron
# --------------------------------------------------------------------------- #
def test_scheduler_wires_enabled_bindings_by_cron(db) -> None:
    import cloudwarden.scheduler as sched

    good = _seed_binding(name="good", schedule="0 2 * * *", enabled=True)
    _seed_binding(name="off", schedule="0 3 * * *", enabled=False)  # disabled → not scheduled
    _seed_binding(name="nosched", schedule=None, enabled=True)  # no cron → not scheduled
    bad = _seed_binding(name="bad", schedule="not-a-cron", enabled=True)  # invalid cron → skipped

    class FakeScheduler:
        def __init__(self) -> None:
            self.job_ids: list[str] = []

        def add_job(self, func, trigger=None, args=None, id=None, **kwargs) -> None:
            self.job_ids.append(id)

    fs = FakeScheduler()
    scheduled = sched._schedule_bindings(fs)
    assert scheduled == 1
    assert fs.job_ids == [f"finops-binding-{good}"]
    assert all(str(bad) not in jid for jid in fs.job_ids)


def _boom(*args, **kwargs):
    raise RuntimeError("scheduled boom")


def test_safe_run_binding_runs_then_swallows_errors(db, monkeypatch) -> None:
    import cloudwarden.scheduler as sched

    bid = _seed_binding(name="safe", n_policies=1, n_subs=1)
    sched._safe_run_binding(bid)  # success path (default runner, mock mode) — must not raise
    with session_scope() as s:
        assert repo._rows(s, "SELECT count(*) AS n FROM policy_executions")[0]["n"] == 1

    monkeypatch.setattr("cloudwarden.custodian.bindings.run_binding", _boom)
    sched._safe_run_binding(bid)  # error path — must be swallowed, not raised
