"""M14.2 — budgets & threshold alerting. Tests written FIRST (TDD).

Three layers, each asserting one behaviour:

* **Pure logic** (no DB): period keys/bounds, percent-of-budget, threshold crossing.
* **Evaluation + dedupe + notification** (``db`` fixture): a crossing fires exactly
  one notification per period/threshold, is idempotent across re-evaluations, and
  resets on a new period. Spend and the notification transport are injected so the
  logic is exercised in isolation from cost SQL and the network.
* **Scope resolution + API** (``db`` fixture): actual spend is summed per scope, and
  every budget mutation is RBAC-guarded and audited.
"""

from __future__ import annotations

import datetime as dt

import pytest

_TODAY = dt.date(2026, 7, 23)  # a Thursday in Q3; deterministic period math


# --------------------------------------------------------------------------- #
# Pure logic — no database
# --------------------------------------------------------------------------- #
def test_period_key_monthly_and_quarterly() -> None:
    from cloudwarden.analysis.budgets import period_key

    assert period_key("monthly", _TODAY) == "2026-07"
    assert period_key("quarterly", _TODAY) == "2026-Q3"


def test_period_bounds_monthly() -> None:
    from cloudwarden.analysis.budgets import period_bounds

    start, end = period_bounds("monthly", _TODAY)
    assert start == dt.date(2026, 7, 1)
    assert end == dt.date(2026, 7, 31)


def test_period_bounds_quarterly() -> None:
    from cloudwarden.analysis.budgets import period_bounds

    start, end = period_bounds("quarterly", _TODAY)
    assert start == dt.date(2026, 7, 1)
    assert end == dt.date(2026, 9, 30)


def test_actual_pct_guards_zero_amount() -> None:
    from cloudwarden.analysis.budgets import actual_pct

    assert actual_pct(500.0, 1000.0) == 50.0
    assert actual_pct(500.0, 0.0) == 0.0  # never divide by zero


def test_crossed_rules_returns_all_below_actual() -> None:
    from cloudwarden.analysis.budgets import crossed_rules, parse_thresholds

    rules = parse_thresholds([{"pct": 50}, {"pct": 80}, {"pct": 100}])
    crossed = crossed_rules(rules, actual_pct=85.0)
    assert [r.pct for r in crossed] == [50.0, 80.0]


def test_crossed_rules_forecast_skipped_when_forecast_absent() -> None:
    from cloudwarden.analysis.budgets import crossed_rules, parse_thresholds

    rules = parse_thresholds([{"pct": 100, "basis": "forecast"}])
    # A forecast rule is never evaluated against actual spend (guarded on M14.4).
    assert crossed_rules(rules, actual_pct=200.0, forecast_pct=None) == []


# --------------------------------------------------------------------------- #
# DB-backed helpers
# --------------------------------------------------------------------------- #
def _channel(s):
    from cloudwarden.storage import repository as repo

    return repo.create_notification_channel(
        s, name="ops-webhook", transport="webhook", target="https://hooks.example/ops"
    )


def _budget(s, *, channel_id=None, thresholds=None, amount=1000.0, period="monthly", **kw):
    from cloudwarden.storage import repository as repo

    return repo.create_budget(
        s,
        name=kw.pop("name", "prod-monthly"),
        scope_type=kw.pop("scope_type", "subscription"),
        scope_value=kw.pop("scope_value", "sub-1"),
        period=period,
        amount=amount,
        thresholds=thresholds if thresholds is not None else [{"pct": 80}, {"pct": 100}],
        channel_id=channel_id,
        **kw,
    )


