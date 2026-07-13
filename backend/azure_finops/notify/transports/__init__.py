"""Concrete notification transports (M8.2) — Slack (webhook) and email (SMTP).

Each transport implements the :class:`azure_finops.notify.service.Transport` seam —
``send(*, target, subject, body, config)`` — turning a *rendered* message into a
delivery. Both take an **injectable** client (an HTTP client for Slack, an SMTP
client for email) so unit tests never touch the network, and both **capture**
delivery failures as ``{"ok": False, "error": ...}`` rather than raising — a failed
notification must never break the policy run that triggered it.
"""

from .email import EmailTransport
from .slack import SlackTransport

__all__ = ["SlackTransport", "EmailTransport"]
