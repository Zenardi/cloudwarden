"""AssetDB schema & ingestion (M4.1): ``assets`` + ``asset_events``.

Written test-first (TDD). DB-backed (the ``db`` testcontainers fixture) exercising
the queryable asset inventory that underpins the M4 AssetDB: ``upsert_assets`` is an
idempotent ON CONFLICT upsert that preserves ``first_seen`` while advancing
``last_seen``/``config`` and returns only the *newly inserted* ids (so the caller
records a "created" ``asset_event`` on first sight); inventory now captures the full
resource ``config``; and a mock pipeline run populates ``assets`` with that config,
each row carrying its (retargeted) ``subscription_id``. No live Azure — mock mode.
"""

from __future__ import annotations

from azure_finops import models as m
from azure_finops.azure.context import SubscriptionContext
from azure_finops.azure.inventory import collect_inventory
from azure_finops.storage import repository as repo
from azure_finops.storage.db import session_scope


def _asset(
    resource_id: str, *, subscription_id: str = "sub-1", config: dict | None = None
) -> m.ResourceRecord:
    return m.ResourceRecord(
        resource_id=resource_id,
        name="vm-x",
        type="microsoft.compute/virtualmachines",
        location="eastus",
        resource_group="rg-1",
        subscription_id=subscription_id,
        sku="Standard_D2s_v5",
        tags={"env": "dev"},
        power_state="PowerState/running",
        config=config or {},
    )


# --------------------------------------------------------------------------- #
# upsert_assets
# --------------------------------------------------------------------------- #
def test_upsert_assets_inserts_new(db) -> None:
    with session_scope() as s:
        new = repo.upsert_assets(
            s,
            [
                _asset("/a/1", config={"provisioningState": "Succeeded"}),
                _asset("/a/2"),
            ],
        )
    assert set(new) == {"/a/1", "/a/2"}

    with session_scope() as s:
        rows = repo._rows(
            s, "SELECT resource_id, subscription_id, config, state FROM assets ORDER BY resource_id"
        )
    assert [r["resource_id"] for r in rows] == ["/a/1", "/a/2"]
    assert rows[0]["config"] == {"provisioningState": "Succeeded"}
    assert rows[0]["state"] == "PowerState/running"


def test_upsert_assets_updates_last_seen(db) -> None:
    with session_scope() as s:
        repo.upsert_assets(s, [_asset("/a/1", config={"v": 1})])
    with session_scope() as s:
        before = repo._rows(s, "SELECT first_seen, last_seen FROM assets WHERE resource_id='/a/1'")[
            0
        ]

    with session_scope() as s:
        new = repo.upsert_assets(s, [_asset("/a/1", config={"v": 2})])
    assert new == []  # already seen → not newly inserted

    with session_scope() as s:
        after = repo._rows(
            s, "SELECT first_seen, last_seen, config FROM assets WHERE resource_id='/a/1'"
        )[0]
    assert after["config"] == {"v": 2}  # config refreshed
    assert after["first_seen"] == before["first_seen"]  # preserved
    assert after["last_seen"] > before["last_seen"]  # advanced


def test_upsert_assets_idempotent_no_duplicate(db) -> None:
    assets = [_asset("/a/1"), _asset("/a/2")]
    with session_scope() as s:
        repo.upsert_assets(s, assets)
    with session_scope() as s:
        repo.upsert_assets(s, assets)

    with session_scope() as s:
        count = repo._rows(s, "SELECT count(*) AS n FROM assets")[0]["n"]
    assert count == 2


def test_upsert_assets_empty_returns_empty(db) -> None:
    with session_scope() as s:
        assert repo.upsert_assets(s, []) == []


