"""Guarded remediation of an approved recommendation.

Flow: recommendation must be `approved` → build a dry-run/execute plan
(REMEDIATION_ENABLED forces dry-run when false) → guardrails (exclude tag +
allow-list) → execute (skipped in mock mode) → record a `remediation_actions`
audit row and update the recommendation status. Every attempt is persisted.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from ..config import get_settings
from ..storage import schema
from . import executor, guardrails

logger = logging.getLogger("cloudwarden.remediation")


class NotFound(Exception):
    pass


class NotApproved(Exception):
    pass


class AlreadyDecided(Exception):
    """The action has already left ``pending`` (approved/rejected/executed/…)."""


def _result(action: schema.RemediationAction) -> dict[str, Any]:
    message = (action.result or {}).get("message") if action.result else None
    return {
        "action_id": action.id,
        "recommendation_id": action.recommendation_id,
        "policy_match_id": action.policy_match_id,
        "source": action.source,
        "policy_id": action.policy_id,
        "action_type": action.action_type,
        "dry_run": action.dry_run,
        "status": action.status,
        "message": message,
        "error": action.error,
    }


def remediate(
    session: Session, rec_id: int, actor: str | None = None, dry_run: bool = True
) -> dict[str, Any]:
    settings = get_settings()
    rec = session.get(schema.Recommendation, rec_id)
    if rec is None:
        raise NotFound(f"recommendation {rec_id} not found")
    if rec.status not in ("approved", "failed"):
        raise NotApproved(f"recommendation status is '{rec.status}'; must be 'approved'")

    # When remediation is disabled globally, force dry-run — never touch Azure.
    effective_dry_run = True if not settings.remediation_enabled else dry_run
    action_type = rec.action or rec.category
    resource = session.get(schema.Resource, rec.resource_id)
    tags = resource.tags if resource else {}

    action = schema.RemediationAction(
        recommendation_id=rec.id,
        action_type=action_type,
        params={"resource_id": rec.resource_id, "recommended_sku": rec.recommended_sku},
        dry_run=effective_dry_run,
        actor=actor,
        status="pending",
    )
    session.add(action)
    session.flush()

    guard = guardrails.check(rec.resource_id, tags, settings)
    guard_note = (
        "" if guard.allowed else " (guardrails would block: " + "; ".join(guard.reasons) + ")"
    )
    # Guardrails hard-block only real execution; a dry-run still previews.
    if not guard.allowed and not effective_dry_run:
        action.status = "blocked"
        action.error = "; ".join(guard.reasons)
        logger.info("remediation blocked for %s: %s", rec.resource_id, action.error)
        return _result(action)

    if settings.finops_mock:
        phase = "dry-run" if effective_dry_run else "mock-exec"
        action.result = {
            "mock": True,
            "message": f"[{phase}] {action_type} {rec.resource_id}{guard_note}",
        }
        action.status = "dry_run" if effective_dry_run else "executed"
        action.executed_at = datetime.now(UTC)
        if not effective_dry_run:
            rec.status = "executed"
            rec.decided_by = actor
        return _result(action)

    try:
        rec.status = "executing"
        session.flush()
        from ..auth import write_credential

        res = executor.execute(
            action_type,
            rec.resource_id,
            action.params,
            settings,
            credential=write_credential(),
            dry_run=effective_dry_run,
        )
        if guard_note and isinstance(res.get("message"), str):
            res["message"] += guard_note
        action.result = res
        action.executed_at = datetime.now(UTC)
        if res.get("executed"):
            action.status = "executed"
            rec.status = "executed"
        else:
            action.status = "dry_run" if effective_dry_run else "skipped"
            rec.status = "approved"  # not executed → remains actionable
    except Exception as exc:  # noqa: BLE001 - recorded on the audit row
        action.status = "failed"
        action.error = str(exc)
        rec.status = "failed"
        logger.exception("remediation failed for %s", rec.resource_id)
    return _result(action)


# --------------------------------------------------------------------------- #
# Policy-action approval workflow (M7.2)
# --------------------------------------------------------------------------- #
# A resource a policy matched has its action **queued pending**; a human then
# approves (→ guarded execution) or rejects (→ never executes). The state machine
# is strict: only a ``pending`` action can be decided.
_PENDING = "pending"


def queue_policy_action(
    session: Session,
    policy_match_id: int,
    action: str | dict,
    *,
    actor: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Queue a policy-match-derived action as **pending** — this never executes.

    ``action`` is a Cloud Custodian action (string shorthand or ``{"type": ...}``
    mapping); it is normalized and stored with the originating match so approval
    can later enforce it. Raises :class:`NotFound` for an unknown match and
    ``ValueError`` for an action with no resolvable type.
    """
    match = session.get(schema.PolicyMatch, policy_match_id)
    if match is None:
        raise NotFound(f"policy match {policy_match_id} not found")
    spec = executor.normalize_action(action)
    # Provenance for the unified audit trail (M7.4): resolve the originating policy
    # and whether the run was binding-triggered, so the row is self-describing.
    execution = session.get(schema.PolicyExecution, match.execution_id)
    policy_id = execution.policy_id if execution else None
    source = "binding" if (execution and execution.binding_id) else "policy"
    row = schema.RemediationAction(
        policy_match_id=policy_match_id,
        source=source,
        policy_id=policy_id,
        action_type=spec["type"],
        params={
            "action": spec,
            "resource_id": match.resource_id,
            "resource_type": match.resource_type,
        },
        dry_run=dry_run,
        actor=actor,
        status=_PENDING,
    )
    session.add(row)
    session.flush()
    return _result(row)


