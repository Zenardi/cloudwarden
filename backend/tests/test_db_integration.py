"""Postgres-backed integration: pipeline, repository, orchestrator, API, approval, cli, scheduler.

Uses the `db` fixture (throwaway PostgreSQL via testcontainers). Skips if Docker
is unavailable.
"""

from __future__ import annotations

import pytest

from cloudwarden.config import get_settings


def test_full_pipeline_and_reads(db) -> None:
    from cloudwarden.orchestrator import run_pipeline
    from cloudwarden.storage import repository as repo
    from cloudwarden.storage.db import session_scope

    counts = run_pipeline(mock=True)["counts"]
    assert counts["resources"] == 7 and counts["cost_rows"] == 360
    # 5 heuristic/idle recs + 4 commitment recs (2 purchase, 1 under-utilized,
    # 1 expiring) from the reservations fixture (M14.1).
    assert counts["recommendations"] == 9 and counts["ai_summary"] == 1
    assert counts["commitments"] == 3 and counts["commitment_coverage"] == 4

    with session_scope() as s:
        assert repo.total_cost(s) > 0
        assert len(repo.cost_by_type(s)) >= 1
        assert len(repo.cost_by_region(s)) == 2
        assert len(repo.cost_by_resource(s, limit=10)) >= 1
        assert len(repo.latest_recommendations(s)) == 9
        assert repo.latest_commitment_coverage(s)
        assert len(repo.list_commitments(s)) == 3
        assert repo.latest_run(s)["status"] == "succeeded"
        assert repo.latest_ai_summary(s)["provider"] == "stub"
        assert len(repo.list_runs(s, limit=5)) == 1
        assert repo.list_remediation_actions(s) == []


def test_orchestrator_records_failure(db, monkeypatch) -> None:
    import cloudwarden.orchestrator as orch
    from cloudwarden.storage import repository as repo
    from cloudwarden.storage.db import session_scope

    def boom(*a, **k):
        raise RuntimeError("collector down")

    # Cost collection is provider-dispatched since M14.11; the seam is _collect_cost.
    monkeypatch.setattr(orch, "_collect_cost", boom)
    with pytest.raises(RuntimeError):
        orch.run_pipeline(mock=True)
    with session_scope() as s:
        assert repo.latest_run(s)["status"] == "failed"


def test_session_rollback(db) -> None:
    from cloudwarden.storage import repository as repo
    from cloudwarden.storage import schema
    from cloudwarden.storage.db import session_scope

    with pytest.raises(RuntimeError):
        with session_scope() as s:
            repo.create_run(
                s,
                run_id="rb",
                subscription_id="x",
                metric_lookback_days=14,
                cost_lookback_days=30,
                mock=True,
            )
            raise RuntimeError("fail before commit")
    with session_scope() as s:
        assert s.get(schema.Run, "rb") is None


def test_api_endpoints(db) -> None:
    from fastapi.testclient import TestClient

    from cloudwarden.api.main import app
    from cloudwarden.orchestrator import run_pipeline

    run_pipeline(mock=True)
    c = TestClient(app)

    assert c.get("/health").json()["status"] == "ok"
    assert c.get("/api/costs/summary").json()["total"] > 0
    assert len(c.get("/api/costs/by-type").json()) >= 1
    assert len(c.get("/api/costs/by-region").json()) == 2
    assert len(c.get("/api/costs/by-resource").json()) >= 1
    recs = c.get("/api/recommendations").json()
    assert len(recs) == 9  # 5 heuristic + 4 commitment (M14.1)
    assert c.get("/api/summary").json()["provider"] == "stub"
    assert c.get("/api/runs/latest").json()["status"] == "succeeded"
    assert len(c.get("/api/runs").json()) >= 1

    # Decisions work on any recommendation; remediation targets a remediable
    # (non-commitment) rec — commitment recs are advisory, not auto-remediable.
    heuristic = [r for r in recs if r["category"] != "commitment"]
    rid = heuristic[0]["id"]
    assert (
        c.post(f"/api/recommendations/{rid}/decision", json={"decision": "approve"}).json()[
            "status"
        ]
        == "approved"
    )
    assert (
        c.post(
            f"/api/recommendations/{heuristic[1]['id']}/decision", json={"decision": "reject"}
        ).json()["status"]
        == "rejected"
    )
    assert (
        c.post(f"/api/recommendations/{rid}/decision", json={"decision": "bad"}).status_code == 400
    )
    assert (
        c.post("/api/recommendations/999999/decision", json={"decision": "approve"}).status_code
        == 404
    )

    assert (
        c.post(f"/api/recommendations/{rid}/remediate?dry_run=true").json()["status"] == "dry_run"
    )
    assert c.post(f"/api/recommendations/{heuristic[2]['id']}/remediate").status_code == 409
    assert c.post("/api/recommendations/999999/remediate").status_code == 404
    assert len(c.get("/api/remediation").json()) >= 1
    fanout = c.post("/api/runs", params={"mock": True}).json()
    assert fanout["subscriptions"] >= 1 and "run_id" in fanout["runs"][0]


def test_api_trigger_run_default_param(db) -> None:
    from fastapi.testclient import TestClient

    from cloudwarden.api.main import app

    c = TestClient(app)
    body = c.post("/api/runs").json()
    assert body["subscriptions"] >= 1 and "run_id" in body["runs"][0]


