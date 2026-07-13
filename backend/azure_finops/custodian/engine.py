"""Cloud Custodian execution-engine wrapper.

Exposes three public functions — :func:`validate_policy`, :func:`run_policy`,
:func:`get_schema` — behind an injectable ``runner`` so unit tests never make a
live Azure or c7n network call. The default runner is a lazily-constructed,
cached :class:`LiveCustodianRunner` that drives c7n's Python API:

* ``validate`` → :func:`c7n.schema.validate` (local, offline)
* ``schema``   → :func:`c7n.schema.generate` + the Azure resource registry (local)
* ``run``      → c7n policy execution against a ``c7n_azure.session.Session``
  (live Azure); in ``FINOPS_MOCK=1`` mode it loads a recorded fixture instead,
  so dry-runs work fully offline.

Follows the existing injectable-client pattern (``azure/inventory.py``) and
reports health via ``resilience.REGISTRY`` under the ``"custodian"`` source.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol, runtime_checkable

from ..azure._fixtures import load_fixture
from ..azure.context import SubscriptionContext
from ..config import get_settings
from ..resilience import REGISTRY

logger = logging.getLogger("azure_finops.custodian.engine")

_FIXTURE_NAME = "custodian_policy_result"


@runtime_checkable
class CustodianRunner(Protocol):
    """The one mockable seam every milestone talks to instead of c7n directly."""

    def validate(self, spec: dict) -> dict:
        """Return ``{"valid": bool, "errors": list[str]}`` for a policy spec."""

    def run(self, spec: dict, subscription_id: str, credential: Any, dry_run: bool) -> dict:
        """Evaluate a policy and return the matched-resource result dict."""

    def schema(self, resource_type: str | None = None) -> dict:
        """Return the resource-type listing (no arg) or one type's schema."""


def _azure_provider() -> Any:
    """The registered Azure provider (owns the c7n registration/registry seam)."""
    from ..providers import registry

    return registry.get("azure")


def _ensure_azure_registered() -> None:
    """Register ``azure.*`` c7n resource types via the Azure provider (idempotent)."""
    _azure_provider().register_resources()


def _azure_registry() -> Any:
    """Return the c7n Azure resource registry (keys are un-prefixed, e.g. ``vm``)."""
    return _azure_provider().resource_registry()


class LiveCustodianRunner:
    """Drives the real c7n / c7n-azure Python API.

    ``validate`` and ``schema`` are local (offline) c7n operations; ``run`` only
    touches live Azure when ``FINOPS_MOCK`` is off — otherwise it returns a
    recorded fixture so dry-runs are fully offline.
    """

    def __init__(self) -> None:
        _ensure_azure_registered()

    def validate(self, spec: dict) -> dict:
        import c7n.schema as c7n_schema

        try:
            errors = c7n_schema.validate(spec) or []
        except Exception as exc:  # noqa: BLE001 - surfaced as a structured error
            REGISTRY.set("custodian", ok=False, error=str(exc))
            return {"valid": False, "errors": [str(exc)]}
        REGISTRY.set("custodian", ok=True)
        return {"valid": not errors, "errors": [str(e) for e in errors]}

    def schema(self, resource_type: str | None = None) -> dict:
        registry = _azure_registry()
        all_types = sorted(f"azure.{name}" for name in registry.keys())
        if resource_type is None:
            REGISTRY.set("custodian", ok=True)
            return {"resource_types": all_types}
        if resource_type not in all_types:
            REGISTRY.set("custodian", ok=True)
            return {
                "error": f"unknown resource type: {resource_type}",
                "resource_type": resource_type,
            }
        import c7n.schema as c7n_schema

        resource_cls = registry.get(resource_type.split(".", 1)[1])
        definition = (
            c7n_schema.generate((resource_type,))
            .get("definitions", {})
            .get("resources", {})
            .get(resource_type, {})
        )
        REGISTRY.set("custodian", ok=True)
        return {
            "resource_type": resource_type,
            "filters": sorted(resource_cls.filter_registry.keys()),
            "actions": sorted(resource_cls.action_registry.keys()),
            "schema": definition,
        }

    def run(self, spec: dict, subscription_id: str, credential: Any, dry_run: bool) -> dict:
        if get_settings().finops_mock:
            return _mock_run_result(spec, dry_run)
        return self._run_live(spec, subscription_id, credential, dry_run)  # pragma: no cover

    def _run_live(  # pragma: no cover - requires live Azure; unit tests use mock mode
        self, spec: dict, subscription_id: str, credential: Any, dry_run: bool
    ) -> dict:
        from c7n.config import Config
        from c7n.loader import PolicyLoader
        from c7n_azure.session import Session

        policy_def = (spec.get("policies") or [{}])[0]
        try:
            session = Session(subscription_id=subscription_id)
            config = Config.empty(dryrun=dry_run, subscription_id=subscription_id)
            collection = PolicyLoader(config).load_data(spec, "memory://custodian")
            matched: list[Any] = []
            for policy in collection:
                policy.session_factory = lambda s=session: s
                matched.extend(policy.run() or [])
        except Exception as exc:  # noqa: BLE001 - report health then re-raise
            REGISTRY.set("custodian", ok=False, error=str(exc))
            raise
        REGISTRY.set("custodian", ok=True)
        return {
            "policy_name": policy_def.get("name"),
            "resource_type": policy_def.get("resource"),
            "dry_run": dry_run,
            "matched": len(matched),
            "resources": matched,
        }


