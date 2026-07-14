"""Unified remediation audit trail (M7.4) — policy actions in `remediation_actions`.

Written test-first (TDD). DB-backed (the ``db`` fixture) + FastAPI ``TestClient``.
Every policy-driven action attempt — dry-run or live — is recorded as a
``remediation_actions`` row carrying its **source** (``policy``/``binding``/
``recommendation``) and originating **policy_id**, and surfaced through
``/api/remediation`` (filterable by source) so the Remediation page can show
policy-sourced actions alongside recommendation-sourced ones.

Invariants (Arrange–Act–Assert), each test one reason to fail:

* queuing a policy action audits a row with ``source="policy"`` and its policy id;
* an execution triggered by a binding tags its actions ``source="binding"``;
* a recommendation action defaults to ``source="recommendation"``;
* a dry-run attempt is audited (``status="dry_run"``, ``dry_run=True``);
* the list surfaces source + policy_id + the resource id; and filters by source;
* an empty audit trail is a clean empty list.

Every test is offline: mock mode (the conftest default) short-circuits the
executor — no Azure, no c7n, no network.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from cloudwarden.api.main import app
from cloudwarden.config import get_settings
from cloudwarden.remediation import approval
from cloudwarden.storage import repository as repo
from cloudwarden.storage import schema
from cloudwarden.storage.db import session_scope

RID = "/subscriptions/s/resourceGroups/rg-app/providers/Microsoft.Compute/virtualMachines/vm-1"


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _seed_match(
    *, resource_id: str = RID, resource_type: str = "azure.vm", binding_id: int | None = None
) -> tuple[int, int]:
    """Create a policy + execution + one PolicyMatch; return ``(match_id, policy_id)``."""
    with session_scope() as s:
        pid = repo.create_policy(
            s,
            name="guard-vms",
            resource_type="azure.vm",
            spec={"policies": [{"name": "guard-vms", "resource": "azure.vm", "actions": ["stop"]}]},
        )["id"]
        repo.create_policy_execution(
            s, execution_id="ex-1", policy_id=pid, subscription_id="sub-1", binding_id=binding_id
        )
        match = schema.PolicyMatch(
            execution_id="ex-1", resource_id=resource_id, resource_type=resource_type
        )
        s.add(match)
        s.flush()
        return match.id, pid


def _create_binding() -> int:
    """A minimal enabled binding (collection × account group) to trigger executions."""
    with session_scope() as s:
        cid = repo.create_collection(s, name="c1")["id"]
        gid = repo.create_account_group(s, name="g1")["id"]
        b = repo.create_binding(s, collection_id=cid, account_group_id=gid)
        return b["id"]


# --------------------------------------------------------------------------- #
# audit write — source + policy_id
# --------------------------------------------------------------------------- #
def test_policy_action_audited_with_source(db) -> None:
    match_id, pid = _seed_match()

    with session_scope() as s:
        queued = approval.queue_policy_action(s, match_id, "stop", actor="alice")

    assert queued["source"] == "policy"
    assert queued["policy_id"] == pid
    with session_scope() as s:
        row = s.get(schema.RemediationAction, queued["action_id"])
        assert row.source == "policy" and row.policy_id == pid


def test_binding_sourced_action_marked_binding(db) -> None:
    bid = _create_binding()
    match_id, _ = _seed_match(binding_id=bid)

    with session_scope() as s:
        queued = approval.queue_policy_action(s, match_id, "stop")

    assert queued["source"] == "binding"


def test_recommendation_action_defaults_to_recommendation_source(db) -> None:
    """A row created without an explicit source (the recommendation path) defaults."""
    with session_scope() as s:
        row = schema.RemediationAction(action_type="resize", status="dry_run", dry_run=True)
        s.add(row)
        s.flush()
        assert row.source == "recommendation" and row.policy_id is None


def test_dry_run_attempt_audited(db, monkeypatch) -> None:
    """A dry-run approval previews and is still recorded (status dry_run)."""
    match_id, _ = _seed_match()
    monkeypatch.setenv("REMEDIATION_ENABLED", "true")
    monkeypatch.setenv("ALLOWED_RESOURCE_GROUPS", "rg-app")
    get_settings.cache_clear()
    with session_scope() as s:
        aid = approval.queue_policy_action(s, match_id, "stop", dry_run=True)["action_id"]

    with session_scope() as s:
        res = approval.approve_action(s, aid)

    assert res["status"] == "dry_run"
    with session_scope() as s:
        rows = repo.list_remediation_actions(s)
        audited = next(r for r in rows if r["id"] == aid)
        assert audited["status"] == "dry_run" and audited["dry_run"] is True
    get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# audit list — source, policy_id, resource_id, filter
# --------------------------------------------------------------------------- #
def test_remediation_list_includes_policy_source(db) -> None:
    match_id, pid = _seed_match()
    with session_scope() as s:
        aid = approval.queue_policy_action(s, match_id, "stop")["action_id"]

    with session_scope() as s:
        rows = repo.list_remediation_actions(s)

    row = next(r for r in rows if r["id"] == aid)
    assert row["source"] == "policy"
    assert row["policy_id"] == pid
    assert row["resource_id"] == RID  # surfaced from params even without a recommendation


def test_remediation_filter_by_source(db) -> None:
    match_id, _ = _seed_match()
    with session_scope() as s:
        policy_aid = approval.queue_policy_action(s, match_id, "stop")["action_id"]
        rec_row = schema.RemediationAction(
            action_type="resize", status="dry_run", dry_run=True
        )  # source defaults to "recommendation"
        s.add(rec_row)
        s.flush()
        rec_aid = rec_row.id

    with session_scope() as s:
        policy_only = repo.list_remediation_actions(s, source="policy")
        rec_only = repo.list_remediation_actions(s, source="recommendation")

    assert [r["id"] for r in policy_only] == [policy_aid]
    assert [r["id"] for r in rec_only] == [rec_aid]


# --------------------------------------------------------------------------- #
# API surface — /api/remediation source + filter + empty
# --------------------------------------------------------------------------- #
def test_api_remediation_includes_source(db, client) -> None:
    match_id, pid = _seed_match()
    with session_scope() as s:
        approval.queue_policy_action(s, match_id, "stop")

    body = client.get("/api/remediation").json()

    assert len(body) == 1
    assert body[0]["source"] == "policy" and body[0]["policy_id"] == pid


def test_api_remediation_filter_by_source(db, client) -> None:
    match_id, _ = _seed_match()
    with session_scope() as s:
        approval.queue_policy_action(s, match_id, "stop")
        s.add(schema.RemediationAction(action_type="resize", status="dry_run", dry_run=True))

    policy = client.get("/api/remediation?source=policy").json()
    recs = client.get("/api/remediation?source=recommendation").json()

    assert len(policy) == 1 and policy[0]["source"] == "policy"
    assert len(recs) == 1 and recs[0]["source"] == "recommendation"


def test_remediation_empty_state(db, client) -> None:
    assert client.get("/api/remediation").json() == []
    assert client.get("/api/remediation?source=policy").json() == []
