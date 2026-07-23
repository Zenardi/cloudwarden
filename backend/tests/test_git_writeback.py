"""M14.8 — policy-as-PR GitOps write-back. Tests written FIRST (TDD).

GitOps sync is read-only today: policies flow *from* git into CloudWarden, but a
policy edited in the UI never flows back. This closes the loop — serializing a
policy to its canonical repo YAML and proposing it as a **pull request** on a new
branch. The default branch is never written directly; the reviewed PR stays the
source of truth. Layers:

* **Serialization** (pure): a persisted policy → its canonical ``policies:`` YAML,
  which **round-trips** with the read-sync layout (no drift on re-import) and its
  canonical repo file path.
* **Propose** (injected ``GitProvider``, no network): branch → commit → push →
  open-PR, returning the PR URL. Refuses to target the default branch, surfaces a
  provider failure without partial state, and fails clearly when the token is
  missing. The provider **token is never carried in a loggable object**.
* **API** (``db`` fixture): ``POST /api/policies/{id}/propose`` — RBAC-guarded and
  audited; a provider failure writes **no** audit row (no partial state).
"""

from __future__ import annotations

import pytest
import yaml

from cloudwarden.config import get_settings

# --------------------------------------------------------------------------- #
# Fakes / fixtures
# --------------------------------------------------------------------------- #
_SPEC = {
    "policies": [
        {
            "name": "cw-x",
            "resource": "azure.vm",
            "description": "flag x",
            "filters": [{"type": "value", "key": "tags.owner", "value": "absent"}],
        }
    ]
}


def _policy(**overrides):
    base = {
        "id": 7,
        "name": "cw-x",
        "resource_type": "azure.vm",
        "description": "flag x",
        "spec": _SPEC,
        "version": 3,
    }
    base.update(overrides)
    return base


class _FakeProvider:
    """Records what it was asked to open — never touches git or the network."""

    def __init__(self, pr_url: str = "https://github.com/org/repo/pull/1", fail: bool = False):
        self.pr_url = pr_url
        self.fail = fail
        self.calls: list = []
        self.tokens: list[str] = []

    def open_pull_request(self, request, *, token: str) -> str:
        self.calls.append(request)
        self.tokens.append(token)
        if self.fail:
            raise RuntimeError("network exploded")
        return self.pr_url


def _configure(monkeypatch, *, token="tok-secret", repo="https://github.com/org/policies.git"):
    if token is not None:
        monkeypatch.setenv("GITOPS_WRITEBACK_TOKEN", token)
    if repo is not None:
        monkeypatch.setenv("GITOPS_WRITEBACK_REPO_URL", repo)
    get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# Serialization — canonical YAML + round-trip with the read-sync layout
# --------------------------------------------------------------------------- #
def test_policy_serializes_to_canonical_yaml() -> None:
    from cloudwarden.custodian import gitwriteback

    text = gitwriteback.serialize_policy(_policy())
    doc = yaml.safe_load(text)

    assert list(doc.keys()) == ["policies"]
    entry = doc["policies"][0]
    assert entry["name"] == "cw-x"
    assert entry["resource"] == "azure.vm"
    assert entry["description"] == "flag x"
    assert entry["filters"] == [{"type": "value", "key": "tags.owner", "value": "absent"}]


def test_serialized_yaml_roundtrips_with_read_sync() -> None:
    from cloudwarden.custodian import gitops, gitwriteback

    policy = _policy()
    text = gitwriteback.serialize_policy(policy)

    # Re-import the serialized YAML through the SAME mapping the read-sync uses.
    doc = yaml.safe_load(text)
    record = gitops.policy_record_from_doc(doc["policies"][0])

    assert record["name"] == policy["name"]
    assert record["resource_type"] == policy["resource_type"]
    assert record["description"] == policy["description"]
    assert record["spec"] == policy["spec"]  # no drift on re-import


