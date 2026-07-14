"""Live-path collector branches via injected fake clients (no network)."""

from __future__ import annotations

import datetime as dt

from cloudwarden.azure import advisor, cost, inventory, logs, metrics
from cloudwarden.azure.advisor import _to_float
from cloudwarden.config import get_settings
from cloudwarden.models import ResourceRecord

_VM_RID = "/subscriptions/s/resourcegroups/rg/providers/microsoft.compute/virtualmachines/vm"


def _vm() -> ResourceRecord:
    return ResourceRecord(
        resource_id=_VM_RID,
        name="vm",
        type="microsoft.compute/virtualmachines",
        location="eastus",
        resource_group="rg",
        subscription_id="s",
    )


# --- inventory ---
class _RGResp:
    def __init__(self, data):
        self.data = data
        self.skip_token = None


class _FakeRG:
    def resources(self, request):
        return _RGResp(
            [
                {
                    "id": "/subscriptions/s/resourceGroups/RG/providers/Microsoft.Compute/virtualMachines/VM",
                    "name": "VM",
                    "type": "Microsoft.Compute/virtualMachines",
                    "location": "eastus",
                    "resourceGroup": "RG",
                    "subscriptionId": "s",
                    "sku": "Standard_D4s_v5",
                    "tags": {"env": "prod"},
                    "powerState": "PowerState/running",
                    "diskState": None,
                    "ipConfig": None,
                    "numberOfSites": None,
                }
            ]
        )


def test_inventory_live(monkeypatch) -> None:
    monkeypatch.setenv("FINOPS_MOCK", "0")
    get_settings.cache_clear()
    records = inventory.collect_inventory(client=_FakeRG())
    assert len(records) == 1 and records[0].resource_id == _VM_RID
    get_settings.cache_clear()


# --- metrics ---
class _Point:
    def __init__(self, ts, avg, mx):
        self.timestamp = ts
        self.average = avg
        self.maximum = mx


class _TSeries:
    def __init__(self, data):
        self.data = data


class _Metric:
    def __init__(self, name, data):
        self.name = name
        self.unit = "Percent"
        self.timeseries = [_TSeries(data)]


class _MetricsResp:
    def __init__(self, metrics):
        self.metrics = metrics


class _FakeMetrics:
    def query_resource(self, resource_id, metric_names, timespan, granularity, aggregations):
        now = dt.datetime.now(dt.UTC)
        return _MetricsResp(
            [_Metric("Percentage CPU", [_Point(now, 10.0, 20.0), _Point(now, None, None)])]
        )


def test_metrics_live(monkeypatch) -> None:
    monkeypatch.setenv("FINOPS_MOCK", "0")
    get_settings.cache_clear()
    samples = metrics.collect_metrics([_vm()], client=_FakeMetrics())
    assert samples and all(s.metric_name == "Percentage CPU" for s in samples)
    get_settings.cache_clear()


# --- advisor ---
class _Short:
    problem = "p"
    solution = "s"


class _Meta:
    resource_id = (
        "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/VM"
    )


class _AdvRec:
    category = "Cost"
    impact = "High"
    short_description = _Short()
    resource_metadata = _Meta()
    extended_properties = {"targetSku": "Standard_D2s_v5", "annualSavingsAmount": "123.4"}


class _Recs:
    def list(self, filter):  # noqa: A002 - matches SDK signature
        return [_AdvRec()]


class _FakeAdvisor:
    recommendations = _Recs()


def test_advisor_live(monkeypatch) -> None:
    monkeypatch.setenv("FINOPS_MOCK", "0")
    get_settings.cache_clear()
    recs = advisor.collect_advisor(client=_FakeAdvisor())
    assert len(recs) == 1
    assert recs[0]["annual_savings"] == 123.4
    assert recs[0]["resource_id"].endswith("/vm")
    get_settings.cache_clear()


def test_to_float() -> None:
    assert _to_float(None) is None
    assert _to_float("bad") is None
    assert _to_float("1.5") == 1.5


# --- cost ---
class _FakeHttpResp:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._p


class _FakeHttpClient:
    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, headers=None, json=None):
        return _FakeHttpResp(
            {
                "properties": {
                    "columns": [
                        {"name": "Cost"},
                        {"name": "UsageDate"},
                        {"name": "ResourceId"},
                        {"name": "ServiceName"},
                        {"name": "Currency"},
                    ],
                    "rows": [[5.0, 20260712, _VM_RID.upper(), "Virtual Machines", "USD"]],
                    "nextLink": None,
                }
            }
        )


def test_cost_live(monkeypatch) -> None:
    monkeypatch.setenv("FINOPS_MOCK", "0")
    get_settings.cache_clear()
    monkeypatch.setattr("cloudwarden.auth.arm_token", lambda credential=None: "tok")
    monkeypatch.setattr(cost.httpx, "Client", _FakeHttpClient)
    rows = cost.collect_cost()
    assert rows and rows[0].cost == 5.0
    assert rows[0].usage_date == dt.date(2026, 7, 12)
    assert {r.cost_type for r in rows} == {"Amortized", "Actual"}
    get_settings.cache_clear()


# --- logs (memory) ---
def test_logs_memory_live(monkeypatch) -> None:
    from azure.monitor.query import LogsQueryStatus

    class _Table:
        columns = ["rid", "TimeGenerated", "used_pct"]
        rows = [[_VM_RID, dt.datetime.now(dt.UTC), 42.0]]

    class _LogsResp:
        status = LogsQueryStatus.SUCCESS
        tables = [_Table()]

    class _FakeLogs:
        def query_workspace(self, workspace_id, query, timespan):
            return _LogsResp()

    monkeypatch.setenv("FINOPS_MOCK", "0")
    monkeypatch.setenv("LOG_ANALYTICS_WORKSPACE_ID", "ws-123")
    get_settings.cache_clear()
    samples = logs.collect_memory([_vm()], client=_FakeLogs())
    assert samples and samples[0].metric_name == "Memory Used %"
    assert float(samples[0].avg) == 42.0
    get_settings.cache_clear()


def test_logs_memory_skipped_without_workspace(monkeypatch) -> None:
    monkeypatch.setenv("FINOPS_MOCK", "0")
    monkeypatch.delenv("LOG_ANALYTICS_WORKSPACE_ID", raising=False)
    get_settings.cache_clear()
    assert logs.collect_memory([_vm()]) == []
    get_settings.cache_clear()
