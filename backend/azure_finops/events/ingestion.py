"""Azure Event Grid ingestion boundary (M6.1).

Three pure, fixture-testable functions the ``POST /api/events/azure`` endpoint composes:

* :func:`verify_event_grid_key` — constant-time shared-key check (mock-friendly: no key
  configured ⇒ accept all);
* :func:`handle_subscription_validation` — completes Event Grid's one-time handshake;
* :func:`normalize_event` — maps a raw resource-change ``EventGridEvent`` to a
  :class:`NormalizedEvent`, or ``None`` for an unrecognized ``eventType``.

Event Grid delivers plain JSON over HTTP, so nothing here touches the Azure SDK.
"""

from __future__ import annotations

import hmac
from datetime import UTC, datetime
from typing import Any

from ..config import Settings
from .models import NormalizedEvent

# Resource-change event types we normalize (Azure activity-log categories via Event Grid).
_SUPPORTED_EVENT_TYPES = {
    "Microsoft.Resources.ResourceWriteSuccess",
    "Microsoft.Resources.ResourceActionSuccess",
    "Microsoft.Resources.ResourceDeleteSuccess",
}
_VALIDATION_EVENT_TYPE = "Microsoft.EventGrid.SubscriptionValidationEvent"
_KEY_HEADER = "x-events-key"
_KEY_QUERY = "key"


def verify_event_grid_key(
    headers: dict[str, Any], query: dict[str, Any], settings: Settings
) -> bool:
    """Authenticate a delivery against ``settings.azure_eventgrid_shared_key``.

    Returns ``True`` when no key is configured (mock-mode friendly). Otherwise the
    delivery must present the key via the ``x-events-key`` header or ``?key=`` query
    param; the compare is constant-time.
    """
    configured = settings.azure_eventgrid_shared_key
    if not configured:
        return True
    lowered = {str(k).lower(): v for k, v in headers.items()}
    provided = lowered.get(_KEY_HEADER) or query.get(_KEY_QUERY)
    if not provided:
        return False
    return hmac.compare_digest(str(provided), str(configured))


def handle_subscription_validation(events: list[Any]) -> dict[str, Any] | None:
    """Detect Event Grid's one-time validation event and echo its ``validationCode``."""
    for event in events:
        if isinstance(event, dict) and event.get("eventType") == _VALIDATION_EVENT_TYPE:
            code = (event.get("data") or {}).get("validationCode")
            return {"validationResponse": code}
    return None


def _resource_type_from_operation(operation_name: str | None) -> str | None:
    """``Microsoft.Compute/virtualMachines/write`` → ``microsoft.compute/virtualmachines``."""
    if not operation_name:
        return None
    parts = operation_name.split("/")
    if len(parts) < 3:
        return None
    return "/".join(parts[:-1]).lower()


def _parse_event_time(value: Any) -> datetime | None:
    """Parse an Event Grid ISO-8601 timestamp (7-digit fractional + ``Z``) to tz-aware."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    # Azure emits 100-ns (7-digit) precision; datetime.fromisoformat only takes 6.
    if "." in text:
        head, _, tail = text.partition(".")
        digits = ""
        idx = 0
        while idx < len(tail) and tail[idx].isdigit():
            digits += tail[idx]
            idx += 1
        text = f"{head}.{digits[:6]}{tail[idx:]}"  # trim fraction, keep the tz offset
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def normalize_event(raw: Any) -> NormalizedEvent | None:
    """Map a raw resource-change ``EventGridEvent`` to a :class:`NormalizedEvent`.

    Returns ``None`` for a non-dict, an event missing its ``id``, or an unrecognized
    ``eventType`` (so the caller simply skips it).
    """
    if not isinstance(raw, dict):
        return None
    event_type = raw.get("eventType")
    if event_type not in _SUPPORTED_EVENT_TYPES:
        return None
    event_id = raw.get("id")
    if not event_id:
        return None
    data = raw.get("data") or {}
    resource_uri = data.get("resourceUri") or raw.get("subject") or ""
    operation_name = data.get("operationName")
    claims = data.get("claims") or {}
    return NormalizedEvent(
        event_id=str(event_id),
        event_type=str(event_type),
        subject=str(raw.get("subject") or ""),
        resource_id=(str(resource_uri).lower() or None),
        subscription_id=data.get("subscriptionId"),
        resource_type=_resource_type_from_operation(operation_name),
        operation_name=operation_name,
        event_time=_parse_event_time(raw.get("eventTime")),
        actor=claims.get("name"),
        status=data.get("status") or "received",
        raw=raw,
    )
