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
    # Owning cloud (M12.1 multi-cloud). ``server_default='azure'`` backfills any
    # pre-existing account rows so they read as Azure, matching prior behaviour.
    provider: Mapped[str] = mapped_column(
        String(32), default="azure", server_default="azure", index=True
    )
    # Optional lifecycle classification: one of Development / QA / Prod / Sandbox,
    # or NULL when unclassified. Drives the savings reclaim factor (non-prod idle
    # waste is safer to cut) — see analysis.savings.ENVIRONMENT_RECLAIM_FACTORS.
    environment: Mapped[str | None] = mapped_column(String(32), index=True)
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
    # The owning team (M11.2 multi-tenancy). NULL = unscoped/global (admin-only).
    # ON DELETE SET NULL so deleting a team leaves its policies as global, not orphaned.
    team_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("teams.id", ondelete="SET NULL"), index=True
    )
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


class InstalledPack(Base):
    """A policy pack that has been installed into a collection (M10.1).

    Installing a bundled pack (:mod:`cloudwarden.packs.registry`) materializes its
    policies (upsert-by-name, ``source='pack'``) and a collection named after the
    pack; this row records the installed ``version`` and that ``collection_id`` so
    re-installing the same version is a no-op and the UI can list what's installed.
    ``enabled`` gates *binding eligibility*: disabling a pack disables its member
    policies so they stop resolving into binding runs. Deleting the collection
    cascades this row away.
    """

    __tablename__ = "installed_packs"

    name: Mapped[str] = mapped_column(String(128), primary_key=True)
    version: Mapped[str] = mapped_column(String(32))
    collection_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("policy_collections.id", ondelete="CASCADE"), index=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    installed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class InstalledFramework(Base):
    """A compliance framework overlay installed via the pack registry (M14.13).

    A *framework overlay* maps controls (SOC 2 / ISO 27001 / PCI / NIST) to
    **existing** policies — it creates no policies, so unlike :class:`InstalledPack`
    it has no ``collection_id``. This row records the installed ``version`` and the
    control/gap counts; the per-control policy mappings land in
    :class:`FrameworkControl` (which cascades from here) so a per-framework posture
    can be queried in SQL (Grafana). Re-installing the same overlay upserts by name.
    """

    __tablename__ = "installed_frameworks"

    name: Mapped[str] = mapped_column(String(128), primary_key=True)
    version: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(256), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    control_count: Mapped[int] = mapped_column(Integer, default=0)
    mapped_count: Mapped[int] = mapped_column(Integer, default=0)
    gap_count: Mapped[int] = mapped_column(Integer, default=0)
    installed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class FrameworkControl(Base):
    """One control → policy mapping row of an installed framework overlay (M14.13).

    A control maps to zero-or-more policies: a mapped control has one row per policy
    (``policy_name`` set); an unmapped control (a coverage **gap**) has a single row
    with ``policy_name = NULL``. ``v_framework_posture`` joins these to the live
    ``v_governance_posture`` so per-control posture (and gaps) are queryable in SQL.
    Reinstalling replaces a framework's rows wholesale, so no unique constraint is
    needed. Deleting the parent :class:`InstalledFramework` cascades these away.
    """

    __tablename__ = "framework_controls"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    framework: Mapped[str] = mapped_column(
        String(128), ForeignKey("installed_frameworks.name", ondelete="CASCADE"), index=True
    )
    control_id: Mapped[str] = mapped_column(String(64), index=True)
    title: Mapped[str] = mapped_column(String(256), default="")
    policy_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    ordinal: Mapped[int] = mapped_column(Integer, default=0)


