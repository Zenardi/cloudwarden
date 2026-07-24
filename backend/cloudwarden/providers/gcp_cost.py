"""GCP cost via the BigQuery Billing Export (M14.11).

The GCP analogue of :mod:`cloudwarden.azure.cost` / :mod:`cloudwarden.providers.aws_cost`:
it returns the **same** normalized :class:`~cloudwarden.models.CostRow` shape so
budgets/anomaly/forecast/showback are provider-agnostic. Unlike Cost Explorer /
the Azure Query API, BigQuery has no 2-dimension cap, so the query aggregates by
resource + service + region + day and the export's ``labels`` map straight onto
``tags`` (the showback dimension) — no inventory enrichment needed.

Everything stays offline in tests and mock mode: the collector talks to an
**injected**/fixture-backed client (``query_billing(*, project_id, lookback_days,
page_token) -> {"rows": [...], "next_page_token": ...}``). Page-token pagination
and 429/rate-limit retries mirror the other collectors. The live BigQuery client
is built lazily and marked ``# pragma: no cover``.
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
from .gcp import GCP_PLACEHOLDER_PROJECT

logger = logging.getLogger("cloudwarden.providers.gcp_cost")

PROVIDER = "gcp"
_FIXTURE_NAME = "gcp_cost"


def collect_cost(
    client: Any = None,
    account: AccountContext | None = None,
    *,
    settings: Settings | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> list[CostRow]:
    """Collect amortized GCP cost rows for a project (offline unless live).

    ``client`` (when given) is used directly — the live-shaped path exercised by
    tests. Otherwise mock mode replays the recorded fixture through the same
    parser; the live BigQuery client is built only outside mock mode.
    """
    settings = settings or get_settings()
    project_id = account.account_id if account is not None else settings.gcp_project_id
    if client is None:
        client = _mock_client() if settings.finops_mock else _live_client(account)
    rows = _collect_via_client(client, project_id, settings, sleep=sleep)
    REGISTRY.set("cost_gcp", ok=True)
    return rows


def _retarget(value: str | None, project_id: str) -> str | None:
    """Rewrite the placeholder project segment of a resource id (parity with providers.gcp)."""
    if not value or not project_id or project_id == GCP_PLACEHOLDER_PROJECT:
        return value
    return value.replace(GCP_PLACEHOLDER_PROJECT, project_id)


def _parse_date(value: Any) -> dt.date:
    if not value:
        return dt.date.today()
    return dt.date.fromisoformat(str(value)[:10])


def _parse_rows(rows: list[dict[str, Any]], project_id: str) -> list[CostRow]:
    """Normalize BigQuery Billing Export rows into ``CostRow``s (labels -> tags)."""
    out: list[CostRow] = []
    for row in rows:
        out.append(
            CostRow(
                usage_date=_parse_date(row.get("usage_date")),
                resource_id=_retarget(row.get("resource_name"), project_id),
                subscription_id=project_id,
                provider=PROVIDER,
                location=row.get("region"),
                service_name=row.get("service"),
                cost=float(row.get("cost") or 0.0),
                currency=row.get("currency") or "USD",
                cost_type="Amortized",
                tags=dict(row.get("labels") or {}),
            )
        )
    return out


def _collect_via_client(
    client: Any, project_id: str, settings: Settings, *, sleep: Callable[[float], None]
) -> list[CostRow]:
    lookback = max(settings.cost_lookback_days, 1)
    # BigQuery rate-limits (429) too. Retry per-page so a throttled later page never
    # re-runs the expensive query for pages that already returned.
    fetch = with_retry(max_attempts=6, base_delay=2.0, max_delay=90.0, sleep=sleep)(
        client.query_billing
    )
    rows: list[CostRow] = []
    token: str | None = None
    while True:
        page = fetch(project_id=project_id, lookback_days=lookback, page_token=token)
        rows.extend(_parse_rows(page.get("rows", []), project_id))
        token = page.get("next_page_token")
        if not token:
            break
    return rows


class _FixtureBillingClient:
    """Offline stand-in for the BigQuery billing reader (single page from the fixture)."""

    def query_billing(
        self, *, project_id: str, lookback_days: int, page_token: str | None = None
    ) -> dict[str, Any]:  # noqa: ARG002 - signature parity with the live client
        from ..azure._fixtures import load_fixture

        return load_fixture(_FIXTURE_NAME)


def _mock_client() -> _FixtureBillingClient:
    return _FixtureBillingClient()


def _live_client(account: AccountContext | None) -> Any:  # pragma: no cover - requires live GCP
    """Build a BigQuery-backed billing reader from the service-account credential.

    Reads the standard usage-cost export table named by ``GCP_BILLING_EXPORT_TABLE``.
    """
    from google.cloud import bigquery  # type: ignore
    from google.oauth2 import service_account  # type: ignore

    settings = get_settings()
    credential = getattr(account, "credential", None) or {}
    creds = None
    if credential.get("service_account_info"):
        creds = service_account.Credentials.from_service_account_info(
            credential["service_account_info"]
        )
    bq = bigquery.Client(project=getattr(account, "account_id", None), credentials=creds)
    table = settings.gcp_billing_export_table

    class _Adapter:
        def query_billing(
            self, *, project_id: str, lookback_days: int, page_token: str | None = None
        ) -> dict[str, Any]:
            sql = (
                "SELECT FORMAT_DATE('%Y-%m-%d', DATE(usage_start_time)) AS usage_date, "
                "resource.name AS resource_name, service.description AS service, "
                "location.region AS region, SUM(cost) AS cost, ANY_VALUE(currency) AS currency "
                f"FROM `{table}` "
                "WHERE DATE(usage_start_time) >= DATE_SUB(CURRENT_DATE(), "
                "INTERVAL @lookback DAY) AND project.id = @project "
                "GROUP BY usage_date, resource_name, service, region"
            )
            job = bq.query(
                sql,
                job_config=bigquery.QueryJobConfig(
                    query_parameters=[
                        bigquery.ScalarQueryParameter("lookback", "INT64", lookback_days),
                        bigquery.ScalarQueryParameter("project", "STRING", project_id),
                    ]
                ),
            )
            rows = [dict(r) for r in job.result()]
            return {"rows": rows, "next_page_token": None}

    return _Adapter()