def test_serialize_omits_empty_resource_and_description() -> None:
    from cloudwarden.custodian import gitops, gitwriteback

    # A policy with no resource_type / description must round-trip back to those
    # same empty values (read-sync reads `resource` default "" and `description` None).
    policy = _policy(resource_type="", description=None, spec={"policies": [{"name": "cw-x"}]})
    doc = yaml.safe_load(gitwriteback.serialize_policy(policy))

    assert "resource" not in doc["policies"][0]
    assert "description" not in doc["policies"][0]
    record = gitops.policy_record_from_doc(doc["policies"][0])
    assert record["resource_type"] == ""
    assert record["description"] is None


def test_serialize_handles_missing_spec() -> None:
    from cloudwarden.custodian import gitwriteback

    # A degenerate policy with no spec still serializes to a valid single-entry doc.
    doc = yaml.safe_load(gitwriteback.serialize_policy(_policy(spec={})))
    assert doc["policies"][0]["name"] == "cw-x"


def test_repo_policy_path_is_canonical(monkeypatch) -> None:
    from cloudwarden.custodian import gitops

    get_settings.cache_clear()
    assert gitops.repo_policy_path("cw-x") == "policies/cw-x.yml"
    # A name with slashes is sanitised so it can never escape the policy dir.
    escaped = gitops.repo_policy_path("../../etc/passwd")
    assert escaped == "policies/..-..-etc-passwd.yml"
    assert "/" not in escaped[len("policies/") :]  # no path separators survive


# --------------------------------------------------------------------------- #
# Propose — branch/commit/push/open-PR via the injected provider
# --------------------------------------------------------------------------- #
def test_propose_creates_branch_and_pr(monkeypatch) -> None:
    from cloudwarden.custodian import gitwriteback

    _configure(monkeypatch)
    provider = _FakeProvider(pr_url="https://github.com/org/policies/pull/9")

    result = gitwriteback.propose_policy_change(_policy(), provider, actor="alice")

    assert result.pr_url == "https://github.com/org/policies/pull/9"
    assert result.branch == "cloudwarden/policy-cw-x-3"
    assert result.base_branch == "main"
    assert result.path == "policies/cw-x.yml"

    assert len(provider.calls) == 1
    req = provider.calls[0]
    assert req.head_branch == "cloudwarden/policy-cw-x-3"
    assert req.base_branch == "main"
    assert req.path == "policies/cw-x.yml"
    assert "cw-x" in req.content
    assert "alice" in req.body
    get_settings.cache_clear()


def test_propose_falls_back_to_sync_repo(monkeypatch) -> None:
    from cloudwarden.custodian import gitwriteback

    # No dedicated write-back repo → reuse the read-sync repo URL.
    monkeypatch.setenv("GITOPS_WRITEBACK_TOKEN", "tok")
    monkeypatch.setenv("GITOPS_REPO_URL", "https://github.com/org/synced.git")
    get_settings.cache_clear()
    provider = _FakeProvider()

    gitwriteback.propose_policy_change(_policy(), provider, actor="bob")

    assert provider.calls[0].repo_url == "https://github.com/org/synced.git"
    get_settings.cache_clear()


def test_refuses_to_target_default_branch(monkeypatch) -> None:
    from cloudwarden.custodian import gitwriteback

    _configure(monkeypatch)
    policy = _policy()
    # Force the computed head branch to collide with the default branch.
    monkeypatch.setenv("GITOPS_BRANCH", gitwriteback.branch_name(policy))
    get_settings.cache_clear()
    provider = _FakeProvider()

    with pytest.raises(gitwriteback.WriteBackError):
        gitwriteback.propose_policy_change(policy, provider, actor="alice")

    assert provider.calls == []  # never asked the provider to push to the default branch
    get_settings.cache_clear()