class _Recorder:
    """A dispatch spy: records every budget notification, makes no network call."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, session, *, budget, context, template_id):
        self.calls.append({"budget": budget, "context": context, "template_id": template_id})
        return {"dispatched": True}


# --------------------------------------------------------------------------- #
# Repository CRUD
# --------------------------------------------------------------------------- #
def test_budget_crud_roundtrip(db) -> None:
    from cloudwarden.storage import repository as repo
    from cloudwarden.storage.db import session_scope

    with session_scope() as s:
        created = repo.create_budget(
            s,
            name="prod-monthly",
            scope_type="subscription",
            scope_value="sub-1",
            period="monthly",
            amount=1000.0,
            thresholds=[{"pct": 100}, {"pct": 80}],  # unsorted on the way in
        )
        bid = created["id"]

    assert created["name"] == "prod-monthly"
    assert created["amount"] == 1000.0
    # Stored normalised: basis defaulted, pct floated, sorted ascending.
    assert created["thresholds"] == [
        {"pct": 80.0, "basis": "actual"},
        {"pct": 100.0, "basis": "actual"},
    ]

    with session_scope() as s:
        got = repo.get_budget(s, bid)
        assert got["scope_value"] == "sub-1"
        assert any(b["id"] == bid for b in repo.list_budgets(s))

    with session_scope() as s:
        updated = repo.update_budget(s, bid, {"amount": 2000.0, "enabled": False})
        assert updated["amount"] == 2000.0
        assert updated["enabled"] is False

    with session_scope() as s:
        assert repo.list_budgets(s, enabled_only=True) == []  # disabled → filtered out
        assert repo.delete_budget(s, bid) is True
        assert repo.get_budget(s, bid) is None
        assert repo.delete_budget(s, bid) is False  # idempotent


def test_create_budget_rejects_bad_threshold_basis(db) -> None:
    from cloudwarden.storage import repository as repo
    from cloudwarden.storage.db import session_scope

    with session_scope() as s:
        with pytest.raises(ValueError):
            repo.create_budget(
                s, name="bad", amount=100.0, thresholds=[{"pct": 50, "basis": "guess"}]
            )


# --------------------------------------------------------------------------- #
# Scope resolution
# --------------------------------------------------------------------------- #
def test_budget_scope_resolution(db) -> None:
    from cloudwarden import models as m
    from cloudwarden.storage import repository as repo
    from cloudwarden.storage.db import session_scope

    rows = [
        m.CostRow(
            usage_date=dt.date(2026, 7, 10), resource_id="/a", subscription_id="sub-1", cost=300.0
        ),
        m.CostRow(
            usage_date=dt.date(2026, 7, 12), resource_id="/b", subscription_id="sub-1", cost=200.0
        ),
        m.CostRow(
            usage_date=dt.date(2026, 7, 12), resource_id="/c", subscription_id="sub-2", cost=999.0
        ),
        # Outside the window — must not be counted.
        m.CostRow(
            usage_date=dt.date(2026, 6, 30), resource_id="/d", subscription_id="sub-1", cost=50.0
        ),
    ]
    with session_scope() as s:
        repo.upsert_cost_snapshots(s, rows)

    with session_scope() as s:
        spend = repo.budget_spend(
            s,
            scope_type="subscription",
            scope_value="sub-1",
            start=dt.date(2026, 7, 1),
            end=dt.date(2026, 7, 31),
        )
        other = repo.budget_spend(
            s,
            scope_type="subscription",
            scope_value="nope",
            start=dt.date(2026, 7, 1),
            end=dt.date(2026, 7, 31),
        )

    assert spend == 500.0  # only sub-1, only within July
    assert other == 0.0


# --------------------------------------------------------------------------- #
# Evaluation, notification-trigger, dedupe
# --------------------------------------------------------------------------- #
def test_actual_below_threshold_no_alert(db) -> None:
    from cloudwarden.analysis import budgets
    from cloudwarden.storage import repository as repo
    from cloudwarden.storage.db import session_scope

    rec = _Recorder()
    with session_scope() as s:
        ch = _channel(s)
        b = _budget(s, channel_id=ch["id"], amount=1000.0)  # thresholds 80/100
        result = budgets.evaluate_budgets(
            s, on=_TODAY, spend_fn=lambda *a, **k: 300.0, dispatch_fn=rec
        )
        events = repo.budget_events_for_period(s, b["id"], "2026-07")

    assert rec.calls == []  # below 80% → silent
    assert result["notifications_sent"] == 0
    assert events == []


def test_threshold_crossed_fires_single_notification(db) -> None:
    from cloudwarden.analysis import budgets
    from cloudwarden.storage import repository as repo
    from cloudwarden.storage.db import session_scope

    rec = _Recorder()
    with session_scope() as s:
        ch = _channel(s)
        b = _budget(s, channel_id=ch["id"], amount=1000.0)  # thresholds 80/100
        result = budgets.evaluate_budgets(
            s,
            on=_TODAY,
            spend_fn=lambda *a, **k: 900.0,
            dispatch_fn=rec,  # 90%
        )
        events = repo.budget_events_for_period(s, b["id"], "2026-07")

    assert len(rec.calls) == 1  # crossed 80% → exactly one notification
    assert rec.calls[0]["context"]["threshold_pct"] == 80.0
    assert rec.calls[0]["context"]["actual_pct"] == 90.0
    assert result["notifications_sent"] == 1
    assert [e["threshold_pct"] for e in events] == [80.0]


def test_multiple_thresholds_crossed_fire_single_notification(db) -> None:
    """Anti-storm: a jump past several thresholds notifies once (the highest)."""
    from cloudwarden.analysis import budgets
    from cloudwarden.storage import repository as repo
    from cloudwarden.storage.db import session_scope

    rec = _Recorder()
    with session_scope() as s:
        ch = _channel(s)
        b = _budget(
            s,
            channel_id=ch["id"],
            amount=1000.0,
            thresholds=[{"pct": 50}, {"pct": 80}, {"pct": 100}],
        )
        budgets.evaluate_budgets(
            s, on=_TODAY, spend_fn=lambda *a, **k: 900.0, dispatch_fn=rec
        )  # 90%
        events = repo.budget_events_for_period(s, b["id"], "2026-07")

    assert len(rec.calls) == 1  # no storm
    assert rec.calls[0]["context"]["threshold_pct"] == 80.0  # highest newly-crossed
    # Both crossings recorded so neither re-fires later.
    assert sorted(e["threshold_pct"] for e in events) == [50.0, 80.0]


def test_second_evaluation_same_period_does_not_refire(db) -> None:
    from cloudwarden.analysis import budgets
    from cloudwarden.storage import repository as repo
    from cloudwarden.storage.db import session_scope

    rec = _Recorder()
    with session_scope() as s:
        ch = _channel(s)
        b = _budget(s, channel_id=ch["id"], amount=1000.0)
        budgets.evaluate_budgets(s, on=_TODAY, spend_fn=lambda *a, **k: 900.0, dispatch_fn=rec)
        budgets.evaluate_budgets(s, on=_TODAY, spend_fn=lambda *a, **k: 900.0, dispatch_fn=rec)
        events = repo.budget_events_for_period(s, b["id"], "2026-07")

    assert len(rec.calls) == 1  # crossed once, second eval is a no-op
    assert len(events) == 1


def test_new_period_resets_thresholds(db) -> None:
    from cloudwarden.analysis import budgets
    from cloudwarden.storage.db import session_scope

    rec = _Recorder()
    with session_scope() as s:
        ch = _channel(s)
        _budget(s, channel_id=ch["id"], amount=1000.0)
        budgets.evaluate_budgets(s, on=_TODAY, spend_fn=lambda *a, **k: 900.0, dispatch_fn=rec)
        # Next month, same overage → fires again.
        budgets.evaluate_budgets(
            s, on=dt.date(2026, 8, 15), spend_fn=lambda *a, **k: 900.0, dispatch_fn=rec
        )

    assert len(rec.calls) == 2
    assert {c["context"]["period_key"] for c in rec.calls} == {"2026-07", "2026-08"}


def test_forecasted_threshold_rule(db) -> None:
    """A forecast-basis threshold fires off projected (not actual) spend (M14.4)."""
    from cloudwarden.analysis import budgets
    from cloudwarden.storage.db import session_scope

    rec = _Recorder()
    with session_scope() as s:
        ch = _channel(s)
        _budget(
            s,
            channel_id=ch["id"],
            amount=1000.0,
            thresholds=[{"pct": 100, "basis": "forecast"}],
        )
        budgets.evaluate_budgets(
            s,
            on=_TODAY,
            spend_fn=lambda *a, **k: 500.0,  # 50% actual → no actual crossing
            forecast_fn=lambda *a, **k: 1100.0,  # projected 110% → forecast crossing
            dispatch_fn=rec,
        )

    assert len(rec.calls) == 1
    assert rec.calls[0]["context"]["basis"] == "forecast"
    assert rec.calls[0]["context"]["threshold_pct"] == 100.0


def test_disabled_budget_not_evaluated(db) -> None:
    from cloudwarden.analysis import budgets
    from cloudwarden.storage.db import session_scope

    rec = _Recorder()
    with session_scope() as s:
        ch = _channel(s)
        _budget(s, channel_id=ch["id"], amount=1000.0, enabled=False)
        result = budgets.evaluate_budgets(
            s, on=_TODAY, spend_fn=lambda *a, **k: 5000.0, dispatch_fn=rec
        )

    assert rec.calls == []
    assert result["budgets_evaluated"] == 0


def test_budget_without_channel_records_event_but_stays_silent(db) -> None:
    from cloudwarden.analysis import budgets
    from cloudwarden.storage import repository as repo
    from cloudwarden.storage.db import session_scope

    rec = _Recorder()
    with session_scope() as s:
        b = _budget(s, channel_id=None, amount=1000.0)  # no channel
        result = budgets.evaluate_budgets(
            s, on=_TODAY, spend_fn=lambda *a, **k: 900.0, dispatch_fn=rec
        )
        events = repo.budget_events_for_period(s, b["id"], "2026-07")

    assert rec.calls == []  # nowhere to send
    assert result["notifications_sent"] == 0
    assert [e["threshold_pct"] for e in events] == [80.0]  # crossing still recorded


def test_record_budget_event_is_idempotent(db) -> None:
    from cloudwarden.storage import repository as repo
    from cloudwarden.storage.db import session_scope

    with session_scope() as s:
        b = _budget(s)
        first = repo.record_budget_event(
            s,
            budget_id=b["id"],
            period_key="2026-07",
            threshold_pct=80.0,
            basis="actual",
            amount=900.0,
            budget_amount=1000.0,
            actual_pct=90.0,
        )
        dup = repo.record_budget_event(
            s,
            budget_id=b["id"],
            period_key="2026-07",
            threshold_pct=80.0,
            basis="actual",
            amount=950.0,
            budget_amount=1000.0,
            actual_pct=95.0,
        )
        events = repo.budget_events_for_period(s, b["id"], "2026-07")

    assert first is not None
    assert dup is None  # conflict → not re-inserted
    assert len(events) == 1


def test_evaluate_dispatch_failure_is_best_effort(db) -> None:
    """A transport error records the event but never breaks evaluation."""
    from cloudwarden.analysis import budgets
    from cloudwarden.storage import repository as repo
    from cloudwarden.storage.db import session_scope

    def _boom(session, *, budget, context, template_id):
        raise RuntimeError("smtp down")

    with session_scope() as s:
        ch = _channel(s)
        b = _budget(s, channel_id=ch["id"], amount=1000.0)
        result = budgets.evaluate_budgets(
            s, on=_TODAY, spend_fn=lambda *a, **k: 900.0, dispatch_fn=_boom
        )
        events = repo.budget_events_for_period(s, b["id"], "2026-07")

    assert result["notifications_sent"] == 0  # send failed, but no crash
    assert [e["threshold_pct"] for e in events] == [80.0]  # crossing persisted


def test_evaluate_measures_real_cost_when_spend_not_injected(db) -> None:
    """The default spend source sums cost_snapshots for the budget's scope/period."""
    from cloudwarden import models as m
    from cloudwarden.analysis import budgets
    from cloudwarden.storage import repository as repo
    from cloudwarden.storage.db import session_scope

    rec = _Recorder()
    with session_scope() as s:
        ch = _channel(s)
        _budget(s, channel_id=ch["id"], amount=1000.0)  # scope sub-1, thresholds 80/100
        repo.upsert_cost_snapshots(
            s,
            [
                m.CostRow(
                    usage_date=dt.date(2026, 7, 15),
                    resource_id="/a",
                    subscription_id="sub-1",
                    cost=900.0,
                )
            ],
        )
        result = budgets.evaluate_budgets(s, on=_TODAY, dispatch_fn=rec)  # real spend + forecast

    assert len(rec.calls) == 1  # 90% of budget → crosses 80
    assert rec.calls[0]["context"]["threshold_pct"] == 80.0
    assert result["notifications_sent"] == 1


