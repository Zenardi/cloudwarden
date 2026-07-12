"""TDD tests for the Cloud Custodian execution engine wrapper (M1.1).

Every test runs fully offline. The public functions (`validate_policy`,
`run_policy`, `get_schema`) are exercised through an injected
``FakeCustodianRunner`` — no c7n, no Azure, no network — and the
``LiveCustodianRunner`` is exercised only on its local/offline surface (schema
introspection, policy validation, and mock-mode ``run``). Live Azure execution
is deliberately out of scope for unit tests: any attempt to construct a
``c7n_azure.session.Session`` is stubbed to explode so the "never call live
Azure" contract is asserted, not assumed.
"""

from __future__ import annotations

import sys
import types

import pytest

from azure_finops.azure._fixtures import PLACEHOLDER_SUBSCRIPTION, load_fixture
from azure_finops.azure.context import SubscriptionContext
from azure_finops.custodian import engine


# --------------------------------------------------------------------------- #
# Test double + helpers
# --------------------------------------------------------------------------- #
class FakeCustodianRunner:
    """In-memory ``CustodianRunner`` double.

    Implements the protocol with just enough logic to distinguish known from
    unknown resource types, and records every call so tests can assert the
    engine passed arguments through unchanged. It never imports c7n or Azure.
    """

    KNOWN_TYPES = ("azure.vm", "azure.disk", "azure.publicip")

    def __init__(self) -> None:
        self.validate_calls: list[dict] = []
        self.run_calls: list[dict] = []
        self.schema_calls: list[str | None] = []

    @staticmethod
    def _resource(spec: dict) -> str:
        return (spec.get("policies") or [{}])[0].get("resource", "")

    def validate(self, spec: dict) -> dict:
        self.validate_calls.append(spec)
        resource = self._resource(spec)
        if resource not in self.KNOWN_TYPES:
            return {"valid": False, "errors": [f"unknown resource type: {resource}"]}
        return {"valid": True, "errors": []}

    def run(self, spec: dict, subscription_id: str, credential, dry_run: bool) -> dict:
        self.run_calls.append(
            {
                "spec": spec,
                "subscription_id": subscription_id,
                "credential": credential,
                "dry_run": dry_run,
            }
        )
        policy = (spec.get("policies") or [{}])[0]
        return {
            "policy_name": policy.get("name", "unnamed"),
            "resource_type": policy.get("resource", ""),
            "dry_run": dry_run,
            "matched": 2,
            "resources": [{"name": "vm-web-01"}, {"name": "vm-batch-02"}],
        }

    def schema(self, resource_type: str | None = None) -> dict:
        self.schema_calls.append(resource_type)
        if resource_type is None:
            return {"resource_types": list(self.KNOWN_TYPES)}
        if resource_type not in self.KNOWN_TYPES:
            return {
                "error": f"unknown resource type: {resource_type}",
                "resource_type": resource_type,
            }
        return {"resource_type": resource_type, "filters": [], "actions": []}


@pytest.fixture
def fake_runner() -> FakeCustodianRunner:
    return FakeCustodianRunner()


@pytest.fixture
def live_runner() -> engine.LiveCustodianRunner:
    return engine.LiveCustodianRunner()


def _vm_policy(name: str = "stopped-vms", resource: str = "azure.vm") -> dict:
    return {"policies": [{"name": name, "resource": resource}]}


