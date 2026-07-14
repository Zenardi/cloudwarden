"""Lightweight per-subscription connectivity check.

Verifies that a subscription's credential can acquire an ARM token AND that the
service principal can actually see the subscription (a GET on the subscription
resource). Mock mode short-circuits with an informational result and makes no
network call. The ``http`` client is injectable for offline testing.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from ..config import get_settings

logger = logging.getLogger("cloudwarden.azure.connectivity")

_SUB_URL = "https://management.azure.com/subscriptions/{sid}?api-version=2022-12-01"


def check_connection(
    subscription_id: str, credential: Any = None, http: Any = None
) -> dict[str, Any]:
    """Return {ok, message, ...}. Never raises — failures come back as ok=False."""
    if get_settings().finops_mock:
        return {
            "ok": True,
            "mock": True,
            "message": "Mock mode — credentials are not verified against Azure.",
        }

    from ..auth import arm_token, read_credential

    try:
        token = arm_token(credential or read_credential())
    except Exception as exc:  # noqa: BLE001 - surfaced to the UI, not raised
        return {"ok": False, "message": f"Token acquisition failed: {exc}"}

    url = _SUB_URL.format(sid=subscription_id)
    headers = {"Authorization": f"Bearer {token}"}
    try:
        if http is not None:
            resp = http.get(url, headers=headers)
        else:
            with httpx.Client(timeout=15.0) as client:
                resp = client.get(url, headers=headers)
    except Exception as exc:  # noqa: BLE001 - network errors surfaced to the UI
        return {"ok": False, "message": f"Request failed: {exc}"}

    if resp.status_code == 200:
        data = resp.json()
        return {
            "ok": True,
            "message": "Connected",
            "subscription_name": data.get("displayName"),
            "state": data.get("state"),
        }
    if resp.status_code in (401, 403):
        return {
            "ok": False,
            "message": (
                f"Access denied ({resp.status_code}) — check the service principal's "
                "role assignment on this subscription."
            ),
        }
    if resp.status_code == 404:
        return {"ok": False, "message": "Subscription not found (404) — check the subscription id."}
    return {"ok": False, "message": f"Azure returned HTTP {resp.status_code}."}
