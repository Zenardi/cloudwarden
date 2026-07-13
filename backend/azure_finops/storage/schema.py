"""SQLAlchemy ORM schema.

Fact tables (`cost_snapshots`, `utilization_samples`) use natural composite
primary keys that include their time column so they can be promoted to
TimescaleDB hypertables (a hypertable's unique indexes must include the
partitioning column). On plain Postgres they behave as ordinary tables.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Subscription(Base):
    """A managed Azure subscription.

    Credentials are optional: when ``client_id``/``client_secret`` are set the
    collectors build a per-subscription ``ClientSecretCredential``; otherwise they
    fall back to the shared env service principal. ``client_secret`` is stored in
    plaintext (v1) — a Key Vault / column-encryption backing is a hardening TODO.
    """

    __tablename__ = "subscriptions"

    subscription_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(256))
    tenant_id: Mapped[str | None] = mapped_column(String(64))
    client_id: Mapped[str | None] = mapped_column(String(128))
    client_secret: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Policy(Base):
    """A governance-as-code rule (Cloud Custodian policy) persisted for CRUD.

    ``spec`` holds the parsed Custodian policy body (the mapping that would sit
    under a single ``policies:`` list entry). ``version`` bumps on every
    ``update_policy`` so callers can detect drift, and ``source`` distinguishes
    user-authored policies from ``library``/``imported`` ones.
    """

    __tablename__ = "policies"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(256), unique=True)
    resource_type: Mapped[str] = mapped_column(String(128), index=True)
    spec: Mapped[dict] = mapped_column(JSONB, default=dict)
    description: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    source: Mapped[str] = mapped_column(String(32), default="custom")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class PolicyVersion(Base):
    """An immutable snapshot of a policy taken each time its content changes.

    ``create_policy`` seeds version 1 and every content-changing ``update_policy``
    appends the next number, so the row set is an append-only audit trail. The
    snapshot copies the policy's authored fields (``name``/``resource_type``/
    ``spec``/``description``) — enough to render history and diff any two revisions
    without reconstructing state. ``actor`` records who made the change (reserved
    for when auth lands; ``NULL`` until then). Rows cascade-delete with the policy.
    """

    __tablename__ = "policy_versions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    policy_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("policies.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(256))
    resource_type: Mapped[str] = mapped_column(String(128))
    spec: Mapped[dict] = mapped_column(JSONB, default=dict)
    description: Mapped[str | None] = mapped_column(Text)
    actor: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PolicyCollection(Base):
    """A named group of policies (a "policy collection", à la Stacklet).

    Membership is many-to-many via ``collection_policies``; a policy may belong to
    any number of collections. Deleting a collection removes only its membership
    rows (``ON DELETE CASCADE`` on the join) — never the member policies.
    """

    __tablename__ = "policy_collections"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(256), unique=True)
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class CollectionPolicy(Base):
    """Join row binding a policy to a collection (composite PK, no duplicates).

    Both foreign keys are ``ON DELETE CASCADE`` so removing either side cleans up
    the membership without orphan rows — deleting a *collection* drops its
    memberships (policies survive); deleting a *policy* drops its memberships.
    """

    __tablename__ = "collection_policies"

    collection_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("policy_collections.id", ondelete="CASCADE"), primary_key=True
    )
    policy_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("policies.id", ondelete="CASCADE"), primary_key=True
    )
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PolicyExecution(Base):
    """One scheduled/triggered run of a policy and its outcome (M3.1).

    Mirrors the ``runs``/:class:`Run` lifecycle: a row is created ``running`` when
    a policy fires, then ``finish_policy_execution`` stamps ``finished_at`` and a
    terminal ``status`` (``succeeded``/``failed``). ``resources_matched`` and the
    ``actions_taken`` JSONB summarise what Cloud Custodian did; per-resource detail
    lives in :class:`PolicyMatch`. ``policy_id`` references the real ``policies.id``
    PK (the M2 policy PK is the autoincrement ``id``, not a string ``policy_id``).
    """

    __tablename__ = "policy_executions"

    execution_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    policy_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("policies.id"), index=True)
    subscription_id: Mapped[str | None] = mapped_column(String(64), index=True)
    # The binding that triggered this execution (M5.3), if any. ON DELETE SET NULL so
    # the audit trail survives a binding being deleted.
    binding_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("bindings.id", ondelete="SET NULL"), index=True
    )
    status: Mapped[str] = mapped_column(String(16), default="running")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resources_matched: Mapped[int] = mapped_column(Integer, default=0)
    actions_taken: Mapped[list] = mapped_column(JSONB, default=list)
    error: Mapped[str | None] = mapped_column(Text)


class PolicyMatch(Base):
    """A single resource a policy matched during a :class:`PolicyExecution` (M3.1).

    ``execution_id`` is freshly minted per run so there is no conflict risk — rows
    are plain inserts (no upsert). ``action_result`` holds the structured outcome
    of any action, the same JSONB-payload pattern as ``RemediationAction.result``.
    """

    __tablename__ = "policy_matches"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    execution_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("policy_executions.execution_id"), index=True
    )
    resource_id: Mapped[str] = mapped_column(String(512), index=True)
    resource_type: Mapped[str | None] = mapped_column(String(256))
    matched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    action_taken: Mapped[str | None] = mapped_column(String(64))
    action_result: Mapped[dict] = mapped_column(JSONB, default=dict)


class Run(Base):
    __tablename__ = "runs"

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), default="running")
    subscription_id: Mapped[str | None] = mapped_column(String(64))
    metric_lookback_days: Mapped[int | None] = mapped_column(Integer)
    cost_lookback_days: Mapped[int | None] = mapped_column(Integer)
    provider_used: Mapped[str | None] = mapped_column(String(32))
    model: Mapped[str | None] = mapped_column(String(64))
    mock: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str | None] = mapped_column(Text)


class Resource(Base):
    __tablename__ = "resources"

    resource_id: Mapped[str] = mapped_column(String(512), primary_key=True)
    subscription_id: Mapped[str | None] = mapped_column(String(64))
    resource_group: Mapped[str | None] = mapped_column(String(256))
    name: Mapped[str | None] = mapped_column(String(256))
    type: Mapped[str | None] = mapped_column(String(256), index=True)
    location: Mapped[str | None] = mapped_column(String(64), index=True)
    sku: Mapped[str | None] = mapped_column(String(128))
    tags: Mapped[dict] = mapped_column(JSONB, default=dict)
    power_state: Mapped[str | None] = mapped_column(String(64))
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Asset(Base):
    """Queryable near-real-time inventory row (M4.1 — AssetDB).

    A richer superset of ``resources``: same identity/location/tags plus the full
    resource ``config`` (JSONB), a coarse ``state``, and first/last-seen stamps.
    Upserted idempotently on each ingestion; ``first_seen`` is set once.
    """

    __tablename__ = "assets"

    resource_id: Mapped[str] = mapped_column(String(512), primary_key=True)
    subscription_id: Mapped[str | None] = mapped_column(String(64), index=True)
    resource_group: Mapped[str | None] = mapped_column(String(256))
    name: Mapped[str | None] = mapped_column(String(256))
    type: Mapped[str | None] = mapped_column(String(256), index=True)
    location: Mapped[str | None] = mapped_column(String(64), index=True)
    sku: Mapped[str | None] = mapped_column(String(128))
    tags: Mapped[dict] = mapped_column(JSONB, default=dict)
    config: Mapped[dict] = mapped_column(JSONB, default=dict)
    state: Mapped[str | None] = mapped_column(String(64))
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AssetEvent(Base):
    """Append-only audit of asset lifecycle changes (M4.1) — who/how/when.

    ``resource_id`` is a plain indexed column (not an FK): events are an
    independent audit trail that must survive an asset being deleted.
    """

    __tablename__ = "asset_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    resource_id: Mapped[str] = mapped_column(String(512), index=True)
    subscription_id: Mapped[str | None] = mapped_column(String(64), index=True)
    event_type: Mapped[str] = mapped_column(String(32))
    data: Mapped[dict] = mapped_column(JSONB, default=dict)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AssetRelationship(Base):
    """A typed, directed edge between two assets (M4.3 — the graph dimension).

    Derived from resource ``config`` during ingestion: a managed disk's
    ``managedBy`` VM (``disk → vm``), a NIC's ``virtualMachine`` (``nic → vm``),
    a public IP's bound NIC (``ip → nic``). Like ``asset_events``, ``source_id`` /
    ``target_id`` are plain indexed columns (not FKs) so an edge can outlive
    either endpoint. The ``(source_id, target_id, kind)`` triple is unique, so
    re-deriving over unchanged inventory is idempotent.
    """

    __tablename__ = "asset_relationships"
    __table_args__ = (
        UniqueConstraint("source_id", "target_id", "kind", name="uq_asset_relationship"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_id: Mapped[str] = mapped_column(String(512), index=True)
    target_id: Mapped[str] = mapped_column(String(512), index=True)
    kind: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AccountGroup(Base):
    """A named group of subscriptions (accounts) — Stacklet account groups (M5.1).

    Membership is many-to-many via ``account_group_members``; a subscription may
    belong to any number of groups. Deleting a group removes only its membership
    rows (``ON DELETE CASCADE`` on the join) — never the member subscriptions.
    """

    __tablename__ = "account_groups"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(256), unique=True)
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AccountGroupMember(Base):
    """Join row binding a subscription to an account group (composite PK).

    Both foreign keys are ``ON DELETE CASCADE`` so removing either side cleans up
    the membership without orphan rows — deleting a *group* drops its memberships
    (subscriptions survive); deleting a *subscription* drops its memberships.
    """

    __tablename__ = "account_group_members"

    group_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("account_groups.id", ondelete="CASCADE"), primary_key=True
    )
    subscription_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("subscriptions.subscription_id", ondelete="CASCADE"),
        primary_key=True,
    )
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Binding(Base):
    """Links a policy collection to an account group with execution config (M5.2).

    Stacklet's core *binding* concept: which policies (``collection_id``) run against
    which accounts (``account_group_id``), how (``mode`` ``pull``/``event``), on what
    ``schedule`` (cron), and whether guarded (``dry_run``) / active (``enabled``).
    Both foreign keys are ``ON DELETE CASCADE`` so deleting either side drops the
    binding without orphan rows.
    """

    __tablename__ = "bindings"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    collection_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("policy_collections.id", ondelete="CASCADE"), index=True
    )
    account_group_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("account_groups.id", ondelete="CASCADE"), index=True
    )
    schedule: Mapped[str | None] = mapped_column(String(128))
    mode: Mapped[str] = mapped_column(String(16), default="pull")
    dry_run: Mapped[bool] = mapped_column(Boolean, default=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class CostSnapshot(Base):
    __tablename__ = "cost_snapshots"

    usage_date: Mapped[date] = mapped_column(Date, primary_key=True)
    resource_id: Mapped[str] = mapped_column(String(512), primary_key=True)
    meter_category: Mapped[str] = mapped_column(String(128), primary_key=True, default="")
    cost_type: Mapped[str] = mapped_column(String(16), primary_key=True, default="Amortized")
    subscription_id: Mapped[str | None] = mapped_column(String(64))
    resource_type: Mapped[str | None] = mapped_column(String(256), index=True)
    resource_group: Mapped[str | None] = mapped_column(String(256))
    location: Mapped[str | None] = mapped_column(String(64), index=True)
    service_name: Mapped[str | None] = mapped_column(String(128))
    cost: Mapped[float] = mapped_column(Numeric(18, 6), default=0)
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    collected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class UtilizationSample(Base):
    __tablename__ = "utilization_samples"

    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    resource_id: Mapped[str] = mapped_column(String(512), primary_key=True)
    metric_name: Mapped[str] = mapped_column(String(64), primary_key=True)
    avg: Mapped[float | None] = mapped_column(Numeric(18, 4))
    min: Mapped[float | None] = mapped_column(Numeric(18, 4))
    max: Mapped[float | None] = mapped_column(Numeric(18, 4))
    unit: Mapped[str | None] = mapped_column(String(32))
    granularity: Mapped[str] = mapped_column(String(16), default="PT1H")


class UtilizationRollup(Base):
    __tablename__ = "utilization_rollups"

    resource_id: Mapped[str] = mapped_column(String(512), primary_key=True)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    cpu_avg: Mapped[float | None] = mapped_column(Numeric(9, 3))
    cpu_p95: Mapped[float | None] = mapped_column(Numeric(9, 3))
    cpu_max: Mapped[float | None] = mapped_column(Numeric(9, 3))
    mem_avg: Mapped[float | None] = mapped_column(Numeric(9, 3))
    mem_p95: Mapped[float | None] = mapped_column(Numeric(9, 3))
    mem_available: Mapped[bool] = mapped_column(Boolean, default=False)
    net_bytes_day: Mapped[float | None] = mapped_column(Numeric(20, 2))
    disk_iops_avg: Mapped[float | None] = mapped_column(Numeric(14, 3))
    sample_count: Mapped[int] = mapped_column(Integer, default=0)
    data_completeness: Mapped[float] = mapped_column(Numeric(5, 3), default=0)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class AdvisorRecommendation(Base):
    __tablename__ = "advisor_recommendations"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    resource_id: Mapped[str | None] = mapped_column(String(512), index=True)
    category: Mapped[str | None] = mapped_column(String(64))
    impact: Mapped[str | None] = mapped_column(String(32))
    problem: Mapped[str | None] = mapped_column(Text)
    solution: Mapped[str | None] = mapped_column(Text)
    recommended_sku: Mapped[str | None] = mapped_column(String(128))
    annual_savings: Mapped[float | None] = mapped_column(Numeric(18, 4))
    extended_properties: Mapped[dict] = mapped_column(JSONB, default=dict)
    collected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Recommendation(Base):
    __tablename__ = "recommendations"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), ForeignKey("runs.run_id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    resource_id: Mapped[str] = mapped_column(String(512), index=True)
    category: Mapped[str] = mapped_column(String(32))
    action: Mapped[str | None] = mapped_column(String(64))
    current_sku: Mapped[str | None] = mapped_column(String(128))
    recommended_sku: Mapped[str | None] = mapped_column(String(128))
    risk: Mapped[str] = mapped_column(String(16), default="medium")
    confidence: Mapped[float] = mapped_column(Numeric(4, 3), default=0.5)
    est_monthly_savings: Mapped[float] = mapped_column(Numeric(18, 4), default=0)
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    source: Mapped[str] = mapped_column(String(16), default="heuristic")
    priority: Mapped[int] = mapped_column(Integer, default=100)
    rationale: Mapped[str | None] = mapped_column(Text)
    caveats: Mapped[list] = mapped_column(JSONB, default=list)
    evidence: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="open")
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_by: Mapped[str | None] = mapped_column(String(128))


class RemediationAction(Base):
    __tablename__ = "remediation_actions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    recommendation_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("recommendations.id"), index=True
    )
    action_type: Mapped[str] = mapped_column(String(64))
    params: Mapped[dict] = mapped_column(JSONB, default=dict)
    dry_run: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[dict] = mapped_column(JSONB, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    actor: Mapped[str | None] = mapped_column(String(128))


class AISummary(Base):
    __tablename__ = "ai_summaries"

    run_id: Mapped[str] = mapped_column(String(64), ForeignKey("runs.run_id"), primary_key=True)
    executive_summary: Mapped[str | None] = mapped_column(Text)
    total_potential_savings: Mapped[float] = mapped_column(Numeric(18, 4), default=0)
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    top_actions: Mapped[list] = mapped_column(JSONB, default=list)
    provider: Mapped[str | None] = mapped_column(String(32))
    model: Mapped[str | None] = mapped_column(String(64))
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
