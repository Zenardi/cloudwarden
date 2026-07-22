"""Rules engine, rollups, idle detectors and savings — pure-function tests."""

from __future__ import annotations

import datetime as dt

from cloudwarden.analysis.idle import detect_idle, detect_idle_by_activity
from cloudwarden.analysis.rollup import build_rollup
from cloudwarden.analysis.rules import evaluate_vms, prioritize
from cloudwarden.analysis.savings import monthly_cost_map
from cloudwarden.config import Settings
from cloudwarden.metricnames import CPU, MEM_USED_PCT, NET_IN, NET_OUT
from cloudwarden.models import (
    ActivitySignal,
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


def test_idle_reserved_disk_from_deallocated_vm() -> None:
    """A Reserved disk (attached to a deallocated VM) is flagged as idle_disk with
    an advisory, non-auto-executable action and the disk's own monthly cost."""
    disk = ResourceRecord(
        resource_id="/x/osdisk",
        name="rw-vm-test-1_OsDisk",
        type="microsoft.compute/disks",
        location="eastus",
        resource_group="rg",
        subscription_id="s",
        extra={"diskState": "Reserved"},
    )
    recs = detect_idle([disk], {"/x/osdisk": 4.87})
    assert len(recs) == 1
    rec = recs[0]
    assert rec.category == "idle_disk"
    assert rec.action == "review_stopped_vm"
    assert rec.est_monthly_savings == 4.87
    assert rec.evidence == {"diskState": "Reserved"}
    # Advisory only — must NOT map to an auto-executable remediation action.
    from cloudwarden.remediation.executor import SUPPORTED

    assert rec.action not in SUPPORTED


def test_stopped_vm_detected() -> None:
    """A deallocated VM is surfaced (stopped_vm) even though it has no rollup."""
    vm = ResourceRecord(
        resource_id="/x/vm",
        name="rw-vm-test-1",
        type="microsoft.compute/virtualmachines",
        location="eastus",
        resource_group="rg",
        subscription_id="s",
        power_state="PowerState/deallocated",
    )
    running = vm.model_copy(update={"resource_id": "/x/vm2", "power_state": "PowerState/running"})
    recs = detect_idle([vm, running], {"/x/vm": 1.51})
    assert [r.category for r in recs] == ["stopped_vm"]
    # Advisory only: savings are 0 (the disk is quantified separately), not the VM's
    # projected historical compute cost — a stopped VM no longer bills for compute.
    assert recs[0].action == "review_stopped_vm" and recs[0].est_monthly_savings == 0.0


def test_attached_disk_is_not_flagged() -> None:
    """A disk attached to a *running* VM (diskState 'Attached') is not waste."""
    disk = ResourceRecord(
        resource_id="/x/osdisk",
        name="d",
        type="microsoft.compute/disks",
        location="eastus",
        resource_group="rg",
        subscription_id="s",
        extra={"diskState": "Attached"},
    )
    assert detect_idle([disk], {"/x/osdisk": 4.87}) == []


def _dc(rid: str, rtype: str, name: str, config: dict) -> ResourceRecord:
    return ResourceRecord(
        resource_id=rid,
        name=name,
        type=rtype,
        location="eastus",
        resource_group="rg",
        subscription_id="s",
        config=config,
    )


def test_idle_devcenter_pool() -> None:
    """DevCenter pools: an empty pool is full-cost reclaim; a pool on a large
    (>=16 vCPU) dev-box definition is an advisory right-sizing candidate."""
    definition = _dc(
        "/x/def",
        "microsoft.devcenter/devcenters/devboxdefinitions",
        "win11-def",
        {"sku": {"name": "general_i_16c64gb512ssd_v2"}},
    )
    empty = _dc(
        "/x/pool-empty",
        "microsoft.devcenter/projects/pools",
        "empty-pool",
        {"devBoxCount": 0},
    )
    oversized = _dc(
        "/x/pool-big",
        "microsoft.devcenter/projects/pools",
        "big-pool",
        {"devBoxCount": 3, "devBoxDefinitionName": "win11-def"},
    )
    monthly = {"/x/pool-empty": 35.24, "/x/pool-big": 387.6}
    recs = {r.category: r for r in detect_idle([definition, empty, oversized], monthly)}
    assert set(recs) == {"idle_pool", "oversized_pool"}
    # Empty pool: full observed monthly cost is reclaimable.
    assert recs["idle_pool"].est_monthly_savings == 35.24
    # Oversized: conservative 0.35x estimate, advisory (low confidence).
    assert recs["oversized_pool"].est_monthly_savings == round(387.6 * 0.35, 2)
    assert recs["oversized_pool"].confidence == 0.4
    assert recs["oversized_pool"].evidence["vcpu"] == 16


def test_idle_mongo_cluster_paid_only() -> None:
    """Paid Cosmos-DB-for-MongoDB vCore clusters are flagged advisory; Free is skipped."""
    paid = _dc(
        "/x/mongo-paid",
        "microsoft.documentdb/mongoclusters",
        "paid-mongo",
        {"nodeGroupSpecs": [{"sku": "M30"}]},
    )
    free = _dc(
        "/x/mongo-free",
        "microsoft.documentdb/mongoclusters",
        "free-mongo",
        {"nodeGroupSpecs": [{"sku": "Free"}]},
    )
    recs = detect_idle([paid, free], {"/x/mongo-paid": 180.0, "/x/mongo-free": 0.0})
    assert [r.category for r in recs] == ["mongo_cluster"]
    rec = recs[0]
    assert rec.current_sku == "M30"
    # Advisory: quantified idle detection is metric-based, so savings stays 0.
    assert rec.est_monthly_savings == 0.0


def _mlc(name: str, config: dict) -> ResourceRecord:
    return _dc(
        f"/x/ws/computes/{name}",
        "microsoft.machinelearningservices/workspaces/computes",
        name,
        {**config, "workspace_id": "/x/ws"},
    )


def test_idle_ml_compute() -> None:
    """ML compute: a running or failed Compute Instance and a warm (min_nodes>0)
    AmlCompute cluster are flagged advisory; a stopped instance and a scale-to-zero
    cluster are the correct end state and produce nothing. Cost rolls up to the
    workspace, so every rec is advisory (savings 0) with workspace cost as context."""
    running = _mlc(
        "ci-run",
        {"compute_type": "ComputeInstance", "state": "Running", "vm_size": "Standard_DS3_v2"},
    )
    stopped = _mlc(
        "ci-stop",
        {"compute_type": "ComputeInstance", "state": "Stopped", "vm_size": "Standard_DS3_v2"},
    )
    failed = _mlc(
        "ci-fail",
        {"compute_type": "ComputeInstance", "state": "CreateFailed", "vm_size": "STANDARD_NC4_T4"},
    )
    warm = _mlc(
        "clus-warm",
        {"compute_type": "AmlCompute", "min_node_count": 2, "vm_size": "Standard_DS3_v2"},
    )
    zero = _mlc(
        "clus-zero",
        {"compute_type": "AmlCompute", "min_node_count": 0, "vm_size": "Standard_DS3_v2"},
    )
    recs = detect_idle([running, stopped, failed, warm, zero], {"/x/ws": 85.81})
    # Only the wasteful shapes are surfaced; stopped + scale-to-zero are fine.
    flagged = {r.resource_id.split("/computes/")[1] for r in recs}
    assert flagged == {"ci-run", "ci-fail", "clus-warm"}
    assert all(r.category == "idle_ml_compute" for r in recs)
    assert all(r.est_monthly_savings == 0.0 for r in recs)
    # Workspace monthly cost is carried as evidence context (not as savings).
    assert all(r.evidence["workspace_monthly"] == 85.81 for r in recs)
    # Advisory only — never an auto-executable remediation action.
    from cloudwarden.remediation.executor import SUPPORTED

    assert all(r.action not in SUPPORTED for r in recs)


def _bastion(rid: str = "/x/bastion") -> ResourceRecord:
    return ResourceRecord(
        resource_id=rid,
        name="b",
        type="microsoft.network/bastionhosts",
        location="eastus",
        resource_group="rg",
        subscription_id="s",
    )


def test_idle_by_activity_flags_zero_activity_billing_resource() -> None:
    """A Bastion that billed all window but logged 0 sessions is flagged advisory."""
    bastion = _bastion()
    activity = {
        "/x/bastion": ActivitySignal(
            resource_id="/x/bastion", metric_name="sessions", total=0.0, datapoints=4032
        )
    }
    recs = detect_idle_by_activity([bastion], activity, {"/x/bastion": 215.21})
    assert len(recs) == 1
    rec = recs[0]
    assert rec.category == "idle_by_activity"
    assert rec.est_monthly_savings == 215.21
    assert rec.evidence["metric"] == "sessions" and rec.evidence["datapoints"] == 4032
    # Advisory only — must NOT map to an auto-executable remediation action.
    from cloudwarden.remediation.executor import SUPPORTED

    assert rec.action not in SUPPORTED


def test_idle_by_activity_ignores_resource_with_activity() -> None:
    """Any activity above the threshold means the resource is in use — not idle."""
    activity = {
        "/x/bastion": ActivitySignal(
            resource_id="/x/bastion", metric_name="sessions", total=12.0, datapoints=4032
        )
    }
    assert detect_idle_by_activity([_bastion()], activity, {"/x/bastion": 215.21}) == []


def test_idle_by_activity_needs_observed_data() -> None:
    """No signal (absent, or 0 datapoints) is 'unknown', never flagged as idle."""
    # Absent from the activity map entirely.
    assert detect_idle_by_activity([_bastion()], {}, {"/x/bastion": 215.21}) == []
    # Present but with no observed datapoints.
    empty = {
        "/x/bastion": ActivitySignal(
            resource_id="/x/bastion", metric_name="sessions", total=0.0, datapoints=0
        )
    }
    assert detect_idle_by_activity([_bastion()], empty, {"/x/bastion": 215.21}) == []


def test_idle_by_activity_skips_trivially_cheap() -> None:
    """A resource under the monthly-cost floor is not worth surfacing."""
    activity = {
        "/x/bastion": ActivitySignal(
            resource_id="/x/bastion", metric_name="sessions", total=0.0, datapoints=4032
        )
    }
    assert detect_idle_by_activity([_bastion()], activity, {"/x/bastion": 0.40}) == []


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
