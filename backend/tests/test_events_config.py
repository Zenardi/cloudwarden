"""Event config gate + recent-events status feed (M6.4) — real-time visibility.

Written test-first (TDD). DB-backed (the ``db`` fixture) + FastAPI ``TestClient``.
Invariants under test (Arrange–Act–Assert):

* ``GET /api/events/recent`` returns ingested deliveries **newest-first** with each
  event's **triggered executions** attached, and supports ``limit``/``offset``;
* an **empty** feed returns ``[]`` (not an error);
* the ``EVENT_MODE_ENABLED`` flag gates ingestion — when ``false``, ``POST
  /api/events/azure`` returns **202** and stores **nothing**;
* an event that triggered an event-mode policy (M6.2) **links** to that execution.
"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient

from cloudwarden.api.main import app, get_custodian_runner
from cloudwarden.azure._fixtures import load_fixture
from cloudwarden.config import get_settings
from cloudwarden.events.models import NormalizedEvent
from cloudwarden.storage import repository as repo
from cloudwarden.storage.db import session_scope

_EVENT_SPEC = {
    "policies": [
        {
            "name": "vm-guard",
            "resource": "azure.vm",
            "mode": {
                "type": "azure-event-grid",
                "events": ["Microsoft.Compute/virtualMachines/write"],
            },
            "actions": ["stop"],
        }
    ]
}


class FakeCustodianRunner:
    """Returns 1 match, records nothing meaningful. No c7n/Azure."""

    def validate(self, spec: dict) -> dict:
        return {"valid": True, "errors": []}

    def run(self, spec: dict, subscription_id: str, credential, dry_run: bool) -> dict:
        return {"resources": [{"id": f"/subscriptions/{subscription_id}/vm-1", "type": "azure.vm"}]}

    def schema(self, resource_type: str | None = None) -> dict:
        return {"resource_types": []}


def _evt(event_id: str, *, resource_id: str = "/subscriptions/s/rg/vm") -> NormalizedEvent:
    return NormalizedEvent(
        event_id=event_id,
        event_type="Microsoft.Resources.ResourceWriteSuccess",
        subject=resource_id,
        resource_id=resource_id,
        subscription_id="sub-1",
        resource_type="microsoft.compute/virtualmachines",
        operation_name="Microsoft.Compute/virtualMachines/write",
        event_time=dt.datetime(2026, 7, 13, 10, 0, tzinfo=dt.UTC),
        actor="alice@contoso.com",
        status="Succeeded",
    )


def _log(event_id: str) -> None:
    with session_scope() as s:
        repo.insert_event_log(s, _evt(event_id))


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def client_with_runner():
    runner = FakeCustodianRunner()
    app.dependency_overrides[get_custodian_runner] = lambda: runner
    yield TestClient(app)
    app.dependency_overrides.clear()


# --------------------------------------------------------------------------- #
# GET /api/events/recent
# --------------------------------------------------------------------------- #
def test_recent_events_returns_latest(db, client) -> None:
    for eid in ("e1", "e2", "e3"):
        _log(eid)

    body = client.get("/api/events/recent").json()

    assert [e["event_id"] for e in body] == ["e3", "e2", "e1"]  # newest-first
    assert body[0]["event_type"] == "Microsoft.Resources.ResourceWriteSuccess"
    assert body[0]["triggered_executions"] == []  # none fired for a bare log


def test_recent_events_empty(db, client) -> None:
    assert client.get("/api/events/recent").json() == []


def test_recent_events_pagination(db, client) -> None:
    for eid in ("e1", "e2", "e3", "e4", "e5"):
        _log(eid)

    page1 = client.get("/api/events/recent?limit=2&offset=0").json()
    page2 = client.get("/api/events/recent?limit=2&offset=2").json()

    assert [e["event_id"] for e in page1] == ["e5", "e4"]
    assert [e["event_id"] for e in page2] == ["e3", "e2"]


def test_event_links_to_triggered_execution(db, client) -> None:
    _log("e1")
    with session_scope() as s:
        pid = repo.create_policy(s, name="p", resource_type="azure.vm", spec=_EVENT_SPEC)["id"]
        repo.create_policy_execution(
            s,
            execution_id="exec-1",
            policy_id=pid,
            subscription_id="sub-1",
            mode="event",
            event_id="e1",
        )
        repo.finish_policy_execution(s, "exec-1", status="succeeded", resources_matched=2)

    event = client.get("/api/events/recent").json()[0]

    assert event["event_id"] == "e1"
    triggered = event["triggered_executions"]
    assert len(triggered) == 1
    assert triggered[0]["execution_id"] == "exec-1"
    assert triggered[0]["policy_id"] == pid
    assert triggered[0]["status"] == "succeeded"
    assert triggered[0]["mode"] == "event"


# --------------------------------------------------------------------------- #
# EVENT_MODE_ENABLED gate
# --------------------------------------------------------------------------- #
def test_ingestion_disabled_returns_202_noop(db, client, monkeypatch) -> None:
    monkeypatch.setenv("EVENT_MODE_ENABLED", "false")
    get_settings.cache_clear()

    resp = client.post("/api/events/azure", json=load_fixture("events/resource_write_success"))

    assert resp.status_code == 202
    with session_scope() as s:
        assert repo.list_events(s) == []  # stored nothing
    get_settings.cache_clear()


def test_ingestion_enabled_by_default_processes(db, client) -> None:
    resp = client.post("/api/events/azure", json=load_fixture("events/resource_write_success"))

    assert resp.status_code == 200
    assert resp.json()["processed"] == 1
    with session_scope() as s:
        assert len(repo.list_events(s)) == 1


# --------------------------------------------------------------------------- #
# End-to-end: a delivery triggers a policy and the feed links them
# --------------------------------------------------------------------------- #
def test_recent_feed_links_endpoint_triggered_execution(db, client_with_runner) -> None:
    with session_scope() as s:
        repo.create_policy(s, name="vm-guard", resource_type="azure.vm", spec=_EVENT_SPEC)

    client_with_runner.post("/api/events/azure", json=load_fixture("events/resource_write_success"))

    feed = client_with_runner.get("/api/events/recent").json()
    assert len(feed) == 1
    triggered = feed[0]["triggered_executions"]
    assert len(triggered) == 1
    assert triggered[0]["mode"] == "event"
    assert triggered[0]["status"] == "succeeded"
