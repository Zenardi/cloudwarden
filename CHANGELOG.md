# Changelog

All notable changes to this project are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/) and [SemVer](https://semver.org/).

## [Unreleased]

### Added
- **Phase 0 — Scaffold:** project layout, `pyproject.toml` (Ruff + pytest),
  `Makefile`, `.env.example`, Docker Compose (TimescaleDB + backend + Grafana,
  frontend behind a profile), nonroot backend image (uid 65532).
- **Config / auth / resilience:** `config.py` (pydantic-settings), `auth.py`
  (read + write `DefaultAzureCredential`, ARM token), `resilience.py`
  (retry/backoff honoring `Retry-After`/`x-ms-ratelimit-*` + last-good cache).
- **Phase 1 — MVP cost pipeline:** Resource Graph inventory + Cost Management
  collectors (mock-backed via fixtures), storage layer (SQLAlchemy models +
  repository + Timescale/views bootstrap), orchestrator, Typer CLI
  (`initdb | run | run --mock | api | scheduler`), Grafana provisioning + Cost
  dashboard.
- **Phase 2 — Metrics + rules engine:** Azure Monitor metrics + Log Analytics
  memory + Advisor collectors (mock-backed), utilization rollups (avg/p95/max +
  data_completeness), FinOps rules (shutdown / downsize / idle-orphan) with
  Retail-Prices-based savings and Advisor confidence boosting, prioritized
  recommendations persisted, and a **Recommendations & Savings** Grafana dashboard.
- **Phase 3 — Pluggable AI layer:** provider abstraction (`AIProvider`) with a
  deterministic offline Stub, Anthropic (`claude-opus-4-8`, adaptive thinking,
  strict-JSON + tolerant parse), and OpenAI-compatible (local/Ollama/vLLM)
  providers — config-selected with a safe fallback so AI is best-effort.
  Aggregated + sanitized payload, executive summary persisted to `ai_summaries`,
  `/api/summary` endpoint, and an AI-summary panel on the Recommendations dashboard.
- **Phase 4 — FastAPI API + Next.js UI:** review/approve endpoints
  (`POST /api/recommendations/{id}/decision`, `GET /api/runs`) plus a Next.js
  (App Router, standalone output) UI — overview (KPIs + AI summary + Grafana
  links), cost explorer, recommendations review/approve, and run history/trigger.
  Served via the `frontend` compose profile (`make up-all`).
- **Phase 5 — Guarded remediation:** executor (VM deallocate/resize, delete
  unattached disk / idle public IP) + guardrails (REMEDIATION_ENABLED forces
  dry-run, resource-group allow-list, `finops:exclude` tag) + approval flow with
  a `remediation_actions` audit trail. `POST /api/recommendations/{id}/remediate`
  (dry-run default) and `GET /api/remediation`, plus a Remediation audit page and
  a Remediate action on approved recommendations. Dry-run by default and fully
  mockable — no Azure writes unless explicitly enabled with the write SP.

### Added (multi-subscription)
- **Manage multiple Azure subscriptions.** New `subscriptions` table + repository
  CRUD, a **Subscriptions** page in the web UI, and REST endpoints
  (`GET/POST /api/subscriptions`, `POST /api/subscriptions/{id}/default`,
  `DELETE /api/subscriptions/{id}`). Each subscription reuses the shared env
  service principal or carries its **own** tenant/client/secret (hybrid model;
  secrets stored in Postgres — Key Vault backing is a hardening TODO, and secrets
  are never returned by the API).
- The collectors are now **subscription-aware** (`SubscriptionContext` threaded
  through inventory/cost/metrics/advisor); mock runs retarget fixture resource ids
  per subscription so multiple subscriptions produce distinct, non-colliding data.
- Runs **fan out across every enabled subscription** (`run_all_subscriptions`),
  one pipeline run each — used by the CLI `run`, the scheduler, and `POST /api/runs`
  (which also accepts `?subscription_id=…` to run a single subscription). The env
  `AZURE_SUBSCRIPTION_ID` is seeded as the default subscription on first start.

### Testing
- Test suite raised to **96 tests / ~98% line coverage** (95% gate enforced via
  `[tool.coverage.report] fail_under`). Adds a Postgres-backed integration suite
  (testcontainers) covering repository/orchestrator/API/approval/CLI/scheduler,
  fake-client tests for every live-Azure path, and resilience/auth/provider
  units. GitHub Actions CI (`.github/workflows/ci.yml`) runs Ruff + coverage +
  the Next.js build. Added `backend/requirements-dev.txt` and `make coverage`.

### Fixed
- **Docker build failure (`docker compose up`).** The backend image used the moving
  Chainguard `python:latest-dev` tag, which (a) runs as nonroot so `python -m venv
  /venv` hit `Permission denied`, and (b) drifted to a newer Python with no wheels
  for the pinned deps (`psycopg-binary`, `pydantic-core` failed to build). Switched
  to the pinned official `python:3.13-slim-trixie` (build stage as root, runtime
  nonroot uid 65532 with a writable `/data`), matching the tested dependency set.
- Pinned `click==8.1.8` — `typer==0.15.1` calls the pre-8.2 `Parameter.make_metavar()`
  signature, so a fresh install pulling `click>=8.2` crashed the CLI (and would fail
  CLI tests in CI) with `make_metavar() missing 1 required positional argument`.
- Bumped `psycopg[binary]` 3.2.3 → 3.2.13 (no 3.2.3 wheel for the image's Python).
- **Anthropic live path always fell back to the deterministic stub.** `anthropic==0.42.0`
  predates the `thinking` parameter, so `messages.create(thinking={"type": "adaptive"})`
  raised `unexpected keyword argument 'thinking'` on every real call. Bumped to
  `anthropic==0.69.0`.
- **Security (Trivy):** bumped `python-dotenv` 1.0.1 → 1.2.2 (CVE-2026-28684, symlink
  file-overwrite) and added a backend healthcheck to `docker-compose.yml`. Trivy
  found no secrets in the code. Remaining advisories are base-image (Debian) OS
  packages — tracked, resolved as the pinned base image updates.
- **Frontend upgraded to Next.js 15 / React 19.** Bumped `next` 14.2.35 → 15.5.20,
  `react`/`react-dom` 18.3.1 → 19.2.7 (+ matching `@types`), and pinned `postcss`
  8.5.10 via an override. Clears all 5 HIGH + remaining Next.js/postcss CVEs — the
  frontend lockfile now scans **0 vulnerabilities**. All pages are client components,
  so no Next 15 async-request-API migration was needed.

### Changed
- The **frontend is now part of the default `docker compose up` stack** (removed the
  `frontend` compose profile). `make up` starts everything; `make up-core` brings up
  db + backend + grafana only.
- `azure-mgmt-resourcegraph` imports `six` without declaring it; pinned `six` in
  `requirements.txt` so the live inventory path works in the container.
