"""Azure Policy translator (M14.10).

Maps a :class:`GuardrailIntent` to a native **Azure Policy** definition with a
``deny`` effect. Azure expresses the widest subset — required tags, allowed
locations, allowed VM SKUs, and deny-public-IP — so ``SUPPORTED_KINDS`` covers every
:data:`GUARDRAIL_KINDS` entry. The emitted dict is CloudWarden's canonical
representation: the standard Azure Policy body (``policyRule`` / ``mode`` /
``parameters``) plus a small ``provider`` / ``kind`` / ``metadata`` envelope for
traceability and preview.
"""

from __future__ import annotations

from typing import Any

from .base import GUARDRAIL_KINDS, GuardrailIntent, NotExpressible

PROVIDER = "azure"
NATIVE_LABEL = "Azure Policy"
SUPPORTED_KINDS = frozenset(GUARDRAIL_KINDS)  # Azure expresses the full subset.


def _envelope(intent: GuardrailIntent, *, display_name: str, mode: str) -> dict[str, Any]:
    return {
        "provider": PROVIDER,
        "kind": intent.kind,
        "displayName": display_name,
        "policyType": "Custom",
        "mode": mode,
        "metadata": {"source": "cloudwarden", "policyName": intent.policy_name},
    }


def _required_tag(intent: GuardrailIntent) -> dict[str, Any]:
    tag = intent.params["tag"]
    body = _envelope(intent, display_name=f"Require tag '{tag}'", mode="Indexed")
    body["policyRule"] = {
        "if": {"field": f"tags['{tag}']", "exists": "false"},
        "then": {"effect": "deny"},
    }
    body["parameters"] = {}
    return body


def _allowed_locations(intent: GuardrailIntent) -> dict[str, Any]:
    locations = list(intent.params["locations"])
    body = _envelope(intent, display_name="Allowed locations", mode="Indexed")
    body["policyRule"] = {
        "if": {"not": {"field": "location", "in": locations}},
        "then": {"effect": "deny"},
    }
    body["parameters"] = {}
    return body


def _allowed_skus(intent: GuardrailIntent) -> dict[str, Any]:
    skus = list(intent.params["skus"])
    body = _envelope(intent, display_name="Allowed virtual machine SKUs", mode="Indexed")
    body["policyRule"] = {
        "if": {
            "allOf": [
                {"field": "type", "equals": "Microsoft.Compute/virtualMachines"},
                {"not": {"field": "Microsoft.Compute/virtualMachines/sku.name", "in": skus}},
            ]
        },
        "then": {"effect": "deny"},
    }
    body["parameters"] = {}
    return body


def _deny_public_ip(intent: GuardrailIntent) -> dict[str, Any]:
    body = _envelope(intent, display_name="Deny public IP addresses", mode="All")
    body["policyRule"] = {
        "if": {"field": "type", "equals": "Microsoft.Network/publicIPAddresses"},
        "then": {"effect": "deny"},
    }
    body["parameters"] = {}
    return body


_BUILDERS = {
    "required_tag": _required_tag,
    "allowed_locations": _allowed_locations,
    "allowed_skus": _allowed_skus,
    "deny_public_ip": _deny_public_ip,
}


def translate(intent: GuardrailIntent) -> dict[str, Any]:
    """Return the native Azure Policy definition, or raise :class:`NotExpressible`."""
    builder = _BUILDERS.get(intent.kind)
    if builder is None:
        raise NotExpressible(f"Azure Policy cannot express guardrail kind '{intent.kind}'")
    return builder(intent)


def scope(intent: GuardrailIntent, *, target: str | None = None) -> dict[str, Any]:
    """The what-if scope: where the deny would be assigned (subscription-level)."""
    return {
        "native": NATIVE_LABEL,
        "level": "subscription",
        "target": target or "<subscription>",
        "assignmentName": f"cloudwarden-{intent.kind}",
    }
