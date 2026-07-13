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

    # --- Event Grid (real-time enforcement) ---
    # Master switch for event-mode ingestion (M6.4). When false, `POST /api/events/azure`
    # accepts deliveries with 202 but stores/triggers nothing — a clean way to pause
    # real-time enforcement without tearing down the Event Grid subscription.
    event_mode_enabled: bool = True
    # Optional shared key for authenticating Event Grid deliveries. Empty/unset accepts
    # all deliveries (local/mock dev); when set, a delivery must present it via the
    # `x-events-key` header or `?key=` query param or it is rejected with 403.
    azure_eventgrid_shared_key: str | None = None

    # --- AI provider ---
    ai_provider: str = "anthropic"  # anthropic | openai
    ai_model: str = "claude-opus-4-8"
    anthropic_api_key: str | None = None
    ai_api_key: str | None = None
    ai_base_url: str | None = None
    ai_max_candidates: int = 40
    ai_max_tokens: int = 8000

    # --- Notification transports (M8.2) ---
    # Slack incoming-webhook URL used when a channel does not carry its own target.
    # Empty = no default; a Slack channel must then supply its own webhook.
    slack_webhook_url: str = ""
    # SMTP relay for the email transport. Host empty = email disabled unless a client
    # is injected. `smtp_from` is the default sender when a channel does not override it.
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_from: str = "finops@localhost"
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_use_tls: bool = True

    # --- Enterprise transports (M8.3) ---
    # Microsoft Teams incoming-webhook URL (used when a channel carries no target).
    teams_webhook_url: str = ""
    # Jira: one instance, many projects. A channel target selects the project;
    # base URL + credentials + default project/issue-type are instance-level here.
    jira_base_url: str = ""
    jira_email: str | None = None
    jira_api_token: str | None = None
    jira_project: str = ""
    jira_issue_type: str = "Task"
    # ServiceNow: instance URL + credentials for the Table API (create incident).
    servicenow_instance_url: str = ""
    servicenow_user: str | None = None
    servicenow_password: str | None = None

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
    # Comma-separated allow-list of Custodian action *types* (e.g. "tag,stop") that
    # policy actions may perform. Empty = no per-type restriction (any action allowed).
    allowed_actions: str = ""

    # --- Runtime ---
    finops_mock: bool = True
    run_interval_seconds: int = 86400
    # Pull-mode policy execution runs on its own cadence, independent of the
    # cost-collection pipeline above (Stacklet-style per-policy scheduling).
    policy_run_interval_seconds: int = 86400
    app_data_dir: str = "/data"
    # Optional scheduled governance report (M9.4): off by default. When enabled, the
    # scheduler writes a timestamped CSV export under APP_DATA_DIR on this cadence.
    governance_report_enabled: bool = False
    governance_report_interval_seconds: int = 86400

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

    @property
    def allowed_actions_list(self) -> list[str]:
        return [x.strip() for x in self.allowed_actions.split(",") if x.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
