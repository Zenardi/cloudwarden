"""Concrete notification transports — the delivery layer.

Each transport implements the :class:`cloudwarden.notify.service.Transport` seam —
``send(*, target, subject, body, config)`` — turning a *rendered* message into a
delivery, and takes an **injectable** client so unit tests never touch the network.
All **capture** delivery failures as ``{"ok": False, "error": ...}`` rather than
raising — a failed notification must never break the policy run that triggered it.

* M8.2 — :class:`SlackTransport` (webhook), :class:`EmailTransport` (SMTP);
* M8.3 — :class:`TeamsTransport` (webhook), :class:`JiraTransport` (create issue),
  :class:`ServiceNowTransport` (create incident) — the ITSM integrations.
"""

from .email import EmailTransport
from .jira import JiraTransport
from .servicenow import ServiceNowTransport
from .slack import SlackTransport
from .teams import TeamsTransport

__all__ = [
    "SlackTransport",
    "EmailTransport",
    "TeamsTransport",
    "JiraTransport",
    "ServiceNowTransport",
]
