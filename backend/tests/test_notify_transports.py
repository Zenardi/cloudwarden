"""Slack & email transports (M8.2) — concrete delivery via injected clients.

Written test-first (TDD). Two concrete :class:`~cloudwarden.notify.service.Transport`
implementations — Slack (webhook POST) and email (SMTP) — turn a *rendered* message
into a delivery. The HTTP client and the SMTP client are **injected**, so no test
ever touches the network. Both conform to the same ``send(*, target, subject, body,
config)`` seam that :func:`cloudwarden.notify.service.notify` dispatches through.

Invariants (Arrange–Act–Assert), each test one reason to fail:

* Slack POSTs the rendered message as a Slack payload to the configured webhook;
* the Slack payload has the expected shape (``text`` + optional ``channel`` override);
* email sends via the injected SMTP client with the correct to / subject / body / from;
* a transport whose client raises returns ``{"ok": False, "error": ...}`` — never raises;
* a channel missing its required config (no webhook / no recipient) returns a
  validation error, not a crash;
* both transports fall back to config.py defaults (Slack webhook, SMTP from);
* both satisfy the ``Transport`` protocol and integrate with ``notify``.

Every test is offline. The pure-transport tests need no database; the integration
test uses the ``db`` fixture.
"""

from __future__ import annotations

from cloudwarden.notify import service
from cloudwarden.notify.transports import EmailTransport, SlackTransport


# --------------------------------------------------------------------------- #
# Test doubles — injected HTTP / SMTP clients (no network, no smtplib)
# --------------------------------------------------------------------------- #
class _FakeResponse:
    def __init__(self, status_code: int = 200) -> None:
        self.status_code = status_code


class _FakeHTTPClient:
    """An httpx.Client stand-in: records POSTs, returns a canned response."""

    def __init__(self, status_code: int = 200) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._status = status_code

    def post(self, url: str, json: dict) -> _FakeResponse:
        self.calls.append((url, json))
        return _FakeResponse(self._status)


class _BoomHTTPClient:
    """An HTTP client whose POST always fails (simulates a network error)."""

    def post(self, url: str, json: dict):
        raise RuntimeError("connection refused")


class _FakeSMTP:
    """An smtplib.SMTP stand-in: records the sent message, performs no I/O."""

    def __init__(self) -> None:
        self.sent: list = []

    def send_message(self, msg) -> dict:
        self.sent.append(msg)
        return {}


class _BoomSMTP:
    """An SMTP client whose send always fails (simulates the server being down)."""

    def send_message(self, msg):
        raise OSError("smtp unreachable")


# --------------------------------------------------------------------------- #
# Slack transport
# --------------------------------------------------------------------------- #
def test_slack_posts_to_webhook() -> None:
    client = _FakeHTTPClient()
    transport = SlackTransport(client=client)

    res = transport.send(
        target="https://hooks.slack.com/services/T/B/x",
        subject="Idle VMs",
        body="3 VMs idle in rg-dev.",
        config={},
    )

    assert res["ok"] is True
    assert res["target"] == "https://hooks.slack.com/services/T/B/x"
    assert len(client.calls) == 1
    url, payload = client.calls[0]
    assert url == "https://hooks.slack.com/services/T/B/x"
    assert "Idle VMs" in payload["text"] and "3 VMs idle in rg-dev." in payload["text"]


def test_slack_payload_shape() -> None:
    client = _FakeHTTPClient()
    transport = SlackTransport(client=client)

    transport.send(
        target="https://hooks.slack.com/x",
        subject="Alert",
        body="body text",
        config={"channel": "#ops", "username": "finops-bot"},
    )

    _, payload = client.calls[0]
    assert payload == {"text": "*Alert*\nbody text", "channel": "#ops", "username": "finops-bot"}


def test_slack_body_only_when_no_subject() -> None:
    client = _FakeHTTPClient()

    SlackTransport(client=client).send(target="https://x", subject="", body="just body", config={})

    _, payload = client.calls[0]
    assert payload == {"text": "just body"}


def test_slack_non_2xx_response_is_failure() -> None:
    client = _FakeHTTPClient(status_code=500)

    res = SlackTransport(client=client).send(target="https://x", subject="s", body="b", config={})

    assert res["ok"] is False
    assert "500" in res["error"] and res["status_code"] == 500


def test_slack_webhook_from_config() -> None:
    client = _FakeHTTPClient()

    res = SlackTransport(client=client).send(
        target="", subject="s", body="b", config={"webhook_url": "https://hooks.slack/from-cfg"}
    )

    assert res["ok"] is True
    assert client.calls[0][0] == "https://hooks.slack/from-cfg"


