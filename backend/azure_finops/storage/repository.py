"""Repository: idempotent writes and read helpers over the ORM schema.

All fact writes use PostgreSQL ``INSERT ... ON CONFLICT DO UPDATE`` and dedupe
within the batch first (Postgres rejects a conflict target hit twice in one
statement), so re-running a collection never creates duplicate rows.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, literal_column, or_, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from .. import models as m
from . import schema


# --------------------------------------------------------------------------- #
# Run lifecycle
# --------------------------------------------------------------------------- #
def create_run(
    session: Session,
    *,
    run_id: str,
    subscription_id: str | None,
    metric_lookback_days: int,
    cost_lookback_days: int,
    mock: bool,
    provider_used: str | None = None,
    model: str | None = None,
) -> None:
    session.add(
        schema.Run(
            run_id=run_id,
            subscription_id=subscription_id,
            metric_lookback_days=metric_lookback_days,
            cost_lookback_days=cost_lookback_days,
            mock=mock,
            provider_used=provider_used,
            model=model,
            status="running",
        )
    )
    session.flush()


def finish_run(session: Session, run_id: str, status: str, notes: str | None = None) -> None:
    run = session.get(schema.Run, run_id)
    if run is not None:
        run.status = status
        run.finished_at = datetime.now(UTC)
        if notes:
            run.notes = notes


# --------------------------------------------------------------------------- #
# Policy executions + matches (M3.1) — audit trail of what policies did
# --------------------------------------------------------------------------- #
def _policy_execution_public(rec: schema.PolicyExecution) -> dict[str, Any]:
    """Serialize an execution row into a JSON-friendly dict (timestamps as ISO-8601)."""
    return {
        "execution_id": rec.execution_id,
        "policy_id": rec.policy_id,
        "subscription_id": rec.subscription_id,
        "binding_id": rec.binding_id,
        "mode": rec.mode,
        "event_id": rec.event_id,
        "status": rec.status,
        "started_at": rec.started_at.isoformat() if rec.started_at else None,
        "finished_at": rec.finished_at.isoformat() if rec.finished_at else None,
        "resources_matched": rec.resources_matched,
        "actions_taken": rec.actions_taken,
        "error": rec.error,
    }


def _policy_match_public(rec: schema.PolicyMatch) -> dict[str, Any]:
    """Serialize a policy-match row into a JSON-friendly dict."""
    return {
        "id": rec.id,
        "execution_id": rec.execution_id,
        "resource_id": rec.resource_id,
        "resource_type": rec.resource_type,
        "matched_at": rec.matched_at.isoformat() if rec.matched_at else None,
        "action_taken": rec.action_taken,
        "action_result": rec.action_result,
    }


def create_policy_execution(
    session: Session,
    *,
    execution_id: str,
    policy_id: int,
    subscription_id: str | None,
    status: str = "running",
    binding_id: int | None = None,
    mode: str = "pull",
    event_id: str | None = None,
) -> None:
    """Open a policy execution (defaults to ``running``), mirroring ``create_run``.

    ``binding_id`` tags an execution triggered by a binding run (M5.3); ``None`` for
    plain pull-mode runs. ``mode`` is ``pull`` (scheduled/manual) or ``event`` (a
    reactive run triggered by an Event Grid delivery, M6.2). ``event_id`` records the
    triggering delivery (M6.4) so the status feed can link an event to its runs.
    """
    session.add(
        schema.PolicyExecution(
            execution_id=execution_id,
            policy_id=policy_id,
            subscription_id=subscription_id,
            status=status,
            binding_id=binding_id,
            mode=mode,
            event_id=event_id,
        )
    )
    session.flush()


def finish_policy_execution(
    session: Session,
    execution_id: str,
    *,
    status: str,
    resources_matched: int = 0,
    actions_taken: list[Any] | None = None,
    error: str | None = None,
) -> None:
    """Close out an execution (status/timestamp/counts). No-op for an unknown id."""
    rec = session.get(schema.PolicyExecution, execution_id)
    if rec is None:
        return
    rec.status = status
    rec.finished_at = datetime.now(UTC)
    rec.resources_matched = resources_matched
    rec.actions_taken = actions_taken if actions_taken is not None else []
    rec.error = error


def insert_policy_matches(session: Session, execution_id: str, matches: list[m.PolicyMatch]) -> int:
    """Persist per-resource matches for an execution (plain inserts). Returns the count."""
    if not matches:
        return 0
    session.add_all(
        schema.PolicyMatch(
            execution_id=execution_id,
            resource_id=match.resource_id,
            resource_type=match.resource_type,
            action_taken=match.action_taken,
            action_result=match.action_result,
        )
        for match in matches
    )
    session.flush()
    return len(matches)


def get_policy_execution(session: Session, execution_id: str) -> dict[str, Any] | None:
    rec = session.get(schema.PolicyExecution, execution_id)
    return _policy_execution_public(rec) if rec is not None else None


def list_policy_executions(
    session: Session,
    *,
    policy_id: int | None = None,
    subscription_id: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """List executions newest-first, filtered by any combination of the given args."""
    query = session.query(schema.PolicyExecution)
    if policy_id is not None:
        query = query.filter(schema.PolicyExecution.policy_id == policy_id)
    if subscription_id is not None:
        query = query.filter(schema.PolicyExecution.subscription_id == subscription_id)
    if status is not None:
        query = query.filter(schema.PolicyExecution.status == status)
    recs = query.order_by(schema.PolicyExecution.started_at.desc()).limit(limit).all()
    return [_policy_execution_public(r) for r in recs]


def list_policy_matches(
    session: Session, execution_id: str, limit: int = 500
) -> list[dict[str, Any]]:
    """List an execution's matches newest-first (``id`` breaks same-timestamp ties)."""
    recs = (
        session.query(schema.PolicyMatch)
        .filter_by(execution_id=execution_id)
        .order_by(schema.PolicyMatch.matched_at.desc(), schema.PolicyMatch.id.desc())
        .limit(limit)
        .all()
    )
    return [_policy_match_public(r) for r in recs]


# --------------------------------------------------------------------------- #
# Subscriptions (multi-subscription management)
# --------------------------------------------------------------------------- #
def _subscription_public(rec: schema.Subscription) -> dict[str, Any]:
    """Serialize a subscription WITHOUT its secret (secrets never leave the DB)."""
    return {
        "subscription_id": rec.subscription_id,
        "display_name": rec.display_name,
        "tenant_id": rec.tenant_id,
        "client_id": rec.client_id,
        "has_credentials": bool(rec.client_id and rec.client_secret),
        "enabled": rec.enabled,
        "is_default": rec.is_default,
        "created_at": rec.created_at.isoformat() if rec.created_at else None,
        "updated_at": rec.updated_at.isoformat() if rec.updated_at else None,
    }


def list_subscriptions(session: Session) -> list[dict[str, Any]]:
    recs = (
        session.query(schema.Subscription)
        .order_by(schema.Subscription.is_default.desc(), schema.Subscription.display_name.asc())
        .all()
    )
    return [_subscription_public(r) for r in recs]


def get_subscription(session: Session, subscription_id: str) -> schema.Subscription | None:
    """Internal: returns the ORM record (including the secret) for credential use."""
    return session.get(schema.Subscription, subscription_id)


