"""Preventive-guardrail core types + the authored-policy opt-in contract (M14.10).

A *preventive guardrail* translates a subset of authored governance intent into a
provider's native **deny** construct (Azure Policy / AWS SCP / GCP Org Policy) so a
non-compliant resource is blocked *at creation*, not merely detected afterwards.

An authored c7n policy opts in by declaring, on its first policy body, a
``metadata.guardrail`` block::

    metadata:
      guardrail:
        kind: required_tag          # one of GUARDRAIL_KINDS
        params: {tag: Environment}

:func:`intent_from_policy` lifts that into a provider-agnostic :class:`GuardrailIntent`.
A policy without the block yields ``None`` — it is a detective-only policy and is
reported as *not expressible* by the translators (never silently dropped). Each
provider translator then maps a supported ``kind`` to its native definition, or raises
:class:`NotExpressible` for a kind it cannot express natively.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# The capability subset expressible as native preventive controls. Not every provider
# expresses every kind (see each translator's ``SUPPORTED_KINDS``).
GUARDRAIL_KINDS: tuple[str, ...] = (
    "required_tag",
    "allowed_locations",
    "allowed_skus",
    "deny_public_ip",
)


class NotExpressible(Exception):
    """A policy/intent cannot be expressed as this provider's native deny construct.

    Raised (never returned as a silent no-op) both when a policy declares no guardrail
    intent and when the intent's ``kind`` is one the target provider cannot express.
    """


class PreventiveError(Exception):
    """A provider write client failed while applying a guardrail — surfaced, not swallowed."""


@dataclass(frozen=True)
class GuardrailIntent:
    """A provider-agnostic preventive intent lifted from an authored policy."""

    kind: str
    resource: str
    policy_name: str
    params: dict[str, Any] = field(default_factory=dict)
    description: str = ""


def intent_from_policy(policy: dict[str, Any]) -> GuardrailIntent | None:
    """Lift a :class:`GuardrailIntent` from a policy's ``metadata.guardrail``, or ``None``.

    ``None`` means the policy does not opt into a preventive guardrail (a detective-only
    policy) — the translators report that explicitly rather than emitting an empty rule.
    """
    spec = policy.get("spec") or {}
    policy_defs = spec.get("policies") or [{}]
    metadata = policy_defs[0].get("metadata") or {}
    guardrail = metadata.get("guardrail")
    if not isinstance(guardrail, dict) or not guardrail.get("kind"):
        return None
    return GuardrailIntent(
        kind=str(guardrail["kind"]),
        resource=policy.get("resource_type") or policy_defs[0].get("resource") or "",
        policy_name=policy.get("name") or policy_defs[0].get("name") or "?",
        params=dict(guardrail.get("params") or {}),
        description=policy.get("description") or "",
    )
