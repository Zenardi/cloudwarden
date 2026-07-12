"""Periodic pipeline runner (APScheduler blocking scheduler)."""

from __future__ import annotations

import logging

from apscheduler.schedulers.blocking import BlockingScheduler

from .config import get_settings
from .orchestrator import run_all_subscriptions

logger = logging.getLogger("azure_finops.scheduler")


def _safe_run() -> None:
    try:
        run_all_subscriptions()
    except Exception:  # noqa: BLE001 - keep the scheduler alive
        logger.exception("scheduled run failed")


def run_scheduler() -> None:
    interval = max(get_settings().run_interval_seconds, 60)
    logger.info("scheduler starting; interval=%ss", interval)
    _safe_run()  # run once immediately on boot
    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(_safe_run, "interval", seconds=interval, id="finops-run")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("scheduler stopped")
