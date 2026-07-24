"""M14.11 — AWS Cost Explorer collector (cost analytics parity, TDD-first).

Everything is exercised with **injected**/fixture-backed Cost Explorer clients —
no test ever reaches AWS. Covers fixture normalization into the shared ``CostRow``
schema (provider='aws', amortized default, region parsed from the ARN),
``NextPageToken`` pagination, throttle retries, and the empty-export case.
"""

from __future__ import annotations

import pytest

from cloudwarden.azure.context import AccountContext
from cloudwarden.models import CostRow
from cloudwarden.providers import aws_cost

_ACCOUNT = "111122223333"


def _ctx(account_id: str = _ACCOUNT) -> AccountContext:
    return AccountContext(account_id=account_id, provider="aws")


# --- Fake Cost Explorer clients (boto-shaped; never touch AWS) -------------- #
class _FakeCE:
    """Returns preset ``get_cost_and_usage`` pages in call order, recording requests."""

    def __init__(self, pages: list[dict]) -> None:
        self._pages = list(pages)
        self.requests: list[dict] = []

    def get_cost_and_usage(self, **request: object) -> dict:
        self.requests.append(request)
        return self._pages[len(self.requests) - 1]


class _Throttle(Exception):
    """A retryable Cost Explorer throttle (HTTP 429) — resilience.with_retry retries it."""

    status_code = 429


class _ThrottleThenOk:
    """Raises a throttle on the first call, then returns the page."""

    def __init__(self, page: dict) -> None:
        self._page = page
        self.attempts = 0

    def get_cost_and_usage(self, **request: object) -> dict:
        self.attempts += 1
        if self.attempts == 1:
            raise _Throttle("Rate exceeded")
        return self._page


def _page(groups: list[dict], *, start: str = "2026-07-20", token: str | None = None) -> dict:
    page: dict = {"ResultsByTime": [{"TimePeriod": {"Start": start}, "Groups": groups}]}
    if token:
        page["NextPageToken"] = token
    return page


def _group(resource_id: str, service: str, amount: str) -> dict:
    return {
        "Keys": [resource_id, service],
        "Metrics": {"AmortizedCost": {"Amount": amount, "Unit": "USD"}},
    }


# --------------------------------------------------------------------------- #
# Fixture normalization (mock mode) -> shared CostRow schema
# --------------------------------------------------------------------------- #
def test_aws_cost_normalized_rows() -> None:
    # Act — mock mode replays the recorded fixture through the collector.
    rows = aws_cost.collect_cost(account=_ctx())
    # Assert — provider-neutral CostRow objects tagged aws, amortized default.
    assert rows and all(isinstance(r, CostRow) for r in rows)
    assert all(r.provider == "aws" for r in rows)
    assert all(r.cost_type == "Amortized" for r in rows)
    ec2 = next(r for r in rows if "i-0a1b2c3d4e5f6a7b8" in (r.resource_id or ""))
    assert ec2.service_name == "Amazon Elastic Compute Cloud - Compute"
    assert ec2.location == "us-east-1"  # region parsed from the ARN
    assert ec2.currency == "USD"
    assert ec2.cost > 0
    # The placeholder account is retargeted onto every ARN.
    assert all(_ACCOUNT in (r.resource_id or "") for r in rows)
    assert all("123456789012" not in (r.resource_id or "") for r in rows)


def test_aws_cost_global_arn_has_no_region() -> None:
    # The S3 bucket ARN (arn:aws:s3:::bucket) carries no region segment.
    rows = aws_cost.collect_cost(account=_ctx())
    s3 = next(r for r in rows if "finops-artifacts" in (r.resource_id or ""))
    assert s3.location is None
    assert s3.service_name == "Amazon Simple Storage Service"


def test_aws_cost_injected_client_takes_precedence() -> None:
    # A client passed explicitly is used even in mock mode (the live-shaped path).
    client = _FakeCE(
        [_page([_group("arn:aws:ec2:us-west-2:123456789012:instance/i-x", "EC2", "2.5")])]
    )
    rows = aws_cost.collect_cost(client=client, account=_ctx())
    assert len(rows) == 1
    assert rows[0].location == "us-west-2"
    assert _ACCOUNT in rows[0].resource_id
    assert client.requests, "the injected client did the work"


# --------------------------------------------------------------------------- #
# Pagination — NextPageToken threaded until exhausted
# --------------------------------------------------------------------------- #
def test_pagination_returns_all_rows() -> None:
    p1 = _page(
        [_group("arn:aws:ec2:us-east-1:123456789012:instance/i-1", "EC2", "1.0")], token="tok"
    )
    p2 = _page([_group("arn:aws:ec2:us-east-1:123456789012:instance/i-2", "EC2", "2.0")])
    client = _FakeCE([p1, p2])
    rows = aws_cost.collect_cost(client=client, account=_ctx())
    ids = {r.resource_id for r in rows}
    assert len(rows) == 2
    assert any("i-1" in i for i in ids) and any("i-2" in i for i in ids)
    # Two calls; the second carried the NextPageToken from the first page.
    assert len(client.requests) == 2
    assert client.requests[1].get("NextPageToken") == "tok"


# --------------------------------------------------------------------------- #
# Throttling — 429 is retried (resilience.with_retry), no rows lost
# --------------------------------------------------------------------------- #
def test_throttle_retried() -> None:
    page = _page([_group("arn:aws:ec2:us-east-1:123456789012:instance/i-1", "EC2", "1.0")])
    client = _ThrottleThenOk(page)
    rows = aws_cost.collect_cost(client=client, account=_ctx(), sleep=lambda _s: None)
    assert client.attempts == 2  # first throttled, second succeeded
    assert len(rows) == 1


# --------------------------------------------------------------------------- #
# Empty billing export -> no rows (never an exception)
# --------------------------------------------------------------------------- #
def test_empty_export_yields_no_rows() -> None:
    assert aws_cost.collect_cost(client=_FakeCE([{"ResultsByTime": []}]), account=_ctx()) == []
    # An empty Groups list is equally a no-op.
    assert aws_cost.collect_cost(client=_FakeCE([_page([])]), account=_ctx()) == []


def test_aws_cost_missing_date_and_region_tolerated() -> None:
    # A group with empty Keys and a bucket with no TimePeriod exercises the
    # defensive parse paths — never an exception, just permissive Nones.
    import datetime as dt

    page = {
        "ResultsByTime": [
            {
                "Groups": [
                    {"Keys": [], "Metrics": {"AmortizedCost": {"Amount": "1.0", "Unit": "USD"}}}
                ]
            }
        ]
    }
    rows = aws_cost.collect_cost(client=_FakeCE([page]), account=_ctx())
    assert len(rows) == 1
    assert rows[0].resource_id is None
    assert rows[0].location is None
    assert rows[0].usage_date == dt.date.today()


def test_aws_cost_placeholder_account_not_retargeted() -> None:
    # Collecting for the placeholder account itself is a retarget no-op.
    rows = aws_cost.collect_cost(account=_ctx("123456789012"))
    assert any("123456789012" in (r.resource_id or "") for r in rows)


def test_aws_cost_defaults_account_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    # With no account context, the collector falls back to settings.aws_account_id.
    from cloudwarden.config import get_settings

    monkeypatch.setenv("AWS_ACCOUNT_ID", "444455556666")
    get_settings.cache_clear()
    rows = aws_cost.collect_cost()
    assert rows and all("444455556666" in (r.resource_id or "") for r in rows)
    get_settings.cache_clear()
