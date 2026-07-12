"""Resource inventory via Azure Resource Graph (mock-backed for offline dev).

Returns one `ResourceRecord` per resource with type/region/tags/SKU plus a few
shape fields used later by the idle detectors (diskState, ipConfig,
numberOfSites). Resource ids are lower-cased so they join cleanly with cost rows
(Cost Management returns lower-cased ResourceIds).
"""

from __future__ import annotations

import logging
from typing import Any

from ..config import get_settings
from ..models import ResourceRecord
from ..resilience import REGISTRY, with_retry
from ._fixtures import load_fixture, retarget
from .context import SubscriptionContext

logger = logging.getLogger("azure_finops.azure.inventory")

_RG_QUERY = """
Resources
| project id, name, type, location, resourceGroup, subscriptionId,
          sku = tostring(sku.name),
          tags,
          powerState = tostring(properties.extended.instanceView.powerState.code),
          diskState = tostring(properties.diskState),
          ipConfig = tostring(properties.ipConfiguration.id),
          numberOfSites = toint(properties.numberOfSites)
"""


def _to_records(
    rows: list[dict[str, Any]], subscription_id: str, mock: bool = False
) -> list[ResourceRecord]:
    records: list[ResourceRecord] = []
    for r in rows:
        rid = str(r["id"]).lower()
        if mock:
            rid = retarget(rid, subscription_id)
        tags = r.get("tags") or {}
        if not isinstance(tags, dict):
            tags = {}
        records.append(
            ResourceRecord(
                resource_id=rid,
                name=r.get("name") or "",
                type=(r.get("type") or "").lower(),
                location=r.get("location") or "",
                resource_group=r.get("resourceGroup") or "",
                subscription_id=subscription_id
                if mock
                else (r.get("subscriptionId") or subscription_id),
                sku=r.get("sku") or None,
                tags={str(k): str(v) for k, v in tags.items()},
                power_state=r.get("powerState") or None,
                extra={k: r.get(k) for k in ("diskState", "ipConfig", "numberOfSites")},
            )
        )
    return records


def collect_inventory(
    client: Any = None, subscription: SubscriptionContext | None = None
) -> list[ResourceRecord]:
    settings = get_settings()
    sub_id = subscription.subscription_id if subscription else settings.azure_subscription_id
    if settings.finops_mock:
        rows = load_fixture("inventory")
        REGISTRY.set("inventory", ok=True)
        return _to_records(rows, sub_id, mock=True)
    cred = subscription.credential if subscription else None
    return _collect_live(client, sub_id, cred)


@with_retry()
def _collect_live(
    client: Any, subscription_id: str, credential: Any = None
) -> list[ResourceRecord]:
    from azure.mgmt.resourcegraph import ResourceGraphClient
    from azure.mgmt.resourcegraph.models import QueryRequest, QueryRequestOptions

    from ..auth import read_credential

    graph = client or ResourceGraphClient(credential or read_credential())
    rows: list[dict[str, Any]] = []
    skip_token: str | None = None
    while True:
        options = QueryRequestOptions(top=1000, skip_token=skip_token)
        request = QueryRequest(subscriptions=[subscription_id], query=_RG_QUERY, options=options)
        response = graph.resources(request)
        rows.extend(list(response.data))
        skip_token = getattr(response, "skip_token", None)
        if not skip_token:
            break
    REGISTRY.set("inventory", ok=True)
    return _to_records(rows, subscription_id)
