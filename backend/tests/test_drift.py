"""M14.7 — configuration drift detection. Tests written FIRST (TDD).

The AssetDB already stores each resource's full ``config`` (JSONB) and change history
(``asset_events``). Drift detection captures a desired-state **baseline** per resource,
diffs live config against it each run, and surfaces **added / removed / changed** field
paths — enriched with the change events that caused them. Layers:

* **Pure logic** (no DB): normalize config (drop volatile/noise fields), a stable hash,
  a recursive structural diff producing classified dotted field paths, and event
  attribution. Identical (or only-volatile-changed) config yields no drift.
* **Repository / detection** (``db`` fixture): baseline capture is versioned; findings
  are idempotent (unique on resource + baseline version + change set); the run captures
  baselines on first sight and flags drift after a change, attributing the events.
* **API** (``db`` fixture): ``GET /api/drift`` lists findings; ``POST /api/drift/baseline``
  re-baselines — clearing the finding, RBAC-guarded and audited.
"""

from __future__ import annotations


# --------------------------------------------------------------------------- #
# Pure logic — normalize + diff + classify (no DB)
# --------------------------------------------------------------------------- #
def test_changed_field_detected_and_classified() -> None:
    from cloudwarden.custodian.drift import diff_config

    changes = diff_config({"sku": "Standard_LRS"}, {"sku": "Premium_LRS"})
    assert len(changes) == 1
    assert changes[0].path == "sku"
    assert changes[0].kind == "changed"
    assert changes[0].old == "Standard_LRS"
    assert changes[0].new == "Premium_LRS"


def test_added_and_removed_fields_classified() -> None:
    from cloudwarden.custodian.drift import diff_config

    changes = {c.path: c for c in diff_config({"a": 1, "b": 2}, {"a": 1, "c": 3})}
    assert changes["b"].kind == "removed"
    assert changes["b"].old == 2
    assert changes["c"].kind == "added"
    assert changes["c"].new == 3


def test_identical_config_no_drift() -> None:
    from cloudwarden.custodian.drift import diff_config

    config = {"sku": "S1", "properties": {"tls": "1.2", "public": False}}
    assert diff_config(config, dict(config)) == []


def test_nested_diff_produces_dotted_paths() -> None:
    from cloudwarden.custodian.drift import diff_config

    changes = diff_config(
        {"properties": {"networkAcls": {"defaultAction": "Deny"}}},
        {"properties": {"networkAcls": {"defaultAction": "Allow"}}},
    )
    assert len(changes) == 1
    assert changes[0].path == "properties.networkAcls.defaultAction"
    assert changes[0].kind == "changed"


def test_volatile_fields_excluded() -> None:
    from cloudwarden.custodian.drift import diff_config

    # Only volatile/noise fields differ → the resource has NOT drifted.
    baseline = {"sku": "S1", "etag": "aaa", "provisioningState": "Succeeded", "last_seen": "t0"}
    current = {"sku": "S1", "etag": "zzz", "provisioningState": "Updating", "last_seen": "t9"}
    assert diff_config(baseline, current) == []


def test_normalize_config_strips_volatile_recursively() -> None:
    from cloudwarden.custodian.drift import normalize_config

    normalized = normalize_config(
        {"sku": "S1", "etag": "x", "properties": {"etag": "y", "tls": "1.2"}}
    )
    assert normalized == {"sku": "S1", "properties": {"tls": "1.2"}}


def test_normalize_config_recurses_into_lists() -> None:
    from cloudwarden.custodian.drift import normalize_config

    # Lists are walked so volatile fields inside list items are dropped too.
    normalized = normalize_config({"rules": [{"port": 443, "etag": "z"}, {"port": 80}]})
    assert normalized == {"rules": [{"port": 443}, {"port": 80}]}


def test_config_hash_ignores_volatile() -> None:
    from cloudwarden.custodian.drift import config_hash

    a = config_hash({"sku": "S1", "etag": "aaa"})
    b = config_hash({"sku": "S1", "etag": "different"})
    c = config_hash({"sku": "S2", "etag": "aaa"})
    assert a == b  # volatile-only difference → same hash
    assert a != c  # a real difference → different hash


