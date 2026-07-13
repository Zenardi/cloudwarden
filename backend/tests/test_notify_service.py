"""Notification service & templates (M8.1) — sandboxed render + pluggable dispatch.

Written test-first (TDD). A ``notify`` service renders a **communication template**
(Stacklet / c7n-mailer heritage) from policy-violation context in a **sandboxed**
Jinja2 environment and dispatches the rendered message through an **injected
transport** — no real network, no Azure. Templates and channels are persisted in
``notification_templates`` / ``notification_channels`` with repository CRUD.

Invariants (Arrange–Act–Assert), each test one reason to fail:

* a template renders the policy name, resource id and count from context;
* rendering is sandboxed — unsafe attribute access (dunders, the ``attr`` filter
  bypass) raises ``SecurityError``, never reaches Python internals;
* the service hands the *rendered* subject/body to an injected transport;
* a missing template variable renders **empty**, not a crash;
* a disabled channel is skipped (never dispatched);
* an unknown template or channel raises ``NotFound``;
* channels (and templates) round-trip through CRUD.

The pure-render tests need no database; the service / CRUD tests use the ``db``
fixture. Every test is offline.
"""

from __future__ import annotations

import pytest

from azure_finops.notify import service
from azure_finops.storage import repository as repo
from azure_finops.storage.db import session_scope


# --------------------------------------------------------------------------- #
# Test doubles — an injected transport and an injected HTTP client (no network)
# --------------------------------------------------------------------------- #
class _RecordingTransport:
    """A transport spy: records every dispatched payload, makes no network call."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    def send(self, *, target: str, subject: str, body: str, config: dict) -> dict:
        self.sent.append({"target": target, "subject": subject, "body": body, "config": config})
        return {"ok": True}


class _FakeResponse:
    status_code = 202


class _FakeHTTPClient:
    """An httpx.Client stand-in that records POSTs instead of performing them."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def post(self, url: str, json: dict) -> _FakeResponse:
        self.calls.append((url, json))
        return _FakeResponse()


# --------------------------------------------------------------------------- #
# render — context, sandbox, missing variable (pure, no DB)
# --------------------------------------------------------------------------- #
def test_render_template_with_context() -> None:
    body = "Policy {{ policy_name }} flagged {{ resource_id }} — {{ count }} resource(s)."
    ctx = {"policy_name": "idle-vms", "resource_id": "/subs/s/vm/1", "count": 3}

    out = service.render(body, ctx)

    assert out == "Policy idle-vms flagged /subs/s/vm/1 — 3 resource(s)."


def test_sandbox_blocks_unsafe_access() -> None:
    # The classic sandbox-escape payload — reach __mro__ → __subclasses__ to break out
    # to arbitrary types — is blocked with a SecurityError; the real class is never
    # reachable (the ``__class__`` hop yields an unsafe Undefined that raises on use).
    with pytest.raises(service.SecurityError):
        service.render("{{ ''.__class__.__mro__[1].__subclasses__() }}", {})


def test_sandbox_blocks_attr_filter_bypass() -> None:
    # jinja2 3.1.6 closes the ``attr()``-filter sandbox bypass (CVE-2025-27516): the
    # filter no longer leaks an unsafe attribute. Pre-fix this rendered "<class 'tuple'>";
    # after the fix the class is unreachable and renders empty.
    assert service.render("{{ ()|attr('__class__') }}", {}) == ""


def test_missing_variable_renders_safely() -> None:
    out = service.render("Hello {{ missing }}!", {})

    assert out == "Hello !"


def test_build_violation_context_exposes_count_and_first_resource() -> None:
    ctx = service.build_violation_context(
        policy_name="idle", resource_type="azure.vm", resource_ids=["/a", "/b"]
    )

    assert ctx["policy_name"] == "idle"
    assert ctx["resource_type"] == "azure.vm"
    assert ctx["count"] == 2
    assert ctx["resource_id"] == "/a"  # convenience: the first matched id
    assert ctx["resource_ids"] == ["/a", "/b"]
    assert ctx["policy"]["name"] == "idle"


def test_build_violation_context_empty_matches() -> None:
    ctx = service.build_violation_context(policy_name="p", resource_ids=[])

    assert ctx["count"] == 0 and ctx["resource_id"] == ""


def test_build_violation_context_merges_extra() -> None:
    ctx = service.build_violation_context(
        policy_name="p", resource_ids=["/a"], extra={"subscription": "sub-1", "count": 99}
    )

    assert ctx["subscription"] == "sub-1"
    assert ctx["count"] == 99  # extra overrides the derived key when provided


# --------------------------------------------------------------------------- #
# WebhookTransport — injectable HTTP client (pure, no DB, no network)
# --------------------------------------------------------------------------- #
def test_webhook_transport_posts_rendered_payload() -> None:
    client = _FakeHTTPClient()
    transport = service.WebhookTransport(client=client)

    res = transport.send(
        target="https://hooks/x", subject="s", body="b", config={"extra": {"channel": "#ops"}}
    )

    assert res["status_code"] == 202 and res["target"] == "https://hooks/x"
    assert client.calls == [("https://hooks/x", {"subject": "s", "body": "b", "channel": "#ops"})]


