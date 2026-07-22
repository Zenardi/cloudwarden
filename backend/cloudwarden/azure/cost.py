"""Cost via the Azure Cost Management Query API (raw REST; mock-backed).

The Query API caps groupings at 2 dimensions, so the live path groups by
``ResourceId`` + ``ServiceName`` and the orchestrator enriches each row's
resource_type/location from the inventory (joined on resource_id). Defaults to
Amortized cost for right-sizing (reservations/savings plans distort Actual for a
single resource). Honours ``nextLink`` pagination and retries on 429/5xx.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

import httpx

from ..config import Settings, get_settings
from ..models import CostRow
from ..resilience import REGISTRY, with_retry
from ._fixtures import load_fixture, retarget
from .context import SubscriptionContext

logger = logging.getLogger("cloudwarden.azure.cost")

_API_VERSION = "2024-08-01"


def collect_cost(
    client: Any = None, subscription: SubscriptionContext | None = None
) -> list[CostRow]:  # noqa: ARG001 - parity with other collectors
    settings = get_settings()
    sub_id = subscription.subscription_id if subscription else settings.azure_subscription_id
    if settings.finops_mock:
        rows = _mock_rows(settings, sub_id)
        REGISTRY.set("cost", ok=True)
        return rows
    cred = subscription.credential if subscription else None
    return _collect_live(settings, sub_id, cred)


def _mock_rows(settings: Settings, subscription_id: str) -> list[CostRow]:
    data = load_fixture("cost")
    currency = data.get("currency", "USD")
    today = dt.date.today()
    out: list[CostRow] = []
    for day_index in range(settings.cost_lookback_days):
        day = today - dt.timedelta(days=day_index)
        for res_index, res in enumerate(data["resources"]):
            factor = 1.0 + 0.05 * (((day_index + res_index) % 5) - 2)
            base = float(res["base_daily_cost"]) * factor
            for cost_type in ("Amortized", "Actual"):
                out.append(
                    CostRow(
                        usage_date=day,
                        resource_id=retarget(str(res["resource_id"]).lower(), subscription_id),
                        subscription_id=subscription_id,
                        resource_type=res.get("resource_type"),
                        location=res.get("location"),
                        service_name=res.get("service_name"),
                        meter_category=res.get("meter_category"),
                        cost=round(base, 4),
                        currency=currency,
                        cost_type=cost_type,
                    )
                )
    return out


def _query_url(subscription_id: str) -> str:
    return (
        f"https://management.azure.com/subscriptions/{subscription_id}"
        f"/providers/Microsoft.CostManagement/query?api-version={_API_VERSION}"
    )


# Azure Cost Management rejects a Custom/Daily query whose range reaches ~1 year
# with a bare 400. Clamp the daily window safely under that so an aggressive
# COST_LOOKBACK_DAYS (e.g. for the monthly chart) can never break collection.
_MAX_DAILY_LOOKBACK_DAYS = 363


def _query_body(lookback_days: int, cost_type: str) -> dict[str, Any]:
    lookback_days = min(max(lookback_days, 1), _MAX_DAILY_LOOKBACK_DAYS)
    today = dt.date.today()
    start = today - dt.timedelta(days=lookback_days)
    metric = "AmortizedCost" if cost_type == "Amortized" else "ActualCost"
    return {
        "type": metric,
        "timeframe": "Custom",
        "timePeriod": {"from": f"{start}T00:00:00Z", "to": f"{today}T23:59:59Z"},
        "dataset": {
            "granularity": "Daily",
            "aggregation": {"totalCost": {"name": "Cost", "function": "Sum"}},
            "grouping": [
                {"type": "Dimension", "name": "ResourceId"},
                {"type": "Dimension", "name": "ServiceName"},
            ],
        },
    }


def _parse_usage_date(value: Any) -> dt.date:
    if value is None:
        return dt.date.today()
    text = str(value)
    if len(text) == 8 and text.isdigit():
        return dt.date(int(text[:4]), int(text[4:6]), int(text[6:8]))
    return dt.date.fromisoformat(text[:10])


def _parse_response(
    payload: dict[str, Any], cost_type: str, subscription_id: str | None = None
) -> list[CostRow]:
    props = payload.get("properties", payload)
    columns = [c.get("name") for c in props.get("columns", [])]
    index = {name: i for i, name in enumerate(columns)}

    def col(row: list[Any], name: str) -> Any:
        i = index.get(name)
        return row[i] if i is not None and i < len(row) else None

    rows: list[CostRow] = []
    for raw in props.get("rows", []):
        rid = col(raw, "ResourceId")
        rows.append(
            CostRow(
                usage_date=_parse_usage_date(col(raw, "UsageDate")),
                resource_id=str(rid).lower() if rid else None,
                # Stamp the subscription being queried so cost is attributable to a
                # cloud/subscription — the provider filter joins on this. The query is
                # per-subscription, so every row belongs to subscription_id.
                subscription_id=subscription_id,
                service_name=col(raw, "ServiceName"),
                cost=float(col(raw, "Cost") or col(raw, "PreTaxCost") or 0.0),
                currency=col(raw, "Currency") or "USD",
                cost_type=cost_type,
            )
        )
    return rows


# Cost Management throttles aggressively (429). Retry per-request — not around the
# whole collection — so a throttled later query never re-fires the ones that already
# succeeded (which would only add more load). Give it a larger budget/ceiling than the
# default so a real throttle window (Azure hints 30–60s via x-ms-ratelimit-*-retry-after,
# now honoured in resilience._retry_after_seconds) is ridden out instead of failing.
@with_retry(max_attempts=6, base_delay=2.0, max_delay=90.0)
def _post_cost_query(
    http: httpx.Client, url: str, headers: dict[str, str], body: dict[str, Any]
) -> dict[str, Any]:
    resp = http.post(url, headers=headers, json=body)
    resp.raise_for_status()
    return resp.json()


def _collect_live(
    settings: Settings, subscription_id: str, credential: Any = None
) -> list[CostRow]:
    from ..auth import arm_token

    token = arm_token(credential)
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    url = _query_url(subscription_id)
    out: list[CostRow] = []
    with httpx.Client(timeout=60.0) as http:
        for cost_type in ("Amortized", "Actual"):
            body = _query_body(settings.cost_lookback_days, cost_type)
            next_url: str | None = url
            while next_url:
                payload = _post_cost_query(http, next_url, headers, body)
                out.extend(_parse_response(payload, cost_type, subscription_id))
                next_url = payload.get("properties", {}).get("nextLink")
    REGISTRY.set("cost", ok=True)
    return out
