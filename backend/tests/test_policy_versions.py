"""Policy version history & diff (M2.5): immutable per-update snapshots + diff.

Written test-first (TDD). Two layers, mirroring the existing policy tests:

* **Repository** (DB-backed via the ``db`` fixture): ``create_policy`` seeds a
  version-1 snapshot; ``update_policy`` snapshots the **new** state and bumps the
  number only when a tracked field (name/resource_type/spec/description) actually
  changes — a no-op update writes no version. ``list_versions`` returns snapshots
  newest-first (``None`` for an unknown policy). ``diff_versions`` is a pure
  field-level diff; ``diff_policy_versions`` loads two stored snapshots and diffs
  them (``None`` when the policy or either version is missing).
* **API** (``TestClient`` + injected ``FakeCustodianRunner``, fully offline):
  ``GET /api/policies/{id}/versions`` (newest-first, ``404`` when missing) and
  ``GET /api/policies/{id}/versions/diff`` (``404`` for an unknown policy/version).

The pure ``diff_versions`` tests need no database and always run.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from azure_finops.api.main import app, get_custodian_runner
from azure_finops.storage import repository as repo
from azure_finops.storage.db import session_scope

# Distinct (parsed) Custodian policy bodies for the three revisions under test.
_V1 = {"policies": [{"name": "stopped-vms", "resource": "azure.vm"}]}
_V2 = {
    "policies": [{"name": "stopped-vms", "resource": "azure.vm", "filters": [{"tag:env": "dev"}]}]
}
_V3 = {"policies": [{"name": "stopped-vms", "resource": "azure.disk"}]}


def _new(session, *, name="p", resource_type="azure.vm", spec=None, **kw):
    return repo.create_policy(
        session, name=name, resource_type=resource_type, spec=spec or _V1, **kw
    )


# --------------------------------------------------------------------------- #
# Pure diff util (no database)
# --------------------------------------------------------------------------- #
def test_diff_reports_changed_fields() -> None:
    old = {"name": "a", "resource_type": "azure.vm", "spec": {"x": 1}, "description": "d"}
    new = {"name": "a", "resource_type": "azure.disk", "spec": {"x": 2}, "description": "d"}

    diff = repo.diff_versions(old, new)

    assert diff["changed_fields"] == ["resource_type", "spec"]
    assert diff["changes"]["resource_type"] == {"old": "azure.vm", "new": "azure.disk"}
    assert diff["changes"]["spec"] == {"old": {"x": 1}, "new": {"x": 2}}
    assert "name" not in diff["changes"]


def test_diff_versions_identical_is_empty() -> None:
    snap = {"name": "a", "resource_type": "azure.vm", "spec": {}, "description": None}

    diff = repo.diff_versions(snap, snap)

    assert diff["changed_fields"] == []
    assert diff["changes"] == {}


# --------------------------------------------------------------------------- #
# Repository: snapshots on create + update
# --------------------------------------------------------------------------- #
def test_create_seeds_version_one(db) -> None:
    with session_scope() as s:
        pid = _new(s, description="initial")["id"]

    with session_scope() as s:
        versions = repo.list_versions(s, pid)
    assert len(versions) == 1
    assert versions[0]["version"] == 1
    assert versions[0]["spec"] == _V1
    assert versions[0]["description"] == "initial"
    assert versions[0]["created_at"]


def test_update_creates_version(db) -> None:
    with session_scope() as s:
        pid = _new(s)["id"]

    with session_scope() as s:
        repo.update_policy(s, pid, spec=_V2)

    with session_scope() as s:
        versions = repo.list_versions(s, pid)
    # create seeded v1, one spec-changing update added exactly one more (v2).
    assert len(versions) == 2
    assert versions[0]["version"] == 2
    assert versions[0]["spec"] == _V2


def test_version_numbers_monotonic(db) -> None:
    with session_scope() as s:
        pid = _new(s)["id"]
    for spec in (_V2, _V3):
        with session_scope() as s:
            repo.update_policy(s, pid, spec=spec)

    with session_scope() as s:
        numbers = [v["version"] for v in repo.list_versions(s, pid)]
    assert numbers == [3, 2, 1]


def test_noop_update_creates_no_version(db) -> None:
    with session_scope() as s:
        pid = _new(s, description="d")["id"]

    # Re-submitting the identical values changes nothing.
    with session_scope() as s:
        result = repo.update_policy(
            s, pid, name="p", resource_type="azure.vm", spec=_V1, description="d"
        )
    assert result["version"] == 1

    with session_scope() as s:
        assert len(repo.list_versions(s, pid)) == 1


def test_empty_update_creates_no_version(db) -> None:
    with session_scope() as s:
        pid = _new(s)["id"]

    with session_scope() as s:
        assert repo.update_policy(s, pid)["version"] == 1

    with session_scope() as s:
        assert len(repo.list_versions(s, pid)) == 1


def test_versions_listed_newest_first(db) -> None:
    with session_scope() as s:
        pid = _new(s)["id"]
    with session_scope() as s:
        repo.update_policy(s, pid, spec=_V2)
    with session_scope() as s:
        repo.update_policy(s, pid, description="renamed desc")

    with session_scope() as s:
        versions = repo.list_versions(s, pid)
    assert [v["version"] for v in versions] == [3, 2, 1]
    # The newest snapshot carries the latest description AND the spec from v2.
    assert versions[0]["description"] == "renamed desc"
    assert versions[0]["spec"] == _V2


def test_update_snapshot_records_actor(db) -> None:
    with session_scope() as s:
        pid = _new(s)["id"]
    with session_scope() as s:
        repo.update_policy(s, pid, spec=_V2, actor="alice")

    with session_scope() as s:
        assert repo.list_versions(s, pid)[0]["actor"] == "alice"


def test_list_versions_unknown_policy_returns_none(db) -> None:
    with session_scope() as s:
        assert repo.list_versions(s, 9_999_999) is None


# --------------------------------------------------------------------------- #
# Repository: diff between two stored versions
# --------------------------------------------------------------------------- #
def test_diff_policy_versions_via_repo(db) -> None:
    with session_scope() as s:
        pid = _new(s)["id"]
    with session_scope() as s:
        repo.update_policy(s, pid, resource_type="azure.disk", spec=_V3)

    with session_scope() as s:
        diff = repo.diff_policy_versions(s, pid, 1, 2)
    assert set(diff["changed_fields"]) == {"resource_type", "spec"}


def test_diff_policy_versions_unknown_returns_none(db) -> None:
    with session_scope() as s:
        pid = _new(s)["id"]

    with session_scope() as s:
        assert repo.diff_policy_versions(s, 9_999_999, 1, 1) is None  # policy missing
        assert repo.diff_policy_versions(s, pid, 1, 99) is None  # version missing


# --------------------------------------------------------------------------- #
# API: GET /api/policies/{id}/versions (+ /diff)
# --------------------------------------------------------------------------- #
_VALID = {"policies": [{"name": "stopped-vms", "resource": "azure.vm"}]}
_SPEC2 = {"policies": [{"name": "stopped-vms", "resource": "azure.disk"}]}


class FakeCustodianRunner:
    """In-memory runner: known resource types validate, others don't. No c7n/Azure."""

    KNOWN = ("azure.vm", "azure.disk", "azure.publicip")

    @staticmethod
    def _resource(spec: dict) -> str:
        return (spec.get("policies") or [{}])[0].get("resource", "")

    def validate(self, spec: dict) -> dict:
        resource = self._resource(spec)
        if resource not in self.KNOWN:
            return {"valid": False, "errors": [f"unknown resource type: {resource}"]}
        return {"valid": True, "errors": []}

    def run(self, spec: dict, subscription_id: str, credential, dry_run: bool) -> dict:
        return {"resources": []}

    def schema(self, resource_type: str | None = None) -> dict:
        return {"resource_types": list(self.KNOWN)}


