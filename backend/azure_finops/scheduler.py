"""Periodic runner (APScheduler blocking scheduler).

Two independently-cadenced jobs share one scheduler: the cost-collection pipeline
(``RUN_INTERVAL_SECONDS``) and pull-mode policy execution
(``POLICY_RUN_INTERVAL_SECONDS``). Both run once at boot, then on their own
interval; each is wrapped so a failure never kills the scheduler.
"""

from __future__ import annotations

import logging

from apscheduler.schedulers.blocking import BlockingScheduler

from .config import get_settings
from .orchestrator import run_all_policies, run_all_subscriptions

logger = logging.getLogger("azure_finops.scheduler")


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
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("scheduler stopped")