def test_attribute_events_returns_recent_changes() -> None:
    from cloudwarden.custodian.drift import attribute_events

    events = [
        {
            "id": 3,
            "event_type": "activity",
            "at": "2026-07-20T10:00:00Z",
            "data": {"caller": "eve"},
        },
        {
            "id": 2,
            "event_type": "activity",
            "at": "2026-07-19T10:00:00Z",
            "data": {"caller": "bob"},
        },
        {"id": 1, "event_type": "created", "at": "2026-07-01T10:00:00Z", "data": {}},
    ]
    attributed = attribute_events(events, top=2)
    # The initial 'created' lifecycle event is not a *change*; recent activity wins.
    assert [e["id"] for e in attributed] == [3, 2]


# --------------------------------------------------------------------------- #
# Repository + detection
# --------------------------------------------------------------------------- #
def _seed_asset(s, resource_id, config, *, provider="azure"):
    from cloudwarden.storage import schema

    s.add(
        schema.Asset(
            resource_id=resource_id,
            subscription_id="sub-a",
            provider=provider,
            type="Microsoft.Storage/storageAccounts",
            name=resource_id.rsplit("/", 1)[-1],
            config=config,
        )
    )
    s.flush()


def _set_asset_config(s, resource_id, config):
    from cloudwarden.storage import schema

    rec = s.get(schema.Asset, resource_id)
    rec.config = config
    s.flush()


def test_upsert_drift_baseline_versions_on_change(db) -> None:
    from cloudwarden.custodian import drift
    from cloudwarden.storage.db import session_scope

    rid = "/subs/rg/sa1"
    with session_scope() as s:
        first = drift.capture_baseline(s, resource_id=rid, config={"sku": "S1"})
    with session_scope() as s:
        # Same config → no version bump (idempotent capture).
        same = drift.capture_baseline(s, resource_id=rid, config={"sku": "S1"})
    with session_scope() as s:
        # Different config → a new baseline version.
        bumped = drift.capture_baseline(s, resource_id=rid, config={"sku": "S2"})

    assert first["version"] == 1
    assert same["version"] == 1
    assert bumped["version"] == 2


def test_record_drift_finding_idempotent(db) -> None:
    from cloudwarden.storage import repository as repo
    from cloudwarden.storage.db import session_scope

    changes = [{"path": "sku", "kind": "changed", "old": "S1", "new": "S2"}]
    args = dict(resource_id="/subs/rg/sa1", baseline_version=1, changes=changes, events=[])
    with session_scope() as s:
        _row1, inserted1 = repo.record_drift_finding(s, **args)
    with session_scope() as s:
        _row2, inserted2 = repo.record_drift_finding(s, **args)
    with session_scope() as s:
        findings = repo.list_drift_findings(s)

    assert inserted1 is True
    assert inserted2 is False  # same resource + baseline version + change set → not duplicated
    assert len(findings) == 1


def test_detect_captures_baseline_first_run_then_flags_change(db) -> None:
    from cloudwarden.custodian import drift
    from cloudwarden.storage import repository as repo
    from cloudwarden.storage.db import session_scope

    rid = "/subs/rg/sa1"
    with session_scope() as s:
        _seed_asset(s, rid, {"sku": "S1", "properties": {"tls": "1.2"}})
        first = drift.detect_drift(s)  # first sight → baseline captured, no drift
    with session_scope() as s:
        assert repo.list_drift_findings(s) == []
    with session_scope() as s:
        # The resource's config changes (e.g. TLS downgraded).
        _set_asset_config(s, rid, {"sku": "S1", "properties": {"tls": "1.0"}})
        second = drift.detect_drift(s)
    with session_scope() as s:
        findings = repo.list_drift_findings(s)

    assert first["baselines_captured"] == 1
    assert second["findings"] == 1
    assert findings[0]["resource_id"] == rid
    paths = {c["path"] for c in findings[0]["changes"]}
    assert "properties.tls" in paths


def test_identical_config_produces_no_finding(db) -> None:
    from cloudwarden.custodian import drift
    from cloudwarden.storage import repository as repo
    from cloudwarden.storage.db import session_scope

    rid = "/subs/rg/sa1"
    with session_scope() as s:
        _seed_asset(s, rid, {"sku": "S1"})
        drift.detect_drift(s)  # baseline captured
    with session_scope() as s:
        drift.detect_drift(s)  # config unchanged → no drift
    with session_scope() as s:
        assert repo.list_drift_findings(s) == []


