"""Repository: idempotent writes and read helpers over the ORM schema.

All fact writes use PostgreSQL ``INSERT ... ON CONFLICT DO UPDATE`` and dedupe
within the batch first (Postgres rejects a conflict target hit twice in one
statement), so re-running a collection never creates duplicate rows.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, literal_column, or_, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from .. import models as m
from . import schema

# PostgreSQL binds at most 65535 parameters per statement. A single
# ``INSERT ... VALUES`` with a wide payload (many columns × many rows) trips this
# hard limit and the whole statement fails — which silently broke cost collection
# once the lookback window grew large. Bulk upserts chunk their rows to stay under
# it, sized per-caller from the row's column count.
_PG_MAX_BIND_PARAMS = 65535


def _rows_per_statement(columns: int) -> int:
    """Max rows per INSERT so ``columns × rows`` stays under Postgres's bind cap."""
    return max(1, _PG_MAX_BIND_PARAMS // max(columns, 1))


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
        "provider": rec.provider,
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
    provider: str = "azure",
    tenant_id: str | None = None,
    client_id: str | None = None,
    client_secret: str | None = None,
    enabled: bool = True,
) -> dict[str, Any]:
    """Create or update a cloud account (subscription).

    ``provider`` is set at creation and is intrinsic to the account (an existing
    account keeps its provider on subsequent upserts). Secret semantics on update:
    ``client_secret=None`` keeps the existing secret, ``client_secret=""`` clears
    it, any other value sets it.
    """
    rec = session.get(schema.Subscription, subscription_id)
    make_default = session.query(schema.Subscription).count() == 0
    if rec is None:
        rec = schema.Subscription(
            subscription_id=subscription_id,
            is_default=make_default,
            provider=provider or "azure",
        )
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


def _auto_display_name(sub_id: str) -> str:
    """The placeholder name the seed assigns before the real cloud name is known."""
    return f"Default ({sub_id[:8]}…)" if len(sub_id) > 8 else sub_id


def ensure_default_subscription(session: Session, settings: Any) -> None:
    """Seed the subscriptions table from the env subscription if it is empty."""
    if session.query(schema.Subscription).count() > 0:
        return
    sub_id = settings.azure_subscription_id
    session.add(
        schema.Subscription(
            subscription_id=sub_id,
            display_name=_auto_display_name(sub_id),
            provider="azure",
            tenant_id=settings.azure_tenant_id,
            enabled=True,
            is_default=True,
        )
    )
    session.flush()


def is_auto_display_name(rec: schema.Subscription) -> bool:
    """True while the row still carries the seed placeholder (real name not yet synced)."""
    return rec.display_name == _auto_display_name(rec.subscription_id)


def backfill_display_name(session: Session, subscription_id: str, real_name: str | None) -> bool:
    """Set the display name to the cloud-resolved name, but only while it is still
    the auto-generated placeholder — so a name the user chose is never clobbered.
    Returns True when a change was persisted."""
    if not real_name or not real_name.strip():
        return False
    rec = session.get(schema.Subscription, subscription_id)
    if rec is None or rec.display_name != _auto_display_name(subscription_id):
        return False
    rec.display_name = real_name.strip()
    session.flush()
    return True


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
        "team_id": rec.team_id,
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
    team_id: int | None = None,
    actor: str | None = None,
) -> dict[str, Any]:
    """Persist a new policy (enabled, version 1). Raises on a duplicate ``name``.

    Seeds the version history with a version-1 snapshot so the created state is
    always the first entry in the audit trail. ``team_id`` scopes the policy to an
    owning team (M11.2); ``None`` leaves it unscoped/global.
    """
    rec = schema.Policy(
        name=name,
        resource_type=resource_type,
        spec=spec,
        description=description,
        source=source,
        team_id=team_id,
    )
    session.add(rec)
    session.flush()
    _snapshot_policy_version(session, rec, actor=actor)
    return _policy_public(rec)


def get_policy(session: Session, policy_id: int) -> dict[str, Any] | None:
    rec = session.get(schema.Policy, policy_id)
    return _policy_public(rec) if rec is not None else None


def get_policy_by_name(session: Session, name: str) -> dict[str, Any] | None:
    """Look up a policy by its unique ``name`` (``None`` if absent)."""
    rec = session.query(schema.Policy).filter(schema.Policy.name == name).one_or_none()
    return _policy_public(rec) if rec is not None else None


def list_policies(
    session: Session,
    enabled_only: bool = False,
    team_ids: list[int] | None = None,
) -> list[dict[str, Any]]:
    """List policies, optionally filtered to ``enabled`` and/or a set of owning teams.

    ``team_ids=None`` applies no team filter (all policies — the unscoped default);
    ``team_ids=[...]`` restricts to policies owned by those teams (M11.2 scoping);
    ``team_ids=[]`` matches nothing (a member of no team sees no scoped policies).
    """
    query = session.query(schema.Policy)
    if enabled_only:
        query = query.filter(schema.Policy.enabled.is_(True))
    if team_ids is not None:
        query = query.filter(schema.Policy.team_id.in_(team_ids))
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


