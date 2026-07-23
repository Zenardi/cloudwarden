"""Detect abnormal daily spend over the ``cost_snapshots`` time-series (M14.3).

FinOps reporting is descriptive: it shows spend after the fact. Anomaly detection
makes it *watchful* — a spend spike is flagged the run it appears, at the
subscription / service / resource-type / resource grain, with a **contribution
breakdown** (what drove it) and a severity.

The detector is deliberately **robust and seasonality-aware**, so it neither cries
wolf on noise nor misses a real jump:

* **Robust baseline.** The trailing window's centre is the **median** and its spread
  the **MAD** (median absolute deviation) — both immune to the very outliers we hunt,
  unlike mean/stdev which a single spike would poison. The deviation score is the
  day's distance from the centre in robust-sigma (MAD) units.
* **Weekday-aware.** Cloud spend is weekly-seasonal (weekday vs weekend). Each day is
  **deseasonalized** by its weekday factor (that weekday's median ÷ the overall
  median) before scoring, so an in-pattern weekend peak is *expected*, not anomalous.
* **Signal-gated.** With fewer than ``min_history`` baseline days the detector emits
  **nothing** — no false positives on thin history — and a scale floor keeps an
  ultra-steady series from turning trivial noise into an infinite score.

The pure helpers (:func:`robust_stats`, :func:`weekday_factors`, :func:`score_series`,
:func:`severity_for`, :func:`attribute_contributors`) are unit-tested without a
database on deterministic seeded series; :func:`detect_cost_anomalies` injects its
data source and dispatcher so the detect/persist/notify flow is exercised offline.
Two invariants match the budget alerting fabric it reuses: an anomaly notifies
**exactly once** per (scope, date), and a transport failure **never breaks the run**.
"""

from __future__ import annotations

import datetime as dt
import logging
import statistics
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from ..notify import service
from ..notify.dispatch import dispatch_for_anomaly
from ..storage import repository as repo

logger = logging.getLogger("cloudwarden.anomaly")

# The grains an anomaly is detected at, coarsest → finest. Each maps to a
# ``cost_snapshots`` column in the repository (validated against a whitelist there).
DEFAULT_SCOPES = ("subscription", "service", "resource_type", "resource")

# Detector tuning (overridable via config / call args).
DEFAULT_MIN_HISTORY = 14  # baseline days required before anything is ever flagged
DEFAULT_SENSITIVITY = 3.5  # robust-sigma (MAD units) above which a day is anomalous
DEFAULT_WINDOW_DAYS = 45  # trailing window feeding the baseline

# MAD → sigma consistency constant (a normal distribution's MAD ≈ 0.6745·sigma).
_MAD_TO_SIGMA = 1.4826
# Scale floors so an ultra-steady series (MAD ≈ 0) never divides by ~0: the spread is
# at least ``_REL_FLOOR`` of the centre (a 5% relative band) and never below ``_ABS_FLOOR``.
_REL_FLOOR = 0.05
_ABS_FLOOR = 0.01
# Cap the reported score so a step from ~0 spend stays inside the stored precision.
_SCORE_CAP = 999.0


@dataclass(frozen=True)
class Deviation:
    """One day's scored deviation from its (deseasonalized) baseline."""

    expected: float  # baseline centre, reseasonalized to the target day's weekday
    actual: float  # the day's measured spend
    score: float  # distance from the centre in robust-sigma (MAD) units
    severity: str  # bucketed label: low | medium | high | critical


def robust_stats(values: list[float]) -> tuple[float, float]:
    """Return the ``(median, MAD)`` of ``values`` — a robust centre and spread.

    MAD (median absolute deviation) is ``median(|x - median|)``; unlike the standard
    deviation it is not dragged by the outliers the detector is looking for.
    """
    center = statistics.median(values)
    mad = statistics.median([abs(v - center) for v in values])
    return center, mad


def severity_for(score: float) -> str:
    """Bucket a robust-sigma ``score`` into a severity label (assumes it is anomalous)."""
    if score >= 12.0:
        return "critical"
    if score >= 8.0:
        return "high"
    if score >= 5.0:
        return "medium"
    return "low"


