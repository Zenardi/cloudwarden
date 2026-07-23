"""M14.1 — commitment coverage & RI/Savings-Plan recommendations.

Collector (``azure.reservations``) + detector (``analysis.commitments``) +
environment weighting (``analysis.savings``) + repository CRUD + the
``GET /api/finops/commitments`` endpoint. Azure/Retail-Prices clients are mocked
or injected; every test asserts a single behaviour (Arrange-Act-Assert).
"""

from __future__ import annotations

import datetime as dt

import pytest

from cloudwarden.analysis.commitments import (
    HOURS_PER_MONTH,
    analyze_commitments,
    compute_coverage,
    default_blended_discount,
    detect_commitments,
)
from cloudwarden.analysis.savings import weight_commitment_savings
from cloudwarden.azure.context import AccountContext
from cloudwarden.azure.reservations import collect_reservations
from cloudwarden.models import (
    CommitmentRecord,
    CommitmentSignals,
    Recommendation,
    SteadyStateUsage,
)

_TODAY = dt.date(2026, 7, 23)


def _commitment(**kw) -> CommitmentRecord:
    base: dict = {
        "commitment_id": "/providers/microsoft.capacity/reservations/res-1",
        "kind": "reservation",
        "display_name": "res-1",
        "scope": "Shared",
        "region": "eastus",
        "sku_family": "Dsv5",
        "term": "P3Y",
        "utilization_pct": 95.0,
        "expiry_date": _TODAY + dt.timedelta(days=400),
        "hourly_committed": 1.0,
        "currency": "USD",
    }
    base.update(kw)
    return CommitmentRecord(**base)


def _usage(window: list[float], family: str = "Fsv2", region: str = "westus") -> SteadyStateUsage:
    return SteadyStateUsage(sku_family=family, region=region, window_hourly=window)


# --------------------------------------------------------------------------- #
# Collector
# --------------------------------------------------------------------------- #
def test_reservations_mock_shape() -> None:
    signals = collect_reservations()

    assert isinstance(signals, CommitmentSignals)
    assert signals.provider == "azure"
    assert signals.commitments, "expected mock commitments"
    assert signals.steady_state, "expected mock steady-state usage"
    assert all(isinstance(c, CommitmentRecord) for c in signals.commitments)
    assert all(isinstance(s, SteadyStateUsage) for s in signals.steady_state)


def test_non_azure_provider_returns_empty() -> None:
    # Arrange: an AWS account context (the detector is Azure-only today).
    ctx = AccountContext(account_id="123456789012", provider="aws")

    # Act
    signals = collect_reservations(subscription=ctx)

    # Assert: no-op stub — nothing collected for a non-Azure cloud.
    assert signals.provider == "aws"
    assert signals.commitments == []
    assert signals.steady_state == []


def test_reservations_live_uses_injected_client(monkeypatch) -> None:
    monkeypatch.setenv("FINOPS_MOCK", "0")
    from cloudwarden.config import get_settings

    get_settings.cache_clear()

    class _FakeClient:
        def list_reservations(self):
            return [
                {
                    "commitment_id": "res-live",
                    "kind": "savings_plan",
                    "sku_family": "Dsv5",
                    "region": "eastus",
                    "term": "P1Y",
                    "utilization_pct": 88.0,
                    "expiry_date": "2027-01-01",
                    "hourly_committed": 2.0,
                }
            ]

        def list_steady_state_usage(self):
            return [{"sku_family": "Dsv5", "region": "eastus", "window_hourly": [3.0, 3.5, 3.0]}]

    signals = collect_reservations(client=_FakeClient())

    assert [c.commitment_id for c in signals.commitments] == ["res-live"]
    assert signals.commitments[0].kind == "savings_plan"
    assert signals.commitments[0].expiry_date == dt.date(2027, 1, 1)
    assert signals.steady_state[0].window_hourly == [3.0, 3.5, 3.0]