def _explode_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install a ``c7n_azure.session`` stub whose ``Session`` raises if built."""
    stub = types.ModuleType("c7n_azure.session")

    class _ExplodingSession:
        def __init__(self, *args, **kwargs):
            raise AssertionError("live c7n_azure Session must not be constructed")

    stub.Session = _ExplodingSession
    monkeypatch.setitem(sys.modules, "c7n_azure.session", stub)


# --------------------------------------------------------------------------- #
# validate_policy — public function, injected runner
# --------------------------------------------------------------------------- #
def test_validate_policy_valid_spec_returns_no_errors(fake_runner: FakeCustodianRunner) -> None:
    result = engine.validate_policy(_vm_policy(), runner=fake_runner)

    assert result == {"valid": True, "errors": []}
    assert fake_runner.validate_calls == [_vm_policy()]


def test_validate_policy_unknown_resource_type_returns_errors(
    fake_runner: FakeCustodianRunner,
) -> None:
    result = engine.validate_policy(_vm_policy(resource="azure.not-a-type"), runner=fake_runner)

    assert result["valid"] is False
    assert result["errors"]  # non-empty; no exception raised


# --------------------------------------------------------------------------- #
# run_policy — public function, injected runner
# --------------------------------------------------------------------------- #
def test_run_policy_dry_run_uses_injected_runner_not_live_azure(
    fake_runner: FakeCustodianRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    _explode_session(monkeypatch)

    result = engine.run_policy(_vm_policy(), dry_run=True, runner=fake_runner)

    assert len(fake_runner.run_calls) == 1
    assert fake_runner.run_calls[0]["dry_run"] is True
    assert result["dry_run"] is True


def test_run_policy_passes_subscription_context(fake_runner: FakeCustodianRunner) -> None:
    sub = SubscriptionContext(
        subscription_id="11111111-1111-1111-1111-111111111111", credential="cred-obj"
    )

    engine.run_policy(_vm_policy(), subscription=sub, dry_run=False, runner=fake_runner)

    call = fake_runner.run_calls[0]
    assert call["subscription_id"] == sub.subscription_id
    assert call["credential"] == "cred-obj"
    assert call["dry_run"] is False


def test_run_policy_defaults_subscription_to_settings(fake_runner: FakeCustodianRunner) -> None:
    from azure_finops.config import get_settings

    engine.run_policy(_vm_policy(), runner=fake_runner)

    call = fake_runner.run_calls[0]
    assert call["subscription_id"] == get_settings().azure_subscription_id
    assert call["credential"] is None


def test_run_policy_defaults_runner_when_not_injected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine, "_default_runner", None)
    built: list[object] = []

    class _SpyRunner:
        def __init__(self) -> None:
            built.append(self)

        def run(self, spec, subscription_id, credential, dry_run):
            return {
                "policy_name": "x",
                "resource_type": "azure.vm",
                "dry_run": dry_run,
                "resources": [],
            }

    monkeypatch.setattr(engine, "LiveCustodianRunner", _SpyRunner)

    engine.run_policy(_vm_policy())
    engine.run_policy(_vm_policy())

    assert len(built) == 1  # lazily constructed exactly once, then cached


# --------------------------------------------------------------------------- #
# get_schema — public function, injected runner
# --------------------------------------------------------------------------- #
def test_get_schema_lists_azure_resource_types(fake_runner: FakeCustodianRunner) -> None:
    schema = engine.get_schema(runner=fake_runner)

    assert "azure.vm" in schema["resource_types"]
    assert fake_runner.schema_calls == [None]


def test_get_schema_unknown_resource_type_returns_error_not_raise(
    fake_runner: FakeCustodianRunner,
) -> None:
    schema = engine.get_schema("azure.bogus", runner=fake_runner)

    assert "error" in schema  # structured error dict, not an exception


# --------------------------------------------------------------------------- #
# LiveCustodianRunner — offline surface only (real c7n, no Azure)
# --------------------------------------------------------------------------- #
def test_c7n_azure_entry_registers_azure_vm(live_runner: engine.LiveCustodianRunner) -> None:
    # Constructing the live runner imports c7n_azure.entry, which registers the
    # Azure resource types. Proven observable: azure.vm is enumerable.
    resource_types = live_runner.schema()["resource_types"]

    assert "azure.vm" in resource_types
    assert any(t.startswith("azure.") for t in resource_types)


def test_live_runner_validates_wellformed_policy(live_runner: engine.LiveCustodianRunner) -> None:
    result = live_runner.validate(_vm_policy())

    assert result["valid"] is True
    assert result["errors"] == []


def test_live_runner_flags_unknown_resource_type(live_runner: engine.LiveCustodianRunner) -> None:
    result = live_runner.validate(_vm_policy(resource="azure.not-a-type"))

    assert result["valid"] is False
    assert result["errors"]  # non-empty; no exception raised


def test_live_runner_validate_surfaces_c7n_error_not_raise(
    live_runner: engine.LiveCustodianRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    # If c7n's validator itself blows up, the wrapper returns a structured error
    # (valid=False) and records unhealthy status instead of propagating.
    import c7n.schema as c7n_schema

    from azure_finops.resilience import REGISTRY

    def _boom(*args, **kwargs):
        raise RuntimeError("schema explosion")

    monkeypatch.setattr(c7n_schema, "validate", _boom)

    result = live_runner.validate(_vm_policy())

    assert result == {"valid": False, "errors": ["schema explosion"]}
    status = {s["name"]: s for s in REGISTRY.snapshot()}["custodian"]
    assert status["ok"] is False


def test_live_runner_schema_for_known_resource(live_runner: engine.LiveCustodianRunner) -> None:
    schema = live_runner.schema("azure.vm")

    assert "error" not in schema
    assert schema["resource_type"] == "azure.vm"


def test_live_runner_schema_unknown_resource_returns_error(
    live_runner: engine.LiveCustodianRunner,
) -> None:
    schema = live_runner.schema("azure.bogus")

    assert "error" in schema  # structured error, not an exception


def test_live_runner_run_mock_mode_returns_fixture_without_session(
    live_runner: engine.LiveCustodianRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    # conftest forces FINOPS_MOCK=1; mock-mode run must load the fixture and
    # never construct a live Azure session.
    _explode_session(monkeypatch)

    result = live_runner.run(
        _vm_policy(), subscription_id=PLACEHOLDER_SUBSCRIPTION, credential=None, dry_run=True
    )

    assert result["policy_name"] == "stopped-vms"
    assert result["dry_run"] is True
    assert result["resources"]  # non-empty, sourced from the fixture


def test_live_runner_reports_health_to_registry(live_runner: engine.LiveCustodianRunner) -> None:
    from azure_finops.resilience import REGISTRY

    live_runner.validate(_vm_policy())

    names = {s["name"] for s in REGISTRY.snapshot()}
    assert "custodian" in names


# --------------------------------------------------------------------------- #
# Recorded fixture
# --------------------------------------------------------------------------- #
def test_custodian_fixture_loads_and_shapes_matched_resources() -> None:
    data = load_fixture("custodian_policy_result")

    assert data["policy_name"]
    assert data["resource_type"] == "azure.vm"
    assert isinstance(data["resources"], list) and data["resources"]
    # Resource ids use the placeholder-subscription convention (retargetable).
    assert any(PLACEHOLDER_SUBSCRIPTION in (r.get("id") or "") for r in data["resources"])
