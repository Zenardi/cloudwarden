"""Pydantic domain models passed between collectors, analysis, AI and storage.

These are transport/in-memory shapes; SQLAlchemy ORM rows (storage/schema.py)
persist them. Keeping them separate lets collectors be unit-tested against
fixtures without a database.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, Field


class ResourceRecord(BaseModel):
    resource_id: str
    name: str
    type: str
    location: str
    resource_group: str
    subscription_id: str
    sku: str | None = None
    tags: dict[str, str] = Field(default_factory=dict)
    power_state: str | None = None
    extra: dict = Field(default_factory=dict)
    config: dict = Field(default_factory=dict)  # full resource properties (AssetDB, M4.1)


class AssetFilter(BaseModel):
    """One allow-listed, parameterized filter clause for an asset query (M4.2).

    ``column`` and ``op`` are validated against server-side allow-lists in
    ``repository.query_assets``; ``value`` is always bound as a parameter (never
    interpolated into SQL), so an injection payload is treated as a literal.
    """

    column: str
    op: str = "eq"  # eq | ne | contains | in
    value: Any = None


class AssetQuery(BaseModel):
    """Structured, injection-safe asset query request (M4.2)."""

    filters: list[AssetFilter] = Field(default_factory=list)
    tags: dict[str, str] = Field(default_factory=dict)  # exact tag key→value match
    limit: int = 100
    offset: int = 0


class CostRow(BaseModel):
    usage_date: date
    resource_id: str | None = None
    subscription_id: str | None = None
    resource_type: str | None = None
    resource_group: str | None = None
    location: str | None = None
    service_name: str | None = None
    meter_category: str | None = None
    cost: float = 0.0
    currency: str = "USD"
    cost_type: str = "Amortized"  # Actual | Amortized


class MetricSample(BaseModel):
    resource_id: str
    metric_name: str
    ts: datetime
    avg: float | None = None
    min: float | None = None
    max: float | None = None
    unit: str | None = None
    granularity: str = "PT1H"


class UtilizationRollup(BaseModel):
    resource_id: str
    window_start: datetime
    window_end: datetime
    cpu_avg: float | None = None
    cpu_p95: float | None = None
    cpu_max: float | None = None
    mem_avg: float | None = None
    mem_p95: float | None = None
    mem_available: bool = False
    net_bytes_day: float | None = None
    disk_iops_avg: float | None = None
    sample_count: int = 0
    data_completeness: float = 0.0


class Recommendation(BaseModel):
    resource_id: str
    category: str  # shutdown | downsize | delete_orphan | idle_ip | empty_asp
    action: str
    current_sku: str | None = None
    recommended_sku: str | None = None
    risk: str = "medium"  # low | medium | high
    confidence: float = 0.5
    est_monthly_savings: float = 0.0
    currency: str = "USD"
    source: str = "heuristic"  # heuristic | advisor | ai | combined
    priority: int = 100
    rationale: str = ""
    caveats: list[str] = Field(default_factory=list)
    evidence: dict = Field(default_factory=dict)


class ValidateRequest(BaseModel):
    """Inbound shape for the dry-run policy-validation endpoint.

    ``spec`` is a parsed Cloud Custodian policy collection — i.e. a mapping with a
    ``policies`` list. Kept permissive (defaults to ``{}``) so the endpoint can
    return a clean ``400`` for a malformed body rather than a framework ``422``.
    """

    spec: dict = Field(default_factory=dict)


class ValidateResult(BaseModel):
    """Outcome of validating a policy spec: ``valid`` plus any error strings."""

    valid: bool = False
    errors: list[str] = Field(default_factory=list)


class PolicyCreate(BaseModel):
    """Inbound shape for creating a governance policy (API validation)."""

    name: str
    resource_type: str
    spec: dict = Field(default_factory=dict)
    description: str | None = None
    source: str = "custom"  # custom | library | imported


class PolicyUpdate(BaseModel):
    """Inbound shape for updating a policy — every field optional (partial update).

    When ``spec`` is provided it is re-validated before the write; the other fields
    are applied as-is. Omitted fields are left unchanged.
    """

    name: str | None = None
    resource_type: str | None = None
    spec: dict | None = None
    description: str | None = None


class PolicyRecord(BaseModel):
    """A persisted governance policy as returned by the repository."""

    id: int
    name: str
    resource_type: str
    spec: dict = Field(default_factory=dict)
    description: str | None = None
    enabled: bool = True
    version: int = 1
    source: str = "custom"
    created_at: str | None = None
    updated_at: str | None = None


class PolicyVersionRecord(BaseModel):
    """An immutable snapshot of a policy at one version (M2.5 history)."""

    policy_id: int
    version: int
    name: str
    resource_type: str
    spec: dict = Field(default_factory=dict)
    description: str | None = None
    actor: str | None = None
    created_at: str | None = None


class PolicyMatch(BaseModel):
    """A resource a policy matched during an execution (transport shape, M3.1).

    The parent ``execution_id`` is supplied to ``insert_policy_matches`` rather than
    carried here, mirroring how ``Recommendation`` omits its ``run_id``.
    """

    resource_id: str
    resource_type: str | None = None
    action_taken: str | None = None
    action_result: dict = Field(default_factory=dict)


class PolicyExecution(BaseModel):
    """One policy run and its outcome (transport shape, M3.1).

    Mirrors the ``policy_executions`` ORM row 1:1 so the M3.2 orchestrator can build
    results without importing SQLAlchemy. Timestamps are ISO-8601 strings (as the
    repository serializes them); the store assigns them server-side.
    """

    execution_id: str
    policy_id: int
    subscription_id: str | None = None
    status: str = "running"
    started_at: str | None = None
    finished_at: str | None = None
    resources_matched: int = 0
    actions_taken: list = Field(default_factory=list)
    error: str | None = None


class CollectionCreate(BaseModel):
    """Inbound shape for creating a policy collection."""

    name: str
    description: str | None = None


class AccountGroupCreate(BaseModel):
    """Inbound shape for creating an account group (M5.1)."""

    name: str
    description: str | None = None


class BindingIn(BaseModel):
    """Inbound shape for creating a binding (M5.2) — collection × account group + config.

    ``mode`` is validated against ``{pull, event}`` in the repository (a bad value is a
    ``400``, not a ``422``). Defaults are guarded: ``dry_run`` and ``enabled`` are true.
    """

    collection_id: int
    account_group_id: int
    schedule: str | None = None
    mode: str = "pull"
    dry_run: bool = True
    enabled: bool = True


class BindingUpdate(BaseModel):
    """Partial update for a binding (M5.2). Only the fields sent are changed."""

    schedule: str | None = None
    mode: str | None = None
    dry_run: bool | None = None
    enabled: bool | None = None


class NotificationChannelIn(BaseModel):
    """Inbound shape for creating a dispatch channel (M8.4).

    ``transport`` is validated against the known transport registry (a bad value is a
    ``400``); ``target`` must be non-empty. ``config`` holds transport-specific extras.
    """

    name: str
    target: str
    transport: str = "webhook"
    config: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class NotificationChannelUpdate(BaseModel):
    """Partial update for a channel (M8.4). Only the fields sent are changed."""

    name: str | None = None
    target: str | None = None
    transport: str | None = None
    config: dict[str, Any] | None = None
    enabled: bool | None = None


class NotificationTemplateIn(BaseModel):
    """Inbound shape for creating a communication template (M8.4)."""

    name: str
    body: str
    subject: str | None = None
    format: str = "text"
    description: str | None = None


class NotificationTemplateUpdate(BaseModel):
    """Partial update for a template (M8.4). Only the fields sent are changed."""

    name: str | None = None
    subject: str | None = None
    body: str | None = None
    format: str | None = None
    description: str | None = None


class BindingNotificationIn(BaseModel):
    """Attach a (channel, template) pair to a binding (M8.4)."""

    channel_id: int
    template_id: int


class CollectionRecord(BaseModel):
    """A persisted policy collection with its members, as returned by the repository."""

    id: int
    name: str
    description: str | None = None
    policy_count: int = 0
    policies: list[dict] = Field(default_factory=list)
    created_at: str | None = None
    updated_at: str | None = None


class AISummary(BaseModel):
    executive_summary: str = ""
    total_potential_monthly_savings: float = 0.0
    currency: str = "USD"
    recommendations: list[Recommendation] = Field(default_factory=list)
    provider: str | None = None
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