def test_webhook_transport_handles_empty_config() -> None:
    client = _FakeHTTPClient()

    res = service.WebhookTransport(client=client).send(
        target="https://x", subject="s", body="b", config={}
    )

    assert res["status_code"] == 202
    assert client.calls[0][1] == {"subject": "s", "body": "b"}


# --------------------------------------------------------------------------- #
# notify service — dispatch, disabled channel, unknown refs (DB-backed)
# --------------------------------------------------------------------------- #
def test_service_dispatches_via_transport(db) -> None:
    with session_scope() as s:
        tid = repo.create_notification_template(
            s,
            name="violation",
            subject="[{{ policy_name }}] {{ count }} violation(s)",
            body="Policy {{ policy_name }} matched {{ resource_id }} ({{ count }} total).",
        )["id"]
        cid = repo.create_notification_channel(
            s, name="ops-webhook", transport="webhook", target="https://hooks.example/ops"
        )["id"]
    transport = _RecordingTransport()
    ctx = service.build_violation_context(policy_name="stopped-vms", resource_ids=["/vm/1"])

    with session_scope() as s:
        result = service.notify(
            s, template_id=tid, channel_id=cid, context=ctx, transport=transport
        )

    assert result["dispatched"] is True
    assert len(transport.sent) == 1
    sent = transport.sent[0]
    assert sent["target"] == "https://hooks.example/ops"
    assert "stopped-vms" in sent["subject"] and "1 violation" in sent["subject"]
    assert "/vm/1" in sent["body"] and "(1 total)" in sent["body"]


def test_disabled_channel_not_dispatched(db) -> None:
    with session_scope() as s:
        tid = repo.create_notification_template(s, name="t", body="hi {{ policy_name }}")["id"]
        cid = repo.create_notification_channel(s, name="c", target="https://x", enabled=False)["id"]
    transport = _RecordingTransport()

    with session_scope() as s:
        res = service.notify(
            s, template_id=tid, channel_id=cid, context={"policy_name": "p"}, transport=transport
        )

    assert res["dispatched"] is False
    assert res["body"] == "hi p"  # still rendered — just not sent
    assert transport.sent == []


def test_notify_unknown_template_raises(db) -> None:
    with session_scope() as s:
        cid = repo.create_notification_channel(s, name="c", target="https://x")["id"]

    with session_scope() as s, pytest.raises(service.NotFound):
        service.notify(
            s, template_id=999999, channel_id=cid, context={}, transport=_RecordingTransport()
        )


def test_notify_unknown_channel_raises(db) -> None:
    with session_scope() as s:
        tid = repo.create_notification_template(s, name="t", body="hi")["id"]

    with session_scope() as s, pytest.raises(service.NotFound):
        service.notify(
            s, template_id=tid, channel_id=999999, context={}, transport=_RecordingTransport()
        )


# --------------------------------------------------------------------------- #
# Repository CRUD — channels (required) and templates
# --------------------------------------------------------------------------- #
def test_channel_crud(db) -> None:
    with session_scope() as s:
        ch = repo.create_notification_channel(
            s,
            name="slack-ops",
            transport="slack",
            target="https://hooks.slack/abc",
            config={"channel": "#ops"},
        )
        cid = ch["id"]
    assert ch["name"] == "slack-ops" and ch["transport"] == "slack"
    assert ch["target"] == "https://hooks.slack/abc" and ch["config"] == {"channel": "#ops"}
    assert ch["enabled"] is True

    with session_scope() as s:
        assert [c["id"] for c in repo.list_notification_channels(s)] == [cid]
        assert repo.get_notification_channel(s, cid)["name"] == "slack-ops"

    with session_scope() as s:
        updated = repo.update_notification_channel(
            s, cid, {"enabled": False, "target": "https://new"}
        )
    assert updated["enabled"] is False and updated["target"] == "https://new"

    with session_scope() as s:
        assert repo.delete_notification_channel(s, cid) is True
        assert repo.get_notification_channel(s, cid) is None
        assert repo.delete_notification_channel(s, cid) is False  # idempotent
        assert repo.update_notification_channel(s, cid, {"enabled": True}) is None  # gone


def test_template_crud(db) -> None:
    with session_scope() as s:
        tpl = repo.create_notification_template(
            s,
            name="violation",
            subject="[{{ policy_name }}]",
            body="{{ count }} resources",
            description="policy violation digest",
        )
        tid = tpl["id"]
    assert tpl["name"] == "violation" and tpl["subject"] == "[{{ policy_name }}]"
    assert tpl["body"] == "{{ count }} resources" and tpl["format"] == "text"

    with session_scope() as s:
        assert [t["id"] for t in repo.list_notification_templates(s)] == [tid]
        assert repo.get_notification_template(s, tid)["name"] == "violation"

    with session_scope() as s:
        assert repo.delete_notification_template(s, tid) is True
        assert repo.get_notification_template(s, tid) is None
        assert repo.delete_notification_template(s, tid) is False  # idempotent
