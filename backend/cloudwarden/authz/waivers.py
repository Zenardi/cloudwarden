"""Exemptions / waivers workflow (M14.9).

A **waiver** is a first-class, scoped, justified, approved, *expiring* exception to a
policy — the governed alternative to a static ``finops:exclude`` tag. Its life cycle is
``pending`` → ``active`` (on RBAC-gated approval) → ``expired`` (auto-reconciled once
``expires_at`` passes), or ``rejected``. Only an **active and unexpired** waiver whose
scope covers a match suppresses enforcement; the match is then recorded as **waived**
(with the waiver id), never enforced, and re-surfaces automatically the moment the waiver
expires.

Two layers, both testable offline:

* **Pure resolution** — :func:`is_active` (state + expiry against a deterministic ``now``),
  :func:`scope_covers` (policy-wide / resource / resource-group / tag) and
  :func:`resolve_waiver` / :func:`is_waived`. No database, no clock.
* **Lifecycle + integration** — :func:`request_waiver` / :func:`approve_waiver` /
  :func:`reject_waiver` drive the state machine (validating justification + future expiry);
  :func:`waiver_for_match` powers match-time suppression; :func:`expire_due_waivers`
  reconciles expiries (audited); :func:`notify_expiring_waivers` fires the expiring-soon
  alert once per waiver.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from ..storage import repository as repo
from . import audit

logger = logging.getLogger("cloudwarden.waivers")

STATE_PENDING = "pending"
STATE_ACTIVE = "active"
STATE_REJECTED = "rejected"
STATE_EXPIRED = "expired"

# The scope grains a waiver may target: the whole policy, one resource, a resource
# group, or a ``key=value`` tag.
SCOPE_TYPES: frozenset[str] = frozenset({"policy", "resource", "resource_group", "tag"})


class WaiverError(Exception):
    """A waiver request is invalid (blank justification, past expiry, bad scope)."""


class WaiverNotFound(Exception):
    """The referenced waiver does not exist."""


class WaiverAlreadyDecided(Exception):
    """The waiver has already left ``pending`` — it cannot be approved/rejected again."""


def _as_datetime(value: Any) -> datetime | None:
    """Coerce a datetime / ISO string to a timezone-aware datetime (assume UTC if naive)."""
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    return value  # pragma: no cover - defensive: inputs are datetime/ISO-str/None


def _resolve(value: Any, settings_value: Any) -> Any:
    return settings_value if value is None else value


def _resource_group_of(resource_id: str) -> str | None:
    """The lowercase resource-group segment of an Azure resource id (or ``None``)."""
    parts = (resource_id or "").lower().split("/")
    if "resourcegroups" in parts:
        idx = parts.index("resourcegroups")
        if idx + 1 < len(parts):
            return parts[idx + 1]
    return None


# --------------------------------------------------------------------------- #
# Pure resolution
# --------------------------------------------------------------------------- #
def is_active(waiver: dict[str, Any], now: datetime) -> bool:
    """True iff the waiver is approved (``active``) **and** not yet past ``expires_at``."""
    if waiver.get("state") != STATE_ACTIVE:
        return False
    expires_at = _as_datetime(waiver.get("expires_at"))
    if expires_at is None:
        return False
    return now < expires_at


def scope_covers(waiver: dict[str, Any], match: dict[str, Any]) -> bool:
    """True iff the waiver's scope covers ``match`` (resource id / group / tag / whole policy).

    ``match`` is ``{"resource_id": ..., "tags": {...}}``. A ``policy`` scope covers every
    match; ``resource`` matches an exact id; ``resource_group`` matches the RG segment
    (case-insensitive); ``tag`` matches a ``key=value`` pair against the resource's tags.
    """
    scope_type = waiver.get("scope_type") or "policy"
    scope_value = waiver.get("scope_value")
    if scope_type == "policy":
        return True
    resource_id = match.get("resource_id") or ""
    if scope_type == "resource":
        return scope_value == resource_id
    if scope_type == "resource_group":
        return _resource_group_of(resource_id) == (scope_value or "").strip().lower()
    if scope_type == "tag":
        key, _, value = (scope_value or "").partition("=")
        return str((match.get("tags") or {}).get(key.strip())) == value.strip()
    return False


def resolve_waiver(
    match: dict[str, Any], waivers: list[dict[str, Any]], *, now: datetime
) -> dict[str, Any] | None:
    """The first waiver in ``waivers`` for this match's policy that is active + in-scope."""
    for waiver in waivers:
        if waiver.get("policy_id") != match.get("policy_id"):
            continue
        if is_active(waiver, now) and scope_covers(waiver, match):
            return waiver
    return None


def is_waived(match: dict[str, Any], waivers: list[dict[str, Any]], *, now: datetime) -> bool:
    """True iff any waiver covers this match (an active, in-scope, same-policy waiver)."""
    return resolve_waiver(match, waivers, now=now) is not None


