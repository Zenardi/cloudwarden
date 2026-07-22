"""Machine Learning compute-target inventory (workspace child resources).

Azure ML compute instances and clusters live *under* a workspace
(``…/workspaces/<ws>/computes/<compute>``) and are **not** returned by the
Resource Graph ``Resources`` table, so the generic inventory pass in
``azure.inventory`` misses them entirely. This collector enumerates them the only
way the platform exposes them: per workspace, via the ML management SDK.

Each compute becomes a ``ResourceRecord`` of type
``microsoft.machinelearningservices/workspaces/computes`` with a normalized
``config`` (compute_type, vm_size, state, provisioning_state, min/max nodes,
owning workspace) that the idle detectors key off. Cost Management attributes ML
compute spend to the *workspace* resource id, not the compute's own id, so these
records carry no direct cost — the detectors surface them as advisory (the
workspace's monthly cost is attached as evidence context, not as savings).
"""

from __future__ import annotations

import logging
from typing import Any

from ..config import get_settings
from ..models import ResourceRecord
from ..resilience import REGISTRY, with_retry
from .context import SubscriptionContext

logger = logging.getLogger("cloudwarden.azure.ml_compute")

_WORKSPACE_TYPE = "microsoft.machinelearningservices/workspaces"
_COMPUTE_TYPE = "microsoft.machinelearningservices/workspaces/computes"


def _compute_config(as_dict: dict[str, Any], workspace_id: str) -> dict[str, Any]:
    """Flatten an ML ``ComputeResource.as_dict()`` into the fields detectors need.

    The SDK nests the interesting bits two levels deep: the outer ``properties``
    carries ``compute_type``/``provisioning_state``; its inner ``properties``
    carries ``vm_size``/``state``/``scale_settings``/``current_node_count``. We
    hoist them to a flat dict so ``analysis.idle`` doesn't reach through the SDK
    shape (and so the asset row stores something legible).
    """
    outer = as_dict.get("properties") or {}
    inner = outer.get("properties") or {}
    scale = inner.get("scale_settings") or {}
    return {
        "compute_type": outer.get("compute_type"),
        "provisioning_state": outer.get("provisioning_state"),
        # vm_size sits directly on ComputeInstance/AmlCompute props, but some
        # variants tuck it under a further "properties" — check both.
        "vm_size": inner.get("vm_size") or (inner.get("properties") or {}).get("vm_size"),
        "state": inner.get("state"),  # ComputeInstance: Running | Stopped | CreateFailed
        "min_node_count": scale.get("min_node_count"),
        "max_node_count": scale.get("max_node_count"),
        "current_node_count": inner.get("current_node_count"),
        "workspace_id": workspace_id,
    }


def collect_ml_computes(
    resources: list[ResourceRecord],
    client: Any = None,
    subscription: SubscriptionContext | None = None,
) -> list[ResourceRecord]:
    """Return one ``ResourceRecord`` per ML compute under every collected workspace.

    Mock mode returns ``[]`` (no fixtures — the detector is exercised via unit
    tests). Discovers workspaces from the already-collected ``resources`` so it
    needs no second Resource Graph pass. Per-workspace failures are logged and
    skipped so one inaccessible workspace never sinks the batch.
    """
    settings = get_settings()
    if settings.finops_mock:
        return []
    workspaces = [r for r in resources if r.type == _WORKSPACE_TYPE]
    if not workspaces:
        return []
    sub_id = subscription.subscription_id if subscription else settings.azure_subscription_id
    cred = subscription.credential if subscription else None
    return _collect_live(workspaces, sub_id, client, cred)


@with_retry()
def _list_computes(client: Any, resource_group: str, workspace_name: str) -> list[Any]:
    """List one workspace's compute targets (retried on throttling/5xx)."""
    return list(client.compute.list(resource_group, workspace_name))


def _collect_live(
    workspaces: list[ResourceRecord],
    subscription_id: str,
    client: Any = None,
    credential: Any = None,
) -> list[ResourceRecord]:
    from azure.mgmt.machinelearningservices import MachineLearningServicesMgmtClient

    from ..auth import read_credential

    ml = client or MachineLearningServicesMgmtClient(
        credential or read_credential(), subscription_id
    )
    out: list[ResourceRecord] = []
    for ws in workspaces:
        try:
            computes = _list_computes(ml, ws.resource_group, ws.name)
        except Exception:  # noqa: BLE001 - one bad workspace must not sink the batch
            logger.warning("ml compute list failed for workspace %s", ws.name, exc_info=True)
            continue
        for c in computes:
            d = c.as_dict()
            cfg = _compute_config(d, ws.resource_id)
            out.append(
                ResourceRecord(
                    resource_id=str(d.get("id") or "").lower(),
                    name=d.get("name") or "",
                    type=_COMPUTE_TYPE,
                    location=d.get("location") or ws.location,
                    resource_group=ws.resource_group,
                    subscription_id=subscription_id,
                    sku=cfg.get("vm_size"),
                    config=cfg,
                )
            )
    REGISTRY.set("ml_compute", ok=True)
    logger.info(
        "collected %d ml compute target(s) across %d workspace(s)", len(out), len(workspaces)
    )
    return out
