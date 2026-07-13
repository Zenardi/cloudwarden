"""Audit log helper (M11.4).

A thin domain layer over the append-only ``audit_log`` store: mutating API handlers
call :func:`record` with the actor, the action, the target, and the before/after state;
it normalises the payload and persists one row (never updating or deleting). Read
handlers never call it, so reads are unaudited by construction.

:func:`changed_fields` is a small diff used to summarise what an update touched.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..storage import repository as repo

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.orm import Session


def record(
    session: Session,
    *,
    actor: str | None,
    action: str,
    target_type: str,
    target_id: str | None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append one audit entry. ``before``/``after`` default to ``{}`` (create/delete)."""
    return repo.insert_audit_log(
        session,
        actor=actor,
        action=action,
        target_type=target_type,
        target_id=target_id,
        before=before or {},
        after=after or {},
    )


def changed_fields(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """The sorted keys whose value differs between ``before`` and ``after``."""
    keys = set(before) | set(after)
    return sorted(k for k in keys if before.get(k) != after.get(k))