def enabled_subscriptions(session: Session) -> list[schema.Subscription]:
    return (
        session.query(schema.Subscription)
        .filter(schema.Subscription.enabled.is_(True))
        .order_by(schema.Subscription.is_default.desc(), schema.Subscription.display_name.asc())
        .all()
    )


def upsert_subscription(
    session: Session,
    *,
    subscription_id: str,
    display_name: str,
    tenant_id: str | None = None,
    client_id: str | None = None,
    client_secret: str | None = None,
    enabled: bool = True,
) -> dict[str, Any]:
    """Create or update a subscription.

    Secret semantics on update: ``client_secret=None`` keeps the existing secret,
    ``client_secret=""`` clears it, any other value sets it.
    """
    rec = session.get(schema.Subscription, subscription_id)
    make_default = session.query(schema.Subscription).count() == 0
    if rec is None:
        rec = schema.Subscription(subscription_id=subscription_id, is_default=make_default)
        session.add(rec)
    rec.display_name = display_name
    rec.tenant_id = tenant_id or None
    rec.client_id = client_id or None
    if client_secret is not None:
        rec.client_secret = client_secret or None
    rec.enabled = enabled
    session.flush()
    return _subscription_public(rec)


def delete_subscription(session: Session, subscription_id: str) -> bool:
    rec = session.get(schema.Subscription, subscription_id)
    if rec is None:
        return False
    was_default = rec.is_default
    session.delete(rec)
    session.flush()
    if was_default:
        nxt = session.query(schema.Subscription).first()
        if nxt is not None:
            nxt.is_default = True
    return True


def set_default_subscription(session: Session, subscription_id: str) -> bool:
    rec = session.get(schema.Subscription, subscription_id)
    if rec is None:
        return False
    session.query(schema.Subscription).update({schema.Subscription.is_default: False})
    rec.is_default = True
    session.flush()
    return True


def ensure_default_subscription(session: Session, settings: Any) -> None:
    """Seed the subscriptions table from the env subscription if it is empty."""
    if session.query(schema.Subscription).count() > 0:
        return
    sub_id = settings.azure_subscription_id
    session.add(
        schema.Subscription(
            subscription_id=sub_id,
            display_name=f"Default ({sub_id[:8]}…)" if len(sub_id) > 8 else sub_id,
            tenant_id=settings.azure_tenant_id,
            enabled=True,
            is_default=True,
        )
    )
    session.flush()


# --------------------------------------------------------------------------- #
# Policies (governance-as-code CRUD)
# --------------------------------------------------------------------------- #
def _policy_public(rec: schema.Policy) -> dict[str, Any]:
    """Serialize a policy row into a JSON-friendly dict (timestamps as ISO-8601)."""
    return {
        "id": rec.id,
        "name": rec.name,
        "resource_type": rec.resource_type,
        "spec": rec.spec,
        "description": rec.description,
        "enabled": rec.enabled,
        "version": rec.version,
        "source": rec.source,
        "created_at": rec.created_at.isoformat() if rec.created_at else None,
        "updated_at": rec.updated_at.isoformat() if rec.updated_at else None,
    }


# Authored fields captured in each version snapshot and compared to detect a
# real (content-changing) update. ``enabled``/``source`` are lifecycle state, not
# policy content, so they neither snapshot nor diff.
_VERSIONED_FIELDS = ("name", "resource_type", "spec", "description")


def _policy_version_public(rec: schema.PolicyVersion) -> dict[str, Any]:
    """Serialize a policy-version snapshot into a JSON-friendly dict."""
    return {
        "policy_id": rec.policy_id,
        "version": rec.version,
        "name": rec.name,
        "resource_type": rec.resource_type,
        "spec": rec.spec,
        "description": rec.description,
        "actor": rec.actor,
        "created_at": rec.created_at.isoformat() if rec.created_at else None,
    }


def _snapshot_policy_version(
    session: Session, rec: schema.Policy, actor: str | None = None
) -> None:
    """Append an immutable snapshot of ``rec`` at its current ``version``."""
    session.add(
        schema.PolicyVersion(
            policy_id=rec.id,
            version=rec.version,
            name=rec.name,
            resource_type=rec.resource_type,
            spec=rec.spec,
            description=rec.description,
            actor=actor,
        )
    )
    session.flush()


def create_policy(
    session: Session,
    *,
    name: str,
    resource_type: str,
    spec: dict[str, Any],
    description: str | None = None,
    source: str = "custom",
    actor: str | None = None,
) -> dict[str, Any]:
    """Persist a new policy (enabled, version 1). Raises on a duplicate ``name``.

    Seeds the version history with a version-1 snapshot so the created state is
    always the first entry in the audit trail.
    """
    rec = schema.Policy(
        name=name,
        resource_type=resource_type,
        spec=spec,
        description=description,
        source=source,
    )
    session.add(rec)
    session.flush()
    _snapshot_policy_version(session, rec, actor=actor)
    return _policy_public(rec)


def get_policy(session: Session, policy_id: int) -> dict[str, Any] | None:
    rec = session.get(schema.Policy, policy_id)
    return _policy_public(rec) if rec is not None else None


def list_policies(session: Session, enabled_only: bool = False) -> list[dict[str, Any]]:
    query = session.query(schema.Policy)
    if enabled_only:
        query = query.filter(schema.Policy.enabled.is_(True))
    recs = query.order_by(schema.Policy.name.asc()).all()
    return [_policy_public(r) for r in recs]


def update_policy(
    session: Session,
    policy_id: int,
    *,
    name: str | None = None,
    resource_type: str | None = None,
    spec: dict[str, Any] | None = None,
    description: str | None = None,
    actor: str | None = None,
) -> dict[str, Any] | None:
    """Apply the supplied fields; bump ``version`` and snapshot only on a real change.

    Only fields whose new value differs from the stored one are applied. When at
    least one authored field changes, ``version`` increments and a new
    :class:`~schema.PolicyVersion` snapshot of the resulting state is appended. A
    no-op update (nothing supplied, or every value already equal) leaves the row
    and its history untouched. Returns ``None`` if the policy is missing.
    """
    rec = session.get(schema.Policy, policy_id)
    if rec is None:
        return None
    changed = False
    if name is not None and name != rec.name:
        rec.name = name
        changed = True
    if resource_type is not None and resource_type != rec.resource_type:
        rec.resource_type = resource_type
        changed = True
    if spec is not None and spec != rec.spec:
        rec.spec = spec
        changed = True
    if description is not None and description != rec.description:
        rec.description = description
        changed = True
    if changed:
        rec.version += 1
        session.flush()
        _snapshot_policy_version(session, rec, actor=actor)
    return _policy_public(rec)


def list_versions(session: Session, policy_id: int) -> list[dict[str, Any]] | None:
    """Return a policy's version snapshots newest-first, or ``None`` if it's missing."""
    if session.get(schema.Policy, policy_id) is None:
        return None
    recs = (
        session.query(schema.PolicyVersion)
        .filter(schema.PolicyVersion.policy_id == policy_id)
        .order_by(schema.PolicyVersion.version.desc())
        .all()
    )
    return [_policy_version_public(r) for r in recs]


