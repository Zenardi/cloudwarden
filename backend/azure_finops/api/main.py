"""FastAPI application exposing cost, recommendations, runs and health.

Grafana reads the SQL views directly from Postgres; this API serves the Next.js
UI and on-demand pipeline triggers. It is intentionally thin — all queries live
in the repository.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError

from ..custodian import engine as custodian
from ..custodian.engine import CustodianRunner
from ..models import (
    AssetQuery,
    CollectionCreate,
    PolicyCreate,
    PolicyUpdate,
    ValidateRequest,
    ValidateResult,
)
from ..remediation import approval as remediation
from ..resilience import REGISTRY
from ..storage import repository as repo
from ..storage.db import init_db, session_scope

logger = logging.getLogger("azure_finops.api")


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        init_db()
        from ..config import get_settings

        with session_scope() as session:
            repo.ensure_default_subscription(session, get_settings())
    except Exception:  # noqa: BLE001 - endpoints will surface DB errors individually
        logger.exception("init_db failed at startup")
    yield


app = FastAPI(title="Azure FinOps Optimizer", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "sources": REGISTRY.snapshot()}


@app.get("/api/costs/summary")
def costs_summary() -> dict[str, Any]:
    with session_scope() as session:
        return {
            "total": repo.total_cost(session),
            "by_type": repo.cost_by_type(session),
            "by_region": repo.cost_by_region(session),
        }


@app.get("/api/costs/by-type")
def costs_by_type() -> list[dict[str, Any]]:
    with session_scope() as session:
        return repo.cost_by_type(session)


@app.get("/api/costs/by-region")
def costs_by_region() -> list[dict[str, Any]]:
    with session_scope() as session:
        return repo.cost_by_region(session)


@app.get("/api/costs/by-resource")
def costs_by_resource(limit: int = 50) -> list[dict[str, Any]]:
    with session_scope() as session:
        return repo.cost_by_resource(session, limit=limit)


@app.get("/api/recommendations")
def recommendations() -> list[dict[str, Any]]:
    with session_scope() as session:
        return repo.latest_recommendations(session)


class Decision(BaseModel):
    decision: str  # approve | reject
    actor: str | None = None


@app.post("/api/recommendations/{rec_id}/decision")
def decide_recommendation(rec_id: int, body: Decision) -> dict[str, Any]:
    status = {"approve": "approved", "reject": "rejected"}.get(body.decision)
    if status is None:
        raise HTTPException(status_code=400, detail="decision must be 'approve' or 'reject'")
    with session_scope() as session:
        ok = repo.decide_recommendation(session, rec_id, status, body.actor)
    if not ok:
        raise HTTPException(status_code=404, detail="recommendation not found")
    return {"id": rec_id, "status": status}


# --------------------------------------------------------------------------- #
# Governance-as-code: policy validation + Custodian schema (M1.3)
# --------------------------------------------------------------------------- #
def get_custodian_runner() -> CustodianRunner | None:
    """Injection seam for the Custodian engine.

    Returns ``None`` so the engine falls back to its cached ``LiveCustodianRunner``
    (real, offline c7n for validate/schema). Tests override this dependency with a
    ``FakeCustodianRunner`` to keep the API suite fully offline.
    """
    return None


@app.post("/api/policies/validate")
def validate_policy(
    body: ValidateRequest,
    runner: Annotated[CustodianRunner | None, Depends(get_custodian_runner)] = None,
) -> ValidateResult:
    """Dry-run validate a Cloud Custodian policy spec — never persists, never 500s.

    A malformed body (no ``policies`` list) is bad input → ``400``. A well-formed
    spec is validated by the engine and reported as ``{valid, errors}`` with
    ``200`` even when invalid (validation ran and produced errors).
    """
    spec = body.spec
    if not isinstance(spec, dict) or not spec.get("policies"):
        raise HTTPException(status_code=400, detail="malformed policy: expected a 'policies' list")
    try:
        result = custodian.validate_policy(spec, runner=runner)
    except Exception as exc:  # noqa: BLE001 - degrade to 400, never surface a 500
        raise HTTPException(status_code=400, detail=f"validation failed: {exc}") from exc
    return ValidateResult(valid=bool(result.get("valid")), errors=list(result.get("errors") or []))


@app.get("/api/custodian/schema")
def custodian_schema(
    resource_type: str | None = None,
    runner: Annotated[CustodianRunner | None, Depends(get_custodian_runner)] = None,
) -> dict[str, Any]:
    """List Azure resource types (no arg) or return one type's filters/actions/schema.

    An unknown ``resource_type`` is bad input → ``400``; any engine error degrades
    to ``400`` rather than a ``500``.
    """
    try:
        result = custodian.get_schema(resource_type, runner=runner)
    except Exception as exc:  # noqa: BLE001 - degrade to 400, never surface a 500
        raise HTTPException(status_code=400, detail=f"schema lookup failed: {exc}") from exc
    if isinstance(result, dict) and result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])
    return result


# --------------------------------------------------------------------------- #
# Policy CRUD (M2.1) — validate-on-write governance-as-code
# --------------------------------------------------------------------------- #
def _policy_view(policy: dict[str, Any]) -> dict[str, Any]:
    """Every persisted policy passed validate-on-write, so surface its status."""
    return {**policy, "validation_status": "valid"}


def _require_valid_spec(spec: dict[str, Any], runner: CustodianRunner | None) -> None:
    """Reject a spec that fails Custodian validation with ``422`` (no persistence)."""
    result = custodian.validate_policy(spec, runner=runner)
    if not result.get("valid"):
        raise HTTPException(
            status_code=422,
            detail={"message": "policy validation failed", "errors": result.get("errors") or []},
        )


@app.get("/api/policies")
def list_policies(enabled: bool | None = None) -> list[dict[str, Any]]:
    """List policies, optionally filtered by ``?enabled=true``/``false``."""
    with session_scope() as session:
        if enabled is None:
            policies = repo.list_policies(session)
        elif enabled:
            policies = repo.list_policies(session, enabled_only=True)
        else:
            policies = [p for p in repo.list_policies(session) if not p["enabled"]]
    return [_policy_view(p) for p in policies]


@app.get("/api/policies/{policy_id}")
def get_policy(policy_id: int) -> dict[str, Any]:
    with session_scope() as session:
        policy = repo.get_policy(session, policy_id)
    if policy is None:
        raise HTTPException(status_code=404, detail="policy not found")
    return _policy_view(policy)


@app.post("/api/policies", status_code=201)
def create_policy(
    body: PolicyCreate,
    runner: Annotated[CustodianRunner | None, Depends(get_custodian_runner)] = None,
) -> dict[str, Any]:
    """Validate the spec, then persist. ``422`` if invalid (no row), ``409`` on dup name."""
    _require_valid_spec(body.spec, runner)
    try:
        with session_scope() as session:
            created = repo.create_policy(
                session,
                name=body.name,
                resource_type=body.resource_type,
                spec=body.spec,
                description=body.description,
                source=body.source,
            )
    except IntegrityError as exc:
        raise HTTPException(status_code=409, detail="policy name already exists") from exc
    return _policy_view(created)


@app.put("/api/policies/{policy_id}")
def update_policy(
    policy_id: int,
    body: PolicyUpdate,
    runner: Annotated[CustodianRunner | None, Depends(get_custodian_runner)] = None,
) -> dict[str, Any]:
    """Apply a partial update (re-validating ``spec`` when supplied). ``404``/``409``."""
    if body.spec is not None:
        _require_valid_spec(body.spec, runner)
    try:
        with session_scope() as session:
            updated = repo.update_policy(
                session,
                policy_id,
                name=body.name,
                resource_type=body.resource_type,
                spec=body.spec,
                description=body.description,
            )
    except IntegrityError as exc:
        raise HTTPException(status_code=409, detail="policy name already exists") from exc
    if updated is None:
        raise HTTPException(status_code=404, detail="policy not found")
    return _policy_view(updated)


@app.delete("/api/policies/{policy_id}")
def delete_policy(policy_id: int) -> dict[str, Any]:
    with session_scope() as session:
        ok = repo.delete_policy(session, policy_id)
    if not ok:
        raise HTTPException(status_code=404, detail="policy not found")
    return {"id": policy_id, "deleted": True}


@app.post("/api/policies/{policy_id}/enabled")
def set_policy_enabled(policy_id: int, enabled: bool = True) -> dict[str, Any]:
    with session_scope() as session:
        updated = repo.set_policy_enabled(session, policy_id, enabled)
    if updated is None:
        raise HTTPException(status_code=404, detail="policy not found")
    return _policy_view(updated)


@app.get("/api/policies/{policy_id}/versions")
def list_policy_versions(policy_id: int) -> list[dict[str, Any]]:
    """List a policy's version history newest-first. ``404`` when the policy is missing."""
    with session_scope() as session:
        versions = repo.list_versions(session, policy_id)
    if versions is None:
        raise HTTPException(status_code=404, detail="policy not found")
    return versions


