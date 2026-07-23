"""Role-based access control (M11.1).

A small, self-contained authorization layer:

* :data:`DEFAULT_ROLES` — the seeded ``admin`` / ``editor`` / ``viewer`` roles and their
  permission grants (action strings like ``policy:write``; ``*`` grants everything);
* :func:`seed_default_roles` — idempotently writes those roles + permissions;
* :func:`has_permission` / :func:`check_permission` — the pure permission logic,
  unit-testable without any HTTP;
* :func:`require_permission` — a FastAPI dependency factory that guards a route: it
  reads the caller from the ``X-Principal`` header, resolves the principal's permissions
  from its role bindings, and raises ``401`` (no principal) / ``403`` (insufficient).

Enforcement is gated by ``RBAC_ENABLED`` (off by default), so the API stays
backward-compatible until roles/bindings are provisioned. Identity is a plain header
today; an SSO subject replaces it in M11.3.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import HTTPException, Request

from ..config import get_settings
from ..storage import repository as repo
from ..storage.db import session_scope

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.orm import Session

# The request header carrying the caller identity (an SSO subject lands in M11.3).
PRINCIPAL_HEADER = "X-Principal"

# The permission that grants every action (held by ``admin``).
WILDCARD = "*"

# Every write/action permission an editor may hold (mutating routes map onto these).
WRITE_PERMISSIONS: tuple[str, ...] = (
    "policy:write",
    "policy:run",
    "policy:propose",
    "collection:write",
    "pack:install",
    "accountgroup:write",
    "binding:write",
    "binding:run",
    "run:trigger",
    "subscription:write",
    "remediation:approve",
    "notification:write",
    "recommendation:decide",
    "budget:write",
    "drift:write",
)

# Administrative permissions (managing RBAC itself, and teams — M11.2) — admin only.
# Not granted to ``editor``; only the ``admin`` wildcard satisfies them.
ADMIN_PERMISSIONS: tuple[str, ...] = ("rbac:admin", "team:write")

# Explicitly-guarded read permissions (M14.1). Most reads are ungated, but the
# commitment endpoint surfaces financially-sensitive purchase recommendations, so
# it is gated behind a FinOps-analyst (``editor``) grant. ``viewer`` stays empty
# (reads that need no grant remain open); ``admin`` satisfies it via the wildcard.
# Budget reads (M14.2) surface spend-vs-limit and are gated the same way. Anomaly
# reads (M14.3) surface spend spikes + drivers and are gated the same way. Forecast
# reads (M14.4) surface projected spend and are gated the same way.
READ_PERMISSIONS: tuple[str, ...] = (
    "commitment:read",
    "budget:read",
    "anomaly:read",
    "forecast:read",
    "showback:read",
    "drift:read",
)

DEFAULT_ROLES: dict[str, dict] = {
    "admin": {
        "description": "Full access to every action, including RBAC administration.",
        "permissions": [WILDCARD],
    },
    "editor": {
        "description": "May create and run governance objects, but not administer RBAC.",
        "permissions": list(WRITE_PERMISSIONS) + list(READ_PERMISSIONS),
    },
    "viewer": {
        "description": "Read-only access. Mutating endpoints are denied.",
        "permissions": [],
    },
}


def has_permission(permissions: set[str], action: str) -> bool:
    """True if ``permissions`` grants ``action`` (exact match or the ``*`` wildcard)."""
    return WILDCARD in permissions or action in permissions


def seed_default_roles(session: Session, bootstrap_admin: str | None = None) -> None:
    """Idempotently create the default roles and their permission grants.

    When ``bootstrap_admin`` is set, that principal is also bound to the ``admin`` role
    — the escape hatch that lets a fresh, RBAC-enabled deployment provision every other
    binding (without it, enabling RBAC with no bindings would lock out role management).
    """
    for name, spec in DEFAULT_ROLES.items():
        repo.upsert_role(
            session,
            name=name,
            description=spec["description"],
            permissions=spec["permissions"],
        )
    if bootstrap_admin:
        repo.assign_role(session, principal=bootstrap_admin, role_name="admin")


def check_permission(
    session: Session, principal: str | None, action: str, *, rbac_enabled: bool
) -> None:
    """Raise unless ``principal`` may perform ``action`` (no-op when RBAC is disabled).

    ``401`` when RBAC is on but no principal is present (unauthenticated); ``403`` when
    the principal is known but lacks the permission. This is the pure core the FastAPI
    dependency delegates to, so it can be tested without any HTTP plumbing.
    """
    if not rbac_enabled:
        return
    if not principal:
        raise HTTPException(status_code=401, detail="authentication required")
    permissions = repo.resolve_permissions(session, principal)
    if not has_permission(permissions, action):
        raise HTTPException(status_code=403, detail=f"permission denied: {action}")


def principal_from_request(request: Request) -> str | None:
    """Resolve the caller principal for RBAC.

    When SSO is enabled (``OIDC_ENABLED``), identity comes from a verified OIDC bearer
    token or first-party session (M11.3) — a present-but-invalid credential raises
    ``401``. Otherwise it is the plain ``X-Principal`` header (``None`` if absent), the
    pre-SSO behaviour that keeps local/mock dev and the existing suites unauthenticated.
    """
    settings = get_settings()
    if settings.oidc_enabled:
        from . import oidc

        return oidc.principal_from_request(request, settings)
    value = request.headers.get(PRINCIPAL_HEADER)
    return value.strip() if value and value.strip() else None


def require_permission(action: str):
    """FastAPI dependency factory that guards a route with ``action``.

    Returns a dependency that short-circuits when RBAC is disabled, otherwise resolves
    the caller's permissions and enforces ``action`` (``401``/``403`` on failure).
    """

    def dependency(request: Request) -> str | None:
        if not get_settings().rbac_enabled:
            return None
        principal = principal_from_request(request)
        with session_scope() as session:
            check_permission(session, principal, action, rbac_enabled=True)
        return principal

    return dependency
