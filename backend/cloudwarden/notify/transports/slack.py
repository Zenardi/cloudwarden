"""Slack transport — POST a rendered message to an incoming webhook.

The HTTP client is **injectable** (the test seam): pass any object exposing
``post(url, json=...)``; live callers omit it and one ``httpx.Client`` is built per
send. The webhook URL comes from the channel ``target`` (falling back to
``config["webhook_url"]`` then :attr:`Settings.slack_webhook_url`); a missing URL is a
validation error, not a crash. Every delivery failure — a network error or a non-2xx
webhook response — is captured as ``{"ok": False, "error": ...}`` and returned, never
raised.
"""

from __future__ import annotations

from typing import Any

from ...config import Settings, get_settings

# Optional Slack payload fields copied straight from channel config when present
# (e.g. override the target channel or the bot's display name / icon).
_PASSTHROUGH_KEYS = ("channel", "username", "icon_emoji", "icon_url")


class SlackTransport:
    """Deliver a rendered message to a Slack incoming webhook."""

    def __init__(self, client: Any = None, settings: Settings | None = None) -> None:
        self._client = client
        self._settings = settings

    def _webhook_url(self, target: str, config: dict) -> str:
        if target:
            return target
        if config.get("webhook_url"):
            return str(config["webhook_url"])
        settings = self._settings or get_settings()
        return getattr(settings, "slack_webhook_url", "") or ""

    def send(self, *, target: str, subject: str, body: str, config: dict) -> dict[str, Any]:
        config = config or {}
        url = self._webhook_url(target, config)
        if not url:
            return {"ok": False, "error": "slack: no webhook url configured"}

        text = f"*{subject}*\n{body}" if subject else body
        payload: dict[str, Any] = {"text": text}
        for key in _PASSTHROUGH_KEYS:
            if config.get(key):
                payload[key] = config[key]

        client = self._client
        close = False
        if client is None:  # pragma: no cover - live path builds a real client
            import httpx

            client = httpx.Client(timeout=10.0)
            close = True
        try:
            resp = client.post(url, json=payload)
        except Exception as exc:  # network / client error — capture, never raise
            return {"ok": False, "error": f"slack transport error: {exc}", "target": url}
        finally:
            if close:  # pragma: no cover - live path only
                client.close()

        status = getattr(resp, "status_code", None)
        if status is not None and status >= 400:
            return {
                "ok": False,
                "error": f"slack webhook responded {status}",
                "status_code": status,
                "target": url,
            }
        return {"ok": True, "status_code": status, "target": url}
