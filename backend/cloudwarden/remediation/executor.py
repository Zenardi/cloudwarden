"""Execute (or dry-run) a remediation action against Azure via the write SP.

Two families live here:

* **Recommendation-driven** actions (:func:`execute`) — VM deallocate, VM resize,
  delete unattached disk, delete idle public IP — resolved from FinOps findings.
* **Custodian actions** (:func:`execute_action`, M7.1) — the ``tag``,
  ``mark-for-op``, ``stop`` and ``delete`` actions declared on a Cloud Custodian
  policy, executed against a matched resource through **injectable** Azure clients.

Both honour ``dry_run=True`` (a preview with **zero** Azure calls, fully testable
offline) and use the write-scoped credential on the live path.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from ..config import Settings
from ..resilience import with_retry

logger = logging.getLogger("cloudwarden.remediation.executor")

SUPPORTED = {"deallocate", "resize", "delete_disk", "delete_public_ip"}

# c7n actions this executor can map to an Azure SDK call (M7.1).
CUSTODIAN_ACTIONS = {"tag", "mark-for-op", "stop", "delete"}

# Default tag key used by ``mark-for-op`` (mirrors c7n's ``custodian_status``).
MARK_TAG_DEFAULT = "custodian_status"


def _parse(resource_id: str) -> dict[str, str | None]:
    parts = resource_id.split("/")
    fields: dict[str, str | None] = {}
    i = 1
    while i + 1 < len(parts):
        fields[parts[i].lower()] = parts[i + 1]
        i += 2
    fields["name"] = parts[-1] if parts else None
    return fields


def preview(action_type: str, resource_id: str, params: dict[str, Any]) -> dict[str, Any]:
    target = params.get("recommended_sku")
    extra = f" → {target}" if action_type == "resize" and target else ""
    return {
        "executed": False,
        "dry_run": True,
        "action": action_type,
        "resource_id": resource_id,
        "message": f"[dry-run] would {action_type} {resource_id}{extra}",
    }


def execute(
    action_type: str,
    resource_id: str,
    params: dict[str, Any],
    settings: Settings,
    credential: Any = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    if dry_run:
        return preview(action_type, resource_id, params)
    if action_type not in SUPPORTED:
        return {
            "executed": False,
            "dry_run": False,
            "action": action_type,
            "resource_id": resource_id,
            "message": f"action '{action_type}' is not auto-executable; handle manually",
        }
    return _execute_live(action_type, resource_id, params, settings, credential)


@with_retry()
def _execute_live(
    action_type: str,
    resource_id: str,
    params: dict[str, Any],
    settings: Settings,
    credential: Any,
) -> dict[str, Any]:
    from ..auth import write_credential

    cred = credential or write_credential()
    ids = _parse(resource_id)
    sub = ids.get("subscriptions") or settings.azure_subscription_id
    rg = ids.get("resourcegroups")
    name = ids.get("name")

    if action_type in ("deallocate", "resize", "delete_disk"):
        from azure.mgmt.compute import ComputeManagementClient

        compute = ComputeManagementClient(cred, sub)
        if action_type == "deallocate":
            compute.virtual_machines.begin_deallocate(rg, name).result()
        elif action_type == "resize":
            sku = params.get("recommended_sku")
            compute.virtual_machines.begin_update(
                rg, name, {"hardware_profile": {"vm_size": sku}}
            ).result()
        else:  # delete_disk
            compute.disks.begin_delete(rg, name).result()
    elif action_type == "delete_public_ip":
        from azure.mgmt.network import NetworkManagementClient

        network = NetworkManagementClient(cred, sub)
        network.public_ip_addresses.begin_delete(rg, name).result()

    return {
        "executed": True,
        "dry_run": False,
        "action": action_type,
        "resource_id": resource_id,
        "message": f"{action_type} completed",
    }


# --------------------------------------------------------------------------- #
# Custodian actions (M7.1): tag / mark-for-op / stop / delete
# --------------------------------------------------------------------------- #
@dataclass
class ActionClients:
    """Injectable Azure SDK clients for custodian-action execution (test seam).

    Live code builds these from the write-scoped credential via
    :func:`_build_action_clients`; unit tests inject spies so no real SDK call is
    ever made.
    """

    compute: Any = None
    resource: Any = None


class _ActionError(Exception):
    """A recoverable, action-level failure surfaced as a structured error dict."""


def normalize_action(action: str | dict) -> dict:
    """Normalize a c7n action to a ``{"type": ...}`` dict.

    Accepts the string shorthand (``"stop"``) or the full mapping
    (``{"type": "tag", ...}``). Raises :class:`ValueError` for anything that
    cannot name an action type.
    """
    if isinstance(action, str):
        return {"type": action}
    if isinstance(action, dict) and action.get("type"):
        return dict(action)
    raise ValueError(f"invalid action (missing 'type'): {action!r}")


def _resource_kind(resource: dict) -> str:
    """Coarse class of a matched resource from its ARM/c7n type: 'vm'/'disk'/''."""
    t = str(resource.get("type") or "").lower()
    if "virtualmachine" in t or t == "azure.vm":
        return "vm"
    if "disk" in t or t == "azure.disk":
        return "disk"
    return ""


def _tags_for(spec: dict, resource: dict) -> dict:
    """The tag payload a ``tag`` / ``mark-for-op`` action should write."""
    if spec["type"] == "mark-for-op":
        key = spec.get("tag") or MARK_TAG_DEFAULT
        return {key: f"marked-for-op:{spec.get('op', 'stop')}"}
    # plain tag: either an explicit {tags: {...}} map or a single tag/value pair.
    if spec.get("tags"):
        return dict(spec["tags"])
    key = spec.get("tag") or spec.get("key")
    return {key: spec.get("value")}


def execute_action(
    action: str | dict,
    resource: dict,
    *,
    settings: Settings,
    clients: ActionClients | None = None,
    credential: Any = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Execute one Cloud Custodian action against one matched resource.

    ``dry_run=True`` returns a preview and makes **zero** Azure calls. Live
    execution dispatches through ``clients`` (built from the write-scoped
    credential when not injected). Unknown action types — or actions that do not
    apply to the resource kind — return a structured ``{"executed": False,
    "error": ...}`` dict rather than raising.
    """
    spec = normalize_action(action)
    atype = spec["type"]
    resource_id = resource.get("id") or resource.get("resource_id") or ""

    if atype not in CUSTODIAN_ACTIONS:
        return _action_error(atype, resource_id, f"unsupported action type: '{atype}'")

    if dry_run:
        return {
            "executed": False,
            "dry_run": True,
            "action": atype,
            "resource_id": resource_id,
            "message": f"[dry-run] would {atype} {resource_id}",
        }

    ids = _parse(resource_id)
    sub = ids.get("subscriptions") or settings.azure_subscription_id
    rg = ids.get("resourcegroups")
    name = ids.get("name")
    if clients is None:
        clients = _build_action_clients(sub, credential)

    try:
        _dispatch_action(spec, resource, resource_id, rg, name, clients)
    except _ActionError as exc:
        return _action_error(atype, resource_id, str(exc))

    return {
        "executed": True,
        "dry_run": False,
        "action": atype,
        "resource_id": resource_id,
        "message": f"{atype} completed",
    }


