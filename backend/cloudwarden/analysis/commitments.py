"""Commitment coverage & RI/Savings-Plan recommendations (M14.1).

Two families of signal, both grounded strictly in collected data:

* **Under-utilized commitment** — an existing Reservation/Savings Plan whose
  utilization sits below a threshold: the idle share is money paid for capacity
  nobody uses (advisory waste).
* **Under-covered steady-state** — eligible on-demand usage that runs every day
  of the window but isn't committed. We size a purchase candidate at the
  *min-of-window* level (the baseline present every single day — never a burst),
  price the delta via a blended commitment discount, and compute break-even for
  each term/payment option.

Every savings figure is an **estimate** carrying a ``basis`` and caveats; advisory
items never over-state (the min-of-window floor and blended discount are
deliberately conservative). Azure-only today; other providers get no signal
(the detector is a no-op behind the ``CloudProvider`` abstraction).
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict

from ..models import (
    CommitmentCoverage,
    CommitmentRecord,
    CommitmentSignals,
    Recommendation,
    SteadyStateUsage,
)
from .pricing import HOURS_PER_MONTH

# The provider the detector understands. AWS/GCP fall through to an empty result
# so the pipeline can call it uniformly behind the CloudProvider abstraction.
_SUPPORTED_PROVIDER = "azure"

# A commitment utilized below this (percent) wastes its idle share — advisory.
UNDER_UTILIZED_THRESHOLD = 80.0
# Surface a commitment as "expiring soon" within this many days of its expiry.
EXPIRING_WITHIN_DAYS = 60
# Ignore a steady-state baseline below this ($/hr): too small (or too bursty, its
# window minimum ≈ 0) to be worth a commitment.
MIN_COMMIT_HOURLY = 0.5

# Commitment terms and payment options we cost out.
_TERMS = ("P1Y", "P3Y")
_PAYMENTS = ("no_upfront", "partial_upfront", "all_upfront")
_TERM_MONTHS = {"P1Y": 12, "P3Y": 36}

# Blended discount off on-demand by term (conservative public-rate midpoints). The
# live path can refine per-family via the Retail Prices API; the static curve keeps
# the estimate deterministic and testable. Upfront payment earns a small extra cut.
_TERM_DISCOUNT = {"P1Y": 0.30, "P3Y": 0.55}
_PAYMENT_BUMP = {"no_upfront": 0.0, "partial_upfront": 0.015, "all_upfront": 0.03}
_MAX_DISCOUNT = 0.70


def default_blended_discount(
    sku_family: str | None, term: str, payment: str = "no_upfront"
) -> float:
    """Blended commitment discount (fraction off on-demand) for a term/payment.

    ``sku_family`` is accepted for a future per-family refinement (Retail Prices);
    today the curve is family-agnostic. Capped at :data:`_MAX_DISCOUNT`."""
    base = _TERM_DISCOUNT.get(term, 0.30)
    return round(min(base + _PAYMENT_BUMP.get(payment, 0.0), _MAX_DISCOUNT), 4)


def _key(family: str | None, region: str | None) -> tuple[str, str]:
    return (family or "unknown", region or "unknown")


def compute_coverage(
    commitments: list[CommitmentRecord],
    steady_state: list[SteadyStateUsage],
    *,
    provider: str = _SUPPORTED_PROVIDER,
) -> list[CommitmentCoverage]:
    """Per family/region coverage % and blended commitment utilization.

    ``committed_monthly`` = existing commitment capacity; ``eligible_monthly`` =
    committed + the uncovered steady-state baseline (avg-of-window). ``coverage_pct``
    is committed / eligible; ``utilization_pct`` is the hourly-weighted blend of the
    commitments in that family/region (None when none exist)."""
    committed: dict[tuple[str, str], float] = defaultdict(float)
    util_num: dict[tuple[str, str], float] = defaultdict(float)
    util_den: dict[tuple[str, str], float] = defaultdict(float)
    currency: dict[tuple[str, str], str] = {}
    for c in commitments:
        k = _key(c.sku_family, c.region)
        committed[k] += c.hourly_committed * HOURS_PER_MONTH
        util_num[k] += c.utilization_pct * c.hourly_committed
        util_den[k] += c.hourly_committed
        currency.setdefault(k, c.currency)

    uncovered: dict[tuple[str, str], float] = defaultdict(float)
    for s in steady_state:
        k = _key(s.sku_family, s.region)
        avg = sum(s.window_hourly) / len(s.window_hourly) if s.window_hourly else 0.0
        uncovered[k] += avg * HOURS_PER_MONTH
        currency.setdefault(k, s.currency)

    out: list[CommitmentCoverage] = []
    for k in sorted(set(committed) | set(uncovered)):
        family, region = k
        c_monthly = round(committed.get(k, 0.0), 4)
        eligible = round(c_monthly + uncovered.get(k, 0.0), 4)
        coverage_pct = round(c_monthly / eligible * 100, 2) if eligible > 0 else 0.0
        util = round(util_num[k] / util_den[k], 2) if util_den.get(k) else None
        out.append(
            CommitmentCoverage(
                provider=provider,
                sku_family=family,
                region=region,
                eligible_monthly=eligible,
                committed_monthly=c_monthly,
                coverage_pct=coverage_pct,
                utilization_pct=util,
                currency=currency.get(k, "USD"),
            )
        )
    return out


def _purchase_options(sku_family: str | None, safe_hourly: float, discount_fn) -> list[dict]:
    """Cost out every term/payment option for a safe hourly baseline.

    Each option carries its blended discount, estimated monthly savings, upfront
    outlay, and break-even (months of on-demand spend the upfront prepay buys back —
    0 for no-upfront, where savings start immediately)."""
    monthly_on_demand = safe_hourly * HOURS_PER_MONTH
    options: list[dict] = []
    for term in _TERMS:
        for payment in _PAYMENTS:
            discount = discount_fn(sku_family, term, payment)
            monthly_savings = monthly_on_demand * discount
            months = _TERM_MONTHS[term]
            prepay = {"no_upfront": 0.0, "partial_upfront": 0.5, "all_upfront": 1.0}[payment]
            upfront = monthly_on_demand * (1 - discount) * months * prepay
            break_even = round(upfront / monthly_on_demand, 1) if monthly_on_demand > 0 else 0.0
            options.append(
                {
                    "term": term,
                    "payment": payment,
                    "discount": round(discount, 4),
                    "est_monthly_savings": round(monthly_savings, 2),
                    "upfront_cost": round(upfront, 2),
                    "break_even_months": break_even,
                }
            )
    return options


def _purchase_rec(
    usage: SteadyStateUsage, *, provider: str, currency: str, discount_fn, min_commit_hourly: float
) -> Recommendation | None:
    """A purchase candidate sized to the min-of-window baseline, or None if bursty."""
    safe_hourly = min(usage.window_hourly) if usage.window_hourly else 0.0
    if safe_hourly < min_commit_hourly:
        return None  # no steady baseline (bursty or trivially small) — don't recommend
    options = _purchase_options(usage.sku_family, safe_hourly, discount_fn)
    best = max(options, key=lambda o: (o["est_monthly_savings"], -o["upfront_cost"]))
    family = usage.sku_family or "unknown"
    region = usage.region or "unknown"
    monthly_on_demand = round(safe_hourly * HOURS_PER_MONTH, 2)
    return Recommendation(
        resource_id=f"commitment/{provider}/{family}/{region}",
        category="commitment",
        action="purchase_commitment",
        current_sku=None,
        recommended_sku=f"{family} {best['term']} {best['payment']}",
        risk="low",
        confidence=0.5,
        est_monthly_savings=best["est_monthly_savings"],
        currency=currency,
        source="heuristic",
        rationale=(
            f"Estimate: {family} in {region} runs a steady baseline of at least "
            f"{safe_hourly:.2f}/hr on-demand every day of the window "
            f"(~{monthly_on_demand:.0f}/mo). Committing that baseline (best option: "
            f"{best['term']} {best['payment']}, ~{int(best['discount'] * 100)}% off) "
            f"saves ~{best['est_monthly_savings']:.0f}/mo."
        ),
        caveats=[
            "Estimate — blended Retail Prices discount; validate against a formal Azure quote",
            "Assumes the steady-state baseline persists across the commitment term",
        ],
        evidence={
            "basis": "blended commitment discount applied to min-of-window steady-state usage",
            "estimate": True,
            "sku_family": family,
            "region": region,
            "safe_commit_hourly": round(safe_hourly, 4),
            "committed_monthly_on_demand": monthly_on_demand,
            "recommended_option": best,
            "options": options,
        },
    )


def _under_utilized_rec(c: CommitmentRecord, threshold: float) -> Recommendation | None:
    """Advisory waste rec for a commitment below the utilization threshold."""
    if c.utilization_pct >= threshold:
        return None
    idle_fraction = (100.0 - c.utilization_pct) / 100.0
    wasted = round(c.hourly_committed * HOURS_PER_MONTH * idle_fraction, 2)
    name = c.display_name or c.commitment_id
    return Recommendation(
        resource_id=c.commitment_id,
        category="commitment",
        action="review_commitment_utilization",
        current_sku=c.sku_family,
        risk="medium",
        confidence=0.4,
        est_monthly_savings=wasted,
        currency=c.currency,
        source="heuristic",
        rationale=(
            f"Estimate: {c.kind} '{name}' is only {c.utilization_pct:.0f}% utilized — about "
            f"{idle_fraction * 100:.0f}% of its committed capacity (~{wasted:.0f}/mo) is paid for "
            f"but unused. Right-size, re-scope (Shared), or exchange it."
        ),
        caveats=[
            "Estimate — based on the reported utilization for the current period",
            "Reservations/Savings Plans can be exchanged or scoped Shared before cancelling",
        ],
        evidence={
            "basis": "idle share of committed capacity at reported utilization",
            "estimate": True,
            "commitment_id": c.commitment_id,
            "kind": c.kind,
            "utilization_pct": c.utilization_pct,
            "term": c.term,
        },
    )


def _expiring_rec(c: CommitmentRecord, now: dt.date, within_days: int) -> Recommendation | None:
    """Informational rec for a commitment expiring within the horizon."""
    if c.expiry_date is None:
        return None
    days = (c.expiry_date - now).days
    if days < 0 or days > within_days:
        return None
    name = c.display_name or c.commitment_id
    return Recommendation(
        resource_id=c.commitment_id,
        category="commitment",
        action="review_commitment_expiry",
        current_sku=c.sku_family,
        risk="low",
        confidence=0.5,
        est_monthly_savings=0.0,
        currency=c.currency,
        source="heuristic",
        rationale=(
            f"{c.kind} '{name}' expires in {days} day(s) (on {c.expiry_date}). Renew or re-plan "
            f"before it lapses to on-demand rates."
        ),
        caveats=["Informational — savings depend on renewing at the then-current rate"],
        evidence={
            "basis": "commitment expiry within the review horizon",
            "estimate": True,
            "commitment_id": c.commitment_id,
            "kind": c.kind,
            "days_to_expiry": days,
            "expiry_date": str(c.expiry_date),
        },
    )


def detect_commitments(
    commitments: list[CommitmentRecord],
    steady_state: list[SteadyStateUsage],
    *,
    provider: str = _SUPPORTED_PROVIDER,
    currency: str = "USD",
    discount_fn=default_blended_discount,
    now: dt.date | None = None,
    under_utilized_threshold: float = UNDER_UTILIZED_THRESHOLD,
    expiring_within_days: int = EXPIRING_WITHIN_DAYS,
    min_commit_hourly: float = MIN_COMMIT_HOURLY,
) -> list[Recommendation]:
    """Commitment recommendations: under-utilized waste, expiring, and purchases.

    A no-op (empty list) for any provider other than Azure — the detector plugs in
    behind the ``CloudProvider`` abstraction and AWS/GCP have no implementation yet."""
    if provider != _SUPPORTED_PROVIDER:
        return []
    today = now or dt.date.today()
    recs: list[Recommendation] = []
    for c in commitments:
        waste = _under_utilized_rec(c, under_utilized_threshold)
        if waste is not None:
            recs.append(waste)
        expiring = _expiring_rec(c, today, expiring_within_days)
        if expiring is not None:
            recs.append(expiring)
    for usage in steady_state:
        buy = _purchase_rec(
            usage,
            provider=provider,
            currency=currency,
            discount_fn=discount_fn,
            min_commit_hourly=min_commit_hourly,
        )
        if buy is not None:
            recs.append(buy)
    return recs


def analyze_commitments(
    signals: CommitmentSignals,
    *,
    currency: str = "USD",
    discount_fn=default_blended_discount,
    now: dt.date | None = None,
    under_utilized_threshold: float = UNDER_UTILIZED_THRESHOLD,
    expiring_within_days: int = EXPIRING_WITHIN_DAYS,
    min_commit_hourly: float = MIN_COMMIT_HOURLY,
) -> tuple[list[Recommendation], list[CommitmentCoverage]]:
    """Convenience wrapper: (recommendations, coverage rollups) for the orchestrator."""
    recs = detect_commitments(
        signals.commitments,
        signals.steady_state,
        provider=signals.provider,
        currency=currency,
        discount_fn=discount_fn,
        now=now,
        under_utilized_threshold=under_utilized_threshold,
        expiring_within_days=expiring_within_days,
        min_commit_hourly=min_commit_hourly,
    )
    coverage = (
        compute_coverage(signals.commitments, signals.steady_state, provider=signals.provider)
        if signals.provider == _SUPPORTED_PROVIDER
        else []
    )
    return recs, coverage