# --------------------------------------------------------------------------- #
# asset_events
# --------------------------------------------------------------------------- #
def test_append_asset_event_writes_row(db) -> None:
    with session_scope() as s:
        repo.append_asset_event(
            s,
            resource_id="/a/1",
            subscription_id="sub-1",
            event_type="created",
            data={"provisioningState": "Succeeded"},
        )
    with session_scope() as s:
        rows = repo._rows(
            s, "SELECT resource_id, subscription_id, event_type, data FROM asset_events"
        )
    assert len(rows) == 1
    assert rows[0]["resource_id"] == "/a/1"
    assert rows[0]["event_type"] == "created"
    assert rows[0]["data"] == {"provisioningState": "Succeeded"}


def test_asset_events_only_on_first_sight(db) -> None:
    records = collect_inventory()  # mock fixture

    def _ingest() -> None:
        with session_scope() as s:
            new = repo.upsert_assets(s, records)
            for rid in new:
                rec = next(r for r in records if r.resource_id == rid)
                repo.append_asset_event(
                    s,
                    resource_id=rid,
                    subscription_id=rec.subscription_id,
                    event_type="created",
                    data=rec.config,
                )

    _ingest()
    _ingest()  # re-ingest the same inventory

    with session_scope() as s:
        n_events = repo._rows(s, "SELECT count(*) AS n FROM asset_events")[0]["n"]
        n_assets = repo._rows(s, "SELECT count(*) AS n FROM assets")[0]["n"]
    assert n_assets == len(records)
    assert n_events == len(records)  # one 'created' per asset, not per run


# --------------------------------------------------------------------------- #
# Inventory config capture + per-subscription tagging
# --------------------------------------------------------------------------- #
def test_to_records_coerces_non_dict_config_and_tags() -> None:
    # Resource Graph can return a non-object properties/tags; coerce to {} defensively.
    from azure_finops.azure.inventory import _to_records

    records = _to_records(
        [{"id": "/X", "properties": "not-a-dict", "tags": "nope"}], "sub-1", mock=True
    )
    assert records[0].config == {}
    assert records[0].tags == {}


def test_collect_inventory_captures_config(db) -> None:
    records = collect_inventory()  # mock
    assert records, "expected fixture resources"
    assert any(r.config for r in records), "expected full config captured"


def test_assets_tagged_with_subscription_id(db) -> None:
    ctx = SubscriptionContext(subscription_id="sub-xyz")
    records = collect_inventory(subscription=ctx)
    assert all(r.subscription_id == "sub-xyz" for r in records)

    with session_scope() as s:
        repo.upsert_assets(s, records)
    with session_scope() as s:
        subs = {
            r["subscription_id"]
            for r in repo._rows(s, "SELECT DISTINCT subscription_id FROM assets")
        }
    assert subs == {"sub-xyz"}


# --------------------------------------------------------------------------- #
# End-to-end ingestion via the pipeline
# --------------------------------------------------------------------------- #
def test_ingestion_captures_full_config(db) -> None:
    from azure_finops.orchestrator import run_pipeline

    run_pipeline(mock=True)

    with session_scope() as s:
        assets = repo._rows(s, "SELECT resource_id, subscription_id, config FROM assets")
        events = repo._rows(s, "SELECT event_type, data FROM asset_events")
        rels = repo._rows(s, "SELECT source_id, target_id, kind FROM asset_relationships")
    assert len(assets) == 7  # fixture resource count
    assert all(a["config"] for a in assets)  # every asset carries full config
    created = [e for e in events if e["event_type"] == "created"]
    activity = [e for e in events if e["event_type"] == "activity"]
    assert len(created) == 7  # one 'created' per asset on first sight
    # M4.4: Activity Log ingested (who/how/when); the malformed fixture record is
    # skipped, so 5 of the 6 recorded events land — each with an actor and operation.
    assert len(activity) == 5
    assert all(a["data"].get("actor") and a["data"].get("operation") for a in activity)
    # M4.3: the pipeline derives the graph — the fixture NIC is attached to vm-web-01.
    assert any(
        r["source_id"].endswith("nic-web-01")
        and r["target_id"].endswith("vm-web-01")
        and r["kind"] == "attached-to"
        for r in rels
    )