def waiver_for_match(
    session: Any,
    *,
    policy_id: int | None,
    resource_id: str,
    tags: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Resolve the active waiver (if any) covering a policy match, loading candidates by policy.

    Returns the covering waiver row, or ``None`` when the match is enforceable (no active,
    in-scope waiver). ``now`` defaults to the current UTC time; expired-but-not-reconciled
    ``active`` rows are filtered out by :func:`is_active`."""
    if policy_id is None:
        return None
    now = now or datetime.now(UTC)
    candidates = repo.active_waivers_for(session, policy_id)
    match = {"policy_id": policy_id, "resource_id": resource_id, "tags": tags or {}}
    return resolve_waiver(match, candidates, now=now)


# --------------------------------------------------------------------------- #
# Lifecycle (state machine)
# --------------------------------------------------------------------------- #
def request_waiver(
    session: Any,
    *,
    policy_id: int,
    justification: str,
    expires_at: Any,
    scope_type: str = "policy",
    scope_value: str | None = None,
    requester: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Create a ``pending`` waiver, validating justification, scope and a future expiry.

    Raises :class:`WaiverError` for a blank justification, an unknown scope type, or an
    ``expires_at`` at/before ``now``."""
    now = now or datetime.now(UTC)
    if not (justification or "").strip():
        raise WaiverError("justification is required")
    if scope_type not in SCOPE_TYPES:
        raise WaiverError(f"unknown scope_type: {scope_type}")
    expiry = _as_datetime(expires_at)
    if expiry is None or expiry <= now:
        raise WaiverError("expires_at must be in the future")
    return repo.create_waiver(
        session,
        policy_id=policy_id,
        justification=justification.strip(),
        expires_at=expiry,
        scope_type=scope_type,
        scope_value=scope_value,
        requester=requester,
        state=STATE_PENDING,
    )


def _decide(session: Any, waiver_id: int) -> dict[str, Any]:
    """Fetch a waiver that is still ``pending`` (raises otherwise) — the decision guard."""
    waiver = repo.get_waiver(session, waiver_id)
    if waiver is None:
        raise WaiverNotFound(f"waiver {waiver_id} not found")
    if waiver["state"] != STATE_PENDING:
        raise WaiverAlreadyDecided(
            f"waiver {waiver_id} is '{waiver['state']}'; only 'pending' can be decided"
        )
    return waiver


def approve_waiver(
    session: Any, waiver_id: int, *, approver: str | None = None, now: datetime | None = None
) -> dict[str, Any]:
    """Approve a pending waiver → ``active`` (records approver + approval time)."""
    _decide(session, waiver_id)
    now = now or datetime.now(UTC)
    return repo.set_waiver_state(
        session, waiver_id, state=STATE_ACTIVE, approver=approver, approved_at=now
    )


def reject_waiver(session: Any, waiver_id: int, *, approver: str | None = None) -> dict[str, Any]:
    """Reject a pending waiver → ``rejected`` (it never suppresses enforcement)."""
    _decide(session, waiver_id)
    return repo.set_waiver_state(session, waiver_id, state=STATE_REJECTED, approver=approver)


def expire_due_waivers(session: Any, *, now: datetime, actor: str | None = None) -> int:
    """Reconcile ``active`` waivers past their expiry to ``expired`` — auditing each.

    This is the auto-expire pass: once flipped, an expired waiver no longer suppresses
    (its finding re-surfaces). Returns the number of waivers expired."""
    due = repo.waivers_due_to_expire(session, now=now)
    for waiver in due:
        repo.set_waiver_state(session, waiver["id"], state=STATE_EXPIRED)
        audit.record(
            session,
            actor=actor,
            action="waiver:expire",
            target_type="waiver",
            target_id=str(waiver["id"]),
            before={"state": waiver["state"]},
            after={"state": STATE_EXPIRED},
        )
    return len(due)


# --------------------------------------------------------------------------- #
# Expiring-soon notification
# --------------------------------------------------------------------------- #
def _waiver_context(session: Any, waiver: dict[str, Any], now: datetime) -> dict[str, Any]:
    from ..notify import service

    policy = repo.get_policy(session, waiver["policy_id"])
    policy_name = policy["name"] if policy else str(waiver["policy_id"])
    expires_at = _as_datetime(waiver["expires_at"])
    days_left = max(0, (expires_at - now).days) if expires_at else 0
    return service.build_waiver_context(
        waiver_id=waiver["id"],
        policy_name=policy_name,
        scope_type=waiver["scope_type"],
        scope_value=waiver["scope_value"],
        expires_at=expires_at,
        days_left=days_left,
        requester=waiver["requester"],
    )


def notify_expiring_waivers(
    session: Any,
    *,
    now: datetime,
    within_days: int | None = None,
    channel_name: str | None = None,
    dispatch_fn: Any = None,
    template_fn: Any = None,
    settings: Any = None,
) -> dict[str, int]:
    """Alert on active waivers expiring within ``within_days`` — once per waiver.

    Resolves the window + channel from settings when not given, dispatches through the named
    channel (best-effort — a transport failure never breaks the sweep), and marks each
    notified waiver so a later pass never re-alerts. Returns ``{"expiring", "notifications_sent"}``.
    """
    if settings is None:
        from ..config import get_settings

        settings = get_settings()
    within_days = _resolve(within_days, getattr(settings, "waiver_expiring_within_days", 7))
    channel_name = _resolve(channel_name, getattr(settings, "waiver_alert_channel", ""))

    cutoff = now + timedelta(days=within_days)
    candidates = repo.waivers_expiring_between(session, after=now, cutoff=cutoff)
    sent = 0
    template_id: int | None = None

    for waiver in candidates:
        if not channel_name:
            continue
        if dispatch_fn is None:
            from ..notify.dispatch import dispatch_for_waiver

            dispatch_fn = dispatch_for_waiver
        if template_id is None:
            template_fn = template_fn or repo.ensure_waiver_template
            template_id = template_fn(session)
        context = _waiver_context(session, waiver, now)
        try:
            result = dispatch_fn(
                session, context=context, template_id=template_id, channel_name=channel_name
            )
        except Exception:  # noqa: BLE001 - a failed alert must never break the sweep
            logger.warning("waiver %s expiring notification failed", waiver["id"], exc_info=True)
            result = None
        if result is not None:
            repo.mark_waiver_notified(session, waiver["id"])
            sent += 1

    return {"expiring": len(candidates), "notifications_sent": sent}
