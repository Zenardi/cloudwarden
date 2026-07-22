"""Regression: bulk upserts must chunk under Postgres's 65535 bound-param cap.

A single ``INSERT ... VALUES`` with a wide payload (columns × rows) that exceeds
65535 parameters fails with ``psycopg.OperationalError: number of parameters must
be between 0 and 65535``. This silently broke cost collection once the lookback
window grew large enough (180 days × resources × 2 cost types × 11 columns), which
in turn froze the Overview monthly chart. ``upsert_cost_snapshots`` (and its
siblings) now chunk their rows; these tests guard that.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import func, select

from cloudwarden import models as m
from cloudwarden.storage import repository as repo
from cloudwarden.storage import schema
from cloudwarden.storage.db import session_scope
from cloudwarden.storage.repository import _PG_MAX_BIND_PARAMS, _rows_per_statement


def test_rows_per_statement_stays_under_pg_cap() -> None:
    for columns in (1, 8, 11, 40, 100, 65535):
        rows = _rows_per_statement(columns)
        assert rows >= 1
        assert rows * columns <= _PG_MAX_BIND_PARAMS


def test_upsert_cost_snapshots_chunks_beyond_param_cap(db) -> None:
    """6500 rows × 11 columns = 71,500 bound params > 65,535 — the pre-fix single
    statement raised OperationalError; chunking lands them all across statements."""
    sub = "sub-bulk"
    day = dt.date(2026, 1, 15)
    n = 6500  # one chunk holds 65535 // 11 = 5957 rows, so this needs two
    rows = [
        m.CostRow(
            usage_date=day,
            resource_id=f"/subscriptions/{sub}/r{i}",
            subscription_id=sub,
            resource_type="microsoft.compute/virtualmachines",
            location="eastus",
            service_name="Virtual Machines",
            meter_category="Compute",
            cost=1.0,
            currency="USD",
            cost_type="Amortized",
        )
        for i in range(n)
    ]

    with session_scope() as s:
        written = repo.upsert_cost_snapshots(s, rows)
    assert written == n

    with session_scope() as s:
        stored = s.scalar(select(func.count()).select_from(schema.CostSnapshot))
    assert stored == n


def test_upsert_cost_snapshots_chunk_boundary_is_idempotent(db) -> None:
    """Re-upserting the same >1-chunk batch updates in place (ON CONFLICT), so the
    row count is unchanged — chunking must not break the idempotency guarantee."""
    sub = "sub-bulk-2"
    day = dt.date(2026, 2, 15)
    n = 6100
    rows = [
        m.CostRow(
            usage_date=day,
            resource_id=f"/subscriptions/{sub}/r{i}",
            subscription_id=sub,
            meter_category="Compute",
            cost=2.0,
            currency="USD",
            cost_type="Amortized",
        )
        for i in range(n)
    ]

    with session_scope() as s:
        repo.upsert_cost_snapshots(s, rows)
    for r in rows:  # second pass changes the cost; must update, not duplicate
        r.cost = 3.0
    with session_scope() as s:
        repo.upsert_cost_snapshots(s, rows)

    with session_scope() as s:
        stored = s.scalar(
            select(func.count())
            .select_from(schema.CostSnapshot)
            .where(schema.CostSnapshot.subscription_id == sub)
        )
        sample = s.scalar(
            select(schema.CostSnapshot.cost).where(
                schema.CostSnapshot.resource_id == f"/subscriptions/{sub}/r0"
            )
        )
    assert stored == n
    assert float(sample) == 3.0
