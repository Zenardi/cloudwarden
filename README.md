# Azure Governance-as-Code & FinOps

[![CI](https://github.com/Zenardi/azure-finops/actions/workflows/ci.yml/badge.svg)](https://github.com/Zenardi/azure-finops/actions/workflows/ci.yml)

**Cloud governance-as-code *and* FinOps for Azure, in one self-hostable stack.**
Two pillars over a shared *collect → store → surface* backbone:

- **Governance-as-code** (à la [Stacklet](https://stacklet.io/)), built on
  **[Cloud Custodian](https://cloudcustodian.io/)** (`c7n` + `c7n-azure`): author,
  validate, **version** and **GitOps-sync** policies, group them into
  **collections**, **evaluate them on a schedule across every subscription** (pull
  mode), and review the full **execution history** — which resources each policy
  matched and how every run turned out.
- **FinOps** cost & utilization optimization: visualize spend by
  **resource / type / region**, generate **AI-assisted right-sizing and shutdown
  recommendations** from CPU / RAM / I/O metrics, and — once approved — **execute
  guarded remediation**.

Everything is pulled from **Azure Cost Management**, **Monitor**, **Resource Graph**
and **Advisor**, persisted to **Postgres/TimescaleDB**, and surfaced on **Grafana**
and a **Next.js** UI — with a **pluggable AI** layer (Anthropic by default; any
OpenAI-compatible/local model). It runs fully offline with recorded fixtures
(`FINOPS_MOCK=1`), so no Azure subscription is required to see it work.

## Status

**Platform & FinOps** — the cost-optimization backbone:

| Phase | Scope | State |
|------|-------|-------|
| 0 | Scaffold (config, auth, resilience, storage, Docker, CI tooling) | ✅ done |
| 1 | **MVP:** cost + inventory → Postgres/Timescale → Grafana cost dashboard | ✅ done, verified |
| 2 | Metrics collector + FinOps rules (shutdown/downsize/idle) + savings | ✅ done |
| 3 | Pluggable AI recommendations + executive summary | ✅ done |
| 4 | FastAPI + Next.js UI (review/approve) | ✅ done |
| 5 | Guarded remediation (deallocate/resize/delete, dry-run default) | ✅ done |

**Governance-as-code** — Cloud Custodian policy management & scheduled execution:

| Milestone | Scope | State |
|------|-------|-------|
| M1 | Policy engine wrapper (`c7n` + `c7n-azure`): validate / schema / dry-run | ✅ done |
| M2 | Policy CRUD API + editor UI, collections, GitOps sync, version history & diff | ✅ done |
| M3.1–M3.3 | Execution results storage, pull-mode orchestrator, execution history API + UI | ✅ done |
| M3.4 | Per-policy compliance & health metrics (API + Grafana) | ✅ done |
| M4.1 | AssetDB — asset inventory with full config (schema + ingestion) | ✅ done |
| M4.2 | AssetDB — filterable, injection-safe asset query API | 🚧 in review |

Both tracks run fully offline with recorded fixtures (`FINOPS_MOCK=1`) — no Azure
subscription required to see the pipeline, policies and dashboards working.

## Dashboards & data model (Grafana)

Grafana's native **Azure Monitor** datasource covers live metrics,
Log Analytics (KQL) and Resource Graph, but has **no native Cost Management
support**. So cost + AI-recommendation data is written to **Postgres/TimescaleDB**
and read via Grafana's Postgres datasource (historical trends + persisted
recommendations), while the Azure Monitor datasource powers live raw-metric
panels. Both datasources are provisioned as code in `grafana/provisioning/`.

## Architecture

```
Azure APIs ─┬─ Cost Management Query API ─┐
            ├─ Monitor Metrics / Logs ────┤
            ├─ Resource Graph ────────────┼─► FastAPI backend + Typer CLI
            └─ Advisor ───────────────────┘   collect → analyze → recommend → store
                                                │            │
                            AI provider ◄───────┘            ▼
                      (Anthropic | OpenAI-compat)     Postgres / TimescaleDB
                                                             │
                          ┌───────────────────────────────────┼───────────────┐
                          ▼                                    ▼               ▼
                     Next.js UI                     Grafana Postgres DS   Grafana Azure
                 (review / approve)                  cost + recs panels    Monitor DS
                          │                                                (live metrics)
                          ▼ (approved)
                 Remediation executor ─► Azure write APIs (deallocate / resize / delete)
                 (dry-run default, guardrails, audit)
```

Two Azure credentials by design: a **read-only SP** for collection (Reader +
Cost Management Reader + Monitoring Reader) and a **separate write-scoped SP**
for remediation.

## Governance-as-code (Cloud Custodian)

The governance pillar — author policies, run them **on a schedule across every
subscription**, and audit exactly what each run matched. This loop is independent of
the FinOps cost pipeline above and runs on its own cadence:

```
Policies ──(author / validate / version / GitOps sync / collections)
   │
   ▼
Cloud Custodian engine (c7n + c7n-azure) ── injectable, mockable seam
   │   scheduled pull mode: run_all_policies() every POLICY_RUN_INTERVAL_SECONDS,
   │   fanned across every enabled subscription (per-policy failure isolation)
   ▼
policy_executions + policy_matches ──► Executions UI (history + per-run drill-down)
```

Governance-as-code is built on **[Cloud Custodian](https://cloudcustodian.io/)**
(`c7n` + `c7n-azure`) — the open-source rules engine (the same one Stacklet
packages commercially). The `custodian/` package wraps c7n's `validate` / `run` /
`schema` operations behind an injectable `CustodianRunner` so every milestone
(policy CRUD, scheduled evaluation, drift detection, remediation-as-policy) calls
one mockable entry point — `validate_policy()`, `run_policy()`, `get_schema()` —
instead of the c7n CLI or live Azure. Importing `c7n_azure.entry` registers the
`azure.*` resource types (`azure.vm`, `azure.disk`, …); the engine reuses the same
`AZURE_*` credentials as the collectors and, in `FINOPS_MOCK=1` mode, evaluates
policies against a recorded fixture so dry-runs run fully offline.

Authored policies are persisted in a **`policies`** table (M1.2) — `id`, unique
`name`, `resource_type` (e.g. `azure.vm`), the parsed Custodian body as JSONB
`spec`, `description`, an `enabled` flag, a `version` that bumps on every update,
and a `source` (`custom` | `library` | `imported`). CRUD lives behind
`storage/repository.py` (`create_policy` / `get_policy` / `list_policies` /
`update_policy` / `delete_policy` / `set_policy_enabled`) alongside the cost and
recommendation tables.

The table is exposed as a **validate-on-write CRUD API** (M2.1):

- `GET /api/policies[?enabled=true|false]` — list policies (optionally filtered by
  enabled state).
- `GET /api/policies/{id}` — fetch one (`404` if missing).
- `POST /api/policies` — **validate the spec first**, then persist: `201` on
  success, `422` with an `errors` array (and **no row written**) when the spec
  fails Custodian validation, `409` on a duplicate `name`.
- `PUT /api/policies/{id}` — partial update; a changed `spec` is **re-validated**
  (`422`) and the `version` bumps **only when an authored field actually changes**.
  `404` if missing, `409` on a name collision.
- `DELETE /api/policies/{id}` — remove (`404` if missing).
- `POST /api/policies/{id}/enabled?enabled=true|false` — toggle the enabled flag
  (`404` if missing).
- `GET /api/policies/{id}/versions` — the policy's version history, newest-first
  (`404` if missing).
- `GET /api/policies/{id}/versions/diff?from_version=&to_version=` — field-level
  diff between two stored versions (`404` for an unknown policy/version).

Writes never persist an invalid policy — every stored row has passed schema
validation, so the API tags responses `validation_status: "valid"`. Validation
goes through the same injectable `CustodianRunner` seam as the M1.3 endpoints.

A **Policies** page in the Next.js UI (M2.2, `frontend/app/policies/`) drives this
API: it lists stored policies with their resource type, source, validation status
and enabled state, and offers a JSON policy-spec editor with a **Validate** button
(inline schema feedback, no save) and **Create/Update** that surfaces `422`
validation errors inline without navigating away, plus per-row Enable/Disable and
Delete.

Policies can be grouped into named **collections** (M2.3) — a many-to-many
grouping (à la Stacklet policy collections) persisted in a `policy_collections`
table plus a `collection_policies` join. A policy may belong to any number of
collections, and **deleting a collection never deletes the member policies** (only
the membership rows). The API is `GET/POST /api/collections`,
`GET/DELETE /api/collections/{id}`, and
`POST/DELETE /api/collections/{id}/policies/{policy_id}` (adding an unknown policy
or collection returns `404`); a **Collections** page manages collections and their
membership in the UI.

Policies can also be managed **GitOps-style** (M2.4): point `GITOPS_REPO_URL`
(+ `GITOPS_BRANCH` / `GITOPS_POLICY_PATH`) at a Git repo of Custodian policy YAML,
then `POST /api/policies/sync` clones/pulls it, validates each policy, and
**upserts by name** with `source='gitops'`. Unparseable or schema-invalid files
are **skipped and reported** (non-fatal), the sync is **idempotent** (a no-op
re-sync writes nothing), and a clone/pull failure returns a structured error
rather than a `500`. The Git client is an injectable seam, so the whole pipeline
is unit-tested offline against a fixture repo.

Every content change to a policy is captured as an immutable **version** (M2.5) in
a `policy_versions` table (`ON DELETE CASCADE` with the policy). `create_policy`
seeds version 1 and each content-changing `update_policy` appends the next number
— a no-op update writes nothing — so the rows form an append-only audit trail.
`GET /api/policies/{id}/versions` lists them newest-first and
`GET /api/policies/{id}/versions/diff?from_version=&to_version=` returns the set of
changed authored fields (name/resource_type/spec/description) between any two
revisions. The **Policies** page adds a **History** panel to browse versions and
compare two side by side.

Every policy run is recorded for audit as a **policy execution** (M3.1). A
`policy_executions` table (one row per run: `execution_id` PK, `policy_id` →
`policies.id`, `subscription_id`, `status` `running|succeeded|failed`,
started/finished timestamps, `resources_matched`, `actions_taken`, `error`) mirrors
the existing `runs` lifecycle, with per-resource detail in `policy_matches`
(`resource_id`, `resource_type`, `matched_at`, `action_taken`, `action_result`).
Repository helpers `create_policy_execution` / `finish_policy_execution` /
`insert_policy_matches` / `get_policy_execution` / `list_policy_executions` (filter
by policy / subscription / status) / `list_policy_matches` give the orchestrator a
stable write/read surface.

**Pull-mode execution (M3.2).** A second scheduled loop — independent of the
cost-collection pipeline — evaluates governance policies on their own cadence.
`orchestrator.run_policies(subscription)` opens a `PolicyExecution` per enabled
policy, evaluates it through the engine's single mockable seam
(`custodian.engine.run_policy`), records the matched resources as `policy_matches`,
and closes the execution `succeeded` (with `resources_matched` + the policy's
declared `actions_taken`) or `failed` (with the error) — one policy's failure never
aborts its siblings. `run_all_policies()` fans that across every enabled
subscription with the same per-subscription isolation as the cost pipeline. It runs
via `python -m azure_finops.cli run-policies [--mock]` and as a second APScheduler
job (`finops-policy-run`) on `POLICY_RUN_INTERVAL_SECONDS`, separate from the
cost-pipeline `RUN_INTERVAL_SECONDS`.

The run history is exposed for review (M3.3): `GET /api/policy-executions`
(newest-first, filterable by `policy_id` / `subscription_id` / `status` + `limit`),
`GET /api/policy-executions/{id}` (`404` when unknown), and
`GET /api/policy-executions/{id}/matches` (the matched-resource drill-down). The
**Executions** page in the Next.js UI (`frontend/app/executions/`) renders that
history with filter dropdowns and an expandable per-row drill-down into the matched
resources.

Those executions roll up into **per-policy compliance & health** (M3.4). The
`v_policy_health` SQL view aggregates each policy's runs — across *every*
subscription it ran in — into `total_executions`, succeeded/failed counts,
`total_matches`, a rounded `success_rate`, and the `last_status` / `last_execution_at`
of the most recent run (with `v_policy_compliance` giving the per-subscription
grain). `GET /api/governance/policy-health` returns that list (empty until a policy
has executed — never an error), and a provisioned **Policy Health & Compliance**
Grafana dashboard visualises success rate, matches over time, and per-policy /
per-subscription health.

**AssetDB (M4.1).** Every pipeline run also populates a queryable, near-real-time
asset inventory (à la Stacklet's AssetDB). The `assets` table is a richer superset
of `resources` — same identity/location/tags plus the **full resource `config`**
(JSONB, captured from Resource Graph `properties`), a coarse `state`, and
`first_seen`/`last_seen`. `repo.upsert_assets` upserts idempotently (`ON CONFLICT`):
`first_seen` is stamped once, `last_seen`/`config` refresh on every re-ingestion, and
the first time a resource is seen an append-only `asset_events` row (`event_type`
`created`, who/how/when + a config snapshot) is written for audit. Each asset carries
its subscription id (retargeted per subscription in mock mode).

AssetDB is queryable via `POST /api/assets/query` (M4.2) — a structured request of
allow-listed `{column, op, value}` filters (`type` / `location` / `subscription_id`
/ `tag` / …, ops `eq`/`ne`/`contains`/`in`), an exact-match `tags` map, and
`limit`/`offset`. The builder is **injection-safe by construction**: unknown columns
or operators are rejected with `400` (never executed), and every value — including
tag values — is bound as a parameter, so a SQL-injection string is a harmless
literal. `limit` is capped at 500 with a stable order.

Two API endpoints expose the engine's offline surface (M1.3):

- `POST /api/policies/validate` — dry-run schema-validate a policy `spec` (a
  parsed Custodian `{"policies": [...]}` body). Returns `{"valid", "errors"}`;
  **never persists** anything. A well-formed but schema-invalid policy still
  returns `200` with `valid: false` and a populated `errors` array; a malformed
  body (no `policies` list) or an unknown resource type is rejected with `400`.
- `GET /api/custodian/schema[?resource_type=azure.vm]` — list the registered
  `azure.*` resource types, or return one type's `filters` / `actions` / JSON
  schema. An unknown `resource_type` returns `400`.

Both endpoints delegate to `custodian/engine.py` through an injectable
`CustodianRunner` seam and are guaranteed to degrade to `400` rather than surface
a `500` if the engine errors.

A stored policy can be **dry-run** against Azure (M1.4):

- `POST /api/policies/{id}/dryrun[?subscription_id=…]` — evaluate a persisted
  policy's `spec` with `engine.run_policy(dry_run=True)` and return the **matched
  resources** without mutating anything. Resolves the target subscription via
  `repo.get_subscription` → `SubscriptionContext` (defaulting to the configured
  subscription when none is given). An unknown policy id or `subscription_id`
  returns `404`. In `FINOPS_MOCK=1` mode the match set comes from
  `fixtures/custodian_policy_result.json`, so dry-runs are fully offline; no
  remediation action is ever executed.

## Quickstart (mock mode, no Azure needed)

Prerequisites: Docker with Compose v2 (`docker compose`).

```bash
cp .env.example .env            # defaults to FINOPS_MOCK=1
make up                         # db (TimescaleDB) + backend (API) + grafana + web UI
make seed                       # runs one mock pipeline → populates the DB
```

`make up` (equivalently a plain `docker compose up -d`) starts the **full stack**,
including the frontend. Use `make up-core` for db + backend + grafana only.

Then open:

- **Web UI (Next.js)** → http://localhost:3001 — overview, cost explorer,
  recommendation review/approve, **subscription management**, a **Policies**
  editor (author / validate / enable / delete Cloud Custodian policies),
  **Collections** (group policies into named sets), and an **Executions** page
  (pull-mode policy-run history with policy/subscription/status filters and a
  per-row drill-down into matched resources).
- **Grafana** → http://localhost:3000 (anonymous viewer enabled) → *FinOps* folder
  → **FinOps — Cost Overview** (cost by type / region / resource + daily trend) and
  **FinOps — Policy Health & Compliance** (per-policy success rate, matches over
  time, and per-subscription compliance).
- **API docs** → http://localhost:8000/docs (`/api/costs/summary`, `/api/recommendations`,
  `/api/policies` CRUD, `/api/policies/validate`, `/api/custodian/schema`,
  `/api/policies/{id}/dryrun`, `/api/policies/{id}/versions`,
  `/api/policies/sync`, `/api/collections`, `/api/policy-executions`,
  `/api/governance/policy-health`, `/api/assets/query`, …).

Run the backend on a schedule instead of one-shot: the `backend` service also
supports `command: ["scheduler"]`.

## Live mode (real Azure)

1. Create the read SP and assign **Reader + Cost Management Reader + Monitoring
   Reader** on the subscription (+ **Log Analytics Reader** for memory metrics).
2. In `.env`: set `AZURE_SUBSCRIPTION_ID`, `AZURE_TENANT_ID/CLIENT_ID/CLIENT_SECRET`,
   `FINOPS_MOCK=0`, and an AI key (`ANTHROPIC_API_KEY`) or `AI_BASE_URL` for a
   local model.
3. `make up && make seed`.

For remediation (Phase 5), additionally set the write SP (`AZURE_REMEDIATION_*`),
`REMEDIATION_ENABLED=true`, and `ALLOWED_RESOURCE_GROUPS`. Remediation defaults
to **dry-run**; resources tagged `finops:exclude=true` are never touched.

## Multiple subscriptions

`AZURE_SUBSCRIPTION_ID` is seeded as the **default** subscription on first start.
Add more on the **Subscriptions** page (or `POST /api/subscriptions`): each row
can reuse the shared env service principal or carry its **own** tenant/client/
secret (e.g. a different tenant). A run with no target (`make seed`, the
scheduler, or `POST /api/runs`) **fans out across every enabled subscription**,
one pipeline run each; the API also accepts `?subscription_id=…` to run just one.
Per-subscription secrets are stored in Postgres (v1) — a Key Vault / column-
encryption backing is the intended hardening step.

## Key configuration

| Env | Purpose |
|-----|---------|
| `FINOPS_MOCK` | `1` = use fixtures (offline); `0` = call Azure |
| `AI_PROVIDER` / `AI_MODEL` | `anthropic` (default `claude-opus-4-8`) or `openai` |
| `AI_BASE_URL` | OpenAI-compatible endpoint for local models (Ollama/vLLM/LM Studio) |
| `COST_LOOKBACK_DAYS` / `METRIC_LOOKBACK_DAYS` | analysis windows |
| `REMEDIATION_ENABLED` | `false` = dry-run only |
| `LOG_ANALYTICS_WORKSPACE_ID` | enables memory-based downsize rules |
| `GITOPS_REPO_URL` / `GITOPS_BRANCH` / `GITOPS_POLICY_PATH` | GitOps policy sync source (blank URL disables) |

Full list: `.env.example`.

## Project layout

```
backend/azure_finops/
  config.py auth.py resilience.py models.py orchestrator.py scheduler.py cli.py
  azure/       inventory.py cost.py metrics.py logs.py advisor.py context.py
  analysis/    (rollup/rules/idle/pricing/savings — Phase 2)
  ai/          (base/anthropic/openai/factory/prompt — Phase 3)
  remediation/ (executor/guardrails/approval — Phase 5)
  custodian/   engine.py gitops.py (Cloud Custodian c7n + c7n-azure — engine + GitOps sync)
  storage/     schema.py db.py repository.py (policies, executions, cost, SQL views)
  api/         main.py
  fixtures/    inventory.json cost.json custodian_policy_result.json
grafana/       provisioning/ + dashboards/ (cost, recommendations, …)
frontend/app/  policies/ collections/ executions/ costs/ recommendations/ … (Next.js)
docker-compose.yml  Makefile  .env.example
```

## Local development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r backend/requirements-dev.txt
make lint      # ruff
make test      # offline unit tests (no DB/Azure needed)
make coverage  # full suite + 95% gate (spins an ephemeral Postgres via testcontainers; needs Docker)
make run-mock  # run pipeline locally against a Postgres at localhost:5432
```

**Tests:** 270 tests, **~99% line coverage** (gate at 95%, enforced in CI —
`.github/workflows/ci.yml`). Live-Azure code paths are covered via injected fake
clients; the DB/API/orchestrator/remediation flows run against a throwaway
PostgreSQL (testcontainers).

## License

TBD.
