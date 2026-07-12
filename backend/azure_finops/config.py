"""Typed application configuration (single source of truth).

All values come from environment variables (see `.env.example`). Field names map
to UPPER_SNAKE env keys case-insensitively via pydantic-settings.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # --- Azure subscription ---
    azure_subscription_id: str = "00000000-0000-0000-0000-000000000000"

    # --- Read-only SP (collection) ---
    azure_tenant_id: str | None = None
    azure_client_id: str | None = None
    azure_client_secret: str | None = None

    # --- Write-scoped SP (remediation) ---
    azure_remediation_tenant_id: str | None = None
    azure_remediation_client_id: str | None = None
    azure_remediation_client_secret: str | None = None

    # --- Memory metrics (optional) ---
    log_analytics_workspace_id: str | None = None

    # --- AI provider ---
    ai_provider: str = "anthropic"  # anthropic | openai
    ai_model: str = "claude-opus-4-8"
    anthropic_api_key: str | None = None
    ai_api_key: str | None = None
    ai_base_url: str | None = None
    ai_max_candidates: int = 40
    ai_max_tokens: int = 8000

    # --- Database ---
    database_url: str = "postgresql+psycopg://finops:finops@localhost:5432/finops"

    # --- Analysis windows & thresholds ---
    metric_lookback_days: int = 14
    cost_lookback_days: int = 30
    min_data_completeness: float = 0.8
    shutdown_cpu_p95: float = 3.0
    shutdown_cpu_max: float = 5.0
    downsize_cpu_p95: float = 40.0
    downsize_cpu_max: float = 80.0
    downsize_mem_p95: float = 50.0

    # --- GitOps policy sync ---
    gitops_repo_url: str = ""  # empty disables sync
    gitops_branch: str = "main"
    gitops_policy_path: str = "policies"  # path within the repo holding policy YAML

    # --- Remediation guardrails ---
    remediation_enabled: bool = False
    allowed_resource_groups: str = ""
    exclude_tag: str = "finops:exclude"

    # --- Runtime ---
    finops_mock: bool = True
    run_interval_seconds: int = 86400
    # Pull-mode policy execution runs on its own cadence, independent of the
    # cost-collection pipeline above (Stacklet-style per-policy scheduling).
    policy_run_interval_seconds: int = 86400
    app_data_dir: str = "/data"

    # --- Derived helpers ---
    @property
    def resolved_ai_key(self) -> str | None:
        return self.ai_api_key or self.anthropic_api_key

    @property
    def allowed_rg_list(self) -> list[str]:
        return [x.strip() for x in self.allowed_resource_groups.split(",") if x.strip()]

    @property
    def exclude_tag_kv(self) -> tuple[str, str] | None:
        if ":" in self.exclude_tag:
            key, value = self.exclude_tag.split(":", 1)
            return key.strip(), value.strip()
        return None


@lru_cache
def get_settings() -> Settings:
    return Settings()
