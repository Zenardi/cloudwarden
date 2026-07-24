"""M14.10 — preventive guardrails (Azure Policy / AWS SCP / GCP Org Policy). Tests FIRST (TDD).

CloudWarden's controls are detective + remediation — they observe and fix, but nothing
*prevents* a non-compliant resource from being created. This translates a subset of
authored intent into native **deny** constructs per provider, with a **what-if / preview**
step and a guarded **apply** behind the same remediation guardrails + write SP.

Layers under test:

* **Translation** (pure, no DB): an authored policy that opts in via
  ``spec.policies[0].metadata.guardrail`` → the provider's native deny definition,
  compared against a fixture. A policy with no guardrail intent → *not expressible*
  (never a silent no-op); a supported kind the provider can't express → *not expressible*.
* **Preview** (pure): returns the native definition + the affected scope (what-if) and
  never mutates.
* **Apply** (``db`` fixture): dry-run-first and gated by ``REMEDIATION_ENABLED`` +
  the resource-group allow-list; a real apply calls the *injected* write client exactly
  once and is audited; a provider error is surfaced, not swallowed.

Cloud clients are injected/mocked — no live cloud is ever touched.
"""

from __future__ import annotations

import json
from importlib import resources

import pytest

# --------------------------------------------------------------------------- #
# Authored policies (opt in to a preventive guardrail via metadata.guardrail)
# --------------------------------------------------------------------------- #


def _policy(name, resource_type, kind, params):
    """An authored c7n policy that opts into a preventive guardrail of ``kind``."""
    return {
        "name": name,
        "resource_type": resource_type,
        "description": f"{name} preventive guardrail",
        "spec": {
            "policies": [
                {
                    "name": name,
                    "resource": resource_type,
                    "metadata": {"guardrail": {"kind": kind, "params": params}},
                    "filters": [],
                }
            ]
        },
    }


_AZURE_TAG_POLICY = _policy(
    "require-environment-tag", "azure.vm", "required_tag", {"tag": "Environment"}
)
_AWS_REGION_POLICY = _policy(
    "allowed-regions", "aws.ec2", "allowed_locations", {"locations": ["us-east-1", "us-west-2"]}
)
_GCP_LOCATION_POLICY = _policy(
    "allowed-locations",
    "gcp.instance",
    "allowed_locations",
    {"locations": ["us-central1", "us-east1"]},
)

# A plain detective policy — no metadata.guardrail, so it is NOT expressible as a
# preventive deny construct (must be reported, never silently dropped).
_DETECTIVE_POLICY = {
    "name": "idle-vm",
    "resource_type": "azure.vm",
    "description": "Flag idle VMs (detective only)",
    "spec": {
        "policies": [
            {
                "name": "idle-vm",
                "resource": "azure.vm",
                "filters": [{"type": "value", "key": "properties.idle", "value": True}],
            }
        ]
    },
}


def _fixture(name):
    ref = resources.files("cloudwarden.fixtures.preventive").joinpath(f"{name}.json")
    with ref.open("r", encoding="utf-8") as fh:
        return json.load(fh)


class _FakeClient:
    """An injected write client — records applies, or raises to simulate a provider error."""

    def __init__(self, *, raises: bool = False) -> None:
        self.calls: list[tuple] = []
        self._raises = raises

    def apply_guardrail(self, provider, definition, scope):
        if self._raises:
            raise RuntimeError("provider rejected the guardrail")
        self.calls.append((provider, definition, scope))
        return {"status": "created", "id": f"{provider}-guardrail-1"}


# --------------------------------------------------------------------------- #
# intent_from_policy — the opt-in contract
# --------------------------------------------------------------------------- #
def test_intent_parsed_from_guardrail_metadata() -> None:
    from cloudwarden.providers import preventive

    intent = preventive.intent_from_policy(_AZURE_TAG_POLICY)
    assert intent is not None
    assert intent.kind == "required_tag"
    assert intent.params == {"tag": "Environment"}
    assert intent.resource == "azure.vm"
    assert intent.policy_name == "require-environment-tag"


def test_detective_policy_has_no_intent() -> None:
    from cloudwarden.providers import preventive

    # A policy that does not opt into a guardrail yields no intent (→ not expressible).
    assert preventive.intent_from_policy(_DETECTIVE_POLICY) is None


# --------------------------------------------------------------------------- #
# Translation → native definition (fixture-compared)
# --------------------------------------------------------------------------- #
def test_azure_policy_translation_matches_fixture() -> None:
    from cloudwarden.providers import preventive

    definition = preventive.translate("azure", _AZURE_TAG_POLICY)
    assert definition == _fixture("azure_policy")


