# 8 · FinOps: Cost & Recommendations

The FinOps side collects cost + utilization, detects idle/oversized resources,
estimates savings, and writes an AI executive summary. **Cost collection is
tri-cloud** (M14.11): Azure, AWS and GCP each expose a `collect_cost` capability
behind the `CloudProvider` seam and emit the identical normalized `CostRow`, so
budgets / anomaly / forecast / showback are provider-agnostic. Utilization
metrics, Advisor and right-sizing rules remain Azure-centric for now.

## The cost pipeline

Run per subscription via `run` / `run-mock` / `POST /api/runs`. Ordered stages:

1. **Collect** — inventory, cost (Azure Cost Management, amortized), metrics
   (Azure Monitor CPU/mem/net/disk), optional memory (Log Analytics), Azure
   Advisor recommendations, and the Activity Log (change events).
2. **Analyze** — build utilization rollups (avg/p95/max + data completeness),
   map monthly cost per resource, evaluate rightsizing/idle rules, detect idle
   resources, prioritize by savings.
3. **AI reconciliation** — package the top recommendations + cost summary and ask
   the model for an executive summary and consolidated savings estimate.
4. **Store** — assets + events + relationships, cost snapshots, metric samples,
   rollups, advisor rows, ranked recommendations, and the AI summary; mark the run
   finished.

A run returns per-table counts, e.g. `assets`, `cost_rows`, `recommendations`,
`rollups`, `ai_summary`.

## Cost views

Cost is amortized over `COST_LOOKBACK_DAYS` (default 30) and surfaced as:

| API | Web UI / Grafana |
|-----|------------------|
| `GET /api/costs/summary` | Overview cards |
| `GET /api/costs/by-type` | Cost by resource type (pie) |
| `GET /api/costs/by-region` | Cost by region (bar) |
| `GET /api/costs/by-resource?limit=N` | Top resources table |

Every `/api/costs/*` endpoint accepts `?provider=azure|aws|gcp` (empty/`all` → all
clouds), filtering on the native `cost_snapshots.provider` column. The Grafana cost
dashboard exposes the same as a **Cloud** template variable.

## Multi-cloud cost collection (M14.11)

Each provider returns the **same** `CostRow` (amortized by default) so nothing
downstream is cloud-specific. Collectors are fixture-backed in `FINOPS_MOCK=1` and
talk to **injected** cloud clients otherwise — no live AWS/GCP in tests. The
orchestrator fans collection across onboarded accounts by provider
(`orchestrator.collect_costs`), isolating a single account's failure.

| Cloud | Source | Grouping | Region | Tags | Required permission |
|-------|--------|----------|--------|------|---------------------|
| Azure | Cost Management Query API | ResourceId + ServiceName (2-dim cap) | enriched from inventory | enriched from inventory | Cost Management Reader |
| AWS | Cost Explorer `get_cost_and_usage` (amortized) | RESOURCE_ID + SERVICE (2-dim cap) | parsed from the ARN | enriched from inventory | `ce:GetCostAndUsage` + resource-level cost enabled |
| GCP | BigQuery Billing Export (standard usage cost) | resource + service + region + day | from the export | from export `labels` | BigQuery Data Viewer + Job User on the export dataset |

Point GCP at its export table with `GCP_BILLING_EXPORT_TABLE=project.dataset.table`.
Pagination (`NextPageToken` / page tokens) and 429/throttle retries mirror the Azure
collector's resilience. AWS/GCP resource ids are globally unique, so a cost row is
self-describing; the pipeline still enriches `resource_type`/tags from inventory where
the cost source can't supply them (as Azure does).

## Recommendation rules & thresholds

Rules run over the utilization rollups. Thresholds are all configurable
([03 · Configuration](03-configuration.md)):

### Idle → stop/deallocate

Flag a VM as idle when CPU p95 < `SHUTDOWN_CPU_P95` (3.0) **and** CPU max <
`SHUTDOWN_CPU_MAX` (5.0) and network is negligible.
- Action: deallocate · Risk: medium · higher confidence
- Savings: full monthly compute (note: attached disks keep billing)

### Oversized → downsize

Flag for rightsizing when CPU p95 < `DOWNSIZE_CPU_P95` (40.0) **and** CPU max <
`DOWNSIZE_CPU_MAX` (80.0) (memory considered when available, threshold
`DOWNSIZE_MEM_P95` 50.0). The engine proposes the next-smaller SKU in the same
family and computes the price delta.
- Action: resize · Risk: low · confidence reduced when memory data is missing

