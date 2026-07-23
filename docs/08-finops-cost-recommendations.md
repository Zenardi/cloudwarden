# 8 · FinOps: Cost & Recommendations

The FinOps side collects cost + utilization, detects idle/oversized resources,
estimates savings, and writes an AI executive summary. This pipeline is
**Azure-centric** today (AWS/GCP participate in AssetDB and governance, not yet in
cost analytics).

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

## Scheduled governance report

Set `GOVERNANCE_REPORT_ENABLED=true` and run the scheduler to write a timestamped
CSV export under `APP_DATA_DIR` on the `GOVERNANCE_REPORT_INTERVAL_SECONDS`
cadence. Ad-hoc evidence export is `GET /api/governance/export?format=csv|json`.
