"""Cost governance pack (M10.2): the FinOps heuristics expressed as c7n policies.

Written test-first (TDD). Uses the **real** offline c7n engine for validation and
filter evaluation (no live Azure — `validate` and `match_resources` are local
operations), and an injected fake runner + the `db` fixture for install. Invariants:

* every policy in the pack is schema-valid via the engine;
* the `pack.yaml` manifest enumerates exactly the policies shipped in the files;
* the idle-VM policy matches a deallocated/stopped VM but not a running one;
* the unattached-disk policy matches an Unattached disk but not an attached one;
* installing the pack materializes a 'Cost Governance' collection of its policies.
"""

from __future__ import annotations

from cloudwarden.azure._fixtures import load_fixture
from cloudwarden.custodian import engine
from cloudwarden.packs import registry
from cloudwarden.storage import repository as repo
from cloudwarden.storage.db import session_scope

COST_PACK = "cost-governance"
COST_COLLECTION = "Cost Governance"
EXPECTED_POLICIES = {
    "cost-idle-vm-deallocated",
    "cost-unattached-disk",
    "cost-idle-public-ip",
    "cost-oversized-vm",
    "cost-untagged-cost-centre",
}


class FakeRunner:
    """Validates any real ``azure.*`` resource type; rejects the rest (offline)."""

    KNOWN = ("azure.vm", "azure.disk", "azure.publicip", "azure.storage")

    def validate(self, spec: dict) -> dict:
        resource = (spec.get("policies") or [{}])[0].get("resource", "")
        ok = resource in self.KNOWN
        return {"valid": ok, "errors": [] if ok else [f"unknown resource: {resource}"]}

    def run(self, spec, subscription_id, credential, dry_run):
        return {"resources": []}

    def schema(self, resource_type=None):
        return {"resource_types": list(self.KNOWN)}


def _policy(pack: dict, name: str) -> dict:
    """Return the c7n spec (wrapped in a ``policies`` list) for one pack policy."""
    match = next(p for p in pack["policies"] if p["name"] == name)
    return {"policies": [match]}


# --------------------------------------------------------------------------- #
# Registry discovery of the directory pack
# --------------------------------------------------------------------------- #
def test_cost_pack_is_discovered() -> None:
    names = {p["name"] for p in registry.list_packs()}

    assert COST_PACK in names
    entry = next(p for p in registry.list_packs() if p["name"] == COST_PACK)
    assert entry["policy_count"] == len(EXPECTED_POLICIES)
    assert entry["version"]


def test_dir_pack_without_manifest_name_is_ignored(tmp_path) -> None:
    root = tmp_path / "packs"
    subdir = root / "nameless"
    subdir.mkdir(parents=True)
    (subdir / "pack.yaml").write_text("version: 0.0.1\ndescription: no name\n", encoding="utf-8")

    assert registry.list_packs(packs_dir=root) == []


def test_cost_pack_manifest_lists_all() -> None:
    pack = registry.get_pack(COST_PACK)

    manifest_names = {m["name"] for m in pack["manifest"]}
    policy_names = {p["name"] for p in pack["policies"]}
    assert manifest_names == policy_names == EXPECTED_POLICIES
    # every enumerated policy carries a human description
    assert all(m.get("description") for m in pack["manifest"])


# --------------------------------------------------------------------------- #
# Validation via the engine
# --------------------------------------------------------------------------- #
def test_cost_pack_policies_validate() -> None:
    pack = registry.get_pack(COST_PACK)

    for policy in pack["policies"]:
        result = engine.validate_policy({"policies": [policy]})
        assert result["valid"], (policy["name"], result["errors"])


# --------------------------------------------------------------------------- #
# Dry-run filter matching against the mock fixtures (real offline c7n)
# --------------------------------------------------------------------------- #
def test_idle_vm_policy_matches_fixture() -> None:
    pack = registry.get_pack(COST_PACK)
    spec = _policy(pack, "cost-idle-vm-deallocated")
    # The recorded custodian fixture holds deallocated + stopped VMs (idle).
    idle_vms = load_fixture("custodian_policy_result")["resources"]
    running_vm = {
        "name": "vm-live",
        "properties": {"instanceView": {"statuses": [{"code": "PowerState/running"}]}},
    }

    matched = engine.match_resources(spec, [*idle_vms, running_vm])

    names = {r["name"] for r in matched}
    assert "vm-idle-03" in names  # the stopped fixture VM
    assert "vm-live" not in names  # a running VM is not idle


def test_orphaned_disk_policy_matches_fixture() -> None:
    pack = registry.get_pack(COST_PACK)
    spec = _policy(pack, "cost-unattached-disk")
    unattached = {"name": "disk-orphan-01", "properties": {"diskState": "Unattached"}}
    attached = {"name": "disk-bound-01", "properties": {"diskState": "Attached"}}

    matched = engine.match_resources(spec, [unattached, attached])

    names = {r["name"] for r in matched}
    assert names == {"disk-orphan-01"}


def test_match_resources_empty_spec_returns_empty() -> None:
    assert engine.match_resources({"policies": []}, [{"name": "x"}]) == []


# --------------------------------------------------------------------------- #
# Install into the 'Cost Governance' collection
# --------------------------------------------------------------------------- #
def test_cost_pack_installs_collection(db) -> None:
    report = registry.install_pack(COST_PACK, runner=FakeRunner())

    assert report["ok"] is True
    assert report["added"] == len(EXPECTED_POLICIES)
    with session_scope() as s:
        collection = repo.get_collection(s, report["collection_id"])
        assert collection["name"] == COST_COLLECTION
        assert {p["name"] for p in collection["policies"]} == EXPECTED_POLICIES
        assert all(repo.get_policy(s, p["id"])["source"] == "pack" for p in collection["policies"])
        installed = repo.get_installed_pack(s, COST_PACK)
        assert installed["version"] == report["version"]


def test_cost_pack_install_is_idempotent(db) -> None:
    runner = FakeRunner()
    registry.install_pack(COST_PACK, runner=runner)

    second = registry.install_pack(COST_PACK, runner=runner)

    assert second["added"] == 0
    assert second["unchanged"] == len(EXPECTED_POLICIES)
    with session_scope() as s:
        collections = [c for c in repo.list_collections(s) if c["name"] == COST_COLLECTION]
        assert len(collections) == 1
