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
| M8.4 | Per-binding notify config & UI — attach channel+template to a binding; fire on violation; `/notifications` page | 🚧 in review |

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

**Tests:** 291 tests, **~99% line coverage** (gate at 95%, enforced in CI —
`.github/workflows/ci.yml`). Live-Azure code paths are covered via injected fake
clients; the DB/API/orchestrator/remediation flows run against a throwaway
PostgreSQL (testcontainers).

## License

TBD.