class Role(Base):
    """A named RBAC role (M11.1) — e.g. ``admin`` / ``editor`` / ``viewer``.

    A role owns a set of :class:`Permission` grants (action strings) and is assigned
    to principals via :class:`RoleBinding`. The default roles are seeded idempotently
    at startup; deleting a role cascades its permissions and bindings away.
    """

    __tablename__ = "roles"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Permission(Base):
    """One action a role may perform (M11.1) — a ``resource:verb`` string.

    ``action`` is an opaque permission token (e.g. ``policy:write``); the sentinel
    ``*`` grants everything (held by ``admin``). ``(role_id, action)`` is unique so a
    role never carries a duplicate grant. Rows cascade-delete with the role.
    """

    __tablename__ = "permissions"
    __table_args__ = (UniqueConstraint("role_id", "action", name="uq_role_permission"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    role_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("roles.id", ondelete="CASCADE"), index=True
    )
    action: Mapped[str] = mapped_column(String(64))


class RoleBinding(Base):
    """Assigns a role to a principal (M11.1) — the subject → role edge.

    ``principal`` is the caller identity (an ``X-Principal`` header today; an SSO
    subject once M11.3 lands). A principal may hold several roles; their permissions
    union. ``(principal, role_id)`` is unique so a binding is created at most once.
    Rows cascade-delete with the role.
    """

    __tablename__ = "role_bindings"
    __table_args__ = (UniqueConstraint("principal", "role_id", name="uq_principal_role"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    principal: Mapped[str] = mapped_column(String(256), index=True)
    role_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("roles.id", ondelete="CASCADE"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Team(Base):
    """A tenant/team that owns governance resources (M11.2 — multi-tenancy).

    Members (:class:`TeamMember`) see and manage only their team's resources; an
    admin (RBAC wildcard) sees across all teams. A scoped resource references its
    owning team via a nullable ``team_id`` (e.g. :attr:`Policy.team_id`); ``NULL``
    means unscoped/global (visible only to admins). Deleting a team cascades its
    membership away and nulls the ``team_id`` on any resource it owned.
    """

    __tablename__ = "teams"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TeamMember(Base):
    """Assigns a principal to a team (M11.2) — the subject → team edge.

    ``principal`` is the caller identity (the ``X-Principal`` header / an SSO subject
    once M11.3 lands); ``role`` is a free-form label for the member's standing within
    the team (``member`` by default). ``(team_id, principal)`` is unique so a principal
    is added to a team at most once. Rows cascade-delete with the team.
    """

    __tablename__ = "team_members"
    __table_args__ = (UniqueConstraint("team_id", "principal", name="uq_team_principal"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    team_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("teams.id", ondelete="CASCADE"), index=True
    )
    principal: Mapped[str] = mapped_column(String(256), index=True)
    role: Mapped[str] = mapped_column(String(32), default="member")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AuditLog(Base):
    """Append-only record of a mutating governance action (M11.4).

    Stacklet-style audit trail: every create/update/delete of a governance object
    writes one row capturing **who** (``actor`` — the resolved RBAC/SSO principal, or
    ``NULL`` when anonymous), **what** (``action`` like ``policy.update``), **which**
    (``target_type`` / ``target_id``), and the **before/after** state as JSONB (a create
    has an empty ``before``; a delete an empty ``after``). Rows are only ever inserted —
    there is no update or delete path, in the repository or the API — so the log is
    tamper-evident by construction. Read requests are never recorded.
    """

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    actor: Mapped[str | None] = mapped_column(String(256), index=True)
    action: Mapped[str] = mapped_column(String(64), index=True)
    target_type: Mapped[str] = mapped_column(String(64), index=True)
    target_id: Mapped[str | None] = mapped_column(String(256), index=True)
    before: Mapped[dict] = mapped_column(JSONB, default=dict)
    after: Mapped[dict] = mapped_column(JSONB, default=dict)
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


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
    # How this run was triggered: ``pull`` (scheduled/manual) or ``event`` (reactive,
    # M6.2 — an Event Grid delivery matched an event-mode policy's resource type).
    mode: Mapped[str] = mapped_column(String(16), default="pull")
    # The Event Grid delivery (``event_log.event_id``) that triggered this reactive run
    # (M6.4), if any — a plain indexed column so the status feed can link event → runs.
    event_id: Mapped[str | None] = mapped_column(String(128), index=True)
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
    provider: Mapped[str] = mapped_column(String(32), default="azure", server_default="azure")
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
    # Owning cloud (M12.2 multi-cloud). ``server_default='azure'`` backfills the
    # pre-existing (Azure) asset rows so multi-cloud queries can filter by provider.
    provider: Mapped[str] = mapped_column(
        String(32), default="azure", server_default="azure", index=True
    )
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


class EventLog(Base):
    """Audit log of Azure Event Grid deliveries (M6.1 — real-time enforcement ingress).

    ``event_id`` (Event Grid's delivery id) is uniquely indexed: Event Grid guarantees
    *at-least-once* delivery, so re-delivery must be tolerated — an idempotent
    ``ON CONFLICT (event_id) DO NOTHING`` upsert never double-logs.
    """

    __tablename__ = "event_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    event_type: Mapped[str] = mapped_column(String(128))
    subject: Mapped[str] = mapped_column(String(512))
    resource_id: Mapped[str | None] = mapped_column(String(512), index=True)
    subscription_id: Mapped[str | None] = mapped_column(String(64))
    event_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    status: Mapped[str] = mapped_column(String(16), default="received")
    raw: Mapped[dict] = mapped_column(JSONB, default=dict)


class CostSnapshot(Base):
    __tablename__ = "cost_snapshots"

    usage_date: Mapped[date] = mapped_column(Date, primary_key=True)
    resource_id: Mapped[str] = mapped_column(String(512), primary_key=True)
    meter_category: Mapped[str] = mapped_column(String(128), primary_key=True, default="")
    cost_type: Mapped[str] = mapped_column(String(16), primary_key=True, default="Amortized")
    subscription_id: Mapped[str | None] = mapped_column(String(64))
    # Owning cloud (M14.11 multi-cloud cost parity). ``server_default='azure'``
    # backfills pre-existing Azure cost rows so ``?provider=`` filtering (and the
    # Grafana provider template) work directly off the fact table, without a
    # subscriptions join. Not part of the natural key: resource ids are globally
    # unique across clouds, so provider is a descriptive tag, not an identity column.
    provider: Mapped[str] = mapped_column(
        String(32), default="azure", server_default="azure", index=True
    )
    resource_type: Mapped[str | None] = mapped_column(String(256), index=True)
    resource_group: Mapped[str | None] = mapped_column(String(256))
    location: Mapped[str | None] = mapped_column(String(64), index=True)
    service_name: Mapped[str | None] = mapped_column(String(128))
    cost: Mapped[float] = mapped_column(Numeric(18, 6), default=0)
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    # Resource tags (enriched from inventory) — the showback/chargeback cost dimension
    # (M14.5). Grouped by an arbitrary tag key via ``tags ->> :key`` (bound, injectable-safe).
    tags: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
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
    # The PolicyMatch this action enforces (M7.2), if it originated from a policy
    # run rather than a FinOps recommendation. Nullable — an action ties to one or
    # the other. Lets the approval workflow gate policy-derived actions.
    policy_match_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("policy_matches.id"), index=True
    )
    # Unified audit provenance (M7.4): where this action came from — "recommendation"
    # (a FinOps recommendation), "policy" (a policy run), or "binding" (a binding run).
    # ``policy_id`` denormalises the originating policy so the audit list can group /
    # filter by it without walking the match → execution → policy chain.
    source: Mapped[str] = mapped_column(String(32), default="recommendation", index=True)
    policy_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("policies.id"), index=True)
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


class NotificationTemplate(Base):
    """A communication template rendered from policy-violation context (M8.1).

    Stacklet / c7n-mailer heritage. ``subject`` and ``body`` are Jinja2 source
    strings rendered in a **sandboxed** environment (``notify/service.render``), so
    an authored template can reference the violation context (policy name, matched
    resource ids, a count) but never reach Python internals. ``format`` names the
    payload shape a transport should expect (``text`` / ``markdown`` / ``html``).
    """

    __tablename__ = "notification_templates"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(256), unique=True)
    subject: Mapped[str | None] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    format: Mapped[str] = mapped_column(String(16), default="text")
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class NotificationChannel(Base):
    """A dispatch destination and its transport kind (M8.1).

    ``transport`` names the delivery mechanism (``webhook`` / ``slack`` / ``email``);
    ``target`` is where that transport writes (a URL, an address); ``config`` holds
    transport-specific extras (e.g. a Slack channel). ``enabled`` gates delivery —
    a disabled channel renders but never dispatches.
    """

    __tablename__ = "notification_channels"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(256), unique=True)
    transport: Mapped[str] = mapped_column(String(32), default="webhook")
    target: Mapped[str] = mapped_column(String(1024))
    config: Mapped[dict] = mapped_column(JSONB, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class BindingNotification(Base):
    """Attaches a (channel, template) pair to a binding — fire-on-violation (M8.4).

    A binding may carry several of these; when a binding run records a policy match
    (a violation), each attached channel is dispatched with the paired template
    rendered from the violation context. A binding with **no** rows here dispatches
    nothing. All three foreign keys are ``ON DELETE CASCADE`` so removing a binding,
    channel or template drops the attachment without orphan rows. ``(binding_id,
    channel_id)`` is unique — a channel is attached to a binding at most once.
    """

    __tablename__ = "binding_notifications"
    __table_args__ = (UniqueConstraint("binding_id", "channel_id", name="uq_binding_channel"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    binding_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("bindings.id", ondelete="CASCADE"), index=True
    )
    channel_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("notification_channels.id", ondelete="CASCADE"), index=True
    )
    template_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("notification_templates.id", ondelete="CASCADE")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CommitmentInventory(Base):
    """An existing Reservation / Savings Plan captured for coverage analysis (M14.1).

    Upserted by ``commitment_id`` on each run so the table always reflects the
    current commitment portfolio (utilization/expiry/scope) without duplicate rows.
    ``config`` (JSONB) carries the raw provider payload; ``provider`` tags the owning
    cloud so AWS/GCP can follow behind the same abstraction.
    """

    __tablename__ = "commitment_inventory"

    commitment_id: Mapped[str] = mapped_column(String(512), primary_key=True)
    provider: Mapped[str] = mapped_column(
        String(32), default="azure", server_default="azure", index=True
    )
    kind: Mapped[str] = mapped_column(String(32), default="reservation")
    display_name: Mapped[str | None] = mapped_column(String(256))
    scope: Mapped[str] = mapped_column(String(32), default="Shared")
    region: Mapped[str | None] = mapped_column(String(64), index=True)
    sku_family: Mapped[str | None] = mapped_column(String(64), index=True)
    term: Mapped[str] = mapped_column(String(16), default="P1Y")
    utilization_pct: Mapped[float] = mapped_column(Numeric(5, 2), default=0)
    expiry_date: Mapped[date | None] = mapped_column(Date)
    hourly_committed: Mapped[float] = mapped_column(Numeric(18, 6), default=0)
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    config: Mapped[dict] = mapped_column(JSONB, default=dict)
    collected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class CommitmentCoverageRollup(Base):
    """Per SKU-family/region commitment coverage & utilization rollup (M14.1).

    One row per (run, family, region): how much eligible steady-state spend is
    already covered by a commitment and the blended utilization of those
    commitments. Replaced per ``run_id`` so a re-run never duplicates. ``config``
    (JSONB) holds any extra rollup context; ``provider`` tags the owning cloud.
    """

    __tablename__ = "commitment_coverage"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), ForeignKey("runs.run_id"), index=True)
    provider: Mapped[str] = mapped_column(
        String(32), default="azure", server_default="azure", index=True
    )
    sku_family: Mapped[str] = mapped_column(String(64), index=True)
    region: Mapped[str] = mapped_column(String(64))
    eligible_monthly: Mapped[float] = mapped_column(Numeric(18, 4), default=0)
    committed_monthly: Mapped[float] = mapped_column(Numeric(18, 4), default=0)
    coverage_pct: Mapped[float] = mapped_column(Numeric(6, 2), default=0)
    utilization_pct: Mapped[float | None] = mapped_column(Numeric(6, 2))
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    config: Mapped[dict] = mapped_column(JSONB, default=dict)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


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


