"""Postgres-backed tests for the cost-trend endpoint (repo + API).

Uses the `db` fixture (throwaway PostgreSQL via testcontainers). Rows are
placed well clear of the 30-day window boundary so a ±1 day skew between the
container's ``CURRENT_DATE`` and the test process's ``date.today()`` can never
flip a row between the current and prior windows.
"""

from __future__ import annotations

import datetime as dt


def _seed(rows: list[tuple[int, float, str]], *, currency: str = "USD") -> None:
    """Insert cost rows. Each row is ``(day_offset, cost, cost_type)`` where
    ``usage_date = today - day_offset``. Resource ids are made unique per row so
    same-day rows are distinct primary keys and get summed by the query."""
    from cloudwarden.storage import schema
    from cloudwarden.storage.db import session_scope

    today = dt.date.today()
    with session_scope() as s:
        for i, (offset, cost, ctype) in enumerate(rows):
            s.add(
                schema.CostSnapshot(
                    usage_date=today - dt.timedelta(days=offset),
                    resource_id=f"/sub/x/vm-{i}",
                    meter_category="Compute",
                    cost_type=ctype,
                    cost=cost,
                    currency=currency,
                )
            )


# Amortized rows clearly inside the current 30-day window (total 60.0).
CURRENT = [(0, 10.0, "Amortized"), (1, 20.0, "Amortized"), (2, 30.0, "Amortized")]
# Amortized rows clearly inside the prior 30-day window (total 20.0).
PRIOR = [(40, 5.0, "Amortized"), (45, 15.0, "Amortized")]


def _trend(days: int = 30) -> dict:
    from cloudwarden.storage import repository as repo
    from cloudwarden.storage.db import session_scope

    with session_scope() as s:
        return repo.cost_trend(s, days=days)


def test_cost_trend_total_and_prior_windows(db) -> None:
    _seed(CURRENT + PRIOR)
    result = _trend(30)
    assert result["total"] == 60.0
    assert result["prior_total"] == 20.0


def test_cost_trend_delta_and_pct(db) -> None:
    _seed(CURRENT + PRIOR)
    result = _trend(30)
    assert result["delta"] == 40.0
    assert result["delta_pct"] == 200.0


def test_cost_trend_delta_pct_null_when_prior_zero(db) -> None:
    _seed(CURRENT)  # no prior-window rows
    result = _trend(30)
    assert result["prior_total"] == 0.0
    assert result["delta"] == 60.0
    assert result["delta_pct"] is None


def test_cost_trend_series_is_daily_ascending_iso(db) -> None:
    _seed(
        [
            (2, 10.0, "Amortized"),
            (1, 20.0, "Amortized"),
            (0, 5.0, "Amortized"),
            (0, 5.0, "Amortized"),  # same day -> summed with the row above
        ]
    )
    series = _trend(30)["series"]
    today = dt.date.today()
    assert [item["date"] for item in series] == [
        (today - dt.timedelta(days=2)).isoformat(),
        (today - dt.timedelta(days=1)).isoformat(),
        today.isoformat(),
    ]
    assert all(isinstance(item["date"], str) for item in series)
    assert series[-1]["cost"] == 10.0  # 5.0 + 5.0 for today


def test_cost_trend_excludes_actual_cost_type(db) -> None:
    _seed(CURRENT + [(1, 999.0, "Actual")])
    result = _trend(30)
    assert result["total"] == 60.0
    assert all(item["cost"] != 999.0 for item in result["series"])


def test_cost_trend_empty_db_returns_zeros_and_null_pct(db) -> None:
    result = _trend(30)
    assert result["total"] == 0.0
    assert result["prior_total"] == 0.0
    assert result["delta"] == 0.0
    assert result["delta_pct"] is None
    assert result["series"] == []
    assert result["currency"] == "USD"


def test_api_costs_trend_shape(db) -> None:
    from fastapi.testclient import TestClient

    from cloudwarden.api.main import app

    _seed(CURRENT + PRIOR)
    body = TestClient(app).get("/api/costs/trend?days=30").json()
    assert set(body) == {
        "days",
        "currency",
        "total",
        "prior_total",
        "delta",
        "delta_pct",
        "series",
    }
    assert body["days"] == 30
    assert body["currency"] == "USD"
    assert body["total"] == 60.0
    assert body["prior_total"] == 20.0
    assert body["delta_pct"] == 200.0
    assert all(set(item) == {"date", "cost"} for item in body["series"])


def test_api_costs_trend_days_clamped_1_to_365(db) -> None:
    from fastapi.testclient import TestClient

    from cloudwarden.api.main import app

    c = TestClient(app)
    assert c.get("/api/costs/trend?days=1000").json()["days"] == 365
    assert c.get("/api/costs/trend?days=0").json()["days"] == 1
    assert c.get("/api/costs/trend?days=-5").json()["days"] == 1
