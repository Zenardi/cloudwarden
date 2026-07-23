"""Fire notifications for a binding's violations (M8.4).

Ties the M8.1–M8.3 machinery to bindings. A binding may carry one or more
``binding_notifications`` — each a (channel, template) pair. When a binding run
records a violation (a policy match), :func:`dispatch_for_binding` renders each
paired template from the violation context and dispatches it through the transport
selected by the channel's ``transport`` kind. A binding with **no** attachments
dispatches nothing.

The transport is resolved by :func:`build_transport` (a small registry over the
concrete transports), but every call site can pass a ``transport_factory`` — the
test seam — so unit tests inject a spy and nothing touches the network. Dispatch is
best-effort at the call site (see :mod:`cloudwarden.custodian.bindings`): a failed
notification must never break the enforcement run that triggered it.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..storage import repository as repo
from . import service
from .service import Transport, WebhookTransport
from .transports import (
    EmailTransport,
    JiraTransport,
    ServiceNowTransport,
    SlackTransport,
    TeamsTransport,
)

# Maps a channel's ``transport`` kind to its concrete transport class. Any
# unrecognized kind falls back to the generic webhook transport.
_REGISTRY: dict[str, type] = {
    "webhook": WebhookTransport,
    "slack": SlackTransport,
    "email": EmailTransport,
    "teams": TeamsTransport,
    "jira": JiraTransport,
    "servicenow": ServiceNowTransport,
}

# The transport kinds a channel may declare (used to validate channel input).
KNOWN_TRANSPORTS = frozenset(_REGISTRY)


def build_transport(transport: str) -> Transport:
    """Return a concrete transport for a channel's ``transport`` kind.

    Unknown kinds fall back to :class:`WebhookTransport`. Live callers omit any
    client, so each transport builds its own per ``send`` (see the transport modules).
    """
    return _REGISTRY.get(transport, WebhookTransport)()


def dispatch_for_binding(
    session: Any,
    *,
    binding_id: int,
    policy_name: str,
    resource_ids: list[str],
    resource_type: str | None = None,
    transport_factory: Callable[[str], Transport] | None = None,
) -> list[dict[str, Any]]:
    """Render + dispatch every channel attached to ``binding_id``.

    Returns one result dict per attachment (empty list when the binding has none).
    ``transport_factory`` maps a channel's transport kind to a transport instance;
    it defaults to :func:`build_transport` and is overridden in tests with a spy.
    """
    configs = repo.list_binding_notifications(session, binding_id)
    if not configs:
        return []
    make = transport_factory or build_transport
    context = service.build_violation_context(
        policy_name=policy_name, resource_ids=resource_ids, resource_type=resource_type
    )
    results: list[dict[str, Any]] = []
    for cfg in configs:
        transport = make(cfg["channel_transport"])
        result = service.notify(
            session,
            template_id=cfg["template_id"],
            channel_id=cfg["channel_id"],
            context=context,
            transport=transport,
        )
        results.append({"channel_id": cfg["channel_id"], **result})
    return results


def dispatch_for_budget(
    session: Any,
    *,
    budget: dict[str, Any],
    context: dict[str, Any],
    template_id: int,
    transport_factory: Callable[[str], Transport] | None = None,
) -> dict[str, Any] | None:
    """Render ``template_id`` against ``context`` and send it via the budget's channel.

    Reuses the same transport registry + :func:`service.notify` seam as bindings, so a
    budget alert adds no new delivery code path (M14.2). Returns ``None`` when the
    budget has no channel (or the channel was deleted) — the budget then evaluates
    silently. ``transport_factory`` is the test seam (a spy transport).
    """
    channel_id = budget.get("channel_id")
    if not channel_id:
        return None
    channel = repo.get_notification_channel(session, channel_id)
    if channel is None:
        return None
    make = transport_factory or build_transport
    transport = make(channel["transport"])
    return service.notify(
        session,
        template_id=template_id,
        channel_id=channel_id,
        context=context,
        transport=transport,
    )
