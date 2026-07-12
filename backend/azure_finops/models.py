"""Pydantic domain models passed between collectors, analysis, AI and storage.

These are transport/in-memory shapes; SQLAlchemy ORM rows (storage/schema.py)
persist them. Keeping them separate lets collectors be unit-tested against
fixtures without a database.
"""

from __future__ import annotations

from datetime import date, datetime

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


class AISummary(BaseModel):
    executive_summary: str = ""
    total_potential_monthly_savings: float = 0.0
    currency: str = "USD"
    recommendations: list[Recommendation] = Field(default_factory=list)
    provider: str | None = None
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
