"""Event-mode policy trigger (M6.2) — reactive, real-time enforcement.

Written test-first (TDD). DB-backed (the ``db`` fixture) with an **injected**
``FakeCustodianRunner`` so no c7n/Azure call is ever made. This is Cloud
Custodian's *event mode*: a normalized Event Grid delivery (M6.1) selects only the
policies whose ``resource_type`` matches **and** that declare an event-grid ``mode``,
then runs exactly those — each recorded as a ``PolicyExecution`` with ``mode='event'``.

Invariants under test (Arrange–Act–Assert):

* an event whose type matches an event-mode policy triggers **exactly** that policy;
* an event with no matching policy triggers **zero** executions;
* executions created by events are recorded with **``mode='event'``**;
* an event for an **unknown** resource type is a **safe no-op** (not an error);
* **all** matching event-mode policies fire (not just the first);
* **pull-mode** and **disabled** policies are never triggered reactively;
* a policy run that raises is isolated and recorded ``failed`` (webhook stays healthy).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from azure_finops.api.main import app, get_custodian_runner
from azure_finops.azure._fixtures import load_fixture
from azure_finops.custodian.eventmode import handle_event
from azure_finops.events.models import NormalizedEvent
from azure_finops.storage import repository as repo
from azure_finops.storage.db import session_scope

# An event-mode c7n policy declares a ``mode`` block; a pull policy has none.
_EVENT_SPEC = {
    "policies": [
        {
            "name": "vm-guard",
            "resource": "azure.vm",
            "mode": {
                "type": "azure-event-grid",
                "events": ["Microsoft.Compute/virtualMachines/write"],
            },
            "actions": ["stop"],
        }
    ]
}
_PULL_SPEC = {"policies": [{"name": "vm-pull", "resource": "azure.vm", "actions": ["stop"]}]}


class FakeCustodianRunner:
    """Records ``run`` args and returns 1 match. No c7n / Azure."""

    def __init__(self) -> None:
        self.run_calls: list[dict] = []

    def validate(self, spec: dict) -> dict:
        return {"valid": True, "errors": []}

    def run(self, spec: dict, subscription_id: str, credential, dry_run: bool) -> dict:
        self.run_calls.append(
            {
                "subscription_id": subscription_id,
                "dry_run": dry_run,
                "policy": (spec.get("policies") or [{}])[0].get("name"),
            }
        )
        return {
            "resources": [{"id": f"/subscriptions/{subscription_id}/vm-1", "type": "azure.vm"}],
            "matched": 1,
            "dry_run": dry_run,
        }

    def schema(self, resource_type: str | None = None) -> dict:
        return {"resource_types": []}


class _RaisingRunner(FakeCustodianRunner):
    def run(self, spec: dict, subscription_id: str, credential, dry_run: bool) -> dict:
        raise RuntimeError("boom")


def _make_event(
    *,
    resource_type: str | None = "microsoft.compute/virtualmachines",
    subscription_id: str = "sub-1",
    event_id: str = "evt-1",
) -> NormalizedEvent:
    return NormalizedEvent(
        event_id=event_id,
        event_type="Microsoft.Resources.ResourceWriteSuccess",
        subject=f"/subscriptions/{subscription_id}/rg/providers/{resource_type}/vm-web-01",
        resource_id=(
            f"/subscriptions/{subscription_id}/resourcegroups/rg/providers/"
            f"{resource_type}/vm-web-01"
        ),
        subscription_id=subscription_id,
        resource_type=resource_type,
        operation_name="Microsoft.Compute/virtualMachines/write",
        status="Succeeded",
    )


def _create_policy(
    *, name: str, resource_type: str = "azure.vm", spec: dict | None = None, enabled: bool = True
) -> int:
    with session_scope() as s:
        pid = repo.create_policy(
            s, name=name, resource_type=resource_type, spec=spec or _EVENT_SPEC
        )["id"]
        if not enabled:
            repo.set_policy_enabled(s, pid, False)
        return pid


def _executions() -> list[dict]:
    with session_scope() as s:
        return repo._rows(
            s, "SELECT policy_id, subscription_id, status, mode FROM policy_executions"
        )


# --------------------------------------------------------------------------- #
# handle_event — selection & execution
# --------------------------------------------------------------------------- #
def test_event_triggers_matching_policy(db) -> None:
    pid = _create_policy(name="vm-guard")
    runner = FakeCustodianRunner()

    result = handle_event(_make_event(), runner=runner)

    assert result["matched"] == 1
    assert len(runner.run_calls) == 1
    rows = _executions()
    assert len(rows) == 1
    assert rows[0]["policy_id"] == pid
    assert rows[0]["status"] == "succeeded"


def test_event_no_match_no_execution(db) -> None:
    _create_policy(name="disk-guard", resource_type="azure.disk")  # event-mode but wrong type
    runner = FakeCustodianRunner()

    result = handle_event(_make_event(), runner=runner)  # a VM event

    assert result["matched"] == 0
    assert runner.run_calls == []
    assert _executions() == []


def test_event_records_execution_mode_event(db) -> None:
    _create_policy(name="vm-guard")

    handle_event(_make_event(), runner=FakeCustodianRunner())

    rows = _executions()
    assert len(rows) == 1
    assert rows[0]["mode"] == "event"


def test_event_unknown_type_is_noop(db) -> None:
    _create_policy(name="vm-guard")
    runner = FakeCustodianRunner()

    # An unknown ARM type (and a type-less event) select nothing and never error.
    assert (
        handle_event(_make_event(resource_type="microsoft.unknown/widgets"), runner=runner)[
            "matched"
        ]
        == 0
    )
    assert handle_event(_make_event(resource_type=None), runner=runner)["matched"] == 0
    assert runner.run_calls == []
    assert _executions() == []


def test_event_triggers_all_matching_policies(db) -> None:
    _create_policy(name="vm-guard-a")
    _create_policy(name="vm-guard-b")
    runner = FakeCustodianRunner()

    result = handle_event(_make_event(), runner=runner)

    assert result["matched"] == 2
    assert len(runner.run_calls) == 2
    assert len(_executions()) == 2


# --------------------------------------------------------------------------- #
# handle_event — negative gating (never over-trigger)
# --------------------------------------------------------------------------- #
def test_event_ignores_pull_mode_policy(db) -> None:
    _create_policy(name="vm-pull", spec=_PULL_SPEC)  # no ``mode`` block → not event-mode
    runner = FakeCustodianRunner()

    assert handle_event(_make_event(), runner=runner)["matched"] == 0
    assert _executions() == []


def test_event_ignores_disabled_policy(db) -> None:
    _create_policy(name="vm-guard", enabled=False)
    runner = FakeCustodianRunner()

    assert handle_event(_make_event(), runner=runner)["matched"] == 0
    assert _executions() == []


def test_event_matches_policy_authored_with_arm_type(db) -> None:
    # A policy may store the ARM type directly instead of the c7n short name.
    _create_policy(name="vm-guard", resource_type="microsoft.compute/virtualmachines")

    assert handle_event(_make_event(), runner=FakeCustodianRunner())["matched"] == 1


def test_handle_event_none_is_noop(db) -> None:
    assert handle_event(None, runner=FakeCustodianRunner())["matched"] == 0
    assert _executions() == []


def test_run_reactive_records_failure(db) -> None:
    _create_policy(name="vm-guard")

    result = handle_event(_make_event(), runner=_RaisingRunner())

    assert result["matched"] == 1
    assert result["executions"][0]["status"] == "failed"
    assert "boom" in result["executions"][0]["error"]
    rows = _executions()
    assert rows[0]["status"] == "failed" and rows[0]["mode"] == "event"


# --------------------------------------------------------------------------- #
# Pure helpers (offline)
# --------------------------------------------------------------------------- #
def test_resource_type_matches_variants() -> None:
    from azure_finops.custodian.eventmode import _resource_type_matches

    arm = "microsoft.compute/virtualmachines"
    assert _resource_type_matches("azure.vm", arm) is True  # c7n short name
    assert _resource_type_matches("AZURE.VM", arm) is True  # case-insensitive
    assert _resource_type_matches(arm, arm) is True  # ARM authored directly
    assert _resource_type_matches("azure.disk", arm) is False  # different type
    assert _resource_type_matches("", arm) is False
    assert _resource_type_matches("azure.vm", None) is False


def test_is_event_mode_variants() -> None:
    from azure_finops.custodian.eventmode import _is_event_mode

    assert _is_event_mode(_EVENT_SPEC) is True
    assert _is_event_mode(_PULL_SPEC) is False  # no mode block
    assert _is_event_mode({"policies": []}) is False  # no policies
    assert _is_event_mode({"policies": [{"mode": {"type": "azure-periodic"}}]}) is False


# --------------------------------------------------------------------------- #
# Endpoint wiring — POST /api/events/azure triggers event-mode policies
# --------------------------------------------------------------------------- #
@pytest.fixture
def client_with_runner():
    runner = FakeCustodianRunner()
    app.dependency_overrides[get_custodian_runner] = lambda: runner
    yield TestClient(app), runner
    app.dependency_overrides.clear()


def test_ingestion_endpoint_triggers_event_mode_policy(db, client_with_runner) -> None:
    client, runner = client_with_runner
    _create_policy(name="vm-guard")  # the write fixture is a VM resource

    resp = client.post("/api/events/azure", json=load_fixture("events/resource_write_success"))

    assert resp.status_code == 200
    assert resp.json()["processed"] == 1
    assert len(runner.run_calls) == 1  # the endpoint drove the reactive run
    rows = _executions()
    assert len(rows) == 1 and rows[0]["mode"] == "event"


def test_ingestion_endpoint_no_policies_is_plain_ingest(db, client_with_runner) -> None:
    client, runner = client_with_runner

    resp = client.post("/api/events/azure", json=load_fixture("events/resource_write_success"))

    assert resp.json() == {"received": 1, "processed": 1}
    assert runner.run_calls == []
    assert _executions() == []
