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
from .analysis.idle import detect_idle, detect_idle_by_activity
from .analysis.rollup import build_rollups
from .analysis.rules import evaluate_vms, prioritize
from .analysis.savings import monthly_cost_map, reclaim_factor
from .azure._fixtures import retarget
from .azure.activity_metrics import collect_activity_metrics
from .azure.activitylog import collect_activity_log
from .azure.advisor import collect_advisor
from .azure.context import AccountContext
from .azure.cost import collect_cost
from .azure.inventory import collect_inventory
from .azure.logs import collect_memory
from .azure.ml_compute import collect_ml_computes
from .azure.metrics import collect_metrics
from .config import get_settings
from .custodian import engine as custodian_engine
from .models import AISummary, CostRow, PolicyMatch, ResourceRecord
from .storage import repository as repo
from .storage.db import init_db, session_scope

logger = logging.getLogger("cloudwarden.orchestrator")


def _context_from_record(record: Any, mock: bool) -> AccountContext:
    """Build a run context from an accounts (`subscriptions`) row via its provider.

    Mints a per-account credential only on the live path when the row carries its
    own SP creds, then delegates to the resolved provider's ``account_context`` so
    the context is provider-neutral (defaulting to Azure for existing rows)."""
    credential = None
    if not mock and record.client_id and record.client_secret:
        from .auth import credential_for

        credential = credential_for(record.tenant_id, record.client_id, record.client_secret)
    from .providers import registry

    provider = registry.get(getattr(record, "provider", None) or "azure")
    return provider.account_context(
        account_id=record.subscription_id,
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
    mock: bool | None = None, subscription: AccountContext | None = None
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
        # ML compute targets (Phase 3): instances/clusters live *under* a workspace
        # and are absent from Resource Graph, so the generic inventory misses them.
        # Enumerate them per workspace and fold them into `resources` so they flow
        # through cost enrichment, AssetDB, and the idle detectors. Non-fatal: a
        # failure here must not sink an otherwise-good cost run.
        try:
            resources += collect_ml_computes(resources, subscription=subscription)
        except Exception:  # noqa: BLE001 - ML compute discovery is best-effort
            logger.warning("ml compute collection failed", exc_info=True)
        cost_rows = collect_cost(subscription=subscription)
        _enrich_cost(cost_rows, resources)
        metric_samples = collect_metrics(resources, subscription=subscription)
        if not settings.finops_mock and settings.log_analytics_workspace_id:
            metric_samples += collect_memory(resources)
        advisor_recs = collect_advisor(subscription=subscription)
        # Platform-metric activity signal (Phase 2b): always-on Azure Monitor
        # metrics (Bastion sessions, storage transactions, ACR pulls) surface
        # resources that bill but nobody uses — the pivot from the empty LA layer.
        activity = collect_activity_metrics(resources, subscription=subscription)
        # AssetDB change history (M4.4): who/how/when each asset changed.
        activity_events = collect_activity_log(subscription=subscription)

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
            + detect_idle_by_activity(
                resources, activity, monthly, window_days=settings.metric_lookback_days
            )
        )

        # --- recommend (AI reconciliation + executive summary) ---
        currency = next((c.currency for c in cost_rows if c.cost_type == "Amortized"), "USD")
        # Environment-weighted potential savings (subscription "kind"): the reclaim
        # factor discounts idle/waste savings by how safely they can be cut — a
        # sandbox resource is delete-on-sight (x1.0), production idle needs review
        # first (x0.5). Unclassified subs keep full value. Applied BEFORE the AI
        # payload so the executive summary total reflects the weighted figure.
        environment = None
        with session_scope() as session:
            sub_rec = repo.get_subscription(session, sub_id)
            if sub_rec is not None:
                environment = sub_rec.environment
        factor = reclaim_factor(environment)
        # Heuristic/advisor recs default to USD (model default) but their savings are
        # all derived from this run's cost rows, so stamp them with the actual billing
        # currency — otherwise the UI mislabels e.g. EUR savings as USD.
        for rec in recommendations:
            rec.currency = currency
            if factor != 1.0 and rec.est_monthly_savings:
                rec.est_monthly_savings = round(rec.est_monthly_savings * factor, 2)
            if environment:
                # Attribution: carry the environment + factor so the UI can group
                # potential savings by subscription kind and show the discount.
                rec.evidence = {
                    **rec.evidence,
                    "environment": environment,
                    "reclaim_factor": factor,
                }
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
            # AssetDB (M4.1): upsert the richer asset rows and record a 'created'
            # event the first time each resource is seen.
            new_asset_ids = repo.upsert_assets(session, resources)
            by_id = {r.resource_id: r for r in resources}
            for rid in new_asset_ids:
                rec = by_id[rid]
                repo.append_asset_event(
                    session,
                    resource_id=rid,
                    subscription_id=rec.subscription_id,
                    event_type="created",
                    data=rec.config,
                )
            counts["assets"] = len(by_id)
            counts["asset_events"] = len(new_asset_ids)
            # AssetDB graph (M4.3): derive typed edges (disk→vm, nic→vm, ip→nic)
            # from asset config. Idempotent; dangling references are skipped.
            counts["asset_relationships"] = repo.build_relationships(session)
            # AssetDB change history (M4.4): persist Activity Log events (who/how/when)
            # into asset_events; malformed records were already skipped on collect.
            counts["activity_events"] = repo.record_activity_events(session, activity_events)
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


def _sync_display_names() -> None:
    """Backfill placeholder subscription names with the real cloud name (Azure only).

    Best-effort and self-limiting: only rows still carrying the seed placeholder
    trigger a connectivity call, and a failure never aborts the collection run.
    The (blocking) ARM connectivity checks run OUTSIDE any DB transaction — we read
    the candidate rows into plain tuples first, then persist each resolved name in
    its own short transaction — so a slow/unreachable ARM never holds Postgres
    locks open.
    """
    from .azure.connectivity import check_connection

    with session_scope() as session:
        candidates = [
            (r.subscription_id, r.tenant_id, r.client_id, r.client_secret)
            for r in repo.enabled_subscriptions(session)
            if (getattr(r, "provider", None) or "azure") == "azure" and repo.is_auto_display_name(r)
        ]

    for sub_id, tenant_id, client_id, client_secret in candidates:
        credential = None
        if client_id and client_secret:
            from .auth import credential_for

            credential = credential_for(tenant_id, client_id, client_secret)
        try:
            result = check_connection(sub_id, credential=credential)
        except Exception:  # noqa: BLE001 - name sync must never break a run
            logger.warning("display-name sync failed for %s", sub_id, exc_info=True)
            continue
        if result.get("ok") and result.get("subscription_name"):
            with session_scope() as session:
                repo.backfill_display_name(session, sub_id, result["subscription_name"])


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
    # Resolve real subscription names before loading the run contexts (network I/O
    # runs outside the transaction above; see _sync_display_names).
    if not settings.finops_mock:
        _sync_display_names()
    with session_scope() as session:
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


def run_policies(subscription: AccountContext, mock: bool | None = None) -> dict[str, Any]:
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
