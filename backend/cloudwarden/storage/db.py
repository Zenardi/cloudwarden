"""Engine/session management and idempotent schema bootstrap.

`init_db()` creates the tables, then best-effort promotes the fact tables to
TimescaleDB hypertables and (re)creates the Grafana-facing SQL views. Every
optional/Timescale-specific step runs in its own transaction so a missing
extension degrades gracefully to plain Postgres instead of aborting the rest.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from ..config import get_settings
from .schema import Base

logger = logging.getLogger("cloudwarden.storage")

_engine: Engine | None = None
_session_factory: sessionmaker | None = None


def get_engine() -> Engine:
    global _engine, _session_factory
    if _engine is None:
        _engine = create_engine(get_settings().database_url, pool_pre_ping=True, future=True)
        _session_factory = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def get_session_factory() -> sessionmaker:
    get_engine()
    assert _session_factory is not None
    return _session_factory


@contextmanager
def session_scope() -> Iterator[Session]:
    factory = get_session_factory()
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


HYPERTABLES = [("cost_snapshots", "usage_date"), ("utilization_samples", "ts")]

_VIEWS_SQL = """
CREATE OR REPLACE VIEW v_cost_by_resource AS
SELECT resource_id, resource_type, location, resource_group,
       SUM(cost) AS cost, currency
FROM cost_snapshots
WHERE cost_type = 'Amortized' AND usage_date >= (CURRENT_DATE - INTERVAL '30 days')
GROUP BY resource_id, resource_type, location, resource_group, currency;

CREATE OR REPLACE VIEW v_cost_by_type AS
SELECT resource_type, SUM(cost) AS cost, currency
FROM cost_snapshots
WHERE cost_type = 'Amortized' AND usage_date >= (CURRENT_DATE - INTERVAL '30 days')
GROUP BY resource_type, currency;

CREATE OR REPLACE VIEW v_cost_by_region AS
SELECT location, SUM(cost) AS cost, currency
FROM cost_snapshots
WHERE cost_type = 'Amortized' AND usage_date >= (CURRENT_DATE - INTERVAL '30 days')
GROUP BY location, currency;

-- Showback/chargeback dimension (M14.5): unnest each cost row's tags into
-- (tag_key, tag_value) rows so Grafana can group spend by any tag key. Untagged
-- spend surfaces via the '(unallocated)' key so it is never silently dropped.
CREATE OR REPLACE VIEW v_cost_by_tag AS
SELECT COALESCE(t.key, '(unallocated)') AS tag_key,
       COALESCE(NULLIF(t.value, ''), '(unallocated)') AS tag_value,
       c.subscription_id, c.usage_date, c.cost, c.currency
FROM cost_snapshots c
LEFT JOIN LATERAL jsonb_each_text(c.tags) AS t(key, value) ON TRUE
WHERE c.cost_type = 'Amortized' AND c.usage_date >= (CURRENT_DATE - INTERVAL '30 days');

CREATE OR REPLACE VIEW v_latest_recommendations AS
SELECT * FROM recommendations
WHERE run_id = (
    SELECT run_id FROM runs WHERE status = 'succeeded'
    ORDER BY started_at DESC LIMIT 1
);

CREATE OR REPLACE VIEW v_savings_by_category AS
SELECT category, SUM(est_monthly_savings) AS est_monthly_savings, currency
FROM v_latest_recommendations
GROUP BY category, currency;

