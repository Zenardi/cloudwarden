"""Role-based access control (M11.1): roles, permissions, role bindings + a
``require_permission`` FastAPI dependency guarding mutating endpoints.

Written test-first (TDD). The permission check is exercised **in isolation** via the
pure ``has_permission`` and the ``check_permission`` core (no HTTP), and end-to-end
through the API with a ``X-Principal`` header. RBAC is gated by ``RBAC_ENABLED`` (off
by default so the existing unauthenticated suite is unaffected); the API tests enable
it explicitly. The ``db`` fixture makes seeding/binding/resolution really persist.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from cloudwarden.authz import rbac
from cloudwarden.storage import repository as repo
from cloudwarden.storage.db import session_scope

VALID_SPEC = {"policies": [{"name": "rbac-p", "resource": "azure.vm"}]}


def _enable_rbac(monkeypatch) -> None:
    from cloudwarden.config import get_settings

    monkeypatch.setenv("RBAC_ENABLED", "1")
    get_settings.cache_clear()


def _seed_and_bind(principal: str, role: str) -> None:
    with session_scope() as s:
        rbac.seed_default_roles(s)
        assert repo.assign_role(s, principal=principal, role_name=role) is not None


# --------------------------------------------------------------------------- #
# Pure permission logic (isolated)
# --------------------------------------------------------------------------- #
def test_has_permission_exact() -> None:
    assert rbac.has_permission({"policy:write"}, "policy:write") is True
    assert rbac.has_permission({"policy:read"}, "policy:write") is False


def test_has_permission_wildcard() -> None:
    assert rbac.has_permission({"*"}, "anything:goes") is True


# --------------------------------------------------------------------------- #
# Default roles + resolution
# --------------------------------------------------------------------------- #
def test_default_roles_seeded(db) -> None:
    with session_scope() as s:
        rbac.seed_default_roles(s)
        roles = {r["name"]: r for r in repo.list_roles(s)}

    assert set(roles) == {"admin", "editor", "viewer"}
    assert roles["admin"]["permissions"] == ["*"]
    assert roles["viewer"]["permissions"] == []
    assert "policy:write" in roles["editor"]["permissions"]


def test_bootstrap_admin_is_bound(db) -> None:
    """A bootstrap-admin principal is granted the admin role at seed time."""
    with session_scope() as s:
        rbac.seed_default_roles(s, bootstrap_admin="root")
        assert "*" in repo.resolve_permissions(s, "root")


def test_seed_is_idempotent(db) -> None:
    with session_scope() as s:
        rbac.seed_default_roles(s)
        rbac.seed_default_roles(s)
        roles = repo.list_roles(s)
        admin = next(r for r in roles if r["name"] == "admin")

    assert len(roles) == 3
    assert admin["permissions"] == ["*"]  # not duplicated


def test_role_binding_resolves_permissions(db) -> None:
    _seed_and_bind("alice", "editor")

    with session_scope() as s:
        perms = repo.resolve_permissions(s, "alice")

    assert "policy:write" in perms
    assert "*" not in perms  # editor is not admin


def test_resolve_permissions_unknown_principal_is_empty(db) -> None:
    with session_scope() as s:
        rbac.seed_default_roles(s)
        assert repo.resolve_permissions(s, "nobody") == set()


def test_assign_unknown_role_returns_none(db) -> None:
    with session_scope() as s:
        rbac.seed_default_roles(s)
        assert repo.assign_role(s, principal="x", role_name="ghost") is None


# --------------------------------------------------------------------------- #
# check_permission core (isolated — no HTTP)
# --------------------------------------------------------------------------- #
def test_permission_granted_allows(db) -> None:
    _seed_and_bind("editor-user", "editor")

    with session_scope() as s:
        # does not raise
        rbac.check_permission(s, "editor-user", "policy:write", rbac_enabled=True)


def test_permission_missing_returns_403(db) -> None:
    _seed_and_bind("viewer-user", "viewer")

    with session_scope() as s, pytest.raises(HTTPException) as exc:
        rbac.check_permission(s, "viewer-user", "policy:write", rbac_enabled=True)
    assert exc.value.status_code == 403


def test_no_principal_returns_401(db) -> None:
    with session_scope() as s, pytest.raises(HTTPException) as exc:
        rbac.check_permission(s, None, "policy:write", rbac_enabled=True)
    assert exc.value.status_code == 401


def test_disabled_rbac_allows_anything(db) -> None:
    with session_scope() as s:
        # no principal, no roles, but RBAC off → allowed
        rbac.check_permission(s, None, "policy:write", rbac_enabled=False)


def test_admin_wildcard_allows_any_action(db) -> None:
    _seed_and_bind("root", "admin")

    with session_scope() as s:
        rbac.check_permission(s, "root", "some:novel:action", rbac_enabled=True)


# --------------------------------------------------------------------------- #
# End-to-end through the API (require_permission dependency)
# --------------------------------------------------------------------------- #
def test_api_rbac_disabled_allows_write(db) -> None:
    """Backward compatibility: with RBAC off, an unauthenticated write still works."""
    from cloudwarden.api.main import app

    resp = TestClient(app).post(
        "/api/policies",
        json={"name": "compat-p", "resource_type": "azure.vm", "spec": VALID_SPEC},
    )

    assert resp.status_code == 201


def test_api_editor_can_create_policy(db, monkeypatch) -> None:
    from cloudwarden.api.main import app

    _enable_rbac(monkeypatch)
    _seed_and_bind("ed", "editor")

    resp = TestClient(app).post(
        "/api/policies",
        json={"name": "ed-p", "resource_type": "azure.vm", "spec": VALID_SPEC},
        headers={"X-Principal": "ed"},
    )

    assert resp.status_code == 201


def test_api_viewer_can_read_not_write(db, monkeypatch) -> None:
    from cloudwarden.api.main import app

    _enable_rbac(monkeypatch)
    _seed_and_bind("vv", "viewer")
    client = TestClient(app)

    read = client.get("/api/policies", headers={"X-Principal": "vv"})
    write = client.post(
        "/api/policies",
        json={"name": "vv-p", "resource_type": "azure.vm", "spec": VALID_SPEC},
        headers={"X-Principal": "vv"},
    )

    assert read.status_code == 200  # reads are ungated
    assert write.status_code == 403


def test_api_write_without_principal_is_401(db, monkeypatch) -> None:
    from cloudwarden.api.main import app

    _enable_rbac(monkeypatch)
    with session_scope() as s:
        rbac.seed_default_roles(s)

    resp = TestClient(app).post(
        "/api/policies",
        json={"name": "anon-p", "resource_type": "azure.vm", "spec": VALID_SPEC},
    )

    assert resp.status_code == 401


def test_api_authz_me_reports_permissions(db, monkeypatch) -> None:
    from cloudwarden.api.main import app

    _enable_rbac(monkeypatch)
    _seed_and_bind("ed", "editor")

    resp = TestClient(app).get("/api/authz/me", headers={"X-Principal": "ed"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["principal"] == "ed"
    assert "policy:write" in body["permissions"]


def test_remove_binding_unknown_role_is_false(db) -> None:
    with session_scope() as s:
        rbac.seed_default_roles(s)
        assert repo.remove_role_binding(s, "someone", "ghost-role") is False


def test_api_create_binding_unknown_role_404(db, monkeypatch) -> None:
    from cloudwarden.api.main import app

    _enable_rbac(monkeypatch)
    _seed_and_bind("boss", "admin")

    resp = TestClient(app).post(
        "/api/authz/role-bindings",
        json={"principal": "x", "role": "ghost-role"},
        headers={"X-Principal": "boss"},
    )

    assert resp.status_code == 404


def test_api_authz_me_anonymous(db) -> None:
    """With no principal header the caller sees an empty permission set (never errors)."""
    from cloudwarden.api.main import app

    resp = TestClient(app).get("/api/authz/me")

    assert resp.status_code == 200
    assert resp.json() == {"principal": None, "permissions": [], "rbac_enabled": False}


def test_api_lists_roles_and_bindings(db) -> None:
    from cloudwarden.api.main import app

    _seed_and_bind("dana", "editor")
    client = TestClient(app)

    roles = client.get("/api/authz/roles").json()
    bindings = client.get("/api/authz/role-bindings", params={"principal": "dana"}).json()

    assert {r["name"] for r in roles} == {"admin", "editor", "viewer"}
    assert bindings == [{"principal": "dana", "role": "editor"}]


def test_api_delete_role_binding(db, monkeypatch) -> None:
    from cloudwarden.api.main import app

    _enable_rbac(monkeypatch)
    _seed_and_bind("boss", "admin")
    _seed_and_bind("temp", "editor")
    client = TestClient(app)

    gone = client.request(
        "DELETE",
        "/api/authz/role-bindings",
        params={"principal": "temp", "role": "editor"},
        headers={"X-Principal": "boss"},
    )
    missing = client.request(
        "DELETE",
        "/api/authz/role-bindings",
        params={"principal": "temp", "role": "editor"},
        headers={"X-Principal": "boss"},
    )

    assert gone.status_code == 200
    assert missing.status_code == 404
    with session_scope() as s:
        assert repo.resolve_permissions(s, "temp") == set()


def test_api_role_binding_requires_admin(db, monkeypatch) -> None:
    from cloudwarden.api.main import app

    _enable_rbac(monkeypatch)
    _seed_and_bind("boss", "admin")
    _seed_and_bind("peon", "viewer")
    client = TestClient(app)

    ok = client.post(
        "/api/authz/role-bindings",
        json={"principal": "newbie", "role": "editor"},
        headers={"X-Principal": "boss"},
    )
    denied = client.post(
        "/api/authz/role-bindings",
        json={"principal": "newbie", "role": "editor"},
        headers={"X-Principal": "peon"},
    )

    assert ok.status_code in (200, 201)
    assert denied.status_code == 403
    with session_scope() as s:
        assert "policy:write" in repo.resolve_permissions(s, "newbie")