# --------------------------------------------------------------------------- #
# Repository — spend scopes, event history, template reuse
# --------------------------------------------------------------------------- #
def test_update_budget_normalizes_thresholds_ignores_unknown_and_missing(db) -> None:
    from cloudwarden.storage import repository as repo
    from cloudwarden.storage.db import session_scope

    with session_scope() as s:
        b = _budget(s)
        updated = repo.update_budget(s, b["id"], {"thresholds": [{"pct": 90}], "bogus": 1})
        assert updated["thresholds"] == [{"pct": 90.0, "basis": "actual"}]  # normalised
        assert repo.update_budget(s, 999999, {"amount": 5.0}) is None  # missing budget


def test_budget_spend_account_group_and_whole_tenant(db) -> None:
    from cloudwarden import models as m
    from cloudwarden.storage import repository as repo
    from cloudwarden.storage import schema
    from cloudwarden.storage.db import session_scope

    start, end = dt.date(2026, 7, 1), dt.date(2026, 7, 31)
    with session_scope() as s:
        s.add(schema.Subscription(subscription_id="sub-1", display_name="one"))
        s.add(schema.Subscription(subscription_id="sub-2", display_name="two"))
        s.flush()
        grp = schema.AccountGroup(name="team-a")
        s.add(grp)
        s.flush()
        s.add(schema.AccountGroupMember(group_id=grp.id, subscription_id="sub-1"))
        s.flush()
        repo.upsert_cost_snapshots(
            s,
            [
                m.CostRow(
                    usage_date=dt.date(2026, 7, 10),
                    resource_id="/a",
                    subscription_id="sub-1",
                    cost=100.0,
                ),
                m.CostRow(
                    usage_date=dt.date(2026, 7, 10),
                    resource_id="/b",
                    subscription_id="sub-2",
                    cost=40.0,
                ),
            ],
        )
        grp_spend = repo.budget_spend(
            s, scope_type="account_group", scope_value="team-a", start=start, end=end
        )
        tag_spend = repo.budget_spend(
            s, scope_type="tag", scope_value="sub-1", start=start, end=end
        )
        all_spend = repo.budget_spend(s, scope_type="tag", scope_value=None, start=start, end=end)

    assert grp_spend == 100.0  # only sub-1 is in the group
    assert tag_spend == 100.0  # tag scope degrades to a subscription match (M14.5 pending)
    assert all_spend == 140.0  # no scope value → whole tenant


