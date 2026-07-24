# 7 · Governance-as-Code (Policies)

The platform embeds **Cloud Custodian (`c7n`)** as its policy engine. Policies are
authored as `c7n`-style specs, validated offline, versioned in the database, and
executed against your accounts in three modes.

## The policy spec

A policy is a `c7n` document: a resource type + filters + actions. Minimal
detection-only example (flag VMs missing an `Environment` tag):

```yaml
policies:
  - name: tag-compliance-vms-require-environment
    resource: azure.vm
    description: Virtual machines must carry an Environment tag.
    filters:
      - type: value
        key: tags.Environment
        value: absent
```

With an action (tag matched resources):

```yaml
policies:
  - name: mark-idle-vms
    resource: azure.vm
    filters:
      - type: value
        key: tags.Environment
        value: production
    actions:
      - type: tag
        tag: reviewed
        value: "true"
```

Resource types are cloud-prefixed: `azure.vm`, `aws.s3`, `gcp.bucket`, etc. Browse
what's available and each type's filters/actions:

```bash
GET /api/custodian/schema                    # list all resource types
GET /api/custodian/schema?resource_type=azure.vm   # filters + actions for one
```

## Authoring policies

**In the UI:** the **Policies** page has an editor with inline validation, an
enabled toggle, and version history with a diff viewer.

**Via API:**

```bash
# Validate a spec without saving (never persists)
POST /api/policies/validate      { "spec": { ...c7n... } }   → { valid, errors }

# Create (validate-on-write; 422 if invalid, 409 on duplicate name)
POST /api/policies               { name, resource_type, spec, description, source }

# Update (re-validates), delete, enable/disable
PUT    /api/policies/{id}
DELETE /api/policies/{id}
POST   /api/policies/{id}/enabled?enabled=true

# Version history + field-by-field diff
GET /api/policies/{id}/versions
GET /api/policies/{id}/versions/diff?from_version=1&to_version=2
```

Every create/update writes an immutable new **version**; nothing is lost.

## The three execution modes

| Mode | When it runs | How to trigger |
|------|--------------|----------------|
| **Pull** | Scheduled / on-demand batch across a scope | binding run, scheduler, CLI |
| **Push** | Ad-hoc, single policy, dry-run | `POST /api/policies/{id}/dryrun` |
| **Event** | Real-time, on a cloud change event | `POST /api/events/azure` |

### Push (dry-run one policy now)

```bash
POST /api/policies/{id}/dryrun?subscription_id=<id>
# → { policy_name, subscription_id, dry_run:true, matched:N, resources:[...] }
```

Matches only — never takes actions. Great for testing a policy before wiring it up.

### Pull (scheduled batch) — collections, account-groups, bindings

Pull mode is organized around three grouping objects:

- **Collection** — a named set of policies (many-to-many). Think "CIS-Azure" or
  "Cost Hygiene".
- **Account-group** — a named set of subscriptions/accounts (many-to-many). Think
  "Production" or "Dev/Test".
- **Binding** — attaches a collection to an account-group with execution config:
  `schedule` (cron; empty = manual only), `mode`, `dry_run`, `enabled`.

**End-to-end setup:**

```bash
# 1. Create a collection and add policies
POST   /api/collections                                  { name, description }
POST   /api/collections/{cid}/policies/{policy_id}

# 2. Create an account-group and add subscriptions
POST   /api/account-groups                               { name, description }
POST   /api/account-groups/{gid}/subscriptions/{subscription_id}

# 3. Bind them (dry-run first!)
POST   /api/bindings   { collection_id, account_group_id,
                         schedule: "0 2 * * *", mode: "pull",
                         dry_run: true, enabled: true }

# 4. Run now (regardless of schedule)
POST   /api/bindings/{bid}/run
# → { binding_id, status, matched, executed, errors, ... }
```

Each run records a **PolicyExecution** (per policy × subscription) with status,
matched-resource count, and actions taken — visible on the **Executions** page and
via `GET /api/policy-executions`. A disabled binding returns `skipped`.

Run **all** bindings/policies on their cadence with the scheduler
(`POLICY_RUN_INTERVAL_SECONDS`) or the CLI:

```bash
docker compose exec backend python -m cloudwarden.cli run-policies --mock
```

### Event (real-time)

Policies whose spec declares event mode participate in real-time enforcement when
a matching cloud change arrives. See
[12 · Real-Time Enforcement](12-real-time-enforcement.md).