def diff_versions(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """Field-level diff of two version snapshots (pure — no DB).

    Returns ``{"changed_fields": [...sorted...], "changes": {field: {old, new}}}``
    over the authored fields (name/resource_type/spec/description).
    """
    changes: dict[str, Any] = {}
    for field in _VERSIONED_FIELDS:
        if old.get(field) != new.get(field):
            changes[field] = {"old": old.get(field), "new": new.get(field)}
    return {"changed_fields": sorted(changes), "changes": changes}


def diff_policy_versions(
    session: Session, policy_id: int, from_version: int, to_version: int
) -> dict[str, Any] | None:
    """Diff two stored versions of a policy. ``None`` if the policy or a version is missing."""
    if session.get(schema.Policy, policy_id) is None:
        return None
    snaps = {
        v.version: v
        for v in session.query(schema.PolicyVersion)
        .filter(
            schema.PolicyVersion.policy_id == policy_id,
            schema.PolicyVersion.version.in_((from_version, to_version)),
        )
        .all()
    }
    if from_version not in snaps or to_version not in snaps:
        return None
    diff = diff_versions(
        _policy_version_public(snaps[from_version]),
        _policy_version_public(snaps[to_version]),
    )
    return {"from_version": from_version, "to_version": to_version, **diff}


def delete_policy(session: Session, policy_id: int) -> bool:
    rec = session.get(schema.Policy, policy_id)
    if rec is None:
        return False
    session.delete(rec)
    session.flush()
    return True


def set_policy_enabled(session: Session, policy_id: int, enabled: bool) -> dict[str, Any] | None:
    """Toggle the ``enabled`` flag. Returns ``None`` if the policy is missing."""
    rec = session.get(schema.Policy, policy_id)
    if rec is None:
        return None
    rec.enabled = enabled
    session.flush()
    return _policy_public(rec)


def upsert_policy_by_name(
    session: Session,
    *,
    name: str,
    resource_type: str,
    spec: dict[str, Any],
    description: str | None = None,
    source: str = "gitops",
) -> str:
    """Insert or update a policy keyed by ``name`` (used by GitOps sync).

    Returns ``"added"``, ``"updated"``, or ``"unchanged"``. When the incoming
    fields are identical to the stored row nothing is written (idempotent), so a
    no-op re-sync never bumps ``version``.
    """
    existing = session.query(schema.Policy).filter(schema.Policy.name == name).one_or_none()
    if existing is None:
        session.add(
            schema.Policy(
                name=name,
                resource_type=resource_type,
                spec=spec,
                description=description,
                source=source,
            )
        )
        session.flush()
        return "added"
    if (
        existing.spec == spec
        and existing.resource_type == resource_type
        and existing.description == description
        and existing.source == source
    ):
        return "unchanged"
    existing.resource_type = resource_type
    existing.spec = spec
    existing.description = description
    existing.source = source
    existing.version += 1
    session.flush()
    return "updated"


# --------------------------------------------------------------------------- #
# Policy collections (many-to-many grouping)
# --------------------------------------------------------------------------- #
def _collection_public(session: Session, rec: schema.PolicyCollection) -> dict[str, Any]:
    """Serialize a collection with its member policies (id/name/type/enabled)."""
    members = (
        session.query(schema.Policy)
        .join(schema.CollectionPolicy, schema.CollectionPolicy.policy_id == schema.Policy.id)
        .filter(schema.CollectionPolicy.collection_id == rec.id)
        .order_by(schema.Policy.name.asc())
        .all()
    )
    return {
        "id": rec.id,
        "name": rec.name,
        "description": rec.description,
        "policy_count": len(members),
        "policies": [
            {"id": p.id, "name": p.name, "resource_type": p.resource_type, "enabled": p.enabled}
            for p in members
        ],
        "created_at": rec.created_at.isoformat() if rec.created_at else None,
        "updated_at": rec.updated_at.isoformat() if rec.updated_at else None,
    }


def create_collection(
    session: Session, *, name: str, description: str | None = None
) -> dict[str, Any]:
    """Persist a new collection. Raises on a duplicate ``name``."""
    rec = schema.PolicyCollection(name=name, description=description)
    session.add(rec)
    session.flush()
    return _collection_public(session, rec)


def get_collection(session: Session, collection_id: int) -> dict[str, Any] | None:
    rec = session.get(schema.PolicyCollection, collection_id)
    return _collection_public(session, rec) if rec is not None else None


def list_collections(session: Session) -> list[dict[str, Any]]:
    recs = session.query(schema.PolicyCollection).order_by(schema.PolicyCollection.name.asc()).all()
    return [_collection_public(session, r) for r in recs]


def delete_collection(session: Session, collection_id: int) -> bool:
    """Delete a collection and its memberships — never the member policies."""
    rec = session.get(schema.PolicyCollection, collection_id)
    if rec is None:
        return False
    session.execute(
        delete(schema.CollectionPolicy).where(
            schema.CollectionPolicy.collection_id == collection_id
        )
    )
    session.delete(rec)
    session.flush()
    return True


def add_policy_to_collection(
    session: Session, collection_id: int, policy_id: int
) -> dict[str, Any] | None:
    """Add a policy to a collection (idempotent). ``None`` if either doesn't exist."""
    collection = session.get(schema.PolicyCollection, collection_id)
    if collection is None:
        return None
    if session.get(schema.Policy, policy_id) is None:
        return None
    if session.get(schema.CollectionPolicy, (collection_id, policy_id)) is None:
        session.add(schema.CollectionPolicy(collection_id=collection_id, policy_id=policy_id))
        session.flush()
    return _collection_public(session, collection)


def remove_policy_from_collection(
    session: Session, collection_id: int, policy_id: int
) -> dict[str, Any] | None:
    """Remove a membership. ``None`` if the collection or membership is absent."""
    collection = session.get(schema.PolicyCollection, collection_id)
    if collection is None:
        return None
    link = session.get(schema.CollectionPolicy, (collection_id, policy_id))
    if link is None:
        return None
    session.delete(link)
    session.flush()
    return _collection_public(session, collection)


# --------------------------------------------------------------------------- #
# Account groups (M5.1) — many-to-many grouping of subscriptions
# --------------------------------------------------------------------------- #
def _account_group_public(session: Session, rec: schema.AccountGroup) -> dict[str, Any]:
    """Serialize a group with its member subscriptions (id/name/enabled)."""
    members = (
        session.query(schema.Subscription)
        .join(
            schema.AccountGroupMember,
            schema.AccountGroupMember.subscription_id == schema.Subscription.subscription_id,
        )
        .filter(schema.AccountGroupMember.group_id == rec.id)
        .order_by(schema.Subscription.display_name.asc())
        .all()
    )
    return {
        "id": rec.id,
        "name": rec.name,
        "description": rec.description,
        "subscription_count": len(members),
        "subscriptions": [
            {
                "subscription_id": s.subscription_id,
                "display_name": s.display_name,
                "enabled": s.enabled,
            }
            for s in members
        ],
        "created_at": rec.created_at.isoformat() if rec.created_at else None,
        "updated_at": rec.updated_at.isoformat() if rec.updated_at else None,
    }


def create_account_group(
    session: Session, *, name: str, description: str | None = None
) -> dict[str, Any]:
    """Persist a new account group. Raises on a duplicate ``name``."""
    rec = schema.AccountGroup(name=name, description=description)
    session.add(rec)
    session.flush()
    return _account_group_public(session, rec)


def get_account_group(session: Session, group_id: int) -> dict[str, Any] | None:
    rec = session.get(schema.AccountGroup, group_id)
    return _account_group_public(session, rec) if rec is not None else None


def list_account_groups(session: Session) -> list[dict[str, Any]]:
    recs = session.query(schema.AccountGroup).order_by(schema.AccountGroup.name.asc()).all()
    return [_account_group_public(session, r) for r in recs]


def delete_account_group(session: Session, group_id: int) -> bool:
    """Delete a group and its memberships — never the member subscriptions."""
    rec = session.get(schema.AccountGroup, group_id)
    if rec is None:
        return False
    session.execute(
        delete(schema.AccountGroupMember).where(schema.AccountGroupMember.group_id == group_id)
    )
    session.delete(rec)
    session.flush()
    return True


def add_subscription_to_group(
    session: Session, group_id: int, subscription_id: str
) -> dict[str, Any] | None:
    """Add a subscription to a group (idempotent). ``None`` if either doesn't exist."""
    group = session.get(schema.AccountGroup, group_id)
    if group is None:
        return None
    if session.get(schema.Subscription, subscription_id) is None:
        return None
    if session.get(schema.AccountGroupMember, (group_id, subscription_id)) is None:
        session.add(schema.AccountGroupMember(group_id=group_id, subscription_id=subscription_id))
        session.flush()
    return _account_group_public(session, group)


def remove_subscription_from_group(
    session: Session, group_id: int, subscription_id: str
) -> dict[str, Any] | None:
    """Remove a membership. ``None`` if the group or membership is absent."""
    group = session.get(schema.AccountGroup, group_id)
    if group is None:
        return None
    link = session.get(schema.AccountGroupMember, (group_id, subscription_id))
    if link is None:
        return None
    session.delete(link)
    session.flush()
    return _account_group_public(session, group)


# --------------------------------------------------------------------------- #
# Bindings (M5.2) — link a policy collection to an account group + exec config
# --------------------------------------------------------------------------- #
_BINDING_MODES = {"pull", "event"}


def _binding_public(rec: schema.Binding) -> dict[str, Any]:
    return {
        "id": rec.id,
        "collection_id": rec.collection_id,
        "account_group_id": rec.account_group_id,
        "schedule": rec.schedule,
        "mode": rec.mode,
        "dry_run": rec.dry_run,
        "enabled": rec.enabled,
        "created_at": rec.created_at.isoformat() if rec.created_at else None,
        "updated_at": rec.updated_at.isoformat() if rec.updated_at else None,
    }


def _validate_mode(mode: str) -> None:
    if mode not in _BINDING_MODES:
        raise ValueError(f"mode must be one of {sorted(_BINDING_MODES)}")


def create_binding(
    session: Session,
    *,
    collection_id: int,
    account_group_id: int,
    schedule: str | None = None,
    mode: str = "pull",
    dry_run: bool = True,
    enabled: bool = True,
) -> dict[str, Any] | None:
    """Create a binding. Raises ``ValueError`` for a bad ``mode``; returns ``None`` if
    the referenced collection or account group does not exist."""
    _validate_mode(mode)
    if session.get(schema.PolicyCollection, collection_id) is None:
        return None
    if session.get(schema.AccountGroup, account_group_id) is None:
        return None
    rec = schema.Binding(
        collection_id=collection_id,
        account_group_id=account_group_id,
        schedule=schedule,
        mode=mode,
        dry_run=dry_run,
        enabled=enabled,
    )
    session.add(rec)
    session.flush()
    return _binding_public(rec)


def get_binding(session: Session, binding_id: int) -> dict[str, Any] | None:
    rec = session.get(schema.Binding, binding_id)
    return _binding_public(rec) if rec is not None else None


def list_bindings(session: Session) -> list[dict[str, Any]]:
    recs = session.query(schema.Binding).order_by(schema.Binding.id.asc()).all()
    return [_binding_public(r) for r in recs]


def update_binding(
    session: Session, binding_id: int, changes: dict[str, Any]
) -> dict[str, Any] | None:
    """Partial update (only the given fields). Raises ``ValueError`` for a bad ``mode``;
    returns ``None`` if the binding does not exist."""
    if "mode" in changes:
        _validate_mode(changes["mode"])
    rec = session.get(schema.Binding, binding_id)
    if rec is None:
        return None
    for field in ("schedule", "mode", "dry_run", "enabled"):
        if field in changes:
            setattr(rec, field, changes[field])
    session.flush()
    return _binding_public(rec)


def delete_binding(session: Session, binding_id: int) -> bool:
    rec = session.get(schema.Binding, binding_id)
    if rec is None:
        return False
    session.delete(rec)
    session.flush()
    return True


def policies_in_collection(
    session: Session, collection_id: int, enabled_only: bool = True
) -> list[dict[str, Any]]:
    """Full policy records (with ``spec``) belonging to a collection — for M5.3 runs."""
    query = (
        session.query(schema.Policy)
        .join(schema.CollectionPolicy, schema.CollectionPolicy.policy_id == schema.Policy.id)
        .filter(schema.CollectionPolicy.collection_id == collection_id)
    )
    if enabled_only:
        query = query.filter(schema.Policy.enabled.is_(True))
    return [_policy_public(p) for p in query.order_by(schema.Policy.name.asc()).all()]


def subscriptions_in_group(
    session: Session, group_id: int, enabled_only: bool = True
) -> list[schema.Subscription]:
    """Subscription ORM rows belonging to an account group — for M5.3 runs."""
    query = (
        session.query(schema.Subscription)
        .join(
            schema.AccountGroupMember,
            schema.AccountGroupMember.subscription_id == schema.Subscription.subscription_id,
        )
        .filter(schema.AccountGroupMember.group_id == group_id)
    )
    if enabled_only:
        query = query.filter(schema.Subscription.enabled.is_(True))
    return query.order_by(schema.Subscription.display_name.asc()).all()


# --------------------------------------------------------------------------- #
# Event Grid deliveries (M6.1)
# --------------------------------------------------------------------------- #
def insert_event_log(session: Session, event: Any, status: str = "received") -> int:
    """Append one normalized Event Grid delivery to ``event_log`` (idempotent on
    ``event_id``). Returns 1 if inserted, 0 if it was a re-delivery (already logged)."""
    stmt = (
        pg_insert(schema.EventLog)
        .values(
            event_id=event.event_id,
            event_type=event.event_type,
            subject=event.subject,
            resource_id=event.resource_id,
            subscription_id=event.subscription_id,
            event_time=event.event_time,
            status=status,
            raw=event.raw,
        )
        .on_conflict_do_nothing(index_elements=["event_id"])
        .returning(schema.EventLog.id)
    )
    inserted = session.execute(stmt).fetchall()
    session.flush()
    return len(inserted)


def list_events(session: Session, limit: int = 50) -> list[dict[str, Any]]:
    """Recent Event Grid deliveries, newest-first (``id`` breaks same-instant ties)."""
    return _rows(
        session,
        "SELECT id, event_id, event_type, subject, resource_id, subscription_id, "
        "event_time, received_at, status, raw "
        "FROM event_log ORDER BY received_at DESC, id DESC LIMIT :limit",
        limit=limit,
    )


def recent_events(session: Session, *, limit: int = 20, offset: int = 0) -> list[dict[str, Any]]:
    """Recent deliveries (newest-first, paginated) each with the executions it triggered.

    The status feed (M6.4): a page of ``event_log`` rows, then one grouped lookup of the
    ``policy_executions`` reactively triggered by those deliveries (``event_id`` join,
    M6.2/M6.4) — so each event carries a ``triggered_executions`` list without an N+1.
    """
    events = _rows(
        session,
        "SELECT event_id, event_type, subject, resource_id, subscription_id, "
        "event_time, received_at, status "
        "FROM event_log ORDER BY received_at DESC, id DESC LIMIT :limit OFFSET :offset",
        limit=limit,
        offset=offset,
    )
    event_ids = [e["event_id"] for e in events]
    by_event: dict[str, list[dict[str, Any]]] = {eid: [] for eid in event_ids}
    if event_ids:
        recs = (
            session.query(schema.PolicyExecution)
            .filter(schema.PolicyExecution.event_id.in_(event_ids))
            .order_by(schema.PolicyExecution.started_at.asc())
            .all()
        )
        for rec in recs:
            by_event[rec.event_id].append(
                {
                    "execution_id": rec.execution_id,
                    "policy_id": rec.policy_id,
                    "status": rec.status,
                    "mode": rec.mode,
                    "started_at": rec.started_at.isoformat() if rec.started_at else None,
                }
            )
    for event in events:
        event["triggered_executions"] = by_event.get(event["event_id"], [])
    return events


# --------------------------------------------------------------------------- #
# Inventory + cost
# --------------------------------------------------------------------------- #
def upsert_resources(session: Session, resources: list[m.ResourceRecord]) -> int:
    if not resources:
        return 0
    now = datetime.now(UTC)
    dedup: dict[str, m.ResourceRecord] = {r.resource_id: r for r in resources}
    rows = [
        {
            "resource_id": r.resource_id,
            "subscription_id": r.subscription_id,
            "resource_group": r.resource_group,
            "name": r.name,
            "type": r.type,
            "location": r.location,
            "sku": r.sku,
            "tags": r.tags,
            "power_state": r.power_state,
            "last_seen": now,
        }
        for r in dedup.values()
    ]
    stmt = pg_insert(schema.Resource).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["resource_id"],
        set_={
            "subscription_id": stmt.excluded.subscription_id,
            "resource_group": stmt.excluded.resource_group,
            "name": stmt.excluded.name,
            "type": stmt.excluded.type,
            "location": stmt.excluded.location,
            "sku": stmt.excluded.sku,
            "tags": stmt.excluded.tags,
            "power_state": stmt.excluded.power_state,
            "last_seen": stmt.excluded.last_seen,
        },
    )
    session.execute(stmt)
    return len(rows)


