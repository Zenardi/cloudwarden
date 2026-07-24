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
    provider: str = "azure"  # owning cloud (M12.2 multi-cloud); azure|aws|…
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
    # Owning cloud (M14.11 multi-cloud cost parity). Every collector stamps its
    # own cloud so a cost row is self-describing; defaults to ``azure`` for the
    # pre-existing Azure path (mirrors the server_default backfill on the table).
    provider: str = "azure"
    resource_type: str | None = None
    resource_group: str | None = None
    location: str | None = None
    service_name: str | None = None
    meter_category: str | None = None
    cost: float = 0.0
    currency: str = "USD"
    cost_type: str = "Amortized"  # Actual | Amortized
    # Resource tags (enriched from inventory) — the showback/chargeback dimension (M14.5).
    tags: dict[str, str] = Field(default_factory=dict)


class KubeCluster(BaseModel):
    """A managed Kubernetes cluster discovered behind a cloud provider (M14.12).

    ``node_monthly_cost`` is the cluster's compute-node bill — the pool total the
    namespace allocation splits by requested resources. ``provider`` is the owning
    cloud (aws=EKS | azure=AKS | gcp=GKE); the K8s resources it contains are tagged
    ``provider="kubernetes"`` in AssetDB so they form their own inventory dimension."""

    cluster_id: str
    name: str
    provider: str = "aws"  # owning cloud: aws (EKS) | azure (AKS) | gcp (GKE)
    region: str | None = None
    version: str | None = None
    node_count: int = 0
    node_monthly_cost: float = 0.0
    currency: str = "USD"
    account_id: str | None = None  # owning subscription / account / project
    config: dict = Field(default_factory=dict)


class KubeWorkload(BaseModel):
    """A workload (Deployment/StatefulSet/DaemonSet) with aggregated per-pod
    resource requests/limits (M14.12). CPU in cores, memory in GiB."""

    cluster_id: str
    namespace: str
    name: str
    kind: str = "Deployment"
    replicas: int = 1
    cpu_request: float = 0.0
    mem_request: float = 0.0
    cpu_limit: float = 0.0
    mem_limit: float = 0.0
    config: dict = Field(default_factory=dict)


class KubeUsage(BaseModel):
    """Observed actual usage for a workload over the window (M14.12), e.g. from
    metrics-server / Prometheus. CPU in cores, memory in GiB.

    ``samples == 0`` (or no row at all) means no usage was observed — the
    right-sizing / idle detectors treat it as *unknown*, never as idle."""

    cluster_id: str
    namespace: str
    workload: str
    cpu_used: float = 0.0
    mem_used: float = 0.0
    samples: int = 0


class NamespaceCost(BaseModel):
    """One namespace's allocated slice of a cluster's node cost (M14.12).

    The allocation partitions the cluster node cost by requested resources, so
    ``sum(cost)`` over a cluster's namespaces reconciles to its ``node_monthly_cost``
    and ``sum(share) == 1``. This is *allocated* (not metered) cost."""

    cluster_id: str
    namespace: str
    cpu_request: float = 0.0
    mem_request: float = 0.0
    cost: float = 0.0
    share: float = 0.0
    currency: str = "USD"


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


class ActivitySignal(BaseModel):
    """Total of a resource's primary Azure Monitor *platform* metric over the
    analysis window (e.g. Bastion ``sessions``, storage ``Transactions``).

    Platform metrics are always-on and need no diagnostic settings, so this works
    for resource types that emit no guest metrics. ``datapoints == 0`` means the
    query returned nothing (unknown state) — which the idle detector treats as
    "cannot conclude", distinct from ``total == 0`` *with* datapoints (a resource
    that was observed and genuinely had no activity)."""

    resource_id: str
    metric_name: str
    total: float = 0.0
    datapoints: int = 0


class CommitmentRecord(BaseModel):
    """An existing Reservation (RI) or Savings Plan (SP) captured for coverage
    analysis (M14.1). ``hourly_committed`` is the on-demand-equivalent spend the
    commitment covers per hour; ``utilization_pct`` is its reported utilization
    (0..100). Provider-tagged so AWS/GCP can follow behind the same abstraction."""

    commitment_id: str
    provider: str = "azure"
    kind: str = "reservation"  # reservation | savings_plan
    display_name: str | None = None
    scope: str = "Shared"  # Shared | Single
    region: str | None = None
    sku_family: str | None = None
    term: str = "P1Y"  # P1Y | P3Y
    utilization_pct: float = 0.0  # 0..100
    expiry_date: date | None = None
    hourly_committed: float = 0.0
    currency: str = "USD"
    config: dict = Field(default_factory=dict)


class SteadyStateUsage(BaseModel):
    """Aggregated eligible on-demand usage for one SKU family in one region (M14.1).

    ``window_hourly`` is the per-day observed running level of *uncovered* eligible
    on-demand spend ($/hr), one entry per day of the analysis window. Aggregated
    (never raw samples) to keep AI token cost bounded; the detector sizes a safe
    commitment at the window minimum (the level present every single day)."""

    provider: str = "azure"
    sku_family: str
    region: str
    window_hourly: list[float] = Field(default_factory=list)
    currency: str = "USD"