# --------------------------------------------------------------------------- #
# Coverage / utilization
# --------------------------------------------------------------------------- #
def test_existing_commitment_utilization_computed() -> None:
    # Arrange: one commitment (1.0 $/hr, 95% utilized) + uncovered steady-state.
    commitments = [_commitment(sku_family="Dsv5", region="eastus", hourly_committed=1.0)]
    steady = [_usage([1.0, 1.0, 1.0], family="Dsv5", region="eastus")]

    # Act
    coverage = compute_coverage(commitments, steady)

    # Assert: one rollup for the family/region carrying its utilization + coverage.
    row = next(c for c in coverage if c.sku_family == "Dsv5" and c.region == "eastus")
    assert row.utilization_pct == 95.0
    committed = 1.0 * HOURS_PER_MONTH
    uncovered = 1.0 * HOURS_PER_MONTH
    assert row.committed_monthly == pytest.approx(committed, rel=1e-3)
    assert row.coverage_pct == pytest.approx(committed / (committed + uncovered) * 100, rel=1e-3)


# --------------------------------------------------------------------------- #
# Detector — waste, expiry, purchase
# --------------------------------------------------------------------------- #
def test_under_utilized_commitment_flagged_advisory() -> None:
    # Arrange: a 1.0 $/hr commitment only 55% utilized (< 80% threshold).
    commitments = [_commitment(utilization_pct=55.0, hourly_committed=1.0)]

    # Act
    recs = detect_commitments(commitments, [], now=_TODAY)

    # Assert: one advisory waste rec sized to the idle share.
    waste = [r for r in recs if r.action == "review_commitment_utilization"]
    assert len(waste) == 1
    rec = waste[0]
    assert rec.category == "commitment"
    assert rec.est_monthly_savings == pytest.approx(0.45 * HOURS_PER_MONTH, rel=1e-3)
    assert rec.caveats  # advisory — never asserted as certain
    assert rec.confidence < 0.5


def test_expiring_commitment_surfaced() -> None:
    # Arrange: a commitment expiring in 30 days (<= 60-day horizon).
    commitments = [_commitment(expiry_date=_TODAY + dt.timedelta(days=30))]

    # Act
    recs = detect_commitments(commitments, [], now=_TODAY)

    # Assert: an informational expiry rec (no savings claimed).
    expiring = [r for r in recs if r.action == "review_commitment_expiry"]
    assert len(expiring) == 1
    assert expiring[0].est_monthly_savings == 0.0
    assert expiring[0].evidence["days_to_expiry"] == 30


def test_purchase_candidate_sized_to_min_of_window() -> None:
    # Arrange: steady-state on-demand that never drops below 10.0 $/hr.
    steady = [_usage([10.0, 12.0, 11.0, 10.0, 13.0], family="Fsv2", region="westus")]

    # Act
    recs = detect_commitments([], steady, now=_TODAY)

    # Assert: a purchase candidate sized to the window minimum (the always-on level).
    buys = [r for r in recs if r.action == "purchase_commitment"]
    assert len(buys) == 1
    rec = buys[0]
    assert rec.evidence["safe_commit_hourly"] == 10.0
    assert rec.evidence["options"], "term/payment options must be surfaced"
    assert rec.est_monthly_savings > 0


def test_bursty_usage_yields_no_purchase_rec() -> None:
    # Arrange: bursty usage — spikes to 20 but sits at ~0 most days (min ≈ 0).
    steady = [_usage([0.0, 0.0, 20.0, 0.0, 0.0], family="Fsv2", region="westus")]

    # Act
    recs = detect_commitments([], steady, now=_TODAY)

    # Assert: no safe steady-state baseline → no purchase recommendation.
    assert [r for r in recs if r.action == "purchase_commitment"] == []


