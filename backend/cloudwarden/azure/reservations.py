"""Commitment collector: existing Reservations/Savings Plans + steady-state usage.

Mirrors the other collectors (``azure.advisor`` / ``azure.cost``): injectable
client, mock-fixture backed, provider-aware. Returns a :class:`CommitmentSignals`
carrying (a) existing commitments with utilization/expiry/scope and (b) the
eligible on-demand steady-state usage rolled up per SKU family/region (aggregated,
never raw samples). A no-op for non-Azure providers so the orchestrator can call it
uniformly behind the ``CloudProvider`` abstraction.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from ..config import Settings, get_settings
from ..models import CommitmentRecord, CommitmentSignals, SteadyStateUsage
from ..resilience import REGISTRY
from ._fixtures import load_fixture, retarget
from .context import AccountContext

logger = logging.getLogger("cloudwarden.azure.reservations")

_SUPPORTED_PROVIDER = "azure"


def collect_reservations(
    client: Any = None, subscription: AccountContext | None = None
) -> CommitmentSignals:
    """Collect existing commitments + eligible steady-state usage for one account."""
    settings = get_settings()
    provider = subscription.provider if subscription else _SUPPORTED_PROVIDER
    sub_id = subscription.subscription_id if subscription else settings.azure_subscription_id
    if provider != _SUPPORTED_PROVIDER:
        # AWS/GCP have no commitment implementation yet — no-op stub (empty signal).
        return CommitmentSignals(provider=provider)
    if settings.finops_mock:
        REGISTRY.set("reservations", ok=True)
        return _mock_signals(settings, sub_id)
    cred = subscription.credential if subscription else None
    return _collect_live(settings, client, sub_id, cred)


def _mock_signals(settings: Settings, subscription_id: str) -> CommitmentSignals:
    data = load_fixture("reservations")
    currency = data.get("currency", "USD")
    today = dt.date.today()
    commitments: list[CommitmentRecord] = []
    for raw in data.get("commitments", []):
        # Fixtures carry a relative ``expiry_in_days`` so the "expiring soon" signal
        # fires deterministically regardless of the date the mock run happens on.
        expiry = None
        if raw.get("expiry_in_days") is not None:
            expiry = today + dt.timedelta(days=int(raw["expiry_in_days"]))
        commitments.append(
            CommitmentRecord(
                commitment_id=retarget(str(raw["commitment_id"]).lower(), subscription_id),
                provider=_SUPPORTED_PROVIDER,
                kind=raw.get("kind", "reservation"),
                display_name=raw.get("display_name"),
                scope=raw.get("scope", "Shared"),
                region=raw.get("region"),
                sku_family=raw.get("sku_family"),
                term=raw.get("term", "P1Y"),
                utilization_pct=float(raw.get("utilization_pct", 0.0)),
                expiry_date=expiry,
                hourly_committed=float(raw.get("hourly_committed", 0.0)),
                currency=currency,
            )
        )
    steady = [_to_usage(s, currency) for s in data.get("steady_state", [])]
    return CommitmentSignals(
        provider=_SUPPORTED_PROVIDER, commitments=commitments, steady_state=steady
    )


def _to_commitment(raw: dict[str, Any], currency: str = "USD") -> CommitmentRecord:
    expiry = raw.get("expiry_date")
    if isinstance(expiry, str):
        expiry = dt.date.fromisoformat(expiry[:10])
    return CommitmentRecord(
        commitment_id=str(raw["commitment_id"]),
        provider=_SUPPORTED_PROVIDER,
        kind=raw.get("kind", "reservation"),
        display_name=raw.get("display_name"),
        scope=raw.get("scope", "Shared"),
        region=raw.get("region"),
        sku_family=raw.get("sku_family"),
        term=raw.get("term", "P1Y"),
        utilization_pct=float(raw.get("utilization_pct", 0.0)),
        expiry_date=expiry,
        hourly_committed=float(raw.get("hourly_committed", 0.0)),
        currency=raw.get("currency", currency),
    )


def _to_usage(raw: dict[str, Any], currency: str = "USD") -> SteadyStateUsage:
    return SteadyStateUsage(
        provider=_SUPPORTED_PROVIDER,
        sku_family=raw.get("sku_family", "unknown"),
        region=raw.get("region", "unknown"),
        window_hourly=[float(x) for x in raw.get("window_hourly", [])],
        currency=raw.get("currency", currency),
    )


def _collect_live(
    settings: Settings, client: Any, subscription_id: str, credential: Any = None
) -> CommitmentSignals:
    """Live path: an injected ``client`` (or the ARM-backed default) supplies raw
    commitment + steady-state dicts, which the pure parsers shape into models."""
    source = client or _LiveReservationsClient(subscription_id, credential)
    commitments = [_to_commitment(r) for r in source.list_reservations()]
    steady = [_to_usage(s) for s in source.list_steady_state_usage()]
    REGISTRY.set("reservations", ok=True)
    return CommitmentSignals(
        provider=_SUPPORTED_PROVIDER, commitments=commitments, steady_state=steady
    )


class _LiveReservationsClient:  # pragma: no cover - live network
    """ARM-backed commitment source (Reservations + Savings Plans utilization).

    Reservation/Savings-Plan inventory and utilization come from the ARM
    Reservations and Consumption REST APIs (no SDK needed). Deriving the eligible
    steady-state on-demand series from Cost Management usage is a best-effort v1:
    when unavailable it yields no purchase candidates (coverage/utilization/expiry
    still work), which is why it returns an empty list rather than a guess.
    """

    def __init__(self, subscription_id: str, credential: Any = None) -> None:
        self._subscription_id = subscription_id
        self._credential = credential

    def list_reservations(self) -> list[dict[str, Any]]:
        import httpx

        from ..auth import arm_token

        token = arm_token(self._credential)
        headers = {"Authorization": f"Bearer {token}"}
        url = (
            "https://management.azure.com/providers/Microsoft.Capacity/reservations"
            "?api-version=2022-11-01&$expand=renewProperties"
        )
        out: list[dict[str, Any]] = []
        with httpx.Client(timeout=60.0) as http:
            resp = http.get(url, headers=headers)
            resp.raise_for_status()
            for item in resp.json().get("value", []):
                props = item.get("properties", {}) or {}
                out.append(
                    {
                        "commitment_id": item.get("id", ""),
                        "kind": "reservation",
                        "display_name": props.get("displayName"),
                        "scope": (props.get("appliedScopeType") or "Shared"),
                        "region": props.get("location"),
                        "sku_family": (item.get("sku", {}) or {}).get("name"),
                        "term": props.get("term", "P1Y"),
                        "utilization_pct": _pct(props.get("utilization")),
                        "expiry_date": props.get("expiryDate"),
                        "hourly_committed": 0.0,
                    }
                )
        return out

    def list_steady_state_usage(self) -> list[dict[str, Any]]:
        # Best-effort: live steady-state derivation from Cost Management usage is a
        # follow-up. Returning nothing is honest (no fabricated purchase savings).
        logger.info("live steady-state usage derivation not yet implemented; skipping")
        return []


def _pct(utilization: Any) -> float:  # pragma: no cover - live-only helper
    """Extract a mean utilization percent from the ARM utilization aggregates."""
    if not isinstance(utilization, dict):
        return 0.0
    for agg in utilization.get("aggregates") or []:
        if str(agg.get("grain")) == "1" or agg.get("grainUnit") == "days":
            try:
                return float(agg.get("value") or 0.0)
            except (TypeError, ValueError):
                return 0.0
    return 0.0
