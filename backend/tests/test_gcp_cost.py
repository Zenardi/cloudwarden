"""M14.11 — GCP BigQuery Billing Export collector (cost analytics parity, TDD-first).

Everything is exercised with **injected**/fixture-backed BigQuery clients — no
test ever reaches GCP. Covers fixture normalization into the shared ``CostRow``
schema (provider='gcp', amortized default, region + labels straight from the
export), page-token pagination, throttle retries, and the empty-export case.
"""

from __future__ import annotations

from cloudwarden.azure.context import AccountContext
from cloudwarden.models import CostRow
from cloudwarden.providers import gcp_cost

_PROJECT = "acme-prod-42"


def _ctx(project_id: str = _PROJECT) -> AccountContext:
    return AccountContext(account_id=project_id, provider="gcp")


# --- Fake BigQuery billing clients (never touch GCP) ----------------------- #
class _FakeBQ:
    """Returns preset billing-export pages in call order, recording page tokens."""

    def __init__(self, pages: list[dict]) -> None:
        self._pages = list(pages)
        self.tokens: list[str | None] = []

    def query_billing(
        self, *, project_id: str, lookback_days: int, page_token: str | None = None
    ) -> dict:
        self.tokens.append(page_token)
        return self._pages[len(self.tokens) - 1]


class _Throttle(Exception):
    """A retryable BigQuery rate-limit (HTTP 429) — resilience.with_retry retries it."""

    status_code = 429


class _ThrottleThenOk:
    def __init__(self, page: dict) -> None:
        self._page = page
        self.attempts = 0

    def query_billing(
        self, *, project_id: str, lookback_days: int, page_token: str | None = None
    ) -> dict:
        self.attempts += 1
        if self.attempts == 1:
            raise _Throttle("rateLimitExceeded")
        return self._page


def _row(
    resource_name: str, service: str, region: str, cost: float, labels: dict | None = None
) -> dict:
    return {
        "usage_date": "2026-07-20",
        "resource_name": resource_name,
        "service": service,
        "region": region,
        "cost": cost,
        "currency": "USD",
        "labels": labels or {},
    }


def _page(rows: list[dict], *, token: str | None = None) -> dict:
    return {"rows": rows, "next_page_token": token}


# --------------------------------------------------------------------------- #
# Fixture normalization (mock mode) -> shared CostRow schema
# --------------------------------------------------------------------------- #
def test_gcp_cost_normalized_rows() -> None:
    rows = gcp_cost.collect_cost(account=_ctx())
    assert rows and all(isinstance(r, CostRow) for r in rows)
    assert all(r.provider == "gcp" for r in rows)
    assert all(r.cost_type == "Amortized" for r in rows)
    web = next(r for r in rows if r.resource_id and r.resource_id.endswith("instances/web-01"))
    assert web.service_name == "Compute Engine"
    assert web.location == "us-central1"
    assert web.currency == "USD"
    assert web.cost > 0
    # Labels flow straight through to tags (showback dimension) — no enrichment needed.
    assert web.tags == {"env": "prod", "team": "web"}
    # The placeholder project is retargeted onto every resource id.
    assert all(_PROJECT in (r.resource_id or "") for r in rows)
    assert all("example-project-123456" not in (r.resource_id or "") for r in rows)


def test_gcp_cost_injected_client_takes_precedence() -> None:
    client = _FakeBQ(
        [_page([_row("//compute/projects/example-project-123456/x", "GCE", "us-east1", 3.0)])]
    )
    rows = gcp_cost.collect_cost(client=client, account=_ctx())
    assert len(rows) == 1
    assert rows[0].location == "us-east1"
    assert _PROJECT in rows[0].resource_id
    assert client.tokens == [None]


# --------------------------------------------------------------------------- #
# Pagination — next_page_token threaded until exhausted
# --------------------------------------------------------------------------- #
def test_gcp_pagination_returns_all_rows() -> None:
    p1 = _page(
        [_row("//compute/projects/example-project-123456/a", "GCE", "us-central1", 1.0)],
        token="pg2",
    )
    p2 = _page([_row("//compute/projects/example-project-123456/b", "GCE", "us-central1", 2.0)])
    client = _FakeBQ([p1, p2])
    rows = gcp_cost.collect_cost(client=client, account=_ctx())
    assert len(rows) == 2
    assert client.tokens == [None, "pg2"]  # second call carried the first page's token


# --------------------------------------------------------------------------- #
# Throttling — 429 is retried
# --------------------------------------------------------------------------- #
def test_gcp_throttle_retried() -> None:
    page = _page([_row("//compute/projects/example-project-123456/a", "GCE", "us-central1", 1.0)])
    client = _ThrottleThenOk(page)
    rows = gcp_cost.collect_cost(client=client, account=_ctx(), sleep=lambda _s: None)
    assert client.attempts == 2
    assert len(rows) == 1


# --------------------------------------------------------------------------- #
# Empty billing export -> no rows
# --------------------------------------------------------------------------- #
def test_gcp_empty_export_yields_no_rows() -> None:
    assert gcp_cost.collect_cost(client=_FakeBQ([_page([])]), account=_ctx()) == []


def test_gcp_cost_missing_date_tolerated() -> None:
    import datetime as dt

    page = _page(
        [{"resource_name": "//x", "service": "GCE", "region": "us", "cost": 1.0, "currency": "USD"}]
    )
    rows = gcp_cost.collect_cost(client=_FakeBQ([page]), account=_ctx())
    assert rows[0].usage_date == dt.date.today()  # missing usage_date -> today


def test_gcp_cost_placeholder_project_not_retargeted() -> None:
    rows = gcp_cost.collect_cost(account=_ctx("example-project-123456"))
    assert any("example-project-123456" in (r.resource_id or "") for r in rows)


def test_gcp_cost_defaults_project_from_settings(monkeypatch) -> None:
    from cloudwarden.config import get_settings

    monkeypatch.setenv("GCP_PROJECT_ID", "billing-proj-9")
    get_settings.cache_clear()
    rows = gcp_cost.collect_cost()
    assert rows and all("billing-proj-9" in (r.resource_id or "") for r in rows)
    get_settings.cache_clear()
