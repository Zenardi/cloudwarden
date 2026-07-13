"""Per-binding/policy notification config & dispatch (M8.4) — TDD.

Wires the M8.1–M8.3 notification machinery to **bindings**: a binding may reference
one or more (channel, template) pairs (``binding_notifications``); when a binding run
records a violation (a policy match), each configured channel is dispatched — through
an **injected** transport, so no test touches the network. A binding with no channel
dispatches nothing. Channels and templates get full CRUD via the API, and the binding
attach/detach is exposed too, backing the ``/notifications`` management page.

Invariants (Arrange–Act–Assert), each test one reason to fail:

* the transport registry maps a channel's ``transport`` to the right transport class;
* a violation on a binding **with** a channel dispatches the rendered message;
* a binding **without** a channel dispatches nothing;
* binding→(channel, template) attach/list/detach round-trips (and validates refs);
* channels/templates CRUD round-trips via the API and is validated (bad transport →
  400, duplicate name → 400);
* the notification API routes return 200.

Pure-registry tests need no database; the rest use the ``db`` fixture (and a
FastAPI ``TestClient`` for the HTTP surface). Every test is offline.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from azure_finops.api.main import app
from azure_finops.notify import dispatch
from azure_finops.notify.transports import (
    EmailTransport,
    JiraTransport,
    ServiceNowTransport,
    SlackTransport,
    TeamsTransport,
)
from azure_finops.storage import repository as repo
from azure_finops.storage.db import session_scope


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


class _RecordingTransport:
    """A transport spy — records every dispatched payload, makes no network call."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    def send(self, *, target: str, subject: str, body: str, config: dict) -> dict:
        self.sent.append({"target": target, "subject": subject, "body": body, "config": config})
        return {"ok": True}


# --------------------------------------------------------------------------- #
# helpers — build a binding and a channel/template
# --------------------------------------------------------------------------- #
def _binding() -> int:
    with session_scope() as s:
        cid = repo.create_collection(s, name="prod-policies")["id"]
        gid = repo.create_account_group(s, name="prod-subs")["id"]
        return repo.create_binding(s, collection_id=cid, account_group_id=gid)["id"]


def _channel(name: str = "ops-webhook", transport: str = "webhook") -> int:
    with session_scope() as s:
        return repo.create_notification_channel(
            s, name=name, transport=transport, target="https://hooks.example/ops"
        )["id"]


def _template(name: str = "violation") -> int:
    with session_scope() as s:
        return repo.create_notification_template(
            s,
            name=name,
            subject="[{{ policy_name }}] {{ count }} violation(s)",
            body="Policy {{ policy_name }} matched {{ resource_id }} ({{ count }} total).",
        )["id"]


# --------------------------------------------------------------------------- #
# transport registry (pure, no DB)
# --------------------------------------------------------------------------- #
def test_build_transport_registry() -> None:
    assert isinstance(dispatch.build_transport("slack"), SlackTransport)
    assert isinstance(dispatch.build_transport("email"), EmailTransport)
    assert isinstance(dispatch.build_transport("teams"), TeamsTransport)
    assert isinstance(dispatch.build_transport("jira"), JiraTransport)
    assert isinstance(dispatch.build_transport("servicenow"), ServiceNowTransport)
    # webhook is the default and the fallback for any unknown transport name.
    from azure_finops.notify.service import WebhookTransport

    assert isinstance(dispatch.build_transport("webhook"), WebhookTransport)
    assert isinstance(dispatch.build_transport("carrier-pigeon"), WebhookTransport)


# --------------------------------------------------------------------------- #
# dispatch — with & without a channel
# --------------------------------------------------------------------------- #
def test_violation_dispatches_when_channel_set(db) -> None:
    bid, cid, tid = _binding(), _channel(), _template()
    with session_scope() as s:
        repo.create_binding_notification(s, binding_id=bid, channel_id=cid, template_id=tid)
    spy = _RecordingTransport()

    with session_scope() as s:
        results = dispatch.dispatch_for_binding(
            s,
            binding_id=bid,
            policy_name="idle-vms",
            resource_ids=["/vm/1"],
            transport_factory=lambda name: spy,
        )

    assert len(results) == 1 and results[0]["dispatched"] is True
    assert len(spy.sent) == 1
    sent = spy.sent[0]
    assert sent["target"] == "https://hooks.example/ops"
    assert "idle-vms" in sent["subject"] and "1 violation" in sent["subject"]
    assert "/vm/1" in sent["body"] and "(1 total)" in sent["body"]


