"""M14.6 — shift-left IaC policy evaluation. Tests written FIRST (TDD).

Run the *same* authored c7n policies against a Terraform plan JSON so a violation
fails the PR/CI **before** anything is provisioned. Layers:

* **Pure logic** (no DB): parse a plan → normalized resource dicts (walking child
  modules), map Terraform types → c7n resource types (unmapped → skipped), evaluate
  policies via the injectable c7n matcher seam, and map severity → CI exit code.
* **CLI** (`db` fixture): `cloudwarden evaluate-iac` exits non-zero on a violating
  plan, zero on a compliant one, and honours `--fail-on <severity>`.
* **API** (`db` fixture): `POST /api/policies/evaluate-iac` returns matches, is
  RBAC-guarded, and 422s a malformed plan.

Evaluation runs fully offline: the default matcher is the engine's `match_resources`
(local c7n filter machinery) — the same one a dry-run uses — so no live cloud or
Terraform is needed.
"""

from __future__ import annotations

import json


def _plan():
    from cloudwarden.azure._fixtures import load_fixture

    return load_fixture("tf_plan")


# An authored c7n storage policy: HTTPS-only must be enforced (severity high).
_HTTPS_POLICY = {
    "name": "require-https",
    "resource_type": "azure.storage",
    "description": "Storage accounts must enforce HTTPS-only traffic",
    "spec": {
        "policies": [
            {
                "name": "require-https",
                "resource": "azure.storage",
                "metadata": {"severity": "high"},
                "filters": [{"type": "value", "key": "enable_https_traffic_only", "value": False}],
            }
        ]
    },
}


def _compliant_plan():
    """A plan with only the compliant storage account (HTTPS enforced)."""
    return {
        "planned_values": {
            "root_module": {
                "resources": [
                    {
                        "address": "azurerm_storage_account.secure",
                        "type": "azurerm_storage_account",
                        "name": "secure",
                        "values": {"name": "stsecure", "enable_https_traffic_only": True},
                    }
                ]
            }
        }
    }


# --------------------------------------------------------------------------- #
# Pure logic — parse + map + evaluate (no DB)
# --------------------------------------------------------------------------- #
def test_parse_plan_extracts_resources_and_addresses() -> None:
    from cloudwarden.custodian.shiftleft import parse_plan

    resources = parse_plan(_plan())
    addresses = {r["__address__"] for r in resources}
    assert "azurerm_storage_account.insecure" in addresses
    assert "azurerm_storage_account.secure" in addresses
    # Resource attributes are flattened onto the dict for c7n value filters.
    insecure = next(r for r in resources if r["__address__"] == "azurerm_storage_account.insecure")
    assert insecure["enable_https_traffic_only"] is False
    assert insecure["__tf_type__"] == "azurerm_storage_account"


def test_parse_plan_walks_child_modules() -> None:
    from cloudwarden.custodian.shiftleft import parse_plan

    addresses = {r["__address__"] for r in parse_plan(_plan())}
    # The NSG lives in a child module — it must still be parsed.
    assert "module.network.azurerm_network_security_group.open" in addresses


def test_malformed_plan_raises_clean_error() -> None:
    import pytest

    from cloudwarden.custodian.shiftleft import ShiftLeftError, parse_plan

    with pytest.raises(ShiftLeftError):
        parse_plan("not even a dict")  # type: ignore[arg-type]
    with pytest.raises(ShiftLeftError):
        parse_plan({"not": "a plan"})  # missing planned_values
    with pytest.raises(ShiftLeftError):
        parse_plan({"planned_values": "wrong-shape"})  # planned_values not an object
    with pytest.raises(ShiftLeftError):
        parse_plan({"planned_values": {"root_module": "wrong"}})  # root_module not an object
    with pytest.raises(ShiftLeftError):
        # a child module that is not an object is a malformed plan, not a crash
        parse_plan(
            {"planned_values": {"root_module": {"resources": [], "child_modules": ["oops"]}}}
        )


def test_map_tf_type_known_and_unknown() -> None:
    from cloudwarden.custodian.shiftleft import map_tf_type

    assert map_tf_type("azurerm_storage_account") == "azure.storage"
    assert map_tf_type("azurerm_network_security_group") == "azure.networksecuritygroup"
    assert map_tf_type("azurerm_resource_group") is None  # unmapped → skipped, not an error


