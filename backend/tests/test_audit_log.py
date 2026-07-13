"""Audit log (M11.4): append-only record of every mutating governance action.

Written test-first (TDD). Two layers are exercised:

* **Repository + helper** — ``insert_audit_log`` / ``list_audit_logs`` and the
  ``authz.audit.record`` helper (actor, action, target, before/after), in isolation.
* **API end-to-end** — creating/updating/deleting a policy writes an entry carrying the
  actor and before/after; **reads are never audited**; ``GET /api/audit`` returns entries
  newest-first with actor/target filters; and the log is **append-only** (no mutation
  routes).

The ``db`` fixture makes the rows really persist. The actor is the resolved principal —
supplied here via the ``X-Principal`` header (RBAC off, so it passes straight through).
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from azure_finops.authz import audit
from azure_finops.storage import repository as repo
from azure_finops.storage.db import session_scope

VALID_SPEC = {"policies": [{"name": "audit-p", "resource": "azure.vm"}]}


def _client() -> TestClient:
    from azure_finops.api.main import app

    return TestClient(app)


def _create(client: TestClient, name: str, *, actor: str | None = "alice", **over) -> dict:
    body = {"name": name, "resource_type": "azure.vm", "spec": VALID_SPEC, **over}
    headers = {"X-Principal": actor} if actor else {}
    resp = client.post("/api/policies", json=body, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _audit_rows(**params) -> list[dict]:
    return _client().get("/api/audit", params=params).json()


# --------------------------------------------------------------------------- #
# Repository + helper (isolated)
# --------------------------------------------------------------------------- #
def test_insert_and_list_audit(db) -> None:
    with session_scope() as s:
        audit.record(
            s,
            actor="bob",
            action="policy.create",
            target_type="policy",
            target_id="7",
            after={"name": "p"},
        )
        rows = repo.list_audit_logs(s)

    assert len(rows) == 1
    row = rows[0]
    assert row["actor"] == "bob"
    assert row["action"] == "policy.create"
    assert row["target_type"] == "policy"
    assert row["target_id"] == "7"
    assert row["after"] == {"name": "p"}
    assert row["before"] == {}


def test_record_normalizes_missing_before_after(db) -> None:
    with session_scope() as s:
        row = audit.record(
            s, actor=None, action="policy.delete", target_type="policy", target_id="1"
        )

    assert row["before"] == {} and row["after"] == {}
    assert row["actor"] is None


def test_list_newest_first(db) -> None:
    with session_scope() as s:
        audit.record(s, actor="a", action="policy.create", target_type="policy", target_id="1")
        audit.record(s, actor="a", action="policy.create", target_type="policy", target_id="2")
        rows = repo.list_audit_logs(s)

    # Newest first: the second insert (higher id) leads, even with equal timestamps.
    assert [r["target_id"] for r in rows] == ["2", "1"]


def test_list_filter_by_actor(db) -> None:
    with session_scope() as s:
        audit.record(s, actor="alice", action="policy.create", target_type="policy", target_id="1")
        audit.record(s, actor="bob", action="policy.create", target_type="policy", target_id="2")
        rows = repo.list_audit_logs(s, actor="alice")

    assert [r["actor"] for r in rows] == ["alice"]


def test_list_filter_by_target(db) -> None:
    with session_scope() as s:
        audit.record(s, actor="a", action="policy.create", target_type="policy", target_id="1")
        audit.record(s, actor="a", action="team.create", target_type="team", target_id="9")
        by_type = repo.list_audit_logs(s, target_type="team")
        by_id = repo.list_audit_logs(s, target_id="1")

    assert [r["target_type"] for r in by_type] == ["team"]
    assert [r["target_id"] for r in by_id] == ["1"]


def test_changed_fields_diff() -> None:
    before = {"name": "a", "description": "old", "enabled": True}
    after = {"name": "a", "description": "new", "enabled": True}

    assert audit.changed_fields(before, after) == ["description"]


# --------------------------------------------------------------------------- #
# API end-to-end
# --------------------------------------------------------------------------- #
def test_create_writes_audit_entry(db) -> None:
    client = _client()
    created = _create(client, "audit-create", actor="alice")

    rows = _audit_rows(target_type="policy")
    assert len(rows) == 1
    row = rows[0]
    assert row["action"] == "policy.create"
    assert row["actor"] == "alice"
    assert row["target_id"] == str(created["id"])
    assert row["after"]["name"] == "audit-create"
    assert row["before"] == {}  # create has no prior state


def test_update_records_before_after(db) -> None:
    client = _client()
    created = _create(client, "audit-update", actor="alice")

    resp = client.put(
        f"/api/policies/{created['id']}",
        json={"description": "changed"},
        headers={"X-Principal": "carol"},
    )
    assert resp.status_code == 200

    rows = _audit_rows(action="policy.update")
    assert len(rows) == 1
    row = rows[0]
    assert row["actor"] == "carol"
    assert row["before"].get("description") != row["after"].get("description")
    assert row["after"]["description"] == "changed"


def test_delete_records_audit(db) -> None:
    client = _client()
    created = _create(client, "audit-delete", actor="alice")

    resp = client.delete(f"/api/policies/{created['id']}", headers={"X-Principal": "dave"})
    assert resp.status_code == 200

    rows = _audit_rows(action="policy.delete")
    assert len(rows) == 1
    row = rows[0]
    assert row["actor"] == "dave"
    assert row["before"]["name"] == "audit-delete"  # prior state captured
    assert row["after"] == {}  # nothing remains


def test_reads_not_audited(db) -> None:
    client = _client()
    created = _create(client, "audit-read", actor="alice")  # 1 entry (the create)

    client.get("/api/policies")
    client.get(f"/api/policies/{created['id']}")
    client.get("/api/policies")

    rows = _audit_rows()
    assert len(rows) == 1
    assert rows[0]["action"] == "policy.create"  # no read entries added


def test_audit_list_newest_first(db) -> None:
    client = _client()
    _create(client, "audit-first", actor="alice")
    _create(client, "audit-second", actor="alice")

    rows = _audit_rows(target_type="policy")
    assert [r["after"]["name"] for r in rows] == ["audit-second", "audit-first"]


def test_audit_filters_by_actor(db) -> None:
    client = _client()
    _create(client, "audit-alice", actor="alice")
    _create(client, "audit-bob", actor="bob")

    rows = _audit_rows(actor="bob")
    assert [r["actor"] for r in rows] == ["bob"]
    assert [r["after"]["name"] for r in rows] == ["audit-bob"]


def test_audit_log_is_append_only(db) -> None:
    """No update/delete endpoints — only GET is defined on /api/audit."""
    client = _client()
    assert client.post("/api/audit", json={}).status_code == 405
    assert client.put("/api/audit", json={}).status_code == 405
    assert client.delete("/api/audit").status_code == 405


def test_set_enabled_writes_audit(db) -> None:
    client = _client()
    created = _create(client, "audit-toggle", actor="alice")

    resp = client.post(
        f"/api/policies/{created['id']}/enabled",
        params={"enabled": False},
        headers={"X-Principal": "erin"},
    )
    assert resp.status_code == 200

    rows = _audit_rows(action="policy.disable")
    assert len(rows) == 1
    assert rows[0]["actor"] == "erin"
    assert rows[0]["after"]["enabled"] is False
