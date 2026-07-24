"""Preventive guardrails — translate authored intent into native deny constructs (M14.10).

This completes the governance loop **detect → remediate → prevent**. A subset of
authored policies is translated into a provider's native **deny** construct — Azure
Policy, AWS SCP, or GCP Org Policy — with a **what-if / preview** step and a guarded,
**dry-run-first apply** behind the same remediation guardrails (``REMEDIATION_ENABLED``
+ the resource-group allow-list) and the write-scoped service principal.

Layers:

* :func:`translate` — an authored policy → the provider's native definition (pure), or
  :class:`NotExpressible` when the policy declares no guardrail intent or the provider
  cannot express its kind (never a silent no-op).
* :func:`build_preview` — the what-if: the native definition + the affected scope,
  without mutating anything.
* :func:`apply` — dry-run-first; a real apply is permitted only when the guardrails
  pass, calls the injected write client exactly once, surfaces any provider error, and
  is always audited.

Provider translators are pluggable and resolved through the provider registry
(:func:`providers.registry.preventive_translator`); cloud write clients are injected so
tests need no live cloud.
"""

from __future__ import annotations

from typing import Any

from ..registry import preventive_translator
from .base import (
    GUARDRAIL_KINDS,
    GuardrailIntent,
    NotExpressible,
    PreventiveError,
    intent_from_policy,
)

__all__ = [
    "GUARDRAIL_KINDS",
    "GuardrailIntent",
    "NotExpressible",
    "PreventiveError",
    "apply",
    "build_preview",
    "intent_from_policy",
    "translate",
]


def translate(provider: str, policy: dict[str, Any]) -> dict[str, Any]:
    """Translate an authored ``policy`` into ``provider``'s native deny definition.

    Raises :class:`NotExpressible` when the policy declares no guardrail intent or the
    provider cannot express its kind, and ``registry.UnknownProviderError`` for an
    unregistered provider name.
    """
    intent = intent_from_policy(policy)
    if intent is None:
        raise NotExpressible(
            f"policy '{policy.get('name', '?')}' declares no preventive guardrail "
            "(no spec.policies[0].metadata.guardrail)"
        )
    module = preventive_translator(provider)
    return module.translate(intent)


def build_preview(
    policy: dict[str, Any], provider: str, *, scope: str | None = None
) -> dict[str, Any]:
    """The what-if for a guardrail: native definition + affected scope, no mutation.

    Always returns a dict with an explicit ``expressible`` flag — a policy that cannot be
    expressed carries ``expressible=False`` + a human-readable ``reason`` (never a silent
    no-op). ``registry.UnknownProviderError`` still propagates for a bad provider name.
    """
    intent = intent_from_policy(policy)
    if intent is None:
        return {
            "policy": policy.get("name"),
            "provider": provider,
            "expressible": False,
            "kind": None,
            "definition": None,
            "scope": None,
            "reason": "policy declares no preventive guardrail (metadata.guardrail)",
            "mutating": False,
        }
    module = preventive_translator(provider)
    try:
        definition = module.translate(intent)
    except NotExpressible as exc:
        return {
            "policy": intent.policy_name,
            "provider": provider,
            "expressible": False,
            "kind": intent.kind,
            "definition": None,
            "scope": None,
            "reason": str(exc),
            "mutating": False,
        }
    return {
        "policy": intent.policy_name,
        "provider": provider,
        "expressible": True,
        "kind": intent.kind,
        "definition": definition,
        "scope": module.scope(intent, target=scope),
        "reason": None,
        "mutating": False,
    }


def _build_live_client(provider: str, settings: Any) -> Any:  # pragma: no cover - live cloud
    """Build a live write client for a real apply. Not exercised in mock-mode tests."""
    raise PreventiveError(
        f"no write client available for provider '{provider}' — inject one or configure "
        "the write-scoped service principal"
    )


def _audit_apply(session: Any, actor: str | None, result: dict[str, Any]) -> None:
    from ...authz import audit  # local import: keep the provider package import-light

    audit.record(
        session,
        actor=actor,
        action="guardrail:apply",
        target_type="guardrail",
        target_id=f"{result['provider']}:{result['policy']}",
        after={
            "provider": result["provider"],
            "kind": result["kind"],
            "expressible": result["expressible"],
            "applied": result["applied"],
            "dry_run": result["dry_run"],
            "error": result["error"],
        },
    )


def apply(
    session: Any,
    *,
    policy: dict[str, Any],
    provider: str,
    settings: Any,
    client: Any | None = None,
    scope: str | None = None,
    dry_run: bool | None = None,
    actor: str | None = None,
) -> dict[str, Any]:
    """Apply a guardrail — dry-run-first, guardrail-gated, and always audited.

    A real apply happens only when the policy is expressible **and** the remediation
    guardrails permit a live write (``REMEDIATION_ENABLED`` + a non-empty resource-group
    allow-list); otherwise it is forced to a dry-run and the injected ``client`` is never
    called. A provider error is surfaced on ``result['error']`` (not swallowed, not a
    500). Every call is recorded to the append-only audit log.
    """
    from ...remediation import guardrails

    preview = build_preview(policy, provider, scope=scope)
    result: dict[str, Any] = {
        **preview,
        "applied": False,
        "dry_run": True,
        "blocked": False,
        "result": None,
        "error": None,
    }
    if not preview["expressible"]:
        result["result"] = "not expressible — nothing applied"
        _audit_apply(session, actor, result)
        return result

    requested_dry_run = True if dry_run is None else bool(dry_run)
    forced_dry_run = guardrails.default_dry_run(settings)
    if requested_dry_run or forced_dry_run:
        result["dry_run"] = True
        result["blocked"] = forced_dry_run and not requested_dry_run
        result["result"] = f"[dry-run] would apply {preview['kind']} guardrail via {provider}"
    else:
        write_client = client if client is not None else _build_live_client(provider, settings)
        try:
            outcome = write_client.apply_guardrail(
                provider, preview["definition"], preview["scope"]
            )
        except Exception as exc:  # noqa: BLE001 - surface the provider error, never a 500
            result["error"] = str(exc)
            result["result"] = f"provider error: {exc}"
        else:
            result["applied"] = True
            result["dry_run"] = False
            result["result"] = outcome
    _audit_apply(session, actor, result)
    return result