def test_change_event_attributed(db) -> None:
    from cloudwarden.custodian import drift
    from cloudwarden.storage import repository as repo
    from cloudwarden.storage.db import session_scope

    rid = "/subs/rg/sa1"
    with session_scope() as s:
        _seed_asset(s, rid, {"sku": "S1"})
        drift.detect_drift(s)  # baseline
    with session_scope() as s:
        _set_asset_config(s, rid, {"sku": "S2"})
        # An Activity-Log change event that plausibly caused the drift.
        repo.append_asset_event(
            s,
            resource_id=rid,
            subscription_id="sub-a",
            event_type="activity",
            data={"caller": "mallory@contoso.com", "operation": "storageAccounts/write"},
        )
        drift.detect_drift(s)
    with session_scope() as s:
        findings = repo.list_drift_findings(s)

    assert len(findings) == 1
    events = findings[0]["events"]
    assert any(e.get("data", {}).get("caller") == "mallory@contoso.com" for e in events)


class _Recorder:
    """A dispatch spy: records every drift notification, makes no network call."""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[dict] = []
        self._fail = fail

    def __call__(self, session, *, context, template_id, channel_name, **_kw):
        self.calls.append({"context": context, "channel_name": channel_name})
        if self._fail:
            raise RuntimeError("transport exploded")
        return {"dispatched": True}


def test_detect_notifies_once_on_new_drift(db) -> None:
    from cloudwarden.custodian import drift
    from cloudwarden.storage import repository as repo
    from cloudwarden.storage.db import session_scope

    rid = "/subs/rg/sa1"
    spy = _Recorder()
    with session_scope() as s:
        _seed_asset(s, rid, {"sku": "S1"})
        drift.detect_drift(s)  # baseline
    with session_scope() as s:
        _set_asset_config(s, rid, {"sku": "S2"})
        first = drift.detect_drift(s, dispatch_fn=spy, channel_name="drift-alerts")
    with session_scope() as s:
        # Re-detecting the same drift updates the finding but never re-notifies.
        second = drift.detect_drift(s, dispatch_fn=spy, channel_name="drift-alerts")
    with session_scope() as s:
        finding = repo.list_drift_findings(s)[0]

    assert first["notifications_sent"] == 1
    assert second["notifications_sent"] == 0
    assert len(spy.calls) == 1  # fired exactly once
    assert finding["notified"] is True


def test_detect_default_dispatch_unknown_channel_is_silent(db) -> None:
    from cloudwarden.custodian import drift
    from cloudwarden.storage import repository as repo
    from cloudwarden.storage.db import session_scope

    rid = "/subs/rg/sa1"
    with session_scope() as s:
        _seed_asset(s, rid, {"sku": "S1"})
        drift.detect_drift(s)
    with session_scope() as s:
        _set_asset_config(s, rid, {"sku": "S2"})
        # No dispatch_fn injected → the default dispatch resolves a channel by name; an
        # unknown channel records the drift silently (no notification, no error).
        summary = drift.detect_drift(s, channel_name="ghost-channel")
    with session_scope() as s:
        assert repo.list_drift_findings(s)[0]["notified"] is False
    assert summary["findings"] == 1
    assert summary["notifications_sent"] == 0


def test_detect_swallows_dispatch_failure(db) -> None:
    from cloudwarden.custodian import drift
    from cloudwarden.storage import repository as repo
    from cloudwarden.storage.db import session_scope

    rid = "/subs/rg/sa1"
    with session_scope() as s:
        _seed_asset(s, rid, {"sku": "S1"})
        drift.detect_drift(s)
    with session_scope() as s:
        _set_asset_config(s, rid, {"sku": "S2"})
        # A transport failure must never break detection — the finding is still recorded.
        summary = drift.detect_drift(
            s, dispatch_fn=_Recorder(fail=True), channel_name="drift-alerts"
        )
    with session_scope() as s:
        findings = repo.list_drift_findings(s)

    assert summary["findings"] == 1
    assert summary["notifications_sent"] == 0
    assert findings[0]["notified"] is False  # dispatch failed → not marked notified


# --------------------------------------------------------------------------- #
# Notification context
# --------------------------------------------------------------------------- #
def test_build_drift_context_renders() -> None:
    from cloudwarden.notify import service

    ctx = service.build_drift_context(
        resource_id="/subs/rg/sa1",
        provider="azure",
        baseline_version=2,
        changes=[{"path": "properties.tls", "kind": "changed", "old": "1.2", "new": "1.0"}],
        events=[{"data": {"caller": "eve"}}],
    )
    assert ctx["resource_id"] == "/subs/rg/sa1"
    assert ctx["change_count"] == 1
    body = service.render(service.DEFAULT_DRIFT_BODY, ctx)
    assert "properties.tls" in body


