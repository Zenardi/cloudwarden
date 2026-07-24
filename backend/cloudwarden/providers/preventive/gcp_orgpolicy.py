"""GCP Organization Policy translator (M14.10).

Maps a :class:`GuardrailIntent` to a native **GCP Organization Policy** constraint.
Org Policy expresses allowed locations (``constraints/gcp.resourceLocations``) and
deny-public-IP (``constraints/compute.vmExternalIpAccess``); it has no native
list-constraint for *required labels on create* or *allowed machine types*, so
``required_tag`` and ``allowed_skus`` are reported as *not expressible*. The emitted
dict is the constraint + ``listPolicy`` plus a ``provider`` / ``kind`` / ``metadata``
envelope for traceability.
"""

from __future__ import annotations

from typing import Any

from .base import GuardrailIntent, NotExpressible

PROVIDER = "gcp"
NATIVE_LABEL = "Organization Policy"
SUPPORTED_KINDS = frozenset({"allowed_locations", "deny_public_ip"})

_CONSTRAINTS = {
    "allowed_locations": "constraints/gcp.resourceLocations",
    "deny_public_ip": "constraints/compute.vmExternalIpAccess",
}


def _envelope(intent: GuardrailIntent) -> dict[str, Any]:
    return {
        "provider": PROVIDER,
        "kind": intent.kind,
        "constraint": _CONSTRAINTS[intent.kind],
        "metadata": {"source": "cloudwarden", "policyName": intent.policy_name},
    }


def _allowed_locations(intent: GuardrailIntent) -> dict[str, Any]:
    body = _envelope(intent)
    body["listPolicy"] = {
        "allowedValues": list(intent.params["locations"]),
        "inheritFromParent": False,
    }
    return body


def _deny_public_ip(intent: GuardrailIntent) -> dict[str, Any]:
    body = _envelope(intent)
    body["listPolicy"] = {"allValues": "DENY"}
    return body


_BUILDERS = {
    "allowed_locations": _allowed_locations,
    "deny_public_ip": _deny_public_ip,
}


def translate(intent: GuardrailIntent) -> dict[str, Any]:
    """Return the native Organization Policy definition, or raise :class:`NotExpressible`."""
    builder = _BUILDERS.get(intent.kind)
    if builder is None:
        raise NotExpressible(
            f"GCP Organization Policy cannot express guardrail kind '{intent.kind}'"
        )
    return builder(intent)


def scope(intent: GuardrailIntent, *, target: str | None = None) -> dict[str, Any]:
    """The what-if scope: the organization/folder/project the constraint would bind to."""
    return {
        "native": NATIVE_LABEL,
        "level": "organization",
        "target": target or "organization",
        "constraint": _CONSTRAINTS[intent.kind],
    }
