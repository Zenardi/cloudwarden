"""Shared pytest fixtures: env defaults + a Postgres-backed `db` fixture.

The `db` fixture spins up a throwaway PostgreSQL (via testcontainers), points the
app at it, runs `init_db()`, and truncates all tables between tests. If Docker or
testcontainers is unavailable, DB-backed tests skip.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("FINOPS_MOCK", "1")
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://finops:finops@localhost:5432/finops")

_TABLES = (
    "runs, resources, assets, asset_events, asset_relationships, cost_snapshots, "
    "utilization_samples, "
    "utilization_rollups, advisor_recommendations, recommendations, remediation_actions, "
    "ai_summaries, account_groups, account_group_members, bindings, event_log, "
    "subscriptions, "
    "policies, policy_versions, policy_collections, "
    "collection_policies, policy_executions, policy_matches"
)


@pytest.fixture(scope="session")
def _pg_engine():
    try:
        from testcontainers.postgres import PostgresContainer
    except Exception:  # pragma: no cover - optional dependency
        pytest.skip("testcontainers not installed")

    import azure_finops.storage.db as dbmod
    from azure_finops.config import get_settings

    try:
        container = PostgresContainer("postgres:16-alpine")
        container.start()
    except Exception as exc:  # pragma: no cover - docker unavailable
        pytest.skip(f"cannot start postgres container: {exc}")

    try:
        url = container.get_connection_url().replace("+psycopg2", "+psycopg")
        os.environ["DATABASE_URL"] = url
        os.environ["FINOPS_MOCK"] = "1"
        get_settings.cache_clear()
        dbmod._engine = None
        dbmod._session_factory = None
        dbmod.init_db()
        yield dbmod.get_engine()
    finally:
        container.stop()


@pytest.fixture
def db(_pg_engine):
    from sqlalchemy import text

    from azure_finops.config import get_settings

    get_settings.cache_clear()
    yield _pg_engine
    with _pg_engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {_TABLES} RESTART IDENTITY CASCADE"))
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _isolate_settings(monkeypatch, tmp_path):
    """Clean, mock-mode settings before every test (isolate from host Azure env,
    any real project `.env`, and cross-test leakage). Tests that need live mode
    override these. chdir to a temp dir so `env_file=".env"` finds nothing."""
    from azure_finops.config import get_settings

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FINOPS_MOCK", "1")
    for var in (
        "AZURE_TENANT_ID",
        "AZURE_CLIENT_ID",
        "AZURE_CLIENT_SECRET",
        "AZURE_REMEDIATION_TENANT_ID",
        "AZURE_REMEDIATION_CLIENT_ID",
        "AZURE_REMEDIATION_CLIENT_SECRET",
        "LOG_ANALYTICS_WORKSPACE_ID",
        "REMEDIATION_ENABLED",
        "ALLOWED_RESOURCE_GROUPS",
        "AI_PROVIDER",
        "AI_BASE_URL",
        "AI_API_KEY",
        "ANTHROPIC_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
