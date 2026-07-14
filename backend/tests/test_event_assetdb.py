"""Real-time AssetDB updates from events (M6.3) — streaming inventory.

Written test-first (TDD). DB-backed (the ``db`` fixture); events are constructed as
``NormalizedEvent`` (M6.1) and applied via ``events.assetdb.apply_asset_event`` — no
live Azure / Event Grid. Invariants under test (Arrange–Act–Assert):

* a **create** (``ResourceWriteSuccess``) for an unseen resource **inserts** an asset
  and appends a ``created`` ``asset_event``;
* a **second write** for the same resource **updates** ``last_seen`` (asset stays one
  row) and appends an ``updated`` event;
* a **delete** (``ResourceDeleteSuccess``) marks the asset ``state='deleted'`` and
  appends a ``deleted`` event;
* an event **missing ``resource_id``** is **ignored** — no asset, no event;
* the appended ``asset_event`` records the **actor** and **operation** from the event;
* an event update **never clobbers** config/tags a full ingestion (M4.1) set.
"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient

from cloudwarden.api.main import app
from cloudwarden.azure._fixtures import load_fixture
from cloudwarden.events.assetdb import apply_asset_event
from cloudwarden.events.models import NormalizedEvent
from cloudwarden.storage import repository as repo
from cloudwarden.storage import schema
from cloudwarden.storage.db import session_scope

_RID = (
    "/subscriptions/00000000-0000-0000-0000-000000000000/resourcegroups/rg-app/"
    "providers/microsoft.compute/virtualmachines/vm-web-01"
)


def _evt(
    *,
    event_type: str = "Microsoft.Resources.ResourceWriteSuccess",
    resource_id: str | None = _RID,
    operation_name: str = "Microsoft.Compute/virtualMachines/write",
    actor: str | None = "alice@contoso.com",
    event_id: str = "evt-1",
    status: str = "Succeeded",
) -> NormalizedEvent:
    return NormalizedEvent(
        event_id=event_id,
        event_type=event_type,
        subject=resource_id or "",
        resource_id=resource_id,
        subscription_id="00000000-0000-0000-0000-000000000000",
        resource_type="microsoft.compute/virtualmachines",
        operation_name=operation_name,
        event_time=dt.datetime(2026, 7, 13, 10, 0, tzinfo=dt.UTC),
        actor=actor,
        status=status,
    )


def _asset(rid: str = _RID):
    with session_scope() as s:
        return repo._asset_public(s.get(schema.Asset, rid)) if s.get(schema.Asset, rid) else None


def _events(rid: str = _RID) -> list[dict]:
    with session_scope() as s:
        return repo.get_asset_history(s, rid)


# --------------------------------------------------------------------------- #
# apply_asset_event — lifecycle
# --------------------------------------------------------------------------- #
def test_create_event_inserts_asset(db) -> None:
    result = apply_asset_event(_evt())

    assert result == {"resource_id": _RID, "lifecycle": "created"}
    asset = _asset()
    assert asset is not None
    assert asset["type"] == "microsoft.compute/virtualmachines"
    assert asset["state"] == "active"
    events = _events()
    assert len(events) == 1 and events[0]["event_type"] == "created"


def test_update_event_appends_event(db) -> None:
    apply_asset_event(_evt(event_id="e1"))  # create
    before = _asset()["last_seen"]

    result = apply_asset_event(_evt(event_id="e2"))  # second write → update

    assert result["lifecycle"] == "updated"
    assert len(_events()) == 2  # created + updated
    with session_scope() as s:
        assert s.query(schema.Asset).count() == 1  # still one asset row
    assert _asset()["last_seen"] >= before


def test_delete_event_marks_deleted(db) -> None:
    apply_asset_event(_evt(event_id="e1"))  # create

    result = apply_asset_event(
        _evt(
            event_id="e2",
            event_type="Microsoft.Resources.ResourceDeleteSuccess",
            operation_name="Microsoft.Compute/virtualMachines/delete",
        )
    )

    assert result["lifecycle"] == "deleted"
    assert _asset()["state"] == "deleted"
    assert [e["event_type"] for e in _events()] == ["deleted", "created"]  # newest-first


def test_event_missing_resource_id_ignored(db) -> None:
    assert apply_asset_event(_evt(resource_id=None)) is None
    with session_scope() as s:
        assert s.query(schema.Asset).count() == 0
        assert s.query(schema.AssetEvent).count() == 0


def test_event_records_actor_and_operation(db) -> None:
    apply_asset_event(_evt(actor="bob@contoso.com"))

    data = _events()[0]["data"]
    assert data["actor"] == "bob@contoso.com"
    assert data["operation"] == "Microsoft.Compute/virtualMachines/write"
    assert data["status"] == "Succeeded"
    assert data["event_id"] == "evt-1"


# --------------------------------------------------------------------------- #
# apply_asset_event — edges
# --------------------------------------------------------------------------- #
def test_action_event_on_existing_asset_is_update(db) -> None:
    apply_asset_event(_evt(event_id="e1"))  # create via write

    result = apply_asset_event(
        _evt(
            event_id="e2",
            event_type="Microsoft.Resources.ResourceActionSuccess",
            operation_name="Microsoft.Compute/virtualMachines/restart/action",
        )
    )

    assert result["lifecycle"] == "updated"


def test_delete_for_unseen_resource_still_records(db) -> None:
    # A delete may arrive for a resource we never ingested — it must still mark deleted.
    result = apply_asset_event(_evt(event_type="Microsoft.Resources.ResourceDeleteSuccess"))

    assert result["lifecycle"] == "deleted"
    assert _asset()["state"] == "deleted"


def test_event_update_preserves_prior_config_and_tags(db) -> None:
    # A full ingestion (M4.1) sets rich config/tags; a later event must not wipe them.
    from cloudwarden.models import ResourceRecord

    with session_scope() as s:
        repo.upsert_assets(
            s,
            [
                ResourceRecord(
                    resource_id=_RID,
                    subscription_id="00000000-0000-0000-0000-000000000000",
                    name="vm-web-01",
                    type="microsoft.compute/virtualmachines",
                    location="eastus",
                    resource_group="rg-app",
                    tags={"env": "prod"},
                    config={"hardwareProfile": {"vmSize": "Standard_D2s_v5"}},
                    power_state="active",
                )
            ],
        )

    apply_asset_event(_evt())  # an event carries no config/tags

    asset = _asset()
    assert asset["tags"] == {"env": "prod"}  # preserved
    assert asset["config"] == {"hardwareProfile": {"vmSize": "Standard_D2s_v5"}}  # preserved


# --------------------------------------------------------------------------- #
# Repository — upsert_asset_from_event
# --------------------------------------------------------------------------- #
def test_upsert_asset_from_event_returns_inserted_flag(db) -> None:
    with session_scope() as s:
        assert repo.upsert_asset_from_event(s, _evt()) is True  # first sight
    with session_scope() as s:
        assert repo.upsert_asset_from_event(s, _evt()) is False  # already present


# --------------------------------------------------------------------------- #
# Endpoint wiring — POST /api/events/azure updates the AssetDB
# --------------------------------------------------------------------------- #
@pytest.fixture
def client():
    return TestClient(app)


def test_ingestion_endpoint_updates_assetdb(db, client) -> None:
    resp = client.post("/api/events/azure", json=load_fixture("events/resource_write_success"))
    assert resp.status_code == 200

    with session_scope() as s:
        assets = s.query(schema.Asset).all()
        events = s.query(schema.AssetEvent).all()
    assert len(assets) == 1
    assert assets[0].resource_id.endswith("/vm-web-01")
    assert len(events) == 1 and events[0].event_type == "created"
