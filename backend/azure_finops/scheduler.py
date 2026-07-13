"""Periodic runner (APScheduler blocking scheduler).

Two independently-cadenced jobs share one scheduler: the cost-collection pipeline
(``RUN_INTERVAL_SECONDS``) and pull-mode policy execution
(``POLICY_RUN_INTERVAL_SECONDS``). Both run once at boot, then on their own
interval; each is wrapped so a failure never kills the scheduler.
"""

from __future__ import annotations

import logging
from typing import Any

from apscheduler.schedulers.blocking import BlockingScheduler

from .config import get_settings
from .orchestrator import run_all_policies, run_all_subscriptions

logger = logging.getLogger("azure_finops.scheduler")


def _safe_run_binding(binding_id: int) -> None:
    from .custodian.bindings import run_binding

    try:
        run_binding(binding_id)
    except Exception:  # noqa: BLE001 - keep the scheduler alive
        logger.exception("scheduled binding %s run failed", binding_id)


def _schedule_bindings(scheduler: Any) -> int:
    """Register a cron job per enabled binding that has a schedule (M5.3).

    Bindings without a schedule, disabled bindings, and bindings with an unparseable
    cron are skipped (the last is logged). Returns the number of jobs registered.
    """
    from apscheduler.triggers.cron import CronTrigger

    from .storage import repository as repo
    from .storage.db import init_db, session_scope

    init_db()
    with session_scope() as session:
        bindings = [b for b in repo.list_bindings(session) if b["enabled"] and b["schedule"]]
    scheduled = 0
    for binding in bindings:
        try:
            trigger = CronTrigger.from_crontab(binding["schedule"], timezone="UTC")
        except (ValueError, TypeError):
            logger.warning(
                "binding %s has an invalid cron %r; skipping", binding["id"], binding["schedule"]
            )
            continue
        scheduler.add_job(
            _safe_run_binding,
            trigger,
            args=[binding["id"]],
            id=f"finops-binding-{binding['id']}",
        )
        scheduled += 1
    logger.info("scheduled %d binding cron job(s)", scheduled)
    return scheduled


def _safe_run_governance_report() -> None:
    from . import reporting

    try:
        path = reporting.write_report("csv")
        logger.info("governance report written to %s", path)
    except Exception:  # noqa: BLE001 - keep the scheduler alive
        logger.exception("scheduled governance report failed")


def _schedule_governance_report(scheduler: Any) -> bool:
    """Register the optional periodic governance report (M9.4), gated by
    ``GOVERNANCE_REPORT_ENABLED``. Returns whether a job was scheduled."""
    settings = get_settings()
    if not settings.governance_report_enabled:
        return False
    interval = max(settings.governance_report_interval_seconds, 60)
    scheduler.add_job(
        _safe_run_governance_report, "interval", seconds=interval, id="finops-governance-report"
    )
    logger.info("scheduled governance report every %ss", interval)
    return True


def _safe_run() -> None:
    try:
        run_all_subscriptions()
    except Exception:  # noqa: BLE001 - keep the scheduler alive
        logger.exception("scheduled run failed")


def _safe_run_policies() -> None:
    try:
        run_all_policies()
    except Exception:  # noqa: BLE001 - keep the scheduler alive
        logger.exception("scheduled policy run failed")


def run_scheduler() -> None:
    settings = get_settings()
    interval = max(settings.run_interval_seconds, 60)
    policy_interval = max(settings.policy_run_interval_seconds, 60)
    logger.info("scheduler starting; interval=%ss policy_interval=%ss", interval, policy_interval)
    _safe_run()  # run once immediately on boot
    _safe_run_policies()
    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(_safe_run, "interval", seconds=interval, id="finops-run")
    scheduler.add_job(
        _safe_run_policies, "interval", seconds=policy_interval, id="finops-policy-run"
    )
    _schedule_bindings(scheduler)  # one cron job per enabled binding (M5.3)
    _schedule_governance_report(scheduler)  # optional periodic export (M9.4)
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("scheduler stopped")
