"""Enterprise transports (M8.3) — Teams, Jira & ServiceNow via injected clients.

Written test-first (TDD). Three concrete
:class:`~cloudwarden.notify.service.Transport` implementations extend the delivery
layer to ITSM / collaboration systems (Stacklet heritage):

* **Teams** POSTs a MessageCard payload to an incoming webhook (like Slack);
* **Jira** creates an issue via ``POST {base}/rest/api/2/issue`` and returns its key;
* **ServiceNow** creates an incident via ``POST {instance}/api/now/table/incident``
  and returns its number.

Each maps the rendered subject/body onto the target artifact's fields, takes an
**injected** HTTP client (so no test touches the network), and — like Slack/email —
**captures** every failure (auth/permission error, non-2xx, exception, missing
config) as ``{"ok": False, "error": ...}`` rather than raising. All conform to the
same ``send(*, target, subject, body, config)`` seam, so they are drop-in for
``notify``.

Invariants (Arrange–Act–Assert), each test one reason to fail. Every test offline.
"""

from __future__ import annotations

from cloudwarden.notify import service
from cloudwarden.notify.transports import (
    JiraTransport,
    ServiceNowTransport,
    TeamsTransport,
)


# --------------------------------------------------------------------------- #
# Test doubles — injected HTTP clients (no network)
# --------------------------------------------------------------------------- #
class _FakeResponse:
    def __init__(self, status_code: int = 200, payload: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> dict:
        return self._payload


class _FakeHTTPClient:
    """Records POSTs (url, json) and returns a canned response."""

    def __init__(self, status_code: int = 200, payload: dict | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._status = status_code
        self._payload = payload or {}

    def post(self, url: str, json: dict) -> _FakeResponse:
        self.calls.append((url, json))
        return _FakeResponse(self._status, self._payload)


class _BoomHTTPClient:
    def post(self, url: str, json: dict):
        raise RuntimeError("connection reset")


# Fake settings carrying server-level endpoints/credentials.
class _Settings:
    teams_webhook_url = ""
    jira_base_url = "https://acme.atlassian.net"
    jira_email = "bot@acme.io"
    jira_api_token = "tok"
    jira_project = "OPS"
    jira_issue_type = "Task"
    servicenow_instance_url = "https://acme.service-now.com"
    servicenow_user = "svc"
    servicenow_password = "pw"


# --------------------------------------------------------------------------- #
# Teams — MessageCard webhook POST
# --------------------------------------------------------------------------- #
def test_teams_posts_message() -> None:
    client = _FakeHTTPClient()
    transport = TeamsTransport(client=client)

    res = transport.send(
        target="https://outlook.office.com/webhook/abc",
        subject="Idle VMs",
        body="3 idle in rg-dev.",
        config={},
    )

    assert res["ok"] is True and res["target"] == "https://outlook.office.com/webhook/abc"
    url, payload = client.calls[0]
    assert url == "https://outlook.office.com/webhook/abc"
    assert payload["@type"] == "MessageCard"
    assert payload["title"] == "Idle VMs" and payload["text"] == "3 idle in rg-dev."
    assert payload["summary"] == "Idle VMs"


def test_teams_falls_back_to_settings_webhook() -> None:
    client = _FakeHTTPClient()

    class _S:
        teams_webhook_url = "https://outlook.office.com/default"

    res = TeamsTransport(client=client, settings=_S()).send(
        target="", subject="s", body="b", config={}
    )

    assert res["ok"] is True and client.calls[0][0] == "https://outlook.office.com/default"


def test_teams_webhook_from_config() -> None:
    client = _FakeHTTPClient()

    res = TeamsTransport(client=client).send(
        target="", subject="s", body="b", config={"webhook_url": "https://outlook.office.com/cfg"}
    )

    assert res["ok"] is True and client.calls[0][0] == "https://outlook.office.com/cfg"


def test_teams_missing_webhook_errors() -> None:
    class _S:
        teams_webhook_url = ""

    res = TeamsTransport(client=_FakeHTTPClient(), settings=_S()).send(
        target="", subject="s", body="b", config={}
    )

    assert res["ok"] is False and "webhook" in res["error"].lower()


# --------------------------------------------------------------------------- #
# Jira — create issue
# --------------------------------------------------------------------------- #
def test_jira_creates_issue() -> None:
    client = _FakeHTTPClient(status_code=201, payload={"key": "OPS-42", "id": "10042"})
    transport = JiraTransport(client=client, settings=_Settings())

    res = transport.send(target="OPS", subject="Idle VM /vm/9", body="Shut it down.", config={})

    assert res["ok"] is True and res["key"] == "OPS-42" and res["id"] == "10042"
    url, payload = client.calls[0]
    assert url == "https://acme.atlassian.net/rest/api/2/issue"
    fields = payload["fields"]
    assert fields["project"] == {"key": "OPS"}
    assert fields["summary"] == "Idle VM /vm/9" and fields["description"] == "Shut it down."
    assert fields["issuetype"] == {"name": "Task"}


def test_jira_project_and_issue_type_from_config() -> None:
    client = _FakeHTTPClient(status_code=201, payload={"key": "SEC-1"})

    JiraTransport(client=client, settings=_Settings()).send(
        target="", subject="s", body="b", config={"project": "SEC", "issue_type": "Bug"}
    )

    _, payload = client.calls[0]
    assert payload["fields"]["project"] == {"key": "SEC"}
    assert payload["fields"]["issuetype"] == {"name": "Bug"}


def test_jira_missing_project_errors() -> None:
    class _S:
        jira_base_url = "https://acme.atlassian.net"
        jira_project = ""
        jira_issue_type = "Task"

    res = JiraTransport(client=_FakeHTTPClient(), settings=_S()).send(
        target="", subject="s", body="b", config={}
    )

    assert res["ok"] is False and "project" in res["error"].lower()


def test_jira_missing_base_url_errors() -> None:
    class _S:
        jira_base_url = ""
        jira_project = "OPS"
        jira_issue_type = "Task"

    res = JiraTransport(client=_FakeHTTPClient(), settings=_S()).send(
        target="OPS", subject="s", body="b", config={}
    )

    assert res["ok"] is False and "base url" in res["error"].lower()


# --------------------------------------------------------------------------- #
# ServiceNow — create incident
# --------------------------------------------------------------------------- #
def test_servicenow_creates_incident() -> None:
    client = _FakeHTTPClient(
        status_code=201, payload={"result": {"number": "INC0012345", "sys_id": "abc123"}}
    )
    transport = ServiceNowTransport(client=client, settings=_Settings())

    res = transport.send(
        target="", subject="Disk full on prod-db", body="80% used.", config={"urgency": "2"}
    )

    assert res["ok"] is True and res["number"] == "INC0012345" and res["sys_id"] == "abc123"
    url, payload = client.calls[0]
    assert url == "https://acme.service-now.com/api/now/table/incident"
    assert payload["short_description"] == "Disk full on prod-db"
    assert payload["description"] == "80% used." and payload["urgency"] == "2"


def test_servicenow_missing_instance_errors() -> None:
    class _S:
        servicenow_instance_url = ""

    res = ServiceNowTransport(client=_FakeHTTPClient(), settings=_S()).send(
        target="", subject="s", body="b", config={}
    )

    assert res["ok"] is False and "instance" in res["error"].lower()


# --------------------------------------------------------------------------- #
# Context maps to the right artifact fields (one assertion set per transport)
# --------------------------------------------------------------------------- #
def test_context_maps_to_fields() -> None:
    subject, body = "Policy idle-vms: 2 hit(s)", "/vm/1, /vm/2 flagged."
    teams = _FakeHTTPClient()
    jira = _FakeHTTPClient(status_code=201, payload={"key": "OPS-1"})
    snow = _FakeHTTPClient(status_code=201, payload={"result": {"number": "INC1"}})

    TeamsTransport(client=teams).send(target="https://wh", subject=subject, body=body, config={})
    JiraTransport(client=jira, settings=_Settings()).send(
        target="OPS", subject=subject, body=body, config={}
    )
    ServiceNowTransport(client=snow, settings=_Settings()).send(
        target="", subject=subject, body=body, config={}
    )

    assert teams.calls[0][1]["title"] == subject and teams.calls[0][1]["text"] == body
    assert jira.calls[0][1]["fields"]["summary"] == subject
    assert jira.calls[0][1]["fields"]["description"] == body
    assert snow.calls[0][1]["short_description"] == subject
    assert snow.calls[0][1]["description"] == body


# --------------------------------------------------------------------------- #
# Auth / permission errors & exceptions — captured, never raised
# --------------------------------------------------------------------------- #
def test_transport_auth_error_returns_error() -> None:
    teams = TeamsTransport(client=_FakeHTTPClient(status_code=403))
    jira = JiraTransport(client=_FakeHTTPClient(status_code=401), settings=_Settings())
    snow = ServiceNowTransport(client=_FakeHTTPClient(status_code=401), settings=_Settings())

    teams_res = teams.send(target="https://wh", subject="s", body="b", config={})
    jira_res = jira.send(target="OPS", subject="s", body="b", config={})
    snow_res = snow.send(target="", subject="s", body="b", config={})

    assert teams_res["ok"] is False and teams_res["status_code"] == 403
    assert jira_res["ok"] is False and jira_res["status_code"] == 401
    assert snow_res["ok"] is False and snow_res["status_code"] == 401


def test_transport_exception_captured() -> None:
    teams = TeamsTransport(client=_BoomHTTPClient()).send(
        target="https://wh", subject="s", body="b", config={}
    )
    jira = JiraTransport(client=_BoomHTTPClient(), settings=_Settings()).send(
        target="OPS", subject="s", body="b", config={}
    )
    snow = ServiceNowTransport(client=_BoomHTTPClient(), settings=_Settings()).send(
        target="", subject="s", body="b", config={}
    )

    for res in (teams, jira, snow):
        assert res["ok"] is False and "connection reset" in res["error"]


# --------------------------------------------------------------------------- #
# Protocol conformance + integration with notify()
# --------------------------------------------------------------------------- #
def test_transports_satisfy_protocol() -> None:
    assert isinstance(TeamsTransport(client=_FakeHTTPClient()), service.Transport)
    assert isinstance(JiraTransport(client=_FakeHTTPClient()), service.Transport)
    assert isinstance(ServiceNowTransport(client=_FakeHTTPClient()), service.Transport)


def test_notify_dispatches_via_teams_transport(db) -> None:
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
            s, name="teams", transport="teams", target="https://outlook.office.com/webhook/x"
        )["id"]
    client = _FakeHTTPClient()
    ctx = service.build_violation_context(policy_name="idle", resource_ids=["/vm/9"])

    with session_scope() as s:
        result = service.notify(
            s,
            template_id=tid,
            channel_id=cid,
            context=ctx,
            transport=TeamsTransport(client=client),
        )

    assert result["dispatched"] is True and result["result"]["ok"] is True
    _, payload = client.calls[0]
    assert "idle" in payload["title"] and "/vm/9" in payload["text"]
