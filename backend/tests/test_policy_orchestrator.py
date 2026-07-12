"""Pull-mode execution orchestrator (M3.2): ``run_policies`` / ``run_all_policies``.

Written test-first (TDD). These drive the scheduled evaluation of every enabled
Cloud Custodian policy against every enabled subscription, persisting a
``PolicyExecution`` + its ``PolicyMatch`` rows per (policy, subscription) via the
M3.1 storage layer. The single mockable seam is ``custodian.engine.run_policy`` —
every test injects a fake so nothing touches live Azure or a real c7n
``PolicyCollection``. DB-backed via the ``db`` testcontainers fixture so rows
really persist, plus in-process CLI/scheduler wiring checks.

Isolation is the crux: one policy's failure is recorded as ``status="failed"`` on
its own row without aborting its siblings, and one subscription's failure does not
abort the fan-out across the others — mirroring ``run_all_subscriptions``.
"""

from __future__ import annotations

import pytest

from azure_finops.azure.context import SubscriptionContext
from azure_finops.custodian import engine
from azure_finops.storage import repository as repo
from azure_finops.storage.db import session_scope

SUB_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
SUB_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _make_policy(session, name: str, resource: str = "azure.vm") -> int:
    return repo.create_policy(
        session,
        name=name,
        resource_type=resource,
        spec={"policies": [{"name": name, "resource": resource}]},
    )["id"]


def _resource(rid: str, rtype: str = "Microsoft.Compute/virtualMachines") -> dict:
    return {"id": rid, "type": rtype}


def _fake_run_policy(by_name: dict):
    """A stand-in for ``engine.run_policy`` keyed on the spec's policy name.

    ``by_name`` maps a policy name to either a list of matched-resource dicts or an
    ``Exception`` instance to raise (to exercise per-policy failure isolation).
    """

    def _run(spec, subscription=None, dry_run=True, runner=None):
        name = (spec.get("policies") or [{}])[0].get("name")
        entry = by_name.get(name, [])
        if isinstance(entry, Exception):
            raise entry
        return {
            "policy_name": name,
            "resource_type": (spec.get("policies") or [{}])[0].get("resource"),
            "dry_run": dry_run,
            "matched": len(entry),
            "resources": entry,
        }

    return _run


# --------------------------------------------------------------------------- #
# run_policies — one subscription, every enabled policy
# --------------------------------------------------------------------------- #
def test_run_policies_persists_execution_per_policy(db, monkeypatch) -> None:
    from azure_finops.orchestrator import run_policies

    with session_scope() as s:
        _make_policy(s, "p-one")
        _make_policy(s, "p-two")
    monkeypatch.setattr(
        engine,
        "run_policy",
        _fake_run_policy({"p-one": [_resource("/r/a")], "p-two": [_resource("/r/b")]}),
    )

    result = run_policies(SubscriptionContext(subscription_id="sub-1"), mock=True)

    assert len(result["executions"]) == 2
    with session_scope() as s:
        execs = repo.list_policy_executions(s, subscription_id="sub-1")
    assert len(execs) == 2
    assert all(e["status"] == "succeeded" for e in execs)


def test_run_policies_records_resources_matched_count(db, monkeypatch) -> None:
    from azure_finops.orchestrator import run_policies

    with session_scope() as s:
        _make_policy(s, "p-count")
    monkeypatch.setattr(
        engine,
        "run_policy",
        _fake_run_policy({"p-count": [_resource("/r/1"), _resource("/r/2"), _resource("/r/3")]}),
    )

    run_policies(SubscriptionContext(subscription_id="sub-1"), mock=True)

    with session_scope() as s:
        (exec_row,) = repo.list_policy_executions(s, subscription_id="sub-1")
    assert exec_row["resources_matched"] == 3


