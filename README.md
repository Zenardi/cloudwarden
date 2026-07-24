# CloudWarden

**Multi-cloud governance-as-code & FinOps.** Guard your policy posture and govern
your cloud spend across Azure, AWS, and GCP from one control plane.

[![CI](https://github.com/Zenardi/cloudwarden/actions/workflows/ci.yml/badge.svg)](https://github.com/Zenardi/cloudwarden/actions/workflows/ci.yml)

**Overview Page**
![overview](./docs/images/overview.png)

**Grafana - Cost Overview Dashboard**
![overview](./docs/images/grafana-cost-overview.png)

**Grafana - Recommendation & Savings Dashboards**
![overview](./docs/images/grafana-recommendation.png)


**Multi-cloud governance-as-code *and* FinOps — Azure, AWS and GCP in one
self-hostable stack.** Two pillars over a shared *collect → store → surface*
backbone:

- **Governance-as-code** (à la [Stacklet](https://stacklet.io/)), built on
  **[Cloud Custodian](https://cloudcustodian.io/)** (`c7n`, with `c7n-azure` /
  `c7n-gcp` provider plugins): onboard **Azure subscriptions, AWS accounts and
  GCP projects**; author, validate, **version** and **GitOps-sync** policies,
  group them into **collections**, **evaluate them on a schedule across every
  account** (pull mode) or **react to change events in real time**, **shift-left**
  the same policies against a **Terraform plan in CI** (fail the PR before anything
  is provisioned, M14.6), detect **configuration drift** against a per-resource
  desired-state baseline (M14.7), **propose UI-authored policy edits back to git as a
  pull request** (policy-as-PR write-back — never touching the default branch, M14.8),
  grant **scoped, approved, expiring waivers** that record a match as *waived* instead of
  enforcing it — re-surfacing the finding the moment they expire (M14.9), translate a policy
  into a provider's **native preventive guardrail** — Azure Policy / AWS SCP / GCP Org Policy
  — to **block a non-compliant resource at creation** (what-if preview + dry-run-first apply,
  M14.10), and review the full **execution history**. A
  cross-cloud **AssetDB** tracks every resource
  (config, relationships, change history), with **posture and execution-health
  that filter and group by cloud provider** — one pane over every cloud.
- **FinOps** cost & utilization optimization: visualize spend by
  **resource / type / region**, generate **AI-assisted right-sizing and shutdown
  recommendations** from CPU / RAM / I/O metrics, and — once approved — **execute
  guarded remediation**.

Governance and AssetDB span **Azure, AWS and GCP**; the FinOps cost/right-sizing
pipeline is **Azure-first** today (AWS/GCP cost analytics are on the roadmap).
Data comes from each cloud's native APIs (Azure: **Cost Management**, **Monitor**,
**Resource Graph**, **Advisor**), is persisted to **Postgres/TimescaleDB**, and is
surfaced on **Grafana** and a **Next.js** UI — with a **pluggable AI** layer
(Anthropic by default; any OpenAI-compatible/local model). It runs fully offline
with recorded fixtures (`FINOPS_MOCK=1`), so **no cloud credentials are required**
to see it work.

> 📖 New here? The full **[operational manual](docs/README.md)** covers setup,
> every screen, multi-cloud onboarding, policies, dashboards and the API.

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
| M4.2 | AssetDB — filterable, injection-safe asset query API | ✅ done |
| M4.3 | AssetDB — asset relationships graph (disk→vm, nic→vm, ip→nic) | ✅ done |
| M4.4 | AssetDB — asset change history & event metadata (Activity Log) | ✅ done |
| M4.5 | AssetDB — asset explorer & detail UI (query, config, graph, history) | ✅ done |
| M5.1 | Account groups — organize subscriptions into named, many-to-many groups | ✅ done |
| M5.2 | Bindings — link a policy collection to an account group with exec config | ✅ done |
| M5.3 | Binding execution engine — run a binding across its accounts, by cron | ✅ done |
| M5.4 | Bindings & account-groups UI — create/edit/run bindings, last-run status | ✅ done |
| M6.1 | Real-time enforcement — Azure Event Grid ingestion endpoint (event mode) | ✅ done |
| M6.2 | Event-mode policy trigger — react to an event by running matching policies | ✅ done |
| M6.3 | Real-time AssetDB updates — events stream create/update/delete into inventory | ✅ done |
| M6.4 | Event config & status UI — EVENT_MODE_ENABLED gate + recent-events feed | ✅ done |
| M7.1 | Custodian action executor — map tag/mark-for-op/stop/delete to Azure SDK | ✅ done |
| M7.2 | Approval workflow — queue policy actions pending; approve/reject before enforce | ✅ done |
| M7.3 | Guardrails for policy actions — RG allow-list, exclude tag, action allow-list, dry-run default | ✅ done |
| M7.4 | Unified remediation audit & UI — policy actions in `remediation_actions`, source column + filter | ✅ done |
| M8.1 | Notification service & templates — sandboxed Jinja2 render + pluggable transport | ✅ done |
| M8.2 | Slack & email transports — webhook / SMTP delivery via injected clients, failures captured | ✅ done |
| M8.3 | Teams, Jira & ServiceNow transports — ITSM integrations (webhook / create issue / create incident) | ✅ done |
| M8.4 | Per-binding notify config & UI — attach channel+template to a binding; fire on violation; `/notifications` page | ✅ done |
| M9.1 | Compliance posture dashboard — compliant/non-compliant counts by policy/subscription/collection (API + Grafana) | ✅ done |
| M9.2 | Policy execution health dashboard — success/failure rate, avg duration & last-run per policy/binding (API + Grafana) | ✅ done |
| M9.3 | Resource compliance explorer (Next.js) — drill policy → matched resources → asset detail | ✅ done |
| M9.4 | Governance reporting & export — streaming, paginated CSV/JSON + optional scheduled report | ✅ done |
| M10.1 | Policy packs — installable, versioned bundles of curated policies that materialize into a collection | ✅ done |
| M10.2 | Cost governance pack — FinOps heuristics as c7n policies (idle VMs, orphan disks/IPs, oversized VMs, untagged) | ✅ done |
| M10.3 | Security & tagging pack — public-IP exposure, permissive NSG, required tags, unencrypted disks (Security Baseline) | ✅ done |
| M10.4 | CIS Azure compliance pack — CIS controls mapped to c7n policies; posture grouped by control id (CIS Azure) | ✅ done |
| M11.1 | RBAC model — roles/permissions/role-bindings + a `require_permission` guard on mutating endpoints | ✅ done |
| M11.2 | Teams & membership — team-scoped multi-tenancy: policies carry an owning team; members see/manage only their team's, admins see all | ✅ done |
| M11.3 | SSO / OIDC authentication — verified bearer token (or first-party session) becomes the RBAC principal; login/callback flow; a login-gated UI | ✅ done |
| M11.4 | Audit log — append-only trail of every mutating action (actor, action, target, before/after); `GET /api/audit` + a UI viewer | ✅ done |
| M12.1 | Cloud provider abstraction — a `CloudProvider` interface + registry with Azure behind it; `SubscriptionContext` generalized to `AccountContext`; accounts carry a `provider` (multi-cloud foundation) | ✅ done |
| M12.2 | AWS onboarding & execution — onboard AWS accounts (STS-validated), dry-run c7n aws policies, ingest AWS resources into AssetDB tagged `provider='aws'`; injectable boto clients (no live AWS in tests) | ✅ done |
| M12.3 | GCP onboarding & execution — onboard GCP projects (Resource-Manager-validated), dry-run c7n gcp policies, ingest GCP resources into AssetDB tagged `provider='gcp'`; injectable clients (no live GCP in tests) | ✅ done |
| M12.4 | Cross-cloud AssetDB & dashboards — asset queries, posture and execution-health filter/group by `provider`; UI + Grafana provider filter defaulting to all clouds (the single multi-cloud pane) | ✅ done |

Both tracks run fully offline with recorded fixtures (`FINOPS_MOCK=1`) — no cloud
credentials required to see the pipeline, policies and dashboards working.

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

## FinOps recommendations (idle & right-sizing)

The FinOps pipeline turns each run's inventory, cost and metrics into ranked,
evidence-backed recommendations across five detector families, plus a preventive
**budget** layer:

- **Budgets & threshold alerting** (`analysis/budgets.py`, M14.2) — preventive, not a
  detector: a spend limit over a scope (subscription / account-group / tag / team) and
  period (monthly / quarterly) with ordered threshold rules (e.g. 80/100% of actual).
  Each run evaluates actual spend and fires **one** notification per newly-crossed
  threshold through the existing transports — deduped per period, no alert storms.
  Manage via `GET/POST/PATCH/DELETE /api/budgets` (+ `/status`), the **Budgets** page,
  and a budget-vs-actual Grafana panel; RBAC-guarded (`budget:write`/`budget:read`) and
  audited.
- **Cost anomaly detection** (`analysis/anomaly.py`, M14.3) — watchful, not descriptive:
  each run scores the latest day's spend per scope (subscription / service / resource
  type / resource) against a **robust, weekday-aware baseline** (rolling median + MAD,
  deseasonalized), flags abnormal days with a **severity** and a **contributor
  breakdown** (what drove the spike), and fires **one** notification per new anomaly
  through the existing transports. **Signal-gated** — nothing is flagged on thin history
  or an in-pattern weekly peak. Surfaced at `GET /api/finops/anomalies` (RBAC
  `anomaly:read`), on the **Cost explorer** page, and in a *Cost anomalies* Grafana
  panel; idempotent on `(scope, date)` so it never re-alerts.
- **Cost forecasting** (`analysis/forecast.py`, M14.4) — answers "where will we **land**
  this month?": each run projects spend to **month-end** and **quarter-end** per scope
  (total / subscription / service) with a **transparent** trend + weekday-seasonal model,
  a **prediction interval**, and a **backtested MAPE** so the number carries its own
  credibility. **Degrades gracefully** — thin history yields a clearly-labelled
  low-confidence estimate, never a fabricated one. **Budgets consume it**: a
  forecast-basis threshold fires the **forecasted-to-exceed** alert *before* the period
  closes over budget. Surfaced at `GET /api/costs/forecast` (RBAC `forecast:read`), a
  *Spend forecast* panel on the **Cost explorer** page, and a *Spend forecast* Grafana
  table; idempotent per `(scope, horizon, day)`.
- **Showback / chargeback** (`analysis/allocation.py`, M14.5) — allocates spend by an
  arbitrary tag key (`CostCenter` / `Owner` / `Team` / `env`), maps tag values to
  **teams**, and surfaces an explicit **unallocated** bucket for untagged spend. Cost
  rows carry inventory tags (a new `cost_snapshots.tags` dimension); grouping is
  **injection-safe** (the tag key is a bound `tags ->> :key` lookup). `allocated +
  unallocated` always reconciles to the total, and a **team-scoped** principal sees only
  its own allocation. Shared costs split **even**/**proportional**. Surfaced at
  `GET /api/costs/by-tag` · `/showback` · streaming `/showback/export` (CSV/JSON, RBAC
  `showback:read`), a **Showback** page, and a *cost by owner* Grafana panel.
- **Commitment coverage** (`analysis/commitments.py`, M14.1) — Reservation /
  Savings-Plan optimization, the largest untapped lever. Flags **under-utilized**
  commitments (advisory waste = the idle share of committed capacity) and
  **expiring** ones, and sizes **purchase candidates** at the **min-of-window**
  steady-state baseline (bursty usage yields none), priced across term (P1Y/P3Y) ×
  payment options with break-even. Coverage % and blended utilization roll up per SKU
  family/region; all savings are conservative, caveated **estimates**. Azure-first
  behind the `CloudProvider` abstraction. Read via `GET /api/finops/commitments`
  (RBAC-guarded) and the *Commitment coverage* panel on the Recommendations page.
- **Utilization rules** (`analysis/rules.py`) — from CPU / RAM / I-O rollups:
  **shutdown** (deallocate) a consistently-idle VM, **downsize** an over-provisioned
  one (memory-aware when Log Analytics is wired in), or **investigate** when metric
  coverage is too thin to conclude. Azure **Advisor** agreement raises confidence.
- **Shape-based idle** (`analysis/idle.py`) — orphaned/waste resources keyed off
  inventory config: unattached & deallocated-VM-reserved **managed disks**,
  unassociated **public IPs**, empty **App Service plans**, deallocated **VMs**,
  empty & oversized **DevCenter project pools** (right-sizing a pool's dev-box
  definition), and paid-tier **Cosmos DB for MongoDB (vCore) clusters**.
- **Activity-based idle** (`azure/activity_metrics.py`) — always-on Azure Monitor
  platform metrics surface resources that bill but nobody uses (Bastion sessions,
  storage transactions, ACR pulls) with **no diagnostic-log dependency**, and only
  when data was actually observed (never flagged on absence of signal).
- **ML compute** (`azure/ml_compute.py`) — ML compute instances/clusters live
  *under* a workspace and are absent from Resource Graph, so a dedicated collector
  enumerates them per workspace; the detector flags a **running** or **failed**
  Compute Instance and an AmlCompute **cluster pinned to a non-zero minimum node
  count**.

**Environment-weighted savings** — a subscription's *kind* (Development / QA / Prod /
Sandbox) discounts idle/waste savings by how safely they can be reclaimed (a sandbox
resource is delete-on-sight; production idle needs review first), so the
potential-savings total reflects risk, not just raw waste. Every recommendation
carries a category, confidence, rationale, caveats and evidence — and the advisory
ones (stopped VMs, Mongo, ML compute) deliberately report **0 estimated savings**
rather than overstate a figure the cost data can't isolate (ML compute spend, for
instance, rolls up to the owning workspace). Recommendations are reconciled and
summarized by the pluggable AI layer, persisted to Postgres, and surfaced in the UI
and Grafana.

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

Policies can also be managed **GitOps-style** (M2.4), with **Git as the single
source of truth**. `custodian/gitops.py::sync_policies` resolves a policy
directory — point `GITOPS_REPO_URL` (+ `GITOPS_BRANCH` / `GITOPS_POLICY_PATH`) at a
Git repo of Custodian policy YAML and it **clones/pulls** it; with no repo URL it
falls back to a **local directory** (`GITOPS_LOCAL_PATH`, defaulting to the bundled
`cloudwarden/policies/`). It then validates every `*.yml|*.yaml|*.json`, **upserts
by name** with `source='gitops'`, and — because Git is authoritative — **deletes
any `gitops` policy no longer present** in the source (hand-authored `custom`
policies are never touched). New policies are seeded **disabled** (`enabled=False`)
so nothing acts until an operator turns it on, and a re-sync of an edited definition
**preserves that toggle** (the update path never rewrites `enabled`). Unparseable or
schema-invalid files are **skipped and reported** (non-fatal), the sync is
**idempotent**, and a clone/pull failure returns a structured error rather than a
`500`. It runs **on every boot** (the FastAPI lifespan hook) and on demand via
`POST /api/policies/sync`; the Git client is an injectable seam, so the whole
pipeline is unit-tested offline against a fixture repo.

**Bundled default policies.** The stack ships **10 disabled-by-default
FinOps/governance policies** as the local GitOps source — `cloudwarden/policies/`:
`cost.yml` (7: unattached disks, orphaned public IPs, stopped/deallocated VMs, empty
App Service plans, stale snapshots, idle load balancers, orphaned NICs) and
`governance.yml` (3: VMs missing an owner tag, public blob storage, Cosmos DB with
public network access). Each uses portable `value` filters plus a **non-destructive
`tag` action** (flag, don't delete); the operator enables the ones they want from the
Policies page. Because Git is the source of truth, the intended workflow is to move
these into your own repo and manage them there.

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
via `python -m cloudwarden.cli run-policies [--mock]` and as a second APScheduler
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

**Compliance posture (M9.1).** The governance console's headline view. The
`v_governance_posture` SQL view takes the **latest execution per (policy,
subscription)** and classifies that pair as **compliant** (matched nothing) or
**non-compliant** (matched ≥1 resource). `GET /api/governance/posture` rolls those
pairs up three ways — `by_policy`, `by_subscription`, `by_collection` — plus a
`totals` block (`compliant` / `non_compliant` / `violations` / `evaluated`); with
nothing executed yet the totals are zeroed and the group lists empty — the empty
state is data, never an error. A provisioned **Compliance Posture** Grafana
dashboard visualises the split, the compliance rate, violations over time, and
per-policy / per-subscription posture tables.

**Execution health (M9.2).** The governance *engine's own* health — so operators
can see whether policy runs are succeeding, and how long they take. The
`v_execution_health` / `v_execution_health_by_binding` SQL views aggregate every
execution into succeeded/failed counts, a rounded `success_rate`, the average
wall-clock `avg_duration_seconds` (over finished runs), and the `last_status` /
`last_execution_at`. `GET /api/governance/execution-health` returns
`{by_policy, by_binding}` (pull-mode runs with no binding are still counted
per-policy but excluded from the per-binding grain); both lists are empty until a
policy has executed — never an error. A provisioned **Policy Execution Health**
Grafana dashboard trends success rate, duration and failures per policy / binding.

**Compliance explorer (M9.3).** A Next.js drill-down at **`/compliance`** for
investigating non-compliance (à la Stacklet's compliance explorer). It lists
policies with their non-compliant resource counts (from the M9.1 posture rollup);
clicking a policy calls `GET /api/governance/policies/{id}/matches` — the resources
flagged by each subscription's latest execution (the current non-compliant set,
its size matching the posture `violations`) — and each matched resource links
through to its **M4.5 AssetDB detail** (`/assets/<resource_id>`). Empty (compliant)
and error states are handled inline. `404` for an unknown policy, `[]` for a policy
with no matches — never an error.

**Reporting & export (M9.4).** Stakeholders get periodic evidence via
`GET /api/governance/export?format=csv|json` — one row per policy execution (policy,
subscription, status, matches, timing), streamed from a **paginated cursor**
(`repo.iter_governance_export`, `LIMIT`/`OFFSET` in batches) so an arbitrarily large
history never loads into memory. CSV carries a header row; JSON is an array of the
same records; any other `format` → `400`. The `reporting` module's serializer also
backs an **optional scheduled report**: with `GOVERNANCE_REPORT_ENABLED=true` the
scheduler writes a timestamped CSV to `APP_DATA_DIR` every
`GOVERNANCE_REPORT_INTERVAL_SECONDS` (off by default — the on-demand export needs no
flag).

**Policy packs (M10.1).** Curated Cloud Custodian policies ship as installable,
versioned **packs** — YAML under `cloudwarden/packs/` (à la Stacklet's out-of-the-box
packs), either a single `<name>.yaml` file or a `<slug>/` directory with a `pack.yaml`
manifest. `GET /api/packs` lists what's available (name, version, policy count);
`POST /api/packs/{name}/install` **validates every policy through the engine, then
materializes** the (upsert-by-name, `source='pack'`) policies plus a collection
(named by the pack's optional `collection`, else its `name`), recording the installed
version in `installed_packs`. Install is **atomic on validation** — a pack with any
invalid policy is reported (`422`) and writes nothing — and **idempotent**:
re-installing the same version reuses the collection and creates no duplicates.
`POST /api/packs/{name}/enabled` toggles a pack's **binding eligibility** by cascading
its `enabled` flag to the member policies (a disabled pack stops resolving into binding
runs). Single-file packs today: `cost-hygiene` (unattached disks, unassociated public
IPs) and `tag-compliance` (Environment / CostCenter tag baselines).

**Cost governance pack (M10.2).** The FinOps heuristics the app already computes,
now expressed as c7n policies — a **directory pack** at `cloudwarden/packs/cost/`
(`pack.yaml` manifest + one `*.yml` per policy) that installs into a **Cost Governance**
collection. Five policies: deallocated/stopped VMs (`cost-idle-vm-deallocated`),
unattached disks (`cost-unattached-disk`), unassociated public IPs
(`cost-idle-public-ip`), oversized (≥ 8 vCPU) VMs (`cost-oversized-vm`), and VMs
missing a CostCenter tag (`cost-untagged-cost-centre`). Every policy is schema-valid
via the engine, and `custodian.engine.match_resources(spec, resources)` runs c7n's
filter machinery **offline** so a policy can be dry-run against recorded/inventory
data — e.g. the idle-VM policy matches the deallocated/stopped fixture VMs but not a
running one, and the unattached-disk policy matches an `Unattached` disk but not an
attached one.

**Security & tagging pack (M10.3).** A security-hygiene **directory pack** at
`cloudwarden/packs/security/` that installs into a **Security Baseline** collection.
Four policies: internet-exposed public IPs (`security-public-ip-exposure`), permissive
inbound NSG rules — Allow from `0.0.0.0/0` to SSH/RDP via c7n-azure's `ingress` filter
(`security-nsg-permissive-inbound`), resources missing a mandated `Environment`/`Owner`
tag (`security-required-tags`), and disks not encrypted with a customer-managed key
(`security-unencrypted-disk`). Each policy also declares a **remediation action** (a
marker `tag`) that runs **dry-run only** under a binding (bindings default
`dry_run=true`). Every policy is schema-valid via the engine, and the required-tags
policy matches a resource missing a mandated tag (offline `match_resources`).

**CIS Azure compliance pack (M10.4).** A starter subset of the CIS Microsoft Azure
Foundations Benchmark mapped to c7n policies — a **directory pack** at
`cloudwarden/packs/cis-azure/` that installs into a **CIS Azure** collection. Each
policy carries its CIS control id in `metadata.control_id`, and compliance posture
(`GET /api/governance/posture`) gains a **`by_control`** rollup that groups
compliant/non-compliant counts by control id (extracted from each policy's stored
spec), so posture is framed against the framework. Policies without a control id are
excluded from `by_control`. The mapping:

| CIS control | Policy | Resource | Check |
|---|---|---|---|
| 3.1 | `cis-3-1-storage-secure-transfer` | `azure.storage` | Secure transfer (HTTPS-only) required |
| 3.8 | `cis-3-8-storage-default-deny` | `azure.storage` | Default network access rule = Deny |
| 6.1 | `cis-6-1-nsg-restrict-rdp` | `azure.networksecuritygroup` | RDP (3389) not open to `0.0.0.0/0` |
| 6.2 | `cis-6-2-nsg-restrict-ssh` | `azure.networksecuritygroup` | SSH (22) not open to `0.0.0.0/0` |
| 7.3 | `cis-7-3-disk-cmk-encryption` | `azure.disk` | Disks encrypted with a customer-managed key |

**RBAC (M11.1).** Role-based access control guards mutating endpoints. Three tables —
`roles`, `permissions` (action grants per role), `role_bindings` (principal → role) —
back three seeded roles: **admin** (`*`, all actions), **editor** (all write/run
actions except RBAC administration), and **viewer** (read-only). A
`require_permission("policy:write")`-style FastAPI dependency reads the caller from the
`X-Principal` header, resolves the union of its bound roles' permissions, and enforces
the route's action — **401** with no principal, **403** without the permission; reads
stay ungated. Enforcement is gated by **`RBAC_ENABLED`** (off by default, so the
existing unauthenticated API is unchanged); `RBAC_BOOTSTRAP_ADMIN` names a principal
auto-bound to `admin` at seed time so a fresh deployment can provision every other
binding. Manage it via `GET /api/authz/me` (your permissions), `GET /api/authz/roles`,
and `GET`/`POST`/`DELETE /api/authz/role-bindings` (writes require `rbac:admin`).
Identity is a plain header today; an SSO subject replaces it in M11.3.

**Teams & multi-tenancy (M11.2).** Governance resources are scoped to an owning
**team** (Stacklet-style tenancy). Two tables — `teams` and `team_members`
(principal → team) — back a nullable `team_id` on `policies` (`ON DELETE SET NULL`,
so deleting a team leaves its policies global rather than orphaned). When RBAC is
enabled, creating a policy assigns the caller's team as owner (derived from
membership, or an explicit `team` in the body that the caller must belong to);
`GET /api/policies` returns **only the caller's team's policies** for a member and
**all** for an admin (RBAC wildcard); and a non-admin reaching a policy in another
team — read, update or delete — gets **403**. Removing a member from a team revokes
their access to its resources. Team administration (`POST /api/teams`,
`POST`/`DELETE /api/teams/{id}/members`) requires the admin-only `team:write`
permission; `GET /api/teams` and `GET /api/teams/{id}/members` are readable. Scoping
is gated by the same `RBAC_ENABLED` flag — with RBAC off, listings are unscoped and
the API stays backward-compatible.

**SSO / OIDC authentication (M11.3).** With **`OIDC_ENABLED`**, identity arrives as a
verified token rather than a plain header. The API accepts either an **OIDC bearer
token** (`Authorization: Bearer <jwt>`, verified with PyJWT — RS256 signature +
`exp`/`iss`/`aud` — against a static public key or the issuer's JWKS) or a **first-party
session cookie** (`finops_session`, a short-lived HS256 JWT minted after login). The
verified **subject** becomes the RBAC principal (`authz/rbac.principal_from_request`
delegates to OIDC when enabled), so roles, teams and permissions all key off the SSO
identity; an expired or invalid credential is **401**. The login flow is
`GET /api/auth/login` (returns the IdP authorization URL) → IdP → `GET /api/auth/callback`
(exchanges the code, verifies the token, sets the session cookie) → `POST /api/auth/logout`;
these routes **404 when OIDC is disabled**. Both the token *verifier* and the OIDC
*client* are injectable, so the flow is exercised fully offline (no IdP is contacted in
tests). The Next.js UI gates behind `/login` when `NEXT_PUBLIC_AUTH_ENABLED=true` (off
by default, so mock dev is unauthenticated). Enable OIDC **and** RBAC together to
authenticate callers and enforce their permissions; identity is a plain `X-Principal`
header only while OIDC is off.

**Audit log (M11.4).** Every mutating governance action is recorded in an **append-only**
`audit_log` — Stacklet's audit trail. Creating, updating, deleting or enabling/disabling
a policy writes one row capturing **who** (`actor`, the resolved RBAC/SSO principal, or
`NULL` when anonymous), **what** (`action`, e.g. `policy.update`), **which**
(`target_type`/`target_id`), and the **before/after** state (JSONB — a create has an empty
`before`, a delete an empty `after`). **Reads are never recorded.** The trail is written as
a side effect of the mutation, inside the same transaction, so it commits atomically with
the change. `GET /api/audit` lists entries **newest-first** (with `id` as the tiebreaker so
same-transaction rows still order correctly), filterable by `actor` / `action` /
`target_type` / `target_id` and paginated by `limit`/`offset`; the **Audit** page renders
it. The log is tamper-evident by construction: there is deliberately no update or delete
path, in the repository or the API (mutating verbs on `/api/audit` are `405`).

**Cloud provider abstraction (M12.1).** The engine, orchestrator and onboarding talk to
a **`CloudProvider`** seam (`cloudwarden.providers.base`) instead of Azure directly — the
foundation for extending governance to AWS/GCP through Cloud Custodian. A name-keyed
**registry** resolves providers: `providers.registry.get("azure")` returns the Azure
implementation (`providers.azure.AzureProvider`), which owns c7n resource registration,
the resource registry, and session construction; an unregistered name raises
`UnknownProviderError` rather than silently defaulting. The per-run context is generalized
from the Azure-only `SubscriptionContext` to a provider-neutral **`AccountContext`**
(`provider` + `account_id` + optional credential) — `SubscriptionContext` stays as a
backward-compatible alias (`subscription_id` → `account_id`), so every collector is
unchanged. Accounts carry a **`provider` column** (`server_default='azure'`, so existing
rows read as Azure) exposed via `GET /api/subscriptions`. A pure, behaviour-preserving
refactor: the entire existing suite stays green.

**AWS onboarding & execution (M12.2).** The second cloud behind the M12.1 seam:
`providers.registry.get("aws")` returns an `AwsProvider` that onboards accounts, dry-runs
Cloud Custodian **aws** policies, and ingests AWS resources into AssetDB. **AWS is native
to Cloud Custodian core** — the already-installed `c7n` registers `aws.*` resource types
(there is no separate `c7n-aws` package) and `boto3` ships transitively — so this adds **no
new image dependency and no new Trivy surface**. Onboarding (`POST /api/aws/accounts`)
validates credentials via STS `get_caller_identity` through an **injectable** client seam
(a bad/expired credential or an account mismatch → `400`), then stores the account with
`provider='aws'`. `POST /api/aws/accounts/{id}/ingest` loads AWS resources into AssetDB
tagged **`provider='aws'`** (the `assets`/`resources` tables gained a `provider` column,
`server_default='azure'`, filterable via the asset query API), and
`POST /api/aws/policies/dryrun` returns the fixture resources a policy matches. Everything
is exercised with injected clients / offline fixtures — **no live AWS call** in tests.

**GCP onboarding & execution (M12.3).** The third cloud behind the M12.1 seam:
`providers.registry.get("gcp")` returns a `GcpProvider` that onboards projects, dry-runs
Cloud Custodian **gcp** policies, and ingests GCP resources into AssetDB tagged
`provider='gcp'` (reusing the `provider` column from M12.2 — no schema change). Onboarding
(`POST /api/gcp/projects`) validates credentials via Resource Manager `get_project` through
an **injectable** client seam (a bad/expired credential or a project mismatch → `400`), then
stores the project with `provider='gcp'`; `POST /api/gcp/projects/{id}/ingest` and
`POST /api/gcp/policies/dryrun` mirror the AWS surface. Unlike AWS (native to c7n core), GCP
lives in the separate **`c7n-gcp`** package, which pulls the heavy `google-*` client tree —
so it is an **optional live-only extra** (not installed by default, to keep the image and its
Trivy surface minimal); the live paths lazily import it. Onboarding, dry-runs and ingestion
all work fully offline via injected clients / the `gcp_assets` fixture — **no live GCP call**
in tests.

**Cross-cloud AssetDB & dashboards (M12.4).** The provider dimension is unified into a single
multi-cloud pane. AssetDB queries already filter by the allow-listed **`provider`** column
(`POST /api/assets/query` with a `provider eq aws` filter returns only that cloud's assets).
Compliance **posture** (`GET /api/governance/posture`) and **execution-health**
(`GET /api/governance/execution-health`) each grow a **`by_provider`** rollup and accept an
optional **`?provider=azure|aws|gcp`** filter (omitting it — or `?provider=all` — spans every
cloud). Provider is intrinsic to the account: an execution's provider is its subscription's
`provider` (an un-onboarded subscription defaults to `azure`, mirroring the `server_default`
backfill), joined in via `v_governance_posture` (now carrying `provider`) and the new
`v_execution_health_by_provider` view. The **assets** and **compliance** UIs expose a *Cloud*
dropdown, and both Grafana boards (**Compliance Posture**, **Execution Health**) gain a
`provider` template variable — all defaulting to **all clouds**.

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

**Asset relationships graph (M4.3).** Ingestion also derives the **graph dimension**
of AssetDB — typed, directed edges between assets built from each asset's `config`:
a managed disk's `managedBy` VM (`disk → vm`), a NIC's `virtualMachine` (`nic → vm`),
and a public IP's bound NIC (`ip → nic`). `repo.build_relationships` resolves each
reference against the assets already stored — **case-insensitively**, since Azure
resource ids are — and upserts one `asset_relationships` edge per *resolvable*
reference; a reference to an asset that isn't present (a dangling or external
reference) is **skipped, never fatal**, and the `(source_id, target_id, kind)` triple
is unique so re-deriving over unchanged inventory writes nothing. Neighbours are
served by `GET /api/assets/{id}/relationships`, which returns an asset's edges in
**both directions** (each row tagged `direction` `inbound`/`outbound` and the
`neighbor` id).

**Asset change history (M4.4).** AssetDB also carries an **audit timeline** — the
*who / how / when* of every change — by ingesting the Azure **Activity Log** into
`asset_events`. The mockable `azure/activitylog.py` collector (`client=None` →
recorded fixture; inject a client for live) parses each entry's **actor** (`caller`),
**operation** (`operationName`) and **timestamp** (`eventTimestamp`); a malformed
record (missing any of those) is **skipped, never fatal**. `repo.record_activity_events`
persists each as an `activity` event whose row time is the *real* event timestamp, so
`GET /api/assets/{id}/history` returns the combined lifecycle + activity timeline
**newest-first**; an unknown asset yields an empty list (`200`), not an error.

**Asset explorer UI (M4.5).** The Next.js **`/assets`** console ties the AssetDB
together (the Stacklet AssetDB experience): a query form (type / location / id-contains
/ tag) drives the injection-safe M4.2 query API with **pagination**, and clicking a row
opens **`/assets/<resource-id>`** — a catch-all route (Azure ids contain slashes) that
composes the three APIs into one view: the asset's **config** (JSON), its
**relationships** (M4.3, with links to each neighbour), and its **change-history**
timeline (M4.4). An unknown id shows a friendly **not-found** state, never a crash.

**Account groups (M5.1).** Subscriptions can be organized into named **account
groups** (à la Stacklet account groups) so policies can target logical sets of
accounts. Membership is **many-to-many** (`account_groups` + `account_group_members`,
both FKs `ON DELETE CASCADE`): a subscription may belong to any number of groups and be
removed from each independently, and **deleting a group keeps its subscriptions** —
only the membership rows go. Managed via `GET/POST/DELETE /api/account-groups[/{id}]`
and `POST/DELETE /api/account-groups/{id}/subscriptions/{subscription_id}` (adding an
unknown subscription or group returns `404`), with an **`/account-groups`** UI to create
groups and manage membership. Reuses the existing `subscriptions` records.

**Bindings (M5.2).** A **binding** is Stacklet's core operational unit: it links a
**policy collection** (M2.3) to an **account group** (M5.1) with execution config —
`schedule` (cron), `mode` (`pull`|`event`), `dry_run` and `enabled`. This is what
operationalizes governance at scale: *which policies run against which accounts, how,
and when.* Managed via `GET/POST/PUT/DELETE /api/bindings[/{id}]`. Creating a binding
requires an **existing** collection and account group (else `404`), `mode` is validated
to `pull`/`event` (else `400`), and bindings default to **`dry_run=true`** / `enabled=true`.
The `bindings` table's FKs are `ON DELETE CASCADE`, so deleting a collection or group
drops its bindings automatically.

**Binding execution engine (M5.3).** `run_binding(binding_id)` (`custodian/bindings.py`)
is what runs governance at scale: it executes **every policy in the binding's
collection** across **every enabled subscription in its account group**, recording one
`PolicyExecution` — **tagged with `binding_id`** — per policy × subscription (reusing the
M3.2 pull-mode executor and `SubscriptionContext`). A **disabled** binding is a no-op
(`status="skipped"`); the binding's **`dry_run`** is passed through to every run (no
actions executed when set); a per-(policy × subscription) failure is isolated on its own
row. Trigger it via **`POST /api/bindings/{id}/run`**, and the scheduler registers **one
cron job per enabled binding** (from its `schedule`) so bindings fire automatically —
invalid crons are skipped, not fatal.

**Bindings UI (M5.4).** The Next.js **`/bindings`** console is the binding-management
UX: it lists every binding with its **collection**, **account group**, **schedule**,
**mode**, dry-run/enabled toggles and **last-run status** (derived from the
`binding_id`-tagged executions). A create form selects an **existing** collection +
account group (the button stays disabled until both are chosen); each row is **editable
inline** (schedule / mode / dry-run / enabled → `PUT`); and a **Run** button calls
`POST /api/bindings/{id}/run` and refreshes the row's status. Empty and error states are
handled. Consumes the M5.2/M5.3 + collections/account-groups APIs — no backend change.

## Real-time enforcement (event mode)

**Event Grid ingestion (M6.1).** Cloud Custodian's Azure provider supports `mode: event`
policies that react to **Azure Event Grid** resource-change notifications instead of
waiting for the next poll — the ingress point for real-time governance. **`POST
/api/events/azure`** is that webhook: it completes Event Grid's one-time
`SubscriptionValidation` handshake (echoing `validationCode` in a `validationResponse`),
**authenticates** each delivery against an optional shared key (`AZURE_EVENTGRID_SHARED_KEY`
via the `x-events-key` header or `?key=` param — empty accepts all, for local/mock dev;
a mismatch is `403`), and **normalizes** each `Microsoft.Resources.Resource{Write,Action,
Delete}Success` `EventGridEvent` into an internal `NormalizedEvent` (actor / operation /
resource id / time) persisted to the new `event_log` table. Event Grid's **at-least-once**
delivery means re-delivery is expected, so the write is idempotent on `event_id`
(`ON CONFLICT DO NOTHING`) — no duplicate rows. Unrecognized event types are skipped; a
non-JSON body is `400`. `GET /api/events` returns recent deliveries newest-first. The whole
flow is fixture-driven and unit-tested without a live Event Grid topic.

**Event-mode policy trigger (M6.2).** Ingestion is only the front door — this is what makes
it *enforcement*. Each accepted delivery is handed to `custodian.eventmode.handle_event`,
which selects the policies that both declare an **event-grid `mode`** in their c7n spec
**and** target the **resource type** the event touched (matching the event's ARM type,
e.g. `microsoft.compute/virtualmachines`, against the policy's c7n type, e.g. `azure.vm`,
or an ARM type authored directly), then runs exactly those against the event's subscription
via the same injectable `CustodianRunner` seam as pull mode. Every reactive run is recorded
as a `PolicyExecution` tagged **`mode='event'`** (vs `pull` for scheduled/binding runs), so
the audit trail distinguishes *why* a policy fired. Matching is deliberately conservative:
an event with **no matching policy**, an **unknown/type-less** resource, or only **pull-mode
/ disabled** policies is a **safe no-op** — never an error, so the webhook always drains and
Event Grid never sees a failure to retry. A single failing policy is isolated (recorded
`failed`) without sinking the others or the delivery.

**Real-time AssetDB updates (M6.3).** The same delivery also **streams into the inventory**
(`events.assetdb.apply_asset_event`) so the AssetDB (M4.1) reflects *who / how / when*
near-instantly instead of waiting for the next poll — Stacklet's streaming inventory. Each
resource-change event **upserts** the `assets` row on `resource_id` (refreshing `last_seen`
and identity; a `ResourceDeleteSuccess` marks `state='deleted'`) and **appends an
`asset_event`** carrying the event's **actor**, **operation**, status and timestamp — the
same audit trail the M4.4 history timeline renders. The upsert is deliberately narrow: it
only touches the columns an event actually knows, so a prior full ingestion's `config` /
`tags` / `name` / `location` are **preserved, never clobbered**; the `asset_event` type is
`created` on first sight, else `updated` (or `deleted`). An event with **no `resource_id`**
is ignored (no write). Inventory-streaming and policy-triggering are separate concerns fed by
one delivery — one keeps the AssetDB current, the other enforces governance.

**Event config & status UI (M6.4).** A master switch and a live feed close out real-time
enforcement. `EVENT_MODE_ENABLED` gates the whole webhook: when off, `POST
/api/events/azure` accepts deliveries with **202** but stores/triggers nothing — a clean
way to pause enforcement without tearing down the Event Grid subscription. **`GET
/api/events/recent`** is the status feed: recent deliveries newest-first, paginated
(`limit`/`offset`), each carrying the event-mode **executions it triggered** — the reactive
`PolicyExecution`s now stamp the `event_id` that fired them, so the feed joins event → runs.
The Next.js **`/events`** page renders it: event type / resource / subscription / received
time, and a status badge per triggered run. An empty feed is `[]`, not an error.

**Custodian action executor (M7.1).** Opens the remediation track — Cloud Custodian's
automated enforcement. The actions declared on a policy (`tag`, `mark-for-op`, `stop`,
`delete`) execute against a matched resource through **injectable** Azure SDK clients:
`remediation/executor.execute_action(action, resource, *, settings, clients=None,
dry_run=True)` maps `tag`/`mark-for-op` → the resource **Tags API**
(`create_or_update_at_scope`, `Merge`) with the resource id + payload, `stop` →
`virtual_machines.begin_deallocate`, and `delete` → `virtual_machines`/`disks.begin_delete`.
**Dry-run is honoured** — a preview with **zero** Azure calls — and the live path builds its
clients from the **write-scoped** credential (`write_credential`); tests inject spies via the
new `ActionClients` seam, so no unit test ever touches Azure. Unknown action types, or
actions that don't apply to the resource kind (e.g. `stop` on a storage account), return a
**structured error** dict rather than raising. `custodian/engine.resolve_actions(spec)`
surfaces a policy's actions, each normalized to a `{"type": ...}` dict.

**Approval workflow for policy actions (M7.2).** Enforcement is **gated on human approval** —
a matched resource's action is queued **pending** and never touches Azure until someone
approves it. `remediation/approval.queue_policy_action(session, policy_match_id, action, …)`
records a `RemediationAction` linked to its originating **`PolicyMatch`** (new
`policy_match_id` FK) in the `pending` state; the state machine is strict:

```
pending ──approve──▶ approved ─(guarded exec)─▶ executed / blocked / failed
        └─reject───▶ rejected                    (never executes)
```

`approve_action` runs the action through the **M7.1 executor** — but still behind the
existing guardrails (exclude-tag + resource-group allow-list) and the `REMEDIATION_ENABLED`
kill-switch, so an approval can still come back `blocked` or a dry-run preview. `reject_action`
sets `rejected` and never executes. Only a `pending` action can be decided: deciding an
**unknown** action is a `404`, an **already-decided** one a `409`. Three endpoints expose it:
`POST /api/policy-matches/{id}/actions` (queue, pending), `POST /api/remediation/{id}/approve`,
and `POST /api/remediation/{id}/reject`.

**Guardrails for policy actions (M7.3).** Every policy-driven action is enforced
**block-by-default** through `remediation/guardrails.check(resource_id, tags, settings,
action=…)`. An action is allowed only when **all** guardrails pass:

- **Resource-group allow-list** — the resource's RG must be in `ALLOWED_RESOURCE_GROUPS`
  (`*` = any; empty = none allowed). A non-allow-listed RG is blocked with a reason.
- **Exclude tag** — a resource carrying the configurable `EXCLUDE_TAG` (`finops:exclude`)
  or the built-in `custodian:exclude` tag is **never actioned**.
- **Action allow-list** — the attempted action *type* must be in the binding's allow-list
  (falls back to the global `ALLOWED_ACTIONS`, e.g. `tag,stop`); empty = no per-type
  restriction. An action type outside the list is blocked.
- **Dry-run default** — `guardrails.default_dry_run(settings)` forces a safe **dry-run**
  whenever guardrails are unset (remediation disabled, or no RG allow-listed) so an
  approval previews rather than mutating.

Guardrails hard-block only a *real* (non-dry-run) execution; a dry-run still previews and
annotates the reason. The approval flow (M7.2) calls this on every approve, so a disallowed
action comes back `blocked` and never reaches Azure.

**Unified remediation audit & UI (M7.4).** Every remediation attempt — a FinOps
recommendation *or* a policy-driven action, dry-run or live — is recorded as a single
`remediation_actions` row. Policy actions carry their provenance: a **`source`**
(`recommendation` / `policy` / `binding` — `binding` when the originating execution was
binding-triggered) and the originating **`policy_id`** (resolved from the match → execution).
`GET /api/remediation[?source=…&limit=…]` returns the unified trail — surfacing `source`,
`policy_id` and the target `resource_id` (from the action params when there's no recommendation
to join) — filterable by source. The **Remediation** page (`/remediation`) adds a **Source**
column and a source filter so policy-sourced actions appear alongside recommendation-sourced
ones. Because dry-run previews are audited too (`status: dry_run`), the page is a complete
attempt-by-attempt record.

**Notification service & templates (M8.1).** Opens the notifications track — a service
that renders a **communication template** from policy-violation context and dispatches it
through a **pluggable transport** (Stacklet / c7n-mailer heritage). Templates and channels
persist in `notification_templates` / `notification_channels` (repository CRUD).
`notify/service.render()` renders template source in a Jinja2 **`SandboxedEnvironment`**:
unsafe attribute access can't escape to Python internals — the classic `__class__ →
__mro__ → __subclasses__` payload raises `SecurityError`, and the `attr()`-filter bypass is
closed (`jinja2==3.1.6`, CVE-2025-27516) — while a **missing variable renders empty**, never a
crash. `notify(session, template_id, channel_id, context, transport)` loads the template +
channel, renders subject/body, and hands the rendered payload to the **injected** `Transport`
(a disabled channel renders but never dispatches); `WebhookTransport` is a concrete transport
whose HTTP client is itself injectable, so nothing touches the network in tests.
`build_violation_context(policy_name, resource_ids, …)` assembles the standard context
(policy name, matched resource ids, a `count`).

**Slack & email transports (M8.2).** Two concrete transports implement that same
`send(*, target, subject, body, config)` seam, so both are drop-in for `notify()`.
`notify/transports/SlackTransport` POSTs the rendered message as a Slack payload
(`{"text": "*subject*\nbody", …}`, with optional `channel`/`username` overrides from
channel config) to the webhook resolved from the channel target → `config["webhook_url"]`
→ `SLACK_WEBHOOK_URL`. `EmailTransport` builds a MIME message and sends it through an SMTP
client with the right to/subject/body/from (recipient from the channel target → `config["to"]`;
sender from `config["from"]` → `SMTP_FROM`). Both take an **injectable** client (an HTTP
client for Slack, an SMTP client for email), so no test touches the network, and both
**capture** delivery failures — a network error, a non-2xx webhook response, an SMTP outage,
or missing config (no webhook / no recipient) — as `{"ok": false, "error": …}` rather than
raising: a broken notification must never break the policy run that triggered it.

**Teams, Jira & ServiceNow transports (M8.3).** Three more transports extend delivery
to the ITSM / collaboration systems, all on the same seam and same capture-don't-raise
contract. `TeamsTransport` POSTs a legacy **MessageCard** (`title`/`text`) to a Teams
incoming webhook (channel target → `config["webhook_url"]` → `TEAMS_WEBHOOK_URL`).
`JiraTransport` **creates an issue** via `POST {JIRA_BASE_URL}/rest/api/2/issue` — the
rendered subject → issue `summary`, body → `description`, project from the channel
target → `config["project"]` → `JIRA_PROJECT` — and returns the new issue key.
`ServiceNowTransport` **creates an incident** via `POST
{SERVICENOW_INSTANCE_URL}/api/now/table/incident` — subject → `short_description`, body
→ `description`, optional `urgency`/`impact`/`assignment_group`/… copied from channel
config — and returns the incident number. Each takes an **injectable** HTTP client
(live callers build one carrying HTTP basic auth from `config.py`), so nothing touches
the network in tests, and each captures an auth/permission error (non-2xx), a network
exception, or missing config as `{"ok": false, "error": …}`.

**Per-binding notify config & UI (M8.4).** Wires the machinery to **bindings**. A new
`binding_notifications` table attaches one or more **(channel, template)** pairs to a
binding; when a binding run records a **violation** (a policy match),
`notify/dispatch.dispatch_for_binding()` renders each paired template from the
violation context and dispatches it through the transport selected by the channel's
kind (a small registry: `webhook`/`slack`/`email`/`teams`/`jira`/`servicenow`). A
binding with **no** attachment dispatches nothing, and dispatch is **best-effort** —
hooked into the binding executor after the execution commits and wrapped so a failed
notification never breaks enforcement. The API gains full CRUD for channels
(`/api/notification-channels`) and templates (`/api/notification-templates`) — a bad
transport kind or duplicate name is a `400` — plus attach/detach on a binding
(`/api/bindings/{id}/notifications`). The **`/notifications`** page manages channels and
templates.

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

## Quickstart (mock mode, no cloud needed)

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
- **API docs** → http://localhost:8000/docs (`/api/costs/summary` |
  `/api/costs/by-type` | `/api/costs/by-region` (each accepts `?days=` (1–365,
  default 30) and `?provider=azure|aws|gcp|all` — parameterized, injection-safe),
  `/api/costs/trend` (Δ vs the prior period + a daily Amortized series;
  `?days=` clamped 1–365), `/api/recommendations`,
  `/api/policies` CRUD, `/api/policies/validate`, `/api/custodian/schema`,
  `/api/policies/{id}/dryrun`, `/api/policies/{id}/versions`,
  `/api/policies/sync`, `/api/collections`, `/api/policy-executions`,
  `/api/governance/policy-health`, `/api/assets/query`, …).

Run the backend on a schedule instead of one-shot: the `backend` service also
supports `command: ["scheduler"]`.

## Live mode (real clouds)

**Azure** (the full cost + governance pipeline):

1. Create the read SP and assign **Reader + Cost Management Reader + Monitoring
   Reader** on the subscription (+ **Log Analytics Reader** for memory metrics).
2. In `.env`: set `AZURE_SUBSCRIPTION_ID`, `AZURE_TENANT_ID/CLIENT_ID/CLIENT_SECRET`,
   `FINOPS_MOCK=0`, and an AI key (`ANTHROPIC_API_KEY`) or `AI_BASE_URL` for a
   local model.
3. `make up && make seed`.

For remediation (Phase 5), additionally set the write SP (`AZURE_REMEDIATION_*`),
`REMEDIATION_ENABLED=true`, and `ALLOWED_RESOURCE_GROUPS`. Remediation defaults
to **dry-run**; resources tagged `finops:exclude=true` are never touched.

**AWS & GCP** (governance + AssetDB; cost analytics stay Azure-first). Onboard on
the **Subscriptions** page or via the API — `POST /api/aws/accounts` (STS-validated)
and `POST /api/gcp/projects` (Resource-Manager-validated) — then ingest their
resources with the matching `…/ingest` endpoints. Set `AWS_*` / `GCP_*` in `.env`
for live credentials (both fall back to ambient / default credentials). Full
walkthrough: [docs/06 — Multi-Cloud Onboarding](docs/06-multi-cloud-onboarding.md).

## Multiple accounts (Azure · AWS · GCP)

`AZURE_SUBSCRIPTION_ID` is seeded as the **default** account on first start. Add
more — Azure subscriptions, AWS accounts, or GCP projects — on the **Subscriptions**
page (or the onboarding APIs above); each carries its own `provider` and can reuse
the shared env credentials or bring its **own**. A run with no target (`make seed`,
the scheduler, or `POST /api/runs`) **fans out across every enabled account**, one
pipeline run each; the API also accepts `?subscription_id=…` to run just one.
**Account groups** + **bindings** then let a single collection of policies evaluate
across a multi-cloud group. Per-account secrets are stored in Postgres (v1) — a Key
Vault / column-encryption backing is the intended hardening step.

## Key configuration

| Env | Purpose |
|-----|---------|
| `FINOPS_MOCK` | `1` = use fixtures (offline); `0` = call the real clouds |
| `AI_PROVIDER` / `AI_MODEL` | `anthropic` (default `claude-opus-4-8`) or `openai` |
| `AI_BASE_URL` | OpenAI-compatible endpoint for local models (Ollama/vLLM/LM Studio) |
| `COST_LOOKBACK_DAYS` / `METRIC_LOOKBACK_DAYS` | analysis windows |
| `REMEDIATION_ENABLED` | `false` = dry-run only |
| `LOG_ANALYTICS_WORKSPACE_ID` | enables memory-based downsize rules |
| `GITOPS_REPO_URL` / `GITOPS_BRANCH` / `GITOPS_POLICY_PATH` | GitOps policy sync source (blank URL → local fallback) |
| `GITOPS_LOCAL_PATH` | Local policy dir when no repo URL (blank → bundled `cloudwarden/policies/`) |
| `GITOPS_WRITEBACK_REPO_URL` / `GITOPS_WRITEBACK_BRANCH_PREFIX` / `GITOPS_WRITEBACK_TOKEN` / `GITOPS_PROVIDER` | Policy-as-PR write-back (M14.8): propose UI edits as a PR (blank token disables; never logged) |
| `WAIVER_EXPIRING_WITHIN_DAYS` / `WAIVER_ALERT_CHANNEL` | Waivers (M14.9): warn when an active waiver is within N days of expiry, via the named channel (blank = silent) |

Full list: `.env.example`.

## Project layout

```
backend/cloudwarden/
  config.py auth.py resilience.py models.py orchestrator.py scheduler.py cli.py
  azure/       inventory.py cost.py metrics.py logs.py advisor.py context.py
               activity_metrics.py activitylog.py ml_compute.py connectivity.py
  analysis/    (rollup/rules/idle/pricing/savings — Phase 2)
  ai/          (base/anthropic/openai/factory/prompt — Phase 3)
  remediation/ (executor/guardrails/approval — Phase 5)
  custodian/   engine.py gitops.py (Cloud Custodian c7n + c7n-azure — engine + GitOps sync)
  policies/    cost.yml governance.yml (10 bundled, disabled-by-default GitOps defaults)
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
make trivy     # security gate — Trivy fs + config scan, HIGH/CRITICAL (needs Docker)
make mutation  # mutation testing on core modules (mutmut, advisory)
make perf      # scale/perf load test — binding across >=50 subs x >=20 policies (needs Docker)
make secrets   # secret-scan the tree with gitleaks — fail on any finding (needs Docker)
make sbom      # generate an SBOM (SPDX JSON) for the backend image (needs Docker)
make lock      # regenerate the hash-pinned backend/requirements.lock (needs pip-tools)
make run-mock  # run pipeline locally against a Postgres at localhost:5432
```

**Tests:** **~99% line coverage** (gate at 95%, enforced in CI —
`.github/workflows/ci.yml` via `--cov-fail-under=95`, backed by
`fail_under = 95` in `pyproject.toml`). Live-Azure code paths are covered via
injected fake clients; the DB/API/orchestrator/remediation flows run against a
throwaway PostgreSQL (testcontainers).

### Test effectiveness (mutation testing)

Line coverage proves code *ran*, not that a test would *catch a bug*. A CI
`mutation` job runs [mutmut](https://mutmut.readthedocs.io/) over the core
governance modules — `analysis/`, `custodian/`, `remediation/` — deliberately
mutating them and checking the suite fails (kills the mutant). Config lives in
[`backend/setup.cfg`](backend/setup.cfg) `[mutmut]`; it runs from `backend/` so
mutated paths (`cloudwarden/...`) match the tests' imports, and scopes each
mutant to a fast offline test subset. The job reports a **mutation score** and
compares it to the documented threshold (**≥80 % of tested mutants killed**). It
is **advisory** for now (`continue-on-error: true` — non-blocking) and flips to
blocking once the score stabilises above the threshold. Run it locally with
`make mutation` (needs `mutmut`, installed via `requirements-dev.txt`).

### Scale & performance testing (nightly)

A repeatable **load test** proves policy execution scales: it runs a binding
across **≥50 subscriptions × ≥20 policies** (≥1000 executions) through the real
execution + persistence path (`run_binding`) with an **offline mock runner** (no
c7n/Azure) against a throwaway Postgres, and asserts the run finishes inside a
documented **time budget** (120s — a regression ceiling, not an SLA; a full run
is ~7s locally) and **memory ceiling** (256 MB peak heap). Throughput is recorded
as a JSON artifact so regressions are visible over time. These tests carry a
`perf` marker and are **excluded from the default PR run** (`addopts = -m 'not
perf'`); a dedicated **`perf`** job in `.github/workflows/ci.yml` runs them
**nightly** (cron) and on manual `workflow_dispatch` — **skipped on PRs**
(non-blocking), **blocking on a budget breach nightly**. Run locally with `make
perf`. Details + the budget: [`backend/tests/perf/README.md`](backend/tests/perf/README.md).

### Security scanning (Trivy CVE gate)

CI fails the build on any HIGH/CRITICAL finding via three Trivy scans in the
`security` job (`.github/workflows/ci.yml`): `trivy fs` (dependencies), `trivy
image` (the built backend + frontend images) and `trivy config` (IaC /
Dockerfiles) — all with `--severity HIGH,CRITICAL --exit-code 1` (the vuln scans
add `--ignore-unfixed` so only *fixable* CVEs block the build). Accepted
exceptions live in a reviewed [`.trivyignore`](.trivyignore) (currently empty —
we fix findings rather than suppress them).

**Run the same gate locally before committing** — no Trivy install needed, it
runs the pinned official image over Docker:

```bash
make trivy   # trivy fs + config (HIGH/CRITICAL); the pre-commit gate

# Or scan a built image directly (matches CI's `trivy image` step):
docker build -t cloudwarden-backend ./backend
docker run --rm -v /var/run/docker.sock:/var/run/docker.sock aquasec/trivy:0.72.0 \
  image --scanners vuln --severity HIGH,CRITICAL --ignore-unfixed --exit-code 1 cloudwarden-backend
```

### Supply chain (SBOM, dependency pinning & secret scanning)

Three supply-chain / credential gates catch tampering and leaks pre-merge
(`.github/workflows/ci.yml`):

- **SBOM** — the `supply-chain` job runs [syft](https://github.com/anchore/syft)
  over the built backend image and uploads an SPDX-JSON **Software Bill of
  Materials** (`backend-sbom`) as a build artifact, so every shipped dependency
  is auditable. Reproduce locally with `make sbom`.
- **Hash-pinned dependencies** — [`backend/requirements.lock`](backend/requirements.lock)
  is the fully-resolved, fully-hashed transitive closure of `requirements.txt`
  (generated with `pip-compile --generate-hashes`; regenerate via `make lock`).
  The same `supply-chain` job installs it with **`pip --require-hashes`**, so a
  substituted or tampered wheel (sha256 mismatch) fails the build.
- **Secret scanning** — the `secrets` job runs [gitleaks](https://github.com/gitleaks/gitleaks)
  over the tree and **fails the build on any finding**. The reviewed allowlist
  lives in [`.gitleaks.toml`](.gitleaks.toml) (only the local, git-ignored `.env`
  is excepted, with justification). Run the identical gate locally before
  committing with `make secrets`.

### Container image footprint (Azure SDK pruning)

`c7n-azure` hard-depends on ~56 `azure-mgmt-*` provider SDKs (~480 MB in the venv),
but imports each provider **lazily** — CloudWarden only ever runs a handful
(compute, network, sql, storage, web, cosmosdb, keyvault + resourcegraph / advisor
/ monitor for collection). The backend `Dockerfile` builder therefore **prunes
every `azure/mgmt/<provider>` the platform doesn't use** (M13.6, issue #129),
listed in [`backend/azure_mgmt_keep.txt`](backend/azure_mgmt_keep.txt) — the single
source of truth shared with the guard test. This trims the image from **~1.13 GB →
~859 MB (−271 MB, ~24%)** with zero functional change.

Two guards keep it safe: a **build-time smoke** re-registers all `azure.*`
resources and imports every kept provider (an over-prune **fails the build**), and
[`backend/tests/test_azure_footprint.py`](backend/tests/test_azure_footprint.py)
asserts in CI that the keep-list still covers every SDK our packs/policies need —
so adding a new Azure resource type without keeping its SDK fails the test rather
than shipping a broken image. `requirements.txt` / `requirements.lock` are
unchanged (pruning is a post-install step), so the hash-pinned supply-chain gate
and SBOM stay intact.

## Observability (metrics, tracing & structured logs)

Operable in production out of the box (M13.4) — zero-config, all always-on:

- **Metrics** — `GET /metrics` exposes Prometheus counters for **policy
  executions** (`cloudwarden_policy_executions_total`, labelled by terminal
  status) and **remediation actions** (`cloudwarden_remediation_actions_total`, by
  action type + status), plus a policy-execution duration histogram. Point a
  Prometheus scrape at it.
- **Readiness vs liveness** — `GET /health` is **liveness** (the process is up);
  `GET /ready` is **readiness** — it probes the database (`SELECT 1`) and returns
  **`200` when reachable, `503` when not**, so a Kubernetes/orchestrator readiness
  gate stops routing traffic to a pod whose DB is down.
- **Structured logs** — every log line is JSON carrying a per-request
  **correlation id**. Send `X-Correlation-ID` on a request to thread your own id
  through the logs (it is echoed back on the response); otherwise one is minted.
- **Tracing** — execution runs (`POST /api/runs`) are wrapped in
  **OpenTelemetry** spans. No exporter is configured by default, so spans stay
  in-process (no network egress) until you wire one up.

Implemented in [`backend/cloudwarden/observability.py`](backend/cloudwarden/observability.py)
(deliberately free of `cloudwarden` internal imports, so metrics/tracing/logging
are safe to call from storage, remediation and the API alike).

## License

TBD.