def test_dispatch_for_drift_sends_through_transport(db) -> None:
    from cloudwarden.notify import service
    from cloudwarden.notify.dispatch import dispatch_for_drift
    from cloudwarden.storage import repository as repo
    from cloudwarden.storage.db import session_scope

    class _Spy:
        def __init__(self) -> None:
            self.sent: list[str] = []

        def send(self, *, target, subject, body, config):
            self.sent.append(body)
            return {"ok": True}

    spy = _Spy()
    with session_scope() as s:
        repo.create_notification_channel(
            s, name="drift-alerts", transport="webhook", target="https://hooks.example/drift"
        )
        tid = repo.ensure_drift_template(s)
        ctx = service.build_drift_context(
            resource_id="/subs/rg/sa1",
            provider="azure",
            baseline_version=1,
            changes=[{"path": "sku", "kind": "changed", "old": "S1", "new": "S2"}],
            events=[],
        )
        result = dispatch_for_drift(
            s,
            context=ctx,
            template_id=tid,
            channel_name="drift-alerts",
            transport_factory=lambda kind: spy,
        )

    assert result is not None
    assert result["dispatched"] is True
    assert len(spy.sent) == 1
    assert "sku" in spy.sent[0]


def test_dispatch_for_drift_without_channel_returns_none(db) -> None:
    from cloudwarden.notify.dispatch import dispatch_for_drift
    from cloudwarden.storage import repository as repo
    from cloudwarden.storage.db import session_scope

    with session_scope() as s:
        tid = repo.ensure_drift_template(s)
        # Empty channel name → silent (records the drift, dispatches nothing).
        assert dispatch_for_drift(s, context={}, template_id=tid, channel_name="") is None


# --------------------------------------------------------------------------- #
# API — GET /api/drift + POST /api/drift/baseline (RBAC + audit)
# --------------------------------------------------------------------------- #
def test_drift_endpoint_lists(db) -> None:
    from fastapi.testclient import TestClient

    from cloudwarden.api.main import app
    from cloudwarden.storage import repository as repo
    from cloudwarden.storage.db import session_scope

    with session_scope() as s:
        repo.record_drift_finding(
            s,
            resource_id="/subs/rg/sa1",
            baseline_version=1,
            changes=[{"path": "sku", "kind": "changed", "old": "S1", "new": "S2"}],
            events=[],
        )
    client = TestClient(app)

    resp = client.get("/api/drift")
    assert resp.status_code == 200
    assert len(resp.json()["findings"]) == 1


def test_rebaseline_clears_finding_and_audits(db) -> None:
    from fastapi.testclient import TestClient

    from cloudwarden.api.main import app
    from cloudwarden.storage import repository as repo
    from cloudwarden.storage import schema
    from cloudwarden.storage.db import session_scope

    rid = "/subs/rg/sa1"
    with session_scope() as s:
        _seed_asset(s, rid, {"sku": "S2"})  # current (drifted) config
        baseline, _ = repo.upsert_drift_baseline(
            s, resource_id=rid, config={"sku": "S1"}, config_hash="h1"
        )
        repo.record_drift_finding(
            s,
            resource_id=rid,
            baseline_version=baseline["version"],
            changes=[{"path": "sku", "kind": "changed", "old": "S1", "new": "S2"}],
            events=[],
        )
    client = TestClient(app)

    resp = client.post("/api/drift/baseline", json={"resource_id": rid})
    assert resp.status_code == 200

    with session_scope() as s:
        # The open finding is resolved (accepted via re-baseline)...
        assert repo.list_drift_findings(s, status="open") == []
        # ...and the action is recorded in the audit trail.
        audits = s.query(schema.AuditLog).filter(schema.AuditLog.action == "drift:baseline").all()
    assert len(audits) == 1


def test_drift_baseline_requires_permission(db, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from cloudwarden.api.main import app
    from cloudwarden.authz import rbac
    from cloudwarden.config import get_settings
    from cloudwarden.storage import repository as repo
    from cloudwarden.storage.db import session_scope

    rid = "/subs/rg/sa1"
    monkeypatch.setenv("RBAC_ENABLED", "1")
    get_settings.cache_clear()
    with session_scope() as s:
        _seed_asset(s, rid, {"sku": "S1"})
        rbac.seed_default_roles(s)
        repo.assign_role(s, principal="ed", role_name="editor")
    client = TestClient(app)

    assert client.post("/api/drift/baseline", json={"resource_id": rid}).status_code == 401
    ok = client.post(
        "/api/drift/baseline", json={"resource_id": rid}, headers={"X-Principal": "ed"}
    )
    assert ok.status_code == 200
    get_settings.cache_clear()
