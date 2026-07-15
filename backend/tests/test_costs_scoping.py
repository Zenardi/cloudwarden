"""Postgres-backed tests for day-window + provider scoping of the cost rollups.

Uses the `db` fixture (real Postgres via testcontainers). Rows sit well clear of
the window boundary so a ±1 day clock skew between the container's
``CURRENT_DATE`` and the test's ``date.today()`` can't reclassify them.
"""

from __future__ import annotations

import datetime as dt

# provider -> its subscription id (the cost→provider mapping goes through subscriptions).
SUBS = {"azure": "sub-az", "aws": "sub-aws", "gcp": "sub-gcp"}


def _seed_subs() -> None:
    from cloudwarden.storage import schema
    from cloudwarden.storage.db import session_scope

    with session_scope() as s:
        for prov, sid in SUBS.items():
            s.add(
                schema.Subscription(subscription_id=sid, display_name=f"{prov} sub", provider=prov)
            )


def _seed_costs(rows: list[tuple[int, float, str, str, str]]) -> None:
    """Each row is ``(day_offset, cost, provider, resource_type, location)``."""
    from cloudwarden.storage import schema
    from cloudwarden.storage.db import session_scope

    today = dt.date.today()
    with session_scope() as s:
        for i, (offset, cost, prov, rtype, loc) in enumerate(rows):
            s.add(
                schema.CostSnapshot(
                    usage_date=today - dt.timedelta(days=offset),
                    resource_id=f"/{prov}/r-{i}",
                    meter_category="Compute",
                    cost_type="Amortized",
                    subscription_id=SUBS[prov],
                    resource_type=rtype,
                    location=loc,
                    cost=cost,
                    currency="USD",
                )
            )


def _total(days: int = 30, provider: str | None = None) -> float:
    from cloudwarden.storage import repository as repo
    from cloudwarden.storage.db import session_scope

    with session_scope() as s:
        return repo.total_cost(s, days=days, provider=provider)


def test_cost_summary_scoped_by_days_window(db) -> None:
    _seed_subs()
    _seed_costs([(2, 10.0, "azure", "vm", "eastus"), (40, 5.0, "azure", "vm", "eastus")])
    assert _total(30) == 10.0  # 40-day-old row is outside the 30-day window
    assert _total(90) == 15.0  # ...but inside the 90-day window


def test_cost_summary_scoped_by_provider(db) -> None:
    _seed_subs()
    _seed_costs(
        [
            (1, 10.0, "azure", "vm", "eastus"),
            (1, 7.0, "aws", "ec2", "us-east-1"),
            (1, 3.0, "gcp", "gce", "us-central1"),
        ]
    )
    assert _total(30, "azure") == 10.0
    assert _total(30, "aws") == 7.0
    assert _total(30, None) == 20.0  # all clouds


def test_cost_by_type_and_region_respect_days_and_provider(db) -> None:
    from cloudwarden.storage import repository as repo
    from cloudwarden.storage.db import session_scope

    _seed_subs()
    _seed_costs(
        [
            (1, 10.0, "azure", "vm", "eastus"),
            (1, 6.0, "aws", "ec2", "us-east-1"),
            (40, 99.0, "azure", "vm", "eastus"),  # outside the 30-day window
        ]
    )
    with session_scope() as s:
        by_type_az = repo.cost_by_type(s, days=30, provider="azure")
        by_region_aws = repo.cost_by_region(s, days=30, provider="aws")
        by_type_all = repo.cost_by_type(s, days=30, provider=None)

    assert by_type_az == [{"resource_type": "vm", "cost": 10.0, "currency": "USD"}]
    assert by_region_aws == [{"location": "us-east-1", "cost": 6.0, "currency": "USD"}]
    assert {r["resource_type"] for r in by_type_all} == {"vm", "ec2"}


def test_api_costs_rejects_invalid_provider(db) -> None:
    from fastapi.testclient import TestClient

    from cloudwarden.api.main import app

    c = TestClient(app)
    for path in ("/api/costs/summary", "/api/costs/by-type", "/api/costs/by-region"):
        assert c.get(f"{path}?provider=xyz").status_code == 400


def test_api_costs_days_clamped(db) -> None:
    from fastapi.testclient import TestClient

    from cloudwarden.api.main import app

    _seed_subs()
    _seed_costs([(0, 10.0, "azure", "vm", "eastus"), (200, 5.0, "azure", "vm", "eastus")])
    c = TestClient(app)

    assert c.get("/api/costs/summary?days=1000").json()["total"] == 15.0  # clamped to 365
    assert c.get("/api/costs/summary?days=30").json()["total"] == 10.0
    # days below 1 clamps up to 1 (not a negative/future window that returns nothing).
    assert c.get("/api/costs/summary?days=-5").json()["total"] == 10.0
