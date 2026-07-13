"""Security & tagging-hygiene pack (M10.3): common security findings as c7n policies.

Written test-first (TDD). Uses the **real** offline c7n engine for validation and
filter evaluation (no live Azure), and an injected fake runner + the `db` fixture for
install. Invariants:

* every policy in the pack is schema-valid via the engine (filters AND dry-run actions);
* the required-tags policy matches a resource missing a mandated tag, not a tagged one;
* the public-IP-exposure and permissive-NSG policies validate and document their filters;
* installing the pack materializes a 'Security Baseline' collection of its policies.
"""

from __future__ import annotations

from azure_finops.custodian import engine
from azure_finops.packs import registry
from azure_finops.storage import repository as repo
from azure_finops.storage.db import session_scope

SECURITY_PACK = "security-baseline"
SECURITY_COLLECTION = "Security Baseline"
EXPECTED_POLICIES = {
    "security-public-ip-exposure",
    "security-nsg-permissive-inbound",
    "security-required-tags",
    "security-unencrypted-disk",
}


class FakeRunner:
    """Validates any real ``azure.*`` resource type; rejects the rest (offline)."""

    KNOWN = ("azure.vm", "azure.disk", "azure.publicip", "azure.networksecuritygroup")

    def validate(self, spec: dict) -> dict:
        resource = (spec.get("policies") or [{}])[0].get("resource", "")
        ok = resource in self.KNOWN
        return {"valid": ok, "errors": [] if ok else [f"unknown resource: {resource}"]}

    def run(self, spec, subscription_id, credential, dry_run):
        return {"resources": []}

    def schema(self, resource_type=None):
        return {"resource_types": list(self.KNOWN)}


def _policy(pack: dict, name: str) -> dict:
    return next(p for p in pack["policies"] if p["name"] == name)


# --------------------------------------------------------------------------- #
# Discovery + manifest
# --------------------------------------------------------------------------- #
def test_security_pack_is_discovered() -> None:
    entry = next((p for p in registry.list_packs() if p["name"] == SECURITY_PACK), None)

    assert entry is not None
    assert entry["policy_count"] == len(EXPECTED_POLICIES)
    assert entry["version"]


def test_security_pack_manifest_lists_all() -> None:
    pack = registry.get_pack(SECURITY_PACK)

    manifest_names = {m["name"] for m in pack["manifest"]}
    policy_names = {p["name"] for p in pack["policies"]}
    assert manifest_names == policy_names == EXPECTED_POLICIES
    assert all(m.get("description") for m in pack["manifest"])


# --------------------------------------------------------------------------- #
# Validation via the engine (filters + dry-run actions)
# --------------------------------------------------------------------------- #
def test_security_pack_policies_validate() -> None:
    pack = registry.get_pack(SECURITY_PACK)

    for policy in pack["policies"]:
        result = engine.validate_policy({"policies": [policy]})
        assert result["valid"], (policy["name"], result["errors"])


def test_public_ip_exposure_policy_validates() -> None:
    pack = registry.get_pack(SECURITY_PACK)
    policy = _policy(pack, "security-public-ip-exposure")

    assert policy["resource"] == "azure.publicip"
    assert policy["filters"]  # documents an exposure filter
    assert engine.validate_policy({"policies": [policy]})["valid"]


def test_nsg_permissive_policy_validates() -> None:
    pack = registry.get_pack(SECURITY_PACK)
    policy = _policy(pack, "security-nsg-permissive-inbound")

    assert policy["resource"] == "azure.networksecuritygroup"
    # documents its filter: a permissive inbound-Allow-from-any ingress rule
    ingress = next(f for f in policy["filters"] if f.get("type") == "ingress")
    assert ingress["access"] == "Allow"
    assert ingress["source"] == "*"
    assert policy.get("description")
    assert engine.validate_policy({"policies": [policy]})["valid"]


def test_unencrypted_disk_policy_validates() -> None:
    pack = registry.get_pack(SECURITY_PACK)
    policy = _policy(pack, "security-unencrypted-disk")

    assert policy["resource"] == "azure.disk"
    assert engine.validate_policy({"policies": [policy]})["valid"]


def test_security_policies_declare_dry_run_actions() -> None:
    """Each policy carries a remediation action (dry-run enforced by bindings)."""
    pack = registry.get_pack(SECURITY_PACK)

    for policy in pack["policies"]:
        actions = policy.get("actions") or []
        assert actions, policy["name"]
        assert any(
            (a.get("type") if isinstance(a, dict) else a) in ("tag", "notify") for a in actions
        ), policy["name"]


# --------------------------------------------------------------------------- #
# Required-tags dry-run matching against an untagged fixture (real offline c7n)
# --------------------------------------------------------------------------- #
def test_required_tags_matches_untagged() -> None:
    pack = registry.get_pack(SECURITY_PACK)
    spec = {"policies": [_policy(pack, "security-required-tags")]}
    # An id is required by c7n's set-based (boolean) filter processing.
    untagged = {"id": "/vm/untagged", "name": "vm-untagged", "tags": {"env": "dev"}}
    tagged = {
        "id": "/vm/tagged",
        "name": "vm-tagged",
        "tags": {"Environment": "prod", "Owner": "web-team"},
    }

    matched = engine.match_resources(spec, [untagged, tagged])

    assert {r["name"] for r in matched} == {"vm-untagged"}


# --------------------------------------------------------------------------- #
# Install into the 'Security Baseline' collection
# --------------------------------------------------------------------------- #
def test_security_pack_installs_collection(db) -> None:
    report = registry.install_pack(SECURITY_PACK, runner=FakeRunner())

    assert report["ok"] is True
    assert report["added"] == len(EXPECTED_POLICIES)
    with session_scope() as s:
        collection = repo.get_collection(s, report["collection_id"])
        assert collection["name"] == SECURITY_COLLECTION
        assert {p["name"] for p in collection["policies"]} == EXPECTED_POLICIES
        assert all(repo.get_policy(s, p["id"])["source"] == "pack" for p in collection["policies"])


def test_security_pack_install_is_idempotent(db) -> None:
    runner = FakeRunner()
    registry.install_pack(SECURITY_PACK, runner=runner)

    second = registry.install_pack(SECURITY_PACK, runner=runner)

    assert second["added"] == 0
    assert second["unchanged"] == len(EXPECTED_POLICIES)
    with session_scope() as s:
        collections = [c for c in repo.list_collections(s) if c["name"] == SECURITY_COLLECTION]
        assert len(collections) == 1