class Budget(Base):
    """A spend budget over a scope + period, with ordered threshold rules (M14.2).

    ``scope_type``/``scope_value`` name what the budget covers (a subscription,
    account, account-group, tag value or team); ``period`` is ``monthly`` or
    ``quarterly``; ``amount`` is the limit in ``currency``. ``thresholds`` (JSONB) is
    the ordered rule list — each ``{"pct": <float>, "basis": "actual"|"forecast"}`` —
    normalised on write. A crossing notifies through ``channel_id`` (a
    ``notification_channels`` row) rendered from ``template_id`` (defaults to the
    seeded budget template); a budget with no channel evaluates silently.
    """

    __tablename__ = "budgets"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(256), unique=True)
    scope_type: Mapped[str] = mapped_column(String(32), default="subscription")
    scope_value: Mapped[str | None] = mapped_column(String(512), index=True)
    period: Mapped[str] = mapped_column(String(16), default="monthly")
    amount: Mapped[float] = mapped_column(Numeric(18, 4), default=0)
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    thresholds: Mapped[list] = mapped_column(JSONB, default=list)
    channel_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("notification_channels.id", ondelete="SET NULL")
    )
    template_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("notification_templates.id", ondelete="SET NULL")
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    config: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class BudgetThresholdEvent(Base):
    """One recorded threshold crossing for a budget in a period (M14.2).

    The natural key ``(budget_id, period_key, threshold_pct, basis)`` is unique — it
    is the dedupe marker that makes a crossing fire **exactly once** per period and
    threshold, even across re-evaluations or a scheduler tick racing a manual run.
    ``notified`` records whether a notification was actually dispatched for the
    crossing (the highest newly-crossed threshold), versus recorded only for dedupe.
    ``run_id`` is the run that detected it (nullable; the row outlives the run).
    """

    __tablename__ = "budget_threshold_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    budget_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("budgets.id", ondelete="CASCADE"), index=True
    )
    period_key: Mapped[str] = mapped_column(String(16), index=True)
    threshold_pct: Mapped[float] = mapped_column(Numeric(6, 2), default=0)
    basis: Mapped[str] = mapped_column(String(16), default="actual")
    amount: Mapped[float] = mapped_column(Numeric(18, 4), default=0)
    budget_amount: Mapped[float] = mapped_column(Numeric(18, 4), default=0)
    actual_pct: Mapped[float] = mapped_column(Numeric(8, 2), default=0)
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    run_id: Mapped[str | None] = mapped_column(String(64), index=True)
    notified: Mapped[bool] = mapped_column(Boolean, default=False)
    config: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint(
            "budget_id", "period_key", "threshold_pct", "basis", name="uq_budget_threshold_event"
        ),
    )


