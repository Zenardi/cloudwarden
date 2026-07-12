"""Policy validation + Custodian schema endpoints (M1.3): API contract, TDD-first.

Exercises ``POST /api/policies/validate`` and ``GET /api/custodian/schema`` through
the FastAPI ``TestClient`` with an **injected** ``FakeCustodianRunner`` (via the
``get_custodian_runner`` dependency override) — no c7n, no Azure, no DB. The
contract under test:

* validate: a well-formed spec returns ``200`` with ``{valid, errors}`` (valid
  ``True`` for a known resource, ``False`` with a non-empty ``errors`` for an
  unknown one); a malformed request body returns ``400``; the endpoint never
  raises (a runner blow-up degrades to ``400``, never ``500``).
* schema: listing (no arg) and a known resource return ``200``; an unknown
  resource returns ``400``; a runner blow-up degrades to ``400``.

Bare ``TestClient(app)`` (no ``with``) is used deliberately so the app lifespan —
and therefore ``init_db()`` — never runs: these endpoints touch neither the DB
nor the network.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from azure_finops.api.main import app, get_custodian_runner


# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #
class FakeCustodianRunner:
    """In-memory ``CustodianRunner`` double. Never imports c7n or Azure."""

    KNOWN_TYPES = ("azure.vm", "azure.disk", "azure.publicip")

    def __init__(self) -> None:
        self.validate_calls: list[dict] = []
        self.schema_calls: list[str | None] = []

    @staticmethod
    def _resource(spec: dict) -> str:
        return (spec.get("policies") or [{}])[0].get("resource", "")

    def validate(self, spec: dict) -> dict:
        self.validate_calls.append(spec)
        resource = self._resource(spec)
        if resource not in self.KNOWN_TYPES:
            return {"valid": False, "errors": [f"unknown resource type: {resource}"]}
        return {"valid": True, "errors": []}

    def run(self, spec: dict, subscription_id: str, credential, dry_run: bool) -> dict:
        return {"resources": []}

    def schema(self, resource_type: str | None = None) -> dict:
        self.schema_calls.append(resource_type)
        if resource_type is None:
            return {"resource_types": list(self.KNOWN_TYPES)}
        if resource_type not in self.KNOWN_TYPES:
            return {
                "error": f"unknown resource type: {resource_type}",
                "resource_type": resource_type,
            }
        return {"resource_type": resource_type, "filters": ["instance-view"], "actions": ["stop"]}


class RaisingCustodianRunner:
    """A runner whose validate/schema blow up — used to prove the API never 500s."""

    def validate(self, spec: dict) -> dict:
        raise RuntimeError("c7n exploded")

    def run(self, spec: dict, subscription_id: str, credential, dry_run: bool) -> dict:
        raise RuntimeError("c7n exploded")

    def schema(self, resource_type: str | None = None) -> dict:
        raise RuntimeError("c7n exploded")


def _vm_spec(name: str = "stopped-vms", resource: str = "azure.vm") -> dict:
    return {"policies": [{"name": name, "resource": resource}]}


@pytest.fixture
def fake_runner() -> FakeCustodianRunner:
    return FakeCustodianRunner()


@pytest.fixture
def client(fake_runner: FakeCustodianRunner):
    """A TestClient with the Custodian runner overridden to the in-memory fake."""
    app.dependency_overrides[get_custodian_runner] = lambda: fake_runner
    yield TestClient(app)
    app.dependency_overrides.clear()


def _client_with(runner):
    app.dependency_overrides[get_custodian_runner] = lambda: runner
    return TestClient(app)


# --------------------------------------------------------------------------- #
# POST /api/policies/validate
# --------------------------------------------------------------------------- #
def test_validate_accepts_valid_policy(client, fake_runner: FakeCustodianRunner) -> None:
    resp = client.post("/api/policies/validate", json={"spec": _vm_spec()})

    assert resp.status_code == 200
    assert resp.json() == {"valid": True, "errors": []}
    assert fake_runner.validate_calls == [_vm_spec()]


def test_validate_rejects_invalid_policy_with_errors(client) -> None:
    # A well-formed request whose policy references an unknown resource type:
    # validation *ran* and reported the policy invalid -> 200, valid=False.
    resp = client.post(
        "/api/policies/validate", json={"spec": _vm_spec(resource="azure.not-a-type")}
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["valid"] is False
    assert body["errors"]  # non-empty


def test_validate_rejects_malformed_body_with_400(client) -> None:
    # A spec with no `policies` list is not a usable policy collection -> 400.
    resp = client.post("/api/policies/validate", json={"spec": {"not": "a-policy"}})

    assert resp.status_code == 400


def test_validate_empty_spec_returns_400(client) -> None:
    resp = client.post("/api/policies/validate", json={"spec": {}})

    assert resp.status_code == 400


def test_validate_never_raises_returns_400_on_runner_error() -> None:
    client = _client_with(RaisingCustodianRunner())
    try:
        resp = client.post("/api/policies/validate", json={"spec": _vm_spec()})
        assert resp.status_code == 400  # degraded, not a 500
    finally:
        app.dependency_overrides.clear()


# --------------------------------------------------------------------------- #
# GET /api/custodian/schema
# --------------------------------------------------------------------------- #
def test_schema_lists_resource_types(client, fake_runner: FakeCustodianRunner) -> None:
    resp = client.get("/api/custodian/schema")

    assert resp.status_code == 200
    assert "azure.vm" in resp.json()["resource_types"]
    assert fake_runner.schema_calls == [None]


def test_schema_for_known_resource_type(client) -> None:
    resp = client.get("/api/custodian/schema", params={"resource_type": "azure.vm"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["resource_type"] == "azure.vm"
    assert "error" not in body


def test_schema_unknown_resource_type_returns_400(client) -> None:
    resp = client.get("/api/custodian/schema", params={"resource_type": "azure.bogus"})

    assert resp.status_code == 400


def test_schema_never_raises_returns_400_on_runner_error() -> None:
    client = _client_with(RaisingCustodianRunner())
    try:
        resp = client.get("/api/custodian/schema")
        assert resp.status_code == 400  # degraded, not a 500
    finally:
        app.dependency_overrides.clear()


# --------------------------------------------------------------------------- #
# Injection seam
# --------------------------------------------------------------------------- #
def test_get_custodian_runner_default_is_none() -> None:
    # Un-overridden, the seam yields None so the engine uses its cached
    # LiveCustodianRunner. Endpoints pass this straight through.
    assert get_custodian_runner() is None
