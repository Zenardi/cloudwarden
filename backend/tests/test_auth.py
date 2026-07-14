"""Credential factories (constructed, never authenticated)."""

from __future__ import annotations

from cloudwarden import auth
from cloudwarden.config import get_settings


def _clear() -> None:
    get_settings.cache_clear()
    auth.read_credential.cache_clear()
    auth.write_credential.cache_clear()


def test_make_credential_service_principal() -> None:
    cred = auth._make_credential("tid", "cid", "secret")
    assert type(cred).__name__ == "ClientSecretCredential"


def test_make_credential_default() -> None:
    cred = auth._make_credential(None, None, None)
    assert type(cred).__name__ == "DefaultAzureCredential"


def test_read_credential_uses_sp(monkeypatch) -> None:
    monkeypatch.setenv("AZURE_TENANT_ID", "t")
    monkeypatch.setenv("AZURE_CLIENT_ID", "c")
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "s")
    _clear()
    assert type(auth.read_credential()).__name__ == "ClientSecretCredential"
    _clear()


def test_write_credential_default(monkeypatch) -> None:
    monkeypatch.delenv("AZURE_REMEDIATION_CLIENT_ID", raising=False)
    monkeypatch.delenv("AZURE_CLIENT_ID", raising=False)
    _clear()
    assert type(auth.write_credential()).__name__ == "DefaultAzureCredential"
    _clear()


def test_arm_token_with_fake_credential() -> None:
    class _Tok:
        token = "abc"

    class _Cred:
        def get_token(self, scope):
            return _Tok()

    assert auth.arm_token(_Cred()) == "abc"
