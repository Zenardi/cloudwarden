# Changelog

All notable changes to this project are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/) and [SemVer](https://semver.org/).

## [Unreleased]

### Added
- **Configuration drift detection (#140, M14.7).** "Drift" previously meant only
  policy-version drift — there was no way to know a resource changed away from its
  intended state. The AssetDB already stores full resource `config` (JSONB) and change
  history, so a new `custodian/drift.py` captures a per-resource desired-state **baseline**
  (a normalized config snapshot — volatile/noise fields dropped — plus a stable hash,
  versioned on re-baseline), and each run diffs live config against it: a recursive
  structural diff yields **added / removed / changed** dotted field paths
  (`properties.networkAcls.defaultAction`), each finding enriched with the recent
  Activity-Log change events that caused it (who/how). **Volatile fields are excluded** so
  an unchanged resource never drifts; findings persist idempotently (unique on
  `(resource_id, baseline_version, change set)`) and notify **once** through the existing
  transports (best-effort). Operators **re-baseline** (accept drift) via a new endpoint —
  explicit, RBAC-guarded and **audited**. New `DriftBaseline`/`DriftFinding` tables,
  `GET /api/drift` (RBAC `drift:read`) and `POST /api/drift/baseline` (RBAC `drift:write`,
  audited), a **Configuration drift** section on the asset-detail page (baseline-vs-current
  field diff + a re-baseline button), and a best-effort drift block in the run pipeline.
  Config: `DRIFT_DETECTION_ENABLED`, `DRIFT_ALERT_CHANNEL`. Azure-first behind the
  `CloudProvider` abstraction. Strict TDD, 100% coverage on the new module, ruff clean.
- **Shift-left IaC policy evaluation (#139, M14.6).** Every governance control ran *after*
  provisioning — a violation was caught only once the resource existed and billed. A new
  `custodian/shiftleft.py` runs the **same authored c7n policies** against a **Terraform
  plan** (`terraform show -json`) so a violation fails the **PR/CI before anything is
  created**. It `parse_plan`s the plan into flat resource dicts (walking child modules,
  keeping the Terraform address), maps each Terraform type to a c7n type
  (`azurerm_storage_account` → `azure.storage`; **unmapped types are skipped**, reported
  not errored), and evaluates each enabled policy's filters through the **offline c7n
  matcher** (`engine.match_resources` — the same local machinery a dry-run uses; the one
  mockable seam, injectable in tests), so it runs **fully offline** (`FINOPS_MOCK=1`) — no
  cloud, credentials, or live Terraform. Each match carries the policy, resource address,
  severity (from `metadata.severity`, default `medium`), and rationale; the worst severity
  maps to a **CI exit code** (`--fail-on <severity>` gates which findings block). A
  malformed plan is a **clean error** (CLI exit 2 / API `422`), never a stack trace. Ships
  as a CLI (`cloudwarden evaluate-iac <plan.json>`), `POST /api/policies/evaluate-iac`
  (RBAC `policy:run`), and a copy-paste GitHub Action
  (`docs/examples/shift-left.github-workflow.yml`). The optional live c7n IaC provider
  (`c7n_left`/`tfparse`) is registered best-effort when present
  (`engine.register_terraform`). Strict TDD, 100% coverage on the new module, ruff clean.
- **Showback / chargeback by tag → team (#138, M14.5).** CloudWarden enforced a
  cost-allocation tag but never reported spend by it. A new `analysis/allocation.py`
  groups spend by an arbitrary tag key (`CostCenter` / `Owner` / `Team` / `env`), maps
  tag values to the existing **teams** model, and surfaces an explicit **unallocated**
  bucket for untagged spend — the thing you actually drive down. Cost rows are now
  enriched with the owning resource's inventory tags (matched on the **lower-cased**
  `resource_id`) and persisted on a new `cost_snapshots.tags` (JSONB) dimension;
  aggregation groups by `tags ->> :key` with the tag key a **bound parameter**
  (injection-safe — a hostile key is a harmless lookup, never executed SQL). Invariants:
  untagged spend is never silently dropped, `allocated + unallocated == total` always
  reconciles, and a team-scoped principal sees **only** its own allocation (other teams'
  spend and the unallocated bucket never leak). A designated shared tag value can be
  split across the others — **even** or **proportional** (by spend). New
  `GET /api/costs/by-tag`, `GET /api/costs/showback`, and a streaming
  `GET /api/costs/showback/export` (CSV/JSON, reusing the governance-export streamer),
  all RBAC-guarded (`showback:read`); a **Showback** web page (allocation table +
  allocated/unallocated cards + export); a `v_cost_by_tag` Grafana view + *cost by owner*
  panel. Config: `SHOWBACK_TAG_KEY` (default `owner`), `SHOWBACK_TEAM_MAP`,
  `SHOWBACK_SHARED_TAG_VALUE`, `SHOWBACK_SPLIT_METHOD`. Azure-first behind the
  `CloudProvider` abstraction. Strict TDD, 100% coverage on the new module, ruff clean.
- **Cost forecasting (#137, M14.4).** Reporting says what we spent; leadership keeps
  asking where we'll **land**. A new `analysis/forecast.py` projects spend to
  **month-end** and **quarter-end** per scope (`total` / `subscription` / `service`) over
  `cost_snapshots`, each run, with a **transparent, explainable** model — an OLS linear
  **trend** plus multiplicative **weekday-seasonal** factors (a seasonal-naive + trend
  decomposition, not a black box). Every forecast carries its own credibility: a
  **prediction interval** (`[lower, upper]`, widening with residual spread and days
  remaining, floored at booked spend) and a **backtested accuracy (MAPE)** from a
  rolling-origin one-step-ahead backtest. It **degrades gracefully** — below a configurable
  minimum history it emits a clearly-labelled `confidence: low` linear estimate rather than
  a fabricated number or nothing. Budgets **consume** it: a `basis: "forecast"` threshold
  (M14.2) now fires off the projection computed the same run (the **forecasted-to-exceed**
  alert) via `forecast_for_budget` injected into `evaluate_budgets`. Forecasts persist to a
  new `cost_forecasts` table (unique on `(scope_type, scope_value, horizon, as_of)`,
  idempotent per day) and run best-effort so a failure never breaks a run. New
  `GET /api/costs/forecast` (RBAC `forecast:read`, filter by scope/horizon), a *Spend
  forecast* panel on the **Cost explorer** page, and a *Spend forecast — projection to
  period end* Grafana table. Azure-first behind the `CloudProvider` abstraction. Tuning via
  `FORECAST_WINDOW_DAYS` / `FORECAST_MIN_HISTORY_DAYS` / `FORECAST_BACKTEST_DAYS` /
  `FORECAST_CONFIDENCE_PCT` / `FORECAST_SEASONALITY`; toggle with `FORECAST_ENABLED`.
  Strict TDD, 99% coverage on the new module, ruff clean.
- **Cost anomaly detection (#136, M14.3).** Spend spikes were invisible until someone
  read a dashboard; detection makes FinOps watchful. A new `analysis/anomaly.py` scores
  the latest day's spend per scope (subscription / service / resource-type / resource)
  against a **robust, weekday-aware baseline** — rolling **median + MAD** deseasonalized
  by a per-weekday factor, so an in-pattern weekend peak is expected, not flagged. Days
  exceeding a configurable robust-sigma threshold become anomalies with a **severity**
  (low→critical) and a **contributor breakdown** (the child resources/meters that drove
  the delta). Detection is **signal-gated** — nothing is emitted on thin history (a
  configurable minimum) or an ultra-steady series (a scale floor prevents runaway
  scores) — so no false positives. Anomalies persist to a new `cost_anomalies` table
  (unique on `(scope_type, scope_value, usage_date)`), run every pipeline run after cost
  is committed, and fire **exactly one** notification per new anomaly through the
  **existing** transports (no new delivery code path), idempotent on `(scope, date)` and
  best-effort so a transport failure never breaks a run. New `GET /api/finops/anomalies`
  (RBAC `anomaly:read`, filter by scope/severity/date window), anomaly markers on the
  **Cost explorer** page, a *Cost anomalies* Grafana panel, and a demo mock spike
  (`fixtures/cost_anomaly.json`, off unless `ANOMALY_MOCK_SPIKE=1`). Azure-first behind
  the `CloudProvider` abstraction. Tuning via `ANOMALY_SENSITIVITY` /
  `ANOMALY_MIN_HISTORY_DAYS` / `ANOMALY_WINDOW_DAYS` / `ANOMALY_SEASONALITY`; toggle with
  `ANOMALY_DETECTION_ENABLED`. Strict TDD, 100% coverage on the new module, ruff clean.
- **Budgets & threshold alerting (#135, M14.2).** FinOps gains a first-class **budget**
  concept: a spend limit over a scope (subscription / account / account-group / tag /
  team) and period (monthly / quarterly) with ordered **threshold rules**
  (e.g. 80/100% of actual, plus forecast-basis rules once M14.4 lands). A new
  `analysis/budgets.py` evaluates actual spend vs each budget every pipeline run and
  scheduler tick; the first time a threshold is newly crossed it fires **exactly one**
  notification (for the highest newly-crossed threshold — no alert storms) through the
  **existing** notification transports (no new delivery code path), persists a
  `BudgetThresholdEvent` deduped on `(budget, period, threshold, basis)`, and never
  re-fires within the period — resetting on a new period. Budget mutations are
  RBAC-guarded (`budget:write`) and audited (`budget.create|update|delete`); reads are
  gated (`budget:read`). New `Budget` + `BudgetThresholdEvent` tables, repository CRUD,
  `GET/POST/PATCH/DELETE /api/budgets` + `GET /api/budgets/{id}/status`, a **Budgets**
  web page (create/edit + spend-vs-limit bars), and a *budget-vs-actual* Grafana panel.
  Azure-first behind the `CloudProvider` abstraction; tag/team scope degrades to a
  subscription match until the M14.5 tag dimension lands. Best-effort — a budget or
  alert failure never breaks a run. Toggle with `BUDGET_ALERTS_ENABLED`. Strict TDD,
  ≥95% coverage on the new modules, ruff clean.
- **Commitment coverage & RI/Savings-Plan recommendations (#134, M14.1).** Adds the
  single largest untapped FinOps lever: commitment-discount optimization. A new
  collector (`azure/reservations.py`, injectable + mock-fixture backed) gathers
  existing Reservations/Savings Plans (utilization, expiry, scope) and eligible
  steady-state on-demand usage per SKU family/region (aggregated — never raw
  samples), and a detector (`analysis/commitments.py`) emits three signals under the
  new `commitment` category: **under-utilized** commitments (advisory waste sized to
  the idle share), **expiring** commitments (informational), and **purchase
  candidates** sized to the **min-of-window** steady-state baseline (bursty usage
  yields none), priced with a blended discount across term (P1Y/P3Y) × payment
  (no/partial/all-upfront) with **break-even** months. Coverage % and blended
  utilization roll up per family/region. All savings are **estimates** with a `basis`
  + caveats, environment-weighted (`weight_commitment_savings`), AI-reconciled (new
  aggregated `commitment_coverage` block in the prompt), and persisted
  (`commitment_inventory` + `commitment_coverage` tables). Surfaced via
  `GET /api/finops/commitments` (RBAC-guarded, `commitment:read`), the
  **Recommendations** page's *Commitment coverage* panel, and two Grafana panels.
  Azure-first behind the `CloudProvider` abstraction (AWS/GCP are no-op stubs).
  Tunable via `COMMITMENT_UNDER_UTILIZED_PCT` / `COMMITMENT_EXPIRING_WITHIN_DAYS` /
  `COMMITMENT_MIN_HOURLY`. Strict TDD, ≥95% coverage on the new modules, ruff clean.

### Changed
- **Backend image shrunk ~271 MB by pruning the unused Azure SDK (#129, M13.6).**
  `c7n-azure` hard-pulls ~56 `azure-mgmt-*` provider SDKs (~480 MB) but imports
  them **lazily**; CloudWarden runs only a handful. The `Dockerfile` builder now
  deletes every `azure/mgmt/<provider>` not in
  [`backend/azure_mgmt_keep.txt`](backend/azure_mgmt_keep.txt) (39 providers), cutting
  the image **~1.13 GB → ~859 MB (−271 MB, ~24%)** with **zero functional change**
  — full offline suite, all 5 bundled packs (18 policies) validating through the
  real c7n engine, and remediation paths all stay green. Two guards prevent an
  over-prune: a **build-time smoke** (re-registers `azure.*` + imports every kept
  provider — fails the build if one is missing) and a **drift test**
  (`backend/tests/test_azure_footprint.py`) that fails CI if the keep-list stops
  covering a needed SDK. `requirements.txt` / `requirements.lock` are unchanged
  (pruning is post-install), so the hash-pinned lock, `pip --require-hashes` gate,
  and SBOM stay consistent; `trivy fs` + `image` show no new HIGH/CRITICAL.

### Fixed
- **Overview design-critique remediation — trust, accessibility, and shareable
  scope.** Resolves the P1–P3 findings from an `/impeccable critique` of the
  Overview. **Honest cloud filter:** the savings-vs-spend ratio is now suppressed
  under an active cloud filter (it had divided an all-cloud savings numerator by a
  provider-scoped spend denominator, yielding an inflated figure), and the three
  panels the filter does *not* re-scope — savings KPI, recommendations, AI
  summary — carry an **"All clouds"** tag so mixed scope never reads as filtered.
  The savings KPI gained a **basis label** ("estimate · from AI summary" /
  "summed from N recommendations"). **Shareable scope:** the cloud + range persist
  to the URL (`?days=&provider=`, validated), so a reload or a shared link
  restores the filtered view (the page stays statically prerendered). **A11y:**
  the cloud and range selectors are now proper `radiogroup`/`radio` controls
  (roving tabindex + arrow keys) with visible **"Cloud" / "Range"** captions
  serving as their accessible names. **Typography:** the standalone section
  headings unify into one sentence-case treatment, distinct from the dense
  uppercase panel labels. Error/validation surfaces use `color-mix` tokens rather
  than a stale `--red` literal. New unit-tested helpers `deriveSavings`,
  `parseScope`/`scopeToQuery`, and `nextRadioIndex` (frontend suite 23 → 49 tests,
  100% coverage on the gated modules).

### Added
- **Scale & performance testing (#55, M13.5).** Adds a repeatable **load test**
  (`backend/tests/perf/test_execution_scale.py`) that proves policy execution
  scales: it runs a binding across **≥50 subscriptions × ≥20 policies** (≥1000
  executions) through the real execution + persistence path (`run_binding`) with
  an **offline mock runner** (no c7n/Azure/network) against a throwaway Postgres,
  and asserts the run completes within a documented **time budget** (120s — a
  regression ceiling, not an SLA; a full run is ~7s locally) and **memory ceiling**
  (256 MB peak heap). Throughput is recorded as a JSON artifact, and a pure
  **regression detector** flags a drop beyond a 25% tolerance. The heavy tests
  carry a `perf` marker and are **excluded from the default PR run** (`addopts =
  -m 'not perf'` in `pyproject.toml`); a new **`perf`** CI job runs them **nightly**
  (cron) + on `workflow_dispatch` — **skipped on PRs** (non-blocking), **blocking
  on a budget breach nightly** — and uploads the results. `make perf` runs them
  locally. See [`backend/tests/perf/README.md`](backend/tests/perf/README.md).
- **Observability: metrics, tracing & structured logs (#54, M13.4).** Adds the
  operability layer behind the governance platform. **Metrics:** a `GET /metrics`
  endpoint exposes Prometheus counters for **policy executions**
  (`cloudwarden_policy_executions_total`, by terminal status) and **remediation
  actions** (`cloudwarden_remediation_actions_total`, by action type + status),
  plus a policy-execution duration histogram. **Readiness:** `GET /ready` returns
  `200` when the database is reachable and `503` when it is not — distinct from
  `/health` liveness — so an orchestrator stops routing traffic to a pod whose DB
  is down. **Structured logs:** every log line is single-line JSON carrying a
  per-request **correlation id** (accepted from / echoed as `X-Correlation-ID`).
  **Tracing:** execution runs (`POST /api/runs`) are wrapped in **OpenTelemetry**
  spans; no exporter is configured by default, so spans stay in-process (no network
  egress) until one is wired up. New zero-config `cloudwarden/observability.py`
  (no internal imports → safe to use from storage/remediation/API);
  `prometheus-client` + `opentelemetry-api`/`-sdk` pinned in `requirements.txt` and
  folded into the hash-pinned `requirements.lock`. TDD-first:
  `backend/tests/test_observability.py` (21 tests) covers the counters, both
  `/ready` states, the JSON formatter + correlation id, and span creation.
- **SBOM, dependency pinning & secret scanning (#53, M13.3).** Closes the
  supply-chain and credential gaps. **SBOM:** a new `supply-chain` CI job runs
  [syft](https://github.com/anchore/syft) over the built backend image and uploads
  an SPDX-JSON Software Bill of Materials (`backend-sbom`) as a build artifact.
  **Hash-pinned dependencies:** `backend/requirements.lock` — the fully-resolved,
  fully-hashed transitive closure of `requirements.txt` (`pip-compile
  --generate-hashes`) — is installed in CI with **`pip --require-hashes`**, so a
  substituted or tampered wheel fails the build (the lock resolves to *patched*
  `cryptography`/`PyJWT`). **Secret scanning:** a new `secrets` CI job runs
  [gitleaks](https://github.com/gitleaks/gitleaks) over the tree and **blocks the
  build on any finding**; the reviewed allowlist in `.gitleaks.toml` excepts only
  the local git-ignored `.env` (a non-secret Azure client-id UUID), documented
  inline. New `make lock` / `make sbom` / `make secrets` targets run each gate
  locally. TDD-first: `backend/tests/test_supply_chain.py` (7 tests) asserts the
  SBOM generation + upload, the hash-pinned lock + `--require-hashes` install, and
  the blocking gitleaks gate + documented allowlist.
- **Coverage & mutation-testing gate (#52, M13.2).** Hardens the quality bar two
  ways. **Coverage:** the CI backend job now enforces the 95 % floor explicitly
  (`pytest --cov-fail-under=95`, backing the existing `fail_under = 95` in
  `pyproject.toml`), so a drop below 95 % fails the build. **Mutation testing:** a
  new advisory `mutation` CI job runs `mutmut` over the core governance modules
  (`analysis/`, `custodian/`, `remediation/`) to measure whether tests actually
  *catch* injected bugs, reporting a **mutation score** against a documented
  threshold (**≥80 % of tested mutants killed**). It is non-blocking initially
  (`continue-on-error`) and scheduled to flip to blocking. `mutmut==3.6.0` is
  pinned in `requirements-dev.txt` (2.x's pony-ORM cache is broken on Python 3.13);
  config lives in `backend/setup.cfg` `[mutmut]` and runs from `backend/` so mutated
  paths match the tests' imports. The autouse test `chdir` isolation now yields to
  mutmut's sandbox (`MUTANT_UNDER_TEST`). New `backend/tests/test_quality_gates.py`
  (TDD-first) asserts the coverage floor, the CI coverage + mutation steps, the
  core-module targeting, and the mutmut pin. Run locally with `make mutation`.
- **Trivy CVE gate in CI — fs + image + config, fail on HIGH/CRITICAL (#51, M13.1).**
  The `security` job now runs three Trivy scans that fail the build on any
  HIGH/CRITICAL finding (`--severity HIGH,CRITICAL --exit-code 1`): `trivy fs`
  (dependencies), `trivy image` (the built backend + frontend images) and the new
  `trivy config` (IaC / Dockerfile misconfigurations). The vuln scans keep
  `--ignore-unfixed` so only *fixable* CVEs block the build. A reviewed root
  **`.trivyignore`** documents accepted exceptions (currently none — the gate is
  clean) and its justification convention is enforced by
  `backend/tests/test_ci_trivy_config.py` (TDD-first: it parses `ci.yml` to assert
  the fs/image/config steps + severity/exit-code, and that every suppression is
  justified). Contributors can run the same gate locally with **`make trivy`**
  (Trivy fs + config over the pinned official Docker image; see README).
- **Overview scoping — multi-cloud filter + date-range across every panel (#116).**
  Resolves the [P1]: the Overview no longer hardcodes a 30-day, all-cloud view.
  A **cloud filter** (All / Azure / AWS / GCP) and the 7/30/90d range control now
  scope **all** cost + governance panels consistently — the cost KPI, cost
  drivers, governance posture, and trend. `GET /api/costs/summary|by-type|
  by-region` accept **`?days=`** (1–365, default 30) and **`?provider=`**
  (`azure|aws|gcp|all`), both bound parameters (injection-safe); the day window
  reuses #113's `make_interval`, and the provider maps through
  `subscriptions.provider`. `days` is clamped 1–365 and an unknown `provider`
  is rejected with **400**. The trend endpoint remains day-scoped (#113); the
  provider filter applies to costs + posture (which already accepted it). New
  `ScopeControls` component + `costScopeQuery` helper; repo `total_cost` /
  `cost_by_type` / `cost_by_region` gain `days`/`provider` params.

### Fixed
- **Overview a11y — single refresh announcement + 44px touch targets (#115).**
  Two P2 accessibility fixes. (1) The KPI trio and the AI-summary carried
  `aria-live="polite"` on their containers, so a screen reader re-read the whole
  block on every refresh / `r`. They now delegate to one dedicated
  visually-hidden `role="status"` region that announces a single concise message
  ("Data refreshed, as of …"); the containers keep `aria-busy` but no longer
  re-read wholesale. (2) Secondary links (`.panel-link` / `.card-link`) get a
  **≥44×44px** hit area on coarse pointers via an invisible centered overlay —
  visual size unchanged (WCAG 2.5.5 / 2.5.8). New `RefreshStatus` component;
  RTL regression guards keep the amortization caveat screen-reader reachable.

### Added
- **Overview cost-KPI trend — Δ badge, sparkline, 7/30/90d control (#114).** The
  Cost KPI now consumes `/api/costs/trend` (#113) to answer *"what changed?"*:
  a direction-carrying **delta badge** (arrow + sign + text — never colour alone,
  WCAG 1.4.1), an inline **SVG sparkline** of the daily series (decorative,
  `aria-hidden`; degrades to a flat baseline for empty/single-point/flat data),
  and a **7 / 30 / 90d segmented control** in the header that re-pulls just the
  trend window. `delta_pct === null` (empty prior window) shows *"vs prior Nd —
  n/a"* instead of a bogus percentage, and a loading/failed trend fetch renders
  no delta at all (never a fabricated figure). New `getCostTrend(days)` +
  `CostTrend` types in `lib/api.ts`; new `lib/trend.ts` helpers (`formatDelta`,
  `sparklinePath`) and `Sparkline` / `CostTrend` / `RangeControl` components.
- **Frontend test harness — Vitest + React Testing Library.** Introduces the
  project's first JS test runner (jsdom, `@vitest/coverage-v8`) with `npm test`
  and `npm run test:cov`. New frontend modules are held to **≥95% line coverage**
  (the #114 modules land at 100%).
- **Cost-trend endpoint — Δ vs the prior period + a daily series (#113).** New
  read-only **`GET /api/costs/trend?days=30`** returns
  `{ days, currency, total, prior_total, delta, delta_pct, series[] }`: the
  Amortized cost for the current `days`-day window, the immediately prior window
  of equal length, their delta, and a daily ISO `series` across the current
  window. `delta_pct` is `null` when the prior window is empty (no bogus % on a
  first-ever period); `days` is clamped **1–365** and bound as a query parameter
  (injection-safe). Pure read-side SQL over the existing daily `cost_snapshots`
  granularity — no new collection or migration. Backs the Overview's
  *"what changed?"* KPI (#114). New `repo.cost_trend(session, days)`.
- **Cross-cloud AssetDB & dashboards — the single multi-cloud pane (M12.4).** The `provider`
  dimension now unifies Azure/AWS/GCP across AssetDB and the governance surface. Asset queries
  already filter by the allow-listed **`provider`** column (`POST /api/assets/query` with a
  `provider eq aws` filter returns only that cloud's assets). Compliance **posture** and
  **execution-health** each grow a **`by_provider`** rollup and accept an optional
  **`?provider=azure|aws|gcp`** filter that defaults to *all clouds* (`?provider=all` or omitted).
  Provider is intrinsic to the account — an execution's provider is its subscription's `provider`
  (an un-onboarded subscription defaults to `azure`, mirroring the `server_default` backfill):
  `v_governance_posture` now carries a `provider` column, and a new
  **`v_execution_health_by_provider`** view aggregates engine health per cloud. The **assets**
  and **compliance** UIs gain a *Cloud* dropdown (and an asset *Cloud* column / a posture
  by-provider strip), and both Grafana boards (**Compliance Posture**, **Execution Health**) gain
  a `provider` template variable — all defaulting to **all clouds**. No new dependency, no schema
  migration (the provider columns were added with `server_default='azure'` in M12.1/M12.2).
- **GCP onboarding & execution — the third cloud (M12.3).** New
  `cloudwarden.providers.gcp.GcpProvider` (registered as `providers.registry.get("gcp")`)
  onboards GCP projects, runs Cloud Custodian **gcp** policy dry-runs, and ingests GCP
  resources into AssetDB tagged **`provider='gcp'`**. Onboarding validates credentials via
  Resource Manager `get_project` through an **injectable** client seam
  (`POST /api/gcp/projects`; a bad/expired credential or a project mismatch → `400`), then
  persists the project with `provider='gcp'`. `POST /api/gcp/projects/{id}/ingest` loads the
  `gcp_assets` fixture into AssetDB (idempotent), and `POST /api/gcp/policies/dryrun` returns
  the fixture resources matching a policy's `resource` type — reusing the `provider` column
  added in M12.2 (no schema change). Unlike AWS (native to c7n core), GCP lives in the separate
  `c7n-gcp` package, which pulls the heavy `google-*` tree; to keep the image and its Trivy
  surface minimal, that is an **optional live-only extra** (not installed by default) — the
  live paths lazily import it (`# pragma: no cover`). Every test uses injected clients / offline
  fixtures — **no live GCP call**.
- **AWS onboarding & execution — the second cloud (M12.2).** New
  `cloudwarden.providers.aws.AwsProvider` (registered as `providers.registry.get("aws")`)
  onboards AWS accounts, runs Cloud Custodian **aws** policy dry-runs, and ingests AWS
  resources into AssetDB. Onboarding validates credentials via STS
  `get_caller_identity` through an **injectable** client seam (`POST /api/aws/accounts`;
  a bad/expired credential or an account mismatch → `400`), then persists the account with
  `provider='aws'`. `POST /api/aws/accounts/{id}/ingest` loads the `aws_assets` fixture into
  AssetDB tagged **`provider='aws'`** (idempotent), and `POST /api/aws/policies/dryrun`
  returns the fixture resources matching a policy's `resource` type. The **`assets`** and
  **`resources`** tables gain a `provider` column (`server_default='azure'`, so existing rows
  read as Azure) — filterable via the asset query API. **No new image dependency:** AWS is
  native to the already-installed `c7n` core (no `c7n-aws` package; `boto3` ships transitively
  and is now pinned explicitly), so the Trivy surface is unchanged. Every test uses injected
  clients / offline fixtures — **no live AWS call**.
- **Cloud provider abstraction — the multi-cloud foundation (M12.1).** New
  `cloudwarden.providers` package introduces a `CloudProvider` interface
  (`providers.base`) and a name-keyed registry (`providers.registry`) so the engine,
  orchestrator and onboarding talk to a provider seam instead of Azure directly.
  `providers.registry.get("azure")` resolves the Azure implementation
  (`providers.azure.AzureProvider`), which now owns Cloud Custodian resource
  registration, the c7n resource registry, and session construction; an unregistered
  name raises `UnknownProviderError` rather than silently defaulting. The Azure-only
  `SubscriptionContext` is generalized to a provider-neutral **`AccountContext`**
  (`provider` + `account_id` + optional credential), with `SubscriptionContext` retained
  as a backward-compatible alias (`subscription_id` maps onto `account_id`) so every
  existing collector keeps working unchanged. Accounts gain a **`provider` column**
  (`server_default='azure'`, so pre-existing rows read as Azure) surfaced through
  `GET /api/subscriptions` and settable via `POST /api/subscriptions`. This is a pure,
  behaviour-preserving refactor — the entire existing suite stays green.
- **Audit log — append-only trail of mutating governance actions (M11.4).** New
  `audit_log` table and an `cloudwarden.authz.audit` helper. Creating, updating,
  deleting or enabling/disabling a policy writes one row capturing the actor (resolved
  RBAC/SSO principal), the action (`policy.create` / `policy.update` / `policy.delete` /
  `policy.enable` / `policy.disable`), the target (`target_type`/`target_id`), and the
  before/after state as JSONB (a create has an empty `before`; a delete an empty
  `after`) — written inside the mutation's own transaction, so it commits atomically.
  **Reads are never audited.** `GET /api/audit` lists entries newest-first (with `id` as
  the tiebreaker), filterable by `actor` / `action` / `target_type` / `target_id` and
  paginated by `limit`/`offset`; a new **Audit** UI page renders the trail. The log is
  append-only by construction — there is no update or delete path in the repository or
  the API (mutating verbs on `/api/audit` return `405`).
- **SSO / OIDC authentication (M11.3).** New `cloudwarden.authz.oidc` module adds an
  identity layer that feeds the RBAC principal. The API accepts either an OIDC bearer
  token (`Authorization: Bearer <jwt>`, verified with PyJWT — RS256 signature +
  `exp`/`iss`/`aud` — against a static public key or the issuer's JWKS) or a first-party
  session cookie (`finops_session`, a short-lived HS256 JWT issued after login); the
  verified subject becomes the RBAC principal (`rbac.principal_from_request` delegates to
  OIDC when enabled), and an expired/invalid credential is **401**. Login flow:
  `GET /api/auth/login` → IdP → `GET /api/auth/callback` (sets the session cookie) →
  `POST /api/auth/logout`; these routes **404 when OIDC is disabled**. Gated by
  **`OIDC_ENABLED`** (off by default, so local/mock dev stays unauthenticated); config
  adds `OIDC_ISSUER` / `OIDC_CLIENT_ID` / `OIDC_CLIENT_SECRET` / `OIDC_REDIRECT_URI` /
  `OIDC_PRINCIPAL_CLAIM` / `OIDC_PUBLIC_KEY` / `SESSION_SECRET`. Both the token verifier
  and the OIDC client are injectable, so the whole flow is unit-tested offline (no IdP
  contacted). The Next.js UI gates behind `/login` when `NEXT_PUBLIC_AUTH_ENABLED=true`.
- **Teams & membership — team-scoped multi-tenancy (M11.2).** New `teams` and
  `team_members` tables and an `cloudwarden.authz.teams` module scope governance
  resources to an owning team. Policies gain a nullable `team_id`
  (`ON DELETE SET NULL`); when RBAC is enabled, creating a policy assigns the caller's
  team as owner (derived from membership, or an explicit `team` name the caller must
  belong to). `GET /api/policies` returns only the caller's team's policies for a
  member and all for an admin (RBAC wildcard); a non-admin that reads, updates or
  deletes a policy in another team gets **403**, and removing a member revokes their
  access. Team administration — `POST /api/teams`, `POST`/`DELETE /api/teams/{id}/members`
  — requires the admin-only `team:write` permission; `GET /api/teams`,
  `GET /api/teams/{id}` and `GET /api/teams/{id}/members` are readable. Scoping is
  gated by the same **`RBAC_ENABLED`** flag, so with RBAC off listings are unscoped and
  the API stays backward-compatible. The scoping core (`is_admin` / `visible_team_ids`
  / `ensure_policy_access` / `resolve_owning_team`) is unit-tested in isolation.
- **RBAC — roles, permissions & role bindings with endpoint enforcement (M11.1).**
  New `roles` / `permissions` / `role_bindings` tables and an `cloudwarden.authz.rbac`
  module. A `require_permission(action)` FastAPI dependency guards every mutating
  endpoint: it reads the caller from the `X-Principal` header, resolves the union of its
  bound roles' permission grants, and enforces the route's action — **401** with no
  principal, **403** without the permission (reads stay ungated). Three roles are seeded
  idempotently — **admin** (`*`), **editor** (all write/run actions except RBAC admin),
  **viewer** (read-only). Enforcement is gated by **`RBAC_ENABLED`** (off by default, so
  the existing unauthenticated API is unchanged); **`RBAC_BOOTSTRAP_ADMIN`** names a
  principal auto-bound to `admin` at seed time. New endpoints: `GET /api/authz/me`,
  `GET /api/authz/roles`, `GET`/`POST`/`DELETE /api/authz/role-bindings` (writes require
  `rbac:admin`). The permission check core (`has_permission` / `check_permission`) is
  unit-tested in isolation.
- **CIS Azure compliance pack + posture grouped by control id (M10.4).** A starter
  subset of the CIS Microsoft Azure Foundations Benchmark mapped to Cloud Custodian
  policies — a **directory pack** at `backend/cloudwarden/packs/cis-azure/` that
  installs into a **CIS Azure** collection. Five controls: 3.1 (storage secure
  transfer), 3.8 (storage default-deny network rule), 6.1 (restrict RDP from the
  internet), 6.2 (restrict SSH from the internet), and 7.3 (disk CMK encryption).
  Each policy carries its CIS control id in `metadata.control_id`, and
  `repo.governance_posture` / `GET /api/governance/posture` now returns a
  **`by_control`** rollup that groups compliant/non-compliant counts by control id
  (extracted from each policy's stored spec via JSONB); policies without a control id
  are excluded.
- **Security & tagging-hygiene pack (M10.3).** A curated **directory pack** at
  `backend/cloudwarden/packs/security/` (`pack.yaml` manifest + one `*.yml` per
  policy) that installs into a **Security Baseline** collection. Four policies:
  `security-public-ip-exposure` (public IPs with an assigned address),
  `security-nsg-permissive-inbound` (NSGs allowing inbound from `0.0.0.0/0` to
  SSH/RDP via c7n-azure's `ingress` filter), `security-required-tags` (resources
  missing a mandated `Environment`/`Owner` tag), and `security-unencrypted-disk`
  (disks not encrypted with a customer-managed key). Each policy also declares a
  remediation action (a marker `tag`) that runs **dry-run only** under a binding.
  Every policy is schema-valid via the engine, and the required-tags policy matches
  a resource missing a mandated tag (offline `engine.match_resources`).
- **Cost governance pack — FinOps heuristics as Cloud Custodian policies (M10.2).**
  A curated **directory pack** at `backend/cloudwarden/packs/cost/` (`pack.yaml`
  manifest + one `*.yml` per policy) installs into a **Cost Governance** collection.
  Five policies: `cost-idle-vm-deallocated` (deallocated/stopped VMs),
  `cost-unattached-disk` (Unattached managed disks), `cost-idle-public-ip`
  (unassociated static public IPs), `cost-oversized-vm` (VMs on ≥ 8 vCPU SKUs), and
  `cost-untagged-cost-centre` (VMs missing a CostCenter tag). The pack registry now
  supports **directory packs** (manifest + policy files) alongside M10.1's single-file
  packs, and honors an optional `collection` name. New engine helper
  `custodian.engine.match_resources(spec, resources)` runs c7n's filter machinery
  **offline** (no Azure) so a policy can be dry-run against recorded/inventory data;
  every cost policy is schema-valid via the engine, and the idle-VM / unattached-disk
  policies match the mock idle/orphan fixtures.
- **Policy packs — installable, versioned bundles of curated policies (M10.1).**
  Curated Cloud Custodian policies now ship as **packs** (YAML under
  `backend/cloudwarden/packs/defs/`): `cost-hygiene` (unattached disks,
  unassociated public IPs) and `tag-compliance` (Environment / CostCenter tag
  baselines). `cloudwarden.packs.registry` discovers them (`list_packs` /
  `get_pack`) and installs one (`install_pack`) by **validating every policy
  through the engine, then materializing** the (upsert-by-name, `source='pack'`)
  policies plus a collection named after the pack, tracking the installed version
  in a new `installed_packs` table. Install is **atomic on validation** (a pack
  with any invalid policy installs nothing and is reported) and **idempotent**
  (re-installing the same version reuses the collection, creating no duplicates).
  New endpoints: `GET /api/packs`, `GET /api/packs/installed`,
  `POST /api/packs/{name}/install` (`404` unknown, `422` invalid),
  `POST /api/packs/{name}/enabled` — enabling/disabling a pack cascades to its
  member policies' `enabled` flag, toggling their binding eligibility.

### Changed
- **CI — bumped deprecated GitHub Actions off the retiring Node 20 runtime.**
  `actions/checkout@v4 → v5`, `actions/setup-node@v4 → v5`, and
  `actions/setup-python@v5 → v6` in `.github/workflows/ci.yml` — clears the
  "Node.js 20 is deprecated … forced to run on Node.js 24" annotations
  ([GitHub Actions Node 20 deprecation](https://github.blog/changelog/2025-09-19-deprecation-of-node-20-on-github-actions-runners/)).
  The frontend build's `node-version: "20"` input is left as-is on purpose — it
  mirrors the `node:20-alpine` image the frontend Dockerfile ships (the deprecation
  is about the action *runtime*, not the build Node).
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
- **M9.4 — Governance reporting & export.** Periodic evidence for stakeholders. New
  streaming endpoint `GET /api/governance/export?format=csv|json` (`api/main.py`) emits
  one row per policy execution (policy, subscription, status, matches, timing) — CSV with
  a header row, JSON as an array of the same records; any other `format` → `400`. It
  streams from a new **paginated cursor** `repo.iter_governance_export()`
  (`storage/repository.py`, `LIMIT`/`OFFSET` in `batch_size` pages) so an arbitrarily large
  history is never held in memory; the session lives inside the `StreamingResponse`
  generator. New `reporting.py` centralizes CSV/JSON serialization (`stream_export` /
  `generate_report` / `write_report`), shared by the endpoint and an **optional scheduled
  report**: `scheduler._schedule_governance_report()` registers a periodic job (gated by
  `GOVERNANCE_REPORT_ENABLED`, cadence `GOVERNANCE_REPORT_INTERVAL_SECONDS`) that writes a
  timestamped CSV under `APP_DATA_DIR`, failure-wrapped so it never kills the scheduler.
- **M9.3 — Resource compliance explorer (Next.js).** A `/compliance` drill-down for
  investigating non-compliance: policy → matched resources → asset detail. New backend
  endpoint `GET /api/governance/policies/{policy_id}/matches` (`api/main.py`) + repository
  helper `policy_matched_resources()` (`storage/repository.py`) return the resources
  flagged by each subscription's **latest** execution of the policy — the current
  non-compliant set (its size equals the policy's posture `violations`), newest match
  first, each carrying `resource_id` / `resource_type` / `subscription_id` / `matched_at`;
  `404` for an unknown policy, `[]` when it has no matches (`policy_id` is bound —
  injection-safe). New Next.js page `frontend/app/compliance/page.tsx`: a policy list
  (non-compliant counts from the M9.1 posture rollup) drilling into the matched
  resources, each linking through to its M4.5 AssetDB detail (`/assets/<resource_id>`),
  with empty (compliant) and error states handled inline. `lib/api.ts` gains
  `getGovernancePosture` / `getPolicyMatchedResources` + `Posture` / `PosturePolicy` /
  `MatchedResource` types; a **Compliance** link joins the nav.
- **M9.2 — Policy execution health dashboard.** The governance engine's *own*
  health. New `v_execution_health` (per policy) and `v_execution_health_by_binding`
  (per binding) SQL views (`storage/db.py`) aggregate executions into
  succeeded/failed counts, a rounded `success_rate`, the average wall-clock
  `avg_duration_seconds` (over finished runs — `EXTRACT(EPOCH …)` cast to numeric
  before `ROUND`), and `last_status` / `last_execution_at`. New repository helper
  `execution_health()` (`storage/repository.py`) returns `{by_policy, by_binding}`,
  newest-executed first (pull-mode runs with no binding are counted per-policy but
  excluded from the per-binding grain). New endpoint
  `GET /api/governance/execution-health` (`api/main.py`) is a thin read — both lists
  empty until a policy has executed, never an error. New provisioned **Policy
  Execution Health** Grafana dashboard (`grafana/dashboards/execution-health.json`):
  success-rate / executions / failed / avg-duration stats, success-rate-by-policy
  bar gauge, duration-over-time trend, and per-policy / per-binding health tables.
- **M9.1 — Compliance posture dashboard.** The governance console's headline view.
  New `v_governance_posture` SQL view (`storage/db.py`) takes the **latest execution
  per (policy, subscription)** — ordered `started_at DESC, execution_id DESC`, mirroring
  `v_policy_health` — and flags that pair **compliant** (matched nothing) or
  **non-compliant** (matched ≥1 resource). New repository helper
  `governance_posture()` (`storage/repository.py`) rolls the view up three ways
  (`by_policy`, `by_subscription`, `by_collection`) plus a `totals` block
  (`compliant` / `non_compliant` / `violations` / `evaluated`); the four count/violation
  measures are shared via a single `_POSTURE_MEASURES` fragment. New endpoint
  `GET /api/governance/posture` (`api/main.py`) is a thin read over the helper —
  with nothing executed yet it returns zeroed totals and empty group lists, never an
  error. New provisioned **Compliance Posture** Grafana dashboard
  (`grafana/dashboards/governance-posture.json`): posture-split donut, compliance-rate
  and violation stats, violations-over-time trend, and per-policy / per-subscription
  posture tables (all `COALESCE`/`NULLIF`-guarded so the empty state renders zeros).
- **M8.4 — Per-binding notify config & UI.** Wires the M8.1–M8.3 notification
  machinery to **bindings**. New `binding_notifications` table (`storage/schema.py`)
  attaches one or more **(channel, template)** pairs to a binding, with repository CRUD
  (`create_/list_/delete_binding_notification`, plus the previously-missing
  `update_notification_template`). New `notify/dispatch.py`: a transport registry
  (`build_transport`, `KNOWN_TRANSPORTS`) mapping a channel's kind →
  webhook/slack/email/teams/jira/servicenow, and `dispatch_for_binding()` which renders
  each attached template from the violation context and dispatches via an **injected**
  transport factory (the test seam). Hooked into `custodian/bindings.py`'s binding
  executor **after** the execution commits and wrapped best-effort — a violation on a
  binding with a channel fires a notification; a binding without one fires nothing; a
  failed notification never breaks enforcement. New API endpoints (`api/main.py`): full
  CRUD for `/api/notification-channels` and `/api/notification-templates` (bad transport
  kind or duplicate name → `400`) and attach/list/detach on
  `/api/bindings/{id}/notifications` (unknown ref → `404`, duplicate channel → `409`).
  New **`/notifications`** Next.js page (+ Nav link + `lib/api.ts` client) manages
  channels and templates. 17 TDD tests (`test_notify_config.py`); new backend code at
  100% coverage.
- **M8.3 — Teams, Jira & ServiceNow transports.** Three more concrete transports on
  the same `send(*, target, subject, body, config)` seam extend delivery to ITSM /
  collaboration systems, each with an **injectable** HTTP client and the same
  capture-don't-raise contract. New `notify/transports/teams.py` `TeamsTransport` POSTs
  a legacy **MessageCard** (`title`/`text`) to a Teams incoming webhook (target →
  `config["webhook_url"]` → `TEAMS_WEBHOOK_URL`). New `notify/transports/jira.py`
  `JiraTransport` **creates an issue** via `POST {JIRA_BASE_URL}/rest/api/2/issue`
  (subject → `summary`, body → `description`, project from target → `config["project"]`
  → `JIRA_PROJECT`, issue type from `config["issue_type"]` → `JIRA_ISSUE_TYPE`) and
  returns the new key. New `notify/transports/servicenow.py` `ServiceNowTransport`
  **creates an incident** via `POST {SERVICENOW_INSTANCE_URL}/api/now/table/incident`
  (subject → `short_description`, body → `description`, optional
  `urgency`/`impact`/`assignment_group`/`caller_id`/`category` from config) and returns
  the incident number. All capture an auth/permission error (non-2xx), a network
  exception, or missing config as `{"ok": false, "error": …}` rather than raising. New
  config defaults (`config.py` + `.env.example`): `TEAMS_WEBHOOK_URL`, `JIRA_BASE_URL` /
  `JIRA_EMAIL` / `JIRA_API_TOKEN` / `JIRA_PROJECT` / `JIRA_ISSUE_TYPE`,
  `SERVICENOW_INSTANCE_URL` / `SERVICENOW_USER` / `SERVICENOW_PASSWORD`. 15 TDD tests
  (`test_notify_enterprise_transports.py`), all three transports at 100% coverage.
- **M8.2 — Slack & email transports.** Two concrete transports implement the M8.1
  `send(*, target, subject, body, config)` seam, so both are drop-in for `notify()`.
  New `notify/transports/slack.py` `SlackTransport` POSTs the rendered message as a
  Slack payload (`{"text": "*subject*\nbody", …}`, with optional `channel`/`username`
  overrides pulled from channel config) to the webhook resolved from the channel target
  → `config["webhook_url"]` → `SLACK_WEBHOOK_URL`. New `notify/transports/email.py`
  `EmailTransport` builds a MIME `EmailMessage` and sends it through an SMTP client with
  the correct to/subject/body/from (recipient from the channel target → `config["to"]`;
  sender from `config["from"]` → `SMTP_FROM`). Both take an **injectable** client (an
  HTTP client for Slack, an SMTP client for email) so no test touches the network, and
  both **capture** delivery failures — network error, non-2xx webhook response, SMTP
  outage, or missing config (no webhook / no recipient) — as `{"ok": false, "error": …}`
  rather than raising, so a broken notification never breaks the policy run that
  triggered it. New config defaults (`config.py` + `.env.example`): `SLACK_WEBHOOK_URL`,
  `SMTP_HOST` / `SMTP_PORT` / `SMTP_FROM` / `SMTP_USERNAME` / `SMTP_PASSWORD` /
  `SMTP_USE_TLS`. 13 TDD tests (`test_notify_transports.py`), transports at 100% coverage.
- **M8.1 — Notification service & templates.** Opens the notifications track — a
  service that renders a **communication template** from policy-violation context and
  dispatches it through a **pluggable transport** (Stacklet / c7n-mailer heritage). Two
  new tables (`storage/schema.py`), `notification_templates` (name / subject / body /
  format) and `notification_channels` (name / transport / target / config / enabled),
  with repository CRUD (`create_/get_/list_/delete_notification_template`,
  `create_/get_/list_/update_/delete_notification_channel`). New `notify/service.py`:
  `render()` renders template source in a Jinja2 **`SandboxedEnvironment`** — the classic
  `__class__ → __mro__ → __subclasses__` escape raises `SecurityError` and the
  `attr()`-filter bypass is closed (`jinja2==3.1.6`, CVE-2025-27516), while a **missing
  variable renders empty**, never a crash; `notify(session, template_id, channel_id,
  context, transport)` loads the template + channel, renders subject/body and hands the
  rendered payload to an **injected** `Transport` (a disabled channel renders but never
  dispatches); `WebhookTransport` is a concrete transport whose HTTP client is itself
  injectable (zero network in tests); `build_violation_context(...)` assembles the standard
  context (policy name, matched resource ids, a `count`). `jinja2==3.1.6` pinned in
  `requirements.txt` (latest; clears the sandbox-escape CVEs). New
  `backend/tests/test_notify_service.py` (15 tests, TDD) covers render-with-context, the
  sandbox blocks (escape chain + `attr()` bypass), missing-variable-safe, the context
  builder, `WebhookTransport` (injected client), dispatch-via-transport, disabled-channel,
  unknown template/channel, and channel + template CRUD. New/changed code at **100%**
  coverage.
- **M7.4 — Unified remediation audit & UI.** Every remediation attempt — a FinOps
  recommendation **or** a policy-driven action, dry-run or live — is recorded as a
  single `remediation_actions` row. Two new columns on `RemediationAction`
  (`storage/schema.py`) capture provenance: **`source`** (`recommendation` / `policy`
  / `binding`; defaults to `recommendation`, so the existing recommendation path is
  unchanged) and the originating **`policy_id`**. `remediation/approval.queue_policy_action`
  resolves both from the match → execution (tagging `binding` when the run was
  binding-triggered) and `_result` surfaces them. `repository.list_remediation_actions`
  now selects `source` + `policy_id`, **filters by `source`** (bound param,
  injection-safe), and `COALESCE`s the resource id from the action `params` so a policy
  action shows its target without a recommendation join; `GET /api/remediation` gains a
  `?source=` query param. The **Remediation** page (`frontend/app/remediation/page.tsx`)
  adds a **Source** column and a source filter select wired to `?source=`. New
  `backend/tests/test_remediation_audit.py` (9 tests, TDD) covers the audit write
  (policy / binding / recommendation-default source), dry-run auditing, the list
  (source + policy_id + resource-from-params), source filtering, and the empty state;
  the CI e2e job asserts `/api/remediation` surfaces `source` and filters by it.
  New/changed code at **100%** coverage.
- **M7.3 — Guardrails for policy actions.** Enforces every policy-driven action
  **block-by-default** through `remediation/guardrails.check(resource_id, tags,
  settings, action=…, allowed_actions=…)`, which now evaluates three guardrails and
  reports **all** failing reasons: the **resource-group allow-list**
  (`ALLOWED_RESOURCE_GROUPS`, `*` = any, empty = none), an **exclude tag** (the
  configurable `EXCLUDE_TAG` **plus** the built-in `custodian:exclude`, so an
  excluded resource is never actioned), and a new **per-binding action-type
  allow-list** (falls back to the global `ALLOWED_ACTIONS` setting; empty = no
  restriction). New `guardrails.default_dry_run(settings)` forces a safe **dry-run**
  whenever guardrails are unset (remediation disabled or no RG allow-listed); the
  M7.2 approval flow now calls the guardrail with the attempted `action` type and
  uses `default_dry_run`, so a disallowed action comes back `blocked` and never
  reaches Azure. New config: `ALLOWED_ACTIONS` (+ `allowed_actions_list`). New
  `backend/tests/test_policy_action_guardrails.py` (18 tests, TDD) covers the RG
  allow-list, both exclude tags, the action allow-list (per-binding + settings
  fallback, case-insensitive, empty = permit-any), the dry-run default, config
  parsing, and DB-backed enforcement (an out-of-allow-list action is hard-blocked in
  the approval flow); the CI e2e job asserts the guardrail blocks over HTTP.
  New/changed code at **100%** coverage.
- **M7.2 — Approval workflow for policy actions.** Gates policy-driven enforcement
  behind **human approval** — a matched resource's action is queued **pending** and
  never touches Azure until approved. `remediation/approval.queue_policy_action(...)`
  records a `RemediationAction` linked to its originating **`PolicyMatch`** (new
  nullable **`policy_match_id`** FK in `storage/schema.py`) in the `pending` state;
  `approve_action` runs it through the M7.1 executor **behind the existing guardrails**
  (exclude-tag + allow-list) and the `REMEDIATION_ENABLED` kill-switch (so an approval
  can still return `blocked` or a dry-run preview), and `reject_action` sets `rejected`
  and never executes. The state machine is strict — only a `pending` action can be
  decided; deciding an **unknown** action raises `NotFound` → **404**, an
  **already-decided** one raises the new `AlreadyDecided` → **409**. Three endpoints
  expose it: `POST /api/policy-matches/{id}/actions` (queue), `POST
  /api/remediation/{id}/approve`, `POST /api/remediation/{id}/reject`;
  `list_remediation_actions` now surfaces `policy_match_id`. New
  `backend/tests/test_policy_action_approval.py` (22 tests, TDD) covers the full state
  machine (pending/approve/reject/blocked/dry-run), the unknown/already-decided edges,
  and the live executor branch; the CI e2e job drives queue → approve → executed and
  reject → rejected through the running stack. New/changed code at **100%** coverage.
- **M7.1 — Custodian action executor.** Opens the remediation track: the actions
  declared on a Cloud Custodian policy (`tag`, `mark-for-op`, `stop`, `delete`) now
  execute against a matched resource through **injectable** Azure SDK clients.
  `remediation/executor.execute_action(action, resource, *, settings, clients=None,
  credential=None, dry_run=True)` maps each action — `tag`/`mark-for-op` → the
  resource **Tags API** (`create_or_update_at_scope`, Merge) with the resource id +
  payload; `stop` → `virtual_machines.begin_deallocate`; `delete` →
  `virtual_machines`/`disks.begin_delete` — and honours **dry-run** (a preview with
  **zero** Azure calls). Live execution builds its clients from the **write-scoped**
  credential (`write_credential`); unit tests inject spies via the new
  `ActionClients` seam, so no test ever touches Azure. Unknown action types — or
  actions that don't apply to the resource kind (e.g. `stop` on a storage account) —
  return a **structured `{"executed": false, "error": ...}`** dict, never a crash.
  `custodian/engine.resolve_actions(spec)` surfaces a policy's actions, each
  normalized to a `{"type": ...}` dict (string shorthand or mapping). New
  `backend/tests/test_custodian_actions.py` (15 tests, TDD) covers every action's
  happy path, dry-run-no-calls, and the negative/edge cases; the CI e2e job dry-runs
  an action through the deployed backend image. New/changed code is at **100%**
  line coverage.
- **M6.4 — Event config & status UI.** Closes out real-time enforcement with a master
  switch and a live feed. New **`EVENT_MODE_ENABLED`** config (default `true`; `.env.example`)
  gates the whole webhook — when off, `POST /api/events/azure` accepts deliveries with **202**
  but stores/triggers nothing (pause enforcement without tearing down the Event Grid
  subscription). New **`GET /api/events/recent`** status feed returns recent deliveries
  newest-first, paginated (`limit`/`offset`), each with the **executions it triggered**: the
  reactive `PolicyExecution`s (M6.2) now stamp a new **`event_id`** column, so
  `repository.recent_events` joins event → runs (one grouped lookup, no N+1). New Next.js
  **`/events`** page (+ Nav link + `lib/api.ts` `RecentEvent`/`fetchRecentEvents`) renders the
  feed with a status badge per triggered run; the CI e2e job asserts the `/events` route. An
  empty feed is `[]`, not an error. (Reuses the existing `event_log` table rather than a
  separate `ingested_events`.)
- **M6.3 — Real-time AssetDB updates from events.** Each accepted Event Grid delivery now
  also **streams into the inventory** so the AssetDB (M4.1) reflects *who / how / when*
  near-instantly. New `events/assetdb.py` `apply_asset_event` **upserts** the `assets` row on
  `resource_id` (refreshing `last_seen` + identity; a `ResourceDeleteSuccess` marks
  `state='deleted'`) and **appends an `asset_event`** carrying the event's actor / operation /
  status / timestamp — the same audit trail the M4.4 history timeline renders. New
  `repository.upsert_asset_from_event` (reusing the `xmax = 0` inserted-detection from
  `upsert_assets`) updates **only** the columns an event knows, so a prior full ingestion's
  `config` / `tags` / `name` / `location` are **preserved, never clobbered**; the lifecycle is
  `created` on first sight, else `updated` (or `deleted`). An event with **no `resource_id`**
  is ignored (no write). Wired into `POST /api/events/azure` alongside (but separate from) the
  M6.2 policy trigger — one keeps inventory current, the other enforces governance. Fully
  DB-fixture tested (`test_event_assetdb.py`).
- **M6.2 — Event-mode policy trigger.** Turns Event Grid ingestion (M6.1) into actual
  **reactive enforcement**: each accepted delivery is handed to the new
  `custodian/eventmode.py` `handle_event`, which selects the policies that both declare an
  **event-grid `mode`** in their c7n spec **and** target the event's **resource type**
  (matching the event's ARM type — e.g. `microsoft.compute/virtualmachines` — against the
  policy's c7n type — e.g. `azure.vm` — or an ARM type authored directly), then runs exactly
  those against the event's subscription via the same injectable `CustodianRunner` seam as
  pull mode. Each reactive run is recorded as a `PolicyExecution` with a new **`mode`** column
  set to **`event`** (`pull` for scheduled/binding runs); `create_policy_execution` and the
  execution serializer carry it through. Wired into `POST /api/events/azure` (via the
  `get_custodian_runner` dependency, so the API suite stays offline). Deliberately
  conservative: an event with **no matching policy**, an **unknown/type-less** resource, or
  only **pull-mode / disabled** policies is a **safe no-op** — never an error, so the webhook
  always drains and Event Grid never retries; a single failing policy is isolated (recorded
  `failed`) without sinking the others. Fully unit-tested with an injected `FakeCustodianRunner`.
- **M6.1 — Azure Event Grid ingestion endpoint.** The ingress point for **real-time
  enforcement** (Cloud Custodian `mode: event`). New **`POST /api/events/azure`** webhook
  completes Event Grid's one-time `SubscriptionValidation` handshake (echoes
  `validationCode`), **authenticates** each delivery against an optional shared key
  (`AZURE_EVENTGRID_SHARED_KEY`; `x-events-key` header or `?key=` param — empty accepts all;
  mismatch → `403`), and **normalizes** each `Microsoft.Resources.Resource{Write,Action,
  Delete}Success` `EventGridEvent` into a `NormalizedEvent` (`events/models.py`) persisted to
  the new **`event_log`** table (auto-created by `init_db()`). Idempotent on `event_id`
  (`ON CONFLICT DO NOTHING`) so Event Grid's at-least-once **re-delivery never duplicates**;
  unrecognized event types are skipped; a non-JSON body → `400`. New `GET /api/events`
  (newest-first). New `events/ingestion.py` (`verify_event_grid_key`,
  `handle_subscription_validation`, `normalize_event`), `azure_eventgrid_shared_key` config +
  `AZURE_EVENTGRID_SHARED_KEY` in `.env.example`, and literal fixtures under
  `fixtures/events/`. Fully fixture-driven — no live Event Grid needed to test.
- **M5.4 — Bindings & account-groups UI.** A Next.js **`/bindings`** console (the
  Stacklet binding-management UX): lists every binding with its **collection**, **account
  group**, **schedule**, **mode**, dry-run/enabled state and **last-run status** (derived
  from the `binding_id`-tagged executions in the M3.3 history). A create form selects an
  **existing** collection + account group (submit disabled until both chosen); each row is
  **editable inline** (schedule / mode / dry-run / enabled → `PUT`); a **Run** button calls
  `POST /api/bindings/{id}/run` and refreshes status. Empty/error states handled. Adds
  `frontend/app/bindings/page.tsx`, a **Bindings** nav link, and `Binding` /
  `BindingRunResult` types (+ `binding_id` on `PolicyExecution`) in `lib/api.ts`. The CI
  `e2e` job now also asserts `/bindings` returns `200` in mock mode. No backend change.
- **M5.3 — Binding execution engine.** `run_binding(binding_id)`
  (`custodian/bindings.py`) runs governance at scale: it executes **every policy in a
  binding's collection** across **every enabled subscription in its account group**,
  recording one `PolicyExecution` — **tagged with the originating `binding_id`** (new
  `PolicyExecution.binding_id` FK, `ON DELETE SET NULL` to preserve the audit trail) —
  per policy × subscription, reusing the M3.2 pull-mode executor and `SubscriptionContext`
  via the injectable `CustodianRunner` seam. A **disabled** binding is a no-op
  (`status="skipped"`); the binding's **`dry_run`** is passed through to every run (no
  actions executed when set); a per-(policy × subscription) failure is isolated on its own
  row. New endpoint **`POST /api/bindings/{id}/run`**, and the scheduler registers **one
  cron job per enabled binding** (from its `schedule`; invalid crons are skipped, not
  fatal). Execution rows now surface `binding_id` in the M3.3 history API.
- **M5.2 — Bindings model.** A **binding** links a **policy collection** (M2.3) to an
  **account group** (M5.1) with execution config — Stacklet's core operational unit that
  operationalizes governance at scale (*which policies run against which accounts, how,
  when*). New `bindings` table (FKs to `policy_collections`/`account_groups`, both
  `ON DELETE CASCADE`), auto-created by `init_db()`, storing `schedule` (cron), `mode`
  (`pull`|`event`), `dry_run` and `enabled`. Repository CRUD + validation and endpoints
  `GET/POST/PUT/DELETE /api/bindings[/{id}]`: creating a binding requires an **existing**
  collection and account group (else `404`), `mode` is validated to `pull`/`event` (else
  `400`), and bindings **default to `dry_run=true`** / `enabled=true`; deleting an unknown
  binding is `404`. New `BindingIn` / `BindingUpdate` models.
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
  use. Wired into a new `python -m cloudwarden.cli run-policies [--mock]` command
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
