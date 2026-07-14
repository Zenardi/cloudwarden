"""SKU pricing and downsize-target selection.

Ships a small static catalog for the common Dsv5 family (enough for the mock and
a sensible offline default). The live path can override via the Azure Retail
Prices API (`prices.azure.com/api/retail/prices`, no auth) — added when needed;
static values keep the rules deterministic and testable.
"""

from __future__ import annotations

# family ordered small -> large: (name, vcpu, ram_gb, hourly_usd_linux)
_DSV5 = [
    ("Standard_D2s_v5", 2, 8, 0.096),
    ("Standard_D4s_v5", 4, 16, 0.192),
    ("Standard_D8s_v5", 8, 32, 0.384),
    ("Standard_D16s_v5", 16, 64, 0.768),
]

_CATALOG = {name: (vcpu, ram, price) for name, vcpu, ram, price in _DSV5}
_ORDER = [name for name, *_ in _DSV5]

HOURS_PER_MONTH = 730.0


def vm_hourly_price(sku: str | None) -> float | None:
    entry = _CATALOG.get(sku or "")
    return entry[2] if entry else None


def vm_monthly_price(sku: str | None) -> float | None:
    hourly = vm_hourly_price(sku)
    return hourly * HOURS_PER_MONTH if hourly is not None else None


def smaller_sku(sku: str | None) -> str | None:
    """Return the next-smaller SKU in the same family, or None."""
    if sku in _ORDER:
        index = _ORDER.index(sku)
        return _ORDER[index - 1] if index > 0 else None
    return None
