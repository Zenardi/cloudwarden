"""Collapse raw metric samples into per-resource utilization rollups.

Percentiles are computed here (the Metrics API doesn't return p95). `net_bytes_day`
is derived from mean hourly Network In+Out × 24; `disk_iops_avg` from mean read+write
ops/sec; memory only when `Memory Used %` samples are present (mem_available).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from ..metricnames import CPU, DISK_READ_OPS, DISK_WRITE_OPS, MEM_USED_PCT, NET_IN, NET_OUT
from ..models import MetricSample, UtilizationRollup


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    k = (len(ordered) - 1) * pct / 100.0
    floor = int(k)
    ceil = min(floor + 1, len(ordered) - 1)
    return float(ordered[floor] + (ordered[ceil] - ordered[floor]) * (k - floor))


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def build_rollup(
    resource_id: str,
    samples: list[MetricSample],
    window_start: datetime,
    window_end: datetime,
    expected_samples: int,
) -> UtilizationRollup:
    avg_by_metric: dict[str, list[float]] = defaultdict(list)
    max_by_metric: dict[str, list[float]] = defaultdict(list)
    for s in samples:
        if s.avg is not None:
            avg_by_metric[s.metric_name].append(float(s.avg))
        if s.max is not None:
            max_by_metric[s.metric_name].append(float(s.max))

    cpu = avg_by_metric.get(CPU, [])
    cpu_max_vals = max_by_metric.get(CPU) or cpu
    cpu_max = max(cpu_max_vals) if cpu_max_vals else None
    mem = avg_by_metric.get(MEM_USED_PCT, [])

    net_in = _mean(avg_by_metric.get(NET_IN, []))
    net_out = _mean(avg_by_metric.get(NET_OUT, []))
    net_day = None
    if net_in is not None or net_out is not None:
        net_day = ((net_in or 0.0) + (net_out or 0.0)) * 24.0

    disk_read = _mean(avg_by_metric.get(DISK_READ_OPS, []))
    disk_write = _mean(avg_by_metric.get(DISK_WRITE_OPS, []))
    disk_iops = None
    if disk_read is not None or disk_write is not None:
        disk_iops = (disk_read or 0.0) + (disk_write or 0.0)

    completeness = min(len(cpu) / expected_samples, 1.0) if expected_samples else 0.0
    return UtilizationRollup(
        resource_id=resource_id,
        window_start=window_start,
        window_end=window_end,
        cpu_avg=_mean(cpu),
        cpu_p95=_percentile(cpu, 95),
        cpu_max=cpu_max,
        mem_avg=_mean(mem),
        mem_p95=_percentile(mem, 95),
        mem_available=bool(mem),
        net_bytes_day=net_day,
        disk_iops_avg=disk_iops,
        sample_count=len(cpu),
        data_completeness=completeness,
    )


def build_rollups(
    samples: list[MetricSample],
    window_start: datetime,
    window_end: datetime,
    expected_samples: int,
) -> list[UtilizationRollup]:
    by_resource: dict[str, list[MetricSample]] = defaultdict(list)
    for s in samples:
        by_resource[s.resource_id].append(s)
    return [
        build_rollup(rid, group, window_start, window_end, expected_samples)
        for rid, group in by_resource.items()
    ]