def test_missing_token_clear_error(monkeypatch) -> None:
    from cloudwarden.custodian import gitwriteback

    monkeypatch.delenv("GITOPS_WRITEBACK_TOKEN", raising=False)
    monkeypatch.setenv("GITOPS_WRITEBACK_REPO_URL", "https://github.com/org/policies.git")
    get_settings.cache_clear()
    provider = _FakeProvider()

    with pytest.raises(gitwriteback.WriteBackError) as exc:
        gitwriteback.propose_policy_change(_policy(), provider, actor="alice")

    assert "token" in str(exc.value).lower()
    assert provider.calls == []  # never reached the provider
    get_settings.cache_clear()


def test_missing_repo_clear_error(monkeypatch) -> None:
    from cloudwarden.custodian import gitwriteback

    monkeypatch.setenv("GITOPS_WRITEBACK_TOKEN", "tok")
    monkeypatch.delenv("GITOPS_WRITEBACK_REPO_URL", raising=False)
    monkeypatch.delenv("GITOPS_REPO_URL", raising=False)
    get_settings.cache_clear()
    provider = _FakeProvider()

    with pytest.raises(gitwriteback.WriteBackError) as exc:
        gitwriteback.propose_policy_change(_policy(), provider, actor="alice")

    assert "repo" in str(exc.value).lower()
    get_settings.cache_clear()


def test_provider_failure_wrapped(monkeypatch) -> None:
    from cloudwarden.custodian import gitwriteback

    _configure(monkeypatch)
    provider = _FakeProvider(fail=True)

    with pytest.raises(gitwriteback.WriteBackProviderError):
        gitwriteback.propose_policy_change(_policy(), provider, actor="alice")

    assert len(provider.calls) == 1  # the provider was reached; the failure surfaced
    get_settings.cache_clear()


def test_empty_pr_url_is_provider_error(monkeypatch) -> None:
    from cloudwarden.custodian import gitwriteback

    _configure(monkeypatch)
    provider = _FakeProvider(pr_url="")

    with pytest.raises(gitwriteback.WriteBackProviderError):
        gitwriteback.propose_policy_change(_policy(), provider, actor="alice")
    get_settings.cache_clear()


def test_token_never_carried_in_loggable_object(monkeypatch) -> None:
    from cloudwarden.custodian import gitwriteback

    _configure(monkeypatch, token="super-secret-pat")
    provider = _FakeProvider()

    result = gitwriteback.propose_policy_change(_policy(), provider, actor="alice")

    # The request/result objects (which may be logged/serialized) never expose the token.
    req = provider.calls[0]
    assert "super-secret-pat" not in repr(req)
    assert "super-secret-pat" not in repr(result)
    assert "super-secret-pat" not in gitwriteback.serialize_policy(_policy())
    get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# API — POST /api/policies/{id}/propose (RBAC + audit + no-partial-write)
# --------------------------------------------------------------------------- #
def _create_policy(session):
    from cloudwarden.storage import repository as repo

    return repo.create_policy(
        session,
        name="cw-x",
        resource_type="azure.vm",
        spec=_SPEC,
        description="flag x",
    )


