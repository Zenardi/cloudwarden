"""Microsoft Teams transport — POST a MessageCard to an incoming webhook.

Like Slack, Teams delivery is a webhook POST; the HTTP client is **injectable** (the
test seam). The webhook URL comes from the channel ``target`` (falling back to
``config["webhook_url"]`` then :attr:`Settings.teams_webhook_url`). The rendered
subject/body map onto a legacy MessageCard (``title`` / ``text``) — the payload every
Teams connector accepts. Every delivery failure — a network error or a non-2xx
response — is captured as ``{"ok": False, "error": ...}`` and returned, never raised.
"""

from __future__ import annotations

from typing import Any

from ...config import Settings, get_settings

_DEFAULT_THEME_COLOR = "0076D7"


class TeamsTransport:
    """Deliver a rendered message to a Microsoft Teams incoming webhook."""

    def __init__(self, client: Any = None, settings: Settings | None = None) -> None:
        self._client = client
        self._settings = settings

    def _webhook_url(self, target: str, config: dict) -> str:
        if target:
            return target
        if config.get("webhook_url"):
            return str(config["webhook_url"])
        settings = self._settings or get_settings()
        return getattr(settings, "teams_webhook_url", "") or ""

    def send(self, *, target: str, subject: str, body: str, config: dict) -> dict[str, Any]:
        config = config or {}
        url = self._webhook_url(target, config)
        if not url:
            return {"ok": False, "error": "teams: no webhook url configured"}

        payload = {
            "@type": "MessageCard",
            "@context": "http://schema.org/extensions",
            "summary": subject or "Notification",
            "themeColor": config.get("theme_color") or _DEFAULT_THEME_COLOR,
            "title": subject,
            "text": body,
        }

        client = self._client
        close = False
        if client is None:  # pragma: no cover - live path builds a real client
            import httpx

            client = httpx.Client(timeout=10.0)
            close = True
        try:
            resp = client.post(url, json=payload)
        except Exception as exc:  # network / client error — capture, never raise
            return {"ok": False, "error": f"teams transport error: {exc}", "target": url}
        finally:
            if close:  # pragma: no cover - live path only
                client.close()

        status = getattr(resp, "status_code", None)
        if status is not None and status >= 400:
            return {
                "ok": False,
                "error": f"teams webhook responded {status}",
                "status_code": status,
                "target": url,
            }
        return {"ok": True, "status_code": status, "target": url}
