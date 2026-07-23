"""M14.3 — cost anomaly detection. Tests written FIRST (TDD).

Three layers, each asserting one behaviour:

* **Pure logic** (no DB): robust median/MAD, weekday deseasonalization, the
  deviation score + severity buckets, and contributor attribution. Deterministic
  seeded series — a spike is flagged, a steady series is not, thin history is
  suppressed, and a weekly-seasonal series is not flagged on its in-pattern peak.
* **Repository** (``db`` fixture): the daily-by-scope series read, per-child
  contributor read, and the idempotent ``upsert_cost_anomaly`` (unique on
  scope+date; ``inserted`` distinguishes the first sighting from a re-detect).
* **Detection + notification + API** (``db`` fixture): a seeded spike in
  ``cost_snapshots`` is detected with contributors, notifies **once** per new
  anomaly (idempotent on scope+date), swallows a transport failure, and the read
  endpoint is RBAC-guarded. Transports are mocked; nothing touches the network.
"""

from __future__ import annotations

import datetime as dt

# A Monday, so day+61 lands on a Saturday for the seasonality series below.
_MON = dt.date(2026, 1, 5)


def _series(start: dt.date, values: list[float]) -> list[tuple[dt.date, float]]:
    """A daily ``(date, cost)`` series starting at ``start`` (one point per day)."""
    return [(start + dt.timedelta(days=i), float(v)) for i, v in enumerate(values)]


# --------------------------------------------------------------------------- #
# Pure logic — no database
# --------------------------------------------------------------------------- #
def test_robust_stats_median_and_mad() -> None:
    from cloudwarden.analysis.anomaly import robust_stats

    center, mad = robust_stats([10.0, 12.0, 14.0, 100.0])
    assert center == 13.0  # median of the four, robust to the 100 outlier
    assert mad == 2.0  # median(|x - 13|) = median(3, 1, 1, 87) = 2


def test_severity_buckets() -> None:
    from cloudwarden.analysis.anomaly import severity_for

    assert severity_for(3.6) == "low"
    assert severity_for(6.0) == "medium"
    assert severity_for(9.0) == "high"
    assert severity_for(20.0) == "critical"


def test_weekday_factors_deseasonalize_weekly_pattern() -> None:
    from cloudwarden.analysis.anomaly import weekday_factors

    # Weekdays ~100, weekends ~300 over 8 weeks.
    baseline = [(d, 300.0 if d.weekday() >= 5 else 100.0) for d, _ in _series(_MON, [0] * 56)]
    factors = weekday_factors(baseline)
    assert round(factors[5], 3) == 3.0  # Saturday runs 3x the overall median
    assert round(factors[0], 3) == 1.0  # Monday is the baseline weekday


def test_spike_detected_with_severity() -> None:
    from cloudwarden.analysis.anomaly import score_series

    values = [100.0 + (i % 5) for i in range(30)]  # steady ~100-104
    values.append(500.0)  # the spike, on day 30
    series = _series(_MON, values)
    on = _MON + dt.timedelta(days=30)

    dev = score_series(series, on=on, min_history=14, threshold=3.5)
    assert dev is not None
    assert dev.actual == 500.0
    assert 90.0 <= dev.expected <= 110.0  # baseline centre, not the spike
    assert dev.score >= 12.0
    assert dev.severity == "critical"


def test_steady_series_no_anomaly() -> None:
    from cloudwarden.analysis.anomaly import score_series

    values = [100.0 + (i % 5) for i in range(30)]
    values.append(102.0)  # in-range last day — noise, not a spike
    series = _series(_MON, values)
    on = _MON + dt.timedelta(days=30)

    assert score_series(series, on=on, min_history=14, threshold=3.5) is None


def test_sparse_history_suppressed() -> None:
    from cloudwarden.analysis.anomaly import score_series

    # Only 5 baseline days — below the min-history gate — even with a huge last day.
    values = [100.0, 100.0, 100.0, 100.0, 100.0, 1000.0]
    series = _series(_MON, values)
    on = _MON + dt.timedelta(days=5)

    assert score_series(series, on=on, min_history=14, threshold=3.5) is None


