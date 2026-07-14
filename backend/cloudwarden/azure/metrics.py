"""VM utilization metrics via Azure Monitor (mock-backed).

Collects Percentage CPU, Network In/Out and Disk Read/Write ops per VM as hourly
samples over the metric lookback window. Memory is guest-level and comes from
Log Analytics (azure/logs.py) on the live path; in mock mode the fixture profile
includes a synthetic ``Memory Used %`` series so downsize rules can exercise the
memory branch.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from ..config import Settings, get_settings
from ..metricnames import CPU, DISK_READ_OPS, DISK_WRITE_OPS, MEM_USED_PCT, NET_IN, NET_OUT
from ..models import MetricSample, ResourceRecord
from ..resilience import REGISTRY, with_retry
from ._fixtures import load_fixture, retarget
from .context import SubscriptionContext

logger = logging.getLogger("cloudwarden.azure.metrics")

_VM_TYPE = "microsoft.compute/virtualmachines"


def collect_metrics(
    resources: list[ResourceRecord],
    client: Any = None,
    subscription: SubscriptionContext | None = None,
) -> list[MetricSample]:
    settings = get_settings()
    if settings.finops_mock:
        sub_id = subscription.subscription_id if subscription else settings.azure_subscription_id
        samples = _mock_samples(settings, resources, sub_id)
        REGISTRY.set("metrics", ok=True)
        return samples
    cred = subscription.credential if subscription else None
    return _collect_live(settings, resources, client, cred)


def _evenly(low: float, high: float, n: int) -> list[float]:
    if n <= 1:
        return [(low + high) / 2.0]
    return [low + (high - low) * i / (n - 1) for i in range(n)]


def _mock_samples(
    settings: Settings, resources: list[ResourceRecord], subscription_id: str
) -> list[MetricSample]:
    profiles = load_fixture("metrics")
    hours = max(settings.metric_lookback_days, 1) * 24
    now = dt.datetime.now(dt.UTC).replace(minute=0, second=0, microsecond=0)
    vm_ids = {r.resource_id for r in resources if r.type == _VM_TYPE}
    out: list[MetricSample] = []
    for raw_rid, prof in profiles.items():
        rid = retarget(str(raw_rid).lower(), subscription_id)
        if rid not in vm_ids:
            continue
        cpu_vals = _evenly(prof["cpu"]["low"], prof["cpu"]["high"], hours)
        mem = prof.get("mem_used_pct")
        mem_vals = _evenly(mem["low"], mem["high"], hours) if mem else None
        net_h = float(prof.get("net_bytes_hour", 0.0))
        iops = float(prof.get("disk_iops", 0.0))
        for i in range(hours):
            ts = now - dt.timedelta(hours=hours - 1 - i)
            cpu = cpu_vals[i]
            out.append(
                MetricSample(
                    resource_id=rid,
                    metric_name=CPU,
                    ts=ts,
                    avg=cpu,
                    max=cpu,
                    min=cpu,
                    unit="Percent",
                )
            )
            out.append(
                MetricSample(
                    resource_id=rid, metric_name=NET_IN, ts=ts, avg=net_h / 2, unit="Bytes"
                )
            )
            out.append(
                MetricSample(
                    resource_id=rid, metric_name=NET_OUT, ts=ts, avg=net_h / 2, unit="Bytes"
                )
            )
            out.append(
                MetricSample(
                    resource_id=rid,
                    metric_name=DISK_READ_OPS,
                    ts=ts,
                    avg=iops / 2,
                    unit="CountPerSecond",
                )
            )
            out.append(
                MetricSample(
                    resource_id=rid,
                    metric_name=DISK_WRITE_OPS,
                    ts=ts,
                    avg=iops / 2,
                    unit="CountPerSecond",
                )
            )
            if mem_vals is not None:
                out.append(
                    MetricSample(
                        resource_id=rid,
                        metric_name=MEM_USED_PCT,
                        ts=ts,
                        avg=mem_vals[i],
                        max=mem_vals[i],
                        unit="Percent",
                    )
                )
    return out


@with_retry()
def _collect_live(
    settings: Settings, resources: list[ResourceRecord], client: Any, credential: Any = None
) -> list[MetricSample]:
    from azure.monitor.query import MetricAggregationType, MetricsQueryClient

    from ..auth import read_credential

    mq = client or MetricsQueryClient(credential or read_credential())
    metric_names = [CPU, NET_IN, NET_OUT, DISK_READ_OPS, DISK_WRITE_OPS]
    timespan = dt.timedelta(days=settings.metric_lookback_days)
    out: list[MetricSample] = []
    for resource in resources:
        if resource.type != _VM_TYPE:
            continue
        response = mq.query_resource(
            resource.resource_id,
            metric_names=metric_names,
            timespan=timespan,
            granularity=dt.timedelta(hours=1),
            aggregations=[MetricAggregationType.AVERAGE, MetricAggregationType.MAXIMUM],
        )
        for metric in response.metrics:
            for series in metric.timeseries:
                for point in series.data:
                    if point.average is None and point.maximum is None:
                        continue
                    out.append(
                        MetricSample(
                            resource_id=resource.resource_id,
                            metric_name=metric.name,
                            ts=point.timestamp,
                            avg=point.average,
                            max=point.maximum,
                            unit=str(metric.unit),
                        )
                    )
    REGISTRY.set("metrics", ok=True)
    return out
