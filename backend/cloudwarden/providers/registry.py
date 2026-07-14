"""Provider registry: resolve a :class:`CloudProvider` by name.

The built-in providers self-register at import time (see ``providers/__init__``).
:func:`get` raises :class:`UnknownProviderError` for an unregistered name so
multi-cloud callers fail loudly instead of silently defaulting to Azure.
"""

from __future__ import annotations

from .base import CloudProvider

_PROVIDERS: dict[str, CloudProvider] = {}


class UnknownProviderError(LookupError):
    """Raised when no provider is registered under the requested name."""


def register(provider: CloudProvider) -> None:
    """Register (or replace) a provider under its ``name``."""
    _PROVIDERS[provider.name] = provider


def get(name: str) -> CloudProvider:
    """Return the provider registered under ``name``.

    Raises :class:`UnknownProviderError` (naming the known providers) when the
    name is not registered.
    """
    try:
        return _PROVIDERS[name]
    except KeyError:
        known = ", ".join(sorted(_PROVIDERS)) or "(none)"
        raise UnknownProviderError(
            f"unknown cloud provider: {name!r} (registered: {known})"
        ) from None


def names() -> list[str]:
    """Return the sorted names of all registered providers."""
    return sorted(_PROVIDERS)
