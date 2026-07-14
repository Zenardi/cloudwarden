"""Asset relationships graph (M4.3): typed edges derived from asset config.

Written test-first (TDD). DB-backed (the ``db`` testcontainers fixture). Exercises
the relationship builder (`repo.build_relationships`) that reads each asset's
config for known references — a managed disk's ``managedBy`` VM, a NIC's
``virtualMachine``, a public IP's bound NIC — and persists one typed
``asset_relationships`` edge per *resolvable* reference. A reference to an asset
that isn't present (dangling/external) is skipped, never fatal; re-running is
idempotent. `repo.get_relationships` (and ``GET /api/assets/{id}/relationships``)
returns an asset's neighbours in *both* directions (inbound and outbound).
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from cloudwarden import models as m
from cloudwarden.api.main import app
from cloudwarden.storage import repository as repo
from cloudwarden.storage.db import session_scope

_SUB = "/subscriptions/00000000-0000-0000-0000-000000000000"
VM = f"{_SUB}/resourcegroups/rg/providers/microsoft.compute/virtualmachines/vm-1"
DISK = f"{_SUB}/resourcegroups/rg/providers/microsoft.compute/disks/disk-1"
NIC = f"{_SUB}/resourcegroups/rg/providers/microsoft.network/networkinterfaces/nic-1"
PIP = f"{_SUB}/resourcegroups/rg/providers/microsoft.network/publicipaddresses/pip-1"


def _asset(
    resource_id: str,
    *,
    type: str,
    config: dict | None = None,
    location: str = "eastus",
    subscription_id: str = "sub-1",
) -> m.ResourceRecord:
    return m.ResourceRecord(
        resource_id=resource_id,
        name=resource_id.split("/")[-1],
        type=type,
        location=location,
        resource_group="rg",
        subscription_id=subscription_id,
        sku=None,
        tags={},
        power_state=None,
        config=config or {},
    )


def _edge_set(rows: list[dict]) -> set[tuple[str, str, str]]:
    return {(r["source_id"], r["target_id"], r["kind"]) for r in rows}


# --------------------------------------------------------------------------- #
# Builder — happy paths (one edge type per test)
# --------------------------------------------------------------------------- #
def test_build_disk_to_vm_edge(db) -> None:
    with session_scope() as s:
        repo.upsert_assets(
            s,
            [
                _asset(VM, type="microsoft.compute/virtualmachines"),
                _asset(DISK, type="microsoft.compute/disks", config={"managedBy": VM}),
            ],
        )
    with session_scope() as s:
        n = repo.build_relationships(s)
    with session_scope() as s:
        edges = _edge_set(repo.get_relationships(s, DISK))
    assert n == 1
    assert (DISK, VM, "attached-to") in edges


def test_build_nic_to_vm_edge(db) -> None:
    with session_scope() as s:
        repo.upsert_assets(
            s,
            [
                _asset(VM, type="microsoft.compute/virtualmachines"),
                _asset(
                    NIC,
                    type="microsoft.network/networkinterfaces",
                    config={"virtualMachine": {"id": VM}},
                ),
            ],
        )
    with session_scope() as s:
        repo.build_relationships(s)
    with session_scope() as s:
        edges = _edge_set(repo.get_relationships(s, NIC))
    assert (NIC, VM, "attached-to") in edges


def test_build_ip_to_nic_edge(db) -> None:
    with session_scope() as s:
        repo.upsert_assets(
            s,
            [
                _asset(NIC, type="microsoft.network/networkinterfaces"),
                _asset(
                    PIP,
                    type="microsoft.network/publicipaddresses",
                    # the IP points at a NIC *ipConfiguration* sub-resource; the
                    # builder resolves it up to the parent NIC asset.
                    config={"ipConfiguration": {"id": f"{NIC}/ipConfigurations/ipconfig1"}},
                ),
            ],
        )
    with session_scope() as s:
        repo.build_relationships(s)
    with session_scope() as s:
        edges = _edge_set(repo.get_relationships(s, PIP))
    assert (PIP, NIC, "bound-to") in edges


def test_reference_resolution_is_case_insensitive(db) -> None:
    # Azure resource ids are case-insensitive; a mixed-case reference must still
    # resolve to the canonically-stored (lower-cased) asset id.
    with session_scope() as s:
        repo.upsert_assets(
            s,
            [
                _asset(VM, type="microsoft.compute/virtualmachines"),
                _asset(DISK, type="microsoft.compute/disks", config={"managedBy": VM.upper()}),
            ],
        )
    with session_scope() as s:
        repo.build_relationships(s)
    with session_scope() as s:
        edges = _edge_set(repo.get_relationships(s, DISK))
    assert (DISK, VM, "attached-to") in edges  # target is the stored (lower-cased) id


# --------------------------------------------------------------------------- #
# Builder — negative / edge cases
# --------------------------------------------------------------------------- #
def test_no_references_no_edges(db) -> None:
    with session_scope() as s:
        repo.upsert_assets(
            s,
            [
                _asset(VM, type="microsoft.compute/virtualmachines"),  # not an edge source
                _asset(DISK, type="microsoft.compute/disks", config={}),  # no managedBy
                _asset(
                    PIP,
                    type="microsoft.network/publicipaddresses",
                    config={"ipConfiguration": None},  # explicit null reference
                ),
            ],
        )
    with session_scope() as s:
        n = repo.build_relationships(s)
    with session_scope() as s:
        assert repo.get_relationships(s, DISK) == []
    assert n == 0  # no error, nothing written


def test_dangling_reference_skipped(db) -> None:
    with session_scope() as s:
        repo.upsert_assets(
            s,
            [
                # disk managed by a VM that was never ingested
                _asset(DISK, type="microsoft.compute/disks", config={"managedBy": VM}),
                # IP bound to a NIC (external ref, no marker) that isn't present
                _asset(
                    PIP,
                    type="microsoft.network/publicipaddresses",
                    config={"ipConfiguration": {"id": f"{_SUB}/…/absent-nic"}},
                ),
            ],
        )
    with session_scope() as s:
        n = repo.build_relationships(s)  # must not raise
    with session_scope() as s:
        rows = repo._rows(s, "SELECT count(*) AS n FROM asset_relationships")
    assert n == 0 and rows[0]["n"] == 0


def test_build_relationships_is_idempotent(db) -> None:
    with session_scope() as s:
        repo.upsert_assets(
            s,
            [
                _asset(VM, type="microsoft.compute/virtualmachines"),
                _asset(DISK, type="microsoft.compute/disks", config={"managedBy": VM}),
            ],
        )
    with session_scope() as s:
        first = repo.build_relationships(s)
    with session_scope() as s:
        second = repo.build_relationships(s)  # re-derive over the same data
    with session_scope() as s:
        total = repo._rows(s, "SELECT count(*) AS n FROM asset_relationships")[0]["n"]
    assert first == 1 and second == 0 and total == 1


# --------------------------------------------------------------------------- #
# get_relationships — neighbours in both directions
# --------------------------------------------------------------------------- #
def test_get_relationships_returns_neighbours(db) -> None:
    with session_scope() as s:
        repo.upsert_assets(
            s,
            [
                _asset(VM, type="microsoft.compute/virtualmachines"),
                _asset(DISK, type="microsoft.compute/disks", config={"managedBy": VM}),
                _asset(
                    NIC,
                    type="microsoft.network/networkinterfaces",
                    config={"virtualMachine": {"id": VM}},
                ),
            ],
        )
    with session_scope() as s:
        repo.build_relationships(s)

    # The VM is the target of two edges → both are inbound neighbours.
    with session_scope() as s:
        vm_rels = repo.get_relationships(s, VM)
    assert _edge_set(vm_rels) == {(DISK, VM, "attached-to"), (NIC, VM, "attached-to")}
    assert all(r["direction"] == "inbound" and r["neighbor"] in {DISK, NIC} for r in vm_rels)

    # The disk is the source of one edge → outbound, neighbour is the VM.
    with session_scope() as s:
        disk_rels = repo.get_relationships(s, DISK)
    assert len(disk_rels) == 1
    assert disk_rels[0]["direction"] == "outbound" and disk_rels[0]["neighbor"] == VM


# --------------------------------------------------------------------------- #
# API endpoint
# --------------------------------------------------------------------------- #
def test_get_relationships_endpoint(db) -> None:
    with session_scope() as s:
        repo.upsert_assets(
            s,
            [
                _asset(VM, type="microsoft.compute/virtualmachines"),
                _asset(DISK, type="microsoft.compute/disks", config={"managedBy": VM}),
            ],
        )
    with session_scope() as s:
        repo.build_relationships(s)

    # Full Azure ids start with "/"; concatenating keeps a single leading slash
    # so the {resource_id:path} route captures the id cleanly.
    resp = TestClient(app).get(f"/api/assets{DISK}/relationships")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["source_id"] == DISK
    assert body[0]["target_id"] == VM
    assert body[0]["kind"] == "attached-to"
    assert body[0]["direction"] == "outbound"