def test_weekly_seasonality_not_flagged() -> None:
    from cloudwarden.analysis.anomaly import score_series

    # 62 days of a weekly pattern; the target day (day 61) is an in-pattern weekend peak.
    series = [(d, 300.0 if d.weekday() >= 5 else 100.0) for d, _ in _series(_MON, [0] * 62)]
    on = _MON + dt.timedelta(days=61)
    assert on.weekday() >= 5  # arrange sanity: target is a weekend

    # Deseasonalized, an in-pattern 300 on a weekend is expected — not an anomaly.
    assert score_series(series, on=on, min_history=14, threshold=3.5) is None


def test_seasonal_peak_above_pattern_is_flagged() -> None:
    from cloudwarden.analysis.anomaly import score_series

    series = [(d, 300.0 if d.weekday() >= 5 else 100.0) for d, _ in _series(_MON, [0] * 62)]
    on = _MON + dt.timedelta(days=61)
    series[-1] = (on, 900.0)  # a weekend running 3x its own seasonal expectation

    dev = score_series(series, on=on, min_history=14, threshold=3.5)
    assert dev is not None
    assert dev.actual == 900.0
    assert round(dev.expected) == 300.0  # reseasonalized weekend expectation
    assert dev.score >= 5.0


def test_seasonal_off_uses_flat_baseline() -> None:
    from cloudwarden.analysis.anomaly import score_series

    series = [(d, 300.0 if d.weekday() >= 5 else 100.0) for d, _ in _series(_MON, [0] * 62)]
    on = _MON + dt.timedelta(days=61)  # weekend, in-pattern 300

    # With seasonality OFF, the weekend 300 towers over the flat ~100 median → flagged.
    dev = score_series(series, on=on, min_history=14, threshold=3.5, seasonal=False)
    assert dev is not None


def test_missing_target_day_returns_none() -> None:
    from cloudwarden.analysis.anomaly import score_series

    series = _series(_MON, [100.0] * 20)
    on = _MON + dt.timedelta(days=40)  # no data for this day
    assert score_series(series, on=on, min_history=14, threshold=3.5) is None


def test_contributor_attribution() -> None:
    from cloudwarden.analysis.anomaly import attribute_contributors

    children = [
        {"child": "/r1", "actual": 600.0, "baseline": 100.0},  # delta 500
        {"child": "/r2", "actual": 50.0, "baseline": 45.0},  # delta 5
        {"child": "/r3", "actual": 10.0, "baseline": 40.0},  # delta -30 (a saver)
    ]
    ranked = attribute_contributors(children, top=2)
    assert [c["child"] for c in ranked] == ["/r1", "/r2"]  # by delta, descending
    assert ranked[0]["delta"] == 500.0
    assert round(ranked[0]["share"], 3) == round(500.0 / 505.0, 3)  # of the positive delta


def test_contributor_attribution_empty() -> None:
    from cloudwarden.analysis.anomaly import attribute_contributors

    assert attribute_contributors([]) == []


# --------------------------------------------------------------------------- #
# Repository
# --------------------------------------------------------------------------- #
def _seed_series(s, *, subscription_id="sub-anom", on, days=30, spike=None):
    """Seed ``cost_snapshots``: ``days`` steady days (r1=60/r2=40) + a target day.

    When ``spike`` is set, the target day concentrates it in ``/r1``; otherwise the
    target day continues the steady pattern.
    """
    from cloudwarden import models as m
    from cloudwarden.storage import repository as repo

    rows = []
    for i in range(days):
        d = on - dt.timedelta(days=days - i)
        rows.append(
            m.CostRow(
                usage_date=d,
                resource_id="/r1",
                subscription_id=subscription_id,
                service_name="Compute",
                resource_type="vm",
                cost=60.0,
            )
        )
        rows.append(
            m.CostRow(
                usage_date=d,
                resource_id="/r2",
                subscription_id=subscription_id,
                service_name="Storage",
                resource_type="disk",
                cost=40.0,
            )
        )
    r1_cost = float(spike) if spike is not None else 60.0
    rows.append(
        m.CostRow(
            usage_date=on,
            resource_id="/r1",
            subscription_id=subscription_id,
            service_name="Compute",
            resource_type="vm",
            cost=r1_cost,
        )
    )
    rows.append(
        m.CostRow(
            usage_date=on,
            resource_id="/r2",
            subscription_id=subscription_id,
            service_name="Storage",
            resource_type="disk",
            cost=40.0,
        )
    )
    repo.upsert_cost_snapshots(s, rows)


