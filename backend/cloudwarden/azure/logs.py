"""Guest memory via Log Analytics (optional).

Host metrics do not expose guest RAM, so memory requires the Azure Monitor Agent
or a Log Analytics workspace. When no workspace is configured (or in mock mode,
where memory comes from the metrics fixture) this returns an empty list and the
downsize rule runs CPU-only with a caveat.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from ..config import Settings, get_settings
from ..metricnames import MEM_USED_PCT
from ..models import MetricSample, ResourceRecord
from ..resilience import REGISTRY, with_retry

logger = logging.getLogger("cloudwarden.azure.logs")

_VM_TYPE = "microsoft.compute/virtualmachines"

_KQL = """
InsightsMetrics
| where Namespace == "Memory" and Name == "AvailableMB"
| where TimeGenerated > ago({days}d)
| extend rid = tolower(tostring(_ResourceId))
| summarize used_pct = 100.0 - avg(Val) / max(Val) * 100.0 by rid, bin(TimeGenerated, 1h)
"""


def collect_memory(resources: list[ResourceRecord], client: Any = None) -> list[MetricSample]:
    settings = get_settings()
    if settings.finops_mock or not settings.log_analytics_workspace_id:
        return []
    return _collect_live(settings, resources, client)


@with_retry()
def _collect_live(
    settings: Settings, resources: list[ResourceRecord], client: Any
) -> list[MetricSample]:
    from azure.monitor.query import LogsQueryClient, LogsQueryStatus

    from ..auth import read_credential

    logs = client or LogsQueryClient(read_credential())
    vm_ids = {r.resource_id for r in resources if r.type == _VM_TYPE}
    query = _KQL.format(days=settings.metric_lookback_days)
    response = logs.query_workspace(
        settings.log_analytics_workspace_id,
        query,
        timespan=dt.timedelta(days=settings.metric_lookback_days),
    )
    out: list[MetricSample] = []
    if response.status == LogsQueryStatus.SUCCESS and response.tables:
        table = response.tables[0]
        cols = {name: i for i, name in enumerate(table.columns)}
        for row in table.rows:
            rid = str(row[cols["rid"]]).lower()
            if rid not in vm_ids:
                continue
            out.append(
                MetricSample(
                    resource_id=rid,
                    metric_name=MEM_USED_PCT,
                    ts=row[cols["TimeGenerated"]],
                    avg=float(row[cols["used_pct"]]),
                    max=float(row[cols["used_pct"]]),
                    unit="Percent",
                )
            )
    REGISTRY.set("memory", ok=True)
    return out
