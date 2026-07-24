# 9 · Remediation

Remediation takes corrective action on a resource — deallocating an idle VM,
deleting an orphaned disk, tagging or stopping a policy-matched resource. It is
**dry-run by default** and gated by layered guardrails, so it's safe to explore
before you ever touch a real cloud.

## Two sources of remediation

1. **FinOps recommendations** — approve a recommendation, then remediate it.
2. **Policy actions** — a pull-mode policy that declares `actions` queues those
   actions on matched resources for approval.

Both funnel into the same guarded executor and the same **Remediation** audit
(`GET /api/remediation`, filterable by `source`).

## The master switch

```
REMEDIATION_ENABLED=false      # default → every action is forced to dry-run
```

While `false`, actions are **previewed** (what *would* happen) but never executed.
This is the global kill-switch. Even when `true`, the guardrails below still apply.

## Guardrails (all must pass for a real write)

| Guardrail | Env | Behavior |
|-----------|-----|----------|
| **Resource-group allow-list** | `ALLOWED_RESOURCE_GROUPS` | Comma-separated. **Empty = nothing writable** (safe default). A resource outside the list is forced to dry-run. |
| **Exclude tag** | `EXCLUDE_TAG` (`finops:exclude`) | A resource carrying this tag is **never** touched. |
| **Action-type allow-list** | `ALLOWED_ACTIONS` | Comma-separated Custodian action types (e.g. `tag,stop`). Empty = any action. |
| **Write SP** | `AZURE_REMEDIATION_*` | Real Azure writes need the write-scoped service principal. |

## Recommendation-driven flow

```bash
# 1. Approve the recommendation
POST /api/recommendations/{rec_id}/decision   { "decision": "approve", "actor": "you" }

# 2. Remediate — dry-run first
POST /api/recommendations/{rec_id}/remediate?dry_run=true&actor=you
#    → preview: "[dry-run] would deallocate <resource>"

# 3. Real execution (needs REMEDIATION_ENABLED=true + guardrails satisfied)
POST /api/recommendations/{rec_id}/remediate?dry_run=false&actor=you
```

In the UI, approving a recommendation unlocks the **Remediate (dry-run)** button.

## Policy-action approval flow

When a pull-mode policy declares actions and matches resources, each action is
queued as **pending** (never auto-executed):

```bash
# Queue an action for a specific policy match (also happens during binding runs)
POST /api/policy-matches/{match_id}/actions   { "action": "stop", "actor": "you", "dry_run": true }

# Review → approve (guarded execution) or reject (never executes)
POST /api/remediation/{action_id}/approve?actor=you
POST /api/remediation/{action_id}/reject?actor=you
```

`approve`/`reject` return 409 if the action was already decided.

## Waivers suppress enforcement (M14.9)

A resource covered by an **active, in-scope waiver** ([governance-as-code](07-governance-as-code.md#exemptions--waivers--m149))
is never enforced. When a match is queued (`queue_policy_action`), it is first resolved
against the policy's active waivers: a covered match is recorded as **`waived`** on the
`PolicyMatch` (with the waiver id) instead of `pending` — visible, audited, **never
silently dropped**, and never executed. An **expired / pending / out-of-scope** waiver
does **not** suppress, so the finding stays enforceable and re-surfaces the moment the
waiver expires. Waivers are a distinct control from the block-by-default guardrails: a
guardrail blocks a real *write* at execution; a waiver removes the *finding* from
enforcement entirely for its scoped, time-boxed, approved window.

## Unified audit

Every approved/rejected action — from recommendations, policies, or bindings — is
recorded and viewable:

```bash
GET /api/remediation?limit=100&source=recommendation|policy|binding
```

The **Remediation** page renders this as an audit table (timestamp, source, action
type, resource, dry-run flag, status, error).

## Recommended rollout

A staged path from safe to production:

```bash
# Phase 1 — preview only (default)
REMEDIATION_ENABLED=false

# Phase 2 — dry-run in one group, no deletes
REMEDIATION_ENABLED=true
ALLOWED_RESOURCE_GROUPS=dev-rg
ALLOWED_ACTIONS=tag,stop
EXCLUDE_TAG=finops:exclude

# Phase 3 — production
REMEDIATION_ENABLED=true
ALLOWED_RESOURCE_GROUPS=prod-rg-1,prod-rg-2
ALLOWED_ACTIONS=tag,stop,delete
# + AZURE_REMEDIATION_TENANT_ID / _CLIENT_ID / _CLIENT_SECRET
```

Keep `dry_run=true` on bindings until you've reviewed a few real runs, then flip it.