class CostAnomaly(Base):
    """A statistically abnormal day of spend for one scope (M14.3).

    ``scope_type``/``scope_value`` name the grain (a subscription, service,
    resource_type or resource); ``usage_date`` is the anomalous day. ``expected`` is
    the robust (weekday-deseasonalized) baseline, ``actual`` the measured spend, and
    ``score`` the deviation in robust-sigma (MAD) units bucketed into ``severity``.
    ``contributors`` (JSONB) is the ranked breakdown of the child rows that drove the
    delta. The natural key ``(scope_type, scope_value, usage_date)`` is unique — the
    idempotency marker that makes a new anomaly notify **exactly once**; ``notified``
    records whether an alert was actually dispatched (vs recorded only). ``provider``
    tags the owning cloud (Azure-first cost analytics); ``run_id`` is the detecting run.
    """

    __tablename__ = "cost_anomalies"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    scope_type: Mapped[str] = mapped_column(String(32), default="subscription", index=True)
    scope_value: Mapped[str] = mapped_column(String(512), index=True)
    usage_date: Mapped[date] = mapped_column(Date, index=True)
    provider: Mapped[str] = mapped_column(
        String(32), default="azure", server_default="azure", index=True
    )
    expected: Mapped[float] = mapped_column(Numeric(18, 6), default=0)
    actual: Mapped[float] = mapped_column(Numeric(18, 6), default=0)
    score: Mapped[float] = mapped_column(Numeric(10, 4), default=0)
    severity: Mapped[str] = mapped_column(String(16), default="low", index=True)
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    contributors: Mapped[list] = mapped_column(JSONB, default=list)
    run_id: Mapped[str | None] = mapped_column(String(64), index=True)
    notified: Mapped[bool] = mapped_column(Boolean, default=False)
    config: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "scope_type", "scope_value", "usage_date", name="uq_cost_anomaly_scope_date"
        ),
    )


