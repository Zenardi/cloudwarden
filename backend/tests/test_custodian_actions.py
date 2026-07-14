"""Custodian action executor (M7.1) — remediation actions → Azure SDK calls.

Written test-first (TDD). Fully offline: every Azure SDK client is a spy injected
through ``executor.ActionClients`` (or, for the default-client path, the SDK
constructors are monkeypatched). No live Azure, no c7n, no network, no DB.

Invariants under test (Arrange–Act–Assert):

* ``tag`` / ``mark-for-op`` call the Azure tag API with the resource id + payload;
* ``stop`` deallocates a VM, ``delete`` deletes a VM/disk — **only** when not dry-run;
* ``dry_run=True`` performs **zero** client calls (asserted via a shared spy log);
* an unknown/unsupported action type returns a **structured error**, never a crash;
* :func:`engine.resolve_actions` surfaces a policy's actions, normalized.
"""

from __future__ import annotations

import azure.mgmt.compute
import azure.mgmt.resource
import pytest

from cloudwarden.config import Settings
from cloudwarden.custodian import engine
from cloudwarden.remediation import executor

# --------------------------------------------------------------------------- #
# Test doubles — spy SDK clients sharing one call log
# --------------------------------------------------------------------------- #
VM = {
    "id": "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm-1",
    "type": "Microsoft.Compute/virtualMachines",
    "name": "vm-1",
}
DISK = {
    "id": "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Compute/disks/d-1",
    "type": "Microsoft.Compute/disks",
    "name": "d-1",
}
BLOB = {
    "id": "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Storage/storageAccounts/sa",
    "type": "Microsoft.Storage/storageAccounts",
    "name": "sa",
}


class _Poller:
    def result(self):
        return None


class _SpyVMs:
    def __init__(self, log):
        self._log = log

    def begin_deallocate(self, rg, name):
        self._log.append(("vm.deallocate", rg, name))
        return _Poller()

    def begin_delete(self, rg, name):
        self._log.append(("vm.delete", rg, name))
        return _Poller()


class _SpyDisks:
    def __init__(self, log):
        self._log = log

    def begin_delete(self, rg, name):
        self._log.append(("disk.delete", rg, name))
        return _Poller()


class _SpyCompute:
    def __init__(self, log):
        self.virtual_machines = _SpyVMs(log)
        self.disks = _SpyDisks(log)


class _SpyTags:
    def __init__(self, log):
        self._log = log

    def create_or_update_at_scope(self, scope, parameters):
        self._log.append(("tag", scope, parameters))
        return _Poller()


class _SpyResource:
    def __init__(self, log):
        self.tags = _SpyTags(log)


@pytest.fixture
def log() -> list:
    return []


@pytest.fixture
def clients(log) -> executor.ActionClients:
    return executor.ActionClients(compute=_SpyCompute(log), resource=_SpyResource(log))


# --------------------------------------------------------------------------- #
# tag / mark-for-op → Azure tag API
# --------------------------------------------------------------------------- #
def test_tag_action_calls_azure_client(clients, log) -> None:
    res = executor.execute_action(
        {"type": "tag", "tag": "cost-center", "value": "eng"},
        VM,
        settings=Settings(),
        clients=clients,
        dry_run=False,
    )

    assert res["executed"] is True
    assert log == [
        ("tag", VM["id"], {"operation": "Merge", "properties": {"tags": {"cost-center": "eng"}}})
    ]


def test_tag_action_with_tags_dict(clients, log) -> None:
    res = executor.execute_action(
        {"type": "tag", "tags": {"env": "prod", "team": "web"}},
        VM,
        settings=Settings(),
        clients=clients,
        dry_run=False,
    )

    assert res["executed"] is True
    assert log[0][2]["properties"]["tags"] == {"env": "prod", "team": "web"}


def test_mark_for_op_writes_status_tag(clients, log) -> None:
    res = executor.execute_action(
        {"type": "mark-for-op", "op": "stop"},
        VM,
        settings=Settings(),
        clients=clients,
        dry_run=False,
    )

    assert res["executed"] is True
    kind, scope, params = log[0]
    assert kind == "tag" and scope == VM["id"]
    assert params["properties"]["tags"]["custodian_status"] == "marked-for-op:stop"