def weekday_factors(baseline: Iterable[tuple[dt.date, float]]) -> dict[int, float]:
    """Per-weekday seasonal multipliers: that weekday's median ÷ the overall median.

    A factor of ``1.0`` means the weekday runs at the overall level; ``3.0`` means it
    runs 3× (a heavy weekend). Weekdays with fewer than two samples are omitted (too
    thin to trust) and default to ``1.0`` at lookup. An empty/zero-median baseline
    yields no factors (no seasonal adjustment).
    """
    by_weekday: dict[int, list[float]] = defaultdict(list)
    values: list[float] = []
    for day, cost in baseline:
        by_weekday[day.weekday()].append(float(cost))
        values.append(float(cost))
    if not values:
        return {}
    overall = statistics.median(values)
    if overall <= 0:
        return {}
    factors: dict[int, float] = {}
    for weekday, samples in by_weekday.items():
        if len(samples) < 2:
            continue
        factors[weekday] = statistics.median(samples) / overall
    return factors


def _scale(center: float, mad: float) -> float:
    """The robust spread used as the score's denominator, floored so it is never ~0."""
    return max(_MAD_TO_SIGMA * mad, _REL_FLOOR * abs(center), _ABS_FLOOR)


def score_series(
    series: Iterable[tuple[dt.date, float]],
    *,
    on: dt.date,
    min_history: int = DEFAULT_MIN_HISTORY,
    threshold: float = DEFAULT_SENSITIVITY,
    seasonal: bool = True,
) -> Deviation | None:
    """Score the spend on ``on`` against the deseasonalized trailing baseline.

    Returns a :class:`Deviation` when the day is anomalous (score ≥ ``threshold``),
    else ``None``. ``None`` is also returned — the **signal gate** — when ``on`` has no
    data or the baseline has fewer than ``min_history`` days, so thin history never
    produces a false positive. With ``seasonal=False`` no weekday adjustment is applied.
    """
    by_date = {day: float(cost) for day, cost in series}
    actual = by_date.get(on)
    if actual is None:
        return None  # no spend recorded for the target day — nothing to score
    baseline = [(day, cost) for day, cost in by_date.items() if day < on]
    if len(baseline) < min_history:
        return None  # signal-gated: too little history to judge

    factors = weekday_factors(baseline) if seasonal else {}
    target_factor = factors.get(on.weekday(), 1.0) or 1.0

    def deseason(day: dt.date, cost: float) -> float:
        factor = factors.get(day.weekday(), 1.0) or 1.0
        return cost / factor

    adjusted = [deseason(day, cost) for day, cost in baseline]
    center, mad = robust_stats(adjusted)
    scale = _scale(center, mad)

    adjusted_target = actual / target_factor
    score = (adjusted_target - center) / scale
    if score < threshold:
        return None  # within normal variation (or below baseline) — not an anomaly

    score = min(round(score, 4), _SCORE_CAP)
    expected = round(center * target_factor, 6)
    return Deviation(expected=expected, actual=actual, score=score, severity=severity_for(score))


def attribute_contributors(children: list[dict[str, Any]], *, top: int = 5) -> list[dict[str, Any]]:
    """Rank a scope's child rows by how much each drove the spike (day vs baseline).

    Each child carries ``actual`` (its spend on the day) and ``baseline`` (its trailing
    average); ``delta = actual - baseline`` is its contribution. The list is sorted by
    ``delta`` descending and truncated to ``top``, and each entry gets a ``share`` of the
    total *positive* delta (so the drivers of the increase sum toward 1.0).
    """
    scored = [
        {**child, "delta": round(float(child["actual"]) - float(child.get("baseline", 0.0)), 6)}
        for child in children
    ]
    scored.sort(key=lambda c: c["delta"], reverse=True)
    total_positive = sum(c["delta"] for c in scored if c["delta"] > 0) or 1.0
    top_children = scored[:top]
    for child in top_children:
        child["share"] = round(max(child["delta"], 0.0) / total_positive, 4)
    return top_children


def _resolve(value: Any, settings_value: Any) -> Any:
    return settings_value if value is None else value


