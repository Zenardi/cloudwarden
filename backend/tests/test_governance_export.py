"""Governance reporting & export (M9.4): streaming CSV/JSON + scheduled report.

Written test-first (TDD). DB-backed (the ``db`` fixture) against seeded
``PolicyExecution`` rows. ``GET /api/governance/export?format=csv|json`` streams the
per-execution governance evidence (policy, subscription, status, matches, timing) —
paginated via ``repo.iter_governance_export`` so it never loads the whole dataset
into memory. An invalid ``format`` is a 400. ``reporting.generate_report`` /
``write_report`` and the scheduler hook produce the same content on a cadence.
"""

from __future__ import annotations

import csv
import io

import pytest
from fastapi.testclient import TestClient

from azure_finops import reporting
from azure_finops.api.main import app
from azure_finops.config import get_settings
from azure_finops.storage import repository as repo
from azure_finops.storage.db import session_scope


def _make_policy(session, name: str = "p1") -> int:
    return repo.create_policy(
        session,
        name=name,
        resource_type="azure.vm",
        spec={"policies": [{"name": name, "resource": "azure.vm"}]},
    )["id"]


def _seed_exec(
    session, *, eid, pid, sub="sub-a", status="succeeded", matched=0, binding_id=None
) -> None:
    repo.create_policy_execution(
        session, execution_id=eid, policy_id=pid, subscription_id=sub, binding_id=binding_id
    )
    repo.finish_policy_execution(session, eid, status=status, resources_matched=matched)


def _boom(*args, **kwargs):
    raise RuntimeError("report boom")


# --------------------------------------------------------------------------- #
# CSV / JSON endpoint
# --------------------------------------------------------------------------- #
def test_export_csv_has_header(db) -> None:
    with session_scope() as s:
        pid = _make_policy(s)
        _seed_exec(s, eid="e1", pid=pid, status="succeeded", matched=2)

    resp = TestClient(app).get("/api/governance/export?format=csv")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")

    rows = list(csv.DictReader(io.StringIO(resp.text)))
    assert rows[0]["execution_id"] == "e1"
    assert rows[0]["policy_name"] == "p1"
    assert rows[0]["status"] == "succeeded"
    assert rows[0]["resources_matched"] == "2"


def test_export_json_matches_csv_rows(db) -> None:
    with session_scope() as s:
        pid = _make_policy(s)
        _seed_exec(s, eid="e1", pid=pid, status="succeeded", matched=1)
        _seed_exec(s, eid="e2", pid=pid, status="failed", matched=0)

    client = TestClient(app)
    csv_rows = list(
        csv.DictReader(io.StringIO(client.get("/api/governance/export?format=csv").text))
    )
    json_rows = client.get("/api/governance/export?format=json").json()

    assert isinstance(json_rows, list)
    assert len(csv_rows) == len(json_rows) == 2
    assert {r["execution_id"] for r in csv_rows} == {r["execution_id"] for r in json_rows}
    # Same status per execution across both encodings.
    assert {r["execution_id"]: r["status"] for r in csv_rows} == {
        r["execution_id"]: r["status"] for r in json_rows
    }


def test_export_invalid_format_400(db) -> None:
    assert TestClient(app).get("/api/governance/export?format=xml").status_code == 400


def test_export_empty_dataset(db) -> None:
    client = TestClient(app)
    csv_resp = client.get("/api/governance/export?format=csv")
    assert csv_resp.status_code == 200
    lines = csv_resp.text.strip().splitlines()
    assert len(lines) == 1 and lines[0].startswith("execution_id")  # header only

    json_resp = client.get("/api/governance/export?format=json")
    assert json_resp.status_code == 200
    assert json_resp.json() == []


# --------------------------------------------------------------------------- #
# Pagination (memory-safety) — the cursor read
# --------------------------------------------------------------------------- #
def test_export_streams_paginated(db) -> None:
    with session_scope() as s:
        pid = _make_policy(s)
        for i in range(5):
            _seed_exec(s, eid=f"e{i}", pid=pid, matched=i)

    # batch_size=2 over 5 rows: a naive single LIMIT-2 query would yield 2 — getting
    # all 5 (in order) proves the pagination loop fetched every page.
    with session_scope() as s:
        rows = list(repo.iter_governance_export(s, batch_size=2))
    assert [r["execution_id"] for r in rows] == [f"e{i}" for i in range(5)]


def test_stream_export_rejects_bad_format(db) -> None:
    with session_scope() as s:
        with pytest.raises(ValueError):
            reporting.stream_export(s, "xml")


# --------------------------------------------------------------------------- #
# Scheduled report (optional) — same content, produced on a cadence
# --------------------------------------------------------------------------- #
def test_generate_report_produces_csv(db) -> None:
    with session_scope() as s:
        pid = _make_policy(s)
        _seed_exec(s, eid="e1", pid=pid, matched=1)

    content = reporting.generate_report("csv")
    assert content.splitlines()[0].startswith("execution_id")
    assert "e1" in content


def test_scheduled_report_writes_file(db, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    with session_scope() as s:
        pid = _make_policy(s)
        _seed_exec(s, eid="e1", pid=pid, matched=1)

    import azure_finops.scheduler as sched

    sched._safe_run_governance_report()
    files = list(tmp_path.glob("governance-report-*.csv"))
    assert len(files) == 1
    assert "e1" in files[0].read_text()


def test_safe_run_governance_report_swallows_errors(monkeypatch) -> None:
    import azure_finops.scheduler as sched

    monkeypatch.setattr("azure_finops.reporting.write_report", _boom)
    sched._safe_run_governance_report()  # must not raise


def test_schedule_governance_report_respects_flag(monkeypatch) -> None:
    import azure_finops.scheduler as sched

    class _Fake:
        def __init__(self) -> None:
            self.ids: list[str] = []

        def add_job(self, func, trigger=None, *, seconds=None, id=None, **kwargs) -> None:
            self.ids.append(id)

    monkeypatch.setenv("GOVERNANCE_REPORT_ENABLED", "false")
    get_settings.cache_clear()
    fs = _Fake()
    assert sched._schedule_governance_report(fs) is False
    assert fs.ids == []

    monkeypatch.setenv("GOVERNANCE_REPORT_ENABLED", "true")
    get_settings.cache_clear()
    fs2 = _Fake()
    assert sched._schedule_governance_report(fs2) is True
    assert fs2.ids == ["finops-governance-report"]