def test_aws_scp_translation_matches_fixture() -> None:
    from cloudwarden.providers import preventive

    definition = preventive.translate("aws", _AWS_REGION_POLICY)
    assert definition == _fixture("aws_scp")


def test_gcp_orgpolicy_translation_matches_fixture() -> None:
    from cloudwarden.providers import preventive

    definition = preventive.translate("gcp", _GCP_LOCATION_POLICY)
    assert definition == _fixture("gcp_orgpolicy")


def test_unsupported_policy_reports_not_expressible() -> None:
    from cloudwarden.providers import preventive

    # A detective policy carries no guardrail intent → explicit NotExpressible, never a no-op.
    with pytest.raises(preventive.NotExpressible):
        preventive.translate("azure", _DETECTIVE_POLICY)


def test_supported_kind_provider_cannot_express_is_not_expressible() -> None:
    from cloudwarden.providers import preventive

    # GCP Org Policy has no native 'require a label on create' constraint → not expressible,
    # even though the *policy* is a valid guardrail intent (azure/aws can express it).
    gcp_tag_policy = _policy("require-tag", "gcp.instance", "required_tag", {"tag": "env"})
    with pytest.raises(preventive.NotExpressible):
        preventive.translate("gcp", gcp_tag_policy)
    # ...and the same intent IS expressible on Azure and AWS.
    assert preventive.translate("azure", gcp_tag_policy)["kind"] == "required_tag"
    assert preventive.translate("aws", gcp_tag_policy)["Statement"]


def test_translate_unknown_provider_raises() -> None:
    from cloudwarden.providers import preventive, registry

    with pytest.raises(registry.UnknownProviderError):
        preventive.translate("oracle", _AZURE_TAG_POLICY)


def test_every_supported_kind_translates_or_reports() -> None:
    """Each provider translates every kind it declares supported, and reports the rest."""
    from cloudwarden.providers import preventive, registry

    params_for = {
        "required_tag": {"tag": "Environment"},
        "allowed_locations": {"locations": ["r1", "r2"]},
        "allowed_skus": {"skus": ["Standard_B2s"]},
        "deny_public_ip": {},
    }
    for provider in ("azure", "aws", "gcp"):
        module = registry.preventive_translator(provider)
        for kind in preventive.GUARDRAIL_KINDS:
            policy = _policy(f"{provider}-{kind}", "azure.vm", kind, params_for[kind])
            if kind in module.SUPPORTED_KINDS:
                definition = preventive.translate(provider, policy)
                assert definition["kind"] == kind
            else:
                with pytest.raises(preventive.NotExpressible):
                    preventive.translate(provider, policy)


# --------------------------------------------------------------------------- #
# Preview — what-if (scope + definition), no mutation
# --------------------------------------------------------------------------- #
def test_preview_returns_scope_without_mutation() -> None:
    from cloudwarden.providers import preventive

    preview = preventive.build_preview(_AZURE_TAG_POLICY, "azure", scope="sub-123")
    assert preview["expressible"] is True
    assert preview["kind"] == "required_tag"
    assert preview["definition"] == _fixture("azure_policy")
    # The what-if scope names WHERE the deny would be enforced, without applying it.
    assert preview["scope"]["target"] == "sub-123"
    assert preview["mutating"] is False
    # A preview carries no execution outcome — it never mutated anything.
    assert "applied" not in preview


def test_preview_not_expressible_is_explicit() -> None:
    from cloudwarden.providers import preventive

    preview = preventive.build_preview(_DETECTIVE_POLICY, "azure")
    assert preview["expressible"] is False
    assert preview["definition"] is None
    assert preview["reason"]  # a human-readable explanation, never a silent no-op


def test_preview_scope_per_provider() -> None:
    from cloudwarden.providers import preventive

    aws = preventive.build_preview(_AWS_REGION_POLICY, "aws", scope="ou-root")
    assert aws["scope"]["native"] == "Service Control Policy"
    assert aws["scope"]["target"] == "ou-root"

    gcp = preventive.build_preview(_GCP_LOCATION_POLICY, "gcp", scope="organizations/42")
    assert gcp["scope"]["native"] == "Organization Policy"
    assert gcp["scope"]["target"] == "organizations/42"


def test_preview_provider_cannot_express_is_explicit() -> None:
    from cloudwarden.providers import preventive

    # An expressible intent (required_tag) that GCP cannot express natively → explicit.
    gcp_tag_policy = _policy("require-tag", "gcp.instance", "required_tag", {"tag": "env"})
    preview = preventive.build_preview(gcp_tag_policy, "gcp")
    assert preview["expressible"] is False
    assert preview["kind"] == "required_tag"  # the intent was understood...
    assert preview["definition"] is None  # ...but has no native GCP construct
    assert preview["reason"]


