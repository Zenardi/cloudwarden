"""Pydantic contract for the AI layer.

`AIResult` is validated against the model's JSON output (the LLM returns the
first four fields); the provider fills the trailing metadata fields after parse.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class AIRecommendation(BaseModel):
    resource_id: str
    action: str | None = None
    priority: int | None = None
    risk: str | None = None
    confidence: float | None = None
    est_monthly_savings: float | None = None
    rationale: str | None = None


class AIResult(BaseModel):
    executive_summary: str = ""
    total_potential_monthly_savings: float = 0.0
    currency: str = "USD"
    recommendations: list[AIRecommendation] = Field(default_factory=list)

    # provider metadata (set by the provider, not the LLM)
    provider: str | None = None
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
