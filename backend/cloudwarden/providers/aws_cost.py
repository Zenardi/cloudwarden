"""AWS cost via the Cost Explorer ``get_cost_and_usage`` API (M14.11).

The AWS analogue of :mod:`cloudwarden.azure.cost`: it returns the **same**
normalized :class:`~cloudwarden.models.CostRow` shape (amortized by default) so
every downstream analytic — budgets, anomaly, forecast, showback — is
provider-agnostic. Cost Explorer caps a query at two ``GroupBy`` dimensions (the
same limit the Azure Query API has), so we group by ``RESOURCE_ID`` +
``SERVICE`` and parse the **region straight out of each resource ARN** rather
than spending the third dimension on it.

Everything stays offline in tests and mock mode: the collector talks to an
**injected**/fixture-backed client shaped like ``boto3``'s Cost Explorer client
(``get_cost_and_usage(**request) -> page``). ``NextPageToken`` pagination and
429/throttle retries mirror the Azure collector's resilience. The live boto3
client is built lazily and marked ``# pragma: no cover``.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from collections.abc import Callable
from typing import Any

from ..azure.context import AccountContext
from ..config import Settings, get_settings
from ..models import CostRow
from ..resilience import REGISTRY, with_retry
from .aws import AWS_PLACEHOLDER_ACCOUNT, DEFAULT_REGION

logger = logging.getLogger("cloudwarden.providers.aws_cost")

PROVIDER = "aws"
_FIXTURE_NAME = "aws_cost"
_METRIC = "AmortizedCost"


def collect_cost(
    client: Any = None,
    account: AccountContext | None = None,
    *,
    settings: Settings | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> list[CostRow]:
    """Collect amortized AWS cost rows for an account (offline unless live).

    ``client`` (when given) is used directly — the live-shaped path exercised by
    tests. Otherwise mock mode replays the recorded fixture through the same
    parser; the live boto3 Cost Explorer client is built only outside mock mode.
    """
    settings = settings or get_settings()
    account_id = account.account_id if account is not None else settings.aws_account_id
    if client is None:
        client = _mock_client() if settings.finops_mock else _live_client(account)
    rows = _collect_via_client(client, account_id, settings, sleep=sleep)
    REGISTRY.set("cost_aws", ok=True)
    return rows


def _retarget(value: str | None, account_id: str) -> str | None:
    """Rewrite the placeholder account segment of an ARN (parity with providers.aws)."""
    if not value or not account_id or account_id == AWS_PLACEHOLDER_ACCOUNT:
        return value
    return value.replace(AWS_PLACEHOLDER_ACCOUNT, account_id)


def _region_from_arn(arn: str | None) -> str | None:
    """Region is the 4th colon-field of an ARN (``arn:partition:service:region:...``).

    Global resources (e.g. S3 ``arn:aws:s3:::bucket``) carry no region → ``None``.
    """
    if not arn:
        return None
    parts = arn.split(":")
    return (parts[3] or None) if len(parts) > 3 else None


def _parse_date(value: Any) -> dt.date:
    if not value:
        return dt.date.today()
    return dt.date.fromisoformat(str(value)[:10])


def _parse_page(page: dict[str, Any], account_id: str) -> list[CostRow]:
    """Normalize one Cost Explorer page into ``CostRow``s (tags enriched later)."""
    rows: list[CostRow] = []
    for bucket in page.get("ResultsByTime", []):
        usage_date = _parse_date(bucket.get("TimePeriod", {}).get("Start"))
        for group in bucket.get("Groups", []):
            keys = group.get("Keys", [])
            resource_id = _retarget(keys[0], account_id) if keys else None
            service = keys[1] if len(keys) > 1 else None
            metric = group.get("Metrics", {}).get(_METRIC, {})
            rows.append(
                CostRow(
                    usage_date=usage_date,
                    resource_id=resource_id,
                    subscription_id=account_id,
                    provider=PROVIDER,
                    location=_region_from_arn(resource_id),
                    service_name=service,
                    cost=float(metric.get("Amount") or 0.0),
                    currency=metric.get("Unit") or "USD",
                    cost_type="Amortized",
                )
            )
    return rows


def _query(account_id: str, settings: Settings) -> dict[str, Any]:
    """Build the base ``get_cost_and_usage`` request (Daily, AmortizedCost, per resource)."""
    today = dt.date.today()
    start = today - dt.timedelta(days=max(settings.cost_lookback_days, 1))
    return {
        "TimePeriod": {"Start": start.isoformat(), "End": today.isoformat()},
        "Granularity": "DAILY",
        "Metrics": [_METRIC],
        "GroupBy": [
            {"Type": "DIMENSION", "Key": "RESOURCE_ID"},
            {"Type": "DIMENSION", "Key": "SERVICE"},
        ],
    }


def _collect_via_client(
    client: Any, account_id: str, settings: Settings, *, sleep: Callable[[float], None]
) -> list[CostRow]:
    base = _query(account_id, settings)
    # Cost Explorer throttles aggressively (429). Retry per-page — never around the
    # whole collection — so a throttled later page can't re-fire pages that already
    # succeeded. Same budget/ceiling as the Azure cost collector's resilience.
    fetch = with_retry(max_attempts=6, base_delay=2.0, max_delay=90.0, sleep=sleep)(
        client.get_cost_and_usage
    )
    rows: list[CostRow] = []
    token: str | None = None
    while True:
        request = {**base, **({"NextPageToken": token} if token else {})}
        page = fetch(**request)
        rows.extend(_parse_page(page, account_id))
        token = page.get("NextPageToken")
        if not token:
            break
    return rows


class _FixtureCostExplorer:
    """Offline stand-in shaped like boto3's Cost Explorer client (single page)."""

    def get_cost_and_usage(self, **request: Any) -> dict[str, Any]:  # noqa: ARG002 - boto parity
        from ..azure._fixtures import load_fixture

        return load_fixture(_FIXTURE_NAME)


def _mock_client() -> _FixtureCostExplorer:
    return _FixtureCostExplorer()


def _live_client(account: AccountContext | None) -> Any:  # pragma: no cover - requires live AWS
    """Build a real boto3 Cost Explorer client from the account credential."""
    import boto3

    credential = getattr(account, "credential", None) or {}
    kwargs: dict[str, Any] = {"region_name": credential.get("region") or DEFAULT_REGION}
    if credential.get("access_key_id") and credential.get("secret_access_key"):
        kwargs["aws_access_key_id"] = credential["access_key_id"]
        kwargs["aws_secret_access_key"] = credential["secret_access_key"]
    return boto3.client("ce", **kwargs)