@app.get("/api/policies/{policy_id}/versions/diff")
def diff_policy_versions(
    policy_id: int,
    from_version: Annotated[int, Query(ge=1)],
    to_version: Annotated[int, Query(ge=1)],
) -> dict[str, Any]:
    """Field-level diff between two stored versions. ``404`` if the policy/version is absent."""
    with session_scope() as session:
        diff = repo.diff_policy_versions(session, policy_id, from_version, to_version)
    if diff is None:
        raise HTTPException(status_code=404, detail="policy or version not found")
    return diff


@app.post("/api/policies/sync")
def sync_policies(
    runner: Annotated[CustodianRunner | None, Depends(get_custodian_runner)] = None,
) -> dict[str, Any]:
    """Sync policies from the configured Git repo (GitOps). Returns a structured
    report (``ok`` + added/updated/unchanged/skipped counts + errors) and never
    surfaces a ``500`` for a git/validation failure."""
    from ..custodian import gitops

    return gitops.sync_policies(runner=runner)


@app.post("/api/policies/{policy_id}/dryrun")
def dryrun_policy(
    policy_id: int,
    subscription_id: str | None = None,
    runner: Annotated[CustodianRunner | None, Depends(get_custodian_runner)] = None,
) -> dict[str, Any]:
    """Evaluate a stored policy against Azure in **dry-run** — matches, never mutates.

    Looks the policy up by id (``404`` if missing) and, when ``subscription_id`` is
    given, resolves that subscription into a run context (``404`` if unknown);
    otherwise the engine targets the default subscription. Delegates to
    ``engine.run_policy(dry_run=True)`` (mock-backed offline) and returns the matched
    resources — no remediation action is ever executed.
    """
    from ..azure.context import SubscriptionContext
    from ..config import get_settings
    from ..orchestrator import _context_from_record

    context: SubscriptionContext | None = None
    with session_scope() as session:
        policy = repo.get_policy(session, policy_id)
        if policy is None:
            raise HTTPException(status_code=404, detail="policy not found")
        if subscription_id:
            record = repo.get_subscription(session, subscription_id)
            if record is None:
                raise HTTPException(status_code=404, detail="subscription not found")
            context = _context_from_record(record, get_settings().finops_mock)

    result = custodian.run_policy(policy["spec"], subscription=context, dry_run=True, runner=runner)
    resources = result.get("resources") or []
    return {
        "policy_id": policy_id,
        "policy_name": policy["name"],
        "subscription_id": context.subscription_id
        if context
        else get_settings().azure_subscription_id,
        "dry_run": True,
        "matched": result.get("matched", len(resources)),
        "resources": resources,
    }


