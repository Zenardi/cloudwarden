# 6 · Multi-Cloud Onboarding

The platform tracks Azure, AWS, and GCP behind one `provider` dimension. Every
onboarded account lands in the `subscriptions` table with a `provider` tag; the
**first** account onboarded becomes the **default**. In mock mode you can onboard
all three with no real credentials.

> **Terminology:** "subscription" is the generic term in the data model for an
> onboarded account — an Azure *subscription*, an AWS *account*, or a GCP
> *project*.

## Azure

Azure is the native provider; its cost/metrics pipeline is the richest.

**Credentials** (`.env`, or per-subscription on the Subscriptions page):

```
AZURE_SUBSCRIPTION_ID=<guid>        # seeded as the default subscription
AZURE_TENANT_ID=<guid>
AZURE_CLIENT_ID=<app-id>
AZURE_CLIENT_SECRET=<secret>
```

The read SP needs **Reader + Cost Management Reader + Monitoring Reader**
(+ **Log Analytics Reader** for memory metrics). Empty client id/secret falls back
to Managed Identity / `az` CLI. The Custodian policy engine (`c7n-azure`) reuses
these same credentials.

**Onboard additional subscriptions** — UI **Subscriptions** page, or:

```bash
curl -X POST http://localhost:8000/api/subscriptions \
  -H 'Content-Type: application/json' \
  -d '{"subscription_id":"<guid>","display_name":"Prod","provider":"azure",
       "tenant_id":"<guid>","client_id":"<id>","client_secret":"<secret>","enabled":true}'
```

Each subscription can reuse the shared env SP or carry its **own**
tenant/client/secret (e.g. a different tenant). Verify with
`POST /api/subscriptions/{id}/test` → `{status:"ok"|"error"}`.

## AWS (M12.2)

**Credentials** (`.env` defaults, or per-account at onboarding):

```
AWS_ACCOUNT_ID=123456789012
AWS_DEFAULT_REGION=us-east-1
AWS_ROLE_ARN=arn:aws:iam::123456789012:role/FinOpsReader   # optional
AWS_ACCESS_KEY_ID=...                                       # optional
AWS_SECRET_ACCESS_KEY=...                                   # optional
```

Keys/role are optional — the live path falls back to the ambient role (IRSA /
instance profile / env).

**Onboard** — validates credentials via **STS `get_caller_identity`**:

```bash
curl -X POST http://localhost:8000/api/aws/accounts \
  -H 'Content-Type: application/json' \
  -d '{"account_id":"123456789012","display_name":"AWS Prod",
       "region":"us-east-1","role_arn":null,
       "access_key_id":null,"secret_access_key":null}'
# → { account: {...}, identity: { account_id, arn, ... } }   (400 if invalid)
```

**Ingest assets** into AssetDB (provider `aws`):

```bash
curl -X POST http://localhost:8000/api/aws/accounts/123456789012/ingest
# → { provider:"aws", account_id, assets: N, new: M }
```

**Dry-run an AWS policy** (no state change):

```bash
curl -X POST http://localhost:8000/api/aws/policies/dryrun \
  -d '{"account_id":"123456789012","region":"us-east-1",
       "spec":{"policies":[{"name":"public-s3","resource":"aws.s3","filters":[],"actions":[]}]}}'
# → { matched: N, resources: [...] }
```

## GCP (M12.3)

**Credentials:**

```
GCP_PROJECT_ID=finops-demo-prod
GCP_DEFAULT_REGION=us-central1
GCP_SERVICE_ACCOUNT_JSON=/path/to/sa-key.json   # or inline JSON; empty → ADC
```

> Live GCP **policy execution** needs the optional `c7n-gcp` extra (see
> `backend/requirements.txt`). Onboarding, ingestion, and mock mode work without it.

**Onboard** — validates via **Resource Manager `get_project`**:

```bash
curl -X POST http://localhost:8000/api/gcp/projects \
  -H 'Content-Type: application/json' \
  -d '{"project_id":"finops-demo-prod","display_name":"GCP Prod",
       "region":"us-central1","service_account_info":null}'
# → { account: {...}, identity: { project_id, ... } }   (400 if invalid)
```

**Ingest assets** (provider `gcp`):

```bash
curl -X POST http://localhost:8000/api/gcp/projects/finops-demo-prod/ingest
# → { provider:"gcp", project_id, assets: N, new: M }
```

**Dry-run a GCP policy:** `POST /api/gcp/policies/dryrun` with
`{project_id, region, spec}`.

## What onboarding enables

Once an account is onboarded (any cloud):

| Capability | How |
|------------|-----|
| Appears in **Subscriptions** page | `GET /api/subscriptions` |
| Assets flow into **AssetDB** | ingest endpoints / cost pipeline (Azure) |
| Policies of that cloud's resource types can run | pull/push/event modes |
| Posture & execution-health roll up by provider | `?provider=azure\|aws\|gcp` |
| Grafana dashboards filter by the **provider** template variable | — |

## Per-cloud model

| | Azure | AWS | GCP |
|-|-------|-----|-----|
| Account identifier | subscription GUID | 12-digit account id | project id |
| Validation | connection test | STS `get_caller_identity` | RM `get_project` |
| Cost collection | Cost Management Query API | Cost Explorer (M14.11) | BigQuery Billing Export (M14.11) |
| Metrics / Advisor / right-sizing | full | — (Azure-centric) | — (Azure-centric) |
| Policy resource types | `azure.*` | `aws.*` | `gcp.*` |
| Live policy engine | `c7n-azure` (bundled) | `c7n` core | `c7n-gcp` (optional extra) |

> **Cost/FinOps note:** cost collection is **tri-cloud** since M14.11 — each provider
> emits the identical normalized `CostRow`, so budgets, anomaly, forecasting and
> showback work across Azure, AWS and GCP. Utilization metrics, Advisor and the
> right-sizing rules remain Azure-centric. See
> [08 · FinOps](08-finops-cost-recommendations.md#multi-cloud-cost-collection-m1411)
> for the per-cloud sources and required permissions.

See [07 · Governance-as-Code](07-governance-as-code.md) to run policies against
onboarded accounts, and [15 · Demo Data](15-demo-data.md) for a script that
onboards all three clouds at once.
