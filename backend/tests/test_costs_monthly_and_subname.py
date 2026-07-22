"""Postgres-backed tests for the monthly cost rollup (#Overview chart) and the
subscription display-name backfill. Uses the `db` fixture (real Postgres via
testcontainers). Cost rows sit mid-month (day 15) so a ±1 day clock skew between
the container's ``CURRENT_DATE`` and the test's ``date.today()`` can't move a row
across a month boundary.
"""

from __future__ import annotations

import datetime as dt

from cloudwarden.storage import repository as repo
from cloudwarden.storage import schema
from cloudwarden.storage.db import session_scope

_TODAY = dt.date.today()


def _mid_month(k: int) -> dt.date:
    """Day 15 of the month ``k`` calendar months before the current one."""
    y, m = _TODAY.year, _TODAY.month - k
    while m <= 0:
        m += 12
        y -= 1
    return dt.date(y, m, 15)


def _ym(k: int) -> str:
    d = _mid_month(k)
    return f"{d.year:04d}-{d.month:02d}"


def _seed_cost(
    day: dt.date, cost: float, provider: str, sub_id: str, currency: str = "USD"
) -> None:
    with session_scope() as s:
        if s.get(schema.Subscription, sub_id) is None:
            s.add(
                schema.Subscription(subscription_id=sub_id, display_name=sub_id, provider=provider)
            )
        s.add(
            schema.CostSnapshot(
                usage_date=day,
                resource_id=f"/{sub_id}/{day.isoformat()}-{cost}",
                meter_category="Compute",
                cost_type="Amortized",
                subscription_id=sub_id,
                resource_type="vm",
                location="eastus",
                cost=cost,
                currency=currency,
            )
        )


def _monthly(months: int, provider: str | None = None) -> dict:
    with session_scope() as s:
        return repo.cost_monthly(s, months=months, provider=provider)


def test_cost_monthly_buckets_by_calendar_month(db) -> None:
    _seed_cost(_mid_month(0), 100.0, "azure", "sub-az")
    _seed_cost(_mid_month(0), 20.0, "azure", "sub-az")  # same month → summed
    _seed_cost(_mid_month(1), 50.0, "azure", "sub-az")
    _seed_cost(_mid_month(2), 10.0, "azure", "sub-az")

    result = _monthly(months=3)
    by_month = {p["month"]: p["cost"] for p in result["series"]}
    assert by_month == {_ym(0): 120.0, _ym(1): 50.0, _ym(2): 10.0}
    # Series is ordered oldest → newest for a left-to-right chart.
    assert [p["month"] for p in result["series"]] == [_ym(2), _ym(1), _ym(0)]


def test_cost_monthly_window_excludes_older_months(db) -> None:
    _seed_cost(_mid_month(0), 100.0, "azure", "sub-az")
    _seed_cost(_mid_month(2), 10.0, "azure", "sub-az")  # 2 months back
    # months=1 → only the current month is in the window.
    assert {p["month"] for p in _monthly(months=1)["series"]} == {_ym(0)}


def test_cost_monthly_provider_filter(db) -> None:
    _seed_cost(_mid_month(0), 100.0, "azure", "sub-az")
    _seed_cost(_mid_month(0), 7.0, "aws", "sub-aws")
    assert _monthly(months=3, provider="azure")["series"][0]["cost"] == 100.0
    assert _monthly(months=3, provider="aws")["series"][0]["cost"] == 7.0


def test_cost_monthly_clamps_months(db) -> None:
    assert _monthly(months=100)["months"] == 24
    assert _monthly(months=0)["months"] == 1
    assert _monthly(months=-5)["months"] == 1


def test_cost_monthly_empty(db) -> None:
    result = _monthly(months=6)
    assert result["series"] == []
    assert result["currency"] == "USD"


# --- display-name backfill -------------------------------------------------- #

_SUB = "3669ff6e-73ad-45ff-adc4-809d0fbe6af5"


def _add_sub(name: str) -> None:
    with session_scope() as s:
        s.add(schema.Subscription(subscription_id=_SUB, display_name=name, provider="azure"))


def test_backfill_replaces_placeholder(db) -> None:
    _add_sub(repo._auto_display_name(_SUB))  # the seed placeholder
    with session_scope() as s:
        assert repo.backfill_display_name(s, _SUB, "BD-AMA-Sandbox") is True
    with session_scope() as s:
        assert s.get(schema.Subscription, _SUB).display_name == "BD-AMA-Sandbox"


def test_backfill_never_clobbers_user_name(db) -> None:
    _add_sub("Production")  # a name the user chose
    with session_scope() as s:
        assert repo.backfill_display_name(s, _SUB, "BD-AMA-Sandbox") is False
    with session_scope() as s:
        assert s.get(schema.Subscription, _SUB).display_name == "Production"


def test_backfill_ignores_blank_name(db) -> None:
    _add_sub(repo._auto_display_name(_SUB))
    with session_scope() as s:
        assert repo.backfill_display_name(s, _SUB, "") is False
        assert repo.backfill_display_name(s, _SUB, "   ") is False
        assert repo.backfill_display_name(s, _SUB, None) is False


def test_is_auto_display_name(db) -> None:
    _add_sub(repo._auto_display_name(_SUB))
    with session_scope() as s:
        assert repo.is_auto_display_name(s.get(schema.Subscription, _SUB)) is True
        s.get(schema.Subscription, _SUB).display_name = "Renamed"
        assert repo.is_auto_display_name(s.get(schema.Subscription, _SUB)) is False