# --------------------------------------------------------------------------- #
# Policy collections (M2.3) — many-to-many grouping
# --------------------------------------------------------------------------- #
@app.get("/api/collections")
def list_collections() -> list[dict[str, Any]]:
    with session_scope() as session:
        return repo.list_collections(session)


@app.post("/api/collections", status_code=201)
def create_collection(body: CollectionCreate) -> dict[str, Any]:
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    try:
        with session_scope() as session:
            return repo.create_collection(session, name=name, description=body.description)
    except IntegrityError as exc:
        raise HTTPException(status_code=409, detail="collection name already exists") from exc


@app.get("/api/collections/{collection_id}")
def get_collection(collection_id: int) -> dict[str, Any]:
    with session_scope() as session:
        collection = repo.get_collection(session, collection_id)
    if collection is None:
        raise HTTPException(status_code=404, detail="collection not found")
    return collection


@app.delete("/api/collections/{collection_id}")
def delete_collection(collection_id: int) -> dict[str, Any]:
    """Delete a collection (and its memberships) — member policies are preserved."""
    with session_scope() as session:
        ok = repo.delete_collection(session, collection_id)
    if not ok:
        raise HTTPException(status_code=404, detail="collection not found")
    return {"id": collection_id, "deleted": True}


@app.post("/api/collections/{collection_id}/policies/{policy_id}")
def add_policy_to_collection(collection_id: int, policy_id: int) -> dict[str, Any]:
    with session_scope() as session:
        collection = repo.add_policy_to_collection(session, collection_id, policy_id)
    if collection is None:
        raise HTTPException(status_code=404, detail="collection or policy not found")
    return collection


