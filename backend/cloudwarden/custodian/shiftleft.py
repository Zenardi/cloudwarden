"""Shift-left IaC policy evaluation (M14.6).

Every other governance control runs *after* provisioning — a violation is caught only
once the resource exists and bills. This evaluates the **same authored c7n policies**
against a **Terraform plan** (plan JSON) so a violation fails the PR/CI **before**
anything is created.

The flow is deliberately offline and reuses the existing engine seam:

1. :func:`parse_plan` normalizes a Terraform plan (walking child modules) into flat
   resource dicts — each attribute lifted to the top level so c7n ``value`` filters
   apply, plus ``__address__`` / ``__tf_type__`` for reporting.
2. :func:`map_tf_type` maps a Terraform resource type (``azurerm_storage_account``) to
   a c7n resource type (``azure.storage``); an **unmapped** type is *skipped*
   (reported, never an error), and a **malformed** plan raises a clean
   :class:`ShiftLeftError`.
3. :func:`evaluate_plan` selects the plan resources each policy targets and runs the
   policy's filters through the injectable **matcher seam** (default
   :func:`engine.match_resources` — the local c7n filter machinery a dry-run uses), so
   no live cloud or Terraform is needed. Each match carries the policy, the resource
   *address*, a *severity*, and a rationale; :meth:`ShiftLeftResult.exit_code` maps the
   worst severity to a CI exit code so a violating plan **blocks the merge**.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from . import engine

# Severity ordering for the CI gate; unknown labels sort at the bottom.
SEVERITY_ORDER = {"low": 1, "medium": 2, "high": 3, "critical": 4}
DEFAULT_SEVERITY = "medium"

# Terraform resource type → c7n resource type. Extend as coverage grows; a type absent
# here is *skipped* (surfaced in ``ShiftLeftResult.skipped``), never a hard error.
TF_TO_C7N: dict[str, str] = {
    "azurerm_storage_account": "azure.storage",
    "azurerm_virtual_machine": "azure.vm",
    "azurerm_linux_virtual_machine": "azure.vm",
    "azurerm_windows_virtual_machine": "azure.vm",
    "azurerm_managed_disk": "azure.disk",
    "azurerm_public_ip": "azure.publicip",
    "azurerm_network_security_group": "azure.networksecuritygroup",
    "azurerm_key_vault": "azure.keyvault",
    "azurerm_cosmosdb_account": "azure.cosmosdb",
    "azurerm_lb": "azure.loadbalancer",
    "azurerm_linux_web_app": "azure.webapp",
    "azurerm_windows_web_app": "azure.webapp",
    "azurerm_app_service": "azure.webapp",
}

MatchFn = Callable[[dict, list[dict]], list[dict]]


class ShiftLeftError(Exception):
    """A plan could not be parsed/evaluated — surfaced cleanly (422 / stderr), not a trace."""


@dataclass(frozen=True)
class Match:
    """One policy violation found in the plan, located by its Terraform address."""

    policy: str
    resource_address: str
    resource_type: str  # the Terraform type (e.g. ``azurerm_storage_account``)
    c7n_type: str  # the mapped c7n type (e.g. ``azure.storage``)
    severity: str
    rationale: str


@dataclass(frozen=True)
class ShiftLeftResult:
    """The outcome of evaluating a plan: matches + what was evaluated/skipped."""

    matches: list[Match]
    evaluated: int  # resources actually run through a policy
    policies_run: int
    skipped: list[str] = field(default_factory=list)  # Terraform types with no c7n mapping

    def worst_severity(self) -> str | None:
        """The highest severity among the matches (``None`` when there are none)."""
        if not self.matches:
            return None
        return max(self.matches, key=lambda m: SEVERITY_ORDER.get(m.severity, 0)).severity

    def exit_code(self, fail_on: str | None = None) -> int:
        """CI exit code: ``0`` clean, ``1`` when a violation should block the merge.

        With no ``fail_on`` any match fails the build. With ``fail_on`` set (a severity
        label) only a match **at or above** that severity fails — lower-severity findings
        are reported but do not block.
        """
        if not self.matches:
            return 0
        if fail_on is None:
            return 1
        threshold = SEVERITY_ORDER.get(fail_on.lower(), 1)
        worst = max(SEVERITY_ORDER.get(m.severity, 0) for m in self.matches)
        return 1 if worst >= threshold else 0


def normalize_c7n_type(resource_type: str) -> str:
    """Normalize a policy's resource type to the ``azure.<name>`` form used for matching."""
    resource_type = (resource_type or "").strip()
    return resource_type if resource_type.startswith("azure.") else f"azure.{resource_type}"


