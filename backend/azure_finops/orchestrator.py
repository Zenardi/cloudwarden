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
from .azure._fixtures import retarget
from .azure.advisor import collect_advisor
from .azure.context import SubscriptionContext
from .azure.cost import collect_cost
from .azure.inventory import collect_inventory
from .azure.logs import collect_memory
from .azure.metrics import collect_metrics
from .config import get_settings
from .custodian import engine as custodian_engine
from .models import AISummary, CostRow, PolicyMatch, ResourceRecord
from .storage import repository as repo
from .storage.db import init_db, session_scope

logger = logging.getLogger("azure_finops.orchestrator")


def _context_from_record(record: Any, mock: bool) -> SubscriptionContext:
    """Build a run context from a `subscriptions` row, minting a per-subscription
    credential only on the live path when the row carries its own SP creds."""
    credential = None
    if not mock and record.client_id and record.client_secret:
        from .auth import credential_for

        credential = credential_for(record.tenant_id, record.client_id, record.client_secret)
    return SubscriptionContext(
        subscription_id=record.subscription_id,
        credential=credential,
        display_name=record.display_name,
    )


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


def run_pipeline(
    mock: bool | None = None, subscription: SubscriptionContext | None = None
) -> dict[str, Any]:
    settings = get_settings()
    if mock is not None:
        settings.finops_mock = mock

    init_db()
    run_id = _new_run_id()
    sub_id = subscription.subscription_id if subscription else settings.azure_subscription_id
    logger.info("starting run %s (subscription=%s, mock=%s)", run_id, sub_id, settings.finops_mock)

    with session_scope() as session:
        repo.create_run(
            session,
            run_id=run_id,
            subscription_id=sub_id,
            metric_lookback_days=settings.metric_lookback_days,
            cost_lookback_days=settings.cost_lookback_days,
            mock=settings.finops_mock,
        )

    counts: dict[str, int] = {}
    try:
        # --- collect ---
        resources = collect_inventory(subscription=subscription)
        cost_rows = collect_cost(subscription=subscription)
        _enrich_cost(cost_rows, resources)
        metric_samples = collect_metrics(resources, subscription=subscription)
        if not settings.finops_mock and settings.log_analytics_workspace_id:
            metric_samples += collect_memory(resources)
        advisor_recs = collect_advisor(subscription=subscription)

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
    return {"run_id": run_id, "subscription_id": sub_id, "counts": counts}


def run_one_subscription(subscription_id: str, mock: bool | None = None) -> dict[str, Any] | None:
    """Run the pipeline for a single subscription by id. Returns None if unknown."""
    settings = get_settings()
    if mock is not None:
        settings.finops_mock = mock
    init_db()
    with session_scope() as session:
        record = repo.get_subscription(session, subscription_id)
        ctx = _context_from_record(record, settings.finops_mock) if record else None
    if ctx is None:
        return None
    return run_pipeline(subscription=ctx)


def run_all_subscriptions(mock: bool | None = None) -> dict[str, Any]:
    """Run the pipeline once per enabled subscription.

    Seeds the subscriptions table from the env subscription on first use, then
    fans out one run per enabled row. A single subscription's failure is recorded
    on its run and does not abort the others.
    """
    settings = get_settings()
    if mock is not None:
        settings.finops_mock = mock
    init_db()
    with session_scope() as session:
        repo.ensure_default_subscription(session, settings)
        records = [
            _context_from_record(r, settings.finops_mock)
            for r in repo.enabled_subscriptions(session)
        ]

    results: list[dict[str, Any]] = []
    for ctx in records:
        try:
            results.append(run_pipeline(subscription=ctx))
        except Exception as exc:  # noqa: BLE001 - per-subscription isolation
            logger.exception("subscription %s run failed", ctx.subscription_id)
            results.append({"subscription_id": ctx.subscription_id, "error": str(exc)})
    logger.info("ran %d subscription(s)", len(results))
    return {"subscriptions": len(results), "runs": results}


# --------------------------------------------------------------------------- #
# Pull-mode policy execution (M3.2): evaluate policies on their own cadence,
# independent of the cost-collection pipeline above.
# --------------------------------------------------------------------------- #
def _new_execution_id() -> str:
    return f"exec_{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}_{uuid.uuid4().hex[:8]}"


