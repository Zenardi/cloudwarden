"""Email transport — send a rendered message via SMTP.

The SMTP client is **injectable** (the test seam): pass any object exposing
``send_message(msg)`` (the :class:`smtplib.SMTP` interface); live callers omit it and
one is built per send from :class:`Settings`. The recipient comes from the channel
``target`` (falling back to ``config["to"]``); the sender from ``config["from"]``
then :attr:`Settings.smtp_from`. A missing recipient is a validation error, and any
send failure is captured as ``{"ok": False, "error": ...}`` rather than raised — a
broken mail server must never break the policy run that triggered the notification.
"""

from __future__ import annotations

from email.message import EmailMessage
from typing import Any

from ...config import Settings, get_settings


class EmailTransport:
    """Deliver a rendered message as an email over SMTP."""

    def __init__(self, client: Any = None, settings: Settings | None = None) -> None:
        self._client = client
        self._settings = settings

    def _settings_or_default(self) -> Settings:
        return self._settings or get_settings()

    def _build_client(self) -> tuple[Any, bool]:  # pragma: no cover - live path only
        import smtplib

        settings = self._settings_or_default()
        client = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10.0)
        if getattr(settings, "smtp_use_tls", False):
            client.starttls()
        if getattr(settings, "smtp_username", None):
            client.login(settings.smtp_username, settings.smtp_password or "")
        return client, True

    def send(self, *, target: str, subject: str, body: str, config: dict) -> dict[str, Any]:
        config = config or {}
        to = target or config.get("to") or ""
        if not to:
            return {"ok": False, "error": "email: no recipient configured"}
        from_addr = config.get("from") or self._settings_or_default().smtp_from

        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = from_addr
        msg["To"] = to
        msg.set_content(body)

        client = self._client
        close = False
        if client is None:  # pragma: no cover - live path builds a real client
            client, close = self._build_client()
        try:
            client.send_message(msg)
        except Exception as exc:  # SMTP / connection error — capture, never raise
            return {"ok": False, "error": f"email transport error: {exc}", "target": to}
        finally:
            if close:  # pragma: no cover - live path only
                client.quit()

        return {"ok": True, "target": to, "subject": subject}
