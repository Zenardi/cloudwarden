"""Policy dry-run endpoint (M1.4): ``POST /api/policies/{id}/dryrun``, TDD-first.

DB-backed (the ``db`` fixture, so a real ``policies`` row exists to dry-run) but the
Cloud Custodian engine is an **injected** ``FakeCustodianRunner`` that returns the
recorded ``custodian_policy_result.json`` fixture — no c7n, no Azure. The contract:

* a valid policy id returns ``200`` with the matched resources and ``dry_run: true``;
* an unknown policy id returns ``404``; an unknown ``subscription_id`` returns ``404``;
* the dry-run wires ``dry_run=True`` into the engine and **never** invokes the
  remediation action executor (asserted with a spy).

Bare ``TestClient(app)`` (no ``with``) is used so the app lifespan never runs — the
``db`` fixture already initialised the schema, and the endpoint reads it via
``session_scope()``.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from azure_finops.api.main import app, get_custodian_runner
from azure_finops.azure._fixtures import load_fixture
from azure_finops.storage import repository as repo
from azure_finops.storage.db import session_scope

_SPEC = {"policies": [{"name": "stopped-vms", "resource": "azure.vm"}]}
SUB_A = "11111111-1111-1111-1111-111111111111"


class FakeCustodianRunner:
    """Returns the recorded dry-run fixture and records ``run`` args. No c7n/Azure."""

    def __init__(self) -> None:
        self.run_calls: list[dict] = []

    def validate(self, spec: dict) -> dict:
        return {"valid": True, "errors": []}

    def run(self, spec: dict, subscription_id: str, credential, dry_run: bool) -> dict:
        self.run_calls.append(
            {
                "spec": spec,
                "subscription_id": subscription_id,
                "credential": credential,
                "dry_run": dry_run,
            }
        )
        return load_fixture("custodian_policy_result")

    def schema(self, resource_type: str | None = None) -> dict:
        return {"resource_types": []}


@pytest.fixture
def fake_runner() -> FakeCustodianRunner:
    return FakeCustodianRunner()


@pytest.fixture
def client(fake_runner: FakeCustodianRunner):
    app.dependency_overrides[get_custodian_runner] = lambda: fake_runner
    yield TestClient(app)
    app.dependency_overrides.clear()


def _make_policy(name: str = "stopped-vms") -> int:
    with session_scope() as s:
        return repo.create_policy(s, name=name, resource_type="azure.vm", spec=_SPEC)["id"]


# --------------------------------------------------------------------------- #
# Happy path — matched resources
# --------------------------------------------------------------------------- #
def test_dryrun_returns_matched_resources(db, client, fake_runner: FakeCustodianRunner) -> None:
    pid = _make_policy()

    resp = client.post(f"/api/policies/{pid}/dryrun")

    assert resp.status_code == 200
    body = resp.json()
    assert body["policy_id"] == pid
    assert body["policy_name"] == "stopped-vms"
    assert body["dry_run"] is True
    assert body["matched"] == 2
    assert len(body["resources"]) == 2
    # The stored spec was evaluated with dry_run=True.
    assert fake_runner.run_calls and fake_runner.run_calls[0]["dry_run"] is True
    assert fake_runner.run_calls[0]["spec"] == _SPEC


def test_dryrun_with_explicit_subscription(db, client, fake_runner: FakeCustodianRunner) -> None:
    pid = _make_policy()
    with session_scope() as s:
        repo.upsert_subscription(s, subscription_id=SUB_A, display_name="A")

    resp = client.post(f"/api/policies/{pid}/dryrun", params={"subscription_id": SUB_A})

    assert resp.status_code == 200
    assert resp.json()["subscription_id"] == SUB_A
    assert fake_runner.run_calls[0]["subscription_id"] == SUB_A


# --------------------------------------------------------------------------- #
# Negative paths — 404s
# --------------------------------------------------------------------------- #
def test_dryrun_unknown_policy_returns_404(db, client) -> None:
    resp = client.post("/api/policies/999999/dryrun")

    assert resp.status_code == 404


def test_dryrun_unknown_subscription_returns_404(db, client) -> None:
    pid = _make_policy()

    resp = client.post(f"/api/policies/{pid}/dryrun", params={"subscription_id": "no-such-sub"})

    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# Guard — a dry-run must never execute an action
# --------------------------------------------------------------------------- #
def test_dryrun_does_not_execute_any_action(db, client, monkeypatch) -> None:
    from azure_finops.remediation import executor

    calls: list = []
    monkeypatch.setattr(executor, "execute", lambda *a, **k: calls.append((a, k)))

    pid = _make_policy()
    resp = client.post(f"/api/policies/{pid}/dryrun")

    assert resp.status_code == 200
    assert calls == []  # dry-run never invokes the remediation executor