class CommitmentSignals(BaseModel):
    """What the reservations collector returns: existing commitments + eligible
    steady-state usage (M14.1). Empty for non-Azure providers (no-op stub)."""

    provider: str = "azure"
    commitments: list[CommitmentRecord] = Field(default_factory=list)
    steady_state: list[SteadyStateUsage] = Field(default_factory=list)


class CommitmentCoverage(BaseModel):
    """Per SKU-family/region commitment coverage & utilization rollup (M14.1).

    ``coverage_pct`` is the share of eligible steady-state spend already covered by
    a commitment; ``utilization_pct`` is the blended utilization of the commitments
    in that family/region (None when none exist)."""

    provider: str = "azure"
    sku_family: str
    region: str
    eligible_monthly: float = 0.0
    committed_monthly: float = 0.0
    coverage_pct: float = 0.0
    utilization_pct: float | None = None
    currency: str = "USD"


class Recommendation(BaseModel):
    resource_id: str
    # shutdown|downsize|delete_orphan|idle_disk|idle_ip|empty_asp|
    # stopped_vm|idle_by_activity|commitment|investigate
    category: str
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


class EvaluateIacRequest(BaseModel):
    """Inbound shape for the shift-left IaC evaluation endpoint (M14.6).

    ``plan`` is a parsed Terraform plan JSON (``terraform show -json``). Permissive
    (defaults to ``{}``) so a malformed plan returns a clean ``422`` from the evaluator
    rather than a framework validation error. ``fail_on`` is the optional severity gate.
    """

    plan: dict = Field(default_factory=dict)
    fail_on: str | None = None


class DriftBaselineRequest(BaseModel):
    """Inbound shape for re-baselining a resource's desired state (M14.7).

    ``resource_id`` names the asset to snapshot; the endpoint reads its current config,
    captures a new baseline (clearing open drift), and records an audit entry."""

    resource_id: str


class WaiverRequest(BaseModel):
    """Inbound shape for requesting a policy waiver (M14.9).

    ``policy_id`` is the policy to exempt; ``justification`` is mandatory (validated
    non-blank server-side); ``expires_at`` must be in the future. ``scope_type`` narrows
    the exemption — ``policy`` (whole policy), ``resource`` / ``resource_group`` / ``tag``
    with ``scope_value`` (a resource id, an RG name, or a ``key=value`` pair)."""

    policy_id: int
    justification: str
    expires_at: datetime
    scope_type: str = "policy"  # policy | resource | resource_group | tag
    scope_value: str | None = None


class GuardrailRequest(BaseModel):
    """Inbound shape for previewing/applying a preventive guardrail (M14.10).

    ``policy_id`` names the authored policy to translate into a native deny construct;
    ``provider`` selects the target cloud (``azure`` / ``aws`` / ``gcp``); ``scope`` is
    the optional target (subscription / OU-root / organization). ``dry_run`` applies to
    the apply endpoint only (dry-run-first): a real apply also requires the remediation
    guardrails to permit a live write."""

    policy_id: int
    provider: str = "azure"
    scope: str | None = None
    dry_run: bool = True


class ValidateResult(BaseModel):
    """Outcome of validating a policy spec: ``valid`` plus any error strings."""

    valid: bool = False
    errors: list[str] = Field(default_factory=list)


class BudgetCreate(BaseModel):
    """Inbound budget definition (M14.2). ``thresholds`` are ordered rules, each
    ``{"pct": <float>, "basis": "actual"|"forecast"}`` (basis defaults to actual)."""

    name: str
    amount: float
    scope_type: str = "subscription"  # subscription | account | account_group | tag | team
    scope_value: str | None = None
    period: str = "monthly"  # monthly | quarterly
    currency: str = "USD"
    thresholds: list[dict] = Field(default_factory=lambda: [{"pct": 80}, {"pct": 100}])
    channel_id: int | None = None
    template_id: int | None = None
    enabled: bool = True


class BudgetUpdate(BaseModel):
    """Partial budget update — every field optional (M14.2)."""

    name: str | None = None
    amount: float | None = None
    scope_type: str | None = None
    scope_value: str | None = None
    period: str | None = None
    currency: str | None = None
    thresholds: list[dict] | None = None
    channel_id: int | None = None
    template_id: int | None = None
    enabled: bool | None = None


class PolicyCreate(BaseModel):
    """Inbound shape for creating a governance policy (API validation)."""

    name: str
    resource_type: str
    spec: dict = Field(default_factory=dict)
    description: str | None = None
    source: str = "custom"  # custom | library | imported
    # Optional owning team (M11.2). When omitted the team is derived from the caller's
    # membership; when set, the caller must be an admin or a member of that team.
    team: str | None = None


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