class CostForecast(Base):
    """A projection of spend to a period end for one scope (M14.4).

    ``scope_type``/``scope_value`` name the grain (``total`` → the whole tenant with
    an empty ``scope_value``; else a subscription or service); ``horizon`` is
    ``month_end`` or ``quarter_end`` and ``as_of`` the day it was computed.
    ``point`` is the projected period total, bracketed by ``[lower, upper]``;
    ``actual_to_date`` is the spend already booked and ``projected`` the remaining-days
    portion. ``mape`` is the rolling-backtest accuracy (nullable — absent on
    thin-history estimates), ``model`` the fit used (``seasonal_trend`` / ``linear`` /
    ``linear_low_confidence``) and ``confidence`` its ``high``/``low`` label. The
    natural key ``(scope_type, scope_value, horizon, as_of)`` is unique — one forecast
    per grain, horizon and day, refreshed idempotently on a same-day re-run.
    """

    __tablename__ = "cost_forecasts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    scope_type: Mapped[str] = mapped_column(String(32), default="total", index=True)
    scope_value: Mapped[str] = mapped_column(String(512), default="", index=True)
    horizon: Mapped[str] = mapped_column(String(16), default="month_end", index=True)
    as_of: Mapped[date] = mapped_column(Date, index=True)
    period_end: Mapped[date] = mapped_column(Date)
    provider: Mapped[str] = mapped_column(
        String(32), default="azure", server_default="azure", index=True
    )
    point: Mapped[float] = mapped_column(Numeric(18, 6), default=0)
    lower: Mapped[float] = mapped_column(Numeric(18, 6), default=0)
    upper: Mapped[float] = mapped_column(Numeric(18, 6), default=0)
    actual_to_date: Mapped[float] = mapped_column(Numeric(18, 6), default=0)
    projected: Mapped[float] = mapped_column(Numeric(18, 6), default=0)
    mape: Mapped[float | None] = mapped_column(Numeric(10, 4))
    model: Mapped[str] = mapped_column(String(32), default="seasonal_trend")
    confidence: Mapped[str] = mapped_column(String(16), default="high", index=True)
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    run_id: Mapped[str | None] = mapped_column(String(64), index=True)
    config: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "scope_type",
            "scope_value",
            "horizon",
            "as_of",
            name="uq_cost_forecast_scope_horizon_asof",
        ),
    )