# --------------------------------------------------------------------------- #
# AssetDB (M4.1) — queryable inventory with full config + a change audit trail
# --------------------------------------------------------------------------- #
def upsert_assets(session: Session, resources: list[m.ResourceRecord]) -> list[str]:
    """Idempotently upsert assets; return the resource_ids **newly inserted** this call.

    ``first_seen`` is stamped once (on insert); ``last_seen`` / ``config`` / ``state``
    and the descriptive columns refresh on every re-ingestion. The Postgres
    ``xmax = 0`` trick distinguishes a freshly-inserted row from an updated one, so
    the caller can record a ``created`` :func:`append_asset_event` only on first sight.
    """
    if not resources:
        return []
    now = datetime.now(UTC)
    dedup: dict[str, m.ResourceRecord] = {r.resource_id: r for r in resources}
    rows = [
        {
            "resource_id": r.resource_id,
            "subscription_id": r.subscription_id,
            "resource_group": r.resource_group,
            "name": r.name,
            "type": r.type,
            "location": r.location,
            "sku": r.sku,
            "tags": r.tags,
            "config": r.config,
            "state": r.power_state,
            "last_seen": now,
        }
        for r in dedup.values()
    ]
    stmt = pg_insert(schema.Asset).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["resource_id"],
        set_={
            "subscription_id": stmt.excluded.subscription_id,
            "resource_group": stmt.excluded.resource_group,
            "name": stmt.excluded.name,
            "type": stmt.excluded.type,
            "location": stmt.excluded.location,
            "sku": stmt.excluded.sku,
            "tags": stmt.excluded.tags,
            "config": stmt.excluded.config,
            "state": stmt.excluded.state,
            "last_seen": stmt.excluded.last_seen,
        },
    ).returning(schema.Asset.resource_id, literal_column("(xmax = 0)").label("inserted"))
    result = session.execute(stmt)
    return [row.resource_id for row in result if row.inserted]