@app.delete("/api/collections/{collection_id}/policies/{policy_id}")
def remove_policy_from_collection(collection_id: int, policy_id: int) -> dict[str, Any]:
    with session_scope() as session:
        collection = repo.remove_policy_from_collection(session, collection_id, policy_id)
    if collection is None:
        raise HTTPException(status_code=404, detail="collection or membership not found")
    return collection


@app.get("/api/summary")
def latest_summary() -> dict[str, Any] | None:
    with session_scope() as session:
        return repo.latest_ai_summary(session)


@app.get("/api/runs/latest")
def latest_run() -> dict[str, Any] | None:
    with session_scope() as session:
        return repo.latest_run(session)


@app.get("/api/runs")
def runs(limit: int = 20) -> list[dict[str, Any]]:
    with session_scope() as session:
        return repo.list_runs(session, limit=limit)


@app.post("/api/runs")
def trigger_run(mock: bool = False, subscription_id: str | None = None) -> dict[str, Any]:
    """Trigger a pipeline run. With no ``subscription_id`` this fans out across
    every enabled subscription; with one it runs just that subscription."""
    from ..orchestrator import run_all_subscriptions, run_one_subscription

    mock_flag = True if mock else None
    if subscription_id:
        result = run_one_subscription(subscription_id, mock=mock_flag)
        if result is None:
            raise HTTPException(status_code=404, detail="subscription not found")
        return result
    return run_all_subscriptions(mock=mock_flag)