class DriftBaseline(Base):
    """A resource's desired-state configuration baseline (M14.7).

    ``config`` is the *normalized* config snapshot (volatile fields dropped) and
    ``config_hash`` its stable digest; ``version`` bumps each time an operator
    re-baselines with a materially different config. One baseline per ``resource_id``
    (the primary key); ``captured_by`` records who last set it. ``provider`` tags the
    owning cloud.
    """

    __tablename__ = "drift_baselines"

    resource_id: Mapped[str] = mapped_column(String(512), primary_key=True)
    provider: Mapped[str] = mapped_column(
        String(32), default="azure", server_default="azure", index=True
    )
    version: Mapped[int] = mapped_column(Integer, default=1)
    config_hash: Mapped[str] = mapped_column(String(64), index=True)
    config: Mapped[dict] = mapped_column(JSONB, default=dict)
    captured_by: Mapped[str | None] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class DriftFinding(Base):
    """A recorded configuration drift for one resource against its baseline (M14.7).

    ``changes`` (JSONB) is the classified diff — a list of
    ``{path, kind (added|removed|changed), old, new}`` — with ``added``/``removed``/
    ``changed`` counts; ``events`` (JSONB) is the attributed Activity-Log change events.
    The natural key ``(resource_id, baseline_version, changes_hash)`` is unique — the
    idempotency marker so re-detecting the same drift updates rather than duplicates and a
    new drift notifies **once**. ``status`` is ``open`` until a re-baseline resolves it;
    ``notified`` records whether an alert was dispatched. ``run_id`` is the detecting run.
    """

    __tablename__ = "drift_findings"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    resource_id: Mapped[str] = mapped_column(String(512), index=True)
    provider: Mapped[str] = mapped_column(
        String(32), default="azure", server_default="azure", index=True
    )
    baseline_version: Mapped[int] = mapped_column(Integer, default=1)
    changes_hash: Mapped[str] = mapped_column(String(64), index=True)
    changes: Mapped[list] = mapped_column(JSONB, default=list)
    added: Mapped[int] = mapped_column(Integer, default=0)
    removed: Mapped[int] = mapped_column(Integer, default=0)
    changed: Mapped[int] = mapped_column(Integer, default=0)
    events: Mapped[list] = mapped_column(JSONB, default=list)
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)
    notified: Mapped[bool] = mapped_column(Boolean, default=False)
    run_id: Mapped[str | None] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "resource_id", "baseline_version", "changes_hash", name="uq_drift_finding"
        ),
    )


class Waiver(Base):
    """A scoped, justified, approved, **expiring** exception to a policy (M14.9).

    A waiver suppresses enforcement for the resources a policy matches within its scope:
    the whole policy (``scope_type='policy'``), one resource (``'resource'`` +
    ``scope_value`` = resource id), a resource group (``'resource_group'`` + RG name) or a
    tag (``'tag'`` + ``key=value``). ``state`` walks ``pending`` → ``active`` (on approval)
    → ``expired`` (a reconcile pass once ``expires_at`` passes) — or ``rejected``. Only an
    ``active`` **and** unexpired waiver suppresses; ``requester``/``approver`` capture who
    asked and who granted, and ``justification`` is mandatory. ``notified_expiring`` dedupes
    the expiring-soon alert so it fires **once**. Rows cascade-delete with the policy.
    """

    __tablename__ = "waivers"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    policy_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("policies.id", ondelete="CASCADE"), index=True
    )
    scope_type: Mapped[str] = mapped_column(String(32), default="policy")
    scope_value: Mapped[str | None] = mapped_column(String(512))
    justification: Mapped[str] = mapped_column(Text)
    requester: Mapped[str | None] = mapped_column(String(256), index=True)
    approver: Mapped[str | None] = mapped_column(String(256))
    state: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notified_expiring: Mapped[bool] = mapped_column(Boolean, default=False)
    config: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