def test_no_dispatch_without_channel(db) -> None:
    bid = _binding()  # no binding_notifications attached
    spy = _RecordingTransport()

    with session_scope() as s:
        results = dispatch.dispatch_for_binding(
            s,
            binding_id=bid,
            policy_name="idle-vms",
            resource_ids=["/vm/1"],
            transport_factory=lambda name: spy,
        )

    assert results == []
    assert spy.sent == []


def test_dispatch_uses_channel_transport(db) -> None:
    # The factory receives the channel's transport kind so live dispatch picks the
    # right transport class per channel.
    bid, tid = _binding(), _template()
    cid = _channel(name="slack-ops", transport="slack")
    with session_scope() as s:
        repo.create_binding_notification(s, binding_id=bid, channel_id=cid, template_id=tid)
    seen: list[str] = []
    spy = _RecordingTransport()

    def factory(name: str):
        seen.append(name)
        return spy

    with session_scope() as s:
        dispatch.dispatch_for_binding(
            s, binding_id=bid, policy_name="p", resource_ids=["/a"], transport_factory=factory
        )

    assert seen == ["slack"]


# --------------------------------------------------------------------------- #
# binding→notification attach/list/detach (repository)
# --------------------------------------------------------------------------- #
def test_binding_notification_crud(db) -> None:
    bid, cid, tid = _binding(), _channel(), _template()

    with session_scope() as s:
        link = repo.create_binding_notification(s, binding_id=bid, channel_id=cid, template_id=tid)
        nid = link["id"]
    assert link["binding_id"] == bid and link["channel_id"] == cid and link["template_id"] == tid
    assert link["channel_name"] == "ops-webhook" and link["template_name"] == "violation"
    assert link["channel_transport"] == "webhook"

    with session_scope() as s:
        listed = repo.list_binding_notifications(s, bid)
    assert [x["id"] for x in listed] == [nid]

    # A duplicate (binding, channel) is rejected; missing refs return None.
    with session_scope() as s:
        with pytest.raises(ValueError):
            repo.create_binding_notification(s, binding_id=bid, channel_id=cid, template_id=tid)
    with session_scope() as s:
        assert (
            repo.create_binding_notification(s, binding_id=999999, channel_id=cid, template_id=tid)
            is None
        )
        assert (
            repo.create_binding_notification(s, binding_id=bid, channel_id=999999, template_id=tid)
            is None
        )
        assert (
            repo.create_binding_notification(s, binding_id=bid, channel_id=cid, template_id=999999)
            is None
        )

    with session_scope() as s:
        assert repo.delete_binding_notification(s, nid) is True
        assert repo.list_binding_notifications(s, bid) == []
        assert repo.delete_binding_notification(s, nid) is False  # idempotent


# --------------------------------------------------------------------------- #
# template update (repository) — completes template CRUD
# --------------------------------------------------------------------------- #
def test_template_update(db) -> None:
    tid = _template()
    with session_scope() as s:
        updated = repo.update_notification_template(s, tid, {"subject": "new subj", "body": "new"})
    assert updated["subject"] == "new subj" and updated["body"] == "new"
    with session_scope() as s:
        assert repo.update_notification_template(s, 999999, {"body": "x"}) is None  # missing


# --------------------------------------------------------------------------- #
# API — channels/templates CRUD + validation, notification routes 200
# --------------------------------------------------------------------------- #
def test_notifications_route_returns_200(db, client: TestClient) -> None:
    assert client.get("/api/notification-channels").status_code == 200
    assert client.get("/api/notification-templates").status_code == 200


