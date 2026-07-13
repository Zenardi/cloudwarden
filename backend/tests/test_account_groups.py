"""Account groups (M5.1): organize subscriptions into named, many-to-many groups.

Written test-first (TDD). DB-backed (the ``db`` fixture) so the join table really
persists. Repository-level tests drive the CRUD + membership seam directly; API tests
drive the same behaviour through FastAPI's ``TestClient``. Invariants under test
(Arrange–Act–Assert):

* a subscription can belong to **multiple** groups and be removed independently;
* **deleting a group never deletes member subscriptions** — only the membership;
* adding an **unknown subscription** (or to an unknown group) returns ``404``;
* listing a group returns its member subscriptions;
* group names are unique (``409`` on collision).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from azure_finops.api.main import app
from azure_finops.storage import repository as repo
from azure_finops.storage.db import session_scope

SUB_A = "11111111-1111-1111-1111-111111111111"
SUB_B = "22222222-2222-2222-2222-222222222222"


def _seed_sub(subscription_id: str, display_name: str) -> None:
    with session_scope() as s:
        repo.upsert_subscription(s, subscription_id=subscription_id, display_name=display_name)


@pytest.fixture
def client():
    return TestClient(app)


# --------------------------------------------------------------------------- #
# Repository layer
# --------------------------------------------------------------------------- #
def test_create_account_group(db) -> None:
    with session_scope() as s:
        group = repo.create_account_group(s, name="prod", description="production accounts")
    assert group["name"] == "prod"
    assert group["description"] == "production accounts"
    assert group["subscription_count"] == 0
    assert group["subscriptions"] == []


def test_add_subscription_to_group(db) -> None:
    _seed_sub(SUB_A, "Account A")
    with session_scope() as s:
        gid = repo.create_account_group(s, name="prod")["id"]
    with session_scope() as s:
        group = repo.add_subscription_to_group(s, gid, SUB_A)
    assert group is not None
    assert group["subscription_count"] == 1
    assert [m["subscription_id"] for m in group["subscriptions"]] == [SUB_A]


def test_remove_subscription_from_group(db) -> None:
    _seed_sub(SUB_A, "Account A")
    with session_scope() as s:
        gid = repo.create_account_group(s, name="prod")["id"]
        repo.add_subscription_to_group(s, gid, SUB_A)
    with session_scope() as s:
        group = repo.remove_subscription_from_group(s, gid, SUB_A)
    assert group is not None
    assert group["subscription_count"] == 0


def test_subscription_in_multiple_groups_removed_independently(db) -> None:
    _seed_sub(SUB_A, "Account A")
    with session_scope() as s:
        g1 = repo.create_account_group(s, name="prod")["id"]
        g2 = repo.create_account_group(s, name="eu")["id"]
        repo.add_subscription_to_group(s, g1, SUB_A)
        repo.add_subscription_to_group(s, g2, SUB_A)
    # Remove from the first group only.
    with session_scope() as s:
        repo.remove_subscription_from_group(s, g1, SUB_A)
    with session_scope() as s:
        left = repo.get_account_group(s, g1)
        right = repo.get_account_group(s, g2)
    assert left["subscription_count"] == 0
    assert [m["subscription_id"] for m in right["subscriptions"]] == [SUB_A]


def test_delete_group_keeps_subscriptions(db) -> None:
    _seed_sub(SUB_A, "Account A")
    with session_scope() as s:
        gid = repo.create_account_group(s, name="prod")["id"]
        repo.add_subscription_to_group(s, gid, SUB_A)
    with session_scope() as s:
        assert repo.delete_account_group(s, gid) is True
    with session_scope() as s:
        assert repo.get_account_group(s, gid) is None  # group gone
        assert repo.get_subscription(s, SUB_A) is not None  # subscription intact


def test_add_unknown_subscription_returns_none(db) -> None:
    with session_scope() as s:
        gid = repo.create_account_group(s, name="prod")["id"]
    with session_scope() as s:
        assert repo.add_subscription_to_group(s, gid, "does-not-exist") is None


def test_add_to_unknown_group_returns_none(db) -> None:
    _seed_sub(SUB_A, "Account A")
    with session_scope() as s:
        assert repo.add_subscription_to_group(s, 999999, SUB_A) is None


def test_list_account_groups_returns_members(db) -> None:
    _seed_sub(SUB_A, "Account A")
    _seed_sub(SUB_B, "Account B")
    with session_scope() as s:
        gid = repo.create_account_group(s, name="prod")["id"]
        repo.add_subscription_to_group(s, gid, SUB_A)
        repo.add_subscription_to_group(s, gid, SUB_B)
    with session_scope() as s:
        groups = repo.list_account_groups(s)
    assert len(groups) == 1
    assert {m["subscription_id"] for m in groups[0]["subscriptions"]} == {SUB_A, SUB_B}


def test_delete_unknown_group_returns_false(db) -> None:
    with session_scope() as s:
        assert repo.delete_account_group(s, 999999) is False


def test_remove_unknown_membership_returns_none(db) -> None:
    _seed_sub(SUB_A, "Account A")
    with session_scope() as s:
        gid = repo.create_account_group(s, name="prod")["id"]
    with session_scope() as s:
        assert repo.remove_subscription_from_group(s, gid, SUB_A) is None


def test_remove_from_unknown_group_returns_none(db) -> None:
    with session_scope() as s:
        assert repo.remove_subscription_from_group(s, 999999, SUB_A) is None


# --------------------------------------------------------------------------- #
# API layer (FastAPI TestClient)
# --------------------------------------------------------------------------- #
def test_api_account_group_lifecycle(db, client) -> None:
    _seed_sub(SUB_A, "Account A")

    created = client.post("/api/account-groups", json={"name": "prod"})
    assert created.status_code == 201
    gid = created.json()["id"]

    # Duplicate name → 409.
    assert client.post("/api/account-groups", json={"name": "prod"}).status_code == 409
    # Blank name → 400.
    assert client.post("/api/account-groups", json={"name": "   "}).status_code == 400

    # Add a real subscription → 200 and it appears as a member.
    added = client.post(f"/api/account-groups/{gid}/subscriptions/{SUB_A}")
    assert added.status_code == 200
    assert [m["subscription_id"] for m in added.json()["subscriptions"]] == [SUB_A]

    # Listing + get expose the membership.
    assert client.get("/api/account-groups").json()[0]["subscription_count"] == 1
    assert client.get(f"/api/account-groups/{gid}").json()["name"] == "prod"

    # Remove the membership → 200 and empty.
    removed = client.delete(f"/api/account-groups/{gid}/subscriptions/{SUB_A}")
    assert removed.status_code == 200
    assert removed.json()["subscription_count"] == 0

    # Delete the group → subscription survives.
    assert client.delete(f"/api/account-groups/{gid}").json()["deleted"] is True
    with session_scope() as s:
        assert repo.get_subscription(s, SUB_A) is not None


def test_api_add_unknown_subscription_404(db, client) -> None:
    gid = client.post("/api/account-groups", json={"name": "prod"}).json()["id"]
    assert client.post(f"/api/account-groups/{gid}/subscriptions/nope").status_code == 404


def test_api_get_unknown_group_404(db, client) -> None:
    assert client.get("/api/account-groups/999999").status_code == 404
    assert client.delete("/api/account-groups/999999").status_code == 404


def test_api_remove_unknown_membership_404(db, client) -> None:
    gid = client.post("/api/account-groups", json={"name": "prod"}).json()["id"]
    # Group exists, but the subscription was never a member → 404.
    assert client.delete(f"/api/account-groups/{gid}/subscriptions/nope").status_code == 404