# --------------------------------------------------------------------------- #
# stop / delete → compute SDK, only when not dry-run
# --------------------------------------------------------------------------- #
def test_stop_action_only_when_not_dry_run(clients, log) -> None:
    dry = executor.execute_action("stop", VM, settings=Settings(), clients=clients, dry_run=True)
    assert dry["executed"] is False and dry["dry_run"] is True
    assert log == []  # dry-run made no call

    live = executor.execute_action("stop", VM, settings=Settings(), clients=clients, dry_run=False)
    assert live["executed"] is True
    assert log == [("vm.deallocate", "rg", "vm-1")]


def test_delete_action_calls_delete(clients, log) -> None:
    res = executor.execute_action("delete", VM, settings=Settings(), clients=clients, dry_run=False)

    assert res["executed"] is True
    assert log == [("vm.delete", "rg", "vm-1")]


def test_delete_disk_calls_disk_delete(clients, log) -> None:
    res = executor.execute_action(
        "delete", DISK, settings=Settings(), clients=clients, dry_run=False
    )

    assert res["executed"] is True
    assert log == [("disk.delete", "rg", "d-1")]


# --------------------------------------------------------------------------- #
# dry-run makes zero client calls
# --------------------------------------------------------------------------- #
def test_dry_run_makes_no_client_calls(clients, log) -> None:
    for action in ("stop", "delete", {"type": "tag", "tag": "k", "value": "v"}):
        res = executor.execute_action(
            action, VM, settings=Settings(), clients=clients, dry_run=True
        )
        assert res["executed"] is False and res["dry_run"] is True

    assert log == []


# --------------------------------------------------------------------------- #
# negative / edge — structured errors, never a crash
# --------------------------------------------------------------------------- #
def test_unknown_action_type_errors(clients, log) -> None:
    res = executor.execute_action(
        "frobnicate", VM, settings=Settings(), clients=clients, dry_run=False
    )

    assert res["executed"] is False
    assert "unsupported action type" in res["error"]
    assert log == []  # rejected before any client call


def test_stop_on_non_vm_returns_error(clients, log) -> None:
    res = executor.execute_action("stop", BLOB, settings=Settings(), clients=clients, dry_run=False)

    assert res["executed"] is False
    assert "only supported for VMs" in res["error"]
    assert log == []


def test_delete_unsupported_type_returns_error(clients, log) -> None:
    res = executor.execute_action(
        "delete", BLOB, settings=Settings(), clients=clients, dry_run=False
    )

    assert res["executed"] is False
    assert "not supported" in res["error"]
    assert log == []


# --------------------------------------------------------------------------- #
# normalize_action
# --------------------------------------------------------------------------- #
def test_normalize_action_string_and_dict() -> None:
    assert executor.normalize_action("stop") == {"type": "stop"}
    assert executor.normalize_action({"type": "tag", "tag": "k"}) == {"type": "tag", "tag": "k"}


def test_normalize_action_invalid_raises() -> None:
    with pytest.raises(ValueError):
        executor.normalize_action({"tag": "k"})  # missing "type"
    with pytest.raises(ValueError):
        executor.normalize_action(123)  # not a str/dict


# --------------------------------------------------------------------------- #
# engine.resolve_actions — surface a policy's resolved actions
# --------------------------------------------------------------------------- #
def test_resolve_actions_surfaces_normalized() -> None:
    spec = {
        "policies": [
            {
                "name": "p",
                "resource": "azure.vm",
                "actions": ["stop", {"type": "tag", "tag": "k", "value": "v"}],
            }
        ]
    }

    assert engine.resolve_actions(spec) == [
        {"type": "stop"},
        {"type": "tag", "tag": "k", "value": "v"},
    ]


def test_resolve_actions_empty_spec() -> None:
    assert engine.resolve_actions({}) == []
    assert engine.resolve_actions({"policies": [{"name": "p"}]}) == []


# --------------------------------------------------------------------------- #
# live default-client wiring (SDK constructors monkeypatched, clients=None)
# --------------------------------------------------------------------------- #
def test_live_path_builds_default_clients(monkeypatch) -> None:
    calls: list = []
    monkeypatch.setattr(
        azure.mgmt.compute, "ComputeManagementClient", lambda cred, sub: _SpyCompute(calls)
    )
    monkeypatch.setattr(
        azure.mgmt.resource, "ResourceManagementClient", lambda cred, sub: _SpyResource(calls)
    )
    monkeypatch.setattr("cloudwarden.auth.write_credential", lambda: object())

    res = executor.execute_action("stop", VM, settings=Settings(), clients=None, dry_run=False)

    assert res["executed"] is True
    assert calls == [("vm.deallocate", "rg", "vm-1")]