def test_run_policies_stores_matches_for_each_matched_resource(db, monkeypatch) -> None:
    from azure_finops.orchestrator import run_policies

    with session_scope() as s:
        _make_policy(s, "p-match")
    monkeypatch.setattr(
        engine,
        "run_policy",
        _fake_run_policy({"p-match": [_resource("/r/x"), _resource("/r/y")]}),
    )

    result = run_policies(SubscriptionContext(subscription_id="sub-1"), mock=True)

    execution_id = result["executions"][0]["execution_id"]
    with session_scope() as s:
        matches = repo.list_policy_matches(s, execution_id)
    assert {mm["resource_id"] for mm in matches} == {"/r/x", "/r/y"}
    assert all(mm["resource_type"] == "Microsoft.Compute/virtualMachines" for mm in matches)


def test_run_policies_one_policy_failure_marks_failed_status_with_error(db, monkeypatch) -> None:
    from azure_finops.orchestrator import run_policies

    with session_scope() as s:
        _make_policy(s, "p-good")
        pid_bad = _make_policy(s, "p-bad")
    monkeypatch.setattr(
        engine,
        "run_policy",
        _fake_run_policy({"p-good": [_resource("/r/ok")], "p-bad": RuntimeError("kaboom")}),
    )

    run_policies(SubscriptionContext(subscription_id="sub-1"), mock=True)

    with session_scope() as s:
        (bad,) = repo.list_policy_executions(s, policy_id=pid_bad)
    assert bad["status"] == "failed"
    assert "kaboom" in bad["error"]
    assert bad["resources_matched"] == 0


def test_run_policies_one_policy_failure_does_not_abort_other_policies(db, monkeypatch) -> None:
    from azure_finops.orchestrator import run_policies

    with session_scope() as s:
        pid_good = _make_policy(s, "p-good")
        _make_policy(s, "p-bad")
    monkeypatch.setattr(
        engine,
        "run_policy",
        _fake_run_policy({"p-good": [_resource("/r/ok")], "p-bad": RuntimeError("kaboom")}),
    )

    run_policies(SubscriptionContext(subscription_id="sub-1"), mock=True)

    # The sibling still ran to success and recorded its match — not aborted.
    with session_scope() as s:
        (good,) = repo.list_policy_executions(s, policy_id=pid_good)
        matches = repo.list_policy_matches(s, good["execution_id"])
    assert good["status"] == "succeeded"
    assert good["resources_matched"] == 1
    assert [mm["resource_id"] for mm in matches] == ["/r/ok"]


def test_run_policies_records_declared_actions_on_execution(db, monkeypatch) -> None:
    from azure_finops.orchestrator import run_policies

    with session_scope() as s:
        repo.create_policy(
            s,
            name="p-stop",
            resource_type="azure.vm",
            spec={"policies": [{"name": "p-stop", "resource": "azure.vm", "actions": ["stop"]}]},
        )
    monkeypatch.setattr(engine, "run_policy", _fake_run_policy({"p-stop": [_resource("/r/a")]}))

    run_policies(SubscriptionContext(subscription_id="sub-1"), mock=True)

    with session_scope() as s:
        (exec_row,) = repo.list_policy_executions(s, subscription_id="sub-1")
    assert exec_row["actions_taken"] == ["stop"]


def test_declared_actions_extracts_string_and_typed_action_identifiers() -> None:
    from azure_finops.orchestrator import _declared_actions

    spec = {"policies": [{"actions": ["stop", {"type": "tag"}, {"no": "type"}]}]}
    # bare-string and ``{"type": ...}`` forms are kept; a typeless mapping is dropped.
    assert _declared_actions(spec) == ["stop", "tag"]


def test_declared_actions_returns_empty_without_policies_or_actions() -> None:
    from azure_finops.orchestrator import _declared_actions

    assert _declared_actions({}) == []
    assert _declared_actions({"policies": []}) == []
    assert _declared_actions({"policies": [{"name": "read-only"}]}) == []


# --------------------------------------------------------------------------- #
# run_all_policies — fan out across every enabled subscription
# --------------------------------------------------------------------------- #
def test_run_all_policies_fans_out_across_enabled_subscriptions(db, monkeypatch) -> None:
    from azure_finops.orchestrator import run_all_policies

    with session_scope() as s:
        repo.upsert_subscription(s, subscription_id=SUB_A, display_name="A")
        repo.upsert_subscription(s, subscription_id=SUB_B, display_name="B")
        _make_policy(s, "p-fan")
    monkeypatch.setattr(engine, "run_policy", _fake_run_policy({"p-fan": [_resource("/r/z")]}))

    result = run_all_policies(mock=True)

    assert result["subscriptions"] == 2
    with session_scope() as s:
        execs = repo.list_policy_executions(s)
    assert {e["subscription_id"] for e in execs} == {SUB_A, SUB_B}