def get_or_create_collection(
    session: Session, *, name: str, description: str | None = None
) -> dict[str, Any]:
    """Return the collection named ``name``, creating it if absent (idempotent).

    Used by pack install so re-installing reuses the pack's collection rather than
    colliding on the unique ``name``. An existing collection's description is left
    untouched.
    """
    existing = (
        session.query(schema.PolicyCollection)
        .filter(schema.PolicyCollection.name == name)
        .one_or_none()
    )
    if existing is not None:
        return _collection_public(session, existing)
    return create_collection(session, name=name, description=description)


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
# Installed policy packs (M10.1)
# --------------------------------------------------------------------------- #
def _installed_pack_public(session: Session, rec: schema.InstalledPack) -> dict[str, Any]:
    """Serialize an installed-pack row with its collection's policy count."""
    policy_count = (
        session.query(schema.CollectionPolicy)
        .filter(schema.CollectionPolicy.collection_id == rec.collection_id)
        .count()
    )
    return {
        "name": rec.name,
        "version": rec.version,
        "collection_id": rec.collection_id,
        "enabled": rec.enabled,
        "policy_count": policy_count,
        "installed_at": rec.installed_at.isoformat() if rec.installed_at else None,
        "updated_at": rec.updated_at.isoformat() if rec.updated_at else None,
    }


def upsert_installed_pack(
    session: Session, *, name: str, version: str, collection_id: int
) -> dict[str, Any]:
    """Record (or update) an installed pack keyed by ``name`` — idempotent.

    A re-install of the same pack updates the tracked ``version``/``collection_id``
    in place (no duplicate row) and preserves the existing ``enabled`` state so a
    disabled pack is not silently re-enabled by re-installing it.
    """
    rec = session.get(schema.InstalledPack, name)
    if rec is None:
        rec = schema.InstalledPack(name=name, version=version, collection_id=collection_id)
        session.add(rec)
    else:
        rec.version = version
        rec.collection_id = collection_id
    session.flush()
    return _installed_pack_public(session, rec)


def get_installed_pack(session: Session, name: str) -> dict[str, Any] | None:
    rec = session.get(schema.InstalledPack, name)
    return _installed_pack_public(session, rec) if rec is not None else None


def list_installed_packs(session: Session) -> list[dict[str, Any]]:
    recs = session.query(schema.InstalledPack).order_by(schema.InstalledPack.name.asc()).all()
    return [_installed_pack_public(session, r) for r in recs]


def set_pack_enabled(session: Session, name: str, enabled: bool) -> dict[str, Any] | None:
    """Toggle a pack's ``enabled`` flag and cascade it to member policies.

    Binding runs resolve a collection's policies enabled-only, so flipping the pack
    flips its policies' ``enabled`` — a disabled pack stops running in bindings.
    Returns ``None`` if the pack isn't installed.
    """
    rec = session.get(schema.InstalledPack, name)
    if rec is None:
        return None
    rec.enabled = enabled
    session.query(schema.Policy).filter(
        schema.Policy.id.in_(
            session.query(schema.CollectionPolicy.policy_id).filter(
                schema.CollectionPolicy.collection_id == rec.collection_id
            )
        )
    ).update({schema.Policy.enabled: enabled}, synchronize_session=False)
    session.flush()
    return _installed_pack_public(session, rec)


# --------------------------------------------------------------------------- #
# RBAC: roles, permissions, role bindings (M11.1)
# --------------------------------------------------------------------------- #
def _role_permissions(session: Session, role_id: int) -> list[str]:
    rows = (
        session.query(schema.Permission.action)
        .filter(schema.Permission.role_id == role_id)
        .order_by(schema.Permission.action.asc())
        .all()
    )
    return [r[0] for r in rows]


def _role_public(session: Session, rec: schema.Role) -> dict[str, Any]:
    return {
        "id": rec.id,
        "name": rec.name,
        "description": rec.description,
        "permissions": _role_permissions(session, rec.id),
    }


def get_role_by_name(session: Session, name: str) -> schema.Role | None:
    return session.query(schema.Role).filter(schema.Role.name == name).one_or_none()


def upsert_role(
    session: Session, *, name: str, description: str | None, permissions: list[str]
) -> dict[str, Any]:
    """Create or update a role and set its permission grants (idempotent).

    The role's permissions are replaced with the supplied set — re-seeding the same
    definition is a no-op and never duplicates grants.
    """
    rec = get_role_by_name(session, name)
    if rec is None:
        rec = schema.Role(name=name, description=description)
        session.add(rec)
        session.flush()
    else:
        rec.description = description
    desired = set(permissions)
    existing = set(_role_permissions(session, rec.id))
    for action in desired - existing:
        session.add(schema.Permission(role_id=rec.id, action=action))
    for action in existing - desired:
        session.query(schema.Permission).filter(
            schema.Permission.role_id == rec.id, schema.Permission.action == action
        ).delete()
    session.flush()
    return _role_public(session, rec)


