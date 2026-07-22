"""Activity-based idle signal via Azure Monitor *platform* metrics.

Some billable resources — Azure Bastion, storage accounts, container registries —
emit no guest metrics but always emit platform metrics, with no diagnostic
settings required. We sum each supported type's primary "is anything using it"
metric over the lookback window; a resource that billed all period yet shows zero
activity is a strong idle candidate. This complements the shape-based detectors in
``analysis/idle.py`` (which key off inventory fields, not usage).

This is the pivot from the Log Analytics activity layer: the target workspace was
empty, but platform metrics are always-on, so they give us the same "unused but
paid-for" signal without depending on diagnostic-log ingestion.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from ..config import Settings, get_settings
from ..models import ActivitySignal, ResourceRecord
from ..resilience import REGISTRY, with_retry
from .context import SubscriptionContext

logger = logging.getLogger("cloudwarden.azure.activity_metrics")

# resource type -> its primary Total-aggregated activity metric. Metric names must
# match Azure Monitor's definitions exactly (verified live against the target
# subscription). Add a type here to extend activity-based idle detection to it.
ACTIVITY_METRICS: dict[str, str] = {
    "microsoft.network/bastionhosts": "sessions",
    "microsoft.storage/storageaccounts": "Transactions",
    "microsoft.containerregistry/registries": "TotalPullCount",
}


def collect_activity_metrics(
    resources: list[ResourceRecord],
    client: Any = None,
    subscription: SubscriptionContext | None = None,
) -> dict[str, ActivitySignal]:
    """Return a signal per supported resource that returned metric data.

    Mock mode returns ``{}`` (no activity fixtures — the detector is exercised via
    unit tests). Resources whose type is unsupported, or that return no data, are
    simply absent from the result.
    """
    settings = get_settings()
    if settings.finops_mock:
        return {}
    cred = subscription.credential if subscription else None
    return _collect_live(settings, resources, client, cred)


@with_retry()
def _query_total(
    mq: Any, resource_id: str, metric: str, timespan: dt.timedelta
) -> tuple[float, int]:
    """Sum one metric's Total-aggregated points over ``timespan``.

    No granularity is requested: some metrics (e.g. Bastion ``sessions``) reject
    coarse grains, so we let Azure choose the bucketing and just sum the totals.
    Retries on throttling/5xx per resource so one 429 doesn't drop the signal.
    """
    from azure.monitor.query import MetricAggregationType

    resp = mq.query_resource(
        resource_id,
        metric_names=[metric],
        timespan=timespan,
        aggregations=[MetricAggregationType.TOTAL],
    )
    total = 0.0
    points = 0
    for m in resp.metrics:
        for series in m.timeseries:
            for point in series.data:
                if point.total is not None:
                    total += point.total
                    points += 1
    return total, points


def _collect_live(
    settings: Settings, resources: list[ResourceRecord], client: Any, credential: Any = None
) -> dict[str, ActivitySignal]:
    from azure.monitor.query import MetricsQueryClient

    from ..auth import read_credential

    mq = client or MetricsQueryClient(credential or read_credential())
    timespan = dt.timedelta(days=settings.metric_lookback_days)
    out: dict[str, ActivitySignal] = {}
    for r in resources:
        metric = ACTIVITY_METRICS.get(r.type)
        if not metric:
            continue
        try:
            total, points = _query_total(mq, r.resource_id, metric, timespan)
        except Exception:  # noqa: BLE001 - one bad resource must not sink the batch
            logger.warning("activity metric query failed for %s", r.resource_id, exc_info=True)
            continue
        if points:
            out[r.resource_id] = ActivitySignal(
                resource_id=r.resource_id, metric_name=metric, total=total, datapoints=points
            )
    REGISTRY.set("activity_metrics", ok=True)
    return out