def append_asset_event(
    session: Session,
    *,
    resource_id: str,
    subscription_id: str | None,
    event_type: str,
    data: dict[str, Any] | None = None,
) -> None:
    """Append one asset lifecycle event to the audit trail."""
    session.add(
        schema.AssetEvent(
            resource_id=resource_id,
            subscription_id=subscription_id,
            event_type=event_type,
            data=data if data is not None else {},
        )
    )
    session.flush()


_DELETE_EVENT_TYPE = "Microsoft.Resources.ResourceDeleteSuccess"


def upsert_asset_from_event(session: Session, event: Any) -> bool:
    """Reflect a single resource-change event into the ``assets`` inventory (M6.3).

    Keeps the AssetDB current in near-real-time: the row is upserted on
    ``resource_id`` with the identity the event carries (subscription, ARM ``type``)
    and a refreshed ``last_seen``; a delete event marks ``state='deleted'``. Only the
    columns the event actually knows are updated on conflict — a prior full ingestion's
    ``config``/``tags``/``name``/``location`` are **preserved**, never clobbered. On a
    first-seen insert the JSONB columns are seeded ``{}`` (the ORM ``default=dict`` does
    not apply to a Core insert). Returns ``True`` iff a new row was inserted
    (``xmax = 0``), so the caller can log ``created`` vs ``updated``.
    """
    is_delete = event.event_type == _DELETE_EVENT_TYPE
    stmt = pg_insert(schema.Asset).values(
        resource_id=event.resource_id,
        subscription_id=event.subscription_id,
        type=event.resource_type,
        state="deleted" if is_delete else "active",
        tags={},
        config={},
        last_seen=datetime.now(UTC),
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["resource_id"],
        set_={
            "subscription_id": stmt.excluded.subscription_id,
            "type": stmt.excluded.type,
            "state": stmt.excluded.state,
            "last_seen": stmt.excluded.last_seen,
        },
    ).returning(literal_column("(xmax = 0)").label("inserted"))
    return bool(session.execute(stmt).scalar_one())


# Allow-listed asset columns for the query builder (M4.2). Only these may be
# filtered on; anything else is rejected before a query is ever built/executed.
_ALLOWED_ASSET_COLUMNS = {
    "resource_id": schema.Asset.resource_id,
    "subscription_id": schema.Asset.subscription_id,
    "resource_group": schema.Asset.resource_group,
    "name": schema.Asset.name,
    "type": schema.Asset.type,
    "location": schema.Asset.location,
    "sku": schema.Asset.sku,
    "state": schema.Asset.state,
}
_MAX_ASSET_LIMIT = 500


