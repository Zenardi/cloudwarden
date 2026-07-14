"""Remediation guardrails (block-by-default).

A resource is only actionable when **every** guardrail passes: it carries no
exclude tag, its resource group is on the allow-list, and — for policy actions —
the attempted action type is permitted by the binding's allow-list. An empty
resource-group allow-list denies everything (safe default); ``*`` allows any.
An empty action allow-list places no restriction on which action types may run.

``default_dry_run`` gives the safe default execution mode: with guardrails unset
(remediation disabled, or no resource group allow-listed) an action previews as a
dry-run and never touches Azure.

These are pure functions — unit-tested without Azure or a database.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import Settings

# Cloud Custodian's own opt-out convention: a resource tagged ``custodian:exclude``
# is never actioned. Recognised in addition to the configurable ``EXCLUDE_TAG``.
CUSTODIAN_EXCLUDE: tuple[str, str] = ("custodian", "exclude")


@dataclass
class GuardResult:
    allowed: bool
    reasons: list[str] = field(default_factory=list)


def resource_group_of(resource_id: str) -> str | None:
    parts = (resource_id or "").lower().split("/")
    if "resourcegroups" in parts:
        idx = parts.index("resourcegroups")
        if idx + 1 < len(parts):
            return parts[idx + 1]
    return None


def check(
    resource_id: str,
    tags: dict | None,
    settings: Settings,
    *,
    action: str | None = None,
    allowed_actions: list[str] | None = None,
) -> GuardResult:
    """Evaluate every guardrail for ``resource_id``; a passing result has no reasons.

    ``action`` is the Custodian action *type* being attempted (e.g. ``"stop"``);
    ``allowed_actions`` is the binding's per-type allow-list. When omitted, the
    global ``ALLOWED_ACTIONS`` setting is used. An empty allow-list permits any
    action type.
    """
    reasons: list[str] = []
    tags = tags or {}

    # Exclude tags: the configured EXCLUDE_TAG plus the built-in custodian:exclude.
    excludes = [CUSTODIAN_EXCLUDE]
    if settings.exclude_tag_kv:
        excludes.append(settings.exclude_tag_kv)
    for key, value in excludes:
        if any(
            tk.lower() == key.lower() and str(tv).lower() == value.lower()
            for tk, tv in tags.items()
        ):
            reasons.append(f"excluded by tag {key}={value}")

    # Resource-group allow-list (block-by-default: empty denies everything).
    allow = [a.lower() for a in settings.allowed_rg_list]
    rg = resource_group_of(resource_id)
    if "*" in allow:
        pass
    elif not allow:
        reasons.append("no resource groups are allow-listed (set ALLOWED_RESOURCE_GROUPS)")
    elif rg not in allow:
        reasons.append(f"resource group '{rg}' is not in the allow-list")

    # Per-binding action-type allow-list (empty ⇒ no restriction).
    permitted = allowed_actions if allowed_actions is not None else settings.allowed_actions_list
    if action and permitted and action.lower() not in [a.lower() for a in permitted]:
        reasons.append(f"action type '{action}' is not in the allow-list")

    return GuardResult(allowed=not reasons, reasons=reasons)


def default_dry_run(settings: Settings) -> bool:
    """The safe default execution mode for a policy action.

    Guardrails are "unset" when remediation is globally disabled or no resource
    group is allow-listed — in either case an action previews as a dry-run and
    never touches Azure. Only a fully configured guardrail permits real execution.
    """
    if not settings.remediation_enabled:
        return True
    if not settings.allowed_rg_list:
        return True
    return False
