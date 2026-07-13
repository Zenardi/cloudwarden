"""Render a communication template and dispatch it through a pluggable transport.

Two seams, both testable fully offline:

* **Rendering** (:func:`render`) runs template source through a Jinja2
  :class:`~jinja2.sandbox.SandboxedEnvironment` — unsafe attribute access (dunders,
  the ``attr`` filter bypass) raises :class:`~jinja2.exceptions.SecurityError`, and a
  missing variable renders as an **empty string** (default ``Undefined``) rather than
  crashing. An authored template can reference the violation context but never reach
  Python internals.
* **Dispatch** (:class:`Transport`) is an injectable protocol — the c7n-mailer heritage.
  :func:`notify` loads a persisted template + channel, renders subject/body, and hands
  the rendered payload to the injected transport. :class:`WebhookTransport` is a
  concrete transport whose HTTP client is itself injectable, so tests never touch the
  network.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol, runtime_checkable

from jinja2 import Undefined
from jinja2.exceptions import SecurityError
from jinja2.sandbox import SandboxedEnvironment
from sqlalchemy.orm import Session

from ..storage import schema

logger = logging.getLogger("azure_finops.notify")

__all__ = [
    "SecurityError",
    "Transport",
    "WebhookTransport",
    "NotFound",
    "render",
    "build_violation_context",
    "notify",
]

# One sandboxed environment shared by every render. ``SandboxedEnvironment`` blocks
# access to unsafe attributes (dunders, internals) and the default ``Undefined``
# renders a missing variable as "" instead of raising. ``autoescape=False`` because
# notifications are plain text / markdown (Slack, email), not HTML.
_env = SandboxedEnvironment(undefined=Undefined, autoescape=False)


class NotFound(Exception):
    """A referenced notification template or channel does not exist."""


@runtime_checkable
class Transport(Protocol):
    """The pluggable dispatch seam — injected, never imported by the caller.

    A transport turns a rendered message into a delivery. Unit tests inject a spy;
    live code injects a concrete transport (e.g. :class:`WebhookTransport`).
    """

    def send(self, *, target: str, subject: str, body: str, config: dict) -> dict[str, Any]:
        """Deliver a rendered message to ``target``; return a structured result."""


def render(template_str: str, context: dict[str, Any]) -> str:
    """Render ``template_str`` against ``context`` in the sandboxed environment.

    Unsafe attribute access raises :class:`~jinja2.exceptions.SecurityError`; a
    missing variable renders as an empty string, never a crash.
    """
    return _env.from_string(template_str).render(**context)


def build_violation_context(
    *,
    policy_name: str,
    resource_ids: list[str],
    resource_type: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble the standard template context for a policy violation.

    Exposes the policy name, the matched resource ids (plus a convenience
    ``resource_id`` — the first — and a ``count``), and merges any ``extra`` keys.
    """
    ids = list(resource_ids)
    context: dict[str, Any] = {
        "policy": {"name": policy_name, "resource_type": resource_type},
        "policy_name": policy_name,
        "resource_type": resource_type,
        "resources": [{"id": rid} for rid in ids],
        "resource_ids": ids,
        "resource_id": ids[0] if ids else "",
        "count": len(ids),
    }
    if extra:
        context.update(extra)
    return context


def notify(
    session: Session,
    *,
    template_id: int,
    channel_id: int,
    context: dict[str, Any],
    transport: Transport,
) -> dict[str, Any]:
    """Render ``template_id`` against ``context`` and dispatch it via ``channel_id``.

    Loads the template + channel, renders subject and body in the sandbox, then hands
    the rendered payload to the injected ``transport``. A **disabled** channel is
    skipped (``dispatched=False``) without calling the transport. Raises
    :class:`NotFound` for an unknown template or channel.
    """
    template = session.get(schema.NotificationTemplate, template_id)
    if template is None:
        raise NotFound(f"notification template {template_id} not found")
    channel = session.get(schema.NotificationChannel, channel_id)
    if channel is None:
        raise NotFound(f"notification channel {channel_id} not found")

    subject = render(template.subject or "", context)
    body = render(template.body, context)

    if not channel.enabled:
        logger.info("channel %s disabled; rendered but not dispatched", channel.name)
        return {
            "channel": channel.name,
            "subject": subject,
            "body": body,
            "dispatched": False,
            "result": None,
        }

    result = transport.send(
        target=channel.target, subject=subject, body=body, config=channel.config or {}
    )
    return {
        "channel": channel.name,
        "subject": subject,
        "body": body,
        "dispatched": True,
        "result": result,
    }


class WebhookTransport:
    """Deliver a rendered message as a JSON POST to the channel target.

    The HTTP client is **injectable** (the test seam): callers may pass any object
    with a ``post(url, json=...)`` method; live callers omit it and one is built per
    ``send``. Extra channel config under ``config["extra"]`` is merged into the JSON
    payload (e.g. a Slack channel override).
    """

    def __init__(self, client: Any = None) -> None:
        self._client = client

    def send(self, *, target: str, subject: str, body: str, config: dict) -> dict[str, Any]:
        payload = {"subject": subject, "body": body, **(config.get("extra") or {})}
        client = self._client
        close = False
        if client is None:  # pragma: no cover - live path builds a real client
            import httpx

            client = httpx.Client(timeout=10.0)
            close = True
        try:
            resp = client.post(target, json=payload)
            return {"status_code": getattr(resp, "status_code", None), "target": target}
        finally:
            if close:  # pragma: no cover - live path only
                client.close()
