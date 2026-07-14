"""Approval workflow for policy-driven actions (M7.2) — guardrailed remediation.

Written test-first (TDD). DB-backed (the ``db`` fixture) + FastAPI ``TestClient``.
A matched resource's action is **queued pending** and only enforced after a human
approves; the state machine is the contract under test:

    pending ──approve──▶ approved ─(guarded exec)─▶ executed / blocked / failed
            └─reject───▶ rejected                    (never executes)

Invariants (Arrange–Act–Assert):

* a queued action starts ``pending`` and runs nothing;
* approving executes it (guarded) and records ``executed``;
* rejecting sets ``rejected`` and never executes;
* deciding an **unknown** action → 404; an **already-decided** one → 409;
* guardrails still hard-block a real (non-dry-run) execution.

Every test is offline: mock mode (``FINOPS_MOCK=1``, the conftest default) short-
circuits the executor, and the live branch monkeypatches ``execute_action`` — no
Azure, no c7n, no network.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from cloudwarden.api.main import app
from cloudwarden.config import get_settings
from cloudwarden.remediation import approval, executor
from cloudwarden.storage import repository as repo
from cloudwarden.storage import schema
from cloudwarden.storage.db import session_scope

RID = "/subscriptions/s/resourceGroups/rg-app/providers/Microsoft.Compute/virtualMachines/vm-1"


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _seed_match(resource_id: str = RID, resource_type: str = "azure.vm") -> int:
    """Create a policy + execution + one PolicyMatch; return the match id."""
    with session_scope() as s:
        pid = repo.create_policy(
            s,
            name="guard-vms",
            resource_type="azure.vm",
            spec={"policies": [{"name": "guard-vms", "resource": "azure.vm", "actions": ["stop"]}]},
        )["id"]
        repo.create_policy_execution(s, execution_id="ex-1", policy_id=pid, subscription_id="sub-1")
        match = schema.PolicyMatch(
            execution_id="ex-1", resource_id=resource_id, resource_type=resource_type
        )
        s.add(match)
        s.flush()
        return match.id


def _enable_remediation(monkeypatch, *, allow: str = "rg-app") -> None:
    monkeypatch.setenv("REMEDIATION_ENABLED", "true")
    monkeypatch.setenv("ALLOWED_RESOURCE_GROUPS", allow)
    get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# state machine — queue / approve / reject
# --------------------------------------------------------------------------- #
def test_action_starts_pending(db) -> None:
    match_id = _seed_match()

    with session_scope() as s:
        queued = approval.queue_policy_action(s, match_id, "stop", actor="alice")

    assert queued["status"] == "pending"
    assert queued["policy_match_id"] == match_id
    assert queued["action_type"] == "stop"
    with session_scope() as s:  # nothing executed
        row = s.get(schema.RemediationAction, queued["action_id"])
        assert row.executed_at is None and not row.result


def test_approve_executes_action(db, monkeypatch) -> None:
    match_id = _seed_match()
    _enable_remediation(monkeypatch)
    with session_scope() as s:
        aid = approval.queue_policy_action(s, match_id, "stop", dry_run=False)["action_id"]

    with session_scope() as s:
        res = approval.approve_action(s, aid, actor="bob")

    assert res["status"] == "executed"
    with session_scope() as s:
        row = s.get(schema.RemediationAction, aid)
        assert row.status == "executed" and row.executed_at is not None
    get_settings.cache_clear()


def test_reject_blocks_execution(db) -> None:
    match_id = _seed_match()
    with session_scope() as s:
        aid = approval.queue_policy_action(s, match_id, "stop", dry_run=False)["action_id"]

    with session_scope() as s:
        res = approval.reject_action(s, aid, actor="carol")

    assert res["status"] == "rejected"
    with session_scope() as s:
        row = s.get(schema.RemediationAction, aid)
        assert row.status == "rejected" and not row.result  # never executed


def test_approve_dry_run_previews(db, monkeypatch) -> None:
    match_id = _seed_match()
    _enable_remediation(monkeypatch)
    with session_scope() as s:
        aid = approval.queue_policy_action(s, match_id, "stop", dry_run=True)["action_id"]

    with session_scope() as s:
        res = approval.approve_action(s, aid)

    assert res["status"] == "dry_run"
    get_settings.cache_clear()


def test_approve_blocked_by_guardrails(db, monkeypatch) -> None:
    match_id = _seed_match()
    _enable_remediation(monkeypatch, allow="rg-other")  # resource's rg not allow-listed
    with session_scope() as s:
        aid = approval.queue_policy_action(s, match_id, "stop", dry_run=False)["action_id"]

    with session_scope() as s:
        res = approval.approve_action(s, aid)

    assert res["status"] == "blocked"
    assert "allow-list" in (res["error"] or "")
    get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# negative / edge — unknown & already-decided
# --------------------------------------------------------------------------- #
def test_approve_unknown_action_raises_notfound(db) -> None:
    with session_scope() as s, pytest.raises(approval.NotFound):
        approval.approve_action(s, 10_000_000)


def test_approve_already_decided_raises(db) -> None:
    match_id = _seed_match()
    with session_scope() as s:
        aid = approval.queue_policy_action(s, match_id, "stop", dry_run=True)["action_id"]
    with session_scope() as s:
        approval.reject_action(s, aid)

    with session_scope() as s, pytest.raises(approval.AlreadyDecided):
        approval.approve_action(s, aid)


def test_reject_already_decided_raises(db, monkeypatch) -> None:
    match_id = _seed_match()
    _enable_remediation(monkeypatch)
    with session_scope() as s:
        aid = approval.queue_policy_action(s, match_id, "stop", dry_run=True)["action_id"]
    with session_scope() as s:
        approval.approve_action(s, aid)

    with session_scope() as s, pytest.raises(approval.AlreadyDecided):
        approval.reject_action(s, aid)
    get_settings.cache_clear()


def test_queue_unknown_match_raises_notfound(db) -> None:
    with session_scope() as s, pytest.raises(approval.NotFound):
        approval.queue_policy_action(s, 10_000_000, "stop")


def test_queue_invalid_action_raises_valueerror(db) -> None:
    match_id = _seed_match()
    with session_scope() as s, pytest.raises(ValueError):
        approval.queue_policy_action(s, match_id, {"tag": "k"})  # missing "type"


# --------------------------------------------------------------------------- #
# live (non-mock) branch — executor injected
# --------------------------------------------------------------------------- #
def test_approve_live_executes(db, monkeypatch) -> None:
    match_id = _seed_match()
    _enable_remediation(monkeypatch)
    monkeypatch.setenv("FINOPS_MOCK", "0")
    get_settings.cache_clear()
    monkeypatch.setattr("cloudwarden.auth.write_credential", lambda: object())
    monkeypatch.setattr(
        executor, "execute_action", lambda *a, **k: {"executed": True, "message": "stop completed"}
    )
    with session_scope() as s:
        aid = approval.queue_policy_action(s, match_id, "stop", dry_run=False)["action_id"]

    with session_scope() as s:
        res = approval.approve_action(s, aid)

    assert res["status"] == "executed"
    get_settings.cache_clear()


def test_approve_live_failure_records_failed(db, monkeypatch) -> None:
    match_id = _seed_match()
    _enable_remediation(monkeypatch)
    monkeypatch.setenv("FINOPS_MOCK", "0")
    get_settings.cache_clear()
    monkeypatch.setattr("cloudwarden.auth.write_credential", lambda: object())

    def boom(*a, **k):
        raise RuntimeError("azure boom")

    monkeypatch.setattr(executor, "execute_action", boom)
    with session_scope() as s:
        aid = approval.queue_policy_action(s, match_id, "stop", dry_run=False)["action_id"]

    with session_scope() as s:
        res = approval.approve_action(s, aid)

    assert res["status"] == "failed" and "boom" in (res["error"] or "")
    get_settings.cache_clear()


def test_approve_live_unsupported_action_records_failed(db, monkeypatch) -> None:
    """A structured executor error (unknown action) maps to a 'failed' action."""
    match_id = _seed_match()
    _enable_remediation(monkeypatch)
    monkeypatch.setenv("FINOPS_MOCK", "0")
    get_settings.cache_clear()
    monkeypatch.setattr("cloudwarden.auth.write_credential", lambda: object())
    with session_scope() as s:
        aid = approval.queue_policy_action(s, match_id, "frobnicate", dry_run=False)["action_id"]

    with session_scope() as s:
        res = approval.approve_action(s, aid)

    assert res["status"] == "failed"
    assert "unsupported action type" in (res["error"] or "")
    get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# HTTP endpoints — approve/reject/queue
# --------------------------------------------------------------------------- #
def test_endpoint_queue_then_approve_executes(db, client, monkeypatch) -> None:
    match_id = _seed_match()
    _enable_remediation(monkeypatch)

    queued = client.post(
        f"/api/policy-matches/{match_id}/actions",
        json={"action": "stop", "actor": "ui", "dry_run": False},
    )
    assert queued.status_code == 200
    aid = queued.json()["action_id"]
    assert queued.json()["status"] == "pending"

    approved = client.post(f"/api/remediation/{aid}/approve")
    assert approved.status_code == 200 and approved.json()["status"] == "executed"
    get_settings.cache_clear()


def test_endpoint_reject_blocks(db, client) -> None:
    match_id = _seed_match()
    aid = client.post(f"/api/policy-matches/{match_id}/actions", json={"action": "stop"}).json()[
        "action_id"
    ]

    rejected = client.post(f"/api/remediation/{aid}/reject")

    assert rejected.status_code == 200 and rejected.json()["status"] == "rejected"


def test_endpoint_approve_unknown_action_404(db, client) -> None:
    assert client.post("/api/remediation/10000000/approve").status_code == 404


def test_endpoint_approve_already_decided_409(db, client) -> None:
    match_id = _seed_match()
    aid = client.post(f"/api/policy-matches/{match_id}/actions", json={"action": "stop"}).json()[
        "action_id"
    ]
    client.post(f"/api/remediation/{aid}/reject")  # decide it once

    assert client.post(f"/api/remediation/{aid}/approve").status_code == 409


def test_endpoint_queue_unknown_match_404(db, client) -> None:
    resp = client.post("/api/policy-matches/10000000/actions", json={"action": "stop"})
    assert resp.status_code == 404


def test_endpoint_queue_invalid_action_400(db, client) -> None:
    match_id = _seed_match()
    resp = client.post(f"/api/policy-matches/{match_id}/actions", json={"action": {"tag": "k"}})
    assert resp.status_code == 400


def test_endpoint_reject_unknown_action_404(db, client) -> None:
    assert client.post("/api/remediation/10000000/reject").status_code == 404


def test_endpoint_reject_already_decided_409(db, client) -> None:
    match_id = _seed_match()
    aid = client.post(f"/api/policy-matches/{match_id}/actions", json={"action": "stop"}).json()[
        "action_id"
    ]
    client.post(f"/api/remediation/{aid}/reject")  # decide it once

    assert client.post(f"/api/remediation/{aid}/reject").status_code == 409


def test_approve_live_dry_run_appends_guard_note(db, monkeypatch) -> None:
    """A dry-run live execution previews even when guardrails would block, noting it."""
    match_id = _seed_match()
    _enable_remediation(monkeypatch, allow="rg-other")  # would block a real exec
    monkeypatch.setenv("FINOPS_MOCK", "0")
    get_settings.cache_clear()
    monkeypatch.setattr("cloudwarden.auth.write_credential", lambda: object())
    monkeypatch.setattr(
        executor,
        "execute_action",
        lambda *a, **k: {"executed": False, "dry_run": True, "message": "[dry-run] would stop"},
    )
    with session_scope() as s:
        aid = approval.queue_policy_action(s, match_id, "stop", dry_run=True)["action_id"]

    with session_scope() as s:
        res = approval.approve_action(s, aid)

    assert res["status"] == "dry_run"
    assert "guardrails would block" in (res["message"] or "")
    get_settings.cache_clear()