### Orphaned resources

Heuristics flag unattached managed disks, unassociated public IPs, and empty App
Service plans for deletion.

### Data quality gate

Recommendations require metric completeness ≥ `MIN_DATA_COMPLETENESS` (0.8);
sparse data lowers confidence or skips the finding.

### Azure Advisor reconciliation

Azure Advisor cost recommendations are merged in: when Advisor agrees on a
resource, the recommendation is marked as combined and confidence is boosted.

## Commitment coverage & RI/Savings-Plan recommendations (M14.1)

Steady-state, always-on resources billed at on-demand rates are the single largest
untapped FinOps lever: Reservations (RI) and Savings Plans (SP) discount them
20–70%. CloudWarden collects existing commitments and eligible steady-state usage
(per SKU family/region, aggregated — never raw samples) and emits two signals, both
under the `commitment` recommendation category:

- **Under-utilized commitment** (advisory waste) — an existing RI/SP utilized below
  `COMMITMENT_UNDER_UTILIZED_PCT` (80%). The idle share of committed capacity is
  money paid for nothing; the estimate is the wasted monthly amount.
- **Expiring commitment** (informational) — a commitment lapsing within
  `COMMITMENT_EXPIRING_WITHIN_DAYS` (60) days; renew or re-plan before it reverts to
  on-demand. No savings are claimed.
- **Under-covered steady-state** (purchase recommendation) — eligible usage that
  runs *every day* of the window but isn't committed. The candidate is sized at the
  **min-of-window** baseline (the level present every single day — never a burst; a
  baseline below `COMMITMENT_MIN_HOURLY` $/hr yields no recommendation), priced with
  a blended commitment discount for each term (P1Y/P3Y) and payment option
  (no/partial/all-upfront), with **break-even** months for each.

Coverage (% of eligible spend already covered) and blended commitment utilization
are rolled up per SKU family/region. All savings are **estimates** carrying a
`basis` and caveats, environment-weighted like idle/waste savings (a `Prod`
subscription discounts them; see the reclaim factors), AI-reconciled, and persisted.
Advisory items never over-state — the min-of-window floor and blended discount are
deliberately conservative.

**Surfaced in:**

| Where | What |
|-------|------|
| `GET /api/finops/commitments` | coverage rollups + commitment portfolio + candidates (RBAC-guarded: `commitment:read`) |
| `GET /api/recommendations` | commitment recs alongside the rest (category `commitment`) |
| **Recommendations** web page | *Commitment coverage* panel + the recs table |
| *Recommendations* Grafana dashboard | coverage-by-family + existing-commitments panels |

Azure-first, behind the `CloudProvider` abstraction — AWS/GCP get no commitment
signal yet (no-op stubs). The live path derives the commitment portfolio from the
ARM Reservations/Consumption APIs; mock mode (`FINOPS_MOCK=1`) is backed by
`fixtures/reservations.json`.

## Budgets & threshold alerting (M14.2)

Everything above is *descriptive* — it reports spend after the fact. Budgets make
FinOps *preventive*. A **budget** is a spend limit over a **scope** (a subscription,
account, account-group, tag value or team) and a **period** (monthly or quarterly),
with an ordered list of **threshold rules** — each `{"pct": <float>, "basis":
"actual"|"forecast"}` (a `forecast`-basis rule is inert until forecasting lands in
M14.4). Every pipeline run — and every scheduler tick, transitively — evaluates
actual (and, when available, forecast) spend against each enabled budget after cost is
persisted.

Two invariants govern alerting:

- **Fire once.** When spend newly crosses a threshold, exactly **one** notification is
  sent — for the *highest* newly-crossed threshold, so a jump past several at once is a
  single alert, never a storm. Each crossing is persisted as a `BudgetThresholdEvent`
  keyed on `(budget, period, threshold, basis)`; re-evaluating the same period is a
  no-op and a new period resets the slate.
- **Never break the run.** Notification dispatch is best-effort — a transport failure
  is logged and swallowed; the crossing is still recorded so it won't re-fire.

Alerts reuse the **existing** notification fabric (`notify/service.py` +
`notify/dispatch.py`, all five transports — Slack / email / Teams / Jira / ServiceNow /
webhook): a budget names a `channel_id` (and optionally a `template_id`, defaulting to
the seeded `budget-alert` template). A budget with no channel evaluates silently. No
new delivery code path is added.

