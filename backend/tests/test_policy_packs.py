"""Policy packs (M10.1): bundled, versioned c7n policies that install into a collection.

Written test-first (TDD). Fully offline: an injected ``FakePackRunner`` makes engine
validation deterministic (no live c7n), and the ``db`` fixture makes install/upsert
really persist. Invariants under test:

* the registry discovers the bundled YAML packs (name + version + policy_count);
* installing a pack materializes its (validated) policies and a collection;
* re-installing the same version is idempotent — no duplicate policies/collections/rows;
* a pack with an invalid policy reports the error and installs **nothing** (atomic);
* an unknown pack name is reported, never raised;
* enabling/disabling a pack toggles its member policies' binding eligibility.
"""

from __future__ import annotations

from pathlib import Path

from cloudwarden.packs import registry
from cloudwarden.storage import repository as repo
from cloudwarden.storage.db import session_scope

# Resource types the bundled packs use (all real c7n-azure types). The fake runner
# treats exactly these as schema-valid so the suite never touches live c7n.
_KNOWN = ("azure.vm", "azure.disk", "azure.publicip", "azure.storage")


class FakePackRunner:
    """Known resource types validate; anything else is reported invalid."""

    def validate(self, spec: dict) -> dict:
        resource = (spec.get("policies") or [{}])[0].get("resource", "")
        if resource not in _KNOWN:
            return {"valid": False, "errors": [f"unknown resource type: {resource}"]}
        return {"valid": True, "errors": []}

    def run(self, spec, subscription_id, credential, dry_run):
        return {"resources": []}

    def schema(self, resource_type: str | None = None) -> dict:
        return {"resource_types": list(_KNOWN)}


def _write_pack(tmp_path: Path, filename: str, body: str) -> Path:
    """Write a single pack YAML into a temp packs dir and return that dir."""
    packs_dir = tmp_path / "packs"
    packs_dir.mkdir(exist_ok=True)
    (packs_dir / filename).write_text(body, encoding="utf-8")
    return packs_dir


# A bundled pack name that must always exist in the shipped registry.
BUNDLED_PACK = "cost-hygiene"


# --------------------------------------------------------------------------- #
# Registry discovery (no DB)
# --------------------------------------------------------------------------- #
def test_registry_lists_bundled_packs() -> None:
    packs = registry.list_packs()

    assert packs, "expected at least one bundled pack"
    names = {p["name"] for p in packs}
    assert BUNDLED_PACK in names
    entry = next(p for p in packs if p["name"] == BUNDLED_PACK)
    assert entry["version"]  # non-empty version string
    assert entry["policy_count"] >= 1
    assert entry["description"]


def test_get_pack_returns_full_spec() -> None:
    pack = registry.get_pack(BUNDLED_PACK)

    assert pack is not None
    assert pack["name"] == BUNDLED_PACK
    assert isinstance(pack["policies"], list) and pack["policies"]


def test_get_unknown_pack_returns_none() -> None:
    assert registry.get_pack("no-such-pack") is None


def test_registry_ignores_non_pack_yaml(tmp_path) -> None:
    packs_dir = _write_pack(tmp_path, "notes.yaml", "some_other_key: true\n")

    assert registry.list_packs(packs_dir=packs_dir) == []
    assert registry.get_pack("notes", packs_dir=packs_dir) is None


def test_registry_missing_dir_is_empty(tmp_path) -> None:
    assert registry.list_packs(packs_dir=tmp_path / "nope") == []


# --------------------------------------------------------------------------- #
# Install
# --------------------------------------------------------------------------- #
def test_install_creates_collection_and_policies(db) -> None:
    pack = registry.get_pack(BUNDLED_PACK)

    report = registry.install_pack(BUNDLED_PACK, runner=FakePackRunner())

    assert report["ok"] is True
    assert report["version"] == pack["version"]
    assert report["added"] == len(pack["policies"])
    with session_scope() as s:
        collection = repo.get_collection(s, report["collection_id"])
        assert collection is not None
        assert collection["name"] == BUNDLED_PACK
        assert collection["policy_count"] == len(pack["policies"])
        # every materialized policy carries the pack provenance
        member_ids = [p["id"] for p in collection["policies"]]
        assert all(repo.get_policy(s, pid)["source"] == "pack" for pid in member_ids)
        installed = repo.get_installed_pack(s, BUNDLED_PACK)
        assert installed["version"] == pack["version"]
        assert installed["enabled"] is True


def test_reinstall_is_idempotent(db) -> None:
    runner = FakePackRunner()
    first = registry.install_pack(BUNDLED_PACK, runner=runner)

    second = registry.install_pack(BUNDLED_PACK, runner=runner)

    assert second["added"] == 0
    assert second["unchanged"] == first["added"]
    assert second["collection_id"] == first["collection_id"]
    with session_scope() as s:
        # no duplicate policies, no duplicate collection, single installed row
        assert len(repo.list_installed_packs(s)) == 1
        collections = [c for c in repo.list_collections(s) if c["name"] == BUNDLED_PACK]
        assert len(collections) == 1
        assert all(
            p["version"] == 1 for p in repo.policies_in_collection(s, first["collection_id"])
        )