def _mock_run_result(spec: dict, dry_run: bool) -> dict:
    """Shape a matched-resource result from the recorded fixture (offline)."""
    fixture = load_fixture(_FIXTURE_NAME)
    policy_def = (spec.get("policies") or [{}])[0]
    resources = fixture.get("resources", [])
    REGISTRY.set("custodian", ok=True)
    return {
        "policy_name": policy_def.get("name") or fixture.get("policy_name"),
        "resource_type": policy_def.get("resource") or fixture.get("resource_type"),
        "dry_run": dry_run,
        "matched": len(resources),
        "resources": resources,
    }


# --- Injectable default runner -------------------------------------------- #
_default_runner: CustodianRunner | None = None


def _get_default_runner() -> CustodianRunner:
    """Lazily build and cache a single :class:`LiveCustodianRunner`."""
    global _default_runner
    if _default_runner is None:
        _default_runner = LiveCustodianRunner()
    return _default_runner


def validate_policy(spec: dict, runner: CustodianRunner | None = None) -> dict:
    """Validate a c7n policy spec, returning ``{"valid": bool, "errors": [...]}``."""
    return (runner if runner is not None else _get_default_runner()).validate(spec)


def run_policy(
    spec: dict,
    subscription: SubscriptionContext | None = None,
    dry_run: bool = True,
    runner: CustodianRunner | None = None,
) -> dict:
    """Evaluate a policy against a subscription (mock-backed unless live mode)."""
    runner = runner if runner is not None else _get_default_runner()
    settings = get_settings()
    subscription_id = (
        subscription.subscription_id if subscription else settings.azure_subscription_id
    )
    credential = subscription.credential if subscription else None
    return runner.run(spec, subscription_id=subscription_id, credential=credential, dry_run=dry_run)


def get_schema(resource_type: str | None = None, runner: CustodianRunner | None = None) -> dict:
    """List Azure resource types (no arg) or return one type's schema."""
    return (runner if runner is not None else _get_default_runner()).schema(resource_type)


def match_resources(spec: dict, resources: list[dict]) -> list[dict]:
    """Apply a policy spec's filters to a list of resource dicts, offline (no Azure).

    Runs c7n's filter machinery locally — the same evaluation a dry-run performs
    against fetched data — so a policy can be checked against recorded/inventory
    resources without a live run. Returns the subset that matches the first policy's
    filters (``[]`` when the spec declares no policies).
    """
    _ensure_azure_registered()
    from c7n.config import Config
    from c7n.loader import PolicyLoader

    policies = list(PolicyLoader(Config.empty()).load_data(spec, "memory://pack"))
    if not policies:
        return []
    return policies[0].resource_manager.filter_resources(resources)


def resolve_actions(spec: dict) -> list[dict]:
    """Surface a policy's remediation actions, each normalized to a ``{"type": ...}`` dict.

    Reads the first policy's ``actions`` list (c7n's shape) and normalizes each
    entry (string shorthand or mapping) so the remediation executor can dispatch
    them uniformly. Returns ``[]`` when the spec declares no policies/actions.
    """
    from ..remediation.executor import normalize_action

    policies = spec.get("policies") or []
    if not policies:
        return []
    return [normalize_action(a) for a in (policies[0].get("actions") or [])]
