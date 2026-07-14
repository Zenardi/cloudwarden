"""Rules engine, rollups, idle detectors and savings — pure-function tests."""

from __future__ import annotations

import datetime as dt

from cloudwarden.analysis.idle import detect_idle
from cloudwarden.analysis.rollup import build_rollup
from cloudwarden.analysis.rules import evaluate_vms, prioritize
from cloudwarden.analysis.savings import monthly_cost_map
from cloudwarden.config import Settings
from cloudwarden.metricnames import CPU, MEM_USED_PCT, NET_IN, NET_OUT
from cloudwarden.models import (
    CostRow,
    MetricSample,
    Recommendation,
    ResourceRecord,
    UtilizationRollup,
)

SETTINGS = Settings()
_NOW = dt.datetime.now(dt.UTC)


def _vm(
    rid: str = "/x/vm", sku: str = "Standard_D4s_v5", power: str = "PowerState/running"
) -> ResourceRecord:
    return ResourceRecord(
        resource_id=rid,
        name="vm",
        type="microsoft.compute/virtualmachines",
        location="eastus",
        resource_group="rg",
        subscription_id="s",
        sku=sku,
        power_state=power,
    )


def _roll(**kw) -> UtilizationRollup:
    base: dict = {
        "resource_id": "/x/vm",
        "window_start": _NOW,
        "window_end": _NOW,
        "data_completeness": 1.0,
    }
    base.update(kw)
    return UtilizationRollup(**base)


def test_rollup_stats() -> None:
    samples: list[MetricSample] = []
    for i in range(100):
        samples.append(
            MetricSample(resource_id="/x/vm", metric_name=CPU, ts=_NOW, avg=float(i), max=float(i))
        )
        samples.append(
            MetricSample(resource_id="/x/vm", metric_name=MEM_USED_PCT, ts=_NOW, avg=30.0)
        )
        samples.append(MetricSample(resource_id="/x/vm", metric_name=NET_IN, ts=_NOW, avg=1000.0))
        samples.append(MetricSample(resource_id="/x/vm", metric_name=NET_OUT, ts=_NOW, avg=1000.0))
    roll = build_rollup("/x/vm", samples, _NOW, _NOW, expected_samples=100)
    assert roll.cpu_max == 99.0
    assert 93 < roll.cpu_p95 < 95
    assert roll.mem_available and roll.mem_p95 == 30.0
    assert roll.net_bytes_day == 48000.0
    assert roll.data_completeness == 1.0


def test_shutdown_rule_fires() -> None:
    roll = _roll(cpu_p95=2.4, cpu_max=2.6, net_bytes_day=1_000_000, mem_available=False)
    recs = evaluate_vms([_vm()], {"/x/vm": roll}, {"/x/vm": 400.0}, set(), SETTINGS)
    assert len(recs) == 1
    assert recs[0].category == "shutdown" and recs[0].action == "deallocate"
    assert recs[0].est_monthly_savings == 400.0


def test_downsize_with_memory() -> None:
    roll = _roll(cpu_p95=30.0, cpu_max=45.0, mem_available=True, mem_p95=40.0)
    recs = evaluate_vms(
        [_vm(sku="Standard_D4s_v5")], {"/x/vm": roll}, {"/x/vm": 140.0}, set(), SETTINGS
    )
    r = recs[0]
    assert r.category == "downsize" and r.recommended_sku == "Standard_D2s_v5"
    assert abs(r.est_monthly_savings - 70.08) < 0.5  # (0.192-0.096)*730
    assert r.confidence == 0.75 and not r.caveats


def test_downsize_cpu_only_caveat() -> None:
    roll = _roll(cpu_p95=30.0, cpu_max=45.0, mem_available=False)
    recs = evaluate_vms([_vm()], {"/x/vm": roll}, {"/x/vm": 140.0}, set(), SETTINGS)
    r = recs[0]
    assert r.category == "downsize" and r.confidence == 0.55
    assert any("memory" in c for c in r.caveats)


def test_advisor_agreement_boosts() -> None:
    roll = _roll(cpu_p95=2.4, cpu_max=2.6, net_bytes_day=1000, mem_available=False)
    recs = evaluate_vms([_vm()], {"/x/vm": roll}, {"/x/vm": 400.0}, {"/x/vm"}, SETTINGS)
    assert recs[0].source == "combined"
    assert abs(recs[0].confidence - 0.9) < 1e-6


def test_low_data_investigate() -> None:
    roll = _roll(cpu_p95=2.0, cpu_max=3.0, data_completeness=0.2)
    recs = evaluate_vms([_vm()], {"/x/vm": roll}, {}, set(), SETTINGS)
    assert recs[0].category == "investigate"


def test_idle_detectors() -> None:
    disk = ResourceRecord(
        resource_id="/x/disk",
        name="d",
        type="microsoft.compute/disks",
        location="eastus",
        resource_group="rg",
        subscription_id="s",
        extra={"diskState": "Unattached"},
    )
    ip = ResourceRecord(
        resource_id="/x/ip",
        name="i",
        type="microsoft.network/publicipaddresses",
        location="eastus",
        resource_group="rg",
        subscription_id="s",
        extra={"ipConfig": None},
    )
    asp = ResourceRecord(
        resource_id="/x/asp",
        name="a",
        type="microsoft.web/serverfarms",
        location="eastus",
        resource_group="rg",
        subscription_id="s",
        extra={"numberOfSites": 0},
    )
    recs = detect_idle([disk, ip, asp], {"/x/disk": 10.0, "/x/ip": 3.0, "/x/asp": 70.0})
    assert {r.category for r in recs} == {"delete_orphan", "idle_ip", "empty_asp"}


def test_monthly_cost_map() -> None:
    today = dt.date.today()
    rows = [
        CostRow(usage_date=today, resource_id="/x/vm", cost=10.0, cost_type="Amortized"),
        CostRow(
            usage_date=today - dt.timedelta(days=1),
            resource_id="/x/vm",
            cost=10.0,
            cost_type="Amortized",
        ),
        CostRow(usage_date=today, resource_id="/x/vm", cost=99.0, cost_type="Actual"),
    ]
    result = monthly_cost_map(rows)
    assert abs(result["/x/vm"] - 304.0) < 1e-6  # avg 10/day * 30.4


def test_prioritize_orders_by_savings() -> None:
    recs = [
        Recommendation(
            resource_id="a", category="downsize", action="resize", est_monthly_savings=10
        ),
        Recommendation(
            resource_id="b", category="shutdown", action="deallocate", est_monthly_savings=50
        ),
    ]
    out = prioritize(recs)
    assert out[0].resource_id == "b" and out[0].priority == 1
    assert out[1].priority == 2