def list_roles(session: Session) -> list[dict[str, Any]]:
    recs = session.query(schema.Role).order_by(schema.Role.name.asc()).all()
    return [_role_public(session, r) for r in recs]


def assign_role(session: Session, *, principal: str, role_name: str) -> dict[str, Any] | None:
    """Bind ``principal`` to ``role_name`` (idempotent). ``None`` if the role is unknown."""
    role = get_role_by_name(session, role_name)
    if role is None:
        return None
    existing = (
        session.query(schema.RoleBinding)
        .filter(
            schema.RoleBinding.principal == principal,
            schema.RoleBinding.role_id == role.id,
        )
        .one_or_none()
    )
    if existing is None:
        session.add(schema.RoleBinding(principal=principal, role_id=role.id))
        session.flush()
    return {"principal": principal, "role": role_name}


def remove_role_binding(session: Session, principal: str, role_name: str) -> bool:
    role = get_role_by_name(session, role_name)
    if role is None:
        return False
    deleted = (
        session.query(schema.RoleBinding)
        .filter(
            schema.RoleBinding.principal == principal,
            schema.RoleBinding.role_id == role.id,
        )
        .delete()
    )
    session.flush()
    return bool(deleted)


def list_role_bindings(session: Session, principal: str | None = None) -> list[dict[str, Any]]:
    query = (
        session.query(schema.RoleBinding.principal, schema.Role.name)
        .join(schema.Role, schema.Role.id == schema.RoleBinding.role_id)
        .order_by(schema.RoleBinding.principal.asc(), schema.Role.name.asc())
    )
    if principal is not None:
        query = query.filter(schema.RoleBinding.principal == principal)
    return [{"principal": p, "role": r} for p, r in query.all()]


def resolve_permissions(session: Session, principal: str) -> set[str]:
    """The union of all action grants a principal holds across its bound roles."""
    rows = (
        session.query(schema.Permission.action)
        .join(schema.Role, schema.Role.id == schema.Permission.role_id)
        .join(schema.RoleBinding, schema.RoleBinding.role_id == schema.Role.id)
        .filter(schema.RoleBinding.principal == principal)
        .distinct()
        .all()
    )
    return {r[0] for r in rows}


# --------------------------------------------------------------------------- #
# Teams & membership (M11.2) — multi-tenancy scoping of governance resources
# --------------------------------------------------------------------------- #
def _team_public(rec: schema.Team) -> dict[str, Any]:
    return {
        "id": rec.id,
        "name": rec.name,
        "description": rec.description,
        "created_at": rec.created_at.isoformat() if rec.created_at else None,
    }


def get_team_by_name(session: Session, name: str) -> schema.Team | None:
    return session.query(schema.Team).filter(schema.Team.name == name).one_or_none()


def create_team(session: Session, *, name: str, description: str | None = None) -> dict[str, Any]:
    """Persist a new team. Raises ``IntegrityError`` on a duplicate ``name``."""
    rec = schema.Team(name=name, description=description)
    session.add(rec)
    session.flush()
    return _team_public(rec)


def get_team(session: Session, team_id: int) -> dict[str, Any] | None:
    rec = session.get(schema.Team, team_id)
    return _team_public(rec) if rec is not None else None


def list_teams(session: Session) -> list[dict[str, Any]]:
    recs = session.query(schema.Team).order_by(schema.Team.name.asc()).all()
    return [_team_public(r) for r in recs]


def add_team_member(
    session: Session, *, team_id: int, principal: str, role: str = "member"
) -> dict[str, Any] | None:
    """Add ``principal`` to a team (idempotent). ``None`` if the team is unknown.

    A re-add is a no-op — the existing membership (and its ``role``) is preserved,
    so the ``(team_id, principal)`` uniqueness is never violated.
    """
    if session.get(schema.Team, team_id) is None:
        return None
    existing = (
        session.query(schema.TeamMember)
        .filter(
            schema.TeamMember.team_id == team_id,
            schema.TeamMember.principal == principal,
        )
        .one_or_none()
    )
    if existing is None:
        session.add(schema.TeamMember(team_id=team_id, principal=principal, role=role))
        session.flush()
        return {"principal": principal, "role": role}
    return {"principal": existing.principal, "role": existing.role}


