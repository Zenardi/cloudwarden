# Azure FinOps Optimizer

Analyze an Azure subscription's **cost and consumption**, visualize spend by
**resource / resource type / region**, and recommend (and, when approved,
**execute**) right-sizing and shutdown actions from CPU / RAM / I/O and other
FinOps metrics — pulling from **Azure Cost Management** and **Azure Monitor**,
surfacing everything on **Grafana**, with a **pluggable AI** layer (Anthropic by
default; any OpenAI-compatible/local model).

## Status

| Phase | Scope | State |
|------|-------|-------|
| 0 | Scaffold (config, auth, resilience, storage, Docker, CI tooling) | ✅ done |
| 1 | **MVP:** cost + inventory → Postgres/Timescale → Grafana cost dashboard | ✅ done, verified |
| 2 | Metrics collector + FinOps rules (shutdown/downsize/idle) + savings | 🔜 scaffolded |
| 3 | Pluggable AI recommendations + executive summary | ✅ done |
| 4 | FastAPI + Next.js UI (review/approve) | ✅ done |
| 5 | Guarded remediation (deallocate/resize/delete, dry-run default) | 🔜 scaffolded |

The MVP runs fully offline with recorded fixtures (`FINOPS_MOCK=1`) — no Azure
subscription required to see the pipeline and dashboards working.

## Grafana feasibility (the assessment you asked for)

**Feasible.** Grafana's native **Azure Monitor** datasource covers live metrics,
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

## Quickstart (mock mode, no Azure needed)

Prerequisites: Docker with Compose v2 (`docker compose`).

```bash
cp .env.example .env            # defaults to FINOPS_MOCK=1
make up                         # starts db (TimescaleDB) + backend (API) + grafana
make seed                       # runs one mock pipeline → populates the DB
```

Then open:

- **Grafana** → http://localhost:3000 (anonymous viewer enabled) → *FinOps* folder
  → **FinOps — Cost Overview** (cost by type / region / resource + daily trend).
- **API docs** → http://localhost:8000/docs (`/api/costs/summary`, `/api/recommendations`, …).
- **Web UI (Next.js)** → run `make up-all` (or `docker compose --profile frontend up -d --build`),
  then http://localhost:3001 — overview, cost explorer, and recommendation review/approve.

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

## Key configuration

| Env | Purpose |
|-----|---------|
| `FINOPS_MOCK` | `1` = use fixtures (offline); `0` = call Azure |
| `AI_PROVIDER` / `AI_MODEL` | `anthropic` (default `claude-opus-4-8`) or `openai` |
| `AI_BASE_URL` | OpenAI-compatible endpoint for local models (Ollama/vLLM/LM Studio) |
| `COST_LOOKBACK_DAYS` / `METRIC_LOOKBACK_DAYS` | analysis windows |
| `REMEDIATION_ENABLED` | `false` = dry-run only |
| `LOG_ANALYTICS_WORKSPACE_ID` | enables memory-based downsize rules |

Full list: `.env.example`.

## Project layout

```
backend/azure_finops/
  config.py auth.py resilience.py models.py orchestrator.py scheduler.py cli.py
  azure/       inventory.py cost.py  (metrics.py logs.py advisor.py — Phase 2)
  analysis/    (rollup/rules/idle/pricing/savings — Phase 2)
  ai/          (base/anthropic/openai/factory/prompt — Phase 3)
  remediation/ (executor/guardrails/approval — Phase 5)
  storage/     schema.py db.py repository.py
  api/         main.py
  fixtures/    inventory.json cost.json
grafana/       provisioning/ + dashboards/finops-cost.json
frontend/      (Next.js — Phase 4)
docker-compose.yml  Makefile  .env.example
```

## Local development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r backend/requirements.txt
make lint      # ruff
make test      # pytest (mock-mode unit tests; no DB/Azure needed)
make run-mock  # run pipeline locally against a Postgres at localhost:5432
```

## License

TBD.
