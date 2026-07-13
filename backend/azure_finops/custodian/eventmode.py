"""Event-mode policy trigger (M6.2) — reactive, real-time enforcement.

Cloud Custodian's *event mode*: instead of a scheduled sweep, an incoming resource
change (delivered by Event Grid and normalized in M6.1) selects and runs only the
policies that both

* declare an **event-grid ``mode``** in their c7n spec, and
* target the **resource type** the event touched,

recording each reactive run as a :class:`PolicyExecution` with ``mode='event'``.

The heavy lifting reuses the pull-mode building blocks — ``run_policy`` via the
injectable ``CustodianRunner`` seam and the orchestrator's execution-id / match /
action helpers — so the whole path stays offline and unit-testable. An event that
matches nothing (unknown or type-less resource, pull-only or disabled policies) is a
**safe no-op**, never an error: the webhook must always drain cleanly.
"""

from __future__ import annotations

import logging
from typing import Any

from ..azure.context import SubscriptionContext
from ..config import get_settings
from ..orchestrator import _declared_actions, _matches_from_result, _new_execution_id
from ..storage import repository as repo
from ..storage.db import init_db, session_scope
from .engine import CustodianRunner, run_policy

logger = logging.getLogger("azure_finops.custodian.eventmode")

# Map c7n's short resource name to the ARM provider type an Event Grid event carries
# (both compared lower-cased). A policy may also be authored with the ARM type
# directly, which matches without going through this table.
_C7N_TO_ARM = {
    "azure.vm": "microsoft.compute/virtualmachines",
    "azure.disk": "microsoft.compute/disks",
    "azure.networkinterface": "microsoft.network/networkinterfaces",
    "azure.publicip": "microsoft.network/publicipaddresses",
    "azure.networksecuritygroup": "microsoft.network/networksecuritygroups",
    "azure.loadbalancer": "microsoft.network/loadbalancers",
    "azure.storage": "microsoft.storage/storageaccounts",
    "azure.sqlserver": "microsoft.sql/servers",
    "azure.sqldatabase": "microsoft.sql/servers/databases",
    "azure.cosmosdb": "microsoft.documentdb/databaseaccounts",
    "azure.keyvault": "microsoft.keyvault/vaults",
    "azure.appserviceplan": "microsoft.web/serverfarms",
    "azure.webapp": "microsoft.web/sites",
}


def _is_event_mode(spec: dict[str, Any]) -> bool:
    """True if the policy's first entry declares an event-grid ``mode`` block."""
    policies = spec.get("policies") or []
    if not policies:
        return False
    mode = policies[0].get("mode")
    if not isinstance(mode, dict):
        return False
    return "event" in str(mode.get("type", "")).lower()


def _resource_type_matches(
    policy_resource_type: str | None, event_resource_type: str | None
) -> bool:
    """True if a policy's ``resource_type`` targets the event's ARM resource type."""
    if not policy_resource_type or not event_resource_type:
        return False
    prt = policy_resource_type.strip().lower()
    ert = event_resource_type.strip().lower()
    return prt == ert or _C7N_TO_ARM.get(prt) == ert


def _matching_policies(policies: list[dict[str, Any]], event: Any) -> list[dict[str, Any]]:
    """The enabled, event-mode policies whose resource type matches the event."""
    return [
        policy
        for policy in policies
        if _is_event_mode(policy["spec"])
        and _resource_type_matches(policy["resource_type"], event.resource_type)
    ]


def handle_event(
    event: Any, runner: CustodianRunner | None = None, dry_run: bool = True
) -> dict[str, Any]:
    """Select and reactively run every event-mode policy that matches ``event``.

    Returns a summary ``{resource_id, resource_type, matched, executions}``. When
    ``event`` is ``None`` / carries no resource type, or nothing matches, it is a
    no-op with ``matched == 0`` (never raises).
    """
    resource_type = getattr(event, "resource_type", None)
    result: dict[str, Any] = {
        "resource_id": getattr(event, "resource_id", None),
        "resource_type": resource_type,
        "matched": 0,
        "executions": [],
    }
    if event is None or not resource_type:
        return result

    init_db()
    with session_scope() as session:
        policies = _matching_policies(repo.list_policies(session, enabled_only=True), event)
    if not policies:
        return result

    logger.info("event on %s matched %d event-mode polic(ies)", resource_type, len(policies))
    result["matched"] = len(policies)
    result["executions"] = [_run_reactive(policy, event, dry_run, runner) for policy in policies]
    return result


def _run_reactive(
    policy: dict[str, Any], event: Any, dry_run: bool, runner: CustodianRunner | None
) -> dict[str, Any]:
    """Run one matched policy for the event's subscription; open/close a ``mode='event'`` row."""
    settings = get_settings()
    sub_id = event.subscription_id or settings.azure_subscription_id
    context = SubscriptionContext(subscription_id=sub_id) if sub_id else None
    execution_id = _new_execution_id()
    with session_scope() as session:
        repo.create_policy_execution(
            session,
            execution_id=execution_id,
            policy_id=policy["id"],
            subscription_id=sub_id,
            mode="event",
        )
    try:
        run_result = run_policy(
            policy["spec"], subscription=context, dry_run=dry_run, runner=runner
        )
        matches = _matches_from_result(run_result, sub_id or "")
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
        return {
            "execution_id": execution_id,
            "policy_id": policy["id"],
            "subscription_id": sub_id,
            "status": "succeeded",
            "resources_matched": len(matches),
        }
    except Exception as exc:  # noqa: BLE001 - isolate a failing policy; the webhook stays healthy
        logger.exception("event-mode policy %s failed on %s", policy["id"], sub_id)
        with session_scope() as session:
            repo.finish_policy_execution(session, execution_id, status="failed", error=str(exc))
        return {
            "execution_id": execution_id,
            "policy_id": policy["id"],
            "subscription_id": sub_id,
            "status": "failed",
            "error": str(exc),
        }
