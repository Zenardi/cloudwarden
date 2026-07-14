"""Azure Activity Log collector (mock-backed) — AssetDB change history (M4.4).

Captures *who / how / when* each asset changed by parsing the Activity Log into a
flat shape the repository persists into ``asset_events``, giving AssetDB its audit
timeline (a core Stacklet differentiator). Each parsed event carries the actor
(``caller``), the operation (``operationName``) and the timestamp
(``eventTimestamp``). Resource ids are lower-cased so they join with the inventory /
assets; malformed records (missing resource id / operation / timestamp) are skipped,
not fatal.
"""

from __future__ import annotations

import logging
from typing import Any

from ..config import get_settings
from ..resilience import REGISTRY, with_retry
from ._fixtures import load_fixture, retarget
from .context import SubscriptionContext

logger = logging.getLogger("cloudwarden.azure.activitylog")

# Live lookback: Activity Log is retained ~90 days; a month of history is plenty for
# the audit timeline while keeping the query cheap.
_LOOKBACK_DAYS = 30


def _dig(node: Any, *keys: str) -> Any:
    """Walk nested dict keys, returning None if any hop is missing or not a dict."""
    for key in keys:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def _parse(entry: dict[str, Any], subscription_id: str, mock: bool) -> dict[str, Any] | None:
    """Normalize one raw Activity Log entry, or return None if it is malformed."""
    resource_id = entry.get("resourceId")
    operation = _dig(entry, "operationName", "value")
    timestamp = entry.get("eventTimestamp")
    if not resource_id or not operation or not timestamp:
        return None
    rid = str(resource_id).lower()
    if mock:
        rid = retarget(rid, subscription_id)
    return {
        "resource_id": rid,
        "subscription_id": subscription_id,
        "operation": str(operation),
        "actor": entry.get("caller"),
        "timestamp": timestamp.isoformat() if hasattr(timestamp, "isoformat") else str(timestamp),
        "status": _dig(entry, "status", "value"),
        "correlation_id": entry.get("correlationId"),
    }


def _parse_all(
    entries: list[dict[str, Any]], subscription_id: str, mock: bool
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for entry in entries:
        parsed = _parse(entry, subscription_id, mock)
        if parsed is None:
            logger.warning("skipping malformed activity record: %r", entry)
            continue
        out.append(parsed)
    return out


def collect_activity_log(
    client: Any = None, subscription: SubscriptionContext | None = None
) -> list[dict[str, Any]]:
    settings = get_settings()
    sub_id = subscription.subscription_id if subscription else settings.azure_subscription_id
    if settings.finops_mock:
        REGISTRY.set("activitylog", ok=True)
        return _parse_all(load_fixture("activitylog"), sub_id, mock=True)
    cred = subscription.credential if subscription else None
    return _collect_live(client, sub_id, cred)


def _to_entry(event: Any) -> dict[str, Any]:
    """Adapt a live ``EventData`` SDK object to the raw dict shape ``_parse`` expects."""
    return {
        "resourceId": getattr(event, "resource_id", None),
        "operationName": {"value": getattr(getattr(event, "operation_name", None), "value", None)},
        "caller": getattr(event, "caller", None),
        "eventTimestamp": getattr(event, "event_timestamp", None),
        "status": {"value": getattr(getattr(event, "status", None), "value", None)},
        "correlationId": getattr(event, "correlation_id", None),
    }


@with_retry()
def _collect_live(
    client: Any, subscription_id: str, credential: Any = None
) -> list[dict[str, Any]]:
    from datetime import UTC, datetime, timedelta

    from azure.mgmt.monitor import MonitorManagementClient

    from ..auth import read_credential

    monitor = client or MonitorManagementClient(credential or read_credential(), subscription_id)
    since = (datetime.now(UTC) - timedelta(days=_LOOKBACK_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    raw = monitor.activity_logs.list(filter=f"eventTimestamp ge '{since}'")
    entries = [_to_entry(e) for e in raw]
    REGISTRY.set("activitylog", ok=True)
    return _parse_all(entries, subscription_id, mock=False)
