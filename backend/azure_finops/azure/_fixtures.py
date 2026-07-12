"""Load recorded API fixtures for FINOPS_MOCK=1 (offline development / tests)."""

from __future__ import annotations

import json
from importlib import resources
from typing import Any

# The fixture resource ids embed this placeholder subscription. In mock mode we
# rewrite it to the target subscription so multi-subscription runs produce
# distinct (non-colliding) resource ids and cost rows instead of overwriting.
PLACEHOLDER_SUBSCRIPTION = "00000000-0000-0000-0000-000000000000"


def load_fixture(name: str) -> Any:
    ref = resources.files("azure_finops.fixtures").joinpath(f"{name}.json")
    with ref.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def retarget(resource_id: str, subscription_id: str) -> str:
    """Rewrite the placeholder subscription segment of a fixture resource id."""
    if not resource_id or subscription_id == PLACEHOLDER_SUBSCRIPTION:
        return resource_id
    return resource_id.replace(PLACEHOLDER_SUBSCRIPTION, subscription_id)