-- Per-policy compliance & health (M3.4): aggregate every policy's executions
-- across ALL subscriptions into matched counts, last status and a success rate.
-- INNER JOIN → a policy that has never executed is absent (empty state = no rows).
CREATE OR REPLACE VIEW v_policy_health AS
SELECT
    p.id                                                              AS policy_id,
    p.name                                                            AS policy_name,
    p.resource_type                                                   AS resource_type,
    COUNT(e.execution_id)                                            AS total_executions,
    COUNT(e.execution_id) FILTER (WHERE e.status = 'succeeded')       AS succeeded_executions,
    COUNT(e.execution_id) FILTER (WHERE e.status = 'failed')          AS failed_executions,
    COALESCE(SUM(e.resources_matched), 0)                            AS total_matches,
    COUNT(DISTINCT e.subscription_id)                                AS subscriptions,
    ROUND(
        (COUNT(e.execution_id) FILTER (WHERE e.status = 'succeeded'))::numeric
        / NULLIF(COUNT(e.execution_id), 0),
        4
    )                                                                 AS success_rate,
    MAX(e.started_at)                                                AS last_execution_at,
    (ARRAY_AGG(e.status ORDER BY e.started_at DESC, e.execution_id DESC))[1] AS last_status
FROM policies p
JOIN policy_executions e ON e.policy_id = p.id
GROUP BY p.id, p.name, p.resource_type;

-- Finer grain for the "across subscriptions" Grafana panel: one row per
-- (policy, subscription).
CREATE OR REPLACE VIEW v_policy_compliance AS
SELECT
    p.id                                                        AS policy_id,
    p.name                                                      AS policy_name,
    e.subscription_id                                          AS subscription_id,
    COUNT(e.execution_id)                                      AS total_executions,
    COUNT(e.execution_id) FILTER (WHERE e.status = 'succeeded') AS succeeded_executions,
    COALESCE(SUM(e.resources_matched), 0)                      AS total_matches,
    MAX(e.started_at)                                          AS last_execution_at
FROM policies p
JOIN policy_executions e ON e.policy_id = p.id
GROUP BY p.id, p.name, e.subscription_id;

-- Compliance posture (M9.1): a *current-state* snapshot. The latest execution
-- per (policy, subscription) decides that pair's posture -- compliant when it
-- matched nothing, non-compliant when it matched >=1 resource. Ordering mirrors
-- v_policy_health's last_status ordering: started_at DESC, execution_id DESC (the
-- id tiebreaker keeps same-timestamp seeds deterministic). One row per evaluated
-- pair, so an empty table yields no rows (which reads back as zeroed totals).
CREATE OR REPLACE VIEW v_governance_posture AS
WITH ranked AS (
    SELECT
        e.policy_id,
        e.subscription_id,
        e.resources_matched,
        e.status,
        e.started_at,
        ROW_NUMBER() OVER (
            PARTITION BY e.policy_id, e.subscription_id
            ORDER BY e.started_at DESC, e.execution_id DESC
        ) AS rn
    FROM policy_executions e
)
SELECT
    r.policy_id                     AS policy_id,
    p.name                          AS policy_name,
    r.subscription_id               AS subscription_id,
    COALESCE(s.provider, 'azure')   AS provider,
    r.resources_matched             AS resources_matched,
    (r.resources_matched > 0)       AS non_compliant,
    (r.resources_matched = 0)       AS compliant,
    r.status                        AS last_status,
    r.started_at                    AS last_execution_at
FROM ranked r
JOIN policies p ON p.id = r.policy_id
LEFT JOIN subscriptions s ON s.subscription_id = r.subscription_id
WHERE r.rn = 1;

-- Policy execution health (M9.2): the governance engine's OWN health, per policy.
-- Aggregates every execution into succeeded/failed counts, a rounded success rate,
-- the average wall-clock duration in seconds (over finished runs only), and the last
-- run's time/status. INNER JOIN -- a never-run policy is absent (empty state = no
-- rows). EXTRACT(EPOCH ...) is double precision, so cast to numeric before ROUND.
CREATE OR REPLACE VIEW v_execution_health AS
SELECT
    p.id                                                        AS policy_id,
    p.name                                                      AS policy_name,
    COUNT(e.execution_id)                                       AS total_executions,
    COUNT(e.execution_id) FILTER (WHERE e.status = 'succeeded')  AS succeeded,
    COUNT(e.execution_id) FILTER (WHERE e.status = 'failed')     AS failed,
    ROUND(
        (COUNT(e.execution_id) FILTER (WHERE e.status = 'succeeded'))::numeric
        / NULLIF(COUNT(e.execution_id), 0),
        4
    )                                                            AS success_rate,
    COALESCE(
        ROUND(
            (AVG(EXTRACT(EPOCH FROM (e.finished_at - e.started_at)))
             FILTER (WHERE e.finished_at IS NOT NULL))::numeric,
            3
        ),
        0
    )                                                            AS avg_duration_seconds,
    MAX(e.started_at)                                           AS last_execution_at,
    (ARRAY_AGG(e.status ORDER BY e.started_at DESC, e.execution_id DESC))[1] AS last_status