def detect_cost_anomalies(
    session: Session,
    *,
    on: dt.date,
    run_id: str | None = None,
    scopes: Iterable[str] | None = None,
    min_history: int | None = None,
    sensitivity: float | None = None,
    window_days: int | None = None,
    seasonal: bool | None = None,
    channel_name: str | None = None,
    dispatch_fn: Callable[..., Any] | None = None,
    template_fn: Callable[[Session], int] | None = None,
    settings: Any | None = None,
) -> dict[str, int]:
    """Detect anomalies over the cost series as of ``on`` and alert on new ones.

    For each scope grain: read the daily-by-scope-value series over the trailing
    ``window_days`` window, score the day ``on`` (seasonality-aware, signal-gated), and
    for each anomalous scope value attribute the top contributors, persist the anomaly
    idempotently (unique on scope+date), and dispatch **one** notification per newly
    recorded anomaly through the configured channel. Re-detecting the same scope+date
    updates the row but never re-notifies. Dispatch is best-effort: a transport failure
    is logged and swallowed, and the anomaly stays recorded (unnotified).

    Tuning (``min_history``/``sensitivity``/``window_days``/``seasonal``/``channel_name``)
    falls back to ``settings`` when ``None``; ``dispatch_fn``/``template_fn`` are the
    injected seams. Returns counts: ``scopes_scanned``, ``anomalies_detected``,
    ``notifications_sent``.
    """
    if settings is None:
        from ..config import get_settings

        settings = get_settings()
    scopes = tuple(scopes) if scopes is not None else DEFAULT_SCOPES
    min_history = int(_resolve(min_history, settings.anomaly_min_history_days))
    sensitivity = float(_resolve(sensitivity, settings.anomaly_sensitivity))
    window_days = int(_resolve(window_days, settings.anomaly_window_days))
    seasonal = bool(_resolve(seasonal, getattr(settings, "anomaly_seasonality", True)))
    channel_name = _resolve(channel_name, settings.anomaly_alert_channel)
    dispatch_fn = dispatch_fn or dispatch_for_anomaly
    template_fn = template_fn or repo.ensure_anomaly_template

    start = on - dt.timedelta(days=window_days)
    anomalies_detected = 0
    notifications_sent = 0
    template_id: int | None = None

    for scope_type in scopes:
        rows = repo.cost_daily_by_scope(session, scope_type=scope_type, start=start, end=on)
        by_scope: dict[str, list[tuple[dt.date, float]]] = defaultdict(list)
        currency: dict[str, str] = {}
        for row in rows:
            by_scope[row["scope_value"]].append((row["usage_date"], row["cost"]))
            currency.setdefault(row["scope_value"], row.get("currency") or "USD")

        for scope_value, series in by_scope.items():
            dev = score_series(
                series,
                on=on,
                min_history=min_history,
                threshold=sensitivity,
                seasonal=seasonal,
            )
            if dev is None:
                continue

            children = repo.cost_scope_children(
                session, scope_type=scope_type, scope_value=scope_value, on=on, start=start
            )
            contributors = attribute_contributors(children)
            row, inserted = repo.upsert_cost_anomaly(
                session,
                scope_type=scope_type,
                scope_value=scope_value,
                usage_date=on,
                expected=dev.expected,
                actual=dev.actual,
                score=dev.score,
                severity=dev.severity,
                currency=currency.get(scope_value, "USD"),
                contributors=contributors,
                run_id=run_id,
            )
            anomalies_detected += 1
            if not inserted:
                continue  # already seen this scope+date — recorded, not re-notified

            if template_id is None:
                template_id = template_fn(session)
            context = service.build_anomaly_context(
                scope_type=scope_type,
                scope_value=scope_value,
                on=on,
                expected=dev.expected,
                actual=dev.actual,
                score=dev.score,
                severity=dev.severity,
                currency=currency.get(scope_value, "USD"),
                contributors=contributors,
            )
            try:
                result = dispatch_fn(
                    session, context=context, template_id=template_id, channel_name=channel_name
                )
            except Exception:  # noqa: BLE001 - a failed alert must never break detection
                logger.warning("anomaly %s notification failed", row["id"], exc_info=True)
                result = None
            if result is not None:
                repo.mark_anomaly_notified(session, row["id"])
                notifications_sent += 1

    return {
        "scopes_scanned": len(scopes),
        "anomalies_detected": anomalies_detected,
        "notifications_sent": notifications_sent,
    }