def test_cost_daily_by_scope_groups_by_scope_value(db) -> None:
    from cloudwarden.storage import repository as repo
    from cloudwarden.storage.db import session_scope

    on = dt.date(2026, 7, 22)
    with session_scope() as s:
        _seed_series(s, on=on, days=10)
        rows = repo.cost_daily_by_scope(
            s, scope_type="subscription", start=on - dt.timedelta(days=45), end=on
        )

    by_value = {r["scope_value"] for r in rows}
    assert by_value == {"sub-anom"}
    # 11 distinct days (10 baseline + target), each summing r1+r2 = 100.
    totals = {r["usage_date"]: r["cost"] for r in rows}
    assert totals[on] == 100.0
    assert len(totals) == 11


def test_cost_scope_children_returns_actual_and_baseline(db) -> None:
    from cloudwarden.storage import repository as repo
    from cloudwarden.storage.db import session_scope

    on = dt.date(2026, 7, 22)
    with session_scope() as s:
        _seed_series(s, on=on, days=20, spike=560.0)
        children = repo.cost_scope_children(
            s,
            scope_type="subscription",
            scope_value="sub-anom",
            on=on,
            start=on - dt.timedelta(days=45),
        )

    by_child = {c["child"]: c for c in children}
    assert by_child["/r1"]["actual"] == 560.0
    assert by_child["/r1"]["baseline"] == 60.0  # steady 60/day before the spike
    assert by_child["/r2"]["actual"] == 40.0


def test_upsert_cost_anomaly_idempotent_on_scope_and_date(db) -> None:
    from cloudwarden.storage import repository as repo
    from cloudwarden.storage.db import session_scope

    on = dt.date(2026, 7, 22)
    args = dict(
        scope_type="subscription",
        scope_value="sub-anom",
        usage_date=on,
        expected=100.0,
        actual=600.0,
        score=80.0,
        severity="critical",
        contributors=[{"child": "/r1", "delta": 500.0}],
    )
    with session_scope() as s:
        row1, inserted1 = repo.upsert_cost_anomaly(s, **args)
    with session_scope() as s:
        # Re-detect the same scope+date with a refreshed score → updates, does not duplicate.
        row2, inserted2 = repo.upsert_cost_anomaly(s, **{**args, "score": 90.0})
    with session_scope() as s:
        listed = repo.list_cost_anomalies(s)

    assert inserted1 is True
    assert inserted2 is False  # second sighting is an update, not an insert
    assert row1["id"] == row2["id"]
    assert len(listed) == 1
    assert listed[0]["score"] == 90.0  # reflects the latest detection


def test_list_cost_anomalies_filters(db) -> None:
    from cloudwarden.storage import repository as repo
    from cloudwarden.storage.db import session_scope

    on = dt.date(2026, 7, 22)
    with session_scope() as s:
        repo.upsert_cost_anomaly(
            s,
            scope_type="subscription",
            scope_value="sub-a",
            usage_date=on,
            expected=100.0,
            actual=600.0,
            score=80.0,
            severity="critical",
        )
        repo.upsert_cost_anomaly(
            s,
            scope_type="service",
            scope_value="Compute",
            usage_date=on,
            expected=100.0,
            actual=140.0,
            score=4.0,
            severity="low",
        )

    with session_scope() as s:
        assert len(repo.list_cost_anomalies(s)) == 2
        assert len(repo.list_cost_anomalies(s, scope_type="subscription")) == 1
        assert len(repo.list_cost_anomalies(s, severity="critical")) == 1
        assert repo.list_cost_anomalies(s, scope_type="subscription")[0]["scope_value"] == "sub-a"


def test_mark_anomaly_notified(db) -> None:
    from cloudwarden.storage import repository as repo
    from cloudwarden.storage.db import session_scope

    on = dt.date(2026, 7, 22)
    with session_scope() as s:
        row, _ = repo.upsert_cost_anomaly(
            s,
            scope_type="subscription",
            scope_value="sub-a",
            usage_date=on,
            expected=100.0,
            actual=600.0,
            score=80.0,
            severity="critical",
        )
        assert row["notified"] is False
        repo.mark_anomaly_notified(s, row["id"])
    with session_scope() as s:
        assert repo.list_cost_anomalies(s)[0]["notified"] is True


def test_ensure_anomaly_template_is_idempotent(db) -> None:
    from cloudwarden.storage import repository as repo
    from cloudwarden.storage.db import session_scope

    with session_scope() as s:
        first = repo.ensure_anomaly_template(s)
    with session_scope() as s:
        second = repo.ensure_anomaly_template(s)
    assert first == second  # reused, not duplicated


