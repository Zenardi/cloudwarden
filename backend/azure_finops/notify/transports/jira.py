"""Jira transport — create an issue via the Jira REST API.

Creates an issue with ``POST {base_url}/rest/api/2/issue``, mapping the rendered
subject → issue **summary** and body → **description**. The base URL, credentials,
default project and issue type come from :class:`Settings` (a single Jira instance,
many projects); the channel ``target`` (falling back to ``config["project"]`` then
:attr:`Settings.jira_project`) selects the project. The HTTP client is **injectable**
— live callers omit it and one carrying HTTP basic auth is built per send. Any
failure — a missing project / base URL, an auth or permission error (non-2xx), or a
network exception — is captured as ``{"ok": False, "error": ...}``, never raised.
"""

from __future__ import annotations

from typing import Any

from ...config import Settings, get_settings


class JiraTransport:
    """Create a Jira issue from a rendered notification."""

    def __init__(self, client: Any = None, settings: Settings | None = None) -> None:
        self._client = client
        self._settings = settings

    def _settings_or_default(self) -> Settings:
        return self._settings or get_settings()

    def _build_client(self) -> tuple[Any, bool]:  # pragma: no cover - live path only
        import httpx

        settings = self._settings_or_default()
        auth = (settings.jira_email or "", settings.jira_api_token or "")
        return httpx.Client(timeout=10.0, auth=auth), True

    def send(self, *, target: str, subject: str, body: str, config: dict) -> dict[str, Any]:
        config = config or {}
        settings = self._settings_or_default()
        base_url = (config.get("base_url") or settings.jira_base_url or "").rstrip("/")
        if not base_url:
            return {"ok": False, "error": "jira: no base url configured"}
        project = target or config.get("project") or getattr(settings, "jira_project", "")
        if not project:
            return {"ok": False, "error": "jira: no project configured"}
        issue_type = config.get("issue_type") or getattr(settings, "jira_issue_type", "Task")

        url = f"{base_url}/rest/api/2/issue"
        payload = {
            "fields": {
                "project": {"key": project},
                "summary": subject,
                "description": body,
                "issuetype": {"name": issue_type},
            }
        }

        client = self._client
        close = False
        if client is None:  # pragma: no cover - live path builds a real client
            client, close = self._build_client()
        try:
            resp = client.post(url, json=payload)
        except Exception as exc:  # network / client error — capture, never raise
            return {"ok": False, "error": f"jira transport error: {exc}", "target": url}
        finally:
            if close:  # pragma: no cover - live path only
                client.close()

        status = getattr(resp, "status_code", None)
        if status is not None and status >= 400:
            return {
                "ok": False,
                "error": f"jira responded {status}",
                "status_code": status,
                "target": url,
            }
        data = resp.json() or {}
        return {
            "ok": True,
            "key": data.get("key"),
            "id": data.get("id"),
            "status_code": status,
            "target": url,
        }