def test_proposal_is_audited(db, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from cloudwarden.api.main import app, get_git_provider
    from cloudwarden.storage import schema
    from cloudwarden.storage.db import session_scope

    _configure(monkeypatch)
    with session_scope() as s:
        pid = _create_policy(s)["id"]

    fake = _FakeProvider(pr_url="https://github.com/org/policies/pull/42")
    app.dependency_overrides[get_git_provider] = lambda: fake
    try:
        client = TestClient(app)
        resp = client.post(f"/api/policies/{pid}/propose")
        assert resp.status_code == 200
        body = resp.json()
        assert body["pr_url"] == "https://github.com/org/policies/pull/42"
        assert body["branch"] == "cloudwarden/policy-cw-x-1"
        assert body["path"] == "policies/cw-x.yml"
    finally:
        app.dependency_overrides.pop(get_git_provider, None)

    with session_scope() as s:
        audits = s.query(schema.AuditLog).filter(schema.AuditLog.action == "policy.propose").all()
    assert len(audits) == 1
    assert audits[0].after["pr_url"] == "https://github.com/org/policies/pull/42"
    assert audits[0].target_id == str(pid)
    get_settings.cache_clear()


def test_provider_error_surfaced_without_partial_write(db, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from cloudwarden.api.main import app, get_git_provider
    from cloudwarden.storage import schema
    from cloudwarden.storage.db import session_scope

    _configure(monkeypatch)
    with session_scope() as s:
        pid = _create_policy(s)["id"]

    fake = _FakeProvider(fail=True)
    app.dependency_overrides[get_git_provider] = lambda: fake
    try:
        client = TestClient(app)
        resp = client.post(f"/api/policies/{pid}/propose")
        assert resp.status_code == 502  # provider failure surfaced
    finally:
        app.dependency_overrides.pop(get_git_provider, None)

    with session_scope() as s:
        audits = s.query(schema.AuditLog).filter(schema.AuditLog.action == "policy.propose").all()
    assert audits == []  # no partial state — nothing was audited
    get_settings.cache_clear()


def test_propose_unknown_policy_404(db, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from cloudwarden.api.main import app, get_git_provider

    _configure(monkeypatch)
    fake = _FakeProvider()
    app.dependency_overrides[get_git_provider] = lambda: fake
    try:
        client = TestClient(app)
        resp = client.post("/api/policies/999999/propose")
        assert resp.status_code == 404
        assert fake.calls == []
    finally:
        app.dependency_overrides.pop(get_git_provider, None)
    get_settings.cache_clear()


def test_propose_missing_token_is_400(db, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from cloudwarden.api.main import app, get_git_provider
    from cloudwarden.storage.db import session_scope

    monkeypatch.delenv("GITOPS_WRITEBACK_TOKEN", raising=False)
    monkeypatch.setenv("GITOPS_WRITEBACK_REPO_URL", "https://github.com/org/policies.git")
    get_settings.cache_clear()
    with session_scope() as s:
        pid = _create_policy(s)["id"]

    fake = _FakeProvider()
    app.dependency_overrides[get_git_provider] = lambda: fake
    try:
        client = TestClient(app)
        resp = client.post(f"/api/policies/{pid}/propose")
        assert resp.status_code == 400  # misconfig, not a 500
    finally:
        app.dependency_overrides.pop(get_git_provider, None)
    get_settings.cache_clear()


def test_get_git_provider_seam_defaults_to_none() -> None:
    from cloudwarden.api.main import get_git_provider

    # The seam returns None so the live provider is built from settings; tests
    # override it with a fake (exercised above). Asserting the default keeps the
    # unoverridden branch covered.
    assert get_git_provider() is None


def test_propose_requires_permission(db, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from cloudwarden.api.main import app, get_git_provider
    from cloudwarden.authz import rbac
    from cloudwarden.storage import repository as repo
    from cloudwarden.storage.db import session_scope

    _configure(monkeypatch)
    monkeypatch.setenv("RBAC_ENABLED", "1")
    get_settings.cache_clear()
    with session_scope() as s:
        pid = _create_policy(s)["id"]
        rbac.seed_default_roles(s)
        repo.assign_role(s, principal="ed", role_name="editor")

    fake = _FakeProvider()
    app.dependency_overrides[get_git_provider] = lambda: fake
    try:
        client = TestClient(app)
        # No principal → 401.
        assert client.post(f"/api/policies/{pid}/propose").status_code == 401
        # An editor holds policy:propose → 200.
        ok = client.post(f"/api/policies/{pid}/propose", headers={"X-Principal": "ed"})
        assert ok.status_code == 200
    finally:
        app.dependency_overrides.pop(get_git_provider, None)
    get_settings.cache_clear()