def _asset_public(rec: schema.Asset) -> dict[str, Any]:
    return {
        "resource_id": rec.resource_id,
        "subscription_id": rec.subscription_id,
        "resource_group": rec.resource_group,
        "name": rec.name,
        "type": rec.type,
        "location": rec.location,
        "sku": rec.sku,
        "tags": rec.tags,
        "config": rec.config,
        "state": rec.state,
        "first_seen": rec.first_seen.isoformat() if rec.first_seen else None,
        "last_seen": rec.last_seen.isoformat() if rec.last_seen else None,
    }


def _asset_filter_clause(column: Any, op: str, value: Any) -> Any:
    """Build a parameterized filter clause. Raises ``ValueError`` for a bad operator.

    ``value`` is always bound as a parameter by SQLAlchemy (never interpolated), so
    an injection payload is a harmless literal.
    """
    if op == "eq":
        return column == value
    if op == "ne":
        return column != value
    if op == "contains":
        return column.ilike(f"%{value}%")
    if op == "in":
        if not isinstance(value, list):
            raise ValueError("operator 'in' requires a list value")
        return column.in_(value)
    raise ValueError(f"unknown operator: {op}")


def query_assets(session: Session, query: m.AssetQuery) -> list[dict[str, Any]]:
    """Filter assets via an allow-listed, fully-parameterized builder (M4.2).

    Only allow-listed columns/operators are honored (an unknown one raises
    ``ValueError`` → HTTP 400 at the API); every value — including tag keys/values —
    is bound as a parameter, so a SQL-injection string is a harmless literal.
    ``limit`` is clamped to ``_MAX_ASSET_LIMIT`` and rows come back in a stable order.
    """
    q = session.query(schema.Asset)
    for f in query.filters:
        column = _ALLOWED_ASSET_COLUMNS.get(f.column)
        if column is None:
            raise ValueError(f"unknown filter column: {f.column}")
        q = q.filter(_asset_filter_clause(column, f.op, f.value))
    for key, value in query.tags.items():
        q = q.filter(schema.Asset.tags[key].astext == value)
    limit = min(max(query.limit, 1), _MAX_ASSET_LIMIT)
    offset = max(query.offset, 0)
    recs = (
        q.order_by(schema.Asset.last_seen.desc(), schema.Asset.resource_id.asc())
        .limit(limit)
        .offset(offset)
        .all()
    )
    return [_asset_public(r) for r in recs]


# --------------------------------------------------------------------------- #
# Asset relationships (M4.3) — the graph dimension of AssetDB
# --------------------------------------------------------------------------- #
def _nic_from_ipconfig(ref: str) -> str:
    """Reduce a NIC ipConfiguration id to its parent NIC resource id.

    ``…/networkInterfaces/nic-1/ipConfigurations/ipconfig1`` → ``…/networkInterfaces/nic-1``.
    A reference without that marker is returned unchanged (it simply won't resolve).
    """
    marker = "/ipconfigurations/"
    idx = ref.lower().find(marker)
    return ref[:idx] if idx != -1 else ref


# Source asset type → list of (config path to a referenced resource id, edge kind,
# optional normalizer). Only these reference shapes become edges.
_RELATIONSHIP_RULES: dict[str, list[tuple[tuple[str, ...], str, Any]]] = {
    "microsoft.compute/disks": [(("managedBy",), "attached-to", None)],
    "microsoft.network/networkinterfaces": [(("virtualMachine", "id"), "attached-to", None)],
    "microsoft.network/publicipaddresses": [
        (("ipConfiguration", "id"), "bound-to", _nic_from_ipconfig)
    ],
}


def _dig(config: Any, path: tuple[str, ...]) -> Any:
    """Walk a nested-dict ``path``; return ``None`` if any hop is missing/not a dict."""
    node = config
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def _edges_for_asset(asset_type: str | None, config: Any) -> list[tuple[str, str]]:
    """Return ``(referenced_target_id, kind)`` candidates declared in an asset's config."""
    edges: list[tuple[str, str]] = []
    for path, kind, normalize in _RELATIONSHIP_RULES.get((asset_type or "").lower(), []):
        ref = _dig(config, path)
        if isinstance(ref, str) and ref.strip():
            edges.append((normalize(ref) if normalize else ref, kind))
    return edges


def build_relationships(session: Session) -> int:
    """Derive typed edges between stored assets from their config and upsert them.

    Reads every asset's config for known reference fields (a managed disk's
    ``managedBy`` VM, a NIC's ``virtualMachine``, a public IP's bound NIC),
    resolves each reference against the assets already stored — case-insensitively,
    since Azure resource ids are case-insensitive — and writes one
    ``asset_relationships`` edge per resolved reference. A reference to an asset
    that isn't present (a dangling or external reference) is skipped, never fatal.
    Idempotent: the ``(source_id, target_id, kind)`` unique key means re-deriving
    over unchanged inventory inserts nothing. Returns the number of edges inserted.
    """
    assets = session.query(schema.Asset.resource_id, schema.Asset.type, schema.Asset.config).all()
    # canonical (as-stored) id keyed by its lower-cased form, for reference resolution
    canonical = {a.resource_id.lower(): a.resource_id for a in assets}
    edges: dict[tuple[str, str, str], dict[str, str]] = {}
    for a in assets:
        for raw_target, kind in _edges_for_asset(a.type, a.config):
            target = canonical.get(raw_target.lower())
            if target is None:
                continue  # dangling/external reference — skip
            edges[(a.resource_id, target, kind)] = {
                "source_id": a.resource_id,
                "target_id": target,
                "kind": kind,
            }
    rows = list(edges.values())
    if not rows:
        return 0
    stmt = pg_insert(schema.AssetRelationship).values(rows)
    stmt = stmt.on_conflict_do_nothing(index_elements=["source_id", "target_id", "kind"]).returning(
        schema.AssetRelationship.id
    )
    # With DO NOTHING, only rows actually inserted are returned (conflicts skipped),
    # so the RETURNING count is the number of *new* edges — reliable, unlike rowcount.
    inserted = session.execute(stmt).fetchall()
    session.flush()
    return len(inserted)


