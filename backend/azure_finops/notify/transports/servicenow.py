"""ServiceNow transport — create an incident via the Table API.

Creates an incident with ``POST {instance_url}/api/now/table/incident``, mapping the
rendered subject → **short_description** and body → **description**. Optional incident
fields (``urgency``, ``impact``, ``assignment_group``, ``caller_id`` …) are copied
straight from channel config when present. The instance URL and credentials come from
:class:`Settings`; the HTTP client is **injectable** — live callers omit it and one
carrying HTTP basic auth is built per send. Any failure — a missing instance URL, an
auth or permission error (non-2xx), or a network exception — is captured as
``{"ok": False, "error": ...}``, never raised.
"""

from __future__ import annotations

from typing import Any

from ...config import Settings, get_settings

# Optional incident fields copied from channel config when present.
_PASSTHROUGH_FIELDS = ("urgency", "impact", "assignment_group", "caller_id", "category")


class ServiceNowTransport:
    """Create a ServiceNow incident from a rendered notification."""

    def __init__(self, client: Any = None, settings: Settings | None = None) -> None:
        self._client = client
        self._settings = settings

    def _settings_or_default(self) -> Settings:
        return self._settings or get_settings()

    def _build_client(self) -> tuple[Any, bool]:  # pragma: no cover - live path only
        import httpx

        settings = self._settings_or_default()
        auth = (settings.servicenow_user or "", settings.servicenow_password or "")
        return httpx.Client(timeout=10.0, auth=auth), True

    def send(self, *, target: str, subject: str, body: str, config: dict) -> dict[str, Any]:
        config = config or {}
        settings = self._settings_or_default()
        instance = (
            config.get("instance_url") or getattr(settings, "servicenow_instance_url", "") or ""
        ).rstrip("/")
        if not instance:
            return {"ok": False, "error": "servicenow: no instance url configured"}

        url = f"{instance}/api/now/table/incident"
        payload: dict[str, Any] = {"short_description": subject, "description": body}
        for field in _PASSTHROUGH_FIELDS:
            if config.get(field):
                payload[field] = config[field]

        client = self._client
        close = False
        if client is None:  # pragma: no cover - live path builds a real client
            client, close = self._build_client()
        try:
            resp = client.post(url, json=payload)
        except Exception as exc:  # network / client error — capture, never raise
            return {"ok": False, "error": f"servicenow transport error: {exc}", "target": url}
        finally:
            if close:  # pragma: no cover - live path only
                client.close()

        status = getattr(resp, "status_code", None)
        if status is not None and status >= 400:
            return {
                "ok": False,
                "error": f"servicenow responded {status}",
                "status_code": status,
                "target": url,
            }
        result = (resp.json() or {}).get("result", {})
        return {
            "ok": True,
            "number": result.get("number"),
            "sys_id": result.get("sys_id"),
            "status_code": status,
            "target": url,
        }