# --------------------------------------------------------------------------- #
# Notification context + dispatch
# --------------------------------------------------------------------------- #
def test_build_anomaly_context_renders() -> None:
    from cloudwarden.notify import service

    ctx = service.build_anomaly_context(
        scope_type="subscription",
        scope_value="sub-a",
        on=dt.date(2026, 7, 22),
        expected=100.0,
        actual=600.0,
        score=80.0,
        severity="critical",
        currency="USD",
        contributors=[{"child": "/r1", "delta": 500.0}],
    )
    assert ctx["scope_value"] == "sub-a"
    assert ctx["severity"] == "critical"
    assert ctx["actual"] == 600.0
    body = service.render(service.DEFAULT_ANOMALY_BODY, ctx)
    assert "sub-a" in body
    assert "critical" in body


def _channel(s, name="anomaly-alerts"):
    from cloudwarden.storage import repository as repo

    return repo.create_notification_channel(
        s, name=name, transport="webhook", target="https://hooks.example/anom"
    )


def test_dispatch_for_anomaly_sends_through_transport(db) -> None:
    from cloudwarden.notify import service
    from cloudwarden.notify.dispatch import dispatch_for_anomaly
    from cloudwarden.storage import repository as repo
    from cloudwarden.storage.db import session_scope

    class _Spy:
        def __init__(self) -> None:
            self.sent: list[dict] = []

        def send(self, *, target, subject, body, config):
            self.sent.append({"body": body})
            return {"ok": True}

    spy = _Spy()
    with session_scope() as s:
        _channel(s)
        tid = repo.ensure_anomaly_template(s)
        ctx = service.build_anomaly_context(
            scope_type="subscription",
            scope_value="sub-a",
            on=dt.date(2026, 7, 22),
            expected=100.0,
            actual=600.0,
            score=80.0,
            severity="critical",
            currency="USD",
            contributors=[],
        )
        result = dispatch_for_anomaly(
            s,
            context=ctx,
            template_id=tid,
            channel_name="anomaly-alerts",
            transport_factory=lambda kind: spy,
        )

    assert result is not None
    assert result["dispatched"] is True
    assert len(spy.sent) == 1
    assert "sub-a" in spy.sent[0]["body"]


def test_dispatch_for_anomaly_without_channel_returns_none(db) -> None:
    from cloudwarden.notify.dispatch import dispatch_for_anomaly
    from cloudwarden.storage import repository as repo
    from cloudwarden.storage.db import session_scope

    with session_scope() as s:
        tid = repo.ensure_anomaly_template(s)
        # Empty channel name → silent (records the anomaly, dispatches nothing).
        assert dispatch_for_anomaly(s, context={}, template_id=tid, channel_name="") is None


def test_dispatch_for_anomaly_unknown_channel_returns_none(db) -> None:
    from cloudwarden.notify.dispatch import dispatch_for_anomaly
    from cloudwarden.storage import repository as repo
    from cloudwarden.storage.db import session_scope

    with session_scope() as s:
        tid = repo.ensure_anomaly_template(s)
        assert dispatch_for_anomaly(s, context={}, template_id=tid, channel_name="nope") is None


# --------------------------------------------------------------------------- #
# Detection orchestration + notification
# --------------------------------------------------------------------------- #
class _Recorder:
    """A dispatch spy: records every anomaly notification, makes no network call."""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[dict] = []
        self._fail = fail

    def __call__(self, session, *, context, template_id, channel_name, **_kw):
        self.calls.append({"context": context, "channel_name": channel_name})
        if self._fail:
            raise RuntimeError("transport exploded")
        return {"dispatched": True}


def test_detect_flags_seeded_spike_with_contributors(db) -> None:
    from cloudwarden.analysis.anomaly import detect_cost_anomalies
    from cloudwarden.storage import repository as repo
    from cloudwarden.storage.db import session_scope

    on = dt.date(2026, 7, 22)
    spy = _Recorder()
    with session_scope() as s:
        _seed_series(s, on=on, days=30, spike=560.0)
        summary = detect_cost_anomalies(
            s, on=on, scopes=["subscription"], dispatch_fn=spy, channel_name="anomaly-alerts"
        )
    with session_scope() as s:
        anomalies = repo.list_cost_anomalies(s, scope_type="subscription")

    assert summary["anomalies_detected"] == 1
    assert len(anomalies) == 1
    a = anomalies[0]
    assert a["scope_value"] == "sub-anom"
    assert a["actual"] == 600.0
    assert a["severity"] in {"high", "critical"}
    # Contributors identify /r1 as the driver of the spike.
    assert a["contributors"][0]["child"] == "/r1"