## Shift-left (pre-provision / CI) — M14.6

Push/pull/event all run *after* provisioning — a violation is only caught once the
resource exists and bills. **Shift-left** evaluation runs the **same authored policies**
against an **IaC plan** (a Terraform `terraform show -json` plan) so a policy violation
fails the **PR/CI before anything is created**.

How it works (`custodian/shiftleft.py`):

1. **Parse** the plan (`parse_plan`) into flat resource dicts — walking child modules,
   lifting each attribute to the top level so c7n `value` filters apply, and keeping the
   Terraform **address** for reporting. A malformed plan raises a clean error (CLI exit 2
   / API `422`), never a stack trace.
2. **Map** each Terraform type to a c7n type (`azurerm_storage_account` → `azure.storage`).
   An **unmapped** type is *skipped* (reported, never an error) — coverage grows over time.
3. **Evaluate**: for each enabled policy, select the plan resources it targets and run the
   policy's filters through the **offline c7n matcher** (`engine.match_resources` — the
   same local filter machinery a dry-run uses; the one mockable seam, injectable for
   tests). Each match carries the policy, the resource **address**, a **severity** (from
   the policy's `metadata.severity`, default `medium`), and a rationale.

The worst severity maps to a **CI exit code**: any violation fails the build, or with
`--fail-on <severity>` only findings **at or above** that severity block (lower ones are
reported but non-blocking). Evaluation is fully offline (`FINOPS_MOCK=1`) — no cloud, no
credentials, no live Terraform. The optional live c7n IaC provider (`c7n_left`/`tfparse`)
is registered best-effort when installed (`engine.register_terraform`), otherwise the
offline path is used.

**Surfaced as:**

| Where | What |
|-------|------|
| `cloudwarden evaluate-iac <plan.json> [--fail-on <sev>]` | CLI — prints violations, exits non-zero to gate CI |
| `POST /api/policies/evaluate-iac` | API — `{"plan": <plan json>}` → matches + severity (RBAC `policy:run`); malformed → `422` |
| [`docs/examples/shift-left.github-workflow.yml`](examples/shift-left.github-workflow.yml) | copy-paste GitHub Action: `terraform show -json` → `evaluate-iac` |

## Configuration drift detection — M14.7

Push/pull/event/shift-left govern *policy*; **drift detection** governs *state*: did a
resource change away from its intended configuration? The AssetDB already stores each
resource's full `config` (JSONB) and change history, so `custodian/drift.py` turns that
into a control.

* **Baseline.** A per-resource desired-state snapshot: `config` **normalized** (volatile /
  noise fields dropped — `etag`, `provisioningState`, timestamps, …) plus a stable hash,
  **versioned** on re-baseline. Captured automatically the first time a resource is seen,
  or explicitly by an operator (re-baseline).
* **Diff.** Each run, `diff_config` recursively compares live (normalized) config against
  the baseline and emits **classified** changes — `added` / `removed` / `changed` — each
  with a **dotted field path** (`properties.networkAcls.defaultAction`). Because volatile
  fields are excluded at every level, an unchanged resource **never** drifts.
* **Attribution.** Each finding carries the recent Activity-Log change **events**
  (`attribute_events`) so it says *who/how* the resource changed, when that's known.
* **Fire once, never break the run.** Findings persist to `drift_findings` keyed on
  `(resource_id, baseline_version, change set)` — idempotent, so a new drift notifies
  exactly once through the existing transports; detection is best-effort in its own
  transaction.
* **Re-baseline (accept drift).** `POST /api/drift/baseline` snapshots the current config
  as the new baseline and **resolves** the resource's open findings — an explicit,
  RBAC-guarded (`drift:write`), **audited** act.

**Surfaced as:**

| Where | What |
|-------|------|
| `GET /api/drift` | drift findings — classified field diffs + attributed events (RBAC `drift:read`; filter by resource/status) |
| `POST /api/drift/baseline` | re-baseline a resource (RBAC `drift:write`, audited) |
| **Asset detail** page | a *Configuration drift* section — baseline-vs-current field diff + a *Re-baseline* button |

Toggled by `DRIFT_DETECTION_ENABLED` (default on); new findings alert through
`DRIFT_ALERT_CHANNEL` (empty = record silently). Azure-first behind the `CloudProvider`
abstraction.

## Exemptions / waivers — M14.9

A **waiver** is a first-class, scoped, justified, approved, **expiring** exception to a
policy — the governed alternative to a static `finops:exclude` tag or an RG allow-list.

**Scope.** A waiver targets a policy plus an optional grain:

| `scope_type` | `scope_value` | covers |
| --- | --- | --- |
| `policy` | — | every resource the policy matches |
| `resource` | a resource id | that one resource |
| `resource_group` | an RG name | resources in that group (case-insensitive) |
| `tag` | `key=value` | resources carrying that tag |

**Lifecycle** (a strict state machine — every transition audited):

```
request → pending ──approve──▶ active ──(expires_at passes)──▶ expired
                  └──reject──▶ rejected
```

Only an **active AND unexpired** waiver suppresses. At execution
(`queue_policy_action`) each match is resolved against the policy's active waivers
(`authz.waivers.waiver_for_match`); a covered match is recorded as **`waived`** on the
`PolicyMatch` — with the waiver id — and **never queued for enforcement**. An
**expired / pending / out-of-scope** waiver does **not** suppress, so the finding stays
enforceable and **re-surfaces automatically** the moment a waiver expires.

| Endpoint | Purpose |
| --- | --- |
| `GET /api/waivers` | list waivers (filter by `policy_id` / `state`) |
| `POST /api/waivers` | request a waiver (RBAC `waiver:request`, audited) → `pending` |
| `POST /api/waivers/{id}/approve` | approve (RBAC `waiver:approve`, audited) → `active` |
| `POST /api/waivers/{id}/reject` | reject (RBAC `waiver:approve`, audited) → `rejected` |
| **Waivers** page | request / approve / reject with state badges; matched resources show a **waived** badge |

An **expiring-soon** notification fires **once** per waiver when it is within
`WAIVER_EXPIRING_WITHIN_DAYS` of expiry, through `WAIVER_ALERT_CHANNEL` (empty = record
silently). A justification is mandatory and the expiry must be in the future.

## Preventive guardrails — M14.10

Every control so far is **detective + remediation** — it observes and fixes, but nothing
*prevents* a non-compliant resource from being created. Preventive guardrails close the
loop **detect → remediate → prevent** by translating a subset of authored intent into the
cloud's own **native deny construct**, enforced *at creation time*.

A policy **opts in** by declaring a guardrail on its first policy body:

```yaml
policies:
  - name: require-environment-tag
    resource: azure.vm
    metadata:
      guardrail:
        kind: required_tag          # required_tag | allowed_locations | allowed_skus | deny_public_ip
        params: {tag: Environment}
```

The capability subset and what each provider can express natively:

| Kind | Azure Policy | AWS SCP | GCP Org Policy |
|------|:---:|:---:|:---:|
| `required_tag` | ✅ `tags[...] exists false → deny` | ✅ `Null aws:RequestTag/... → Deny` | ❌ not expressible |
| `allowed_locations` | ✅ `not location in [...] → deny` | ✅ `aws:RequestedRegion → Deny` | ✅ `constraints/gcp.resourceLocations` |
| `allowed_skus` | ✅ `sku.name not in [...] → deny` | ❌ not expressible | ❌ not expressible |
| `deny_public_ip` | ✅ deny `publicIPAddresses` | ✅ deny `AssociatePublicIpAddress` | ✅ `constraints/compute.vmExternalIpAccess` |

A policy that declares **no** guardrail, or a kind the target provider **cannot** express,
returns an explicit **not-expressible** result — surfaced with a reason, **never a silent
no-op**.

```bash
# 1. Preview (what-if): the native definition + affected scope, NO mutation.
POST /api/guardrails/preview   { "policy_id": 12, "provider": "azure", "scope": "sub-123" }

# 2. Apply — dry-run-first, gated by the SAME remediation guardrails as write remediation.
POST /api/guardrails/apply     { "policy_id": 12, "provider": "azure", "dry_run": true }
```

**Safety model.** `apply` performs a real write **only** when the policy is expressible
**and** the remediation guardrails permit it — `REMEDIATION_ENABLED=true` **and** a
non-empty `ALLOWED_RESOURCE_GROUPS` (plus the write-scoped service principal). Otherwise it
is forced to a **dry-run** and the cloud is never touched. A provider error is **surfaced**
on the result (never a 500), and **every** preview and apply is **audited**
(`guardrail:preview` / `guardrail:apply`). Both endpoints are RBAC-guarded
(`guardrail:preview` / `guardrail:apply`). Translators sit behind a `preventive_translator`
capability on the `CloudProvider` abstraction, and cloud write clients are injectable — so
the whole path is verifiable with no live cloud. The **Guardrails** page walks translate →
what-if → dry-run apply.

## Policy packs

Pre-built, versioned bundles of policies (e.g. tag-compliance, cost-hygiene,
cis-azure). Installing a pack validates and materializes its policies into a
collection.

```bash
GET  /api/packs                       # available packs
GET  /api/packs/installed             # installed + version/enabled
POST /api/packs/{name}/install        # → { installed_policies, collection_id, errors }
POST /api/packs/{name}/enabled        { enabled: true|false }
```

Install is idempotent — re-installing the same version is a no-op. After install,
bind the resulting collection to an account-group.

## GitOps sync

Keep policies in a Git repo and sync them in. Configure:

```
GITOPS_REPO_URL=https://github.com/org/finops-policies.git   # empty disables
GITOPS_BRANCH=main
GITOPS_POLICY_PATH=policies
```

Trigger a sync (also runnable on the scheduler):

```bash
POST /api/policies/sync
# → { ok, added, updated, unchanged, skipped, errors }
```

Sync discovers `*.yml`/`*.yaml`/`*.json` under `GITOPS_POLICY_PATH`, validates each
through the engine, and upserts by policy **name** (idempotent — unchanged policies
don't bump their version). **Invalid files are skipped and reported, never fatal**,
so you can iterate the repo safely. The endpoint never 500s on git/validation
failure.

## GitOps write-back (policy-as-PR)

Sync above is **read-only** — policies flow *from* git into CloudWarden, but a policy
edited in the UI never flowed back. Write-back closes the loop: proposing a policy
opens a **pull request** against the policy repo. Nothing is pushed to the default
branch directly — the reviewed PR stays the source of truth, preserving the GitOps
model.

```bash
POST /api/policies/{id}/propose      # RBAC policy:propose, audited
# → { pr_url, branch, base_branch, path }
```

Configure the target repo + credentials (the token is **read from config and never
logged**):

```
GITOPS_WRITEBACK_REPO_URL=https://github.com/org/finops-policies.git  # blank → GITOPS_REPO_URL
GITOPS_WRITEBACK_BRANCH_PREFIX=cloudwarden/policy-
GITOPS_WRITEBACK_TOKEN=<PAT with repo/api scope>   # blank → proposing returns 400
GITOPS_PROVIDER=github                              # github | gitlab
```

Proposing (1) serializes the policy to its **canonical repo YAML** — the same layout
the read-sync imports, so it **round-trips with no drift** on re-import; (2) creates a
`cloudwarden/policy-<name>-<version>` branch, commits the file at
`<GITOPS_POLICY_PATH>/<name>.yml`, and pushes via an **injectable provider client**
(git + GitHub/GitLab API; mocked in tests, so the suite runs with no network); (3)
opens a PR with a templated body (policy, resource, author) and returns its URL.

**Safety guarantees.** It **refuses to target the default branch** (a proposal must
open a PR from a new branch). A missing token/repo is a clear `400` (never a silent or
partial write). A provider/network failure surfaces as `502` **with no partial state**
— nothing is audited unless the PR actually opened. Every successful proposal is
**audited** (`policy.propose` — actor, policy, PR URL). The provider token is passed to
the client out-of-band and never stored on a loggable object.

In the UI, each policy row's **Actions ▸ Propose change (open PR)** runs the flow and
surfaces the resulting PR link.

## Posture & health

Governance produces two rollups (both filterable by `?provider=`):

- `GET /api/governance/posture` → totals + breakdowns `by_policy`,
  `by_subscription`, `by_collection`, `by_control`, `by_provider` (compliant vs
  non-compliant, violation counts).
- `GET /api/governance/execution-health` → success rate, avg duration, last run —
  `by_policy`, `by_binding`, `by_provider`.
- `GET /api/governance/policies/{id}/matches` → resources currently flagged by a
  policy (the Compliance page drill-down).
- `GET /api/governance/export?format=csv|json` → stream governance evidence.

See these visualized in the **Compliance** page and the Grafana *Posture* /
*Execution Health* dashboards ([13](13-dashboards-grafana.md)).
