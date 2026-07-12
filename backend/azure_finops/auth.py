"""Azure credential factories.

`DefaultAzureCredential` resolves in order: env-var service principal
(AZURE_CLIENT_ID/SECRET/TENANT_ID), Managed Identity, then Azure CLI. We build a
read-only credential for collection and a separate write-scoped credential for
remediation (distinct env vars) so least-privilege holds by construction.
azure-identity is imported lazily so the package still imports in mock mode
without the SDK installed.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from .config import get_settings

ARM_SCOPE = "https://management.azure.com/.default"


def _make_credential(
    tenant_id: str | None, client_id: str | None, client_secret: str | None
) -> Any:
    from azure.identity import ClientSecretCredential, DefaultAzureCredential

    if tenant_id and client_id and client_secret:
        return ClientSecretCredential(tenant_id, client_id, client_secret)
    return DefaultAzureCredential()


@lru_cache
def read_credential() -> Any:
    s = get_settings()
    return _make_credential(s.azure_tenant_id, s.azure_client_id, s.azure_client_secret)


@lru_cache
def write_credential() -> Any:
    s = get_settings()
    return _make_credential(
        s.azure_remediation_tenant_id or s.azure_tenant_id,
        s.azure_remediation_client_id,
        s.azure_remediation_client_secret,
    )


def credential_for(tenant_id: str | None, client_id: str | None, client_secret: str | None) -> Any:
    """Per-subscription credential: a dedicated SP when creds are supplied,
    otherwise the shared read SP (env / Managed Identity / CLI). The tenant falls
    back to the env tenant so only client_id/secret are strictly required."""
    if client_id and client_secret:
        s = get_settings()
        return _make_credential(tenant_id or s.azure_tenant_id, client_id, client_secret)
    return read_credential()


def arm_token(credential: Any = None) -> str:
    cred = credential or read_credential()
    return cred.get_token(ARM_SCOPE).token
