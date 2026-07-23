"""Evaluate spend against budgets and fire threshold alerts (M14.2).

FinOps is descriptive until a budget makes it prescriptive. A :class:`Budget` sets a
limit over a scope (a subscription/account/group/tag/team) and a period (monthly or
quarterly), plus ordered **threshold rules** (e.g. 50/80/100% of actual, or a
forecast-basis rule once M14.4 lands). Every pipeline run (and scheduler tick)
evaluates actual — and, when available, forecast — spend against each budget.

Two invariants shape the design:

* **Fire once.** A crossing emits **exactly one** notification per period and
  threshold. Dedupe is persisted as :class:`BudgetThresholdEvent` rows keyed on
  ``(budget, period, threshold, basis)``; re-evaluating the same period is a no-op,
  and a new period resets the slate. A jump past several thresholds at once notifies
  once — for the highest newly-crossed — so an overage never triggers an alert storm.
* **Never break the run.** Notification dispatch is best-effort: a transport failure
  is logged and swallowed; the crossing is still recorded so it won't re-fire.

The pure helpers (:func:`period_key`, :func:`actual_pct`, :func:`crossed_rules`) are
unit-tested without a database; :func:`evaluate_budgets` injects its spend source and
dispatcher so the crossing/dedupe logic is exercised offline.
"""

from __future__ import annotations

import calendar
import datetime as dt
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from ..notify import service
from ..notify.dispatch import dispatch_for_budget
from ..storage import repository as repo

logger = logging.getLogger("cloudwarden.budgets")

_VALID_BASES = ("actual", "forecast")


@dataclass(frozen=True)
class ThresholdRule:
    """One ordered budget threshold: cross ``pct`` percent of the ``basis`` metric."""

    pct: float
    basis: str = "actual"  # actual | forecast


def parse_thresholds(raw: list[dict[str, Any]]) -> list[ThresholdRule]:
    """Parse stored threshold dicts into sorted :class:`ThresholdRule`s (asc by pct)."""
    rules = [
        ThresholdRule(pct=float(item["pct"]), basis=str(item.get("basis", "actual")).lower())
        for item in raw or []
    ]
    return sorted(rules, key=lambda r: r.pct)


def period_key(period: str, on: dt.date) -> str:
    """A stable identifier for the budget period containing ``on``.

    Monthly → ``"YYYY-MM"``; quarterly → ``"YYYY-Qn"``. This is the dedupe scope: two
    crossings share a period iff they share this key.
    """
    if period == "quarterly":
        quarter = (on.month - 1) // 3 + 1
        return f"{on.year:04d}-Q{quarter}"
    return f"{on.year:04d}-{on.month:02d}"


def period_bounds(period: str, on: dt.date) -> tuple[dt.date, dt.date]:
    """The inclusive ``(start, end)`` dates of the period containing ``on``."""
    if period == "quarterly":
        q_index = (on.month - 1) // 3  # 0..3
        start_month = q_index * 3 + 1
        end_month = start_month + 2
        start = dt.date(on.year, start_month, 1)
        end = dt.date(on.year, end_month, calendar.monthrange(on.year, end_month)[1])
        return start, end
    start = dt.date(on.year, on.month, 1)
    end = dt.date(on.year, on.month, calendar.monthrange(on.year, on.month)[1])
    return start, end


def actual_pct(spend: float, amount: float) -> float:
    """Spend as a percentage of the budget ``amount`` (0.0 when ``amount`` ≤ 0)."""
    if amount <= 0:
        return 0.0
    return spend / amount * 100.0


def crossed_rules(
    rules: list[ThresholdRule],
    *,
    actual_pct: float,
    forecast_pct: float | None = None,
) -> list[ThresholdRule]:
    """Rules whose threshold is met by the relevant metric, ordered ascending.

    An ``actual`` rule is compared to ``actual_pct``; a ``forecast`` rule to
    ``forecast_pct`` — and is skipped entirely when no forecast is available (M14.4
    not yet landed), so a forecast rule never fires off actual spend.
    """
    crossed: list[ThresholdRule] = []
    for rule in rules:
        if rule.basis == "forecast":
            if forecast_pct is None:
                continue
            metric = forecast_pct
        else:
            metric = actual_pct
        if metric >= rule.pct:
            crossed.append(rule)
    return crossed


