"""Asset change history & event metadata (M4.4) — TDD, no live Azure.

Covers the mockable Activity Log collector (parse actor/operation/timestamp; skip
malformed; injected-client live path) and the DB-backed change-history timeline
endpoint (newest-first; unknown asset → empty 200).
"""

from __future__ import annotations

import datetime as dt

from fastapi.testclient import TestClient

from azure_finops.api.main import app
from azure_finops.azure.activitylog import _parse, collect_activity_log
from azure_finops.config import get_settings
from azure_finops.storage import repository as repo
from azure_finops.storage.db import session_scope

_SUB = "00000000-0000-0000-0000-000000000000"
# Stored asset ids are lower-cased; the fixtures/live API return mixed case.
_VM = (
    f"/subscriptions/{_SUB}/resourcegroups/rg-app/providers/"
    "microsoft.compute/virtualmachines/vm-web-01"
)


def _raw(**over: object) -> dict:
    """An Activity Log entry in the raw Azure shape (mixed case, nested fields)."""
    base: dict = {
        "resourceId": _VM.upper(),
        "operationName": {"value": "Microsoft.Compute/virtualMachines/write"},
        "caller": "alice@contoso.com",
        "eventTimestamp": "2026-07-10T14:23:01Z",
        "status": {"value": "Succeeded"},
        "correlationId": "corr-1",
    }
    base.update(over)
    return base


# --------------------------------------------------------------------------- #
# Collector — parsing
# --------------------------------------------------------------------------- #
def test_activitylog_parses_actor_operation() -> None:
    parsed = _parse(_raw(), _SUB, mock=True)
    assert parsed is not None
    assert parsed["actor"] == "alice@contoso.com"
    assert parsed["operation"] == "Microsoft.Compute/virtualMachines/write"
    assert parsed["timestamp"] == "2026-07-10T14:23:01Z"
    assert parsed["resource_id"] == _VM  # lower-cased for a clean join with assets


def test_activitylog_malformed_record_skipped() -> None:
    # Missing resource id / operation / timestamp → skipped (None), never an exception.
    assert _parse(_raw(resourceId=None), _SUB, mock=True) is None
    assert _parse(_raw(operationName={}), _SUB, mock=True) is None
    assert _parse(_raw(operationName="not-a-dict"), _SUB, mock=True) is None
    assert _parse(_raw(eventTimestamp=None), _SUB, mock=True) is None


def test_collect_activity_log_mock_returns_parsed_events() -> None:
    events = collect_activity_log()
    assert events, "expected fixture activity events"
    assert {"resource_id", "subscription_id", "operation", "actor", "timestamp"} <= events[0].keys()
    assert all(e["resource_id"] == e["resource_id"].lower() for e in events)


# --------------------------------------------------------------------------- #
# Collector — injected client (live path, no network)
# --------------------------------------------------------------------------- #
class _Localizable:
    def __init__(self, value: str) -> None:
        self.value = value


class _Event:
    def __init__(self, rid: str, op: str, caller: str, ts: dt.datetime) -> None:
        self.resource_id = rid
        self.operation_name = _Localizable(op)
        self.caller = caller
        self.event_timestamp = ts
        self.status = _Localizable("Succeeded")
        self.correlation_id = "corr-x"


class _ActivityLogs:
    def __init__(self, events: list[_Event]) -> None:
        self._events = events

    def list(self, filter: str):  # noqa: A002 - matches the Azure SDK signature
        return iter(self._events)


class _FakeMonitor:
    def __init__(self, events: list[_Event]) -> None:
        self.activity_logs = _ActivityLogs(events)


def test_activitylog_uses_injected_client(monkeypatch) -> None:
    monkeypatch.setenv("FINOPS_MOCK", "0")
    get_settings.cache_clear()
    ts = dt.datetime(2026, 7, 12, 8, 0, tzinfo=dt.UTC)
    client = _FakeMonitor(
        [_Event(_VM.upper(), "Microsoft.Compute/virtualMachines/write", "bob@contoso.com", ts)]
    )
    events = collect_activity_log(client=client)
    assert len(events) == 1
    assert events[0]["actor"] == "bob@contoso.com"
    assert events[0]["operation"] == "Microsoft.Compute/virtualMachines/write"
    assert events[0]["resource_id"] == _VM
    assert events[0]["timestamp"] == ts.isoformat()
    get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# Persistence + history endpoint
# --------------------------------------------------------------------------- #
def _event(operation: str, actor: str, timestamp: str) -> dict:
    return {
        "resource_id": _VM,
        "subscription_id": _SUB,
        "operation": operation,
        "actor": actor,
        "timestamp": timestamp,
        "status": "Succeeded",
        "correlation_id": f"c-{operation}",
    }


def test_parse_ts_variants() -> None:
    from azure_finops.storage.repository import _parse_ts

    assert _parse_ts("2026-07-10T14:23:01Z").tzinfo is not None  # trailing-Z string
    assert _parse_ts("2026-07-10T14:23:01").tzinfo == dt.UTC  # naive string → assumed UTC
    assert _parse_ts(dt.datetime(2026, 7, 10, 14, 0)).tzinfo == dt.UTC  # naive datetime → UTC
    aware = dt.datetime(2026, 7, 10, tzinfo=dt.UTC)
    assert _parse_ts(aware) is aware  # aware datetime passes through untouched
    assert _parse_ts(None) is None  # empty
    assert _parse_ts("") is None
    assert _parse_ts("not-a-date") is None  # unparseable


def test_record_activity_events_persists_metadata(db) -> None:
    with session_scope() as s:
        assert repo.record_activity_events(s, []) == 0  # nothing to do
        n = repo.record_activity_events(s, [_event("write", "dana", "2026-07-10T14:23:01Z")])
    assert n == 1
    with session_scope() as s:
        rows = repo._rows(
            s,
            "SELECT event_type, data, at FROM asset_events WHERE resource_id=:r",
            r=_VM,
        )
    assert len(rows) == 1
    assert rows[0]["event_type"] == "activity"
    assert rows[0]["data"]["actor"] == "dana"
    assert rows[0]["data"]["operation"] == "write"
    # `at` reflects the real activity timestamp, not the ingestion time.
    at = rows[0]["at"]
    assert (at.year, at.month, at.day) == (2026, 7, 10)


def test_history_endpoint_newest_first(db) -> None:
    with session_scope() as s:
        repo.record_activity_events(
            s,
            [
                _event("write", "alice", "2026-07-10T09:00:00Z"),
                _event("restart", "bob", "2026-07-12T09:00:00Z"),
                _event("deallocate", "carol", "2026-07-11T09:00:00Z"),
            ],
        )
    body = TestClient(app).get(f"/api/assets{_VM}/history").json()
    assert [row["data"]["operation"] for row in body] == ["restart", "deallocate", "write"]
    assert body[0]["data"]["actor"] == "bob"


def test_history_unknown_asset_empty(db) -> None:
    resp = TestClient(app).get("/api/assets/subscriptions/x/does-not-exist/history")
    assert resp.status_code == 200
    assert resp.json() == []