def test_translate_unknown_kind_is_not_expressible() -> None:
    from cloudwarden.providers import preventive

    # A guardrail kind no translator knows → NotExpressible (exercises the provider's
    # defensive rejection even for a cloud that expresses every *known* kind).
    bogus = _policy("bogus", "azure.vm", "encrypt_everything", {})
    with pytest.raises(preventive.NotExpressible):
        preventive.translate("azure", bogus)


# --------------------------------------------------------------------------- #
# Apply — dry-run-first, guardrail-gated, audited (db fixture)
# --------------------------------------------------------------------------- #
def _settings(**overrides):
    from cloudwarden.config import Settings

    return Settings(**overrides)


def test_apply_blocked_without_remediation_enabled(db) -> None:
    from cloudwarden.providers import preventive
    from cloudwarden.storage.db import session_scope

    client = _FakeClient()
    settings = _settings(remediation_enabled=False)
    with session_scope() as s:
        result = preventive.apply(
            s,
            policy=_AZURE_TAG_POLICY,
            provider="azure",
            settings=settings,
            client=client,
            scope="sub-123",
            dry_run=False,  # even when a real apply is requested...
            actor="ed",
        )
    # ...remediation being disabled forces a dry-run: the write client is NEVER called.
    assert result["applied"] is False
    assert result["dry_run"] is True
    assert client.calls == []


def test_apply_respects_allow_list_and_audits(db) -> None:
    from cloudwarden.providers import preventive
    from cloudwarden.storage import repository as repo
    from cloudwarden.storage.db import session_scope

    client = _FakeClient()
    # Guardrails fully satisfied: remediation enabled AND a resource group allow-listed.
    settings = _settings(remediation_enabled=True, allowed_resource_groups="*")
    with session_scope() as s:
        result = preventive.apply(
            s,
            policy=_AZURE_TAG_POLICY,
            provider="azure",
            settings=settings,
            client=client,
            scope="sub-123",
            dry_run=False,
            actor="ed",
        )
    assert result["applied"] is True
    assert result["dry_run"] is False
    assert len(client.calls) == 1  # the injected write client was called exactly once
    # And the apply was audited (append-only) with the guardrail action.
    with session_scope() as s:
        entries = repo.list_audit_logs(s, action="guardrail:apply")
    assert entries and entries[0]["actor"] == "ed"


def test_apply_empty_allow_list_stays_dry_run(db) -> None:
    from cloudwarden.providers import preventive
    from cloudwarden.storage.db import session_scope

    client = _FakeClient()
    # Remediation enabled but NO resource group allow-listed → still forced to dry-run.
    settings = _settings(remediation_enabled=True, allowed_resource_groups="")
    with session_scope() as s:
        result = preventive.apply(
            s,
            policy=_AZURE_TAG_POLICY,
            provider="azure",
            settings=settings,
            client=client,
            scope="sub-123",
            dry_run=False,
            actor="ed",
        )
    assert result["applied"] is False
    assert result["dry_run"] is True
    assert client.calls == []


def test_apply_not_expressible_never_calls_client(db) -> None:
    from cloudwarden.providers import preventive
    from cloudwarden.storage.db import session_scope

    client = _FakeClient()
    settings = _settings(remediation_enabled=True, allowed_resource_groups="*")
    with session_scope() as s:
        result = preventive.apply(
            s,
            policy=_DETECTIVE_POLICY,
            provider="azure",
            settings=settings,
            client=client,
            dry_run=False,
            actor="ed",
        )
    assert result["expressible"] is False
    assert result["applied"] is False
    assert client.calls == []  # nothing to apply → the client is never touched


def test_apply_surfaces_provider_error(db) -> None:
    from cloudwarden.providers import preventive
    from cloudwarden.storage import repository as repo
    from cloudwarden.storage.db import session_scope

    client = _FakeClient(raises=True)  # the provider rejects the write
    settings = _settings(remediation_enabled=True, allowed_resource_groups="*")
    with session_scope() as s:
        result = preventive.apply(
            s,
            policy=_AZURE_TAG_POLICY,
            provider="azure",
            settings=settings,
            client=client,
            scope="sub-123",
            dry_run=False,
            actor="ed",
        )
    # The error is surfaced on the result (not swallowed, not a 500), and still audited.
    assert result["applied"] is False
    assert result["error"]
    assert "provider rejected" in result["error"]
    with session_scope() as s:
        assert repo.list_audit_logs(s, action="guardrail:apply")