def get_relationships(session: Session, resource_id: str) -> list[dict[str, Any]]:
    """Return an asset's edges — both outbound (it is source) and inbound (it is
    target) — so a caller sees every neighbour. Each row carries the edge plus its
    ``direction`` and the ``neighbor`` id relative to ``resource_id``. Stable by id.
    """
    recs = (
        session.query(schema.AssetRelationship)
        .filter(
            or_(
                schema.AssetRelationship.source_id == resource_id,
                schema.AssetRelationship.target_id == resource_id,
            )
        )
        .order_by(schema.AssetRelationship.id.asc())
        .all()
    )
    out: list[dict[str, Any]] = []
    for r in recs:
        outbound = r.source_id == resource_id
        out.append(
            {
                "id": r.id,
                "source_id": r.source_id,
                "target_id": r.target_id,
                "kind": r.kind,
                "direction": "outbound" if outbound else "inbound",
                "neighbor": r.target_id if outbound else r.source_id,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
        )
    return out


def _parse_ts(value: Any) -> datetime | None:
    """Parse an ISO-8601 activity timestamp to a tz-aware datetime; None if unparseable."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def record_activity_events(session: Session, events: list[dict[str, Any]]) -> int:
    """Persist parsed Activity Log events (M4.4) into the ``asset_events`` audit trail.

    Each event's real timestamp becomes the row ``at`` — so the history timeline is
    ordered by when the change actually happened, not when we ingested it — while the
    actor, operation and other metadata live in ``data``. Returns the count inserted.
    """
    rows: list[schema.AssetEvent] = []
    for e in events:
        event = schema.AssetEvent(
            resource_id=e["resource_id"],
            subscription_id=e.get("subscription_id"),
            event_type="activity",
            data={
                k: e.get(k) for k in ("actor", "operation", "status", "correlation_id", "timestamp")
            },
        )
        at = _parse_ts(e.get("timestamp"))
        if at is not None:
            event.at = at
        rows.append(event)
    if not rows:
        return 0
    session.add_all(rows)
    session.flush()
    return len(rows)


def get_asset_history(session: Session, resource_id: str) -> list[dict[str, Any]]:
    """Return an asset's change timeline newest-first (M4.4).

    Every ``asset_event`` recorded for the resource — lifecycle (``created``) plus the
    ingested Activity Log (``activity``) — ordered by event time then id (a stable
    tie-break). An unknown asset simply yields an empty list.
    """
    recs = (
        session.query(schema.AssetEvent)
        .filter(schema.AssetEvent.resource_id == resource_id)
        .order_by(schema.AssetEvent.at.desc(), schema.AssetEvent.id.desc())
        .all()
    )
    return [
        {
            "id": r.id,
            "resource_id": r.resource_id,
            "subscription_id": r.subscription_id,
            "event_type": r.event_type,
            "data": r.data,
            "at": r.at.isoformat() if r.at else None,
        }
        for r in recs
    ]


def upsert_cost_snapshots(session: Session, rows: list[m.CostRow]) -> int:
    if not rows:
        return 0
    dedup: dict[tuple, m.CostRow] = {}
    for c in rows:
        key = (
            c.usage_date,
            c.resource_id or "unassigned",
            c.meter_category or "",
            c.cost_type or "Amortized",
        )
        dedup[key] = c
    payload = [
        {
            "usage_date": c.usage_date,
            "resource_id": c.resource_id or "unassigned",
            "meter_category": c.meter_category or "",
            "cost_type": c.cost_type or "Amortized",
            "subscription_id": c.subscription_id,
            "resource_type": c.resource_type,
            "resource_group": c.resource_group,
            "location": c.location,
            "service_name": c.service_name,
            "cost": c.cost,
            "currency": c.currency,
        }
        for c in dedup.values()
    ]
    stmt = pg_insert(schema.CostSnapshot).values(payload)
    stmt = stmt.on_conflict_do_update(
        index_elements=["usage_date", "resource_id", "meter_category", "cost_type"],
        set_={
            "cost": stmt.excluded.cost,
            "currency": stmt.excluded.currency,
            "subscription_id": stmt.excluded.subscription_id,
            "resource_type": stmt.excluded.resource_type,
            "resource_group": stmt.excluded.resource_group,
            "location": stmt.excluded.location,
            "service_name": stmt.excluded.service_name,
        },
    )
    session.execute(stmt)
    return len(payload)


# --------------------------------------------------------------------------- #
# Metrics + rollups
# --------------------------------------------------------------------------- #
def insert_metric_samples(session: Session, samples: list[m.MetricSample]) -> int:
    if not samples:
        return 0
    dedup: dict[tuple, m.MetricSample] = {(s.ts, s.resource_id, s.metric_name): s for s in samples}
    payload = [
        {
            "ts": s.ts,
            "resource_id": s.resource_id,
            "metric_name": s.metric_name,
            "avg": s.avg,
            "min": s.min,
            "max": s.max,
            "unit": s.unit,
            "granularity": s.granularity,
        }
        for s in dedup.values()
    ]
    stmt = pg_insert(schema.UtilizationSample).values(payload)
    stmt = stmt.on_conflict_do_update(
        index_elements=["ts", "resource_id", "metric_name"],
        set_={
            "avg": stmt.excluded.avg,
            "min": stmt.excluded.min,
            "max": stmt.excluded.max,
            "unit": stmt.excluded.unit,
            "granularity": stmt.excluded.granularity,
        },
    )
    session.execute(stmt)
    return len(payload)


def upsert_rollups(session: Session, rollups: list[m.UtilizationRollup]) -> int:
    if not rollups:
        return 0
    dedup: dict[tuple, m.UtilizationRollup] = {(r.resource_id, r.window_end): r for r in rollups}
    payload = [r.model_dump() for r in dedup.values()]
    stmt = pg_insert(schema.UtilizationRollup).values(payload)
    update_cols = {
        c: getattr(stmt.excluded, c) for c in payload[0] if c not in {"resource_id", "window_end"}
    }
    stmt = stmt.on_conflict_do_update(
        index_elements=["resource_id", "window_end"], set_=update_cols
    )
    session.execute(stmt)
    return len(payload)


# --------------------------------------------------------------------------- #
# Advisor
# --------------------------------------------------------------------------- #
def insert_advisor(session: Session, recs: list[dict[str, Any]]) -> int:
    session.execute(delete(schema.AdvisorRecommendation))
    for a in recs:
        session.add(
            schema.AdvisorRecommendation(
                resource_id=a.get("resource_id"),
                category=a.get("category"),
                impact=a.get("impact"),
                problem=a.get("problem"),
                solution=a.get("solution"),
                recommended_sku=a.get("recommended_sku"),
                annual_savings=a.get("annual_savings"),
                extended_properties=a.get("extended_properties") or {},
            )
        )
    session.flush()
    return len(recs)


# --------------------------------------------------------------------------- #
# Recommendations + AI summary
# --------------------------------------------------------------------------- #
def replace_recommendations(session: Session, run_id: str, recs: list[m.Recommendation]) -> int:
    session.execute(delete(schema.Recommendation).where(schema.Recommendation.run_id == run_id))
    for r in recs:
        session.add(
            schema.Recommendation(
                run_id=run_id,
                resource_id=r.resource_id,
                category=r.category,
                action=r.action,
                current_sku=r.current_sku,
                recommended_sku=r.recommended_sku,
                risk=r.risk,
                confidence=r.confidence,
                est_monthly_savings=r.est_monthly_savings,
                currency=r.currency,
                source=r.source,
                priority=r.priority,
                rationale=r.rationale,
                caveats=r.caveats,
                evidence=r.evidence,
                status="open",
            )
        )
    session.flush()
    return len(recs)


def upsert_ai_summary(session: Session, run_id: str, summary: m.AISummary) -> None:
    session.merge(
        schema.AISummary(
            run_id=run_id,
            executive_summary=summary.executive_summary,
            total_potential_savings=summary.total_potential_monthly_savings,
            currency=summary.currency,
            top_actions=[r.model_dump() for r in summary.recommendations[:10]],
            provider=summary.provider,
            model=summary.model,
            input_tokens=summary.input_tokens,
            output_tokens=summary.output_tokens,
        )
    )


# --------------------------------------------------------------------------- #
# Read helpers (used by the API/UI)
# --------------------------------------------------------------------------- #
def _coerce(value: Any) -> Any:
    # JSON-friendly numbers: Numeric columns come back as Decimal.
    return float(value) if isinstance(value, Decimal) else value


def _rows(session: Session, sql: str, **params: Any) -> list[dict[str, Any]]:
    result = session.execute(text(sql), params)
    return [{k: _coerce(v) for k, v in row._mapping.items()} for row in result]


def latest_run(session: Session) -> dict[str, Any] | None:
    rows = _rows(session, "SELECT * FROM runs ORDER BY started_at DESC LIMIT 1")
    return rows[0] if rows else None


def cost_by_type(session: Session) -> list[dict[str, Any]]:
    return _rows(session, "SELECT * FROM v_cost_by_type ORDER BY cost DESC")


def cost_by_region(session: Session) -> list[dict[str, Any]]:
    return _rows(session, "SELECT * FROM v_cost_by_region ORDER BY cost DESC")


def cost_by_resource(session: Session, limit: int = 50) -> list[dict[str, Any]]:
    return _rows(
        session,
        "SELECT * FROM v_cost_by_resource ORDER BY cost DESC LIMIT :limit",
        limit=limit,
    )


def total_cost(session: Session) -> float:
    rows = _rows(session, "SELECT COALESCE(SUM(cost), 0) AS total FROM v_cost_by_type")
    return float(rows[0]["total"]) if rows else 0.0


def latest_recommendations(session: Session, limit: int = 200) -> list[dict[str, Any]]:
    return _rows(
        session,
        "SELECT * FROM v_latest_recommendations ORDER BY priority ASC, est_monthly_savings DESC "
        "LIMIT :limit",
        limit=limit,
    )


def latest_ai_summary(session: Session) -> dict[str, Any] | None:
    rows = _rows(
        session,
        "SELECT s.* FROM ai_summaries s JOIN runs r ON s.run_id = r.run_id "
        "ORDER BY r.started_at DESC LIMIT 1",
    )
    return rows[0] if rows else None


def decide_recommendation(
    session: Session, rec_id: int, status: str, actor: str | None = None
) -> bool:
    rec = session.get(schema.Recommendation, rec_id)
    if rec is None:
        return False
    rec.status = status
    rec.decided_at = datetime.now(UTC)
    rec.decided_by = actor
    return True


def list_runs(session: Session, limit: int = 20) -> list[dict[str, Any]]:
    return _rows(session, "SELECT * FROM runs ORDER BY started_at DESC LIMIT :limit", limit=limit)


def policy_health(session: Session) -> list[dict[str, Any]]:
    """Per-policy compliance & health (M3.4), newest-executed first.

    Reads ``v_policy_health`` — one aggregate row per policy that has executed at
    least once (a never-run policy is absent), across every subscription it ran in.
    """
    return _rows(
        session,
        "SELECT * FROM v_policy_health ORDER BY last_execution_at DESC NULLS LAST, policy_name ASC",
    )


def list_remediation_actions(
    session: Session, limit: int = 100, source: str | None = None
) -> list[dict[str, Any]]:
    """Unified remediation audit (M7.4): recommendation- and policy-sourced actions.

    Surfaces each row's ``source`` (recommendation/policy/binding) and originating
    ``policy_id``; the resource id falls back to the action ``params`` so policy
    actions (which have no recommendation join) still show their target. ``source``,
    when given, filters the trail — the value is bound (injection-safe).
    """
    where = ""
    params: dict[str, Any] = {"limit": limit}
    if source:
        where = "WHERE ra.source = :source "
        params["source"] = source
    return _rows(
        session,
        "SELECT ra.id, ra.action_type, ra.dry_run, ra.status, ra.error, ra.requested_at, "
        "ra.executed_at, ra.actor, ra.source, ra.policy_id, ra.policy_match_id, "
        "COALESCE(r.resource_id, ra.params->>'resource_id') AS resource_id, r.category "
        "FROM remediation_actions ra "
        "LEFT JOIN recommendations r ON ra.recommendation_id = r.id "
        f"{where}"
        "ORDER BY ra.requested_at DESC LIMIT :limit",
        **params,
    )


# --------------------------------------------------------------------------- #
# Notification templates & channels (M8.1)
# --------------------------------------------------------------------------- #
def _notification_template_public(rec: schema.NotificationTemplate) -> dict[str, Any]:
    return {
        "id": rec.id,
        "name": rec.name,
        "subject": rec.subject,
        "body": rec.body,
        "format": rec.format,
        "description": rec.description,
        "created_at": rec.created_at.isoformat() if rec.created_at else None,
        "updated_at": rec.updated_at.isoformat() if rec.updated_at else None,
    }


def create_notification_template(
    session: Session,
    *,
    name: str,
    body: str,
    subject: str | None = None,
    format: str = "text",
    description: str | None = None,
) -> dict[str, Any]:
    """Persist a communication template. Raises on a duplicate ``name``."""
    rec = schema.NotificationTemplate(
        name=name, body=body, subject=subject, format=format, description=description
    )
    session.add(rec)
    session.flush()
    return _notification_template_public(rec)


def get_notification_template(session: Session, template_id: int) -> dict[str, Any] | None:
    rec = session.get(schema.NotificationTemplate, template_id)
    return _notification_template_public(rec) if rec is not None else None


def list_notification_templates(session: Session) -> list[dict[str, Any]]:
    recs = (
        session.query(schema.NotificationTemplate)
        .order_by(schema.NotificationTemplate.name.asc())
        .all()
    )
    return [_notification_template_public(r) for r in recs]


def delete_notification_template(session: Session, template_id: int) -> bool:
    rec = session.get(schema.NotificationTemplate, template_id)
    if rec is None:
        return False
    session.delete(rec)
    session.flush()
    return True


def _notification_channel_public(rec: schema.NotificationChannel) -> dict[str, Any]:
    return {
        "id": rec.id,
        "name": rec.name,
        "transport": rec.transport,
        "target": rec.target,
        "config": rec.config,
        "enabled": rec.enabled,
        "created_at": rec.created_at.isoformat() if rec.created_at else None,
        "updated_at": rec.updated_at.isoformat() if rec.updated_at else None,
    }


def create_notification_channel(
    session: Session,
    *,
    name: str,
    target: str,
    transport: str = "webhook",
    config: dict[str, Any] | None = None,
    enabled: bool = True,
) -> dict[str, Any]:
    """Persist a dispatch channel. Raises on a duplicate ``name``."""
    rec = schema.NotificationChannel(
        name=name,
        target=target,
        transport=transport,
        config=config if config is not None else {},
        enabled=enabled,
    )
    session.add(rec)
    session.flush()
    return _notification_channel_public(rec)


def get_notification_channel(session: Session, channel_id: int) -> dict[str, Any] | None:
    rec = session.get(schema.NotificationChannel, channel_id)
    return _notification_channel_public(rec) if rec is not None else None


def list_notification_channels(session: Session) -> list[dict[str, Any]]:
    recs = (
        session.query(schema.NotificationChannel)
        .order_by(schema.NotificationChannel.name.asc())
        .all()
    )
    return [_notification_channel_public(r) for r in recs]


def update_notification_channel(
    session: Session, channel_id: int, changes: dict[str, Any]
) -> dict[str, Any] | None:
    """Partial update (only the given fields). ``None`` if the channel is missing."""
    rec = session.get(schema.NotificationChannel, channel_id)
    if rec is None:
        return None
    for field in ("name", "transport", "target", "config", "enabled"):
        if field in changes:
            setattr(rec, field, changes[field])
    session.flush()
    return _notification_channel_public(rec)


def delete_notification_channel(session: Session, channel_id: int) -> bool:
    rec = session.get(schema.NotificationChannel, channel_id)
    if rec is None:
        return False
    session.delete(rec)
    session.flush()
    return True