def approve_action(session: Session, action_id: int, *, actor: str | None = None) -> dict[str, Any]:
    """Approve a pending policy action → execute it (guarded) → record the outcome."""
    action = _pending_or_raise(session, action_id)
    action.status = "approved"
    if actor:
        action.actor = actor
    session.flush()
    return _execute_policy_action(session, action)


def reject_action(session: Session, action_id: int, *, actor: str | None = None) -> dict[str, Any]:
    """Reject a pending policy action — it is never executed."""
    action = _pending_or_raise(session, action_id)
    action.status = "rejected"
    if actor:
        action.actor = actor
    action.executed_at = datetime.now(UTC)
    return _result(action)


def _pending_or_raise(session: Session, action_id: int) -> schema.RemediationAction:
    action = session.get(schema.RemediationAction, action_id)
    if action is None:
        raise NotFound(f"remediation action {action_id} not found")
    if action.status != _PENDING:
        raise AlreadyDecided(
            f"action {action_id} is '{action.status}'; only 'pending' can be decided"
        )
    return action


def _execute_policy_action(session: Session, action: schema.RemediationAction) -> dict[str, Any]:
    """Enforce an approved action through the M7.1 executor, honouring guardrails."""
    settings = get_settings()
    params = action.params or {}
    resource_id = params.get("resource_id") or ""
    resource = {"id": resource_id, "type": params.get("resource_type")}
    spec = params.get("action") or {"type": action.action_type}

    # Unset guardrails (kill-switch off, or no RG allow-listed) force a dry-run —
    # never touch Azure. An explicit dry_run request is likewise honoured.
    effective_dry_run = action.dry_run or guardrails.default_dry_run(settings)
    res_obj = session.get(schema.Resource, resource_id) if resource_id else None
    tags = res_obj.tags if res_obj else {}
    guard = guardrails.check(resource_id, tags, settings, action=action.action_type)
    guard_note = (
        "" if guard.allowed else " (guardrails would block: " + "; ".join(guard.reasons) + ")"
    )

    # Guardrails hard-block only real execution; a dry-run still previews.
    if not guard.allowed and not effective_dry_run:
        action.status = "blocked"
        action.error = "; ".join(guard.reasons)
        logger.info("policy action blocked for %s: %s", resource_id, action.error)
        return _result(action)

    if settings.finops_mock:
        phase = "dry-run" if effective_dry_run else "mock-exec"
        action.result = {
            "mock": True,
            "message": f"[{phase}] {action.action_type} {resource_id}{guard_note}",
        }
        action.status = "dry_run" if effective_dry_run else "executed"
        action.executed_at = datetime.now(UTC)
        return _result(action)

    try:
        from ..auth import write_credential

        res = executor.execute_action(
            spec,
            resource,
            settings=settings,
            credential=write_credential(),
            dry_run=effective_dry_run,
        )
        if guard_note and isinstance(res.get("message"), str):
            res["message"] += guard_note
        action.result = res
        action.executed_at = datetime.now(UTC)
        if res.get("executed"):
            action.status = "executed"
        elif res.get("error"):
            action.status = "failed"
            action.error = res["error"]
        else:
            action.status = "dry_run" if effective_dry_run else "skipped"
    except Exception as exc:  # noqa: BLE001 - recorded on the audit row
        action.status = "failed"
        action.error = str(exc)
        logger.exception("policy action execution failed for %s", resource_id)
    return _result(action)
