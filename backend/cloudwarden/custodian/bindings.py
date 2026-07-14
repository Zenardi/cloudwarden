"""Binding execution engine (M5.3) — run governance at scale.

Executing a binding runs **every policy in its collection** across **every enabled
subscription in its account group**, recording one ``PolicyExecution`` (tagged with
``binding_id``) per policy × subscription. Reuses the pull-mode executor's building
blocks (``run_policy`` via the injectable ``CustodianRunner`` seam, plus the
orchestrator's execution-id / match / action helpers) so tests stay fully offline.

* a **disabled** binding is a no-op (``status="skipped"``);
* the binding's **``dry_run``** flag is passed through to every policy run — no actions
  are executed when it is set;
* an **unknown** binding returns ``None`` (the API maps that to ``404``).
"""

from __future__ import annotations

import logging
from typing import Any

from ..config import get_settings
from ..orchestrator import (
    _context_from_record,
    _declared_actions,
    _matches_from_result,
    _new_execution_id,
)
from ..storage import repository as repo
from ..storage.db import init_db, session_scope
from .engine import CustodianRunner, run_policy

logger = logging.getLogger("cloudwarden.custodian.bindings")


def run_binding(
    binding_id: int, runner: CustodianRunner | None = None, mock: bool | None = None
) -> dict[str, Any] | None:
    """Execute a binding: its collection's policies × its group's subscriptions.

    Returns ``None`` if the binding does not exist, a ``skipped`` result if it is
    disabled, else a ``completed`` result listing one execution per policy × subscription.
    """
    settings = get_settings()
    if mock is not None:
        settings.finops_mock = mock
    init_db()

    with session_scope() as session:
        binding = repo.get_binding(session, binding_id)
    if binding is None:
        return None
    if not binding["enabled"]:
        logger.info("binding %s is disabled; skipping", binding_id)
        return {
            "binding_id": binding_id,
            "status": "skipped",
            "reason": "disabled",
            "executions": [],
        }

    dry_run = binding["dry_run"]
    with session_scope() as session:
        policies = repo.policies_in_collection(session, binding["collection_id"], enabled_only=True)
        contexts = [
            _context_from_record(record, settings.finops_mock)
            for record in repo.subscriptions_in_group(
                session, binding["account_group_id"], enabled_only=True
            )
        ]
    logger.info(
        "binding %s: %d polic(ies) × %d subscription(s), dry_run=%s",
        binding_id,
        len(policies),
        len(contexts),
        dry_run,
    )

    executions: list[dict[str, Any]] = []
    for context in contexts:
        for policy in policies:
            executions.append(_run_one(binding_id, policy, context, dry_run, runner))
    return {
        "binding_id": binding_id,
        "status": "completed",
        "dry_run": dry_run,
        "executions": executions,
    }


def _run_one(
    binding_id: int,
    policy: dict[str, Any],
    context: Any,
    dry_run: bool,
    runner: CustodianRunner | None,
) -> dict[str, Any]:
    """Run one policy against one subscription; open/close its tagged execution row."""
    sub_id = context.subscription_id
    execution_id = _new_execution_id()
    with session_scope() as session:
        repo.create_policy_execution(
            session,
            execution_id=execution_id,
            policy_id=policy["id"],
            subscription_id=sub_id,
            binding_id=binding_id,
        )
    try:
        result = run_policy(policy["spec"], subscription=context, dry_run=dry_run, runner=runner)
        matches = _matches_from_result(result, sub_id)
        # In dry-run no action is executed, so record none; otherwise the declared actions.
        actions = [] if dry_run else _declared_actions(policy["spec"])
        with session_scope() as session:
            repo.insert_policy_matches(session, execution_id, matches)
            repo.finish_policy_execution(
                session,
                execution_id,
                status="succeeded",
                resources_matched=len(matches),
                actions_taken=actions,
            )
        # M8.4: a violation on a binding with a channel fires a notification. Runs
        # after the execution commits and never raises — a failed notification must
        # not fail enforcement.
        if matches:
            _fire_binding_notifications(binding_id, policy, matches)
        return {
            "execution_id": execution_id,
            "policy_id": policy["id"],
            "subscription_id": sub_id,
            "status": "succeeded",
            "resources_matched": len(matches),
        }
    except Exception as exc:  # noqa: BLE001 - per (policy × subscription) isolation
        logger.exception("binding %s policy %s failed on %s", binding_id, policy["id"], sub_id)
        with session_scope() as session:
            repo.finish_policy_execution(session, execution_id, status="failed", error=str(exc))
        return {
            "execution_id": execution_id,
            "policy_id": policy["id"],
            "subscription_id": sub_id,
            "status": "failed",
            "error": str(exc),
        }


def _fire_binding_notifications(
    binding_id: int, policy: dict[str, Any], matches: list[Any]
) -> None:
    """Dispatch this binding's configured channels for a violation (best-effort).

    Opens its own session so the notification I/O is outside the enforcement
    transaction, and swallows every error — a broken channel or template must never
    fail the binding run that produced the violation.
    """
    from ..notify.dispatch import dispatch_for_binding

    try:
        with session_scope() as session:
            dispatch_for_binding(
                session,
                binding_id=binding_id,
                policy_name=policy.get("name", ""),
                resource_ids=[m.resource_id for m in matches],
                resource_type=matches[0].resource_type,
            )
    except Exception:  # pragma: no cover - defensive; notifications never break a run
        logger.exception("binding %s notification dispatch failed", binding_id)


def run_enabled_bindings(runner: CustodianRunner | None = None) -> dict[str, Any]:
    """Run every enabled binding once (used by the scheduler / a manual sweep)."""
    init_db()
    with session_scope() as session:
        binding_ids = [b["id"] for b in repo.list_bindings(session) if b["enabled"]]
    runs = [run_binding(bid, runner=runner) for bid in binding_ids]
    logger.info("ran %d enabled binding(s)", len(runs))
    return {"bindings": len(runs), "runs": runs}