def test_last_budget_event_and_template_reuse(db) -> None:
    from cloudwarden.storage import repository as repo
    from cloudwarden.storage.db import session_scope

    with session_scope() as s:
        b = _budget(s)
        assert repo.last_budget_event(s, b["id"]) is None
        for pct, spend, ap in [(80.0, 900.0, 90.0), (100.0, 1100.0, 110.0)]:
            repo.record_budget_event(
                s,
                budget_id=b["id"],
                period_key="2026-07",
                threshold_pct=pct,
                basis="actual",
                amount=spend,
                budget_amount=1000.0,
                actual_pct=ap,
            )
        last = repo.last_budget_event(s, b["id"], period_key="2026-07")
        assert last["threshold_pct"] == 100.0  # most recent
        assert repo.ensure_budget_template(s) == repo.ensure_budget_template(s)  # created once


# --------------------------------------------------------------------------- #
# Notification context + real dispatch seam
# --------------------------------------------------------------------------- #
def test_build_budget_context_exposes_template_fields() -> None:
    from cloudwarden.notify import service

    ctx = service.build_budget_context(
        budget={
            "name": "prod",
            "period": "monthly",
            "currency": "USD",
            "amount": 1000.0,
            "scope_type": "subscription",
            "scope_value": "sub-1",
        },
        period_key="2026-07",
        spend=900.0,
        actual_pct=90.0,
        threshold_pct=80.0,
        basis="actual",
    )
    assert ctx["budget_name"] == "prod"
    assert ctx["threshold_pct"] == 80.0
    assert ctx["actual_pct"] == 90.0
    assert ctx["spend"] == 900.0
    assert ctx["period_key"] == "2026-07"
    # Rendered through the sandbox without error.
    body = service.render(service.DEFAULT_BUDGET_BODY, ctx)
    assert "prod" in body


