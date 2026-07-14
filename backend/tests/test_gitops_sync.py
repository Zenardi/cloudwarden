"""GitOps policy sync (M2.4): sync Cloud Custodian policy YAML from a Git repo.

Written test-first (TDD). Fully offline: an **injectable** ``FakeGitClient`` points
the sync at a temp fixture repo directory (no real clone/network) and an injected
``FakeCustodianRunner`` makes validation deterministic. DB-backed via the ``db``
fixture so upserts really persist. Invariants under test:

* new policy files are imported (``source='gitops'``);
* a changed file updates the row and bumps its version;
* unparseable / schema-invalid files are **skipped and reported** (non-fatal);
* a no-change re-sync writes nothing (idempotent — versions stay put);
* a git clone/pull failure returns a structured error, never raises.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cloudwarden.custodian.gitops import sync_policies
from cloudwarden.storage import repository as repo
from cloudwarden.storage.db import session_scope

GOOD_VM = "policies:\n  - name: stopped-vms\n    resource: azure.vm\n"
GOOD_DISK = "policies:\n  - name: unattached-disks\n    resource: azure.disk\n"
INVALID_POLICY = "policies:\n  - name: bogus\n    resource: azure.not-a-type\n"
BAD_YAML = "policies: [unclosed list\n  : : :\n"
NO_POLICIES = "some_other_key: true\n"


class FakeGitClient:
    """Stands in for a real clone/pull: returns a prepared local directory."""

    def __init__(self, path, fail: bool = False) -> None:
        self.path = str(path)
        self.fail = fail
        self.calls = 0

    def clone_or_pull(self, repo_url: str, branch: str) -> str:
        self.calls += 1
        if self.fail:
            raise RuntimeError("clone failed: host unreachable")
        return self.path


class FakeCustodianRunner:
    """Known resource types validate; anything else is reported invalid."""

    KNOWN = ("azure.vm", "azure.disk", "azure.publicip")

    def validate(self, spec: dict) -> dict:
        resource = (spec.get("policies") or [{}])[0].get("resource", "")
        if resource not in self.KNOWN:
            return {"valid": False, "errors": [f"unknown resource type: {resource}"]}
        return {"valid": True, "errors": []}

    def run(self, spec, subscription_id, credential, dry_run):
        return {"resources": []}

    def schema(self, resource_type: str | None = None) -> dict:
        return {"resource_types": list(self.KNOWN)}


def _write_repo(tmp_path: Path, files: dict[str, str]) -> Path:
    root = tmp_path / "repo"
    policies = root / "policies"
    policies.mkdir(parents=True)
    for name, content in files.items():
        (policies / name).write_text(content, encoding="utf-8")
    return root


@pytest.fixture
def gitops_env(monkeypatch):
    monkeypatch.setenv("GITOPS_REPO_URL", "https://example.test/policies.git")
    monkeypatch.setenv("GITOPS_BRANCH", "main")
    monkeypatch.setenv("GITOPS_POLICY_PATH", "policies")
    from cloudwarden.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _sync(repo_root, **kw):
    return sync_policies(git_client=FakeGitClient(repo_root), runner=FakeCustodianRunner(), **kw)


# --------------------------------------------------------------------------- #
# Happy paths
# --------------------------------------------------------------------------- #
def test_sync_imports_new_policies(db, gitops_env, tmp_path) -> None:
    root = _write_repo(tmp_path, {"a.yml": GOOD_VM, "b.yaml": GOOD_DISK})

    result = _sync(root)

    assert result["ok"] is True
    assert result["added"] == 2
    assert result["updated"] == 0
    assert result["skipped"] == 0
    with session_scope() as s:
        assert {p["name"] for p in repo.list_policies(s)} == {"stopped-vms", "unattached-disks"}


def test_sync_marks_source_gitops(db, gitops_env, tmp_path) -> None:
    root = _write_repo(tmp_path, {"a.yml": GOOD_VM})

    _sync(root)

    with session_scope() as s:
        assert repo.list_policies(s)[0]["source"] == "gitops"


def test_sync_updates_changed_policy(db, gitops_env, tmp_path) -> None:
    root = _write_repo(tmp_path, {"a.yml": GOOD_VM})
    _sync(root)
    # change the file's spec
    (root / "policies" / "a.yml").write_text(
        "policies:\n  - name: stopped-vms\n    resource: azure.vm\n    filters:\n"
        "      - type: instance-view\n",
        encoding="utf-8",
    )

    result = _sync(root)

    assert result["added"] == 0
    assert result["updated"] == 1
    with session_scope() as s:
        policy = next(p for p in repo.list_policies(s) if p["name"] == "stopped-vms")
    assert policy["version"] == 2


def test_sync_is_idempotent_on_no_change(db, gitops_env, tmp_path) -> None:
    root = _write_repo(tmp_path, {"a.yml": GOOD_VM, "b.yaml": GOOD_DISK})

    first = _sync(root)
    second = _sync(root)

    assert first["added"] == 2
    assert second["added"] == 0
    assert second["updated"] == 0
    assert second["unchanged"] == 2
    with session_scope() as s:
        assert all(p["version"] == 1 for p in repo.list_policies(s))  # no bumps


# --------------------------------------------------------------------------- #
# Skip + report (non-fatal)
# --------------------------------------------------------------------------- #
def test_sync_skips_invalid_yaml_and_reports(db, gitops_env, tmp_path) -> None:
    root = _write_repo(tmp_path, {"good.yml": GOOD_VM, "bad.yml": BAD_YAML})

    result = _sync(root)

    assert result["ok"] is True  # non-fatal
    assert result["added"] == 1
    assert result["skipped"] == 1
    assert any("bad.yml" in e.get("file", "") for e in result["errors"])


def test_sync_skips_schema_invalid_policy(db, gitops_env, tmp_path) -> None:
    root = _write_repo(tmp_path, {"bad.yml": INVALID_POLICY})

    result = _sync(root)

    assert result["added"] == 0
    assert result["skipped"] == 1
    assert result["errors"]
    with session_scope() as s:
        assert repo.list_policies(s) == []  # nothing persisted


def test_sync_skips_file_without_policies_list(db, gitops_env, tmp_path) -> None:
    root = _write_repo(tmp_path, {"notes.yaml": NO_POLICIES})

    result = _sync(root)

    assert result["added"] == 0
    assert result["skipped"] == 1


def test_sync_skips_policy_missing_name(db, gitops_env, tmp_path) -> None:
    root = _write_repo(tmp_path, {"a.yml": "policies:\n  - resource: azure.vm\n"})

    result = _sync(root)

    assert result["added"] == 0
    assert result["skipped"] == 1
    assert any("name" in (e.get("error") or "") for e in result["errors"])


def test_sync_missing_policy_dir_is_noop(db, gitops_env, tmp_path) -> None:
    root = tmp_path / "repo"
    root.mkdir()  # no `policies/` subdirectory

    result = sync_policies(git_client=FakeGitClient(root), runner=FakeCustodianRunner())

    assert result["ok"] is True
    assert result["added"] == 0
    assert result["skipped"] == 0


# --------------------------------------------------------------------------- #
# Error handling
# --------------------------------------------------------------------------- #
def test_sync_clone_failure_returns_error(db, gitops_env, tmp_path) -> None:
    client = FakeGitClient(tmp_path, fail=True)

    result = sync_policies(git_client=client, runner=FakeCustodianRunner())

    assert result["ok"] is False
    assert result["error"]
    assert result["added"] == 0


def test_sync_not_configured_returns_error(db) -> None:
    from cloudwarden.config import get_settings

    get_settings.cache_clear()  # GITOPS_REPO_URL unset by the isolate-settings fixture

    result = sync_policies(git_client=FakeGitClient("/nope"), runner=FakeCustodianRunner())

    assert result["ok"] is False
    assert "configured" in result["error"].lower()


def test_default_git_client_is_live() -> None:
    from cloudwarden.custodian import gitops

    assert isinstance(gitops._default_git_client(), gitops.LiveGitClient)


# --------------------------------------------------------------------------- #
# API endpoint
# --------------------------------------------------------------------------- #
def test_api_sync_endpoint(db, gitops_env, tmp_path, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    import cloudwarden.custodian.gitops as gitops_mod
    from cloudwarden.api.main import app, get_custodian_runner

    root = _write_repo(tmp_path, {"a.yml": GOOD_VM})
    monkeypatch.setattr(gitops_mod, "_default_git_client", lambda: FakeGitClient(root))
    app.dependency_overrides[get_custodian_runner] = lambda: FakeCustodianRunner()
    try:
        resp = TestClient(app).post("/api/policies/sync")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["added"] == 1
    finally:
        app.dependency_overrides.clear()
