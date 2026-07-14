"""Policy domain model & storage (M1.2): DB-backed repository CRUD + enable toggle.

Written test-first (TDD): these exercise the `create_policy` / `get_policy` /
`list_policies` / `update_policy` / `delete_policy` / `set_policy_enabled` seam and
its negative cases (duplicate name, missing id) against the `db` fixture (a
throwaway Postgres via testcontainers). Skips if Docker is unavailable.
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from cloudwarden.storage import repository as repo
from cloudwarden.storage.db import session_scope

# A realistic (parsed) Cloud Custodian policy body for `azure.vm`.
_SPEC = {
    "name": "stopped-vms",
    "resource": "azure.vm",
    "filters": [
        {
            "type": "instance-view",
            "key": "statuses[].code",
            "op": "in",
            "value": "PowerState/deallocated",
        }
    ],
}


def _create(session, *, name: str = "stopped-vms", resource_type: str = "azure.vm", **kw):
    return repo.create_policy(session, name=name, resource_type=resource_type, spec=_SPEC, **kw)


# --------------------------------------------------------------------------- #
# Positive scenarios
# --------------------------------------------------------------------------- #
def test_create_policy_persists_spec_and_defaults(db) -> None:
    with session_scope() as s:
        pol = _create(s, description="deallocated VMs")

    assert pol["id"] > 0
    assert pol["name"] == "stopped-vms"
    assert pol["resource_type"] == "azure.vm"
    assert pol["spec"] == _SPEC
    assert pol["description"] == "deallocated VMs"
    assert pol["enabled"] is True
    assert pol["version"] == 1
    assert pol["source"] == "custom"
    assert pol["created_at"] and pol["updated_at"]

    # Round-trips through a fresh session with spec + defaults intact.
    with session_scope() as s:
        got = repo.get_policy(s, pol["id"])
    assert got is not None
    assert got["spec"] == _SPEC
    assert got["version"] == 1
    assert got["enabled"] is True


def test_list_policies_filters_enabled_only(db) -> None:
    with session_scope() as s:
        on = _create(s, name="enabled-pol")
        off = _create(s, name="disabled-pol")
        repo.set_policy_enabled(s, off["id"], False)

    with session_scope() as s:
        all_names = {p["name"] for p in repo.list_policies(s)}
        enabled_names = {p["name"] for p in repo.list_policies(s, enabled_only=True)}

    assert all_names == {"enabled-pol", "disabled-pol"}
    assert enabled_names == {"enabled-pol"}
    # Both were created enabled; only `off` was toggled off afterwards.
    assert on["enabled"] is True and off["enabled"] is True


def test_update_policy_increments_version(db) -> None:
    with session_scope() as s:
        pol = _create(s)
        pid = pol["id"]

    with session_scope() as s:
        first = repo.update_policy(s, pid, description="v2", spec={"resource": "azure.disk"})
    assert first is not None
    assert first["version"] == 2

    with session_scope() as s:
        second = repo.update_policy(s, pid, name="renamed", resource_type="azure.disk")
    assert second is not None
    assert second["version"] == 3
    assert second["name"] == "renamed"
    assert second["resource_type"] == "azure.disk"

    with session_scope() as s:
        got = repo.get_policy(s, pid)
    assert got["version"] == 3
    assert got["description"] == "v2"
    assert got["spec"] == {"resource": "azure.disk"}


def test_delete_policy_removes_row(db) -> None:
    with session_scope() as s:
        pid = _create(s)["id"]

    with session_scope() as s:
        assert repo.delete_policy(s, pid) is True
        # A second delete of the now-missing row is a no-op.
        assert repo.delete_policy(s, pid) is False

    with session_scope() as s:
        assert repo.get_policy(s, pid) is None


def test_set_policy_enabled_toggles_flag(db) -> None:
    with session_scope() as s:
        pid = _create(s)["id"]

    with session_scope() as s:
        disabled = repo.set_policy_enabled(s, pid, False)
    assert disabled is not None and disabled["enabled"] is False

    with session_scope() as s:
        enabled = repo.set_policy_enabled(s, pid, True)
    assert enabled is not None and enabled["enabled"] is True

    with session_scope() as s:
        assert repo.get_policy(s, pid)["enabled"] is True


# --------------------------------------------------------------------------- #
# Negative scenarios
# --------------------------------------------------------------------------- #
def test_create_policy_duplicate_name_raises(db) -> None:
    with session_scope() as s:
        _create(s, name="unique-pol")

    # The unique constraint on `name` rejects the second insert; the failed
    # transaction rolls back, leaving no partial row.
    with pytest.raises(IntegrityError):
        with session_scope() as s:
            _create(s, name="unique-pol")

    with session_scope() as s:
        rows = [p for p in repo.list_policies(s) if p["name"] == "unique-pol"]
    assert len(rows) == 1


def test_get_policy_returns_none_for_missing_id(db) -> None:
    with session_scope() as s:
        assert repo.get_policy(s, 9_999_999) is None


def test_update_policy_missing_id_returns_none(db) -> None:
    with session_scope() as s:
        pid = _create(s)["id"]

    with session_scope() as s:
        assert repo.update_policy(s, 9_999_999, description="nope") is None

    # The real policy is untouched: still version 1 with no description.
    with session_scope() as s:
        got = repo.get_policy(s, pid)
    assert got["version"] == 1
    assert got["description"] is None


def test_set_policy_enabled_missing_id(db) -> None:
    with session_scope() as s:
        assert repo.set_policy_enabled(s, 9_999_999, False) is None