def test_dispatch_for_budget_sends_through_transport(db) -> None:
    from cloudwarden.notify import service
    from cloudwarden.notify.dispatch import dispatch_for_budget
    from cloudwarden.storage import repository as repo
    from cloudwarden.storage.db import session_scope

    class _Spy:
        def __init__(self) -> None:
            self.sent: list[dict] = []

        def send(self, *, target, subject, body, config):
            self.sent.append({"target": target, "subject": subject, "body": body})
            return {"ok": True}

    spy = _Spy()
    with session_scope() as s:
        ch = _channel(s)
        tid = repo.ensure_budget_template(s)
        b = repo.create_budget(
            s, name="prod", amount=1000.0, scope_value="sub-1", channel_id=ch["id"]
        )
        ctx = service.build_budget_context(
            budget=b,
            period_key="2026-07",
            spend=900.0,
            actual_pct=90.0,
            threshold_pct=80.0,
            basis="actual",
        )
        result = dispatch_for_budget(
            s, budget=b, context=ctx, template_id=tid, transport_factory=lambda kind: spy
        )

    assert result is not None
    assert result["dispatched"] is True
    assert len(spy.sent) == 1
    assert "prod" in spy.sent[0]["body"]


def test_dispatch_for_budget_without_channel_returns_none(db) -> None:
    from cloudwarden.notify.dispatch import dispatch_for_budget
    from cloudwarden.storage import repository as repo
    from cloudwarden.storage.db import session_scope

    with session_scope() as s:
        tid = repo.ensure_budget_template(s)
        b = repo.create_budget(s, name="prod", amount=1000.0, scope_value="sub-1")
        result = dispatch_for_budget(s, budget=b, context={}, template_id=tid)

    assert result is None