def test_unmapped_resource_type_skipped() -> None:
    from cloudwarden.custodian.shiftleft import evaluate_plan

    result = evaluate_plan(_plan(), [_HTTPS_POLICY])
    # The resource group has no c7n mapping — reported as skipped, never evaluated.
    assert "azurerm_resource_group" in result.skipped


def test_violating_plan_returns_match() -> None:
    from cloudwarden.custodian.shiftleft import evaluate_plan

    result = evaluate_plan(_plan(), [_HTTPS_POLICY])
    assert len(result.matches) == 1  # only the insecure storage account
    match = result.matches[0]
    assert match.resource_address == "azurerm_storage_account.insecure"
    assert match.policy == "require-https"
    assert match.severity == "high"


def test_compliant_plan_no_match() -> None:
    from cloudwarden.custodian.shiftleft import evaluate_plan

    result = evaluate_plan(_compliant_plan(), [_HTTPS_POLICY])
    assert result.matches == []
    assert result.exit_code() == 0


def test_evaluate_plan_uses_injected_matcher_seam() -> None:
    from cloudwarden.custodian.shiftleft import evaluate_plan

    calls: list[str] = []

    def fake_match(spec, resources):
        # The one mockable entry point — pretend every selected resource violates.
        calls.append(spec["policies"][0]["name"])
        return list(resources)

    result = evaluate_plan(_plan(), [_HTTPS_POLICY], match_fn=fake_match)
    assert calls == ["require-https"]  # the seam was used
    assert len(result.matches) == 2  # both storage accounts "violate" via the fake


def test_severity_defaults_to_medium_without_metadata() -> None:
    from cloudwarden.custodian.shiftleft import evaluate_plan

    policy = {
        "name": "no-meta",
        "resource_type": "azure.storage",
        "spec": {
            "policies": [
                {
                    "name": "no-meta",
                    "resource": "azure.storage",
                    "filters": [
                        {"type": "value", "key": "enable_https_traffic_only", "value": False}
                    ],
                }
            ]
        },
    }
    result = evaluate_plan(_plan(), [policy])
    assert result.matches[0].severity == "medium"  # no metadata.severity → default


def test_exit_code_and_fail_on_threshold() -> None:
    from cloudwarden.custodian.shiftleft import evaluate_plan

    result = evaluate_plan(_plan(), [_HTTPS_POLICY])  # one high-severity match
    assert result.exit_code() == 1  # any violation → non-zero by default
    assert result.exit_code(fail_on="high") == 1  # high >= high → gated
    assert result.exit_code(fail_on="critical") == 0  # high < critical → below threshold
    assert result.worst_severity() == "high"


def test_worst_severity_none_when_no_matches() -> None:
    from cloudwarden.custodian.shiftleft import evaluate_plan

    result = evaluate_plan(_compliant_plan(), [_HTTPS_POLICY])
    assert result.worst_severity() is None  # nothing violated → no worst severity


def test_policy_targeting_absent_type_evaluates_nothing() -> None:
    from cloudwarden.custodian.shiftleft import evaluate_plan

    # A VM policy against a plan with no VMs selects nothing → no matches, no evaluation.
    vm_policy = {
        "name": "vm-noop",
        "resource_type": "azure.vm",
        "spec": {"policies": [{"name": "vm-noop", "resource": "azure.vm", "filters": []}]},
    }
    result = evaluate_plan(_compliant_plan(), [vm_policy])
    assert result.matches == []
    assert result.evaluated == 0


# --------------------------------------------------------------------------- #
# CLI — exit codes for CI gating
# --------------------------------------------------------------------------- #
def _seed_https_policy(s):
    from cloudwarden.storage import repository as repo

    # create_policy persists enabled by default (schema default), so the policy is
    # immediately picked up by list_policies(enabled_only=True).
    return repo.create_policy(
        s,
        name="require-https",
        resource_type="azure.storage",
        spec=_HTTPS_POLICY["spec"],
        description=_HTTPS_POLICY["description"],
    )


