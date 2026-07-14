"""Team-scoped multi-tenancy (M11.2).

Builds on the RBAC layer (:mod:`cloudwarden.authz.rbac`) to scope governance
resources (policies today) to an owning **team**. A team member sees and manages only
their team's resources; an **admin** (holding the RBAC wildcard ``*``) sees across all
teams. Scoping is gated by ``RBAC_ENABLED`` — with RBAC off there is no principal, so
every resource is visible (backward-compatible).

The functions here are the pure, HTTP-free core the API delegates to:

* :func:`is_admin` — does a principal hold the wildcard permission?
* :func:`visible_team_ids` — which teams' resources may a principal list? (``None`` =
  all — admin or RBAC disabled; otherwise the principal's team ids, possibly empty);
* :func:`ensure_policy_access` — raise ``403`` unless a principal may touch one policy;
* :func:`resolve_owning_team` — the team a newly created policy should belong to.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import HTTPException

from ..storage import repository as repo
from . import rbac

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.orm import Session


def is_admin(session: Session, principal: str | None) -> bool:
    """True if ``principal`` holds the RBAC wildcard (and so transcends team scoping)."""
    if not principal:
        return False
    return rbac.WILDCARD in repo.resolve_permissions(session, principal)


def visible_team_ids(
    session: Session, principal: str | None, *, rbac_enabled: bool
) -> list[int] | None:
    """The team ids whose resources ``principal`` may see (``None`` means *all*).

    ``None`` when scoping does not apply — RBAC disabled, or the caller is an admin —
    so the caller sees every resource. Otherwise the principal's team ids (an empty
    list for a principal that belongs to no team, or an anonymous caller).
    """
    if not rbac_enabled or is_admin(session, principal):
        return None
    if not principal:
        return []
    return repo.list_teams_for_principal(session, principal)


def ensure_policy_access(
    session: Session, principal: str | None, policy: dict[str, Any], *, rbac_enabled: bool
) -> None:
    """Raise ``403`` unless ``principal`` may access ``policy`` under team scoping.

    A no-op when RBAC is disabled or the caller is an admin. Otherwise the caller must
    be a member of the policy's owning team; a policy owned by another team (or an
    unscoped policy, ``team_id`` ``None``) is denied to a non-admin.
    """
    if not rbac_enabled or is_admin(session, principal):
        return
    team_id = policy.get("team_id")
    visible = repo.list_teams_for_principal(session, principal) if principal else []
    if team_id is None or team_id not in visible:
        raise HTTPException(status_code=403, detail="cross-team access denied")


def resolve_owning_team(
    session: Session, principal: str | None, *, requested_team: str | None, rbac_enabled: bool
) -> int | None:
    """The team a new policy should be owned by (``None`` = unscoped/global).

    With RBAC off, always ``None`` (no scoping). When a ``requested_team`` name is
    given the caller must be an admin or a member of it (``404`` unknown team, ``403``
    not a member). Otherwise the team is derived from the caller's membership: exactly
    one team → that team; zero or several (ambiguous), or an admin → ``None``.
    """
    if not rbac_enabled:
        return None
    if requested_team:
        team = repo.get_team_by_name(session, requested_team)
        if team is None:
            raise HTTPException(status_code=404, detail=f"unknown team: {requested_team}")
        if is_admin(session, principal):
            return team.id
        if not principal or not repo.is_team_member(session, team.id, principal):
            raise HTTPException(status_code=403, detail=f"not a member of team: {requested_team}")
        return team.id
    if not principal or is_admin(session, principal):
        return None
    team_ids = repo.list_teams_for_principal(session, principal)
    return team_ids[0] if len(team_ids) == 1 else None
