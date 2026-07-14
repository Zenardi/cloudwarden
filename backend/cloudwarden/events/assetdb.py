"""Real-time AssetDB updates from events (M6.3) — streaming inventory.

Every accepted Event Grid delivery (M6.1) is reflected into the AssetDB (M4.1) so the
queryable inventory tracks *who / how / when* near-instantly, the way Stacklet streams
resource state instead of waiting for the next poll. One resource-change event:

* **upserts** the ``assets`` row (identity + refreshed ``last_seen``; a delete marks
  ``state='deleted'``) — see :func:`repository.upsert_asset_from_event`;
* **appends** an ``asset_event`` to the audit trail carrying the event's actor,
  operation, status and timestamp.

An event without a ``resource_id`` is ignored (no write). This is intentionally
separate from event-mode *policy* triggering (``custodian.eventmode``): one keeps the
inventory current, the other enforces governance — both fed by the same delivery.
"""

from __future__ import annotations

import logging
from typing import Any

from ..storage import repository as repo
from ..storage.db import init_db, session_scope

logger = logging.getLogger("cloudwarden.events.assetdb")

_DELETE_EVENT_TYPE = "Microsoft.Resources.ResourceDeleteSuccess"


def apply_asset_event(event: Any) -> dict[str, Any] | None:
    """Reflect one normalized event into the AssetDB; return a ``{resource_id, lifecycle}``
    summary, or ``None`` when the event carries no ``resource_id`` (ignored, no write).

    ``lifecycle`` is ``deleted`` for a delete event, ``created`` when the asset was seen
    for the first time, else ``updated``.
    """
    resource_id = getattr(event, "resource_id", None)
    if not resource_id:
        return None

    init_db()
    with session_scope() as session:
        inserted = repo.upsert_asset_from_event(session, event)
        if event.event_type == _DELETE_EVENT_TYPE:
            lifecycle = "deleted"
        else:
            lifecycle = "created" if inserted else "updated"
        repo.append_asset_event(
            session,
            resource_id=resource_id,
            subscription_id=event.subscription_id,
            event_type=lifecycle,
            data={
                "actor": event.actor,
                "operation": event.operation_name,
                "status": event.status,
                "event_id": event.event_id,
                "event_type": event.event_type,
                "event_time": event.event_time.isoformat() if event.event_time else None,
            },
        )
    logger.info("assetdb %s %s", lifecycle, resource_id)
    return {"resource_id": resource_id, "lifecycle": lifecycle}