Scope resolution runs over `cost_snapshots` (amortized): `subscription`/`account`
filter the subscription directly, `account_group` resolves to its member
subscriptions. `tag`/`team` scope **degrades** to a subscription match on the scope
value (the M14.5 tag dimension backs showback reporting below; wiring it into budget
scope resolution is a later step).

**Surfaced in:**

| Where | What |
|-------|------|
| `GET /api/budgets` · `POST` · `PATCH` · `DELETE /api/budgets/{id}` | budget CRUD (RBAC `budget:read`/`budget:write`; mutations audited) |
| `GET /api/budgets/{id}/status` | current-period spend, percent-of-limit, crossed thresholds, recorded events |
| **Budgets** web page | create/edit budgets + spend-vs-limit bars and threshold chips |
| *FinOps — Cost Overview* Grafana dashboard | budget-vs-actual (latest crossing) panel |

Budget evaluation is toggled by `BUDGET_ALERTS_ENABLED` (default on) and is Azure-first
behind the `CloudProvider` abstraction.

## Cost anomaly detection (M14.3)

Budgets guard a *known* limit; anomaly detection catches the *unexpected* — a spend
spike nobody set a threshold for. Every pipeline run, after cost is persisted,
`analysis/anomaly.py` scores the latest day's spend for each scope and flags the
statistically abnormal ones.

**Grain.** Detection runs at four grains — `subscription`, `service`, `resource_type`,
`resource` — so a spike is caught whether it's one runaway VM or a whole service line.

**A robust, seasonality-aware baseline.** For each scope's trailing window
(`ANOMALY_WINDOW_DAYS`, default 45) the centre is the **median** and the spread the
**MAD** (median absolute deviation) — both immune to the very outliers we hunt, unlike
mean/stdev which a single spike would poison. Cloud spend is weekly-seasonal, so each
day is **deseasonalized** by its weekday factor (that weekday's median ÷ the overall
median) before scoring: an in-pattern weekend peak is *expected*, not anomalous. The
score is the day's distance from the centre in robust-sigma (MAD) units; a day scoring
at or above `ANOMALY_SENSITIVITY` (default 3.5; **lower = more sensitive**) is an
anomaly, bucketed into a **severity** — `low` / `medium` / `high` / `critical`.

**Signal-gated — no false positives.** Two guards keep it quiet when it should be:

- **Thin history.** With fewer than `ANOMALY_MIN_HISTORY_DAYS` (default 14) baseline
  days, the detector emits **nothing** — a new subscription never trips an alert.
- **Ultra-steady series.** A scale floor (the spread is at least a small fraction of the
  centre) stops a perfectly flat series turning trivial noise into an infinite score.

**Contribution breakdown.** Each anomaly carries a ranked `contributors` list — the
child rows (resources, or meters for a resource-grain anomaly) whose day-vs-baseline
delta drove the spike, each with its `share` of the increase — so an alert says *what*
moved, not just *that* something did.

**Fire once, never break the run.** An anomaly persists to `cost_anomalies` keyed on
`(scope_type, scope_value, usage_date)`. The first time a scope+date is seen it fires
**exactly one** notification through the **existing** transports (the channel named by
`ANOMALY_ALERT_CHANNEL`; empty = record silently — no new delivery code path); a
re-detect refreshes the row but never re-alerts. Dispatch is best-effort — a transport
failure is logged and swallowed, and the anomaly stays recorded (unnotified).

**Surfaced in:**

| Where | What |
|-------|------|
| `GET /api/finops/anomalies` | recorded anomalies + expected/actual/score/severity/contributors (RBAC-guarded: `anomaly:read`; filter by scope/severity/date window) |
| **Cost explorer** web page | a *Cost anomalies* panel — severity, spike ratio, and the top driver |
| *FinOps — Cost Overview* Grafana dashboard | *Cost anomalies — recent spikes* table |

Detection is toggled by `ANOMALY_DETECTION_ENABLED` (default on), deseasonalization by
`ANOMALY_SEASONALITY`, and is Azure-first behind the `CloudProvider` abstraction. In
mock mode a seeded spike (`fixtures/cost_anomaly.json`) is overlaid only when
`ANOMALY_MOCK_SPIKE=1`, so a demo stack surfaces a live anomaly while the default mock
series (and the test suite) stay smooth.

## Cost forecasting (M14.4)

Reporting answers "what did we spend?"; leadership keeps asking "where will we **land**
this month?". `analysis/forecast.py` projects spend to **month-end** and **quarter-end**
per scope (`total` / `subscription` / `service`) over the `cost_snapshots` time-series,
each run, right after the cost store commits and just before budgets evaluate.

