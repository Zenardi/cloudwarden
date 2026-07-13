# Changelog

All notable changes to this project are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/) and [SemVer](https://semver.org/).

## [Unreleased]

### Changed
- **CI hardening.** `.github/workflows/ci.yml` now, in addition to the existing
  backend (lint + unit/integration tests + 95% coverage gate) and frontend
  (`next build`) jobs: **builds the whole solution** as container images
  (`build` job — backend + frontend), runs an **end-to-end** job (`e2e`) that boots
  the compose stack in mock mode and smoke-tests the pull-mode pipeline
  (`/health` → seed policy → `run-policies --mock` → assert `/api/policy-executions`
  + its `/matches`), and adds a **Trivy security gate** (`security` job) that scans
  the filesystem + both images and **fails on any *fixable* HIGH/CRITICAL CVE**
  (`--ignore-unfixed`; no baseline / `.trivyignore` — no specific CVEs are waived).
  Add these jobs as **required status checks** in branch protection to block merges.

### Security
- **Hardened & guarded the c7n-pinned CVE mitigation (issue #58).** `c7n 0.9.51` /
  `c7n-azure 0.7.50` (still the latest releases) hard-pin the vulnerable
  `cryptography==46.0.7` (GHSA-537c-gmf6-5ccf) and `pyjwt==2.12.1` (CVE-2026-48526),
  so they can't be bumped in `requirements.txt` without `ResolutionImpossible`. The
  `--no-deps` force-upgrade to the patched `cryptography 48.0.1` / `PyJWT 2.13.0` now
  lives in a **single source of truth** — `backend/requirements-overrides-security.txt`
  — applied by both `backend/Dockerfile` **and** the CI backend job, so the test suite
  validates the exact versions the image ships (previously CI tested the vulnerable
  pins while the image shipped the patched ones). A new guard/**watch** test
  (`backend/tests/test_dependency_security.py`) asserts the effective versions stay
  patched and **fails the moment upstream relaxes a pin** — the trigger to drop the
  override and bump `requirements.txt` for good.
- **Remediated all fixable image CVEs; Trivy gate is green.** Everything with an
  available upstream fix is fixed:
  - **Backend** — `fastapi` 0.115.6 → **0.139.0** and pinned `starlette` **1.3.1**
    (clears CVE-2025-62727 / CVE-2026-48818 / CVE-2026-54283); `cryptography`
    **48.0.1** (GHSA-537c-gmf6-5ccf) and `PyJWT` **2.13.0** (CVE-2026-48526)
    force-upgraded in the Dockerfile over c7n / c7n-azure's hard pins (validated
    API-compatible by the full test suite).
  - **Frontend** — `apk upgrade` patches `libssl3`/`libcrypto3` (CVE-2026-45447),
    and the unused **npm is removed** from the runtime image, dropping the HIGH CVEs
    its bundled deps (`tar` / `sigstore` / `cross-spawn` / `glob` / `minimatch`)
    carried. The frontend image now reports **0** HIGH/CRITICAL.
  - The only remaining backend image findings are **Debian *Essential* packages**
    (`perl-base`, `util-linux`, `ncurses`, `gzip`, `libacl`) with **no upstream
    fix** — they can be neither patched nor removed — so the gate scopes them out
    via `--ignore-unfixed` while still failing on anything actionable.

### Added
- **M5.1 — Account groups.** Organize subscriptions into named **account groups**
  (à la Stacklet account groups) so policies can target logical sets of accounts. New
  `account_groups` table + `account_group_members` join (both FKs `ON DELETE CASCADE`),
  auto-created by `init_db()`. Membership is **many-to-many**: a subscription can belong
  to any number of groups and be removed from each **independently**, and **deleting a
  group keeps its subscriptions** (only the membership rows are removed). Repository CRUD
  + membership, endpoints `GET/POST/DELETE /api/account-groups[/{id}]` and
  `POST/DELETE /api/account-groups/{id}/subscriptions/{subscription_id}` (adding an
  unknown subscription or an unknown group returns `404`; duplicate name → `409`), and an
  **`/account-groups`** UI (with an **Account Groups** nav link) to create groups and
  manage membership. Builds directly on the existing `subscriptions` records.
- **M4.5 — Asset explorer & detail UI.** A Next.js **Asset Explorer** (the Stacklet
  AssetDB console) at **`/assets`**: a query form (type / location / id-contains / tag)
  drives the injection-safe M4.2 query API with **pagination**, and each row links to
  **`/assets/<resource-id>`** — a **catch-all** route (Azure resource ids contain
  slashes) that composes the asset's **config** (M4.2), **relationships** (M4.3, each
  neighbour linked) and **change-history** timeline (M4.4) into a single detail view.
  An unknown id renders a friendly **not-found** state, never a crash. Adds
  `frontend/app/assets/page.tsx`, `frontend/app/assets/[...id]/page.tsx`, an **Assets**
  nav link, and typed `lib/api.ts` helpers (`buildAssetQuery`, `queryAssets`, `getAsset`,
  `getAssetRelationships`, `getAssetHistory`). The CI `e2e` job now also boots the
  frontend and asserts the explorer + a detail route return `200` in mock mode.
- **M4.4 — Asset change history & event metadata.** AssetDB gains an **audit
  timeline** — the *who / how / when* of every asset change — by ingesting the Azure
  **Activity Log** into `asset_events`. New mockable collector
  `azure/activitylog.py` (`client=None` → recorded `fixtures/activitylog.json`; inject
  a client for live) parses each entry's **actor** (`caller`), **operation**
  (`operationName`) and **timestamp** (`eventTimestamp`), lower-casing resource ids to
  join with assets; a malformed record (missing any of those) is **skipped, never
  fatal**. `repo.record_activity_events` persists each as an `activity` event whose row
  time is the *real* event timestamp, and the pipeline collects it each run. New
  endpoint `GET /api/assets/{id}/history` returns the combined lifecycle + activity
  timeline **newest-first**; an unknown asset yields an empty list (`200`), not an
  error. No new dependency (`azure-mgmt-monitor` is already pinned transitively by
  c7n-azure).
- **M4.3 — Asset relationships graph.** The **graph dimension** of AssetDB (à la
  Stacklet's asset relationships). Ingestion now derives typed, directed edges
  between assets from each asset's `config`: a managed disk's `managedBy` VM
  (`disk → vm`), a NIC's `virtualMachine` (`nic → vm`), and a public IP's bound NIC
  (`ip → nic`, resolved up from the referenced ipConfiguration sub-resource). New
  `asset_relationships` table (auto-created by `init_db()`) with a unique
  `(source_id, target_id, kind)` triple; `source_id`/`target_id` are plain indexed
  columns (not FKs) so an edge can outlive either endpoint, like `asset_events`.
  `repo.build_relationships` resolves each reference against the stored assets
  **case-insensitively** (Azure resource ids are), **skips dangling/external
  references** (never fatal), and is **idempotent** (`ON CONFLICT DO NOTHING`;
  re-deriving over unchanged inventory writes nothing) — wired into the pipeline's
  store phase (`counts["asset_relationships"]`). New
  `GET /api/assets/{id}/relationships` returns an asset's neighbours in **both
  directions**, each row tagged `direction` (`inbound`/`outbound`) and the
  `neighbor` id. The mock `inventory.json` gains a NIC attached to `vm-web-01` so
  the graph is exercised end-to-end. TDD: `test_asset_relationships.py` (9 tests,
  DB-backed) covers `disk→vm` / `nic→vm` / `ip→nic` edges, case-insensitive
  resolution, no-references → no edges, dangling references skipped, idempotency,
  both-direction neighbours, and the endpoint — `api/main.py`, `models.py`,
  `schema.py` at 100% coverage.
- **M4.2 — Asset query API (filterable, injection-safe).** A structured query
  surface over AssetDB (à la Stacklet's SQL-enabled asset queries). New
  `POST /api/assets/query` takes an `AssetQuery` (a list of `AssetFilter`
  `{column, op, value}` clauses, an exact-match `tags` map, plus `limit`/`offset`)
  and returns the matching assets. The builder (`repo.query_assets`) is
  **injection-safe by construction**: `column` and `op` are checked against
  server-side **allow-lists** (`resource_id` / `subscription_id` / `resource_group`
  / `name` / `type` / `location` / `sku` / `state`; ops `eq` / `ne` / `contains` /
  `in`) — an unknown one raises `ValueError` → **HTTP 400**, never executed — and
  **every** value, including tag keys/values, is bound as a SQLAlchemy parameter, so
  a SQL-injection payload is a harmless literal (matches nothing; the table is
  untouched). `limit` is clamped to 500 and results come back in a stable order
  (`last_seen` desc, `resource_id` asc). TDD: `test_asset_query.py` (10 tests,
  DB-backed) covers type / tag / subscription+region filters, the `ne`/`contains`/
  `in` operators, unknown-column/operator/`in`-not-a-list → 400, a
  `'; DROP TABLE assets; --` payload returning zero rows with the table intact, and
  pagination caps + stable order — `api/main.py`, `models.py`, and the new builder at
  100% coverage.
- **M4.1 — AssetDB schema & ingestion.** The foundation of the M4 **AssetDB** — a
  queryable, near-real-time inventory of every cloud resource with its full config
  (à la Stacklet's AssetDB). Two new tables auto-created by `init_db()`: `assets`
  (a richer superset of `resources` — `resource_id` PK, `subscription_id`, `type`,
  `location`, `resource_group`, `name`, `sku`, `tags` JSONB, the **full resource
  `config`** JSONB, a coarse `state`, and `first_seen`/`last_seen`) and
  `asset_events` (an append-only change/audit trail — `resource_id`,
  `subscription_id`, `event_type`, `data` JSONB, `at`). `azure/inventory.py` now
  projects the full Resource Graph `properties` into `ResourceRecord.config`
  (fixtures extended accordingly), and the orchestrator's store phase persists
  assets each run via `repo.upsert_assets` — an idempotent `ON CONFLICT` upsert that
  stamps `first_seen` once, refreshes `last_seen`/`config`/`state`, and (via the
  Postgres `xmax = 0` trick) returns only the **newly inserted** ids so a `created`
  `asset_event` is recorded on **first sight** only. Re-ingesting the same resource
  updates it in place without duplicating rows, and each asset carries its
  (mock-retargeted) `subscription_id`. TDD: `test_assetdb_ingestion.py` (10 tests,
  DB-backed) covers insert / update-last-seen / idempotence / empty / event-on-first-
  sight-only / full-config capture / per-subscription tagging / end-to-end pipeline
  ingestion — `schema.py`, `models.py`, `inventory.py` at 100% coverage.
- **M3.4 — Per-policy compliance & health metrics.** Aggregates the pull-mode
  execution results (M3.1–M3.3) into per-policy health, surfaced via the API and a
  Grafana dashboard (Stacklet-style policy-health reporting). New SQL views in
  `storage/db.py`: `v_policy_health` (one row per policy that has executed at least
  once — `total_executions`, `succeeded`/`failed`, `total_matches`, distinct
  `subscriptions`, a rounded `success_rate`, and the `last_status` /
  `last_execution_at` of the most recent run, **aggregated across every
  subscription** the policy ran in) and `v_policy_compliance` (the finer per-(policy,
  subscription) grain). Both `INNER JOIN` policies to executions, so a policy that
  has never run is absent and the empty state is an empty list — never an error.
  New `repo.policy_health` read helper and `GET /api/governance/policy-health`
  endpoint. New provisioned Grafana dashboard `grafana/dashboards/policy-health.json`
  (avg success rate / policies executed / total matches stats, resources-matched-over-
  time timeseries, per-policy health table with a `success_rate` gauge, and a
  compliance-by-subscription table). TDD: `test_policy_health.py` (7 tests) covers
  the empty state, post-execution aggregates, the success-rate maths, multi-
  subscription aggregation, never-run-policy absence, and the API happy/empty paths
  — `db.py` and `api/main.py` at 100% coverage.
- **M3.3 — Execution history API & UI.** The read/review surface over the pull-mode
  runs from M3.2. Three thin FastAPI endpoints over the M3.1 repository helpers:
  `GET /api/policy-executions` (newest-first, filterable by any combination of
  `policy_id` / `subscription_id` / `status`, with `limit`; blank query-string
  filters normalize to "no filter" so an "all" dropdown returns everything),
  `GET /api/policy-executions/{execution_id}` (`404 execution not found` when
  unknown), and `GET /api/policy-executions/{execution_id}/matches` (the
  matched-resource drill-down, also `404` for an unknown execution). New Next.js
  **Executions** page (`frontend/app/executions/page.tsx`, linked from `Nav.tsx`):
  a history table with policy / subscription / status filter dropdowns that
  re-query the API on change, and a click-to-expand per-row drill-down that lazily
  fetches and caches each execution's matched resources (`resource_id` /
  `resource_type` / `action_taken`). Added `PolicyExecution` / `PolicyMatch`
  TypeScript interfaces to `lib/api.ts`. TDD: `test_policy_execution_api.py`
  (11 `TestClient` tests) covers the empty list, each filter alone and combined,
  `limit`, blank-filter normalization, both `404`s (asserting the specific detail),
  and the known-id happy paths — `api/main.py` at 100% coverage.
- **M3.2 — Pull-mode execution orchestrator.** Scheduled evaluation of every enabled
  Cloud Custodian policy against every enabled subscription, on its own cadence,
  independent of the cost-collection pipeline (Stacklet-style "pull mode"). New
  `orchestrator.run_policies(subscription, mock=None)` opens a `PolicyExecution`
  (`running`) per policy, evaluates it through the M2 engine's single mockable seam
  `custodian.engine.run_policy`, persists the matched resources as `PolicyMatch`
  rows, then closes the execution `succeeded` (with `resources_matched` +
  `actions_taken`) or `failed` (with `error`) — a single policy's failure is
  isolated to its own row and never aborts its siblings. `run_all_policies(mock=None)`
  fans that out across every enabled subscription with the same per-subscription
  isolation as `run_all_subscriptions`, seeding the default subscription on first
  use. Wired into a new `python -m azure_finops.cli run-policies [--mock]` command
  and a second, independently-cadenced APScheduler job (`finops-policy-run`) on
  `POLICY_RUN_INTERVAL_SECONDS` (new `Settings` field + `.env.example`). No test
  touches live Azure or a real c7n `PolicyCollection` — the engine seam is injected
  everywhere. TDD: `test_policy_orchestrator.py` (14 tests) covers per-policy and
  per-subscription failure isolation, the persisted execution/match rows,
  disabled-subscription skipping, declared-action recording, and the CLI/scheduler
  wiring. Rather than add a second mock path (a `policy_matches.json` read directly
  by the orchestrator), the orchestrator delegates entirely to the engine's existing
  `FINOPS_MOCK` fixture, preserving the "one mockable seam" design.
- **M3.1 — Execution results domain model & storage.** The persistence foundation
  for recording what Cloud Custodian actually did (à la Stacklet executions), ahead
  of the M3.2 orchestrator. Two new tables, auto-created by `init_db()`:
  `policy_executions` (one row per policy run — `execution_id` PK, `policy_id` FK →
  `policies.id`, `subscription_id`, `status` `running|succeeded|failed`, started/
  finished timestamps, `resources_matched`, `actions_taken` JSONB, `error`) and
  `policy_matches` (per-resource detail — FK → `policy_executions.execution_id`,
  `resource_id`, `resource_type`, `matched_at`, `action_taken`, `action_result`
  JSONB). Mirrored Pydantic transport models `PolicyExecution` / `PolicyMatch` let
  the orchestrator build results without importing SQLAlchemy. Six repository
  functions mirror the existing `create_run`/`finish_run` lifecycle:
  `create_policy_execution` (defaults to `running`), `finish_policy_execution`
  (stamps `finished_at` + terminal status/counts; no-op for an unknown id),
  `insert_policy_matches` (plain inserts, returns the count), `get_policy_execution`
  (`None` when missing), `list_policy_executions` (filter by any of `policy_id` /
  `subscription_id` / `status`, newest-first, limited), and `list_policy_matches`
  (newest-first). Pure storage — no orchestration or API here (that's M3.2/M3.3).
  Note: the FK targets the real `policies.id` PK (the issue's `policies.policy_id`
  predates the M2 schema). TDD: `test_policy_storage.py` (14 tests, DB-backed)
  covers table creation, the create→matches→finish lifecycle, each filter alone and
  combined, limit, ordering, and unknown-id → `None` — 100% coverage on the changed
  code.
- **M2.5 — Policy version history & diff.** Every content change to a policy is
  captured as an immutable snapshot for audit and rollback. New `policy_versions`
  table (FK to `policies`, `ON DELETE CASCADE`) recording `version` + the authored
  fields (`name`/`resource_type`/`spec`/`description`) + `actor`. `create_policy`
  seeds a **version-1** snapshot; `update_policy` now snapshots the new state and
  bumps the number **only when an authored field actually changes** — a no-op
  update (nothing supplied, or every value already equal) leaves the row and its
  history untouched. Repository adds `list_versions` (newest-first; `None` for an
  unknown policy), a pure `diff_versions` field-level diff, and a DB-backed
  `diff_policy_versions`. The API adds `GET /api/policies/{id}/versions` (`404`
  when missing) and `GET /api/policies/{id}/versions/diff?from_version&to_version`
  (`404` for an unknown policy/version). The **Policies** page gains a **History**
  panel that lists versions and compares any two. TDD: `test_policy_versions.py`
  (17 tests — pure-diff + DB-backed repo + API) covers create-seeds-v1 /
  create-on-change / monotonic numbers / no-version-on-noop / newest-first /
  unknown-404 / field diff — 100% line coverage on the changed code.
- **M2.4 — GitOps policy sync.** New `custodian/gitops.py` with
  `sync_policies(git_client=None, runner=None)` and a `POST /api/policies/sync`
  endpoint: it clones/pulls a configured Git repo (`GITOPS_REPO_URL` /
  `GITOPS_BRANCH` / `GITOPS_POLICY_PATH`), parses the policy YAML/JSON files,
  validates each policy through the engine, and **upserts by name** with
  `source='gitops'` (new `repository.upsert_policy_by_name` returning
  `added`/`updated`/`unchanged`). Unparseable or schema-invalid files are
  **skipped and reported** (non-fatal); the sync is **idempotent** (a no-op
  re-sync writes nothing — versions stay put); a clone/pull failure returns a
  structured error instead of a `500`. The `GitClient` seam is injectable (the
  default `LiveGitClient` shells out to `git`), so the whole pipeline is
  unit-tested offline. TDD: `test_gitops_sync.py` (13 tests, DB-backed + a
  `FakeGitClient` over a temp fixture repo + injected `FakeCustodianRunner`)
  covers import / update / skip-invalid-and-report / idempotence / `source=gitops`
  / clone-failure — 100% line coverage on the changed code. New `GITOPS_*` config
  in `config.py` + `.env.example`.
- **M2.3 — Policy collections.** Group policies into named **collections** (à la
  Stacklet policy collections). New `policy_collections` table + a
  `collection_policies` many-to-many join (both FKs `ON DELETE CASCADE`), so a
  policy can belong to any number of collections and **deleting a collection never
  deletes the member policies** — only the memberships. Repository adds
  `create_collection` / `get_collection` / `list_collections` / `delete_collection`
  / `add_policy_to_collection` / `remove_policy_from_collection` (+ a
  `_collection_public` serializer that embeds members); the API adds
  `GET/POST /api/collections`, `GET/DELETE /api/collections/{id}`, and
  `POST/DELETE /api/collections/{id}/policies/{policy_id}` (unknown policy or
  collection → `404`, duplicate name → `409`), with `CollectionCreate` /
  `CollectionRecord` models. A new Next.js **Collections** page manages collections
  and membership. TDD: `test_policy_collections.py` (14 tests, DB-backed) covers
  the repo + API happy paths and the delete-keeps-policies / unknown-policy-404 /
  multi-collection-membership invariants — 100% line coverage on the changed code.
- **M2.2 — Policy editor UI (Next.js).** A new **Policies** page
  (`frontend/app/policies/page.tsx`) plus a header nav link, consuming the M2.1
  CRUD API. Lists stored policies (name, resource type, source, `validation_status`
  and enabled badges, version) with per-row Edit / Enable-Disable / Delete, and a
  policy-spec editor with a **Validate** button (calls `POST /api/policies/validate`
  and shows schema errors inline without saving) and **Create/Update** that surfaces
  `422` validation errors and `409` duplicate-name errors inline **without
  navigating away**. `lib/api.ts` gains an `apiPut` helper, an `ApiError` that
  carries the response `status`/`body` (so 422 payloads render inline), and `Policy`
  / `ValidationResult` types; `globals.css` gains `.policy-editor` + validation
  styling. No backend changes — verified via `next build` (clean TypeScript compile)
  and an end-to-end mock-mode walkthrough (`docker compose up`).
- **M2.1 — Policy CRUD API.** A validate-on-write REST surface over the M1.2
  `policies` table: `GET /api/policies[?enabled=]`, `GET /api/policies/{id}`,
  `POST /api/policies`, `PUT /api/policies/{id}`, `DELETE /api/policies/{id}`, and
  `POST /api/policies/{id}/enabled?enabled=`. Every write is **gated by Cloud
  Custodian schema validation** — `POST`/`PUT` validate the `spec` first and return
  `422` with an `errors` array **without persisting** when it is invalid; a
  duplicate `name` returns `409` (caught from the DB unique constraint); an unknown
  id returns `404`; `PUT` re-validates a changed `spec` and bumps `version`. Since
  no invalid policy is ever stored, responses carry `validation_status: "valid"`.
  Validation reuses the M1.3 `get_custodian_runner` injection seam, and a new
  `PolicyUpdate` pydantic model backs partial updates. TDD:
  `test_policies_api.py` (13 tests, DB-backed + injected `FakeCustodianRunner`)
  covers list/filter, get, create (201/422-no-row/409), update (re-validate/404/409),
  delete (200→404), and the enable toggle — 100% line coverage on the changed code.
- **M1.4 — Policy dry-run endpoint.** New `POST /api/policies/{id}/dryrun`
  [`?subscription_id=…`] route: it loads a persisted policy (`404` if missing),
  optionally resolves a target subscription via `repo.get_subscription` →
  `SubscriptionContext` (`404` if unknown, else the default subscription), and
  evaluates the policy's `spec` with `engine.run_policy(dry_run=True)` — returning
  the **matched resources** without mutating anything. It reuses the M1.3
  `get_custodian_runner` injection seam and, in `FINOPS_MOCK=1` mode, sources the
  match set from `fixtures/custodian_policy_result.json`, so dry-runs are fully
  offline and **never** touch the remediation action executor. TDD:
  `test_policy_dryrun_api.py` (5 tests, DB-backed + injected `FakeCustodianRunner`)
  covers matched-resources, explicit-subscription, unknown-policy/​unknown-
  subscription `404`s, and a spy proving no action executor runs — 100% line
  coverage on the changed code.
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