def test_savings_estimate_has_basis_and_caveats() -> None:
    # Arrange: a clear purchase candidate.
    steady = [_usage([10.0, 10.0, 10.0], family="Fsv2", region="westus")]

    # Act
    recs = detect_commitments([], steady, now=_TODAY)

    # Assert: the estimate is labelled with a basis and carries caveats.
    rec = next(r for r in recs if r.action == "purchase_commitment")
    assert rec.evidence.get("basis")
    assert rec.evidence.get("estimate") is True
    assert rec.caveats
    assert "estimate" in rec.rationale.lower()


def test_break_even_present_for_purchase_options() -> None:
    steady = [_usage([10.0, 10.0, 10.0], family="Fsv2", region="westus")]

    recs = detect_commitments([], steady, now=_TODAY)

    rec = next(r for r in recs if r.action == "purchase_commitment")
    for opt in rec.evidence["options"]:
        assert opt["break_even_months"] >= 0.0
        # No-upfront options break even immediately; upfront options take time.
        if opt["payment"] == "no_upfront":
            assert opt["break_even_months"] == 0.0
            assert opt["upfront_cost"] == 0.0


def test_commitment_without_expiry_not_surfaced() -> None:
    # A commitment with no known expiry date is never an "expiring soon" signal.
    commitments = [_commitment(expiry_date=None, utilization_pct=95.0)]

    recs = detect_commitments(commitments, [], now=_TODAY)

    assert [r for r in recs if r.action == "review_commitment_expiry"] == []


def test_expired_commitment_not_surfaced() -> None:
    # A commitment that already lapsed (negative days) is not an "expiring soon" signal.
    commitments = [_commitment(expiry_date=_TODAY - dt.timedelta(days=5))]

    recs = detect_commitments(commitments, [], now=_TODAY)

    assert [r for r in recs if r.action == "review_commitment_expiry"] == []


def test_well_utilized_commitment_not_flagged() -> None:
    commitments = [_commitment(utilization_pct=98.0)]

    recs = detect_commitments(commitments, [], now=_TODAY)

    assert [r for r in recs if r.action == "review_commitment_utilization"] == []


def test_detect_non_azure_provider_returns_empty() -> None:
    steady = [_usage([10.0, 10.0, 10.0])]

    assert detect_commitments([], steady, provider="gcp", now=_TODAY) == []


def test_analyze_commitments_non_azure_is_empty() -> None:
    signals = CommitmentSignals(
        provider="aws", steady_state=[_usage([10.0, 10.0], family="m5", region="us-east-1")]
    )

    recs, coverage = analyze_commitments(signals, now=_TODAY)

    assert recs == [] and coverage == []


def test_analyze_commitments_returns_recs_and_coverage() -> None:
    signals = CommitmentSignals(
        commitments=[_commitment(sku_family="Dsv5", region="eastus", utilization_pct=50.0)],
        steady_state=[_usage([10.0, 10.0], family="Fsv2", region="westus")],
    )

    recs, coverage = analyze_commitments(signals, now=_TODAY)

    assert any(r.action == "purchase_commitment" for r in recs)
    assert any(r.action == "review_commitment_utilization" for r in recs)
    assert coverage  # coverage rollups produced


# --------------------------------------------------------------------------- #
# Discount curve
# --------------------------------------------------------------------------- #
def test_three_year_discount_beats_one_year() -> None:
    one = default_blended_discount("Dsv5", "P1Y", "no_upfront")
    three = default_blended_discount("Dsv5", "P3Y", "no_upfront")

    assert 0.0 < one < three < 1.0


def test_all_upfront_discount_beats_no_upfront() -> None:
    assert default_blended_discount("Dsv5", "P1Y", "all_upfront") > default_blended_discount(
        "Dsv5", "P1Y", "no_upfront"
    )


# --------------------------------------------------------------------------- #
# Environment weighting (savings.py)
# --------------------------------------------------------------------------- #
def _buy_rec(savings: float = 100.0) -> Recommendation:
    return Recommendation(
        resource_id="commitment/azure/Fsv2/westus",
        category="commitment",
        action="purchase_commitment",
        est_monthly_savings=savings,
    )