def _write(tmp_path, plan) -> str:
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan))
    return str(path)


def test_cli_exits_nonzero_on_violation(db, tmp_path) -> None:
    from typer.testing import CliRunner

    from cloudwarden.cli import app
    from cloudwarden.storage.db import session_scope

    with session_scope() as s:
        _seed_https_policy(s)
    result = CliRunner().invoke(app, ["evaluate-iac", _write(tmp_path, _plan())])

    assert result.exit_code == 1  # a violation blocks the merge
    assert "require-https" in result.stdout


def test_cli_compliant_plan_exits_zero(db, tmp_path) -> None:
    from typer.testing import CliRunner

    from cloudwarden.cli import app
    from cloudwarden.storage.db import session_scope

    with session_scope() as s:
        _seed_https_policy(s)
    result = CliRunner().invoke(app, ["evaluate-iac", _write(tmp_path, _compliant_plan())])

    assert result.exit_code == 0  # nothing to block


def test_cli_fail_on_severity_threshold(db, tmp_path) -> None:
    from typer.testing import CliRunner

    from cloudwarden.cli import app
    from cloudwarden.storage.db import session_scope

    with session_scope() as s:
        _seed_https_policy(s)  # a high-severity violation
    # --fail-on critical: a high match is below the threshold → do not fail the build.
    result = CliRunner().invoke(
        app, ["evaluate-iac", _write(tmp_path, _plan()), "--fail-on", "critical"]
    )
    assert result.exit_code == 0


def test_cli_missing_plan_file_exits_2() -> None:
    from typer.testing import CliRunner

    from cloudwarden.cli import app

    # A read error is a usage error (exit 2), distinct from a policy violation (exit 1).
    result = CliRunner().invoke(app, ["evaluate-iac", "/nonexistent/plan.json"])
    assert result.exit_code == 2


def test_cli_malformed_plan_exits_2(db, tmp_path) -> None:
    from typer.testing import CliRunner

    from cloudwarden.cli import app
    from cloudwarden.storage.db import session_scope

    with session_scope() as s:
        _seed_https_policy(s)
    result = CliRunner().invoke(app, ["evaluate-iac", _write(tmp_path, {"not": "a plan"})])
    assert result.exit_code == 2  # malformed plan → clean usage error, not a crash


# --------------------------------------------------------------------------- #
# API — POST /api/policies/evaluate-iac + RBAC
# --------------------------------------------------------------------------- #
def test_api_evaluate_iac_returns_matches(db) -> None:
    from fastapi.testclient import TestClient

    from cloudwarden.api.main import app
    from cloudwarden.storage.db import session_scope

    with session_scope() as s:
        _seed_https_policy(s)
    client = TestClient(app)

    resp = client.post("/api/policies/evaluate-iac", json={"plan": _plan()})
    assert resp.status_code == 200
    body = resp.json()
    assert body["violations"] == 1
    assert body["matches"][0]["resource_address"] == "azurerm_storage_account.insecure"


def test_api_evaluate_iac_malformed_returns_422(db) -> None:
    from fastapi.testclient import TestClient

    from cloudwarden.api.main import app

    client = TestClient(app)
    resp = client.post("/api/policies/evaluate-iac", json={"plan": {"not": "a plan"}})
    assert resp.status_code == 422  # a clean error, not a 500 stack trace


def test_api_evaluate_iac_requires_permission(db, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from cloudwarden.api.main import app
    from cloudwarden.authz import rbac
    from cloudwarden.config import get_settings
    from cloudwarden.storage import repository as repo
    from cloudwarden.storage.db import session_scope

    monkeypatch.setenv("RBAC_ENABLED", "1")
    get_settings.cache_clear()
    with session_scope() as s:
        rbac.seed_default_roles(s)
        repo.assign_role(s, principal="ed", role_name="editor")
        _seed_https_policy(s)
    client = TestClient(app)

    assert client.post("/api/policies/evaluate-iac", json={"plan": _plan()}).status_code == 401
    ok = client.post(
        "/api/policies/evaluate-iac", json={"plan": _plan()}, headers={"X-Principal": "ed"}
    )
    assert ok.status_code == 200
    get_settings.cache_clear()