FROM policies p
JOIN policy_executions e ON e.policy_id = p.id
GROUP BY p.id, p.name;

-- Same measures at the binding grain (M9.2): only binding-triggered executions
-- (binding_id NOT NULL) -- a pull-mode run with no binding is excluded here but
-- still counted per-policy above.
CREATE OR REPLACE VIEW v_execution_health_by_binding AS
SELECT
    e.binding_id                                                AS binding_id,
    COUNT(e.execution_id)                                       AS total_executions,
    COUNT(e.execution_id) FILTER (WHERE e.status = 'succeeded')  AS succeeded,
    COUNT(e.execution_id) FILTER (WHERE e.status = 'failed')     AS failed,
    ROUND(
        (COUNT(e.execution_id) FILTER (WHERE e.status = 'succeeded'))::numeric
        / NULLIF(COUNT(e.execution_id), 0),
        4
    )                                                            AS success_rate,
    COALESCE(
        ROUND(
            (AVG(EXTRACT(EPOCH FROM (e.finished_at - e.started_at)))
             FILTER (WHERE e.finished_at IS NOT NULL))::numeric,
            3
        ),
        0
    )                                                            AS avg_duration_seconds,
    MAX(e.started_at)                                           AS last_execution_at,
    (ARRAY_AGG(e.status ORDER BY e.started_at DESC, e.execution_id DESC))[1] AS last_status
FROM policy_executions e
WHERE e.binding_id IS NOT NULL
GROUP BY e.binding_id;

-- Same measures at the *provider* grain (M12.4 cross-cloud): every execution is
-- attributed to its subscription's cloud (an un-onboarded subscription defaults to
-- 'azure', mirroring the server_default backfill), then aggregated per provider so
-- Azure/AWS/GCP execution health reads as a single multi-cloud pane.
CREATE OR REPLACE VIEW v_execution_health_by_provider AS
SELECT
    COALESCE(s.provider, 'azure')                               AS provider,
    COUNT(e.execution_id)                                       AS total_executions,
    COUNT(e.execution_id) FILTER (WHERE e.status = 'succeeded')  AS succeeded,
    COUNT(e.execution_id) FILTER (WHERE e.status = 'failed')     AS failed,
    ROUND(
        (COUNT(e.execution_id) FILTER (WHERE e.status = 'succeeded'))::numeric
        / NULLIF(COUNT(e.execution_id), 0),
        4
    )                                                            AS success_rate,
    COALESCE(
        ROUND(
            (AVG(EXTRACT(EPOCH FROM (e.finished_at - e.started_at)))
             FILTER (WHERE e.finished_at IS NOT NULL))::numeric,
            3
        ),
        0
    )                                                            AS avg_duration_seconds,
    MAX(e.started_at)                                           AS last_execution_at,
    (ARRAY_AGG(e.status ORDER BY e.started_at DESC, e.execution_id DESC))[1] AS last_status
FROM policy_executions e
LEFT JOIN subscriptions s ON s.subscription_id = e.subscription_id
GROUP BY COALESCE(s.provider, 'azure');

