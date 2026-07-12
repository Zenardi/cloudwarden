# Changelog

All notable changes to this project are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/) and [SemVer](https://semver.org/).

## [Unreleased]

### Added
- **M1.3 — Policy validation + Custodian schema endpoints.** Two new FastAPI
  routes exposing the offline surface of the Cloud Custodian engine:
  `POST /api/policies/validate` (dry-run schema-validate a policy `spec`; returns
  `{"valid", "errors"}` and **never persists**) and
  `GET /api/custodian/schema[?resource_type=…]` (list registered `azure.*`
  resource types, or one type's `filters`/`actions`/schema). Both delegate to
  `custodian/engine.py` through an injectable `CustodianRunner` seam (a new
  `get_custodian_runner` FastAPI dependency tests override with a
  `FakeCustodianRunner`) and are hardened to **never raise** — malformed input,
  unknown resource types, or an engine blow-up all degrade to `400` instead of a
  `500`. New `ValidateRequest` / `ValidateResult` pydantic models. TDD:
  `test_policy_validation_api.py` (10 fully-offline tests, TestClient + injected
  fake) covering the valid / invalid / malformed and schema happy / error paths
  at 100% line coverage on the changed code.
- **M1.2 — Policy domain model & storage.** New `policies` table (`Policy` ORM in
  `storage/schema.py`) persisting governance-as-code rules: `id`, unique `name`,
  indexed `resource_type`, the parsed Custodian body as JSONB `spec`,
  `description`, an `enabled` flag, a `version` that increments on each update,
  and a `source` (`custom` | `library` | `imported`), plus server-managed
  `created_at`/`updated_at`. Six repository functions (`create_policy`,
  `get_policy`, `list_policies` with `enabled_only`, `update_policy`,
  `delete_policy`, `set_policy_enabled`) and a `_policy_public` serializer follow
  the existing `Subscription`/`Recommendation` pattern, with `PolicyRecord` /
  `PolicyCreate` pydantic models for API validation. Test-first (TDD):
  `test_policy_repository.py` (9 DB-backed tests) covers the CRUD + enable-toggle
  happy paths and the negative cases (duplicate-name integrity error with no
  partial row, missing-id returns `None`) at 100% line coverage on the new code.
- **M1.1 — Cloud Custodian engine wrapper.** New `custodian/` package embedding
  [Cloud Custodian](https://cloudcustodian.io/) (`c7n` + `c7n-azure`) as the
  policy engine (the same open-source rules engine Stacklet packages
  commercially). `custodian/engine.py` exposes `validate_policy()`, `run_policy()`,
  and `get_schema()` behind an injectable `CustodianRunner` protocol so every
  later milestone (policy CRUD, scheduled evaluation, drift detection,
  remediation-as-policy) calls one mockable seam instead of the c7n CLI or live
  Azure. `LiveCustodianRunner` drives c7n's Python API (`c7n.schema.validate`/
  `generate`, importing `c7n_azure.entry` once to register the 112 `azure.*`
  resource types) and reports health via `resilience.REGISTRY`; in `FINOPS_MOCK=1`
  mode `run_policy` returns the recorded `fixtures/custodian_policy_result.json`
  so dry-runs are fully offline. TDD: `test_custodian_engine.py` (17 tests, 100%
  line coverage on the package) with a `FakeCustodianRunner` double — no test
  touches live Azure or c7n network paths.

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
- **Test connection** per subscription (`POST /api/subscriptions/{id}/test` + a
  "Test" button on the Subscriptions page): acquires an ARM token with that
  subscription's credential and GETs the subscription to confirm the SP can see
  it, returning a friendly ok/denied/not-found result (mock mode reports without
  calling Azure).

### Testing
- Test suite raised to **106 tests / ~98% line coverage** (95% gate enforced via
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
- **Security (Trivy) — Cloud Custodian transitive pins (M1.1).** `c7n-azure==0.7.50`
  (the latest release) hard-pins (`==`) `cryptography==46.0.7` (GHSA-537c-gmf6-5ccf,
  HIGH) and `pyjwt==2.12.1` (CVE-2026-48526, HIGH); they cannot be bumped without a
  `ResolutionImpossible` conflict against the mandated engine, and no newer c7n
  release relaxes them. Both are transitive Azure-auth-library deps (`msal`/`adal`);
  the app performs no attacker-controlled JWT verification of its own. Tracked
  upstream — resolved when Cloud Custodian relaxes these pins. The three
  `starlette` advisories reported by Trivy are pre-existing (via `fastapi`,
  unchanged by this PR), not introduced by the Custodian dependency. To align
  cleanly with c7n's pin matrix this milestone also bumped `azure-identity`
  1.19.0→1.25.3, `azure-mgmt-compute` 33.0.0→34.1.0, `azure-mgmt-network`
  28.0.0→28.1.0, `apscheduler` 3.11.0→3.11.2, `click` 8.1.8→8.3.3 (+ `typer`
  0.15.1→0.16.0), pinned `azure-mgmt-resourcegraph` 8.0.0→7.0.0, and dropped the
  unused `azure-mgmt-costmanagement` SDK pin (`cost.py` calls the REST API directly).
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