def test_approval_flows(db, monkeypatch) -> None:
    from cloudwarden.orchestrator import run_pipeline
    from cloudwarden.remediation import approval
    from cloudwarden.storage import repository as repo
    from cloudwarden.storage.db import session_scope

    run_pipeline(mock=True)
    with session_scope() as s:
        recs = repo.latest_recommendations(s)
    by_res = {r["resource_id"].split("/")[-1]: r for r in recs}
    batch, web = by_res["vm-batch-02"], by_res["vm-web-01"]

    with session_scope() as s, pytest.raises(approval.NotFound):
        approval.remediate(s, 10_000_000)
    with session_scope() as s, pytest.raises(approval.NotApproved):
        approval.remediate(s, batch["id"])

    monkeypatch.setenv("REMEDIATION_ENABLED", "true")
    monkeypatch.setenv("ALLOWED_RESOURCE_GROUPS", "rg-batch")
    get_settings.cache_clear()
    with session_scope() as s:
        repo.decide_recommendation(s, batch["id"], "approved", "t")
    with session_scope() as s:
        assert approval.remediate(s, batch["id"], actor="t", dry_run=False)["status"] == "executed"
    with session_scope() as s:
        st = repo._rows(s, "SELECT status FROM recommendations WHERE id=:i", i=batch["id"])
    assert st[0]["status"] == "executed"

    monkeypatch.setenv("ALLOWED_RESOURCE_GROUPS", "rg-none")
    get_settings.cache_clear()
    with session_scope() as s:
        repo.decide_recommendation(s, web["id"], "approved", "t")
    with session_scope() as s:
        assert approval.remediate(s, web["id"], actor="t", dry_run=False)["status"] == "blocked"
    get_settings.cache_clear()


def test_approval_live_branch(db, monkeypatch) -> None:
    from cloudwarden.orchestrator import run_pipeline
    from cloudwarden.remediation import approval, executor
    from cloudwarden.storage import repository as repo
    from cloudwarden.storage.db import session_scope

    run_pipeline(mock=True)
    with session_scope() as s:
        recs = repo.latest_recommendations(s)
    batch = next(r for r in recs if r["resource_id"].endswith("vm-batch-02"))

    monkeypatch.setenv("FINOPS_MOCK", "0")
    monkeypatch.setenv("REMEDIATION_ENABLED", "true")
    monkeypatch.setenv("ALLOWED_RESOURCE_GROUPS", "rg-batch")
    get_settings.cache_clear()
    monkeypatch.setattr("cloudwarden.auth.write_credential", lambda: object())
    monkeypatch.setattr(executor, "execute", lambda *a, **k: {"executed": True, "message": "done"})
    with session_scope() as s:
        repo.decide_recommendation(s, batch["id"], "approved", "t")
    with session_scope() as s:
        assert approval.remediate(s, batch["id"], actor="t", dry_run=False)["status"] == "executed"

    def raise_exec(*a, **k):
        raise RuntimeError("azure boom")

    monkeypatch.setattr(executor, "execute", raise_exec)
    with session_scope() as s:
        repo.decide_recommendation(s, batch["id"], "approved", "t")
    with session_scope() as s:
        res = approval.remediate(s, batch["id"], actor="t", dry_run=False)
    assert res["status"] == "failed" and "boom" in (res["error"] or "")
    get_settings.cache_clear()


def test_cli_initdb_and_run(db) -> None:
    from typer.testing import CliRunner

    from cloudwarden.cli import app

    runner = CliRunner()
    assert runner.invoke(app, ["initdb"]).exit_code == 0
    result = runner.invoke(app, ["run", "--mock"])
    assert result.exit_code == 0 and "run complete" in result.stdout


def test_cli_api_and_scheduler_commands(monkeypatch) -> None:
    from typer.testing import CliRunner

    import cloudwarden.cli as cli

    called: dict[str, bool] = {}
    monkeypatch.setattr("uvicorn.run", lambda *a, **k: called.setdefault("api", True))
    runner = CliRunner()
    assert runner.invoke(cli.app, ["api"]).exit_code == 0
    assert called.get("api")

    monkeypatch.setattr(
        "cloudwarden.scheduler.run_scheduler", lambda: called.setdefault("sched", True)
    )
    assert runner.invoke(cli.app, ["scheduler"]).exit_code == 0
    assert called.get("sched")


def test_scheduler_safe_run(monkeypatch) -> None:
    import cloudwarden.scheduler as sched

    ran: list[str] = []
    monkeypatch.setattr(sched, "run_all_subscriptions", lambda: ran.append("ok"))
    sched._safe_run()
    assert ran == ["ok"]

    def boom():
        raise RuntimeError("x")

    monkeypatch.setattr(sched, "run_all_subscriptions", boom)
    sched._safe_run()  # must swallow the error


def test_run_scheduler_loop(monkeypatch) -> None:
    import cloudwarden.scheduler as sched

    events: list[str] = []
    monkeypatch.setattr(sched, "run_all_subscriptions", lambda: events.append("run"))

    class _FakeScheduler:
        def __init__(self, timezone=None):
            pass

        def add_job(self, *a, **k):
            events.append("add_job")

        def start(self):
            raise KeyboardInterrupt()

    monkeypatch.setattr(sched, "BlockingScheduler", _FakeScheduler)
    sched.run_scheduler()
    assert "run" in events and "add_job" in events


def test_api_lifespan_handles_initdb_error(monkeypatch) -> None:
    from fastapi.testclient import TestClient

    import cloudwarden.api.main as apimain

    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(apimain, "init_db", boom)
    with TestClient(apimain.app):
        pass
