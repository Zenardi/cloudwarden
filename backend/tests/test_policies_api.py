"""Policy CRUD API (M2.1): ``GET/POST/PUT/DELETE /api/policies`` + enable toggle.

Written test-first (TDD). DB-backed (the ``db`` fixture, so rows really persist)
with an **injected** ``FakeCustodianRunner`` so validate-on-write is deterministic
and fully offline. The contract under test:

* ``GET /api/policies`` lists policies (``?enabled=true|false`` filters);
* ``GET /api/policies/{id}`` → ``404`` when missing;
* ``POST /api/policies`` validates **before** persisting — ``201`` on success,
  ``422`` (and **no row**) when the spec is invalid, ``409`` on a duplicate name;
* ``PUT /api/policies/{id}`` re-validates a changed ``spec`` (``422``), bumps the
  version, ``404`` when missing, ``409`` on a name collision;
* ``DELETE /api/policies/{id}`` is idempotent (``404`` once already gone);
* ``POST /api/policies/{id}/enabled?enabled=`` toggles the flag (``404`` missing).

Bare ``TestClient(app)`` (no ``with``) so the app lifespan never runs; the ``db``
fixture already created the schema and the endpoints read it via ``session_scope``.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from cloudwarden.api.main import app, get_custodian_runner
from cloudwarden.storage.db import session_scope  # noqa: F401 - imported for parity/debugging

_VALID = {"policies": [{"name": "stopped-vms", "resource": "azure.vm"}]}
_INVALID = {"policies": [{"name": "x", "resource": "azure.not-a-type"}]}


class FakeCustodianRunner:
    """In-memory runner: known resource types validate, others don't. No c7n/Azure."""

    KNOWN = ("azure.vm", "azure.disk", "azure.publicip")

    def __init__(self) -> None:
        self.validate_calls: list[dict] = []

    @staticmethod
    def _resource(spec: dict) -> str:
        return (spec.get("policies") or [{}])[0].get("resource", "")

    def validate(self, spec: dict) -> dict:
        self.validate_calls.append(spec)
        resource = self._resource(spec)
        if resource not in self.KNOWN:
            return {"valid": False, "errors": [f"unknown resource type: {resource}"]}
        return {"valid": True, "errors": []}

    def run(self, spec: dict, subscription_id: str, credential, dry_run: bool) -> dict:
        return {"resources": []}

    def schema(self, resource_type: str | None = None) -> dict:
        return {"resource_types": list(self.KNOWN)}


@pytest.fixture
def fake_runner() -> FakeCustodianRunner:
    return FakeCustodianRunner()


@pytest.fixture
def client(fake_runner: FakeCustodianRunner):
    app.dependency_overrides[get_custodian_runner] = lambda: fake_runner
    yield TestClient(app)
    app.dependency_overrides.clear()


def _body(
    name: str = "stopped-vms", resource_type: str = "azure.vm", spec: dict | None = None, **kw
):
    return {"name": name, "resource_type": resource_type, "spec": spec or _VALID, **kw}


# --------------------------------------------------------------------------- #
# GET /api/policies (+ ?enabled= filter)
# --------------------------------------------------------------------------- #
def test_list_policies_empty_then_populated(db, client) -> None:
    assert client.get("/api/policies").json() == []

    client.post("/api/policies", json=_body())
    listing = client.get("/api/policies").json()

    assert len(listing) == 1
    assert listing[0]["name"] == "stopped-vms"
    assert listing[0]["validation_status"] == "valid"


def test_list_policies_enabled_filter(db, client) -> None:
    client.post("/api/policies", json=_body(name="on"))
    off_id = client.post("/api/policies", json=_body(name="off")).json()["id"]
    client.post(f"/api/policies/{off_id}/enabled", params={"enabled": False})

    all_names = {p["name"] for p in client.get("/api/policies").json()}
    on_names = {p["name"] for p in client.get("/api/policies", params={"enabled": True}).json()}
    off_names = {p["name"] for p in client.get("/api/policies", params={"enabled": False}).json()}

    assert all_names == {"on", "off"}
    assert on_names == {"on"}
    assert off_names == {"off"}