def test_run_all_policies_one_subscription_failure_does_not_abort_others(db, monkeypatch) -> None:
    import azure_finops.orchestrator as orch

    with session_scope() as s:
        repo.upsert_subscription(s, subscription_id=SUB_A, display_name="A")
        repo.upsert_subscription(s, subscription_id=SUB_B, display_name="B")
        _make_policy(s, "p-fan")

    def boom(*a, **k):
        raise RuntimeError("subscription down")

    monkeypatch.setattr(orch, "run_policies", boom)

    result = orch.run_all_policies(mock=True)

    assert result["subscriptions"] == 2
    assert all("error" in r for r in result["runs"])


def test_run_all_policies_skips_disabled_subscriptions(db, monkeypatch) -> None:
    from azure_finops.orchestrator import run_all_policies

    with session_scope() as s:
        repo.upsert_subscription(s, subscription_id=SUB_A, display_name="A")
        repo.upsert_subscription(s, subscription_id=SUB_B, display_name="B", enabled=False)
        _make_policy(s, "p-fan")
    monkeypatch.setattr(engine, "run_policy", _fake_run_policy({"p-fan": [_resource("/r/z")]}))

    result = run_all_policies(mock=True)

    assert result["subscriptions"] == 1
    with session_scope() as s:
        execs = repo.list_policy_executions(s)
    assert {e["subscription_id"] for e in execs} == {SUB_A}


# --------------------------------------------------------------------------- #
# CLI + scheduler wiring
# --------------------------------------------------------------------------- #
def test_cli_run_policies_command_invokes_run_all_policies(monkeypatch) -> None:
    from typer.testing import CliRunner

    import azure_finops.cli as cli
    import azure_finops.orchestrator as orch

    called: dict = {}

    def fake_run_all_policies(mock=None):
        called["mock"] = mock
        return {"subscriptions": 0, "runs": []}

    monkeypatch.setattr(orch, "run_all_policies", fake_run_all_policies)
    result = CliRunner().invoke(cli.app, ["run-policies", "--mock"])

    assert result.exit_code == 0
    assert "policy run complete" in result.stdout
    assert called["mock"] is True


def test_scheduler_registers_independent_policy_job_interval(monkeypatch) -> None:
    import azure_finops.scheduler as sched
    from azure_finops.config import get_settings

    monkeypatch.setenv("RUN_INTERVAL_SECONDS", "60")
    monkeypatch.setenv("POLICY_RUN_INTERVAL_SECONDS", "120")
    get_settings.cache_clear()

    monkeypatch.setattr(sched, "run_all_subscriptions", lambda: None)
    monkeypatch.setattr(sched, "run_all_policies", lambda: None)

    jobs: list[dict] = []

    class _FakeScheduler:
        def __init__(self, timezone=None):
            pass

        def add_job(self, func, trigger, *, seconds=None, id=None):
            jobs.append({"id": id, "seconds": seconds})

        def start(self):
            raise KeyboardInterrupt()

    monkeypatch.setattr(sched, "BlockingScheduler", _FakeScheduler)
    sched.run_scheduler()

    by_id = {j["id"]: j["seconds"] for j in jobs}
    assert by_id["finops-run"] == 60
    assert by_id["finops-policy-run"] == 120


def test_scheduler_safe_run_policies_swallows_errors(monkeypatch) -> None:
    import azure_finops.scheduler as sched

    ran: list[str] = []
    monkeypatch.setattr(sched, "run_all_policies", lambda: ran.append("ok"))
    sched._safe_run_policies()
    assert ran == ["ok"]

    def boom():
        raise RuntimeError("x")

    monkeypatch.setattr(sched, "run_all_policies", boom)
    sched._safe_run_policies()  # must swallow the error


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