# --------------------------------------------------------------------------- #
# Policy executions (M3.3 — pull-mode execution history & drill-down)
# --------------------------------------------------------------------------- #
@app.get("/api/policy-executions")
def policy_executions(
    policy_id: int | None = None,
    subscription_id: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """List executions newest-first, filtered by any combination of the args.

    Blank query-string filters (an "all" dropdown) normalize to no filter.
    """
    with session_scope() as session:
        return repo.list_policy_executions(
            session,
            policy_id=policy_id,
            subscription_id=subscription_id or None,
            status=status or None,
            limit=limit,
        )


@app.get("/api/policy-executions/{execution_id}")
def get_policy_execution(execution_id: str) -> dict[str, Any]:
    with session_scope() as session:
        execution = repo.get_policy_execution(session, execution_id)
    if execution is None:
        raise HTTPException(status_code=404, detail="execution not found")
    return execution


@app.get("/api/policy-executions/{execution_id}/matches")
def policy_execution_matches(execution_id: str) -> list[dict[str, Any]]:
    with session_scope() as session:
        if repo.get_policy_execution(session, execution_id) is None:
            raise HTTPException(status_code=404, detail="execution not found")
        return repo.list_policy_matches(session, execution_id)


@app.get("/api/governance/policy-health")
def policy_health() -> list[dict[str, Any]]:
    """Per-policy compliance & health aggregates (M3.4), across all subscriptions.

    Empty list when no policy has executed yet — never an error.
    """
    with session_scope() as session:
        return repo.policy_health(session)


@app.post("/api/assets/query")
def query_assets(body: AssetQuery) -> list[dict[str, Any]]:
    """Filter AssetDB via an allow-listed, injection-safe query (M4.2).

    An unknown filter column or operator is rejected with ``400`` and never executed;
    all values (including tag values) are bound as parameters.
    """
    with session_scope() as session:
        try:
            return repo.query_assets(session, body)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/assets/{resource_id:path}/relationships")
def asset_relationships(resource_id: str) -> list[dict[str, Any]]:
    """Return an asset's relationship edges — inbound and outbound (M4.3).

    Full Azure resource ids contain slashes, so ``resource_id`` is a path
    parameter; the leading slash is normalized back on so it matches the stored
    (leading-slash) asset id. Unknown ids simply yield an empty list.
    """
    normalized = "/" + resource_id.lstrip("/")
    with session_scope() as session:
        return repo.get_relationships(session, normalized)


# --------------------------------------------------------------------------- #
# Subscriptions (multi-subscription management)
# --------------------------------------------------------------------------- #
class SubscriptionIn(BaseModel):
    subscription_id: str
    display_name: str
    tenant_id: str | None = None
    client_id: str | None = None
    client_secret: str | None = None  # None keeps existing, "" clears, else sets
    enabled: bool = True


@app.get("/api/subscriptions")
def list_subscriptions() -> list[dict[str, Any]]:
    with session_scope() as session:
        return repo.list_subscriptions(session)


@app.post("/api/subscriptions")
def upsert_subscription(body: SubscriptionIn) -> dict[str, Any]:
    sub_id = body.subscription_id.strip()
    if not sub_id or not body.display_name.strip():
        raise HTTPException(status_code=400, detail="subscription_id and display_name are required")
    with session_scope() as session:
        return repo.upsert_subscription(
            session,
            subscription_id=sub_id,
            display_name=body.display_name.strip(),
            tenant_id=body.tenant_id,
            client_id=body.client_id,
            client_secret=body.client_secret,
            enabled=body.enabled,
        )


@app.delete("/api/subscriptions/{subscription_id}")
def delete_subscription(subscription_id: str) -> dict[str, Any]:
    with session_scope() as session:
        ok = repo.delete_subscription(session, subscription_id)
    if not ok:
        raise HTTPException(status_code=404, detail="subscription not found")
    return {"subscription_id": subscription_id, "deleted": True}


@app.post("/api/subscriptions/{subscription_id}/default")
def set_default_subscription(subscription_id: str) -> dict[str, Any]:
    with session_scope() as session:
        ok = repo.set_default_subscription(session, subscription_id)
    if not ok:
        raise HTTPException(status_code=404, detail="subscription not found")
    return {"subscription_id": subscription_id, "is_default": True}


@app.post("/api/subscriptions/{subscription_id}/test")
def test_subscription(subscription_id: str) -> dict[str, Any]:
    """Verify the subscription's credential can reach Azure and see the sub."""
    from ..azure.connectivity import check_connection
    from ..config import get_settings

    with session_scope() as session:
        rec = repo.get_subscription(session, subscription_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="subscription not found")

    credential = None
    if not get_settings().finops_mock and rec.client_id and rec.client_secret:
        from ..auth import credential_for

        credential = credential_for(rec.tenant_id, rec.client_id, rec.client_secret)
    return check_connection(rec.subscription_id, credential=credential)


@app.post("/api/recommendations/{rec_id}/remediate")
def remediate(rec_id: int, dry_run: bool = True, actor: str | None = None) -> dict[str, Any]:
    with session_scope() as session:
        try:
            return remediation.remediate(session, rec_id, actor=actor or "ui", dry_run=dry_run)
        except remediation.NotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except remediation.NotApproved as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/remediation")
def remediation_actions(limit: int = 100) -> list[dict[str, Any]]:
    with session_scope() as session:
        return repo.list_remediation_actions(session, limit=limit)
