"""Bindings (M5.2): link a policy collection to an account group with exec config.

Written test-first (TDD). DB-backed (the ``db`` fixture). Repository-level tests drive
the CRUD + validation seam directly; API tests drive the same behaviour through
FastAPI's ``TestClient``. Invariants under test (Arrange–Act–Assert):

* a binding references an **existing** collection and account group (else ``404``);
* ``mode`` must be ``pull`` or ``event`` (else ``400``);
* bindings default to ``dry_run=true`` / ``enabled=true``;
* list / get / update / delete work; deleting an unknown binding is ``404``;
* deleting the collection or group **cascades** the binding away.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from cloudwarden.api.main import app
from cloudwarden.storage import repository as repo
from cloudwarden.storage.db import session_scope


def _collection(name: str = "prod-policies") -> int:
    with session_scope() as s:
        return repo.create_collection(s, name=name)["id"]


def _group(name: str = "prod-accounts") -> int:
    with session_scope() as s:
        return repo.create_account_group(s, name=name)["id"]


@pytest.fixture
def client():
    return TestClient(app)


# --------------------------------------------------------------------------- #
# Repository layer
# --------------------------------------------------------------------------- #
def test_create_binding(db) -> None:
    cid, gid = _collection(), _group()
    with session_scope() as s:
        binding = repo.create_binding(
            s,
            collection_id=cid,
            account_group_id=gid,
            schedule="0 2 * * *",
            mode="pull",
            dry_run=False,
            enabled=True,
        )
    assert binding is not None
    assert binding["collection_id"] == cid
    assert binding["account_group_id"] == gid
    assert binding["schedule"] == "0 2 * * *"
    assert binding["mode"] == "pull"
    assert binding["dry_run"] is False
    assert binding["enabled"] is True


def test_binding_defaults_dry_run_true(db) -> None:
    cid, gid = _collection(), _group()
    with session_scope() as s:
        binding = repo.create_binding(s, collection_id=cid, account_group_id=gid)
    assert binding["dry_run"] is True
    assert binding["enabled"] is True
    assert binding["mode"] == "pull"


def test_binding_requires_existing_collection(db) -> None:
    gid = _group()
    with session_scope() as s:
        assert repo.create_binding(s, collection_id=999999, account_group_id=gid) is None


def test_binding_requires_existing_group(db) -> None:
    cid = _collection()
    with session_scope() as s:
        assert repo.create_binding(s, collection_id=cid, account_group_id=999999) is None


def test_binding_invalid_mode_raises(db) -> None:
    cid, gid = _collection(), _group()
    with session_scope() as s, pytest.raises(ValueError):
        repo.create_binding(s, collection_id=cid, account_group_id=gid, mode="sideways")


def test_list_and_get_binding(db) -> None:
    cid, gid = _collection(), _group()
    with session_scope() as s:
        bid = repo.create_binding(s, collection_id=cid, account_group_id=gid)["id"]
    with session_scope() as s:
        assert len(repo.list_bindings(s)) == 1
        assert repo.get_binding(s, bid)["id"] == bid
        assert repo.get_binding(s, 999999) is None


def test_update_binding(db) -> None:
    cid, gid = _collection(), _group()
    with session_scope() as s:
        bid = repo.create_binding(s, collection_id=cid, account_group_id=gid)["id"]
    with session_scope() as s:
        updated = repo.update_binding(
            s, bid, {"schedule": "*/5 * * * *", "mode": "event", "enabled": False}
        )
    assert updated["schedule"] == "*/5 * * * *"
    assert updated["mode"] == "event"
    assert updated["enabled"] is False
    assert updated["dry_run"] is True  # untouched field preserved


def test_update_unknown_binding_returns_none(db) -> None:
    with session_scope() as s:
        assert repo.update_binding(s, 999999, {"enabled": False}) is None


def test_update_binding_invalid_mode_raises(db) -> None:
    cid, gid = _collection(), _group()
    with session_scope() as s:
        bid = repo.create_binding(s, collection_id=cid, account_group_id=gid)["id"]
    with session_scope() as s, pytest.raises(ValueError):
        repo.update_binding(s, bid, {"mode": "nope"})


def test_delete_binding(db) -> None:
    cid, gid = _collection(), _group()
    with session_scope() as s:
        bid = repo.create_binding(s, collection_id=cid, account_group_id=gid)["id"]
    with session_scope() as s:
        assert repo.delete_binding(s, bid) is True
    with session_scope() as s:
        assert repo.get_binding(s, bid) is None


def test_delete_unknown_binding_returns_false(db) -> None:
    with session_scope() as s:
        assert repo.delete_binding(s, 999999) is False


def test_deleting_collection_cascades_binding(db) -> None:
    cid, gid = _collection(), _group()
    with session_scope() as s:
        repo.create_binding(s, collection_id=cid, account_group_id=gid)
    with session_scope() as s:
        repo.delete_collection(s, cid)  # cascade should drop the binding
    with session_scope() as s:
        assert repo.list_bindings(s) == []


# --------------------------------------------------------------------------- #
# API layer (FastAPI TestClient)
# --------------------------------------------------------------------------- #
def test_api_create_binding_defaults_dry_run_true(db, client) -> None:
    cid, gid = _collection(), _group()
    resp = client.post(
        "/api/bindings", json={"collection_id": cid, "account_group_id": gid, "mode": "pull"}
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["dry_run"] is True
    assert body["mode"] == "pull"


def test_api_binding_requires_existing_collection_404(db, client) -> None:
    gid = _group()
    resp = client.post("/api/bindings", json={"collection_id": 999999, "account_group_id": gid})
    assert resp.status_code == 404


def test_api_binding_requires_existing_group_404(db, client) -> None:
    cid = _collection()
    resp = client.post("/api/bindings", json={"collection_id": cid, "account_group_id": 999999})
    assert resp.status_code == 404


def test_api_binding_invalid_mode_400(db, client) -> None:
    cid, gid = _collection(), _group()
    resp = client.post(
        "/api/bindings",
        json={"collection_id": cid, "account_group_id": gid, "mode": "sideways"},
    )
    assert resp.status_code == 400


def test_api_list_update_delete(db, client) -> None:
    cid, gid = _collection(), _group()
    bid = client.post("/api/bindings", json={"collection_id": cid, "account_group_id": gid}).json()[
        "id"
    ]

    assert len(client.get("/api/bindings").json()) == 1
    assert client.get(f"/api/bindings/{bid}").json()["id"] == bid

    updated = client.put(f"/api/bindings/{bid}", json={"mode": "event", "dry_run": False})
    assert updated.status_code == 200
    assert updated.json()["mode"] == "event" and updated.json()["dry_run"] is False

    assert client.delete(f"/api/bindings/{bid}").json()["deleted"] is True
    assert client.get(f"/api/bindings/{bid}").status_code == 404


def test_api_delete_unknown_binding_404(db, client) -> None:
    assert client.delete("/api/bindings/999999").status_code == 404


def test_api_update_unknown_binding_404(db, client) -> None:
    assert client.put("/api/bindings/999999", json={"enabled": False}).status_code == 404


def test_api_get_unknown_binding_404(db, client) -> None:
    assert client.get("/api/bindings/999999").status_code == 404


def test_api_update_invalid_mode_400(db, client) -> None:
    cid, gid = _collection(), _group()
    bid = client.post("/api/bindings", json={"collection_id": cid, "account_group_id": gid}).json()[
        "id"
    ]
    assert client.put(f"/api/bindings/{bid}", json={"mode": "bad"}).status_code == 400