def test_dispatch_for_budget_deleted_channel_returns_none(db) -> None:
    from cloudwarden.notify.dispatch import dispatch_for_budget
    from cloudwarden.storage import repository as repo
    from cloudwarden.storage.db import session_scope

    with session_scope() as s:
        tid = repo.ensure_budget_template(s)
        # channel_id references a channel that no longer exists → no send.
        result = dispatch_for_budget(s, budget={"channel_id": 999999}, context={}, template_id=tid)

    assert result is None


# --------------------------------------------------------------------------- #
# API — RBAC + audit
# --------------------------------------------------------------------------- #
def test_mutations_require_permission_and_audit(db, monkeypatch) -> None:
    from fastapi.testclient import TestClient
    from sqlalchemy import text

    from cloudwarden.api.main import app
    from cloudwarden.authz import rbac
    from cloudwarden.config import get_settings
    from cloudwarden.storage import repository as repo
    from cloudwarden.storage.db import session_scope

    monkeypatch.setenv("RBAC_ENABLED", "1")
    get_settings.cache_clear()
    with session_scope() as s:
        rbac.seed_default_roles(s)
        repo.assign_role(s, principal="ed", role_name="editor")
        repo.assign_role(s, principal="vv", role_name="viewer")
    client = TestClient(app)
    body = {
        "name": "prod",
        "amount": 1000,
        "scope_type": "subscription",
        "scope_value": "sub-1",
        "thresholds": [{"pct": 80}],
    }

    # create: anon 401, viewer 403, editor 201
    assert client.post("/api/budgets", json=body).status_code == 401
    assert client.post("/api/budgets", json=body, headers={"X-Principal": "vv"}).status_code == 403
    created = client.post("/api/budgets", json=body, headers={"X-Principal": "ed"})
    assert created.status_code == 201
    bid = created.json()["id"]

    # update (PATCH) + delete are editor-only
    patched = client.patch(
        f"/api/budgets/{bid}", json={"amount": 2000}, headers={"X-Principal": "ed"}
    )
    assert patched.status_code == 200
    assert patched.json()["amount"] == 2000.0
    assert (
        client.patch(
            f"/api/budgets/{bid}", json={"amount": 3}, headers={"X-Principal": "vv"}
        ).status_code
        == 403
    )
    assert client.delete(f"/api/budgets/{bid}", headers={"X-Principal": "vv"}).status_code == 403
    assert client.delete(f"/api/budgets/{bid}", headers={"X-Principal": "ed"}).status_code == 200

    # every mutation wrote an audit row; the read did not.
    with session_scope() as s:
        actions = [
            r[0]
            for r in s.execute(
                text("SELECT action FROM audit_log WHERE target_type='budget' ORDER BY id")
            )
        ]
    assert actions == ["budget.create", "budget.update", "budget.delete"]
    get_settings.cache_clear()


