"""Per-run subscription context threaded through the collectors.

Carries the target subscription id and an optional pre-built credential (a
per-subscription ``ClientSecretCredential``). When ``credential`` is None the
collectors fall back to the shared read SP, so single-subscription/mock flows
keep working unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class SubscriptionContext:
    subscription_id: str
    credential: Any | None = None
    display_name: str | None = None


def resolve_subscription_id(subscription: SubscriptionContext | None, default: str) -> str:
    return subscription.subscription_id if subscription is not None else default