def _default_spend(session: Session, budget: dict[str, Any], start: dt.date, end: dt.date) -> float:
    return repo.budget_spend(
        session,
        scope_type=budget["scope_type"],
        scope_value=budget.get("scope_value"),
        start=start,
        end=end,
    )


def _no_forecast(*_args: Any, **_kwargs: Any) -> float | None:
    # Forecasting arrives in M14.4; until then no budget has a forecast metric.
    return None


def evaluate_budgets(
    session: Session,
    *,
    on: dt.date,
    run_id: str | None = None,
    dispatch_fn: Callable[..., Any] | None = None,
    spend_fn: Callable[..., float] | None = None,
    forecast_fn: Callable[..., float | None] | None = None,
    template_fn: Callable[[Session], int] | None = None,
) -> dict[str, int]:
    """Evaluate every enabled budget against spend as of ``on`` and alert on crossings.

    For each budget: resolve the current period, measure actual (and forecast) spend,
    determine which thresholds are newly crossed versus the recorded events for the
    period, persist a :class:`BudgetThresholdEvent` for each, and dispatch **one**
    notification (for the highest newly-crossed threshold) through the budget's
    channel. ``spend_fn``/``forecast_fn``/``dispatch_fn``/``template_fn`` are injected
    in tests; production uses the repository + notify seams.

    Returns counts: ``budgets_evaluated``, ``events_recorded``, ``notifications_sent``.
    """
    dispatch_fn = dispatch_fn or dispatch_for_budget
    spend_fn = spend_fn or _default_spend
    forecast_fn = forecast_fn or _no_forecast
    template_fn = template_fn or repo.ensure_budget_template

    budgets = repo.list_budgets(session, enabled_only=True)
    events_recorded = 0
    notifications_sent = 0
    default_template_id: int | None = None

    for budget in budgets:
        pkey = period_key(budget["period"], on)
        start, _end = period_bounds(budget["period"], on)
        spend = float(spend_fn(session, budget, start, on))
        amount = float(budget["amount"])
        pct = actual_pct(spend, amount)

        forecast = forecast_fn(session, budget, start, on, spend)
        forecast_pct = actual_pct(float(forecast), amount) if forecast is not None else None

        rules = parse_thresholds(budget["thresholds"])
        crossed = crossed_rules(rules, actual_pct=pct, forecast_pct=forecast_pct)
        if not crossed:
            continue

        fired = {
            (float(e["threshold_pct"]), e["basis"])
            for e in repo.budget_events_for_period(session, budget["id"], pkey)
        }
        newly = [r for r in crossed if (r.pct, r.basis) not in fired]
        if not newly:
            continue

        # Anti-storm: one notification per evaluation, for the highest newly-crossed
        # threshold. Every newly-crossed threshold is still recorded so none re-fires.
        highest = max(newly, key=lambda r: r.pct)
        for rule in newly:
            metric_pct = forecast_pct if rule.basis == "forecast" else pct
            metric_spend = float(forecast) if rule.basis == "forecast" else spend
            recorded = repo.record_budget_event(
                session,
                budget_id=budget["id"],
                period_key=pkey,
                threshold_pct=rule.pct,
                basis=rule.basis,
                amount=metric_spend,
                budget_amount=amount,
                actual_pct=metric_pct if metric_pct is not None else 0.0,
                currency=budget["currency"],
                run_id=run_id,
                notified=(rule == highest and bool(budget.get("channel_id"))),
            )
            if recorded is not None:
                events_recorded += 1

        if not budget.get("channel_id"):
            continue  # crossing recorded, but nowhere to notify

        if default_template_id is None:
            default_template_id = template_fn(session)
        template_id = budget.get("template_id") or default_template_id
        metric_pct = forecast_pct if highest.basis == "forecast" else pct
        metric_spend = float(forecast) if highest.basis == "forecast" else spend
        context = service.build_budget_context(
            budget=budget,
            period_key=pkey,
            spend=metric_spend,
            actual_pct=metric_pct if metric_pct is not None else 0.0,
            threshold_pct=highest.pct,
            basis=highest.basis,
        )
        try:
            result = dispatch_fn(session, budget=budget, context=context, template_id=template_id)
            if result is not None:
                notifications_sent += 1
        except Exception:  # noqa: BLE001 - a failed alert must never break the run
            logger.warning("budget %s notification failed", budget["id"], exc_info=True)

    return {
        "budgets_evaluated": len(budgets),
        "events_recorded": events_recorded,
        "notifications_sent": notifications_sent,
    }