def remove_team_member(session: Session, team_id: int, principal: str) -> bool:
    """Remove a principal from a team. ``False`` if no such membership existed."""
    deleted = (
        session.query(schema.TeamMember)
        .filter(
            schema.TeamMember.team_id == team_id,
            schema.TeamMember.principal == principal,
        )
        .delete()
    )
    session.flush()
    return bool(deleted)


def list_team_members(session: Session, team_id: int) -> list[dict[str, Any]]:
    rows = (
        session.query(schema.TeamMember.principal, schema.TeamMember.role)
        .filter(schema.TeamMember.team_id == team_id)
        .order_by(schema.TeamMember.principal.asc())
        .all()
    )
    return [{"principal": p, "role": r} for p, r in rows]


def list_teams_for_principal(session: Session, principal: str) -> list[int]:
    """The ids of every team a principal belongs to (empty when they belong to none)."""
    rows = (
        session.query(schema.TeamMember.team_id)
        .filter(schema.TeamMember.principal == principal)
        .order_by(schema.TeamMember.team_id.asc())
        .all()
    )
    return [r[0] for r in rows]


def is_team_member(session: Session, team_id: int, principal: str) -> bool:
    return (
        session.query(schema.TeamMember.id)
        .filter(
            schema.TeamMember.team_id == team_id,
            schema.TeamMember.principal == principal,
        )
        .first()
        is not None
    )


# --------------------------------------------------------------------------- #
# Audit log (M11.4) — append-only record of mutating governance actions
# --------------------------------------------------------------------------- #
def _audit_public(rec: schema.AuditLog) -> dict[str, Any]:
    return {
        "id": rec.id,
        "actor": rec.actor,
        "action": rec.action,
        "target_type": rec.target_type,
        "target_id": rec.target_id,
        "before": rec.before,
        "after": rec.after,
        "at": rec.at.isoformat() if rec.at else None,
    }


def insert_audit_log(
    session: Session,
    *,
    actor: str | None,
    action: str,
    target_type: str,
    target_id: str | None,
    before: dict[str, Any],
    after: dict[str, Any],
) -> dict[str, Any]:
    """Append one audit row (insert-only — the log is never updated or deleted)."""
    rec = schema.AuditLog(
        actor=actor,
        action=action,
        target_type=target_type,
        target_id=target_id,
        before=before,
        after=after,
    )
    session.add(rec)
    session.flush()
    return _audit_public(rec)


