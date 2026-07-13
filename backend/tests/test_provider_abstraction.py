"""M12.1 — cloud provider abstraction (behaviour-preserving refactor).

Covers the provider registry, the Azure provider implementing the interface,
the generalized `AccountContext`, and the fact that existing Azure runs and the
`provider` account column keep working (defaulting to ``azure``).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from azure_finops.azure.context import (
    AccountContext,
    SubscriptionContext,
    resolve_account_id,
    resolve_subscription_id,
)
from azure_finops.providers import base, registry
from azure_finops.providers.azure import AzureProvider

# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #


def test_registry_returns_azure_provider() -> None:
    # Act
    provider = registry.get("azure")
    # Assert — resolves the Azure provider, and it is a stable singleton.
    assert provider.name == "azure"
    assert registry.get("azure") is provider


def test_registry_unknown_provider_errors() -> None:
    # Act / Assert — an unregistered cloud fails loudly (not a silent azure default).
    with pytest.raises(registry.UnknownProviderError) as excinfo:
        registry.get("gcp")
    assert "gcp" in str(excinfo.value)


def test_registry_names_lists_azure() -> None:
    assert "azure" in registry.names()


def test_register_custom_provider_roundtrip() -> None:
    # Arrange
    dummy = SimpleNamespace(name="dummy-cloud")
    # Act
    registry.register(dummy)
    try:
        # Assert
        assert registry.get("dummy-cloud") is dummy
    finally:
        registry._PROVIDERS.pop("dummy-cloud", None)


# --------------------------------------------------------------------------- #
# Azure provider implements the interface
# --------------------------------------------------------------------------- #


def test_azure_provider_implements_interface() -> None:
    # Arrange
    provider = registry.get("azure")
    # Assert — structurally satisfies the CloudProvider protocol.
    assert isinstance(provider, base.CloudProvider)
    assert isinstance(provider, AzureProvider)
    for attr in (
        "register_resources",
        "resource_registry",
        "account_context",
        "default_account_id",
        "build_session",
    ):
        assert callable(getattr(provider, attr))


def test_azure_account_context_carries_fields() -> None:
    # Arrange
    provider = registry.get("azure")
    # Act
    ctx = provider.account_context(account_id="sub-1", credential="CRED", display_name="Prod")
    # Assert
    assert ctx.provider == "azure"
    assert ctx.account_id == "sub-1"
    assert ctx.subscription_id == "sub-1"  # azure-flavoured alias
    assert ctx.credential == "CRED"
    assert ctx.display_name == "Prod"


def test_azure_default_account_id_reads_settings() -> None:
    # Arrange
    provider = registry.get("azure")
    settings = SimpleNamespace(azure_subscription_id="sub-default")
    # Act / Assert
    assert provider.default_account_id(settings) == "sub-default"


# --------------------------------------------------------------------------- #
# Generalized AccountContext (back-compat with SubscriptionContext)
# --------------------------------------------------------------------------- #


def test_subscription_context_is_an_account_context() -> None:
    # Act
    ctx = SubscriptionContext(subscription_id="s1", display_name="d")
    # Assert
    assert isinstance(ctx, AccountContext)
    assert ctx.account_id == "s1"
    assert ctx.subscription_id == "s1"
    assert ctx.provider == "azure"


def test_account_context_subscription_id_alias() -> None:
    assert AccountContext(account_id="acc").subscription_id == "acc"


def test_resolve_helpers_return_default_or_id() -> None:
    # resolve_subscription_id (azure alias) and resolve_account_id agree.
    assert resolve_subscription_id(None, "default") == "default"
    assert resolve_subscription_id(SubscriptionContext("abc"), "default") == "abc"
    assert resolve_account_id(None, "default") == "default"
    assert resolve_account_id(AccountContext(account_id="xyz"), "default") == "xyz"


# --------------------------------------------------------------------------- #
# Orchestrator routes through the provider
# --------------------------------------------------------------------------- #


def test_context_from_record_uses_provider() -> None:
    import azure_finops.orchestrator as orch

    rec = SimpleNamespace(
        subscription_id="s9",
        display_name="Nine",
        tenant_id=None,
        client_id=None,
        client_secret=None,
        provider="azure",
    )
    ctx = orch._context_from_record(rec, mock=True)
    assert ctx.subscription_id == "s9"
    assert ctx.provider == "azure"
    assert ctx.credential is None


def test_existing_run_still_works(db) -> None:
    from azure_finops.orchestrator import run_pipeline

    provider = registry.get("azure")
    ctx = provider.account_context(account_id="sub-run", display_name="Run")
    out = run_pipeline(mock=True, subscription=ctx)
    assert out["subscription_id"] == "sub-run"
    assert out["run_id"].startswith("run_")
    assert out["counts"]["resources"] > 0  # the pipeline actually collected & stored


# --------------------------------------------------------------------------- #
# The `provider` account column defaults to azure
# --------------------------------------------------------------------------- #


def test_account_defaults_provider_azure(db) -> None:
    from azure_finops.storage import repository as repo
    from azure_finops.storage.db import session_scope

    with session_scope() as session:
        public = repo.upsert_subscription(
            session, subscription_id="acc-1", display_name="Account 1"
        )
    # Public serialization advertises the provider …
    assert public["provider"] == "azure"
    # … and the stored row defaults to azure at the DB level.
    with session_scope() as session:
        rec = repo.get_subscription(session, "acc-1")
        assert rec.provider == "azure"


def test_upsert_subscription_accepts_explicit_provider(db) -> None:
    from azure_finops.storage import repository as repo
    from azure_finops.storage.db import session_scope

    with session_scope() as session:
        public = repo.upsert_subscription(
            session, subscription_id="aws-1", display_name="AWS 1", provider="aws"
        )
    assert public["provider"] == "aws"
