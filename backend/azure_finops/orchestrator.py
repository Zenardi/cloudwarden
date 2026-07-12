"""The FinOps 'run' pipeline: collect -> analyze -> recommend -> store.

collect (inventory + cost + metrics + advisor) -> analyze (rollups + rules +
idle) -> store, as one idempotent run. Cost rows from the live API carry only
resource_id + service_name (the Query API caps groupings at 2), so we enrich
resource_type/location from the inventory. The AI reconciliation pass (Phase 3)
slots in between rules and store.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from .ai import factory as ai_factory
from .ai.prompt import build_payload
from .analysis.idle import detect_idle
from .analysis.rollup import build_rollups
from .analysis.rules import evaluate_vms, prioritize
from .analysis.savings import monthly_cost_map
from .azure.advisor import collect_advisor
from .azure.cost import collect_cost
from .azure.inventory import collect_inventory
from .azure.logs import collect_memory
from .azure.metrics import collect_metrics
from .config import get_settings
from .models import AISummary, CostRow, ResourceRecord
from .storage import repository as repo
from .storage.db import init_db, session_scope

logger = logging.getLogger("azure_finops.orchestrator")


def _new_run_id() -> str:
    return f"run_{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}_{uuid.uuid4().hex[:8]}"


def _enrich_cost(cost_rows: list[CostRow], resources: list[ResourceRecord]) -> None:
    by_id = {r.resource_id: r for r in resources}
    for c in cost_rows:
        if not c.resource_id:
            continue
        res = by_id.get(c.resource_id)
        if res is None:
            continue
        c.resource_type = c.resource_type or res.type
        c.location = c.location or res.location
        c.resource_group = c.resource_group or res.resource_group


def run_pipeline(mock: bool | None = None) -> dict[str, Any]:
    settings = get_settings()
    if mock is not None:
        settings.finops_mock = mock

    init_db()
    run_id = _new_run_id()
    logger.info("starting run %s (mock=%s)", run_id, settings.finops_mock)

    with session_scope() as session:
        repo.create_run(
            session,
            run_id=run_id,
            subscription_id=settings.azure_subscription_id,
            metric_lookback_days=settings.metric_lookback_days,
            cost_lookback_days=settings.cost_lookback_days,
            mock=settings.finops_mock,
        )

    counts: dict[str, int] = {}
    try:
        # --- collect ---
        resources = collect_inventory()
        cost_rows = collect_cost()
        _enrich_cost(cost_rows, resources)
        metric_samples = collect_metrics(resources)
        if not settings.finops_mock and settings.log_analytics_workspace_id:
            metric_samples += collect_memory(resources)
        advisor_recs = collect_advisor()

        # --- analyze ---
        now = datetime.now(UTC)
        window_start = now - timedelta(days=settings.metric_lookback_days)
        expected = max(settings.metric_lookback_days, 1) * 24
        rollups = build_rollups(metric_samples, window_start, now, expected)
        rollup_by_id = {r.resource_id: r for r in rollups}
        monthly = monthly_cost_map(cost_rows)
        advisor_ids = {a["resource_id"] for a in advisor_recs if a.get("resource_id")}
        recommendations = prioritize(
            evaluate_vms(resources, rollup_by_id, monthly, advisor_ids, settings)
            + detect_idle(resources, monthly)
        )

        # --- recommend (AI reconciliation + executive summary) ---
        currency = next((c.currency for c in cost_rows if c.cost_type == "Amortized"), "USD")
        payload = build_payload(
            recommendations,
            cost_rows,
            currency=currency,
            max_candidates=settings.ai_max_candidates,
        )
        ai_result = ai_factory.generate(payload)
        ai_summary = AISummary(
            executive_summary=ai_result.executive_summary,
            total_potential_monthly_savings=ai_result.total_potential_monthly_savings,
            currency=ai_result.currency,
            recommendations=recommendations[:10],
            provider=ai_result.provider,
            model=ai_result.model,
            input_tokens=ai_result.input_tokens,
            output_tokens=ai_result.output_tokens,
        )
        logger.info("AI summary via %s/%s", ai_result.provider, ai_result.model)

        # --- store ---
        with session_scope() as session:
            counts["resources"] = repo.upsert_resources(session, resources)
            counts["cost_rows"] = repo.upsert_cost_snapshots(session, cost_rows)
            counts["metric_samples"] = repo.insert_metric_samples(session, metric_samples)
            counts["rollups"] = repo.upsert_rollups(session, rollups)
            counts["advisor"] = repo.insert_advisor(session, advisor_recs)
            counts["recommendations"] = repo.replace_recommendations(
                session, run_id, recommendations
            )
            repo.upsert_ai_summary(session, run_id, ai_summary)
            counts["ai_summary"] = 1
        with session_scope() as session:
            repo.finish_run(session, run_id, status="succeeded")
    except Exception as exc:  # noqa: BLE001 - recorded then re-raised
        logger.exception("run %s failed", run_id)
        with session_scope() as session:
            repo.finish_run(session, run_id, status="failed", notes=str(exc))
        raise

    logger.info("run %s complete: %s", run_id, counts)
    return {"run_id": run_id, "counts": counts}