def test_environment_weighting_applied() -> None:
    # Arrange: a Prod subscription discounts reclaimable savings by its factor (0.5).
    recs = [_buy_rec(100.0)]

    # Act
    weight_commitment_savings(recs, "Prod")

    # Assert: savings halved and the environment/factor stamped for the UI.
    assert recs[0].est_monthly_savings == 50.0
    assert recs[0].evidence["environment"] == "Prod"
    assert recs[0].evidence["reclaim_factor"] == 0.5


def test_environment_weighting_noop_when_unclassified() -> None:
    recs = [_buy_rec(100.0)]

    weight_commitment_savings(recs, None)

    assert recs[0].est_monthly_savings == 100.0
    assert "environment" not in recs[0].evidence


# --------------------------------------------------------------------------- #
# Repository + API (DB-backed; skips without Docker/testcontainers)
# --------------------------------------------------------------------------- #
def test_repository_upsert_and_list_commitments(db) -> None:
    from cloudwarden.storage import repository as repo
    from cloudwarden.storage.db import session_scope

    commitments = [_commitment(commitment_id="res-A"), _commitment(commitment_id="res-B")]
    with session_scope() as s:
        n = repo.upsert_commitment(s, commitments)
    assert n == 2

    # Idempotent: re-upsert updates in place, no duplicate rows.
    with session_scope() as s:
        repo.upsert_commitment(s, [_commitment(commitment_id="res-A", utilization_pct=10.0)])
    with session_scope() as s:
        rows = repo.list_commitments(s)
    assert len(rows) == 2
    row_a = next(r for r in rows if r["commitment_id"] == "res-A")
    assert float(row_a["utilization_pct"]) == 10.0


def test_repository_commitment_coverage_roundtrip(db) -> None:
    from cloudwarden.storage import repository as repo
    from cloudwarden.storage.db import session_scope

    coverage = compute_coverage(
        [_commitment(sku_family="Dsv5", region="eastus")],
        [_usage([1.0, 1.0], family="Dsv5", region="eastus")],
    )
    with session_scope() as s:
        repo.create_run(
            s,
            run_id="run-cov",
            subscription_id="sub-1",
            metric_lookback_days=14,
            cost_lookback_days=30,
            mock=True,
        )
        n = repo.upsert_commitment_coverage(s, "run-cov", coverage)
    assert n == len(coverage)

    with session_scope() as s:
        rows = repo.latest_commitment_coverage(s)
    assert rows
    assert rows[0]["sku_family"] == "Dsv5"


def test_api_finops_commitments_returns_coverage(db) -> None:
    from fastapi.testclient import TestClient

    from cloudwarden.api.main import app
    from cloudwarden.storage import repository as repo
    from cloudwarden.storage.db import session_scope

    with session_scope() as s:
        repo.upsert_commitment(s, [_commitment(commitment_id="res-api")])

    resp = TestClient(app).get("/api/finops/commitments")

    assert resp.status_code == 200
    body = resp.json()
    assert "coverage" in body and "commitments" in body and "recommendations" in body
    assert any(c["commitment_id"] == "res-api" for c in body["commitments"])


def test_api_finops_commitments_rbac_guarded(db, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from cloudwarden.api.main import app
    from cloudwarden.authz import rbac
    from cloudwarden.config import get_settings
    from cloudwarden.storage import repository as repo
    from cloudwarden.storage.db import session_scope

    monkeypatch.setenv("RBAC_ENABLED", "1")
    get_settings.cache_clear()
    with session_scope() as s:
        rbac.seed_default_roles(s)
        repo.assign_role(s, principal="ed", role_name="editor")
    client = TestClient(app)

    anon = client.get("/api/finops/commitments")
    editor = client.get("/api/finops/commitments", headers={"X-Principal": "ed"})

    assert anon.status_code == 401
    assert editor.status_code == 200
    get_settings.cache_clear()
