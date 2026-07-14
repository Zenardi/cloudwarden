"""Teams & membership (M11.2): multi-tenancy scoping of governance resources.

Written test-first (TDD). Three layers are exercised:

* **Repository** — team CRUD, membership, and ``team_id`` scoping on policies, in
  isolation (no HTTP).
* **authz.teams core** — the pure scoping logic (``is_admin`` / ``visible_team_ids`` /
  ``ensure_policy_access`` / ``resolve_owning_team``), also isolated.
* **API end-to-end** — a member sees only their team's policies, an admin sees all,
  cross-team access is denied (403), and removing a member revokes access.

Team scoping is gated by ``RBAC_ENABLED`` (off by default so the existing
unauthenticated suite is unaffected); the API tests enable it explicitly. The ``db``
fixture makes seeding/membership/scoping really persist.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from cloudwarden.authz import rbac, teams
from cloudwarden.storage import repository as repo
from cloudwarden.storage.db import session_scope

VALID_SPEC = {"policies": [{"name": "team-p", "resource": "azure.vm"}]}


def _enable_rbac(monkeypatch) -> None:
    from cloudwarden.config import get_settings

    monkeypatch.setenv("RBAC_ENABLED", "1")
    get_settings.cache_clear()


def _seed_and_bind(principal: str, role: str) -> None:
    with session_scope() as s:
        rbac.seed_default_roles(s)
        assert repo.assign_role(s, principal=principal, role_name=role) is not None


def _new_team(name: str, description: str | None = None) -> int:
    with session_scope() as s:
        return repo.create_team(s, name=name, description=description)["id"]


def _member_policy(name: str, team_id: int | None) -> dict:
    with session_scope() as s:
        return repo.create_policy(
            s, name=name, resource_type="azure.vm", spec=VALID_SPEC, team_id=team_id
        )


# --------------------------------------------------------------------------- #
# Team CRUD (repository, isolated)
# --------------------------------------------------------------------------- #
def test_create_team(db) -> None:
    tid = _new_team("platform", "Platform engineering")

    with session_scope() as s:
        teams_list = repo.list_teams(s)

    assert [(t["name"], t["description"]) for t in teams_list] == [
        ("platform", "Platform engineering")
    ]
    assert isinstance(tid, int)


def test_create_team_duplicate_name_raises(db) -> None:
    _new_team("dup")

    with pytest.raises(IntegrityError), session_scope() as s:
        repo.create_team(s, name="dup", description=None)


def test_get_team_by_name(db) -> None:
    _new_team("findme")

    with session_scope() as s:
        assert repo.get_team_by_name(s, "findme") is not None
        assert repo.get_team_by_name(s, "ghost") is None


# --------------------------------------------------------------------------- #
# Membership (repository, isolated)
# --------------------------------------------------------------------------- #
def test_add_and_list_members(db) -> None:
    tid = _new_team("t")

    with session_scope() as s:
        assert repo.add_team_member(s, team_id=tid, principal="alice") is not None
        members = repo.list_team_members(s, tid)

    assert members == [{"principal": "alice", "role": "member"}]


def test_add_member_unknown_team_returns_none(db) -> None:
    with session_scope() as s:
        assert repo.add_team_member(s, team_id=999, principal="alice") is None


def test_add_member_is_idempotent(db) -> None:
    tid = _new_team("t")

    with session_scope() as s:
        repo.add_team_member(s, team_id=tid, principal="alice")
        repo.add_team_member(s, team_id=tid, principal="alice")
        assert repo.list_team_members(s, tid) == [{"principal": "alice", "role": "member"}]


def test_remove_member(db) -> None:
    tid = _new_team("t")

    with session_scope() as s:
        repo.add_team_member(s, team_id=tid, principal="alice")
        assert repo.remove_team_member(s, tid, "alice") is True
        assert repo.remove_team_member(s, tid, "alice") is False
        assert repo.list_team_members(s, tid) == []


def test_list_teams_for_principal(db) -> None:
    a = _new_team("a")
    b = _new_team("b")
    _new_team("c")

    with session_scope() as s:
        repo.add_team_member(s, team_id=a, principal="alice")
        repo.add_team_member(s, team_id=b, principal="alice")
        assert sorted(repo.list_teams_for_principal(s, "alice")) == sorted([a, b])
        assert repo.list_teams_for_principal(s, "nobody") == []


def test_is_team_member(db) -> None:
    tid = _new_team("t")

    with session_scope() as s:
        repo.add_team_member(s, team_id=tid, principal="alice")
        assert repo.is_team_member(s, tid, "alice") is True
        assert repo.is_team_member(s, tid, "bob") is False


# --------------------------------------------------------------------------- #
# Policy team scoping (repository, isolated)
# --------------------------------------------------------------------------- #
def test_policy_scoped_to_team(db) -> None:
    tid = _new_team("owner")

    policy = _member_policy("scoped-p", tid)

    assert policy["team_id"] == tid


def test_policy_defaults_to_no_team(db) -> None:
    policy = _member_policy("global-p", None)

    assert policy["team_id"] is None


def test_list_policies_team_filter(db) -> None:
    a = _new_team("a")
    b = _new_team("b")
    _member_policy("a-p", a)
    _member_policy("b-p", b)
    _member_policy("global-p", None)

    with session_scope() as s:
        only_a = repo.list_policies(s, team_ids=[a])
        all_policies = repo.list_policies(s)
        none_visible = repo.list_policies(s, team_ids=[])

    assert [p["name"] for p in only_a] == ["a-p"]
    assert {p["name"] for p in all_policies} == {"a-p", "b-p", "global-p"}
    assert none_visible == []


# --------------------------------------------------------------------------- #
# authz.teams core (isolated — no HTTP)
# --------------------------------------------------------------------------- #
def test_is_admin_true_for_wildcard(db) -> None:
    _seed_and_bind("root", "admin")

    with session_scope() as s:
        assert teams.is_admin(s, "root") is True


def test_is_admin_false_for_editor(db) -> None:
    _seed_and_bind("ed", "editor")

    with session_scope() as s:
        assert teams.is_admin(s, "ed") is False
        assert teams.is_admin(s, None) is False


def test_visible_team_ids_none_when_rbac_disabled(db) -> None:
    with session_scope() as s:
        assert teams.visible_team_ids(s, "alice", rbac_enabled=False) is None


def test_visible_team_ids_none_for_admin(db) -> None:
    _seed_and_bind("root", "admin")

    with session_scope() as s:
        assert teams.visible_team_ids(s, "root", rbac_enabled=True) is None


def test_visible_team_ids_for_member(db) -> None:
    tid = _new_team("t")
    _seed_and_bind("alice", "editor")

    with session_scope() as s:
        repo.add_team_member(s, team_id=tid, principal="alice")
        assert teams.visible_team_ids(s, "alice", rbac_enabled=True) == [tid]


def test_visible_team_ids_empty_for_anonymous(db) -> None:
    with session_scope() as s:
        assert teams.visible_team_ids(s, None, rbac_enabled=True) == []


def test_ensure_policy_access_allows_member(db) -> None:
    tid = _new_team("t")
    _seed_and_bind("alice", "editor")
    policy = _member_policy("p", tid)

    with session_scope() as s:
        repo.add_team_member(s, team_id=tid, principal="alice")
        teams.ensure_policy_access(s, "alice", policy, rbac_enabled=True)  # no raise


def test_ensure_policy_access_denies_non_member(db) -> None:
    tid = _new_team("t")
    _seed_and_bind("bob", "editor")
    policy = _member_policy("p", tid)

    with session_scope() as s, pytest.raises(HTTPException) as exc:
        teams.ensure_policy_access(s, "bob", policy, rbac_enabled=True)
    assert exc.value.status_code == 403


def test_ensure_policy_access_allows_admin(db) -> None:
    tid = _new_team("t")
    _seed_and_bind("root", "admin")
    policy = _member_policy("p", tid)

    with session_scope() as s:
        teams.ensure_policy_access(s, "root", policy, rbac_enabled=True)  # no raise


def test_ensure_policy_access_noop_when_disabled(db) -> None:
    tid = _new_team("t")
    policy = _member_policy("p", tid)

    with session_scope() as s:
        teams.ensure_policy_access(s, None, policy, rbac_enabled=False)  # no raise


def test_resolve_owning_team_derives_from_membership(db) -> None:
    tid = _new_team("t")
    _seed_and_bind("alice", "editor")

    with session_scope() as s:
        repo.add_team_member(s, team_id=tid, principal="alice")
        got = teams.resolve_owning_team(s, "alice", requested_team=None, rbac_enabled=True)
    assert got == tid


def test_resolve_owning_team_none_when_disabled(db) -> None:
    with session_scope() as s:
        assert teams.resolve_owning_team(s, "x", requested_team=None, rbac_enabled=False) is None


def test_resolve_owning_team_explicit_requires_membership(db) -> None:
    _new_team("t")
    _seed_and_bind("bob", "editor")

    with session_scope() as s, pytest.raises(HTTPException) as exc:
        teams.resolve_owning_team(s, "bob", requested_team="t", rbac_enabled=True)
    assert exc.value.status_code == 403


def test_resolve_owning_team_unknown_team_404(db) -> None:
    _seed_and_bind("root", "admin")

    with session_scope() as s, pytest.raises(HTTPException) as exc:
        teams.resolve_owning_team(s, "root", requested_team="ghost", rbac_enabled=True)
    assert exc.value.status_code == 404


def test_resolve_owning_team_admin_explicit(db) -> None:
    tid = _new_team("t")
    _seed_and_bind("root", "admin")

    with session_scope() as s:
        got = teams.resolve_owning_team(s, "root", requested_team="t", rbac_enabled=True)
    assert got == tid


def test_resolve_owning_team_explicit_member_ok(db) -> None:
    """A non-admin member naming their own team gets that team (not a 403)."""
    tid = _new_team("t")
    _seed_and_bind("alice", "editor")

    with session_scope() as s:
        repo.add_team_member(s, team_id=tid, principal="alice")
        got = teams.resolve_owning_team(s, "alice", requested_team="t", rbac_enabled=True)
    assert got == tid


def test_resolve_owning_team_admin_derives_none(db) -> None:
    """An admin with no requested team owns nothing in particular — unscoped/global."""
    _seed_and_bind("root", "admin")

    with session_scope() as s:
        assert teams.resolve_owning_team(s, "root", requested_team=None, rbac_enabled=True) is None
        # An anonymous caller likewise derives no team.
        assert teams.resolve_owning_team(s, None, requested_team=None, rbac_enabled=True) is None


# --------------------------------------------------------------------------- #
# API end-to-end (require_permission + team scoping)
# --------------------------------------------------------------------------- #
def test_api_create_team_requires_admin(db, monkeypatch) -> None:
    from cloudwarden.api.main import app

    _enable_rbac(monkeypatch)
    _seed_and_bind("root", "admin")
    _seed_and_bind("peon", "editor")
    client = TestClient(app)

    ok = client.post("/api/teams", json={"name": "t1"}, headers={"X-Principal": "root"})
    denied = client.post("/api/teams", json={"name": "t2"}, headers={"X-Principal": "peon"})

    assert ok.status_code == 201
    assert denied.status_code == 403


def test_api_create_team_duplicate_409(db, monkeypatch) -> None:
    from cloudwarden.api.main import app

    _enable_rbac(monkeypatch)
    _seed_and_bind("root", "admin")
    client = TestClient(app)

    client.post("/api/teams", json={"name": "t"}, headers={"X-Principal": "root"})
    dup = client.post("/api/teams", json={"name": "t"}, headers={"X-Principal": "root"})

    assert dup.status_code == 409


def test_api_add_member_unknown_team_404(db, monkeypatch) -> None:
    from cloudwarden.api.main import app

    _enable_rbac(monkeypatch)
    _seed_and_bind("root", "admin")

    resp = TestClient(app).post(
        "/api/teams/999/members",
        json={"principal": "alice"},
        headers={"X-Principal": "root"},
    )

    assert resp.status_code == 404


def test_api_create_policy_assigns_team(db, monkeypatch) -> None:
    from cloudwarden.api.main import app

    _enable_rbac(monkeypatch)
    _seed_and_bind("alice", "editor")
    tid = _new_team("alpha")
    with session_scope() as s:
        repo.add_team_member(s, team_id=tid, principal="alice")

    resp = TestClient(app).post(
        "/api/policies",
        json={"name": "alice-p", "resource_type": "azure.vm", "spec": VALID_SPEC},
        headers={"X-Principal": "alice"},
    )

    assert resp.status_code == 201
    assert resp.json()["team_id"] == tid


def test_api_member_sees_only_team_resources(db, monkeypatch) -> None:
    from cloudwarden.api.main import app

    _enable_rbac(monkeypatch)
    _seed_and_bind("alice", "editor")
    _seed_and_bind("bob", "editor")
    ta = _new_team("alpha")
    tb = _new_team("beta")
    with session_scope() as s:
        repo.add_team_member(s, team_id=ta, principal="alice")
        repo.add_team_member(s, team_id=tb, principal="bob")
    _member_policy("alpha-p", ta)
    _member_policy("beta-p", tb)
    client = TestClient(app)

    alice_view = client.get("/api/policies", headers={"X-Principal": "alice"}).json()
    bob_view = client.get("/api/policies", headers={"X-Principal": "bob"}).json()

    assert [p["name"] for p in alice_view] == ["alpha-p"]
    assert [p["name"] for p in bob_view] == ["beta-p"]


def test_api_admin_sees_all_teams(db, monkeypatch) -> None:
    from cloudwarden.api.main import app

    _enable_rbac(monkeypatch)
    _seed_and_bind("root", "admin")
    ta = _new_team("alpha")
    tb = _new_team("beta")
    _member_policy("alpha-p", ta)
    _member_policy("beta-p", tb)

    view = TestClient(app).get("/api/policies", headers={"X-Principal": "root"}).json()

    assert {p["name"] for p in view} == {"alpha-p", "beta-p"}


def test_api_cross_team_access_denied(db, monkeypatch) -> None:
    from cloudwarden.api.main import app

    _enable_rbac(monkeypatch)
    _seed_and_bind("alice", "editor")
    ta = _new_team("alpha")
    tb = _new_team("beta")
    with session_scope() as s:
        repo.add_team_member(s, team_id=ta, principal="alice")
    beta_policy = _member_policy("beta-p", tb)
    client = TestClient(app)

    own = client.get(
        f"/api/policies/{_member_policy('alpha-p', ta)['id']}", headers={"X-Principal": "alice"}
    )
    cross = client.get(f"/api/policies/{beta_policy['id']}", headers={"X-Principal": "alice"})

    assert own.status_code == 200
    assert cross.status_code == 403


def test_api_cross_team_update_denied(db, monkeypatch) -> None:
    from cloudwarden.api.main import app

    _enable_rbac(monkeypatch)
    _seed_and_bind("alice", "editor")
    ta = _new_team("alpha")
    tb = _new_team("beta")
    with session_scope() as s:
        repo.add_team_member(s, team_id=ta, principal="alice")
    beta_policy = _member_policy("beta-p", tb)

    resp = TestClient(app).delete(
        f"/api/policies/{beta_policy['id']}", headers={"X-Principal": "alice"}
    )

    assert resp.status_code == 403


def test_api_remove_member_revokes_access(db, monkeypatch) -> None:
    from cloudwarden.api.main import app

    _enable_rbac(monkeypatch)
    _seed_and_bind("root", "admin")
    _seed_and_bind("alice", "editor")
    tid = _new_team("alpha")
    with session_scope() as s:
        repo.add_team_member(s, team_id=tid, principal="alice")
    policy = _member_policy("alpha-p", tid)
    client = TestClient(app)

    before = client.get("/api/policies", headers={"X-Principal": "alice"}).json()
    removed = client.request(
        "DELETE",
        f"/api/teams/{tid}/members/alice",
        headers={"X-Principal": "root"},
    )
    after = client.get("/api/policies", headers={"X-Principal": "alice"}).json()
    cross = client.get(f"/api/policies/{policy['id']}", headers={"X-Principal": "alice"})

    assert [p["name"] for p in before] == ["alpha-p"]
    assert removed.status_code == 200
    assert after == []
    assert cross.status_code == 403


def test_api_remove_member_unknown_404(db, monkeypatch) -> None:
    from cloudwarden.api.main import app

    _enable_rbac(monkeypatch)
    _seed_and_bind("root", "admin")
    tid = _new_team("alpha")

    resp = TestClient(app).request(
        "DELETE", f"/api/teams/{tid}/members/ghost", headers={"X-Principal": "root"}
    )

    assert resp.status_code == 404


def test_api_list_and_get_team(db, monkeypatch) -> None:
    from cloudwarden.api.main import app

    _enable_rbac(monkeypatch)
    _seed_and_bind("root", "admin")
    tid = _new_team("alpha")
    with session_scope() as s:
        repo.add_team_member(s, team_id=tid, principal="alice")
    client = TestClient(app)

    listing = client.get("/api/teams").json()
    detail = client.get(f"/api/teams/{tid}").json()
    members = client.get(f"/api/teams/{tid}/members").json()
    missing = client.get("/api/teams/999")

    assert [t["name"] for t in listing] == ["alpha"]
    assert detail["name"] == "alpha"
    assert members == [{"principal": "alice", "role": "member"}]
    assert missing.status_code == 404


def test_api_add_member_via_endpoint(db, monkeypatch) -> None:
    from cloudwarden.api.main import app

    _enable_rbac(monkeypatch)
    _seed_and_bind("root", "admin")
    tid = _new_team("alpha")

    resp = TestClient(app).post(
        f"/api/teams/{tid}/members",
        json={"principal": "alice", "role": "lead"},
        headers={"X-Principal": "root"},
    )

    assert resp.status_code == 201
    with session_scope() as s:
        assert repo.list_team_members(s, tid) == [{"principal": "alice", "role": "lead"}]


def test_api_rbac_disabled_lists_all_policies(db) -> None:
    """Backward compatibility: with RBAC off, listing is unscoped (no team filter)."""
    from cloudwarden.api.main import app

    a = _new_team("a")
    _member_policy("a-p", a)
    _member_policy("global-p", None)

    view = TestClient(app).get("/api/policies").json()

    assert {p["name"] for p in view} == {"a-p", "global-p"}
