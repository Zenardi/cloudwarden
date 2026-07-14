"""Executor live-path branches via monkeypatched Azure SDK clients."""

from __future__ import annotations

import azure.mgmt.compute
import azure.mgmt.network

from cloudwarden.config import Settings
from cloudwarden.remediation import executor

RID = "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm"
DISK = "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Compute/disks/d"
PIP = "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Network/publicIPAddresses/ip"


class _Poller:
    def result(self):
        return None


class _VMs:
    def begin_deallocate(self, rg, name):
        return _Poller()

    def begin_update(self, rg, name, params):
        return _Poller()


class _Disks:
    def begin_delete(self, rg, name):
        return _Poller()


class _FakeCompute:
    def __init__(self, cred, sub):
        self.virtual_machines = _VMs()
        self.disks = _Disks()


class _PIPs:
    def begin_delete(self, rg, name):
        return _Poller()


class _FakeNetwork:
    def __init__(self, cred, sub):
        self.public_ip_addresses = _PIPs()


def _patch(monkeypatch) -> None:
    monkeypatch.setattr(azure.mgmt.compute, "ComputeManagementClient", _FakeCompute)
    monkeypatch.setattr(azure.mgmt.network, "NetworkManagementClient", _FakeNetwork)
    monkeypatch.setattr("cloudwarden.auth.write_credential", lambda: object())


def test_execute_deallocate(monkeypatch) -> None:
    _patch(monkeypatch)
    res = executor.execute("deallocate", RID, {}, Settings(), dry_run=False)
    assert res["executed"] is True


def test_execute_resize(monkeypatch) -> None:
    _patch(monkeypatch)
    res = executor.execute(
        "resize", RID, {"recommended_sku": "Standard_D2s_v5"}, Settings(), dry_run=False
    )
    assert res["executed"] is True


def test_execute_delete_disk(monkeypatch) -> None:
    _patch(monkeypatch)
    res = executor.execute("delete_disk", DISK, {}, Settings(), dry_run=False)
    assert res["executed"] is True


def test_execute_delete_public_ip(monkeypatch) -> None:
    _patch(monkeypatch)
    res = executor.execute("delete_public_ip", PIP, {}, Settings(), dry_run=False)
    assert res["executed"] is True