def test_new_anomaly_notifies_once(db) -> None:
    from cloudwarden.analysis.anomaly import detect_cost_anomalies
    from cloudwarden.storage.db import session_scope

    on = dt.date(2026, 7, 22)
    spy = _Recorder()
    with session_scope() as s:
        _seed_series(s, on=on, days=30, spike=560.0)
    # First detection fires one notification; the second (same scope+date) is a no-op.
    with session_scope() as s:
        detect_cost_anomalies(
            s, on=on, scopes=["subscription"], dispatch_fn=spy, channel_name="anomaly-alerts"
        )
    with session_scope() as s:
        second = detect_cost_anomalies(
            s, on=on, scopes=["subscription"], dispatch_fn=spy, channel_name="anomaly-alerts"
        )

    assert len(spy.calls) == 1  # notified exactly once across two detections
    assert second["notifications_sent"] == 0


def test_detect_steady_series_no_anomaly(db) -> None:
    from cloudwarden.analysis.anomaly import detect_cost_anomalies
    from cloudwarden.storage import repository as repo
    from cloudwarden.storage.db import session_scope

    on = dt.date(2026, 7, 22)
    with session_scope() as s:
        _seed_series(s, on=on, days=30)  # no spike
        summary = detect_cost_anomalies(s, on=on, scopes=["subscription"], dispatch_fn=_Recorder())
    with session_scope() as s:
        assert repo.list_cost_anomalies(s) == []
    assert summary["anomalies_detected"] == 0


def test_detect_signal_gated_on_sparse_history(db) -> None:
    from cloudwarden.analysis.anomaly import detect_cost_anomalies
    from cloudwarden.storage import repository as repo
    from cloudwarden.storage.db import session_scope

    on = dt.date(2026, 7, 22)
    with session_scope() as s:
        _seed_series(s, on=on, days=5, spike=5000.0)  # only 5 baseline days
        detect_cost_anomalies(s, on=on, scopes=["subscription"], dispatch_fn=_Recorder())
    with session_scope() as s:
        assert repo.list_cost_anomalies(s) == []  # suppressed on thin history


def test_detect_swallows_dispatch_failure(db) -> None:
    from cloudwarden.analysis.anomaly import detect_cost_anomalies
    from cloudwarden.storage import repository as repo
    from cloudwarden.storage.db import session_scope

    on = dt.date(2026, 7, 22)
    spy = _Recorder(fail=True)
    with session_scope() as s:
        _seed_series(s, on=on, days=30, spike=560.0)
        # A transport failure must never break detection — the anomaly is still recorded.
        summary = detect_cost_anomalies(
            s, on=on, scopes=["subscription"], dispatch_fn=spy, channel_name="anomaly-alerts"
        )
    with session_scope() as s:
        anomalies = repo.list_cost_anomalies(s)

    assert len(anomalies) == 1
    assert anomalies[0]["notified"] is False  # dispatch failed → not marked notified
    assert summary["notifications_sent"] == 0


def test_detect_default_scopes_cover_all_grains(db) -> None:
    from cloudwarden.analysis.anomaly import DEFAULT_SCOPES

    assert set(DEFAULT_SCOPES) == {"subscription", "service", "resource_type", "resource"}


# --------------------------------------------------------------------------- #
# API — read + RBAC
# --------------------------------------------------------------------------- #
def test_anomalies_endpoint_lists_and_filters(db) -> None:
    from fastapi.testclient import TestClient

    from cloudwarden.api.main import app
    from cloudwarden.storage import repository as repo
    from cloudwarden.storage.db import session_scope

    on = dt.date(2026, 7, 22)
    with session_scope() as s:
        repo.upsert_cost_anomaly(
            s,
            scope_type="subscription",
            scope_value="sub-a",
            usage_date=on,
            expected=100.0,
            actual=600.0,
            score=80.0,
            severity="critical",
        )
        repo.upsert_cost_anomaly(
            s,
            scope_type="service",
            scope_value="Compute",
            usage_date=on,
            expected=100.0,
            actual=140.0,
            score=4.0,
            severity="low",
        )
    client = TestClient(app)  # RBAC off by default

    resp = client.get("/api/finops/anomalies")
    assert resp.status_code == 200
    assert len(resp.json()["anomalies"]) == 2
    assert len(client.get("/api/finops/anomalies?severity=critical").json()["anomalies"]) == 1
    filtered = client.get("/api/finops/anomalies?scope_type=subscription").json()["anomalies"]
    assert len(filtered) == 1
    assert filtered[0]["scope_value"] == "sub-a"