def test_budget_read_requires_permission(db, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from cloudwarden.api.main import app
    from cloudwarden.authz import rbac
    from cloudwarden.config import get_settings
    from cloudwarden.storage import repository as repo
    from cloudwarden.storage.db import session_scope

    monkeypatch.setenv("RBAC_ENABLED", "1")
    get_settings.cache_clear()
    with session_scope() as s:
        rbac.seed_default_roles(s)
        repo.assign_role(s, principal="ed", role_name="editor")
    client = TestClient(app)

    assert client.get("/api/budgets").status_code == 401
    assert client.get("/api/budgets", headers={"X-Principal": "ed"}).status_code == 200
    get_settings.cache_clear()


def test_budget_status_endpoint_reports_spend_and_thresholds(db) -> None:
    from fastapi.testclient import TestClient

    from cloudwarden import models as m
    from cloudwarden.api.main import app
    from cloudwarden.storage import repository as repo
    from cloudwarden.storage.db import session_scope

    with session_scope() as s:
        b = repo.create_budget(
            s,
            name="prod",
            amount=1000.0,
            scope_type="subscription",
            scope_value="sub-1",
            thresholds=[{"pct": 80}, {"pct": 100}],
        )
        bid = b["id"]
        # Seed at the real "today" so the current-period window always contains it
        # (the status endpoint measures spend as of date.today()).
        repo.upsert_cost_snapshots(
            s,
            [
                m.CostRow(
                    usage_date=dt.date.today(),
                    resource_id="/a",
                    subscription_id="sub-1",
                    cost=850.0,
                )
            ],
        )
    client = TestClient(app)  # RBAC off by default

    resp = client.get(f"/api/budgets/{bid}/status")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["budget"]["id"] == bid
    assert payload["spend"] == 850.0
    assert payload["actual_pct"] == 85.0
    assert payload["crossed"] == [80.0]  # 85% ≥ 80, < 100


def test_budget_status_missing_returns_404(db) -> None:
    from fastapi.testclient import TestClient

    from cloudwarden.api.main import app

    client = TestClient(app)
    assert client.get("/api/budgets/99999/status").status_code == 404


def test_budget_api_crud_and_error_paths(db) -> None:
    from fastapi.testclient import TestClient

    from cloudwarden.api.main import app

    client = TestClient(app)  # RBAC off by default

    created = client.post(
        "/api/budgets", json={"name": "prod", "amount": 1000, "scope_value": "sub-1"}
    )
    assert created.status_code == 201
    bid = created.json()["id"]

    assert client.get(f"/api/budgets/{bid}").json()["name"] == "prod"
    assert client.get("/api/budgets/424242").status_code == 404

    # duplicate name → 409 on both create and update
    dup = client.post("/api/budgets", json={"name": "prod", "amount": 5, "scope_value": "x"})
    assert dup.status_code == 409
    other = client.post("/api/budgets", json={"name": "other", "amount": 5, "scope_value": "x"})
    assert other.status_code == 201
    assert client.patch(f"/api/budgets/{bid}", json={"name": "other"}).status_code == 409

    # invalid threshold basis → 422 on both create and update
    bad = {"name": "bad", "amount": 5, "thresholds": [{"pct": 50, "basis": "nope"}]}
    assert client.post("/api/budgets", json=bad).status_code == 422
    assert (
        client.patch(
            f"/api/budgets/{bid}", json={"thresholds": [{"pct": 50, "basis": "nope"}]}
        ).status_code
        == 422
    )

    # mutations on a missing budget → 404
    assert client.patch("/api/budgets/424242", json={"amount": 1}).status_code == 404
    assert client.delete("/api/budgets/424242").status_code == 404