# --------------------------------------------------------------------------- #
# GET /api/policies/{id}
# --------------------------------------------------------------------------- #
def test_get_policy_returns_row(db, client) -> None:
    pid = client.post("/api/policies", json=_body()).json()["id"]

    resp = client.get(f"/api/policies/{pid}")

    assert resp.status_code == 200
    assert resp.json()["id"] == pid


def test_get_policy_not_found_returns_404(db, client) -> None:
    assert client.get("/api/policies/999999").status_code == 404


# --------------------------------------------------------------------------- #
# POST /api/policies (validate-first)
# --------------------------------------------------------------------------- #
def test_create_policy_valid_returns_201_and_persists(db, client, fake_runner) -> None:
    resp = client.post("/api/policies", json=_body(description="deallocated VMs"))

    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "stopped-vms"
    assert body["validation_status"] == "valid"
    assert body["enabled"] is True
    assert body["version"] == 1
    # validated before persisting
    assert fake_runner.validate_calls == [_VALID]
    # and it really persisted
    assert client.get(f"/api/policies/{body['id']}").status_code == 200


def test_create_policy_invalid_returns_422_and_no_row(db, client) -> None:
    resp = client.post(
        "/api/policies",
        json=_body(name="bad", resource_type="azure.not-a-type", spec=_INVALID),
    )

    assert resp.status_code == 422
    assert resp.json()["detail"]["errors"]  # non-empty
    # nothing was persisted
    assert client.get("/api/policies").json() == []


def test_create_policy_duplicate_name_returns_409(db, client) -> None:
    assert client.post("/api/policies", json=_body(name="dup")).status_code == 201

    resp = client.post("/api/policies", json=_body(name="dup"))

    assert resp.status_code == 409
    assert len(client.get("/api/policies").json()) == 1  # only the first


# --------------------------------------------------------------------------- #
# PUT /api/policies/{id} (re-validate on spec change)
# --------------------------------------------------------------------------- #
def test_update_policy_revalidates_and_bumps_version(db, client) -> None:
    pid = client.post("/api/policies", json=_body()).json()["id"]

    # an invalid spec is rejected 422 and does not mutate the row
    bad = client.put(f"/api/policies/{pid}", json={"spec": _INVALID})
    assert bad.status_code == 422
    assert client.get(f"/api/policies/{pid}").json()["version"] == 1

    # a valid change is applied and bumps the version
    good = client.put(
        f"/api/policies/{pid}",
        json={
            "description": "v2",
            "spec": {"policies": [{"name": "stopped-vms", "resource": "azure.disk"}]},
        },
    )
    assert good.status_code == 200
    assert good.json()["version"] == 2
    assert good.json()["description"] == "v2"


def test_update_policy_not_found_returns_404(db, client) -> None:
    assert client.put("/api/policies/999999", json={"description": "x"}).status_code == 404


def test_update_policy_duplicate_name_returns_409(db, client) -> None:
    a_id = client.post("/api/policies", json=_body(name="a")).json()["id"]
    client.post("/api/policies", json=_body(name="b"))

    resp = client.put(f"/api/policies/{a_id}", json={"name": "b"})

    assert resp.status_code == 409


# --------------------------------------------------------------------------- #
# DELETE /api/policies/{id}
# --------------------------------------------------------------------------- #
def test_delete_policy_then_404(db, client) -> None:
    pid = client.post("/api/policies", json=_body()).json()["id"]

    assert client.delete(f"/api/policies/{pid}").status_code == 200
    assert client.delete(f"/api/policies/{pid}").status_code == 404
    assert client.get(f"/api/policies/{pid}").status_code == 404


# --------------------------------------------------------------------------- #
# POST /api/policies/{id}/enabled
# --------------------------------------------------------------------------- #
def test_set_enabled_toggle(db, client) -> None:
    pid = client.post("/api/policies", json=_body()).json()["id"]

    off = client.post(f"/api/policies/{pid}/enabled", params={"enabled": False})
    assert off.status_code == 200
    assert off.json()["enabled"] is False

    on = client.post(f"/api/policies/{pid}/enabled", params={"enabled": True})
    assert on.json()["enabled"] is True


def test_set_enabled_not_found_returns_404(db, client) -> None:
    assert client.post("/api/policies/999999/enabled", params={"enabled": False}).status_code == 404