def test_anomalies_read_requires_permission(db, monkeypatch) -> None:
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

    assert client.get("/api/finops/anomalies").status_code == 401
    assert client.get("/api/finops/anomalies", headers={"X-Principal": "ed"}).status_code == 200
    get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# Edge cases (guards, validation, filters)
# --------------------------------------------------------------------------- #
def test_weekday_factors_edge_cases() -> None:
    from cloudwarden.analysis.anomaly import weekday_factors

    assert weekday_factors([]) == {}  # empty baseline → no factors
    zeros = [(dt.date(2026, 1, 5), 0.0), (dt.date(2026, 1, 6), 0.0)]
    assert weekday_factors(zeros) == {}  # zero-median baseline → no factors

    # A weekday with a single sample is too thin to trust — omitted (defaults to 1.0).
    thin = [
        (dt.date(2026, 1, 5), 100.0),  # Monday
        (dt.date(2026, 1, 12), 100.0),  # Monday
        (dt.date(2026, 1, 13), 100.0),  # a lone Tuesday
    ]
    factors = weekday_factors(thin)
    assert 0 in factors  # Monday has 2 samples
    assert 1 not in factors  # Tuesday has 1 — skipped


def test_cost_daily_by_scope_rejects_unknown_scope(db) -> None:
    import pytest

    from cloudwarden.storage import repository as repo
    from cloudwarden.storage.db import session_scope

    with session_scope() as s, pytest.raises(ValueError, match="unknown anomaly scope_type"):
        repo.cost_daily_by_scope(
            s, scope_type="bogus", start=dt.date(2026, 7, 1), end=dt.date(2026, 7, 22)
        )


def test_list_cost_anomalies_date_window(db) -> None:
    from cloudwarden.storage import repository as repo
    from cloudwarden.storage.db import session_scope

    with session_scope() as s:
        for day in (dt.date(2026, 7, 10), dt.date(2026, 7, 20)):
            repo.upsert_cost_anomaly(
                s,
                scope_type="subscription",
                scope_value="sub-a",
                usage_date=day,
                expected=100.0,
                actual=600.0,
                score=80.0,
                severity="critical",
            )
    with session_scope() as s:
        windowed = repo.list_cost_anomalies(
            s, since=dt.date(2026, 7, 15), until=dt.date(2026, 7, 25)
        )
    assert [a["usage_date"] for a in windowed] == ["2026-07-20"]


def test_mark_anomaly_notified_missing_returns_false(db) -> None:
    from cloudwarden.storage import repository as repo
    from cloudwarden.storage.db import session_scope

    with session_scope() as s:
        assert repo.mark_anomaly_notified(s, 999999) is False


def test_mock_spike_overlay_only_when_enabled(monkeypatch) -> None:
    from cloudwarden.azure.cost import collect_cost
    from cloudwarden.config import get_settings

    def _peak_by_resource() -> dict[str, dict]:
        rows = [r for r in collect_cost() if r.cost_type == "Amortized"]
        by_res: dict[str, dict] = {}
        for r in rows:
            by_res.setdefault(r.resource_id, {})[r.usage_date] = r.cost
        return by_res

    # Default: the mock series is smooth — no single day towers over its neighbours.
    get_settings.cache_clear()
    smooth = _peak_by_resource()
    for days in smooth.values():
        assert max(days.values()) < 2 * min(days.values())

    # Enabled: one resource's most-recent day spikes far above its own baseline.
    monkeypatch.setenv("ANOMALY_MOCK_SPIKE", "1")
    get_settings.cache_clear()
    today = dt.date.today()
    spiked = _peak_by_resource()
    driver = max(spiked, key=lambda rid: max(spiked[rid].values()))
    baseline = [c for d, c in spiked[driver].items() if d != today]
    assert spiked[driver][today] > 3 * max(baseline)
    get_settings.cache_clear()
