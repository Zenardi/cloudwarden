"""Cloud-provider abstraction (M12.1).

Defines the :class:`CloudProvider` interface that the engine, orchestrator and
onboarding talk to instead of Azure directly — the seam that lets AWS/GCP plug
in behind the same governance pipeline via Cloud Custodian. Azure is the only
implementation today (:class:`providers.azure.AzureProvider`); the registry
resolves providers by name so nothing downstream hard-codes a cloud.

Mirrors the existing injectable-``Protocol`` pattern (``custodian.engine`` /
``azure.inventory``): a ``@runtime_checkable`` structural interface, no ABC
inheritance required of implementations.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..azure.context import AccountContext


@runtime_checkable
class CloudProvider(Protocol):
    """The provider seam: everything cloud-specific lives behind this interface.

    ``name`` doubles as the registry key and the Cloud Custodian cloud / resource
    prefix (e.g. ``azure`` → ``azure.vm``).
    """

    name: str

    def register_resources(self) -> None:
        """Register this provider's Cloud Custodian resource types (idempotent)."""

    def resource_registry(self) -> Any:
        """Return the c7n resource registry for this cloud (keys un-prefixed)."""

    def account_context(
        self,
        *,
        account_id: str,
        credential: Any | None = None,
        display_name: str | None = None,
    ) -> AccountContext:
        """Build the per-run account context threaded through the collectors."""

    def default_account_id(self, settings: Any) -> str:
        """Return the default account id for this provider from settings."""

    def build_session(self, account_id: str) -> Any:
        """Build a live Cloud Custodian session for an account (live path only)."""