-- Per-framework, per-control compliance posture (M14.13). Joins an installed
-- framework overlay's control->policy mappings (framework_controls) to the live
-- v_governance_posture. A control's status mirrors the API rollup: a gap when it
-- maps to no policy (policy_name IS NULL), non_compliant when any mapped policy
-- flags a resource, compliant only when EVERY mapped policy has run and matched
-- nothing, else not_evaluated. Drives the Grafana per-framework panel. Empty until
-- a framework is installed (rows appear on install).
CREATE OR REPLACE VIEW v_framework_posture AS
WITH control_policy AS (
    SELECT
        fc.framework,
        fc.control_id,
        fc.title,
        fc.ordinal,
        fc.policy_name,
        gp.non_compliant,
        gp.resources_matched,
        gp.last_execution_at
    FROM framework_controls fc
    LEFT JOIN policies p ON p.name = fc.policy_name
    LEFT JOIN v_governance_posture gp ON gp.policy_id = p.id
)
SELECT
    cp.framework                                                     AS framework,
    cp.control_id                                                    AS control_id,
    MIN(cp.title)                                                    AS title,
    MIN(cp.ordinal)                                                  AS ordinal,
    bool_and(cp.policy_name IS NULL)                                 AS is_gap,
    COUNT(DISTINCT cp.policy_name)                                   AS mapped_policies,
    COUNT(DISTINCT cp.policy_name) FILTER (WHERE cp.non_compliant IS NOT NULL)
                                                                     AS evaluated_policies,
    COUNT(*) FILTER (WHERE cp.non_compliant)                         AS noncompliant_pairs,
    COALESCE(SUM(cp.resources_matched), 0)                           AS violations,
    MAX(cp.last_execution_at)                                        AS last_execution_at,
    CASE
        WHEN bool_and(cp.policy_name IS NULL) THEN 'gap'
        WHEN COUNT(*) FILTER (WHERE cp.non_compliant) > 0 THEN 'non_compliant'
        WHEN COUNT(DISTINCT cp.policy_name) > 0
             AND COUNT(DISTINCT cp.policy_name) FILTER (WHERE cp.non_compliant IS NOT NULL)
                 = COUNT(DISTINCT cp.policy_name) THEN 'compliant'
        ELSE 'not_evaluated'
    END                                                              AS status
FROM control_policy cp
GROUP BY cp.framework, cp.control_id;
"""


def _split_sql(block: str) -> list[str]:
    return [stmt.strip() for stmt in block.split(";") if stmt.strip()]


def _try_exec(engine: Engine, sql: str) -> None:
    try:
        with engine.begin() as conn:
            conn.execute(text(sql))
    except Exception as exc:  # noqa: BLE001 - optional DDL, degrade gracefully
        logger.info("optional DDL skipped (%s...): %s", sql[:48].replace("\n", " "), exc)


# Idempotent column back-fills for tables that predate a column. ``create_all``
# only creates missing *tables*, never missing columns on existing ones, so a new
# field on a long-lived table (e.g. subscriptions) needs an explicit, safe ALTER.
_COLUMN_ADDITIONS = [
    "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS environment VARCHAR(32)",
    # M14.11: cost rows gained an owning-cloud tag. Backfill existing (Azure) rows.
    "ALTER TABLE cost_snapshots ADD COLUMN IF NOT EXISTS provider VARCHAR(32) "
    "NOT NULL DEFAULT 'azure'",
]


def init_db() -> None:
    engine = get_engine()
    _try_exec(engine, "CREATE EXTENSION IF NOT EXISTS timescaledb")
    Base.metadata.create_all(engine)
    for stmt in _COLUMN_ADDITIONS:
        _try_exec(engine, stmt)
    for table, column in HYPERTABLES:
        _try_exec(
            engine,
            f"SELECT create_hypertable('{table}', '{column}', "
            "if_not_exists => TRUE, migrate_data => TRUE)",
        )
    with engine.begin() as conn:
        for stmt in _split_sql(_VIEWS_SQL):
            conn.execute(text(stmt))
    logger.info("database schema ready")