def test_channel_template_crud(db, client: TestClient) -> None:
    # Channel: create → get → update → delete
    r = client.post(
        "/api/notification-channels",
        json={"name": "slack-ops", "transport": "slack", "target": "https://hooks.slack/x"},
    )
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    assert client.get(f"/api/notification-channels/{cid}").json()["name"] == "slack-ops"
    assert [c["id"] for c in client.get("/api/notification-channels").json()] == [cid]
    r = client.put(f"/api/notification-channels/{cid}", json={"enabled": False})
    assert r.status_code == 200 and r.json()["enabled"] is False
    assert client.delete(f"/api/notification-channels/{cid}").status_code == 200
    assert client.get(f"/api/notification-channels/{cid}").status_code == 404

    # Template: create → get → update → delete
    r = client.post(
        "/api/notification-templates",
        json={"name": "viol", "subject": "[{{ policy_name }}]", "body": "{{ count }}"},
    )
    assert r.status_code == 201, r.text
    tid = r.json()["id"]
    assert client.get(f"/api/notification-templates/{tid}").json()["name"] == "viol"
    r = client.put(f"/api/notification-templates/{tid}", json={"body": "changed"})
    assert r.status_code == 200 and r.json()["body"] == "changed"
    assert client.delete(f"/api/notification-templates/{tid}").status_code == 200
    assert client.get(f"/api/notification-templates/{tid}").status_code == 404


def test_invalid_channel_rejected(db, client: TestClient) -> None:
    # Unknown transport kind → 400 (validated against the transport registry).
    r = client.post(
        "/api/notification-channels",
        json={"name": "bad", "transport": "carrier-pigeon", "target": "x"},
    )
    assert r.status_code == 400
    # Empty target → 400.
    r = client.post(
        "/api/notification-channels", json={"name": "bad2", "transport": "webhook", "target": " "}
    )
    assert r.status_code == 400
    # Updating an existing channel to a bad transport is also rejected.
    cid = client.post(
        "/api/notification-channels",
        json={"name": "ok", "transport": "webhook", "target": "https://x"},
    ).json()["id"]
    r = client.put(f"/api/notification-channels/{cid}", json={"transport": "smoke-signal"})
    assert r.status_code == 400


def test_duplicate_channel_name_rejected(db, client: TestClient) -> None:
    body = {"name": "dup", "transport": "webhook", "target": "https://x"}
    assert client.post("/api/notification-channels", json=body).status_code == 201
    assert client.post("/api/notification-channels", json=body).status_code == 400


def test_duplicate_template_name_rejected(db, client: TestClient) -> None:
    body = {"name": "dup-tpl", "body": "hi {{ policy_name }}"}
    assert client.post("/api/notification-templates", json=body).status_code == 201
    assert client.post("/api/notification-templates", json=body).status_code == 400


def test_missing_channel_or_template_404(db, client: TestClient) -> None:
    assert client.get("/api/notification-channels/999999").status_code == 404
    assert (
        client.put("/api/notification-channels/999999", json={"enabled": True}).status_code == 404
    )
    assert client.delete("/api/notification-channels/999999").status_code == 404
    assert client.get("/api/notification-templates/999999").status_code == 404
    assert client.put("/api/notification-templates/999999", json={"body": "x"}).status_code == 404
    assert client.delete("/api/notification-templates/999999").status_code == 404


# --------------------------------------------------------------------------- #
# API — attach/list/detach a channel+template to a binding
# --------------------------------------------------------------------------- #
def test_attach_notification_to_binding_api(db, client: TestClient) -> None:
    bid, cid, tid = _binding(), _channel(), _template()

    r = client.post(
        f"/api/bindings/{bid}/notifications", json={"channel_id": cid, "template_id": tid}
    )
    assert r.status_code == 201, r.text
    nid = r.json()["id"]
    assert r.json()["channel_name"] == "ops-webhook"

    listed = client.get(f"/api/bindings/{bid}/notifications").json()
    assert [x["id"] for x in listed] == [nid]

    assert client.delete(f"/api/bindings/{bid}/notifications/{nid}").status_code == 200
    assert client.get(f"/api/bindings/{bid}/notifications").json() == []


def test_attach_unknown_channel_returns_404(db, client: TestClient) -> None:
    bid, tid = _binding(), _template()
    r = client.post(
        f"/api/bindings/{bid}/notifications", json={"channel_id": 999999, "template_id": tid}
    )
    assert r.status_code == 404


def test_attach_duplicate_channel_returns_409(db, client: TestClient) -> None:
    bid, cid, tid = _binding(), _channel(), _template()
    body = {"channel_id": cid, "template_id": tid}
    assert client.post(f"/api/bindings/{bid}/notifications", json=body).status_code == 201
    assert client.post(f"/api/bindings/{bid}/notifications", json=body).status_code == 409


def test_detach_unknown_notification_returns_404(db, client: TestClient) -> None:
    bid = _binding()
    assert client.delete(f"/api/bindings/{bid}/notifications/999999").status_code == 404
