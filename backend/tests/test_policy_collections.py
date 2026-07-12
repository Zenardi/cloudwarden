"""Policy collections (M2.3): group policies into named, many-to-many collections.

Written test-first (TDD). DB-backed (the ``db`` fixture) so the join table really
persists. Repository-level tests drive the CRUD + membership seam directly; API
tests drive the same behaviour through FastAPI's ``TestClient``. The invariants
under test (Arrange–Act–Assert):

* a policy can belong to **multiple** collections independently;
* **deleting a collection never deletes member policies** — only the membership;
* adding an **unknown policy** (or to an unknown collection) returns ``404``;
* collection names are unique (``409`` on collision).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from azure_finops.api.main import app
from azure_finops.storage import repository as repo
from azure_finops.storage.db import session_scope

_SPEC = {"policies": [{"name": "p", "resource": "azure.vm"}]}


def _new_policy(name: str) -> int:
    with session_scope() as s:
        return repo.create_policy(s, name=name, resource_type="azure.vm", spec=_SPEC)["id"]


@pytest.fixture
def client():
    return TestClient(app)


# --------------------------------------------------------------------------- #
# Repository layer
# --------------------------------------------------------------------------- #
def test_list_collections_empty(db) -> None:
    with session_scope() as s:
        assert repo.list_collections(s) == []


def test_create_collection(db) -> None:
    # Arrange / Act
    with session_scope() as s:
        coll = repo.create_collection(s, name="prod", description="production estate")
    # Assert
    assert coll["id"] > 0
    assert coll["name"] == "prod"
    assert coll["description"] == "production estate"
    assert coll["policy_count"] == 0
    assert coll["policies"] == []
    with session_scope() as s:
        assert [c["name"] for c in repo.list_collections(s)] == ["prod"]


def test_add_policy_to_collection(db) -> None:
    # Arrange
    pid = _new_policy("stopped-vms")
    with session_scope() as s:
        cid = repo.create_collection(s, name="prod")["id"]
    # Act
    with session_scope() as s:
        result = repo.add_policy_to_collection(s, cid, pid)
    # Assert
    assert result is not None
    assert result["policy_count"] == 1
    assert [p["id"] for p in result["policies"]] == [pid]
    # idempotent — adding again does not duplicate
    with session_scope() as s:
        again = repo.add_policy_to_collection(s, cid, pid)
    assert again["policy_count"] == 1


def test_remove_policy_from_collection(db) -> None:
    # Arrange
    pid = _new_policy("stopped-vms")
    with session_scope() as s:
        cid = repo.create_collection(s, name="prod")["id"]
        repo.add_policy_to_collection(s, cid, pid)
    # Act
    with session_scope() as s:
        result = repo.remove_policy_from_collection(s, cid, pid)
    # Assert
    assert result is not None
    assert result["policy_count"] == 0
    # removing a non-member now returns None (nothing to remove)
    with session_scope() as s:
        assert repo.remove_policy_from_collection(s, cid, pid) is None


def test_policy_can_belong_to_multiple_collections(db) -> None:
    pid = _new_policy("stopped-vms")
    with session_scope() as s:
        a = repo.create_collection(s, name="team-a")["id"]
        b = repo.create_collection(s, name="team-b")["id"]
        repo.add_policy_to_collection(s, a, pid)
        repo.add_policy_to_collection(s, b, pid)
    with session_scope() as s:
        assert repo.get_collection(s, a)["policy_count"] == 1
        assert repo.get_collection(s, b)["policy_count"] == 1


def test_delete_collection_keeps_policies(db) -> None:
    # Arrange
    pid = _new_policy("stopped-vms")
    with session_scope() as s:
        cid = repo.create_collection(s, name="prod")["id"]
        repo.add_policy_to_collection(s, cid, pid)
    # Act
    with session_scope() as s:
        assert repo.delete_collection(s, cid) is True
    # Assert — the collection is gone but the policy survives
    with session_scope() as s:
        assert repo.get_collection(s, cid) is None
        assert repo.get_policy(s, pid) is not None


def test_delete_missing_collection_returns_false(db) -> None:
    with session_scope() as s:
        assert repo.delete_collection(s, 999999) is False


def test_add_to_unknown_collection_returns_none(db) -> None:
    pid = _new_policy("stopped-vms")
    with session_scope() as s:
        assert repo.add_policy_to_collection(s, 999999, pid) is None


def test_add_unknown_policy_to_collection_returns_none(db) -> None:
    with session_scope() as s:
        cid = repo.create_collection(s, name="prod")["id"]
    with session_scope() as s:
        assert repo.add_policy_to_collection(s, cid, 999999) is None


# --------------------------------------------------------------------------- #
# API layer
# --------------------------------------------------------------------------- #
def test_api_collections_crud_and_membership(db, client) -> None:
    # create
    r = client.post("/api/collections", json={"name": "prod", "description": "d"})
    assert r.status_code == 201
    cid = r.json()["id"]
    assert r.json()["policy_count"] == 0

    # duplicate name -> 409
    assert client.post("/api/collections", json={"name": "prod"}).status_code == 409

    # list + get
    assert len(client.get("/api/collections").json()) == 1
    assert client.get(f"/api/collections/{cid}").status_code == 200
    assert client.get("/api/collections/999999").status_code == 404

    # add a real policy
    pid = _new_policy("stopped-vms")
    added = client.post(f"/api/collections/{cid}/policies/{pid}")
    assert added.status_code == 200
    assert added.json()["policy_count"] == 1

    # remove it
    removed = client.request("DELETE", f"/api/collections/{cid}/policies/{pid}")
    assert removed.status_code == 200
    assert removed.json()["policy_count"] == 0

    # delete the collection
    assert client.request("DELETE", f"/api/collections/{cid}").status_code == 200
    assert client.get(f"/api/collections/{cid}").status_code == 404


def test_add_unknown_policy_returns_404(db, client) -> None:
    cid = client.post("/api/collections", json={"name": "prod"}).json()["id"]

    resp = client.post(f"/api/collections/{cid}/policies/999999")

    assert resp.status_code == 404


def test_api_delete_missing_collection_returns_404(db, client) -> None:
    assert client.request("DELETE", "/api/collections/999999").status_code == 404


def test_api_create_collection_blank_name_returns_400(db, client) -> None:
    assert client.post("/api/collections", json={"name": "   "}).status_code == 400


def test_api_remove_from_unknown_collection_returns_404(db, client) -> None:
    assert client.request("DELETE", "/api/collections/999999/policies/1").status_code == 404
