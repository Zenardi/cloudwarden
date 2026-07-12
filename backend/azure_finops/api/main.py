"""FastAPI application exposing cost, recommendations, runs and health.

Grafana reads the SQL views directly from Postgres; this API serves the Next.js
UI and on-demand pipeline triggers. It is intentionally thin — all queries live
in the repository.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from ..custodian import engine as custodian
from ..custodian.engine import CustodianRunner
from ..models import ValidateRequest, ValidateResult
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
