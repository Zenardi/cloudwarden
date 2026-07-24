"""Offline Kubernetes fixture loading + shared id helpers (M14.12)."""

from __future__ import annotations

import json
from importlib import resources
from typing import Any


def load_k8s_fixture(name: str) -> Any:
    """Load ``cloudwarden/fixtures/k8s/<name>.json`` via importlib.resources."""
    ref = resources.files("cloudwarden.fixtures.k8s").joinpath(f"{name}.json")
    with ref.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def cluster_key(cluster_id: str) -> str:
    """Trailing cluster-name segment of a cluster id — stable under account
    retargeting (only the account/subscription/project id is rewritten, never the
    cluster name), so the fixture clients can match workloads/usage to a cluster
    whether or not the id was retargeted to an onboarded account."""
    return (cluster_id or "").rsplit("/", 1)[-1]
