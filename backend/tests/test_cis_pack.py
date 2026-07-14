"""CIS Azure compliance pack (M10.4): CIS controls mapped to c7n policies.

Written test-first (TDD). Uses the **real** offline c7n engine for validation, an
injected fake runner + the `db` fixture for install, and seeded executions to prove
posture can be grouped by CIS control id. Invariants:

* every CIS policy is schema-valid via the engine and carries a `metadata.control_id`;
* the `pack.yaml` manifest maps each policy to a CIS control number (consistent with
  the policy metadata);
* compliance posture can be grouped by control id (compliant/non-compliant rollups);
* installing the pack materializes a 'CIS Azure' collection of its policies.
"""

from __future__ import annotations

from cloudwarden.custodian import engine
from cloudwarden.packs import registry
from cloudwarden.storage import repository as repo
from cloudwarden.storage.db import session_scope

CIS_PACK = "cis-azure"
CIS_COLLECTION = "CIS Azure"
EXPECTED_CONTROLS = {
    "cis-3-1-storage-secure-transfer": "3.1",
    "cis-3-8-storage-default-deny": "3.8",
    "cis-6-1-nsg-restrict-rdp": "6.1",
    "cis-6-2-nsg-restrict-ssh": "6.2",
    "cis-7-3-disk-cmk-encryption": "7.3",
}


class FakeRunner:
    """Validates any real ``azure.*`` resource type; rejects the rest (offline)."""

    KNOWN = ("azure.vm", "azure.disk", "azure.storage", "azure.networksecuritygroup")

    def validate(self, spec: dict) -> dict:
        resource = (spec.get("policies") or [{}])[0].get("resource", "")
        ok = resource in self.KNOWN
        return {"valid": ok, "errors": [] if ok else [f"unknown resource: {resource}"]}

    def run(self, spec, subscription_id, credential, dry_run):
        return {"resources": []}

    def schema(self, resource_type=None):
        return {"resource_types": list(self.KNOWN)}


# --------------------------------------------------------------------------- #
# Discovery + validation + control-id metadata
# --------------------------------------------------------------------------- #
def test_cis_pack_is_discovered() -> None:
    entry = next((p for p in registry.list_packs() if p["name"] == CIS_PACK), None)

    assert entry is not None
    assert entry["policy_count"] == len(EXPECTED_CONTROLS)


def test_cis_pack_policies_validate() -> None:
    pack = registry.get_pack(CIS_PACK)

    for policy in pack["policies"]:
        result = engine.validate_policy({"policies": [policy]})
        assert result["valid"], (policy["name"], result["errors"])


def test_cis_policies_have_control_id() -> None:
    pack = registry.get_pack(CIS_PACK)

    control_ids = {p["name"]: (p.get("metadata") or {}).get("control_id") for p in pack["policies"]}
    assert control_ids == EXPECTED_CONTROLS
    assert all(control_ids.values())  # none missing


def test_pack_manifest_maps_controls() -> None:
    pack = registry.get_pack(CIS_PACK)

    # the manifest maps each policy name to a CIS control number ...
    manifest_map = {m["name"]: m["control_id"] for m in pack["manifest"]}
    assert manifest_map == EXPECTED_CONTROLS
    # ... and stays consistent with the control id embedded in each policy's metadata
    policy_map = {p["name"]: p["metadata"]["control_id"] for p in pack["policies"]}
    assert manifest_map == policy_map


# --------------------------------------------------------------------------- #
# Install into the 'CIS Azure' collection
# --------------------------------------------------------------------------- #
def test_cis_pack_installs_collection(db) -> None:
    report = registry.install_pack(CIS_PACK, runner=FakeRunner())

    assert report["ok"] is True
    assert report["added"] == len(EXPECTED_CONTROLS)
    with session_scope() as s:
        collection = repo.get_collection(s, report["collection_id"])
        assert collection["name"] == CIS_COLLECTION
        assert {p["name"] for p in collection["policies"]} == set(EXPECTED_CONTROLS)


# --------------------------------------------------------------------------- #
# Posture grouped by CIS control id
# --------------------------------------------------------------------------- #
def _seed_execution(session, policy_id, exec_id, matched) -> None:
    repo.create_policy_execution(
        session, execution_id=exec_id, policy_id=policy_id, subscription_id="sub-cis"
    )
    repo.finish_policy_execution(session, exec_id, status="succeeded", resources_matched=matched)


def test_posture_groups_by_control_id(db) -> None:
    registry.install_pack(CIS_PACK, runner=FakeRunner())
    with session_scope() as s:
        clean = repo.get_policy_by_name(s, "cis-3-1-storage-secure-transfer")["id"]
        bad = repo.get_policy_by_name(s, "cis-6-1-nsg-restrict-rdp")["id"]
        _seed_execution(s, clean, "cis-e-clean", matched=0)  # compliant
        _seed_execution(s, bad, "cis-e-bad", matched=2)  # 2 violations

    with session_scope() as s:
        posture = repo.governance_posture(s)

    by_control = {row["control_id"]: row for row in posture["by_control"]}
    assert by_control["3.1"]["compliant"] == 1
    assert by_control["3.1"]["non_compliant"] == 0
    assert by_control["6.1"]["non_compliant"] == 1
    assert by_control["6.1"]["violations"] == 2


def test_posture_by_control_excludes_uncontrolled_policies(db) -> None:
    """A plain policy without a control id never appears in the by_control rollup."""
    with session_scope() as s:
        pid = repo.create_policy(
            s, name="plain", resource_type="azure.vm", spec={"policies": [{"name": "plain"}]}
        )["id"]
        _seed_execution(s, pid, "plain-e", matched=1)

    with session_scope() as s:
        posture = repo.governance_posture(s)

    assert posture["by_control"] == []


def test_api_posture_exposes_by_control(db) -> None:
    from fastapi.testclient import TestClient

    from cloudwarden.api.main import app

    registry.install_pack(CIS_PACK, runner=FakeRunner())
    with session_scope() as s:
        pid = repo.get_policy_by_name(s, "cis-6-2-nsg-restrict-ssh")["id"]
        _seed_execution(s, pid, "api-cis-e", matched=3)

    resp = TestClient(app).get("/api/governance/posture")

    assert resp.status_code == 200
    by_control = {row["control_id"]: row for row in resp.json()["by_control"]}
    assert by_control["6.2"]["violations"] == 3
