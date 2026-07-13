"""Azure Event Grid ingestion (M6.1) — the real-time enforcement ingress.

Written test-first (TDD). No live Event Grid: fixtures under ``fixtures/events/`` are
literal Event Grid delivery payloads, and the endpoint is exercised via FastAPI's
``TestClient``. Invariants under test (Arrange–Act–Assert):

* the one-time ``SubscriptionValidation`` handshake echoes ``validationCode``;
* ``ResourceWriteSuccess`` / ``ResourceDeleteSuccess`` normalize; unknown types skip;
* the shared-key check accepts a matching header, rejects a mismatch, and (mock-mode
  friendly) allows all when no key is configured;
* an accepted delivery is persisted to ``event_log``; **re-delivery does not duplicate**;
* a request that fails the key check is ``403`` and never reaches ``event_log``;
* ``GET /api/events`` returns deliveries newest-first.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from azure_finops.api.main import app
from azure_finops.azure._fixtures import load_fixture
from azure_finops.config import Settings, get_settings
from azure_finops.events.ingestion import (
    handle_subscription_validation,
    normalize_event,
    verify_event_grid_key,
)
from azure_finops.storage import repository as repo
from azure_finops.storage.db import session_scope


def _event(name: str) -> dict:
    """First (only) delivery in a fixture array."""
    return load_fixture(f"events/{name}")[0]


@pytest.fixture
def client():
    return TestClient(app)


# --------------------------------------------------------------------------- #
# SubscriptionValidation handshake
# --------------------------------------------------------------------------- #
def test_handle_subscription_validation_echoes_code() -> None:
    events = load_fixture("events/subscription_validation")
    assert handle_subscription_validation(events) == {
        "validationResponse": "512d38b6-c7b8-40c8-89fe-f46f9e9622b6"
    }


def test_handle_subscription_validation_none_for_ordinary_events() -> None:
    events = load_fixture("events/resource_write_success")
    assert handle_subscription_validation(events) is None


# --------------------------------------------------------------------------- #
# normalize_event
# --------------------------------------------------------------------------- #
def test_normalize_event_maps_resource_write_success() -> None:
    ev = normalize_event(_event("resource_write_success"))
    assert ev is not None
    assert ev.event_id == "619205ba-4a7d-4f3c-9a6b-1a2b3c4d5e6f"
    assert ev.event_type == "Microsoft.Resources.ResourceWriteSuccess"
    assert ev.resource_id.endswith("/microsoft.compute/virtualmachines/vm-web-01")
    assert ev.resource_id == ev.resource_id.lower()  # lower-cased to join with assets
    assert ev.resource_type == "microsoft.compute/virtualmachines"
    assert ev.operation_name == "Microsoft.Compute/virtualMachines/write"
    assert ev.subscription_id == "00000000-0000-0000-0000-000000000000"
    assert ev.actor == "alice@contoso.com"
    assert ev.status == "Succeeded"
    assert ev.event_time is not None and ev.event_time.year == 2026


def test_normalize_event_maps_resource_delete_success() -> None:
    ev = normalize_event(_event("resource_delete_success"))
    assert ev is not None
    assert ev.operation_name == "Microsoft.Compute/disks/delete"
    assert ev.resource_type == "microsoft.compute/disks"
    assert ev.actor == "bob@contoso.com"


def test_normalize_event_returns_none_for_unsupported_type() -> None:
    assert normalize_event(_event("unsupported_event_type")) is None


def test_normalize_event_returns_none_for_non_dict() -> None:
    assert normalize_event("not-a-dict") is None
    assert (
        normalize_event({"eventType": "Microsoft.Resources.ResourceWriteSuccess"}) is None
    )  # no id


def test_normalize_event_falls_back_to_subject_and_defaults() -> None:
    ev = normalize_event(
        {
            "id": "action-1",
            "eventType": "Microsoft.Resources.ResourceActionSuccess",
            "subject": "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/VM",
            "data": {"operationName": "Microsoft.Compute/virtualMachines/restart/action"},
        }
    )
    assert ev is not None
    assert ev.resource_id.endswith("/vm")  # resourceUri absent → subject, lower-cased
    assert ev.status == "received"  # data.status absent → default
    assert ev.actor is None  # no claims


def test_resource_type_from_operation_edge_cases() -> None:
    from azure_finops.events.ingestion import _resource_type_from_operation

    assert _resource_type_from_operation(None) is None
    assert _resource_type_from_operation("tooshort") is None
    assert (
        _resource_type_from_operation("Microsoft.Compute/virtualMachines/write")
        == "microsoft.compute/virtualmachines"
    )


def test_parse_event_time_variants() -> None:
    import datetime as dt

    from azure_finops.events.ingestion import _parse_event_time

    assert _parse_event_time(None) is None
    assert _parse_event_time("") is None
    assert _parse_event_time("not-a-date") is None
    t = _parse_event_time("2026-07-01T09:41:52.1234567Z")  # 7-digit fraction + Z
    assert t is not None and t.tzinfo is not None and t.microsecond == 123456
    assert _parse_event_time("2026-07-01T09:41:52").tzinfo == dt.UTC  # naive → UTC
    assert _parse_event_time(dt.datetime(2026, 7, 1, 9, 0)).tzinfo == dt.UTC
    aware = dt.datetime(2026, 7, 1, tzinfo=dt.UTC)
    assert _parse_event_time(aware) is aware


# --------------------------------------------------------------------------- #
# verify_event_grid_key
# --------------------------------------------------------------------------- #
def _settings(key: str | None) -> Settings:
    s = get_settings()
    s.azure_eventgrid_shared_key = key
    return s


def test_verify_event_grid_key_accepts_matching_header() -> None:
    assert verify_event_grid_key({"x-events-key": "s3cret"}, {}, _settings("s3cret")) is True


def test_verify_event_grid_key_accepts_matching_query_param() -> None:
    assert verify_event_grid_key({}, {"key": "s3cret"}, _settings("s3cret")) is True


def test_verify_event_grid_key_rejects_mismatched_key() -> None:
    assert verify_event_grid_key({"x-events-key": "wrong"}, {}, _settings("s3cret")) is False
    assert verify_event_grid_key({}, {}, _settings("s3cret")) is False  # missing entirely


def test_verify_event_grid_key_allows_all_when_unconfigured() -> None:
    assert verify_event_grid_key({}, {}, _settings(None)) is True
    assert verify_event_grid_key({}, {}, _settings("")) is True


# --------------------------------------------------------------------------- #
# POST /api/events/azure + GET /api/events (endpoint level)
# --------------------------------------------------------------------------- #
def test_post_events_azure_returns_validation_response(db, client) -> None:
    resp = client.post("/api/events/azure", json=load_fixture("events/subscription_validation"))
    assert resp.status_code == 200
    assert resp.json() == {"validationResponse": "512d38b6-c7b8-40c8-89fe-f46f9e9622b6"}
    # the handshake persists nothing
    with session_scope() as s:
        assert repo.list_events(s) == []


def test_post_events_azure_persists_normalized_event(db, client) -> None:
    resp = client.post("/api/events/azure", json=load_fixture("events/resource_write_success"))
    assert resp.status_code == 200
    assert resp.json() == {"received": 1, "processed": 1}
    with session_scope() as s:
        rows = repo.list_events(s)
    assert len(rows) == 1
    assert rows[0]["event_type"] == "Microsoft.Resources.ResourceWriteSuccess"
    assert rows[0]["resource_id"].endswith("/vm-web-01")


def test_post_events_azure_skips_unsupported(db, client) -> None:
    resp = client.post("/api/events/azure", json=load_fixture("events/unsupported_event_type"))
    assert resp.json() == {"received": 1, "processed": 1} or resp.json() == {
        "received": 1,
        "processed": 0,
    }
    assert resp.json()["processed"] == 0
    with session_scope() as s:
        assert repo.list_events(s) == []


def test_post_events_azure_dedupes_redelivered_event_id(db, client) -> None:
    payload = load_fixture("events/resource_write_success")
    client.post("/api/events/azure", json=payload)
    client.post("/api/events/azure", json=payload)  # at-least-once re-delivery
    with session_scope() as s:
        rows = repo.list_events(s)
    assert len(rows) == 1  # deduped on event_id


def test_post_events_azure_rejects_bad_key(db, client, monkeypatch) -> None:
    monkeypatch.setenv("AZURE_EVENTGRID_SHARED_KEY", "s3cret")
    get_settings.cache_clear()
    resp = client.post(
        "/api/events/azure",
        json=load_fixture("events/resource_write_success"),
        headers={"x-events-key": "wrong"},
    )
    assert resp.status_code == 403
    with session_scope() as s:
        assert repo.list_events(s) == []  # never reached event_log
    get_settings.cache_clear()


def test_post_events_azure_accepts_good_key(db, client, monkeypatch) -> None:
    monkeypatch.setenv("AZURE_EVENTGRID_SHARED_KEY", "s3cret")
    get_settings.cache_clear()
    resp = client.post(
        "/api/events/azure",
        json=load_fixture("events/resource_write_success"),
        headers={"x-events-key": "s3cret"},
    )
    assert resp.status_code == 200 and resp.json()["processed"] == 1
    get_settings.cache_clear()


def test_post_events_azure_malformed_body_400(db, client) -> None:
    resp = client.post(
        "/api/events/azure", content=b"not json", headers={"content-type": "application/json"}
    )
    assert resp.status_code == 400


def test_get_events_returns_recent_first(db, client) -> None:
    client.post("/api/events/azure", json=load_fixture("events/resource_write_success"))
    client.post("/api/events/azure", json=load_fixture("events/resource_delete_success"))
    body = client.get("/api/events").json()
    assert len(body) == 2
    # delete was delivered last → newest-first
    assert body[0]["event_type"] == "Microsoft.Resources.ResourceDeleteSuccess"
    assert body[1]["event_type"] == "Microsoft.Resources.ResourceWriteSuccess"