**A transparent, explainable model — not a black box.** The forecast decomposes into two
parts anyone can reason about:

* **Trend** — an ordinary least-squares line over the trailing window's daily totals
  (default `FORECAST_WINDOW_DAYS=90`), indexed by *calendar day* so gaps don't skew the
  slope.
* **Weekday seasonality** — multiplicative per-weekday factors (that weekday's median
  ratio to the trend line), so a heavy-weekend / light-weekend pattern is projected, not
  averaged away. Each remaining day is `max(trend, 0) × weekday_factor`.

The period point is `actual-to-date (this period) + Σ projected remaining days`.

**Every forecast carries its own credibility.** Two honesty guarantees:

* **A prediction interval.** `[lower, upper]` widens with the residual spread and the
  number of days still to project (`z · σ · √remaining`, `z` from
  `FORECAST_CONFIDENCE_PCT`, default 80%), floored at what's already been spent — so the
  point always sits inside its interval and the band never dips below booked spend.
* **A backtested accuracy (MAPE).** A rolling-origin one-step-ahead backtest over the
  tail of the window records a mean absolute percentage error next to the number, so the
  estimate is honest about its typical miss.

**Degrades gracefully — never fabricates, never hides.** Below `FORECAST_MIN_HISTORY_DAYS`
(default 14) the forecaster still emits a **clearly-labelled** `confidence: low`
(`model: linear_low_confidence`) estimate — a wider band, no seasonality, no backtest —
rather than a confident-looking fiction or nothing at all.

**Budgets consume it.** A budget threshold with `basis: "forecast"` (M14.2) fires off the
projection computed the same run: the pipeline injects `forecast_for_budget` into
`evaluate_budgets`, mapping the budget's period to a horizon and its scope to a forecast
grain (a tenant-wide budget → `total`). This is the **forecasted-to-exceed** alert —
warn *before* the month closes over budget, not after. Groups/tags/teams have no forecast
dimension until the M14.5 tag-cost work and simply yield no forecast metric (the forecast
rule is skipped, never fired off actual spend).

**Fit once, feed many; never break the run.** Forecasts persist to `cost_forecasts` keyed
on `(scope_type, scope_value, horizon, as_of)` — one row per grain, horizon and day,
refreshed idempotently on a same-day re-run. Forecasting runs best-effort in its own
transaction: a failure is logged and swallowed, never failing a collection run.

**Surfaced in:**

| Where | What |
|-------|------|
| `GET /api/costs/forecast` | recorded forecasts + point/interval/MAPE/confidence per scope & horizon (RBAC-guarded: `forecast:read`; filter by scope/horizon) |
| **Cost explorer** web page | a *Spend forecast* panel — projected total, range, booked-to-date and backtest error per horizon |
| *FinOps — Cost Overview* Grafana dashboard | *Spend forecast — projection to period end* table |

Forecasting is toggled by `FORECAST_ENABLED` (default on), weekday seasonality by
`FORECAST_SEASONALITY`, and is Azure-first behind the `CloudProvider` abstraction.

## Showback / chargeback by tag → team (M14.5)

CloudWarden *enforces* a cost-allocation tag (the cost / tag-compliance packs) but until
now never *reported spend by it* — cost rolled up only by resource / type / region. This
adds **cost allocation**: group spend by an arbitrary tag key (`CostCenter` / `Owner` /
`Team` / `env`), map tag values to the existing **teams** model, and surface an explicit
**unallocated** bucket for untagged spend.

**Tags become a cost dimension.** Each run enriches cost rows with the owning resource's
tags from inventory (matched on the **lower-cased** `resource_id`, since the Cost API and
Resource Graph can disagree on casing) and persists them on `cost_snapshots.tags` (JSONB).
Aggregation groups by `tags ->> :key` — the tag key is a **bound parameter**, so an
arbitrary/hostile key is a harmless JSONB lookup, never executed SQL (injection-safe).

Three invariants shape the allocation:

- **Nothing is silently dropped.** Spend with no value for the grouping key lands in an
  explicit `unallocated` bucket — the number you actually want to drive down — never
  discarded.
- **Reconciliation.** `allocated + unallocated == total` always holds; the report is a
  partition of the scoped spend.
- **Team-scoped.** Tag values map to teams (`SHOWBACK_TEAM_MAP`, a JSON
  `{tag_value: team}`); a team-scoped principal (RBAC on) sees **only** its own
  allocation — the query filters to that team's tag values, so another team's spend (and
  the unallocated bucket) never leak. An admin / RBAC-off caller sees the full partition.

