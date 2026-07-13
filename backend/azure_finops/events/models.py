"""Transport model for a normalized Azure Event Grid delivery (M6.1).

Kept separate from the ``event_log`` ORM row (mirroring ``models.py`` vs
``storage/schema.py``): ``NormalizedEvent`` is the internal shape that
``events/ingestion.py`` produces from a raw ``EventGridEvent`` and that later
milestones (event-mode policy triggering, real-time AssetDB updates) consume.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class NormalizedEvent(BaseModel):
    """One resource-change notification, normalized from an Event Grid delivery."""

    event_id: str
    event_type: str
    subject: str
    resource_id: str | None = None
    subscription_id: str | None = None
    resource_type: str | None = None
    operation_name: str | None = None
    event_time: datetime | None = None
    actor: str | None = None
    status: str = "received"
    raw: dict = {}
