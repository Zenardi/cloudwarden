"""Repository: idempotent writes and read helpers over the ORM schema.

All fact writes use PostgreSQL ``INSERT ... ON CONFLICT DO UPDATE`` and dedupe
within the batch first (Postgres rejects a conflict target hit twice in one
statement), so re-running a collection never creates duplicate rows.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, text
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


def list_remediation_actions(session: Session, limit: int = 100) -> list[dict[str, Any]]:
    return _rows(
        session,
        "SELECT ra.id, ra.action_type, ra.dry_run, ra.status, ra.error, ra.requested_at, "
        "ra.executed_at, ra.actor, r.resource_id, r.category "
        "FROM remediation_actions ra "
        "LEFT JOIN recommendations r ON ra.recommendation_id = r.id "
        "ORDER BY ra.requested_at DESC LIMIT :limit",
        limit=limit,
    )
