"""Azure cloud provider (M12.1).

Wraps the Azure-specific Cloud Custodian integration — resource registration,
the c7n resource registry, and live session construction — plus the per-run
account context, behind the :class:`providers.base.CloudProvider` interface.
Behaviour is identical to the pre-abstraction engine: this is a pure move so
the existing Azure test suite stays green.
"""

from __future__ import annotations

from typing import Any

from ..azure.context import AccountContext


class AzureProvider:
    """:class:`CloudProvider` implementation for Microsoft Azure."""

    name = "azure"

    def __init__(self) -> None:
        # c7n_azure registers ~110 resource types the first time its entry module
        # is imported; this guard ensures that happens exactly once per process.
        self._registered = False

    def register_resources(self) -> None:
        """Import ``c7n_azure.entry`` once so ``azure.*`` resource types register."""
        if self._registered:
            return
        import c7n_azure.entry  # noqa: F401 - side-effecting: registers azure.* resources
        from c7n.resources import load_resources

        load_resources(("azure.*",))
        self._registered = True

    def resource_registry(self) -> Any:
        """Return the c7n Azure resource registry (keys un-prefixed, e.g. ``vm``)."""
        from c7n.provider import clouds

        return clouds[self.name].resources

    def account_context(
        self,
        *,
        account_id: str,
        credential: Any | None = None,
        display_name: str | None = None,
    ) -> AccountContext:
        """Build an Azure per-run account context (an ``AccountContext``)."""
        return AccountContext(
            account_id=account_id,
            provider=self.name,
            credential=credential,
            display_name=display_name,
        )

    def default_account_id(self, settings: Any) -> str:
        """The default Azure subscription id from settings."""
        return settings.azure_subscription_id

    def build_session(self, account_id: str) -> Any:  # pragma: no cover - live network
        """Build a live c7n Azure session for a subscription."""
        from c7n_azure.session import Session

        return Session(subscription_id=account_id)

    def preventive_translator(self) -> Any:
        """The Azure Policy translator for preventive guardrails (M14.10)."""
        from .preventive import azure_policy

        return azure_policy