@pytest.fixture
def client():
    app.dependency_overrides[get_custodian_runner] = FakeCustodianRunner
    yield TestClient(app)
    app.dependency_overrides.clear()


def _body(name="stopped-vms", resource_type="azure.vm", spec=None, **kw):
    return {"name": name, "resource_type": resource_type, "spec": spec or _VALID, **kw}


def test_api_list_versions_newest_first(db, client) -> None:
    pid = client.post("/api/policies", json=_body()).json()["id"]
    client.put(f"/api/policies/{pid}", json={"spec": _SPEC2})

    resp = client.get(f"/api/policies/{pid}/versions")

    assert resp.status_code == 200
    assert [v["version"] for v in resp.json()] == [2, 1]


def test_api_list_versions_unknown_policy_404(db, client) -> None:
    assert client.get("/api/policies/999999/versions").status_code == 404


def test_api_diff_versions_reports_changes(db, client) -> None:
    pid = client.post("/api/policies", json=_body()).json()["id"]
    client.put(f"/api/policies/{pid}", json={"resource_type": "azure.disk", "spec": _SPEC2})

    resp = client.get(
        f"/api/policies/{pid}/versions/diff", params={"from_version": 1, "to_version": 2}
    )

    assert resp.status_code == 200
    assert set(resp.json()["changed_fields"]) == {"resource_type", "spec"}


def test_api_diff_unknown_policy_404(db, client) -> None:
    resp = client.get(
        "/api/policies/999999/versions/diff", params={"from_version": 1, "to_version": 2}
    )
    assert resp.status_code == 404


def test_api_diff_unknown_version_404(db, client) -> None:
    pid = client.post("/api/policies", json=_body()).json()["id"]

    resp = client.get(
        f"/api/policies/{pid}/versions/diff", params={"from_version": 1, "to_version": 99}
    )
    assert resp.status_code == 404