def map_tf_type(tf_type: str) -> str | None:
    """The c7n resource type for a Terraform type, or ``None`` when unmapped (skipped)."""
    return TF_TO_C7N.get(tf_type)


def _normalize_resource(node: dict[str, Any]) -> dict[str, Any]:
    """Flatten one plan resource node into a c7n-filterable dict."""
    values = node.get("values") or {}
    return {
        **values,
        "name": values.get("name") or node.get("name"),
        "__address__": node.get("address"),
        "__tf_type__": node.get("type"),
    }


def _walk_module(module: dict[str, Any], out: list[dict[str, Any]]) -> None:
    """Recursively collect resources from a plan module and its child modules."""
    if not isinstance(module, dict):
        raise ShiftLeftError("malformed plan: module is not an object")
    for node in module.get("resources") or []:
        if isinstance(node, dict) and node.get("type") and node.get("address"):
            out.append(_normalize_resource(node))
    for child in module.get("child_modules") or []:
        _walk_module(child, out)


def parse_plan(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize a Terraform plan JSON into flat resource dicts (walks child modules).

    Raises :class:`ShiftLeftError` for a plan missing the expected
    ``planned_values.root_module`` shape — a clean error rather than a ``KeyError``.
    """
    if not isinstance(plan, dict):
        raise ShiftLeftError("malformed plan: not a JSON object")
    planned = plan.get("planned_values")
    if not isinstance(planned, dict):
        raise ShiftLeftError("malformed plan: missing 'planned_values' object")
    root = planned.get("root_module")
    if not isinstance(root, dict):
        raise ShiftLeftError("malformed plan: missing 'planned_values.root_module' object")
    resources: list[dict[str, Any]] = []
    _walk_module(root, resources)
    return resources


def policy_severity(policy: dict[str, Any]) -> str:
    """The severity for a policy — its ``metadata.severity`` or :data:`DEFAULT_SEVERITY`."""
    spec = policy.get("spec") or {}
    policy_defs = spec.get("policies") or [{}]
    metadata = policy_defs[0].get("metadata") or {}
    return str(metadata.get("severity") or policy.get("severity") or DEFAULT_SEVERITY).lower()


def evaluate_plan(
    plan: dict[str, Any],
    policies: Iterable[dict[str, Any]],
    *,
    match_fn: MatchFn | None = None,
) -> ShiftLeftResult:
    """Evaluate ``policies`` against a Terraform ``plan``; return matches + counts.

    For each policy, select the plan resources whose mapped c7n type equals the policy's
    resource type, then run the policy's filters through ``match_fn`` (default
    :func:`engine.match_resources` — offline c7n). Unmapped Terraform types are collected
    in ``skipped``. A malformed plan raises :class:`ShiftLeftError`.
    """
    # Enable the live c7n IaC provider when installed; otherwise the default matcher is
    # the offline azure filter path (best-effort, never raises).
    engine.register_terraform()
    match_fn = match_fn or engine.match_resources
    resources = parse_plan(plan)
    policies = list(policies)

    skipped = sorted({r["__tf_type__"] for r in resources if map_tf_type(r["__tf_type__"]) is None})
    matches: list[Match] = []
    evaluated = 0
    for policy in policies:
        target = normalize_c7n_type(policy.get("resource_type", ""))
        selected = [r for r in resources if map_tf_type(r["__tf_type__"]) == target]
        if not selected:
            continue
        evaluated += len(selected)
        severity = policy_severity(policy)
        rationale = policy.get("description") or f"{policy.get('name')} matched"
        for violating in match_fn(policy["spec"], selected):
            matches.append(
                Match(
                    policy=policy.get("name", "?"),
                    resource_address=violating.get("__address__", violating.get("name", "?")),
                    resource_type=violating.get("__tf_type__", ""),
                    c7n_type=target,
                    severity=severity,
                    rationale=rationale,
                )
            )

    return ShiftLeftResult(
        matches=matches,
        evaluated=evaluated,
        policies_run=len(policies),
        skipped=skipped,
    )


def result_public(result: ShiftLeftResult) -> dict[str, Any]:
    """Serialize a result for the JSON API."""
    return {
        "violations": len(result.matches),
        "evaluated": result.evaluated,
        "policies_run": result.policies_run,
        "skipped": result.skipped,
        "worst_severity": result.worst_severity(),
        "matches": [
            {
                "policy": m.policy,
                "resource_address": m.resource_address,
                "resource_type": m.resource_type,
                "c7n_type": m.c7n_type,
                "severity": m.severity,
                "rationale": m.rationale,
            }
            for m in result.matches
        ],
    }
