"""Cloud-provider abstraction package (M12.1).

Importing this package registers the built-in providers (Azure today) so that
``providers.registry.get("azure")`` resolves without any explicit bootstrap.
Additional clouds (AWS/GCP) register the same way once implemented.
"""

from __future__ import annotations

from . import base, registry
from .azure import AzureProvider

registry.register(AzureProvider())

__all__ = ["AzureProvider", "base", "registry"]
