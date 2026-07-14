"""Per-run cloud-account context threaded through the collectors.

Generalized in M12.1 from an Azure-only ``SubscriptionContext`` to a provider-
neutral :class:`AccountContext` (provider + account id + optional pre-built
credential). ``SubscriptionContext`` is retained as the Azure-flavoured alias so
the existing collectors keep working unchanged — ``subscription_id`` maps onto
``account_id``. When ``credential`` is None the collectors fall back to the
shared read SP, so single-account/mock flows keep working unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class AccountContext:
    """A generalized per-run cloud-account context.

    ``account_id`` is the provider-native account identifier (an Azure
    subscription id, an AWS account id, a GCP project id). ``provider`` names the
    owning cloud so downstream code can resolve provider-specific behaviour, and
    ``credential`` is an optional pre-built credential object.
    """

    account_id: str
    provider: str = "azure"
    credential: Any | None = None
    display_name: str | None = None

    @property
    def subscription_id(self) -> str:
        """Azure-flavoured alias for ``account_id`` (backward compatibility)."""
        return self.account_id


class SubscriptionContext(AccountContext):
    """Azure subscription context — an :class:`AccountContext` keyed by subscription id.

    Retained for backward compatibility; new code should prefer
    :class:`AccountContext` (or build one via
    ``providers.azure.AzureProvider.account_context``). The ``subscription_id``
    constructor keyword maps onto ``account_id``.
    """

    def __init__(
        self,
        subscription_id: str,
        credential: Any | None = None,
        display_name: str | None = None,
    ) -> None:
        super().__init__(
            account_id=subscription_id,
            provider="azure",
            credential=credential,
            display_name=display_name,
        )


def resolve_account_id(account: AccountContext | None, default: str) -> str:
    """Return the context's account id, or ``default`` when no context is given."""
    return account.account_id if account is not None else default


def resolve_subscription_id(subscription: AccountContext | None, default: str) -> str:
    """Azure-flavoured alias of :func:`resolve_account_id` (backward compatibility)."""
    return resolve_account_id(subscription, default)