def list_audit_logs(
    session: Session,
    *,
    actor: str | None = None,
    action: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """List audit rows newest-first, optionally filtered by actor/action/target.

    Ordered by ``at`` descending with ``id`` as the tiebreaker, so entries written in
    the same transaction (identical timestamps) still surface newest-first.
    """
    query = session.query(schema.AuditLog)
    if actor is not None:
        query = query.filter(schema.AuditLog.actor == actor)
    if action is not None:
        query = query.filter(schema.AuditLog.action == action)
    if target_type is not None:
        query = query.filter(schema.AuditLog.target_type == target_type)
    if target_id is not None:
        query = query.filter(schema.AuditLog.target_id == target_id)
    recs = (
        query.order_by(schema.AuditLog.at.desc(), schema.AuditLog.id.desc())
        .limit(limit)
        .offset(offset)
        .all()
    )
    return [_audit_public(r) for r in recs]


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
            "provider": r.provider,
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
            "provider": stmt.excluded.provider,
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
            "provider": r.provider,
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
            "provider": stmt.excluded.provider,
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
    "provider": schema.Asset.provider,
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
        "provider": rec.provider,
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
    step = _rows_per_statement(columns=11)  # cost_snapshots row = 11 bound params
    for start in range(0, len(payload), step):
        chunk = payload[start : start + step]
        stmt = pg_insert(schema.CostSnapshot).values(chunk)
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
    step = _rows_per_statement(columns=8)  # utilization_samples row = 8 bound params
    for start in range(0, len(payload), step):
        chunk = payload[start : start + step]
        stmt = pg_insert(schema.UtilizationSample).values(chunk)
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
    step = _rows_per_statement(columns=len(payload[0]))
    for start in range(0, len(payload), step):
        chunk = payload[start : start + step]
        stmt = pg_insert(schema.UtilizationRollup).values(chunk)
        update_cols = {
            c: getattr(stmt.excluded, c) for c in chunk[0] if c not in {"resource_id", "window_end"}
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


def _cost_scope(days: int, provider: str | None) -> tuple[str, dict[str, Any]]:
    """WHERE fragment + bound params scoping ``cost_snapshots`` to a day window
    and (optionally) one cloud (#116). The window uses ``make_interval`` (as in
    #113); the provider filter maps through ``subscriptions.provider``. Both are
    bound parameters — injection-safe. ``provider`` None/"" means all clouds."""
    sql = (
        " WHERE cost_type = 'Amortized' "
        "AND usage_date >= CURRENT_DATE - make_interval(days => :days)"
    )
    params: dict[str, Any] = {"days": days}
    if provider:
        sql += (
            " AND subscription_id IN "
            "(SELECT subscription_id FROM subscriptions WHERE provider = :provider)"
        )
        params["provider"] = provider
    return sql, params


def cost_by_type(
    session: Session, days: int = 30, provider: str | None = None
) -> list[dict[str, Any]]:
    where, params = _cost_scope(days, provider)
    return _rows(
        session,
        "SELECT resource_type, SUM(cost) AS cost, currency FROM cost_snapshots"
        + where
        + " GROUP BY resource_type, currency ORDER BY cost DESC",
        **params,
    )


def cost_by_region(
    session: Session, days: int = 30, provider: str | None = None
) -> list[dict[str, Any]]:
    where, params = _cost_scope(days, provider)
    return _rows(
        session,
        "SELECT location, SUM(cost) AS cost, currency FROM cost_snapshots"
        + where
        + " GROUP BY location, currency ORDER BY cost DESC",
        **params,
    )


def cost_by_resource(session: Session, limit: int = 50) -> list[dict[str, Any]]:
    return _rows(
        session,
        "SELECT * FROM v_cost_by_resource ORDER BY cost DESC LIMIT :limit",
        limit=limit,
    )


def total_cost(session: Session, days: int = 30, provider: str | None = None) -> float:
    where, params = _cost_scope(days, provider)
    rows = _rows(
        session,
        "SELECT COALESCE(SUM(cost), 0) AS total FROM cost_snapshots" + where,
        **params,
    )
    return float(rows[0]["total"]) if rows else 0.0


def cost_trend(session: Session, days: int = 30) -> dict[str, Any]:
    """Amortized cost for the current ``days``-day window vs the immediately
    prior window of the same length, plus a daily series across the current
    window.

    ``days`` is bound as a parameter (injection-safe). Numeric costs are cast to
    float for JSON. ``delta_pct`` is ``None`` when the prior window is empty
    (a division-by-zero guard, so a first-ever period never reports a bogus %).
    """
    series_rows = _rows(
        session,
        "SELECT usage_date, SUM(cost) AS cost FROM cost_snapshots "
        "WHERE cost_type = 'Amortized' "
        "AND usage_date >= CURRENT_DATE - make_interval(days => :days) "
        "GROUP BY usage_date ORDER BY usage_date",
        days=days,
    )
    series = [{"date": r["usage_date"].isoformat(), "cost": float(r["cost"])} for r in series_rows]
    total = round(sum(item["cost"] for item in series), 6)

    prior_rows = _rows(
        session,
        "SELECT COALESCE(SUM(cost), 0) AS total FROM cost_snapshots "
        "WHERE cost_type = 'Amortized' "
        "AND usage_date >= CURRENT_DATE - make_interval(days => :prior_days) "
        "AND usage_date < CURRENT_DATE - make_interval(days => :days)",
        days=days,
        prior_days=days * 2,
    )
    prior_total = float(prior_rows[0]["total"]) if prior_rows else 0.0

    currency_rows = _rows(
        session,
        "SELECT currency FROM cost_snapshots WHERE cost_type = 'Amortized' "
        "ORDER BY usage_date DESC LIMIT 1",
    )
    currency = currency_rows[0]["currency"] if currency_rows else "USD"

    delta = round(total - prior_total, 6)
    delta_pct = round(delta / prior_total * 100, 2) if prior_total else None

    return {
        "days": days,
        "currency": currency,
        "total": total,
        "prior_total": prior_total,
        "delta": delta,
        "delta_pct": delta_pct,
        "series": series,
    }


def cost_monthly(session: Session, months: int = 6, provider: str | None = None) -> dict[str, Any]:
    """Amortized spend bucketed by calendar month over the last ``months`` months
    (including the current, partial one). Only months that actually have data are
    returned. The provider filter mirrors ``_cost_scope``; ``months`` is clamped to
    1..24 and every value is a bound parameter (injection-safe)."""
    months = min(max(months, 1), 24)
    where = (
        " WHERE cost_type = 'Amortized' "
        "AND usage_date >= date_trunc('month', CURRENT_DATE) - make_interval(months => :back)"
    )
    params: dict[str, Any] = {"back": months - 1}
    if provider:
        where += (
            " AND subscription_id IN "
            "(SELECT subscription_id FROM subscriptions WHERE provider = :provider)"
        )
        params["provider"] = provider
    rows = _rows(
        session,
        "SELECT to_char(date_trunc('month', usage_date), 'YYYY-MM') AS month, "
        "SUM(cost) AS cost, MAX(currency) AS currency FROM cost_snapshots"
        + where
        + " GROUP BY date_trunc('month', usage_date) ORDER BY date_trunc('month', usage_date)",
        **params,
    )
    series = [
        {"month": r["month"], "cost": float(r["cost"]), "currency": r["currency"]} for r in rows
    ]
    currency = series[-1]["currency"] if series else "USD"
    return {"months": months, "currency": currency, "series": series}


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


# Reusable count/violation aggregates over v_governance_posture (M9.1). Each
# grouping SELECTs the same four measures; only the GROUP BY / labels differ.
_POSTURE_MEASURES = (
    "COUNT(*) FILTER (WHERE gp.compliant)     AS compliant, "
    "COUNT(*) FILTER (WHERE gp.non_compliant) AS non_compliant, "
    "COALESCE(SUM(gp.resources_matched), 0)   AS violations, "
    "COUNT(*)                                 AS evaluated"
)


def governance_posture(session: Session, *, provider: str | None = None) -> dict[str, Any]:
    """Compliance posture (M9.1, M12.4): compliant vs non-compliant rollups.

    Reads ``v_governance_posture`` — the latest execution per ``(policy,
    subscription)`` — and aggregates it five ways (by policy, by subscription, by
    collection, by CIS ``control_id``, and by cloud ``provider``) plus a ``totals``
    block. ``violations`` sums matched resources. The ``by_control`` rollup extracts
    each policy's ``metadata.control_id`` from its stored spec (M10.4) so posture can
    be framed against a compliance framework; policies without one are excluded.

    ``provider`` filters the *entire* response to one cloud (``azure``/``aws``/``gcp``);
    ``None`` (the default) spans all clouds — the cross-cloud single pane. With
    nothing executed yet the totals are zeroed and the group lists empty — the empty
    state is data, never an error.
    """
    # Optional provider predicate, bound as a parameter (injection-safe). ``where`` is
    # for the group queries that carry no filter today; ``and_`` extends by_control's
    # existing WHERE. Both are empty strings when no provider is requested.
    params: dict[str, Any] = {}
    where = ""
    and_ = ""
    if provider:
        params["provider"] = provider
        where = " WHERE gp.provider = :provider"
        and_ = " AND gp.provider = :provider"

    totals = _rows(
        session,
        f"SELECT {_POSTURE_MEASURES} FROM v_governance_posture gp{where}",
        **params,
    )[0]
    by_policy = _rows(
        session,
        f"SELECT gp.policy_id, gp.policy_name, {_POSTURE_MEASURES} "
        f"FROM v_governance_posture gp{where} "
        "GROUP BY gp.policy_id, gp.policy_name "
        "ORDER BY gp.policy_name ASC",
        **params,
    )
    by_subscription = _rows(
        session,
        f"SELECT gp.subscription_id, {_POSTURE_MEASURES} "
        f"FROM v_governance_posture gp{where} "
        "GROUP BY gp.subscription_id "
        "ORDER BY gp.subscription_id ASC",
        **params,
    )
    by_provider = _rows(
        session,
        f"SELECT gp.provider, {_POSTURE_MEASURES} "
        f"FROM v_governance_posture gp{where} "
        "GROUP BY gp.provider "
        "ORDER BY gp.provider ASC",
        **params,
    )
    by_collection = _rows(
        session,
        f"SELECT c.id AS collection_id, c.name AS collection_name, {_POSTURE_MEASURES} "
        "FROM v_governance_posture gp "
        "JOIN collection_policies cp ON cp.policy_id = gp.policy_id "
        f"JOIN policy_collections c ON c.id = cp.collection_id{where} "
        "GROUP BY c.id, c.name "
        "ORDER BY c.name ASC",
        **params,
    )
    # by CIS control id (M10.4): extract metadata.control_id from the stored spec
    # (spec -> policies[0] -> metadata -> control_id); uncontrolled policies drop out.
    control_expr = "p.spec -> 'policies' -> 0 -> 'metadata' ->> 'control_id'"
    by_control = _rows(
        session,
        f"SELECT ({control_expr}) AS control_id, {_POSTURE_MEASURES} "
        "FROM v_governance_posture gp "
        "JOIN policies p ON p.id = gp.policy_id "
        f"WHERE ({control_expr}) IS NOT NULL{and_} "
        "GROUP BY control_id "
        "ORDER BY control_id ASC",
        **params,
    )
    return {
        "totals": totals,
        "by_policy": by_policy,
        "by_subscription": by_subscription,
        "by_collection": by_collection,
        "by_control": by_control,
        "by_provider": by_provider,
    }


# The execution-health measures at the *execution* grain (executions aliased ``e``).
# Kept identical to the v_execution_health / v_execution_health_by_binding view
# columns so a provider-filtered recompute (which must join subscriptions) returns
# the very same shape as the unfiltered view read.
_EXEC_MEASURES = (
    "COUNT(e.execution_id)                                       AS total_executions, "
    "COUNT(e.execution_id) FILTER (WHERE e.status = 'succeeded')  AS succeeded, "
    "COUNT(e.execution_id) FILTER (WHERE e.status = 'failed')     AS failed, "
    "ROUND((COUNT(e.execution_id) FILTER (WHERE e.status = 'succeeded'))::numeric "
    "      / NULLIF(COUNT(e.execution_id), 0), 4)                 AS success_rate, "
    "COALESCE(ROUND((AVG(EXTRACT(EPOCH FROM (e.finished_at - e.started_at))) "
    "         FILTER (WHERE e.finished_at IS NOT NULL))::numeric, 3), 0) AS avg_duration_seconds, "
    "MAX(e.started_at)                                           AS last_execution_at, "
    "(ARRAY_AGG(e.status ORDER BY e.started_at DESC, e.execution_id DESC))[1] AS last_status"
)


def execution_health(session: Session, *, provider: str | None = None) -> dict[str, Any]:
    """Policy execution health (M9.2, M12.4): the governance engine's own health.

    Returns ``by_policy`` (per policy), ``by_binding`` (per binding) and
    ``by_provider`` (per cloud) — succeeded/failed counts, success rate, average
    wall-clock duration and last run — newest-executed first. A policy/binding/
    provider that has never executed is absent, so the empty state is three empty
    lists — never an error.

    ``provider`` scopes ``by_policy``/``by_binding`` to executions attributed to
    that cloud (an execution's provider is its subscription's ``provider``, defaulting
    to ``azure``) and narrows ``by_provider`` to the single row; ``None`` (the
    default) spans all clouds.
    """
    if provider:
        params = {"provider": provider}
        by_policy = _rows(
            session,
            f"SELECT p.id AS policy_id, p.name AS policy_name, {_EXEC_MEASURES} "
            "FROM policies p "
            "JOIN policy_executions e ON e.policy_id = p.id "
            "LEFT JOIN subscriptions s ON s.subscription_id = e.subscription_id "
            "WHERE COALESCE(s.provider, 'azure') = :provider "
            "GROUP BY p.id, p.name "
            "ORDER BY MAX(e.started_at) DESC NULLS LAST, p.name ASC",
            **params,
        )
        by_binding = _rows(
            session,
            f"SELECT e.binding_id AS binding_id, {_EXEC_MEASURES} "
            "FROM policy_executions e "
            "LEFT JOIN subscriptions s ON s.subscription_id = e.subscription_id "
            "WHERE e.binding_id IS NOT NULL AND COALESCE(s.provider, 'azure') = :provider "
            "GROUP BY e.binding_id "
            "ORDER BY MAX(e.started_at) DESC NULLS LAST, e.binding_id ASC",
            **params,
        )
        by_provider = _rows(
            session,
            "SELECT * FROM v_execution_health_by_provider WHERE provider = :provider "
            "ORDER BY provider ASC",
            **params,
        )
    else:
        by_policy = _rows(
            session,
            "SELECT * FROM v_execution_health "
            "ORDER BY last_execution_at DESC NULLS LAST, policy_name ASC",
        )
        by_binding = _rows(
            session,
            "SELECT * FROM v_execution_health_by_binding "
            "ORDER BY last_execution_at DESC NULLS LAST, binding_id ASC",
        )
        by_provider = _rows(
            session,
            "SELECT * FROM v_execution_health_by_provider ORDER BY provider ASC",
        )
    return {"by_policy": by_policy, "by_binding": by_binding, "by_provider": by_provider}


# Column order for the governance export (M9.4) — the CSV header and JSON keys.
GOVERNANCE_EXPORT_COLUMNS = (
    "execution_id",
    "policy_id",
    "policy_name",
    "subscription_id",
    "binding_id",
    "status",
    "resources_matched",
    "started_at",
    "finished_at",
    "duration_seconds",
)

_EXPORT_SQL = """
SELECT
    e.execution_id                                        AS execution_id,
    e.policy_id                                           AS policy_id,
    p.name                                                AS policy_name,
    e.subscription_id                                     AS subscription_id,
    e.binding_id                                          AS binding_id,
    e.status                                              AS status,
    e.resources_matched                                   AS resources_matched,
    e.started_at                                          AS started_at,
    e.finished_at                                         AS finished_at,
    EXTRACT(EPOCH FROM (e.finished_at - e.started_at))    AS duration_seconds
FROM policy_executions e
JOIN policies p ON p.id = e.policy_id
ORDER BY e.started_at ASC, e.execution_id ASC
LIMIT :limit OFFSET :offset
"""


def iter_governance_export(session: Session, batch_size: int = 500) -> Iterator[dict[str, Any]]:
    """Yield per-execution governance-export rows (M9.4), one at a time.

    Reads the joined ``policy_executions``/``policies`` evidence in ``batch_size``
    pages (``LIMIT``/``OFFSET``) so at most one page is ever held in memory — the
    export streams over arbitrarily large histories without a full in-memory load.
    Ordered by ``started_at``, ``execution_id`` for a deterministic, resumable cursor.
    """
    offset = 0
    while True:
        rows = _rows(session, _EXPORT_SQL, limit=batch_size, offset=offset)
        yield from rows
        if len(rows) < batch_size:
            break
        offset += batch_size


def policy_matched_resources(
    session: Session, policy_id: int, limit: int = 500
) -> list[dict[str, Any]]:
    """Resources currently flagged by a policy (M9.3), for the compliance explorer.

    Returns the matches from each subscription's **latest** execution of the policy —
    the current non-compliant set (its size equals the policy's posture ``violations``
    count), newest match first. Each row carries the ``resource_id`` (linkable to its
    M4.5 AssetDB detail), ``resource_type``, ``subscription_id`` and ``matched_at``.
    Empty when the policy has no matches. ``policy_id`` is bound (injection-safe).
    """
    return _rows(
        session,
        """
        WITH latest AS (
            SELECT
                e.execution_id,
                e.subscription_id,
                ROW_NUMBER() OVER (
                    PARTITION BY e.subscription_id
                    ORDER BY e.started_at DESC, e.execution_id DESC
                ) AS rn
            FROM policy_executions e
            WHERE e.policy_id = :policy_id
        )
        SELECT
            mt.resource_id      AS resource_id,
            mt.resource_type    AS resource_type,
            l.subscription_id   AS subscription_id,
            mt.matched_at       AS matched_at
        FROM latest l
        JOIN policy_matches mt ON mt.execution_id = l.execution_id
        WHERE l.rn = 1
        ORDER BY mt.matched_at DESC, mt.resource_id ASC
        LIMIT :limit
        """,
        policy_id=policy_id,
        limit=limit,
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


def update_notification_template(
    session: Session, template_id: int, changes: dict[str, Any]
) -> dict[str, Any] | None:
    """Partial update (only the given fields). ``None`` if the template is missing."""
    rec = session.get(schema.NotificationTemplate, template_id)
    if rec is None:
        return None
    for field in ("name", "subject", "body", "format", "description"):
        if field in changes:
            setattr(rec, field, changes[field])
    session.flush()
    return _notification_template_public(rec)


# Binding ↔ notification attachments (M8.4) --------------------------------- #
def _binding_notification_public(
    rec: schema.BindingNotification,
    channel: schema.NotificationChannel,
    template: schema.NotificationTemplate,
) -> dict[str, Any]:
    return {
        "id": rec.id,
        "binding_id": rec.binding_id,
        "channel_id": rec.channel_id,
        "channel_name": channel.name,
        "channel_transport": channel.transport,
        "template_id": rec.template_id,
        "template_name": template.name,
        "created_at": rec.created_at.isoformat() if rec.created_at else None,
    }


def create_binding_notification(
    session: Session, *, binding_id: int, channel_id: int, template_id: int
) -> dict[str, Any] | None:
    """Attach a (channel, template) pair to a binding.

    Returns ``None`` if the binding, channel or template does not exist; raises
    ``ValueError`` if the channel is already attached to this binding.
    """
    binding = session.get(schema.Binding, binding_id)
    channel = session.get(schema.NotificationChannel, channel_id)
    template = session.get(schema.NotificationTemplate, template_id)
    if binding is None or channel is None or template is None:
        return None
    existing = (
        session.query(schema.BindingNotification)
        .filter(
            schema.BindingNotification.binding_id == binding_id,
            schema.BindingNotification.channel_id == channel_id,
        )
        .first()
    )
    if existing is not None:
        raise ValueError("channel already attached to this binding")
    rec = schema.BindingNotification(
        binding_id=binding_id, channel_id=channel_id, template_id=template_id
    )
    session.add(rec)
    session.flush()
    return _binding_notification_public(rec, channel, template)


def list_binding_notifications(session: Session, binding_id: int) -> list[dict[str, Any]]:
    """The (channel, template) attachments on a binding, enriched with their names."""
    rows = (
        session.query(
            schema.BindingNotification, schema.NotificationChannel, schema.NotificationTemplate
        )
        .join(
            schema.NotificationChannel,
            schema.NotificationChannel.id == schema.BindingNotification.channel_id,
        )
        .join(
            schema.NotificationTemplate,
            schema.NotificationTemplate.id == schema.BindingNotification.template_id,
        )
        .filter(schema.BindingNotification.binding_id == binding_id)
        .order_by(schema.BindingNotification.id.asc())
        .all()
    )
    return [_binding_notification_public(rec, channel, template) for rec, channel, template in rows]


def delete_binding_notification(session: Session, notification_id: int) -> bool:
    rec = session.get(schema.BindingNotification, notification_id)
    if rec is None:
        return False
    session.delete(rec)
    session.flush()
    return True