def test_install_unknown_pack_reports_error(db) -> None:
    report = registry.install_pack("no-such-pack", runner=FakePackRunner())

    assert report["ok"] is False
    assert "unknown pack" in report["error"].lower()
    with session_scope() as s:
        assert repo.list_installed_packs(s) == []


def test_install_invalid_pack_reports_error_and_installs_nothing(db, tmp_path) -> None:
    packs_dir = _write_pack(
        tmp_path,
        "broken.yaml",
        "name: broken\nversion: 0.0.1\ndescription: has a bad policy\n"
        "policies:\n"
        "  - name: broken-good\n    resource: azure.vm\n"
        "  - name: broken-bad\n    resource: azure.not-a-type\n",
    )

    report = registry.install_pack("broken", runner=FakePackRunner(), packs_dir=packs_dir)

    assert report["ok"] is False
    assert report["errors"]  # per-policy validation errors surfaced
    with session_scope() as s:
        # atomic: not even the valid policy or the collection was created
        assert repo.list_policies(s) == []
        assert [c for c in repo.list_collections(s) if c["name"] == "broken"] == []
        assert repo.get_installed_pack(s, "broken") is None


# --------------------------------------------------------------------------- #
# Enable / disable — toggles binding eligibility
# --------------------------------------------------------------------------- #
def test_enable_disable_pack_toggles_binding_eligibility(db) -> None:
    report = registry.install_pack(BUNDLED_PACK, runner=FakePackRunner())
    collection_id = report["collection_id"]

    with session_scope() as s:
        disabled = repo.set_pack_enabled(s, BUNDLED_PACK, False)
        assert disabled["enabled"] is False
        # disabled pack drops out of binding runs (enabled-only resolution)
        assert repo.policies_in_collection(s, collection_id, enabled_only=True) == []

    with session_scope() as s:
        enabled = repo.set_pack_enabled(s, BUNDLED_PACK, True)
        assert enabled["enabled"] is True
        eligible = repo.policies_in_collection(s, collection_id, enabled_only=True)
        assert len(eligible) == report["added"]


def test_set_pack_enabled_unknown_returns_none(db) -> None:
    with session_scope() as s:
        assert repo.set_pack_enabled(s, "no-such-pack", False) is None


# --------------------------------------------------------------------------- #
# API endpoints
# --------------------------------------------------------------------------- #
def _client():
    from fastapi.testclient import TestClient

    from cloudwarden.api.main import app, get_custodian_runner

    app.dependency_overrides[get_custodian_runner] = lambda: FakePackRunner()
    return TestClient(app), app


def test_api_list_packs(db) -> None:
    client, app = _client()
    try:
        resp = client.get("/api/packs")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    body = resp.json()
    assert any(p["name"] == BUNDLED_PACK and p["version"] for p in body)


def test_api_install_pack(db) -> None:
    client, app = _client()
    try:
        resp = client.post(f"/api/packs/{BUNDLED_PACK}/install")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    with session_scope() as s:
        assert repo.get_installed_pack(s, BUNDLED_PACK) is not None


def test_api_install_unknown_pack_404(db) -> None:
    client, app = _client()
    try:
        resp = client.post("/api/packs/no-such-pack/install")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 404


def test_api_install_invalid_pack_422(db) -> None:
    from fastapi.testclient import TestClient

    from cloudwarden.api.main import app, get_custodian_runner

    class RejectAllRunner:
        def validate(self, spec):
            return {"valid": False, "errors": ["nope"]}

        def run(self, spec, subscription_id, credential, dry_run):
            return {"resources": []}

        def schema(self, resource_type=None):
            return {"resource_types": []}

    app.dependency_overrides[get_custodian_runner] = lambda: RejectAllRunner()
    try:
        resp = TestClient(app).post(f"/api/packs/{BUNDLED_PACK}/install")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 422
    with session_scope() as s:
        assert repo.get_installed_pack(s, BUNDLED_PACK) is None


def test_api_list_installed_packs(db) -> None:
    client, app = _client()
    try:
        client.post(f"/api/packs/{BUNDLED_PACK}/install")
        resp = client.get("/api/packs/installed")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert any(p["name"] == BUNDLED_PACK for p in resp.json())


def test_api_enable_disable_pack(db) -> None:
    client, app = _client()
    try:
        client.post(f"/api/packs/{BUNDLED_PACK}/install")
        resp = client.post(f"/api/packs/{BUNDLED_PACK}/enabled", json={"enabled": False})
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert resp.json()["enabled"] is False


def test_api_enable_unknown_pack_404(db) -> None:
    client, app = _client()
    try:
        resp = client.post("/api/packs/no-such-pack/enabled", json={"enabled": False})
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 404


def test_bundled_packs_are_schema_valid_shape() -> None:
    """Every shipped pack declares name/version/description and a non-empty policy list."""
    for entry in registry.list_packs():
        pack = registry.get_pack(entry["name"])
        assert pack["name"] and pack["version"] and pack["description"]
        assert pack["policies"]
        for policy in pack["policies"]:
            assert policy.get("name") and policy.get("resource")