def _declared_actions(spec: dict[str, Any]) -> list[str]:
    """Extract a policy's declared action identifiers from its c7n spec.

    Dry-run pull mode executes no actions, but recording what the policy is
    *configured* to do gives the execution summary (and the M3.3 UI) real content.
    Actions may be bare strings (``"stop"``) or mappings (``{"type": "stop"}``).
    """
    policies = spec.get("policies") or []
    if not policies:
        return []
    actions = policies[0].get("actions") or []
    identifiers: list[str] = []
    for action in actions:
        if isinstance(action, str):
            identifiers.append(action)
        elif isinstance(action, dict) and action.get("type"):
            identifiers.append(action["type"])
    return identifiers


def _matches_from_result(result: dict[str, Any], subscription_id: str) -> list[PolicyMatch]:
    """Convert an engine ``run_policy`` result into ``PolicyMatch`` transport rows.

    Fixture-backed (mock) resource ids embed the placeholder subscription, so we
    retarget them to the run's subscription for distinct, non-colliding ids.
    """
    return [
        PolicyMatch(
            resource_id=retarget(resource.get("id", ""), subscription_id),
            resource_type=resource.get("type"),
        )
        for resource in result.get("resources", [])
    ]


def run_policies(subscription: SubscriptionContext, mock: bool | None = None) -> dict[str, Any]:
    """Execute every enabled policy against one subscription (pull mode).

    Each policy gets its own ``PolicyExecution`` (opened ``running``, then closed
    ``succeeded``/``failed``) plus its ``PolicyMatch`` rows. A single policy's
    failure is recorded on its own row and does not abort the siblings — the same
    per-item isolation ``run_all_subscriptions`` applies across subscriptions.
    """
    settings = get_settings()
    if mock is not None:
        settings.finops_mock = mock
    init_db()
    sub_id = subscription.subscription_id
    with session_scope() as session:
        policies = repo.list_policies(session, enabled_only=True)
    logger.info("evaluating %d polic(ies) against subscription %s", len(policies), sub_id)

    executions: list[dict[str, Any]] = []
    for policy in policies:
        execution_id = _new_execution_id()
        with session_scope() as session:
            repo.create_policy_execution(
                session,
                execution_id=execution_id,
                policy_id=policy["id"],
                subscription_id=sub_id,
            )
        try:
            result = custodian_engine.run_policy(policy["spec"], subscription=subscription)
            matches = _matches_from_result(result, sub_id)
            with session_scope() as session:
                repo.insert_policy_matches(session, execution_id, matches)
                repo.finish_policy_execution(
                    session,
                    execution_id,
                    status="succeeded",
                    resources_matched=len(matches),
                    actions_taken=_declared_actions(policy["spec"]),
                )
            executions.append(
                {
                    "execution_id": execution_id,
                    "policy_id": policy["id"],
                    "status": "succeeded",
                    "resources_matched": len(matches),
                }
            )
        except Exception as exc:  # noqa: BLE001 - per-policy isolation
            logger.exception("policy %s execution %s failed", policy["id"], execution_id)
            with session_scope() as session:
                repo.finish_policy_execution(session, execution_id, status="failed", error=str(exc))
            executions.append(
                {
                    "execution_id": execution_id,
                    "policy_id": policy["id"],
                    "status": "failed",
                    "error": str(exc),
                }
            )
    logger.info("subscription %s: %d execution(s)", sub_id, len(executions))
    return {"subscription_id": sub_id, "executions": executions}


def run_all_policies(mock: bool | None = None) -> dict[str, Any]:
    """Run pull-mode policy execution once per enabled subscription.

    Seeds the subscriptions table from the env subscription on first use (like
    ``run_all_subscriptions``), then fans ``run_policies`` out across every enabled
    row with per-subscription failure isolation.
    """
    settings = get_settings()
    if mock is not None:
        settings.finops_mock = mock
    init_db()
    with session_scope() as session:
        repo.ensure_default_subscription(session, settings)
        contexts = [
            _context_from_record(r, settings.finops_mock)
            for r in repo.enabled_subscriptions(session)
        ]

    results: list[dict[str, Any]] = []
    for ctx in contexts:
        try:
            results.append(run_policies(ctx))
        except Exception as exc:  # noqa: BLE001 - per-subscription isolation
            logger.exception("subscription %s policy run failed", ctx.subscription_id)
            results.append({"subscription_id": ctx.subscription_id, "error": str(exc)})
    logger.info("ran policies for %d subscription(s)", len(results))
    return {"subscriptions": len(results), "runs": results}
