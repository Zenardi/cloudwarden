"""Configuration drift detection (M14.7).

The AssetDB already stores each resource's full ``config`` (JSONB) and change history
(``asset_events``). Drift detection turns that into a control: capture a desired-state
**baseline** per resource, diff live config against it each run, and surface
**added / removed / changed** field paths — enriched with the Activity-Log change events
that caused them. Operators re-baseline (accept the drift) or open a remediation.

Design:

* **Baseline** = a *normalized* config snapshot (volatile/noise fields dropped) plus a
  stable hash, versioned on re-baseline — so an unchanged resource never drifts and a
  re-baseline is an explicit, auditable act.
* **Diff** (:func:`diff_config`) is a recursive structural comparison producing dotted
  field paths (``properties.networkAcls.defaultAction``) each classified ``added`` /
  ``removed`` / ``changed``; volatile fields are skipped at every level.
* **Attribution** (:func:`attribute_events`) joins the recent Activity-Log change events
  so a finding says *who/how* changed the resource where that's known.

The pure helpers (:func:`normalize_config`, :func:`config_hash`, :func:`diff_config`,
:func:`attribute_events`) are unit-tested without a database; :func:`detect_drift`
captures baselines on first sight and records classified findings idempotently, and
:func:`capture_baseline` re-baselines (clearing open findings).
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..storage import repository as repo

logger = logging.getLogger("cloudwarden.drift")

# Volatile / noise fields excluded from the baseline and the diff at every nesting level,
# so churn in bookkeeping metadata never registers as configuration drift.
VOLATILE_FIELDS = frozenset(
    {
        "etag",
        "provisioningState",
        "provisioning_state",
        "last_seen",
        "first_seen",
        "lastSeen",
        "firstSeen",
        "timeCreated",
        "createdTime",
        "created_at",
        "changedTime",
        "lastModifiedTime",
        "lastModified",
        "last_modified",
        "updated_at",
        "collected_at",
        "generation",
        "resourceGuid",
    }
)


@dataclass(frozen=True)
class Change:
    """One classified difference between the baseline and the current config."""

    path: str  # dotted field path, e.g. ``properties.networkAcls.defaultAction``
    kind: str  # added | removed | changed
    old: Any = None
    new: Any = None


def normalize_config(config: Any, *, exclude: frozenset[str] = VOLATILE_FIELDS) -> Any:
    """Return ``config`` with volatile keys dropped recursively (a stable baseline shape)."""
    if isinstance(config, dict):
        return {
            key: normalize_config(value, exclude=exclude)
            for key, value in config.items()
            if key not in exclude
        }
    if isinstance(config, list):
        return [normalize_config(value, exclude=exclude) for value in config]
    return config


def config_hash(config: Any, *, exclude: frozenset[str] = VOLATILE_FIELDS) -> str:
    """A stable SHA-256 over the normalized config — equal iff the non-volatile shape is."""
    canonical = json.dumps(normalize_config(config, exclude=exclude), sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def diff_config(
    baseline: Any,
    current: Any,
    *,
    exclude: frozenset[str] = VOLATILE_FIELDS,
    _prefix: str = "",
) -> list[Change]:
    """Recursively diff ``current`` against ``baseline`` → classified :class:`Change`s.

    Yields one change per differing leaf/subtree, with a dotted path; nested dicts recurse
    so a deep change reports the exact field. Volatile fields (:data:`VOLATILE_FIELDS`) are
    skipped at every level, so an only-volatile-changed resource has **no** drift. The
    result is sorted by path for a deterministic, idempotent finding.
    """
    base = baseline if isinstance(baseline, dict) else {}
    curr = current if isinstance(current, dict) else {}
    changes: list[Change] = []
    for key in sorted(set(base) | set(curr)):
        if key in exclude:
            continue
        path = f"{_prefix}{key}"
        in_base, in_curr = key in base, key in curr
        if in_base and not in_curr:
            changes.append(Change(path=path, kind="removed", old=base[key]))
        elif in_curr and not in_base:
            changes.append(Change(path=path, kind="added", new=curr[key]))
        else:
            base_val, curr_val = base[key], curr[key]
            if isinstance(base_val, dict) and isinstance(curr_val, dict):
                changes.extend(diff_config(base_val, curr_val, exclude=exclude, _prefix=f"{path}."))
            elif base_val != curr_val:
                changes.append(Change(path=path, kind="changed", old=base_val, new=curr_val))
    return changes


def attribute_events(events: list[dict[str, Any]], *, top: int = 3) -> list[dict[str, Any]]:
    """The recent *change* events likely responsible for a drift.

    Filters out the ``created`` lifecycle marker (not a change) and returns the most
    recent ``top`` events (the history is already newest-first) so a finding says who/how
    the resource changed, when that's known.
    """
    changes = [e for e in events if e.get("event_type") != "created"]
    return changes[:top]


def _change_dicts(changes: list[Change]) -> list[dict[str, Any]]:
    return [{"path": c.path, "kind": c.kind, "old": c.old, "new": c.new} for c in changes]


def capture_baseline(
    session: Any,
    *,
    resource_id: str,
    config: dict[str, Any],
    provider: str = "azure",
    captured_by: str | None = None,
) -> dict[str, Any]:
    """Capture (or re-capture) a resource's desired-state baseline, clearing its drift.

    Normalizes and hashes ``config``, upserts the baseline (bumping the version only when
    the non-volatile shape changed), and **resolves any open findings** for the resource —
    so a re-baseline is how an operator accepts drift. Returns the baseline row.
    """
    normalized = normalize_config(config)
    baseline, _changed = repo.upsert_drift_baseline(
        session,
        resource_id=resource_id,
        config=normalized,
        config_hash=config_hash(config),
        provider=provider,
        captured_by=captured_by,
    )
    repo.resolve_drift_findings(session, resource_id)
    return baseline


def _resolve(value: Any, settings_value: Any) -> Any:
    return settings_value if value is None else value


def detect_drift(
    session: Any,
    *,
    run_id: str | None = None,
    provider: str | None = None,
    channel_name: str | None = None,
    dispatch_fn: Callable[..., Any] | None = None,
    template_fn: Callable[[Any], int] | None = None,
    settings: Any | None = None,
) -> dict[str, int]:
    """Capture baselines for new assets and record drift for changed ones.

    For each asset: with no baseline yet, snapshot one (first sight → desired state). With
    a baseline, diff the current (normalized) config against it; a non-empty diff becomes a
    finding recorded idempotently (unique on resource + baseline version + change set),
    attributed to the recent change events. A newly recorded finding notifies once through
    the configured channel (best-effort — a transport failure never breaks the run).
    Returns counts: ``assets_scanned``, ``baselines_captured``, ``findings``,
    ``notifications_sent``.
    """
    if settings is None:
        from ..config import get_settings

        settings = get_settings()
    channel_name = _resolve(channel_name, getattr(settings, "drift_alert_channel", ""))

    assets = repo.assets_for_drift(session, provider=provider)
    baselines = {b["resource_id"]: b for b in repo.list_drift_baselines(session, provider=provider)}
    captured = 0
    findings = 0
    notifications = 0
    template_id: int | None = None

    for asset in assets:
        resource_id = asset["resource_id"]
        baseline = baselines.get(resource_id)
        if baseline is None:
            capture_baseline(
                session,
                resource_id=resource_id,
                config=asset["config"],
                provider=asset.get("provider") or "azure",
            )
            captured += 1
            continue

        changes = diff_config(baseline["config"], normalize_config(asset["config"]))
        if not changes:
            continue
        events = attribute_events(repo.get_asset_history(session, resource_id))
        row, inserted = repo.record_drift_finding(
            session,
            resource_id=resource_id,
            provider=asset.get("provider") or "azure",
            baseline_version=baseline["version"],
            changes=_change_dicts(changes),
            events=events,
            run_id=run_id,
        )
        findings += 1
        if not inserted:
            continue

        if not channel_name:
            continue
        if dispatch_fn is None:
            from ..notify.dispatch import dispatch_for_drift

            dispatch_fn = dispatch_for_drift
        if template_id is None:
            template_fn = template_fn or repo.ensure_drift_template
            template_id = template_fn(session)
        context = _drift_context(resource_id, asset, baseline, changes, events)
        try:
            result = dispatch_fn(
                session, context=context, template_id=template_id, channel_name=channel_name
            )
        except Exception:  # noqa: BLE001 - a failed alert must never break detection
            logger.warning("drift %s notification failed", row["id"], exc_info=True)
            result = None
        if result is not None:
            repo.mark_drift_notified(session, row["id"])
            notifications += 1

    return {
        "assets_scanned": len(assets),
        "baselines_captured": captured,
        "findings": findings,
        "notifications_sent": notifications,
    }


def _drift_context(resource_id, asset, baseline, changes, events):
    from ..notify import service

    return service.build_drift_context(
        resource_id=resource_id,
        provider=asset.get("provider") or "azure",
        baseline_version=baseline["version"],
        changes=_change_dicts(changes),
        events=events,
    )