def _dispatch_action(
    spec: dict,
    resource: dict,
    resource_id: str,
    rg: str | None,
    name: str | None,
    clients: ActionClients,
) -> None:
    atype = spec["type"]
    if atype in ("tag", "mark-for-op"):
        clients.resource.tags.create_or_update_at_scope(
            scope=resource_id,
            parameters={"operation": "Merge", "properties": {"tags": _tags_for(spec, resource)}},
        )
        return

    kind = _resource_kind(resource)
    if atype == "stop":
        if kind != "vm":
            raise _ActionError(f"'stop' is only supported for VMs, not {resource.get('type')!r}")
        clients.compute.virtual_machines.begin_deallocate(rg, name).result()
        return

    # atype == "delete"
    if kind == "vm":
        clients.compute.virtual_machines.begin_delete(rg, name).result()
    elif kind == "disk":
        clients.compute.disks.begin_delete(rg, name).result()
    else:
        raise _ActionError(f"'delete' is not supported for {resource.get('type')!r}")


def _build_action_clients(subscription_id: str, credential: Any) -> ActionClients:
    """Build live Azure SDK clients for the write path (from ``write_credential``)."""
    from azure.mgmt.compute import ComputeManagementClient
    from azure.mgmt.resource import ResourceManagementClient

    from ..auth import write_credential

    cred = credential or write_credential()
    return ActionClients(
        compute=ComputeManagementClient(cred, subscription_id),
        resource=ResourceManagementClient(cred, subscription_id),
    )


def _action_error(action_type: str, resource_id: str, message: str) -> dict[str, Any]:
    return {
        "executed": False,
        "dry_run": False,
        "action": action_type,
        "resource_id": resource_id,
        "error": message,
        "message": message,
    }