**Shared costs.** A designated `SHOWBACK_SHARED_TAG_VALUE` (e.g. a `shared` platform
bucket) is redistributed across the other allocated buckets — **even** (equal) or
**proportional** (by each bucket's own spend, `SHOWBACK_SPLIT_METHOD`) — preserving the
total.

**Surfaced in:**

| Where | What |
|-------|------|
| `GET /api/costs/by-tag` · `GET /api/costs/showback` | the allocation report — per tag value (mapped to a team) + the unallocated bucket, reconciling to the scoped total (RBAC `showback:read`, team-scoped) |
| `GET /api/costs/showback/export?format=csv\|json` | the same, streamed one row per tag value (reuses the governance-export streamer) |
| **Showback** web page | allocation table + total/allocated/unallocated cards + CSV/JSON export |
| *FinOps — Cost Overview* Grafana dashboard | *Showback — cost by owner (tag)* bar panel (over the `v_cost_by_tag` view) |

The grouping key defaults to `SHOWBACK_TAG_KEY` (`owner` — the mock inventory's tag; set
to `CostCenter` in production). Azure-first behind the `CloudProvider` abstraction.

## AI executive summary

Configured via the `AI_*` keys ([03](03-configuration.md#ai-provider-executive-summary)).
The engine sends the top `AI_MAX_CANDIDATES` (40) recommendations + a cost summary
to the model (`AI_PROVIDER`/`AI_MODEL`, default Anthropic `claude-opus-4-8`; or an
OpenAI-compatible endpoint via `AI_BASE_URL`) and stores a short narrative of key
risks, priorities, and quick wins. Read it on the **Overview** page,
`GET /api/summary`, and the *Recommendations* Grafana dashboard. In mock mode it's
produced from fixtures without a live model call.

## Reviewing & acting on recommendations

On the **Recommendations** page (or via API):

```bash
GET  /api/recommendations                              # list latest, with savings

# Approve or reject (records the decision + actor)
POST /api/recommendations/{rec_id}/decision   { "decision": "approve", "actor": "you" }

# Remediate an approved recommendation (dry-run by default)
POST /api/recommendations/{rec_id}/remediate?dry_run=true&actor=you
```

Remediation is gated by guardrails and (for real writes) `REMEDIATION_ENABLED` —
see [09 · Remediation](09-remediation.md).

## Kubernetes namespace cost & right-sizing (M14.12)

Managed clusters (AKS/EKS/GKE) are discovered behind each cloud; their **node cost** is
the pool CloudWarden allocates. See
[06 · Onboarding](06-multi-cloud-onboarding.md#kubernetes-m1412) for the endpoints and the
**Kubernetes** UI page / Grafana **cost-by-namespace** panel.

**Namespace cost allocation.** Each cluster's monthly node cost is split across its
namespaces **by requested resources** — CPU-request share and memory-request share
blended 50/50. The result is a *partition*: the per-namespace costs reconcile exactly to
the cluster node cost (`sum(cost) == node_monthly_cost`, `sum(share) == 1`), with any
rounding residual placed on the largest namespace. Stored as `provider="kubernetes"` /
`cost_type="Allocated"` cost rows — self-describing and deliberately **excluded** from the
Amortized cloud-cost queries, so K8s allocation never double-counts into
budgets/anomaly/forecast.

**Workload right-sizing.** A workload whose observed usage sits under **both** its CPU and
memory requests (below `K8S_OVERPROVISION_THRESHOLD`, default 50 %) is over-provisioned;
the recommendation proposes lower per-pod requests at
`observed usage × K8S_RIGHTSIZE_HEADROOM` (default +20 % headroom). Savings are
**advisory** — K8s cost rolls up to the node, so a reclaim only materializes if freed
capacity is scaled in — and every rec carries that caveat.

**Idle namespaces.** A namespace whose observed workloads recorded ~0 usage (at/under
`K8S_IDLE_THRESHOLD` cores/GiB) is flagged, with its allocated node cost as the advisory
saving.

**Signal-gated.** Right-sizing and idle detection fire **only** when usage was observed
(`samples > 0`). A workload or namespace with no usage row is *unknown* — never flagged on
the absence of data.

## Scheduled governance report

Set `GOVERNANCE_REPORT_ENABLED=true` and run the scheduler to write a timestamped
CSV export under `APP_DATA_DIR` on the `GOVERNANCE_REPORT_INTERVAL_SECONDS`
cadence. Ad-hoc evidence export is `GET /api/governance/export?format=csv|json`.
