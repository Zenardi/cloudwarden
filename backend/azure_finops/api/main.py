"""FastAPI application exposing cost, recommendations, runs and health.

Grafana reads the SQL views directly from Postgres; this API serves the Next.js
UI and on-demand pipeline triggers. It is intentionally thin — all queries live
in the repository.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from ..resilience import REGISTRY
from ..storage import repository as repo
from ..storage.db import init_db, session_scope

logger = logging.getLogger("azure_finops.api")


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        init_db()
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
def trigger_run(mock: bool = False) -> dict[str, Any]:
    from ..orchestrator import run_pipeline

    return run_pipeline(mock=True if mock else None)
