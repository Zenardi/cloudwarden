"""M14.11 — cross-cloud cost parity + orchestrator fan-out (TDD-first).

Asserts that Azure, AWS and GCP collectors emit the *identical* ``CostRow``
schema so every downstream analytic (budgets/anomaly/forecast/showback) is
provider-agnostic, that the orchestrator fans cost collection across onboarded
accounts by provider, and that ``cost_snapshots`` now carries + filters on a
native ``provider`` column. All mock-mode/offline — no live cloud.
"""

from __future__ import annotations

from cloudwarden.azure.context import AccountContext
from cloudwarden.models import CostRow
from cloudwarden.providers import registry

_AZ = AccountContext(account_id="00000000-0000-0000-0000-0000000000aa", provider="azure")
_AWS = AccountContext(account_id="111122223333", provider="aws")
_GCP = AccountContext(account_id="acme-prod-42", provider="gcp")


def _collect(ctx: AccountContext) -> list[CostRow]:
    return registry.get(ctx.provider).collect_cost(account=ctx)


# --------------------------------------------------------------------------- #
# Schema parity across all three clouds
# --------------------------------------------------------------------------- #
def test_all_providers_emit_identical_schema() -> None:
    groups = {"azure": _collect(_AZ), "aws": _collect(_AWS), "gcp": _collect(_GCP)}
    assert all(rows for rows in groups.values()), "each provider yields rows in mock mode"

    fields = set(CostRow.model_fields)
    for provider, rows in groups.items():
        assert all(isinstance(r, CostRow) for r in rows)
        # Identical serialized schema (same keys) for every provider's rows.
        assert all(set(r.model_dump().keys()) == fields for r in rows)
        # Each row is stamped with its owning cloud, and the core FinOps
        # dimensions the analytics read are always populated.
        assert all(r.provider == provider for r in rows)
        # Amortized is the default the right-sizing analytics consume; every cloud
        # emits it (Azure additionally emits Actual, so this is `any`, not `all`).
        assert any(r.cost_type == "Amortized" for r in rows)
        assert all(r.resource_id and r.currency and r.usage_date for r in rows)


# --------------------------------------------------------------------------- #
# Orchestrator fans cost collection across onboarded accounts by provider
# --------------------------------------------------------------------------- #
def test_orchestrator_fans_across_providers() -> None:
    from cloudwarden import orchestrator

    rows = orchestrator.collect_costs([_AZ, _AWS, _GCP])
    assert {r.provider for r in rows} == {"azure", "aws", "gcp"}
    # Each account's identity is threaded onto its own rows.
    assert any(r.provider == "aws" and "111122223333" in (r.resource_id or "") for r in rows)
    assert any(r.provider == "gcp" and "acme-prod-42" in (r.resource_id or "") for r in rows)


def test_collect_costs_isolates_provider_failure(monkeypatch) -> None:
    # One provider blowing up must not sink the others' cost collection.
    from cloudwarden import orchestrator

    def _boom(account, *, client=None, settings=None):
        if account.provider == "aws":
            raise RuntimeError("cost API down")
        return registry.get(account.provider).collect_cost(account=account)

    monkeypatch.setattr(orchestrator, "_collect_cost", _boom)
    rows = orchestrator.collect_costs([_AZ, _AWS, _GCP])
    assert {r.provider for r in rows} == {"azure", "gcp"}  # aws failed, others survived


# --------------------------------------------------------------------------- #
# cost_snapshots carries + filters on a native provider column (add + backfill)
# --------------------------------------------------------------------------- #
def test_cost_rows_persist_and_filter_by_provider(db) -> None:
    from cloudwarden.storage import repository as repo
    from cloudwarden.storage.db import session_scope

    aws_rows = _collect(_AWS)
    gcp_rows = _collect(_GCP)
    with session_scope() as s:
        repo.upsert_cost_snapshots(s, aws_rows + gcp_rows)

    with session_scope() as s:
        aws_total = repo.total_cost(s, days=3650, provider="aws")
        gcp_total = repo.total_cost(s, days=3650, provider="gcp")
        azure_total = repo.total_cost(s, days=3650, provider="azure")
        all_total = repo.total_cost(s, days=3650, provider=None)

    assert aws_total > 0 and gcp_total > 0
    assert azure_total == 0.0  # no azure rows were persisted
    assert round(all_total, 4) == round(aws_total + gcp_total, 4)


def test_cost_snapshot_defaults_provider_azure(db) -> None:
    # A row written without an explicit provider backfills to 'azure' (server_default),
    # so pre-M14.11 Azure rows remain attributable after the column is added.
    import datetime as dt

    from cloudwarden.storage import schema
    from cloudwarden.storage.db import session_scope

    with session_scope() as s:
        s.add(
            schema.CostSnapshot(
                usage_date=dt.date.today(),
                resource_id="/legacy/row",
                meter_category="Compute",
                cost_type="Amortized",
                subscription_id="sub-legacy",
                cost=5.0,
                currency="USD",
            )
        )
    with session_scope() as s:
        row = s.get(
            schema.CostSnapshot,
            {
                "usage_date": dt.date.today(),
                "resource_id": "/legacy/row",
                "meter_category": "Compute",
                "cost_type": "Amortized",
            },
        )
    assert row.provider == "azure"
