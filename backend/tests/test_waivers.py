"""M14.9 — exemptions / waivers workflow. Tests written FIRST (TDD).

A **waiver** is a first-class, scoped, justified, approved, *expiring* exception to a
policy: request → approve (RBAC) → active-until-expiry → auto-expire. At execution,
a matched resource covered by an *active* waiver is recorded as **waived** (with the
waiver id), never enforced; when the waiver expires the finding re-surfaces. Layers:

* **Pure resolution** (no DB): ``is_active`` (state + expiry), ``scope_covers``
  (policy-wide / resource / resource-group / tag), and ``resolve_waiver`` / ``is_waived``
  — the deterministic ``now`` core, unit-tested without a database.
* **Repository / lifecycle** (``db`` fixture): request creates a *pending* waiver,
  approve activates it, reject rejects it, and expiry reconciles active→expired — each
  transition audited; ``active_waivers_for`` powers match-time suppression.
* **Enforcement** (``db`` fixture): ``queue_policy_action`` resolves a match against
  active waivers; a covered match is persisted **waived** (with the waiver id) and never
  queued for enforcement — expired / pending / out-of-scope matches still enforce.
* **API** (``db`` fixture): ``GET/POST /api/waivers`` + ``approve|reject`` — RBAC-guarded
  and audited; expiring-soon notifications fire once through the configured channel.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

NOW = datetime(2026, 7, 24, 12, 0, 0, tzinfo=UTC)


def _waiver(**overrides):
    """A public-shaped waiver dict (active, policy-wide, far-future expiry) + overrides."""
    base = {
        "id": 1,
        "policy_id": 10,
        "scope_type": "policy",
        "scope_value": None,
        "justification": "planned migration",
        "requester": "dev@corp",
        "approver": "lead@corp",
        "state": "active",
        "expires_at": NOW + timedelta(days=30),
        "notified_expiring": False,
    }
    base.update(overrides)
    return base


def _match(policy_id=10, resource_id="/subs/s/resourceGroups/rg-a/providers/x/vm1", tags=None):
    return {"policy_id": policy_id, "resource_id": resource_id, "tags": tags or {}}


# --------------------------------------------------------------------------- #
# Pure resolution — is_active + scope_covers + resolve (no DB)
# --------------------------------------------------------------------------- #
def test_is_active_true_for_approved_unexpired() -> None:
    from cloudwarden.authz import waivers

    assert waivers.is_active(_waiver(), NOW) is True


def test_is_active_false_for_expired() -> None:
    from cloudwarden.authz import waivers

    assert waivers.is_active(_waiver(expires_at=NOW - timedelta(seconds=1)), NOW) is False


def test_is_active_false_for_pending() -> None:
    from cloudwarden.authz import waivers

    # Not yet approved → never active, even with a future expiry.
    assert waivers.is_active(_waiver(state="pending"), NOW) is False


def test_is_active_false_for_rejected() -> None:
    from cloudwarden.authz import waivers

    assert waivers.is_active(_waiver(state="rejected"), NOW) is False


def test_scope_covers_policy_wide() -> None:
    from cloudwarden.authz import waivers

    # A policy-scoped waiver covers every resource the policy matches.
    assert waivers.scope_covers(_waiver(scope_type="policy"), _match()) is True


def test_scope_covers_specific_resource() -> None:
    from cloudwarden.authz import waivers

    rid = "/subs/s/resourceGroups/rg-a/providers/x/vm1"
    w = _waiver(scope_type="resource", scope_value=rid)
    assert waivers.scope_covers(w, _match(resource_id=rid)) is True


def test_scope_specific_resource_mismatch_not_covered() -> None:
    from cloudwarden.authz import waivers

    w = _waiver(scope_type="resource", scope_value="/subs/s/other/vmX")
    assert waivers.scope_covers(w, _match(resource_id="/subs/s/rg/vm1")) is False


def test_scope_covers_resource_group_case_insensitive() -> None:
    from cloudwarden.authz import waivers

    w = _waiver(scope_type="resource_group", scope_value="RG-A")
    rid = "/subs/s/resourceGroups/rg-a/providers/x/vm1"
    assert waivers.scope_covers(w, _match(resource_id=rid)) is True


def test_scope_resource_group_mismatch_not_covered() -> None:
    from cloudwarden.authz import waivers

    w = _waiver(scope_type="resource_group", scope_value="rg-prod")
    rid = "/subs/s/resourceGroups/rg-dev/providers/x/vm1"
    assert waivers.scope_covers(w, _match(resource_id=rid)) is False


def test_scope_covers_tag() -> None:
    from cloudwarden.authz import waivers

    w = _waiver(scope_type="tag", scope_value="env=sandbox")
    assert waivers.scope_covers(w, _match(tags={"env": "sandbox"})) is True


def test_scope_tag_mismatch_not_covered() -> None:
    from cloudwarden.authz import waivers

    w = _waiver(scope_type="tag", scope_value="env=sandbox")
    assert waivers.scope_covers(w, _match(tags={"env": "prod"})) is False


def test_resolve_waiver_picks_matching_active() -> None:
    from cloudwarden.authz import waivers

    pending = _waiver(id=1, state="pending")
    expired = _waiver(id=2, expires_at=NOW - timedelta(days=1))
    good = _waiver(id=3, scope_type="resource", scope_value="/subs/s/rg/vm1")
    resolved = waivers.resolve_waiver(
        _match(resource_id="/subs/s/rg/vm1"), [pending, expired, good], now=NOW
    )
    assert resolved is not None
    assert resolved["id"] == 3


def test_is_waived_false_when_policy_differs() -> None:
    from cloudwarden.authz import waivers

    # An active, in-scope waiver for a *different* policy never suppresses this match.
    w = _waiver(policy_id=999)
    assert waivers.is_waived(_match(policy_id=10), [w], now=NOW) is False


def test_is_waived_true_for_covering_active_waiver() -> None:
    from cloudwarden.authz import waivers

    assert waivers.is_waived(_match(), [_waiver()], now=NOW) is True


def test_is_active_false_when_expiry_missing() -> None:
    from cloudwarden.authz import waivers

    assert waivers.is_active(_waiver(expires_at=None), NOW) is False


def test_is_active_accepts_iso_string_expiry() -> None:
    from cloudwarden.authz import waivers

    # A waiver loaded from JSON carries an ISO-string expiry; it must still resolve.
    iso = (NOW + timedelta(days=1)).isoformat()
    assert waivers.is_active(_waiver(expires_at=iso), NOW) is True


def test_scope_resource_group_without_rg_not_covered() -> None:
    from cloudwarden.authz import waivers

    # A resource id with no resource-group segment can never match an RG scope.
    w = _waiver(scope_type="resource_group", scope_value="rg-a")
    assert waivers.scope_covers(w, _match(resource_id="/subs/s/providers/x/vm1")) is False


def test_scope_unknown_type_not_covered() -> None:
    from cloudwarden.authz import waivers

    assert waivers.scope_covers(_waiver(scope_type="galaxy"), _match()) is False


def test_waiver_for_match_none_policy_returns_none() -> None:
    from cloudwarden.authz import waivers

    # No policy id (unresolved execution) → nothing to waive; never touches the DB.
    assert waivers.waiver_for_match(None, policy_id=None, resource_id="/x") is None


def test_request_rejects_unknown_scope_type() -> None:
    import pytest

    from cloudwarden.authz import waivers

    # scope_type is validated before any DB call, so session may be None here.
    with pytest.raises(waivers.WaiverError):
        waivers.request_waiver(
            None,
            policy_id=1,
            justification="j",
            expires_at=NOW + timedelta(days=1),
            scope_type="galaxy",
            now=NOW,
        )


# --------------------------------------------------------------------------- #
# Notification context
# --------------------------------------------------------------------------- #
def test_build_waiver_context_renders() -> None:
    from cloudwarden.notify import service

    ctx = service.build_waiver_context(
        waiver_id=7,
        policy_name="stop-idle-vms",
        scope_type="resource_group",
        scope_value="rg-a",
        expires_at=NOW + timedelta(days=2),
        days_left=2,
        requester="dev@corp",
    )
    assert ctx["waiver_id"] == 7
    assert ctx["days_left"] == 2
    body = service.render(service.DEFAULT_WAIVER_SUBJECT, ctx)
    assert "stop-idle-vms" in body


# --------------------------------------------------------------------------- #
# Repository + lifecycle
# --------------------------------------------------------------------------- #
def _seed_policy(s, name="stop-idle-vms", resource_type="azure.vm"):
    from cloudwarden.storage import schema

    rec = schema.Policy(name=name, resource_type=resource_type, spec={"policies": []})
    s.add(rec)
    s.flush()
    return rec.id


def test_waiver_request_approve_lifecycle(db) -> None:
    from cloudwarden.authz import waivers
    from cloudwarden.storage import repository as repo
    from cloudwarden.storage.db import session_scope

    with session_scope() as s:
        pid = _seed_policy(s)
        requested = waivers.request_waiver(
            s,
            policy_id=pid,
            justification="planned migration window",
            expires_at=NOW + timedelta(days=7),
            requester="dev@corp",
            now=NOW,
        )
    with session_scope() as s:
        approved = waivers.approve_waiver(s, requested["id"], approver="lead@corp", now=NOW)
    with session_scope() as s:
        stored = repo.get_waiver(s, requested["id"])

    assert requested["state"] == "pending"
    assert requested["approver"] is None
    assert approved["state"] == "active"
    assert approved["approver"] == "lead@corp"
    assert stored["state"] == "active"


def test_request_rejects_blank_justification(db) -> None:
    import pytest

    from cloudwarden.authz import waivers
    from cloudwarden.storage.db import session_scope

    with session_scope() as s:
        pid = _seed_policy(s)
        with pytest.raises(waivers.WaiverError):
            waivers.request_waiver(
                s, policy_id=pid, justification="   ", expires_at=NOW + timedelta(days=1), now=NOW
            )


def test_request_rejects_past_expiry(db) -> None:
    import pytest

    from cloudwarden.authz import waivers
    from cloudwarden.storage.db import session_scope

    with session_scope() as s:
        pid = _seed_policy(s)
        with pytest.raises(waivers.WaiverError):
            waivers.request_waiver(
                s, policy_id=pid, justification="oops", expires_at=NOW - timedelta(days=1), now=NOW
            )


def test_approve_non_pending_raises(db) -> None:
    import pytest

    from cloudwarden.authz import waivers
    from cloudwarden.storage.db import session_scope

    with session_scope() as s:
        pid = _seed_policy(s)
        w = waivers.request_waiver(
            s, policy_id=pid, justification="j", expires_at=NOW + timedelta(days=3), now=NOW
        )
        waivers.approve_waiver(s, w["id"], approver="lead", now=NOW)
    with session_scope() as s:
        with pytest.raises(waivers.WaiverAlreadyDecided):
            waivers.approve_waiver(s, w["id"], approver="lead", now=NOW)


def test_approve_unknown_waiver_raises(db) -> None:
    import pytest

    from cloudwarden.authz import waivers
    from cloudwarden.storage.db import session_scope

    with session_scope() as s:
        with pytest.raises(waivers.WaiverNotFound):
            waivers.approve_waiver(s, 999999, approver="lead", now=NOW)


def test_reject_sets_state_rejected(db) -> None:
    from cloudwarden.authz import waivers
    from cloudwarden.storage.db import session_scope

    with session_scope() as s:
        pid = _seed_policy(s)
        w = waivers.request_waiver(
            s, policy_id=pid, justification="j", expires_at=NOW + timedelta(days=3), now=NOW
        )
        rejected = waivers.reject_waiver(s, w["id"], approver="lead")
    assert rejected["state"] == "rejected"


def test_active_waivers_for_returns_only_active(db) -> None:
    from cloudwarden.authz import waivers
    from cloudwarden.storage import repository as repo
    from cloudwarden.storage.db import session_scope

    with session_scope() as s:
        pid = _seed_policy(s)
        pending = waivers.request_waiver(
            s, policy_id=pid, justification="p", expires_at=NOW + timedelta(days=5), now=NOW
        )
        active = waivers.request_waiver(
            s, policy_id=pid, justification="a", expires_at=NOW + timedelta(days=5), now=NOW
        )
        waivers.approve_waiver(s, active["id"], approver="lead", now=NOW)
    with session_scope() as s:
        rows = repo.active_waivers_for(s, pid)

    ids = {r["id"] for r in rows}
    assert active["id"] in ids
    assert pending["id"] not in ids


def test_expire_due_waivers_flips_and_audits(db) -> None:
    from cloudwarden.authz import waivers
    from cloudwarden.storage import repository as repo
    from cloudwarden.storage import schema
    from cloudwarden.storage.db import session_scope

    with session_scope() as s:
        pid = _seed_policy(s)
        due = waivers.request_waiver(
            s, policy_id=pid, justification="due", expires_at=NOW + timedelta(days=1), now=NOW
        )
        waivers.approve_waiver(s, due["id"], approver="lead", now=NOW)
        fresh = waivers.request_waiver(
            s, policy_id=pid, justification="fresh", expires_at=NOW + timedelta(days=30), now=NOW
        )
        waivers.approve_waiver(s, fresh["id"], approver="lead", now=NOW)
    with session_scope() as s:
        # Two days later the first waiver is past its expiry; reconcile flips it.
        expired = waivers.expire_due_waivers(s, now=NOW + timedelta(days=2), actor="system")
    with session_scope() as s:
        assert repo.get_waiver(s, due["id"])["state"] == "expired"
        assert repo.get_waiver(s, fresh["id"])["state"] == "active"
        audits = s.query(schema.AuditLog).filter(schema.AuditLog.action == "waiver:expire").all()
    assert expired == 1
    assert len(audits) == 1


# --------------------------------------------------------------------------- #
# Enforcement — waived matches are recorded, never enforced
# --------------------------------------------------------------------------- #
def _seed_match(s, policy_id, resource_id, resource_type="azure.vm"):
    """A PolicyExecution + PolicyMatch for ``policy_id``; returns the match id."""
    from cloudwarden.storage import repository as repo
    from cloudwarden.storage import schema

    repo.create_policy_execution(
        s, execution_id=f"ex-{resource_id}", policy_id=policy_id, subscription_id="sub-a"
    )
    match = schema.PolicyMatch(
        execution_id=f"ex-{resource_id}", resource_id=resource_id, resource_type=resource_type
    )
    s.add(match)
    s.flush()
    return match.id


def test_active_waiver_suppresses_enforcement_as_waived(db) -> None:
    from cloudwarden.authz import waivers
    from cloudwarden.remediation import approval
    from cloudwarden.storage import repository as repo
    from cloudwarden.storage import schema
    from cloudwarden.storage.db import session_scope

    rid = "/subs/s/resourceGroups/rg-a/providers/x/vm1"
    with session_scope() as s:
        pid = _seed_policy(s)
        w = waivers.request_waiver(
            s,
            policy_id=pid,
            justification="waive it",
            expires_at=datetime.now(UTC) + timedelta(days=30),
        )
        waivers.approve_waiver(s, w["id"], approver="lead")
        match_id = _seed_match(s, pid, rid)
    with session_scope() as s:
        result = approval.queue_policy_action(s, match_id, "stop")
    with session_scope() as s:
        match = repo.get_policy_match(s, match_id)
        actions = s.query(schema.RemediationAction).all()

    assert result["status"] == "waived"
    assert result["waiver_id"] == w["id"]
    assert match["action_taken"] == "waived"
    assert match["action_result"]["waiver_id"] == w["id"]
    # Recorded as waived, never queued pending for enforcement.
    assert len(actions) == 1
    assert actions[0].status == "waived"


def test_expired_waiver_reexposes_finding(db) -> None:
    from cloudwarden.remediation import approval
    from cloudwarden.storage import repository as repo
    from cloudwarden.storage.db import session_scope

    rid = "/subs/s/resourceGroups/rg-a/providers/x/vm1"
    with session_scope() as s:
        pid = _seed_policy(s)
        # Seed directly as 'active' but already past expiry (an un-reconciled expired waiver);
        # request_waiver would rightly reject a past expiry, so bypass it for this fixture.
        repo.create_waiver(
            s,
            policy_id=pid,
            justification="expired",
            expires_at=datetime.now(UTC) - timedelta(days=1),
            state="active",
        )
        match_id = _seed_match(s, pid, rid)
    with session_scope() as s:
        result = approval.queue_policy_action(s, match_id, "stop")

    # Past expiry → not waived → the action is enforceable (pending).
    assert result["status"] == "pending"
    assert result["waiver_id"] is None


def test_unapproved_waiver_does_not_suppress(db) -> None:
    from cloudwarden.authz import waivers
    from cloudwarden.remediation import approval
    from cloudwarden.storage.db import session_scope

    rid = "/subs/s/resourceGroups/rg-a/providers/x/vm1"
    with session_scope() as s:
        pid = _seed_policy(s)
        waivers.request_waiver(
            s,
            policy_id=pid,
            justification="pending",
            expires_at=datetime.now(UTC) + timedelta(days=5),
        )
        match_id = _seed_match(s, pid, rid)
    with session_scope() as s:
        result = approval.queue_policy_action(s, match_id, "stop")

    assert result["status"] == "pending"  # pending waiver never suppresses


def test_out_of_scope_resource_still_enforced(db) -> None:
    from cloudwarden.authz import waivers
    from cloudwarden.remediation import approval
    from cloudwarden.storage.db import session_scope

    with session_scope() as s:
        pid = _seed_policy(s)
        w = waivers.request_waiver(
            s,
            policy_id=pid,
            justification="only vm1",
            scope_type="resource",
            scope_value="/subs/s/resourceGroups/rg-a/providers/x/vm1",
            expires_at=datetime.now(UTC) + timedelta(days=30),
        )
        waivers.approve_waiver(s, w["id"], approver="lead")
        # A *different* resource under the same policy — not covered by the waiver.
        match_id = _seed_match(s, pid, "/subs/s/resourceGroups/rg-a/providers/x/vm2")
    with session_scope() as s:
        result = approval.queue_policy_action(s, match_id, "stop")

    assert result["status"] == "pending"


# --------------------------------------------------------------------------- #
# Expiring-soon notification
# --------------------------------------------------------------------------- #
class _Recorder:
    """A dispatch spy: records every waiver notification, makes no network call."""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[dict] = []
        self._fail = fail

    def __call__(self, session, *, context, template_id, channel_name, **_kw):
        self.calls.append({"context": context, "channel_name": channel_name})
        if self._fail:
            raise RuntimeError("transport exploded")
        return {"dispatched": True}


def _seed_active_waiver(s, pid, *, justification, expires_at):
    from cloudwarden.authz import waivers

    w = waivers.request_waiver(s, policy_id=pid, justification=justification, expires_at=expires_at)
    waivers.approve_waiver(s, w["id"], approver="lead")
    return w


def test_expiring_soon_notification(db) -> None:
    from cloudwarden.authz import waivers
    from cloudwarden.storage import repository as repo
    from cloudwarden.storage.db import session_scope

    spy = _Recorder()
    with session_scope() as s:
        pid = _seed_policy(s)
        soon = _seed_active_waiver(s, pid, justification="soon", expires_at=NOW + timedelta(days=2))
        _seed_active_waiver(s, pid, justification="later", expires_at=NOW + timedelta(days=30))
    with session_scope() as s:
        first = waivers.notify_expiring_waivers(
            s, now=NOW, within_days=7, channel_name="waiver-alerts", dispatch_fn=spy
        )
    with session_scope() as s:
        # A second pass never re-notifies the same waiver.
        second = waivers.notify_expiring_waivers(
            s, now=NOW, within_days=7, channel_name="waiver-alerts", dispatch_fn=spy
        )
    with session_scope() as s:
        stored = repo.get_waiver(s, soon["id"])

    assert first["notifications_sent"] == 1
    assert second["notifications_sent"] == 0
    assert len(spy.calls) == 1  # only the one expiring within the window
    assert stored["notified_expiring"] is True


def test_notify_expiring_no_channel_is_noop(db) -> None:
    from cloudwarden.authz import waivers
    from cloudwarden.storage.db import session_scope

    with session_scope() as s:
        pid = _seed_policy(s)
        _seed_active_waiver(s, pid, justification="soon", expires_at=NOW + timedelta(days=1))
    with session_scope() as s:
        # A waiver is expiring, but no channel is configured → counted, never dispatched.
        summary = waivers.notify_expiring_waivers(s, now=NOW, within_days=7, channel_name="")

    assert summary["expiring"] == 1
    assert summary["notifications_sent"] == 0


def test_notify_expiring_unknown_channel_is_silent(db) -> None:
    from cloudwarden.authz import waivers
    from cloudwarden.storage import repository as repo
    from cloudwarden.storage.db import session_scope

    with session_scope() as s:
        pid = _seed_policy(s)
        w = _seed_active_waiver(s, pid, justification="soon", expires_at=NOW + timedelta(days=1))
    with session_scope() as s:
        # No dispatch_fn → default dispatch resolves a channel by name; an unknown
        # channel is silent (no notification, no error, not marked notified).
        summary = waivers.notify_expiring_waivers(
            s, now=NOW, within_days=7, channel_name="ghost-channel"
        )
    with session_scope() as s:
        assert repo.get_waiver(s, w["id"])["notified_expiring"] is False
    assert summary["notifications_sent"] == 0


def test_notify_expiring_swallows_dispatch_failure(db) -> None:
    from cloudwarden.authz import waivers
    from cloudwarden.storage import repository as repo
    from cloudwarden.storage.db import session_scope

    with session_scope() as s:
        pid = _seed_policy(s)
        w = _seed_active_waiver(s, pid, justification="soon", expires_at=NOW + timedelta(days=1))
    with session_scope() as s:
        # A transport failure must never break the sweep.
        summary = waivers.notify_expiring_waivers(
            s,
            now=NOW,
            within_days=7,
            channel_name="waiver-alerts",
            dispatch_fn=_Recorder(fail=True),
        )
    with session_scope() as s:
        assert repo.get_waiver(s, w["id"])["notified_expiring"] is False
    assert summary["notifications_sent"] == 0


def test_dispatch_for_waiver_sends_through_transport(db) -> None:
    from cloudwarden.notify import service
    from cloudwarden.notify.dispatch import dispatch_for_waiver
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
            s, name="waiver-alerts", transport="webhook", target="https://hooks.example/waiver"
        )
        tid = repo.ensure_waiver_template(s)
        ctx = service.build_waiver_context(
            waiver_id=1,
            policy_name="stop-idle-vms",
            scope_type="policy",
            scope_value=None,
            expires_at=NOW + timedelta(days=2),
            days_left=2,
            requester="dev@corp",
        )
        result = dispatch_for_waiver(
            s,
            context=ctx,
            template_id=tid,
            channel_name="waiver-alerts",
            transport_factory=lambda kind: spy,
        )

    assert result is not None
    assert result["dispatched"] is True
    assert len(spy.sent) == 1
    assert "stop-idle-vms" in spy.sent[0]


def test_dispatch_for_waiver_without_channel_returns_none(db) -> None:
    from cloudwarden.notify.dispatch import dispatch_for_waiver
    from cloudwarden.storage import repository as repo
    from cloudwarden.storage.db import session_scope

    with session_scope() as s:
        tid = repo.ensure_waiver_template(s)
        assert dispatch_for_waiver(s, context={}, template_id=tid, channel_name="") is None


# --------------------------------------------------------------------------- #
# API — GET/POST /api/waivers + approve|reject (RBAC + audit)
# --------------------------------------------------------------------------- #
def _future(days=7):
    return (datetime.now(UTC) + timedelta(days=days)).isoformat()


def test_list_and_create_waiver_endpoint(db) -> None:
    from fastapi.testclient import TestClient

    from cloudwarden.api.main import app
    from cloudwarden.storage.db import session_scope

    with session_scope() as s:
        pid = _seed_policy(s)
    client = TestClient(app)

    created = client.post(
        "/api/waivers",
        json={"policy_id": pid, "justification": "planned work", "expires_at": _future()},
    )
    assert created.status_code == 200
    assert created.json()["state"] == "pending"

    listed = client.get("/api/waivers")
    assert listed.status_code == 200
    assert len(listed.json()["waivers"]) == 1


def test_create_waiver_unknown_policy_404(db) -> None:
    from fastapi.testclient import TestClient

    from cloudwarden.api.main import app

    client = TestClient(app)
    resp = client.post(
        "/api/waivers",
        json={"policy_id": 424242, "justification": "x", "expires_at": _future()},
    )
    assert resp.status_code == 404


def test_create_waiver_blank_justification_400(db) -> None:
    from fastapi.testclient import TestClient

    from cloudwarden.api.main import app
    from cloudwarden.storage.db import session_scope

    with session_scope() as s:
        pid = _seed_policy(s)
    client = TestClient(app)
    resp = client.post(
        "/api/waivers",
        json={"policy_id": pid, "justification": "  ", "expires_at": _future()},
    )
    assert resp.status_code == 400


def test_approve_endpoint_activates_and_audits(db) -> None:
    from fastapi.testclient import TestClient

    from cloudwarden.api.main import app
    from cloudwarden.storage import schema
    from cloudwarden.storage.db import session_scope

    with session_scope() as s:
        pid = _seed_policy(s)
    client = TestClient(app)
    wid = client.post(
        "/api/waivers",
        json={"policy_id": pid, "justification": "j", "expires_at": _future()},
    ).json()["id"]

    resp = client.post(f"/api/waivers/{wid}/approve", headers={"X-Principal": "lead"})
    assert resp.status_code == 200
    assert resp.json()["state"] == "active"

    with session_scope() as s:
        audits = s.query(schema.AuditLog).filter(schema.AuditLog.action == "waiver:approve").all()
    assert len(audits) == 1


def test_approve_unknown_waiver_endpoint_404(db) -> None:
    from fastapi.testclient import TestClient

    from cloudwarden.api.main import app

    client = TestClient(app)
    assert client.post("/api/waivers/999999/approve").status_code == 404


def test_approve_non_pending_endpoint_409(db) -> None:
    from fastapi.testclient import TestClient

    from cloudwarden.api.main import app
    from cloudwarden.storage.db import session_scope

    with session_scope() as s:
        pid = _seed_policy(s)
    client = TestClient(app)
    wid = client.post(
        "/api/waivers",
        json={"policy_id": pid, "justification": "j", "expires_at": _future()},
    ).json()["id"]
    client.post(f"/api/waivers/{wid}/approve")
    # Approving an already-active waiver conflicts.
    assert client.post(f"/api/waivers/{wid}/approve").status_code == 409


def test_all_state_changes_audited(db) -> None:
    from fastapi.testclient import TestClient

    from cloudwarden.api.main import app
    from cloudwarden.authz import waivers
    from cloudwarden.storage import schema
    from cloudwarden.storage.db import session_scope

    with session_scope() as s:
        pid = _seed_policy(s)
    client = TestClient(app)

    # request → approve one waiver; request → reject another.
    a = client.post(
        "/api/waivers", json={"policy_id": pid, "justification": "a", "expires_at": _future(1)}
    ).json()
    client.post(f"/api/waivers/{a['id']}/approve", headers={"X-Principal": "lead"})
    b = client.post(
        "/api/waivers", json={"policy_id": pid, "justification": "b", "expires_at": _future()}
    ).json()
    client.post(f"/api/waivers/{b['id']}/reject", headers={"X-Principal": "lead"})

    # Auto-expire the approved (soon-expiring) waiver → an expiry audit.
    with session_scope() as s:
        waivers.expire_due_waivers(s, now=datetime.now(UTC) + timedelta(days=2), actor="system")
    with session_scope() as s:
        actions = {
            row.action
            for row in s.query(schema.AuditLog)
            .filter(schema.AuditLog.target_type == "waiver")
            .all()
        }
    assert {"waiver:request", "waiver:approve", "waiver:reject", "waiver:expire"} <= actions


def test_reject_endpoint(db) -> None:
    from fastapi.testclient import TestClient

    from cloudwarden.api.main import app
    from cloudwarden.storage.db import session_scope

    with session_scope() as s:
        pid = _seed_policy(s)
    client = TestClient(app)
    wid = client.post(
        "/api/waivers", json={"policy_id": pid, "justification": "j", "expires_at": _future()}
    ).json()["id"]
    resp = client.post(f"/api/waivers/{wid}/reject")
    assert resp.status_code == 200
    assert resp.json()["state"] == "rejected"


def test_approval_requires_permission(db, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from cloudwarden.api.main import app
    from cloudwarden.authz import rbac
    from cloudwarden.config import get_settings
    from cloudwarden.storage import repository as repo
    from cloudwarden.storage.db import session_scope

    monkeypatch.setenv("RBAC_ENABLED", "1")
    get_settings.cache_clear()
    with session_scope() as s:
        pid = _seed_policy(s)
        rbac.seed_default_roles(s)
        repo.assign_role(s, principal="ed", role_name="editor")
        w = repo.create_waiver(
            s,
            policy_id=pid,
            justification="j",
            expires_at=datetime.now(UTC) + timedelta(days=5),
        )
    client = TestClient(app)

    # No principal → 401; an editor (holds waiver:approve) → 200.
    assert client.post(f"/api/waivers/{w['id']}/approve").status_code == 401
    ok = client.post(f"/api/waivers/{w['id']}/approve", headers={"X-Principal": "ed"})
    assert ok.status_code == 200
    get_settings.cache_clear()