def test_slack_falls_back_to_settings_webhook() -> None:
    client = _FakeHTTPClient()

    class _S:
        slack_webhook_url = "https://hooks.slack.com/default"

    res = SlackTransport(client=client, settings=_S()).send(
        target="", subject="s", body="b", config={}
    )

    assert res["ok"] is True
    assert client.calls[0][0] == "https://hooks.slack.com/default"


# --------------------------------------------------------------------------- #
# Email transport
# --------------------------------------------------------------------------- #
def test_email_sends_via_smtp() -> None:
    smtp = _FakeSMTP()
    transport = EmailTransport(client=smtp)

    res = transport.send(
        target="ops@example.com",
        subject="Disk full",
        body="Server X is full.",
        config={"from": "finops@corp.com"},
    )

    assert res["ok"] is True and res["target"] == "ops@example.com"
    assert len(smtp.sent) == 1
    msg = smtp.sent[0]
    assert msg["To"] == "ops@example.com"
    assert msg["Subject"] == "Disk full"
    assert msg["From"] == "finops@corp.com"
    assert msg.get_content().strip() == "Server X is full."


def test_email_falls_back_to_settings_from() -> None:
    smtp = _FakeSMTP()

    class _S:
        smtp_from = "noreply@finops.local"

    res = EmailTransport(client=smtp, settings=_S()).send(
        target="ops@example.com", subject="s", body="b", config={}
    )

    assert res["ok"] is True
    assert smtp.sent[0]["From"] == "noreply@finops.local"


def test_email_recipient_from_config_target() -> None:
    smtp = _FakeSMTP()

    EmailTransport(client=smtp).send(
        target="", subject="s", body="b", config={"to": "team@example.com"}
    )

    assert smtp.sent[0]["To"] == "team@example.com"


# --------------------------------------------------------------------------- #
# Failure & missing-config — errors captured, never raised
# --------------------------------------------------------------------------- #
def test_transport_failure_returns_error() -> None:
    slack = SlackTransport(client=_BoomHTTPClient())
    email = EmailTransport(client=_BoomSMTP())

    slack_res = slack.send(target="https://x", subject="s", body="b", config={})
    email_res = email.send(target="ops@example.com", subject="s", body="b", config={})

    assert slack_res["ok"] is False and "connection refused" in slack_res["error"]
    assert email_res["ok"] is False and "smtp unreachable" in email_res["error"]


def test_missing_channel_config_errors() -> None:
    class _S:
        slack_webhook_url = ""
        smtp_from = "finops@localhost"

    slack_res = SlackTransport(client=_FakeHTTPClient(), settings=_S()).send(
        target="", subject="s", body="b", config={}
    )
    email_res = EmailTransport(client=_FakeSMTP(), settings=_S()).send(
        target="", subject="s", body="b", config={}
    )

    assert slack_res["ok"] is False and "webhook" in slack_res["error"].lower()
    assert email_res["ok"] is False and "recipient" in email_res["error"].lower()


# --------------------------------------------------------------------------- #
# Protocol conformance + integration with notify()
# --------------------------------------------------------------------------- #
def test_transports_satisfy_protocol() -> None:
    assert isinstance(SlackTransport(client=_FakeHTTPClient()), service.Transport)
    assert isinstance(EmailTransport(client=_FakeSMTP()), service.Transport)


def test_notify_dispatches_via_slack_transport(db) -> None:
    from cloudwarden.storage import repository as repo
    from cloudwarden.storage.db import session_scope

    with session_scope() as s:
        tid = repo.create_notification_template(
            s,
            name="viol",
            subject="[{{ policy_name }}] {{ count }} hit(s)",
            body="{{ resource_id }} flagged.",
        )["id"]
        cid = repo.create_notification_channel(
            s, name="slack", transport="slack", target="https://hooks.slack/ops"
        )["id"]
    client = _FakeHTTPClient()
    ctx = service.build_violation_context(policy_name="idle", resource_ids=["/vm/9"])

    with session_scope() as s:
        result = service.notify(
            s,
            template_id=tid,
            channel_id=cid,
            context=ctx,
            transport=SlackTransport(client=client),
        )

    assert result["dispatched"] is True and result["result"]["ok"] is True
    _, payload = client.calls[0]
    assert "idle" in payload["text"] and "/vm/9" in payload["text"]