def test_apply_dry_run_default_is_dry_run(db) -> None:
    from cloudwarden.providers import preventive
    from cloudwarden.storage.db import session_scope

    client = _FakeClient()
    settings = _settings(remediation_enabled=True, allowed_resource_groups="*")
    with session_scope() as s:
        # dry_run omitted → dry-run-first: no real apply even with guardrails satisfied.
        result = preventive.apply(
            s,
            policy=_AZURE_TAG_POLICY,
            provider="azure",
            settings=settings,
            client=client,
            scope="sub-123",
            actor="ed",
        )
    assert result["dry_run"] is True
    assert result["applied"] is False
    assert client.calls == []


# --------------------------------------------------------------------------- #
# Provider registry exposes the per-provider translator
# --------------------------------------------------------------------------- #
def test_registry_exposes_preventive_translator() -> None:
    from cloudwarden.providers import registry
    from cloudwarden.providers.preventive import aws_scp, azure_policy, gcp_orgpolicy

    assert registry.preventive_translator("azure") is azure_policy
    assert registry.preventive_translator("aws") is aws_scp
    assert registry.preventive_translator("gcp") is gcp_orgpolicy


def test_provider_exposes_preventive_capability() -> None:
    from cloudwarden.providers import registry

    provider = registry.get("azure")
    # The optional preventive capability is present on the provider abstraction.
    assert provider.preventive_translator().PROVIDER == "azure"


# --------------------------------------------------------------------------- #
# API — POST /api/guardrails/preview + /apply (RBAC-guarded, audited)
# --------------------------------------------------------------------------- #
def _seed_policy(s, policy):
    from cloudwarden.storage import repository as repo

    return repo.create_policy(
        s,
        name=policy["name"],
        resource_type=policy["resource_type"],
        spec=policy["spec"],
        description=policy["description"],
    )


def test_api_preview_returns_definition_and_audits(db) -> None:
    from fastapi.testclient import TestClient

    from cloudwarden.api.main import app
    from cloudwarden.storage import repository as repo
    from cloudwarden.storage.db import session_scope

    with session_scope() as s:
        policy = _seed_policy(s, _AZURE_TAG_POLICY)
    client = TestClient(app)

    resp = client.post(
        "/api/guardrails/preview", json={"policy_id": policy["id"], "provider": "azure"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["expressible"] is True
    assert body["definition"] == _fixture("azure_policy")
    with session_scope() as s:
        assert repo.list_audit_logs(s, action="guardrail:preview")


def test_api_preview_unknown_policy_404(db) -> None:
    from fastapi.testclient import TestClient

    from cloudwarden.api.main import app

    client = TestClient(app)
    resp = client.post("/api/guardrails/preview", json={"policy_id": 999999, "provider": "azure"})
    assert resp.status_code == 404


def test_api_preview_unknown_provider_400(db) -> None:
    from fastapi.testclient import TestClient

    from cloudwarden.api.main import app
    from cloudwarden.storage.db import session_scope

    with session_scope() as s:
        policy = _seed_policy(s, _AZURE_TAG_POLICY)
    client = TestClient(app)
    resp = client.post(
        "/api/guardrails/preview", json={"policy_id": policy["id"], "provider": "oracle"}
    )
    assert resp.status_code == 400


def test_api_apply_dry_run_default_no_mutation(db) -> None:
    from fastapi.testclient import TestClient

    from cloudwarden.api.main import app
    from cloudwarden.storage import repository as repo
    from cloudwarden.storage.db import session_scope

    with session_scope() as s:
        policy = _seed_policy(s, _AZURE_TAG_POLICY)
    client = TestClient(app)

    # Default settings: remediation disabled → apply is forced to dry-run (no live client built).
    resp = client.post(
        "/api/guardrails/apply", json={"policy_id": policy["id"], "provider": "azure"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["dry_run"] is True
    assert body["applied"] is False
    with session_scope() as s:
        assert repo.list_audit_logs(s, action="guardrail:apply")


def test_api_guardrails_require_permission(db, monkeypatch) -> None:
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
        policy = _seed_policy(s, _AZURE_TAG_POLICY)
    client = TestClient(app)

    body = {"policy_id": policy["id"], "provider": "azure"}
    assert client.post("/api/guardrails/preview", json=body).status_code == 401
    ok = client.post("/api/guardrails/preview", json=body, headers={"X-Principal": "ed"})
    assert ok.status_code == 200
    assert client.post("/api/guardrails/apply", json=body).status_code == 401
    ok2 = client.post("/api/guardrails/apply", json=body, headers={"X-Principal": "ed"})
    assert ok2.status_code == 200
    get_settings.cache_clear()
