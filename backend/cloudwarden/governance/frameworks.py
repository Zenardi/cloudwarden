"""Compliance framework overlays & auditor evidence export (M14.13).

A *framework overlay* is a versioned YAML under ``packs/frameworks/`` mapping each
control of a compliance framework (SOC 2, ISO 27001, PCI DSS, NIST 800-53) to
zero-or-more of CloudWarden's existing policies — a thin layer *over* the policy
library, not a new set of policies.

Per-control posture rolls up the mapped policies' **latest** results
(``v_governance_posture`` via :func:`repository.policy_posture_by_name`):

* ``compliant``      — every mapped policy has run and matched nothing;
* ``non_compliant``  — at least one mapped policy currently flags a resource;
* ``not_evaluated``  — mapped, but some mapped policy has never run (never green
  by omission);
* ``gap``            — the control maps to **no** policy at all (an honest
  coverage gap, never counted compliant).

The evidence bundle exports control → policy → matched resources → status with the
run timestamps an auditor asks for, and **reconciles** with posture (the same
per-control status decides both). Loading is offline (pure YAML); posture and
evidence take a DB session.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from ..storage import repository as repo

# Overlays live alongside the policy packs so they version like a pack (M10).
FRAMEWORKS_DIR = Path(__file__).resolve().parent.parent / "packs" / "frameworks"

_EXTS = {".yml", ".yaml"}

# Per-control posture states.
COMPLIANT = "compliant"
NON_COMPLIANT = "non_compliant"
NOT_EVALUATED = "not_evaluated"
GAP = "gap"

# Flat column order for the streamed evidence export (CSV header / JSON keys).
EVIDENCE_COLUMNS = (
    "framework",
    "framework_version",
    "control_id",
    "control_title",
    "control_status",
    "policy_name",
    "policy_status",
    "resources_matched",
    "last_execution_at",
    "is_gap",
    "generated_at",
)


# --------------------------------------------------------------------------- #
# Loading (offline — no DB)
# --------------------------------------------------------------------------- #
def _dir(frameworks_dir: Path | None) -> Path:
    return frameworks_dir if frameworks_dir is not None else FRAMEWORKS_DIR


def _normalize(data: dict[str, Any]) -> dict[str, Any]:
    """Coerce a parsed overlay into a stable shape (ids/policies stringified)."""
    controls = [
        {
            "id": str(raw.get("id", "")),
            "title": raw.get("title") or "",
            "description": raw.get("description") or "",
            "policies": [str(p) for p in (raw.get("policies") or [])],
        }
        for raw in (data.get("controls") or [])
    ]
    return {
        "name": data["name"],
        "version": str(data.get("version") or ""),
        "title": data.get("title") or data["name"],
        "description": data.get("description") or "",
        "controls": controls,
    }


def load_framework(framework_id: str, frameworks_dir: Path | None = None) -> dict[str, Any] | None:
    """Parse a single framework overlay by id, or ``None`` if unknown/malformed."""
    directory = _dir(frameworks_dir)
    if not directory.is_dir():
        return None
    for ext in (".yaml", ".yml"):
        path = directory / f"{framework_id}{ext}"
        if path.is_file():
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("name"):
                return _normalize(data)
    return None


def _all(frameworks_dir: Path | None = None) -> list[dict[str, Any]]:
    directory = _dir(frameworks_dir)
    if not directory.is_dir():
        return []
    out: dict[str, dict[str, Any]] = {}
    for path in sorted(directory.iterdir()):
        if path.suffix.lower() not in _EXTS:
            continue
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("name"):
            out[data["name"]] = _normalize(data)
    return [out[name] for name in sorted(out)]


def _summary(fwk: dict[str, Any]) -> dict[str, Any]:
    controls = fwk["controls"]
    gaps = sum(1 for c in controls if not c["policies"])
    return {
        "name": fwk["name"],
        "version": fwk["version"],
        "title": fwk["title"],
        "description": fwk["description"],
        "control_count": len(controls),
        "mapped_count": len(controls) - gaps,
        "gap_count": gaps,
    }


def list_frameworks(frameworks_dir: Path | None = None) -> list[dict[str, Any]]:
    """List discoverable overlays (name/version/title/control & gap counts), sorted."""
    return [_summary(f) for f in _all(frameworks_dir)]


# --------------------------------------------------------------------------- #
# Posture rollup + gap detection
# --------------------------------------------------------------------------- #
def _posture_by_name(session) -> dict[str, dict[str, Any]]:
    return {r["policy_name"]: r for r in repo.policy_posture_by_name(session)}


def _control_status(
    control: dict[str, Any], by_name: dict[str, dict[str, Any]]
) -> tuple[str, list[dict[str, Any]], int, Any]:
    """Roll a control's mapped policies up to a status + per-policy detail.

    Returns ``(status, policies, resources_matched, last_execution_at)``.
    """
    names = control["policies"]
    policies: list[dict[str, Any]] = []
    any_non_compliant = False
    all_evaluated = True
    resources = 0
    last: Any = None
    for name in names:
        p = by_name.get(name)
        evaluated = p is not None
        non_compliant = bool(p and int(p["non_compliant"]) > 0)
        matched = int(p["resources_matched"]) if p else 0
        executed_at = p["last_execution_at"] if p else None
        policies.append(
            {
                "policy_name": name,
                "evaluated": evaluated,
                "status": (NON_COMPLIANT if non_compliant else COMPLIANT)
                if evaluated
                else NOT_EVALUATED,
                "resources_matched": matched,
                "last_execution_at": executed_at,
            }
        )
        any_non_compliant = any_non_compliant or non_compliant
        all_evaluated = all_evaluated and evaluated
        resources += matched
        if executed_at is not None and (last is None or executed_at > last):
            last = executed_at

    if not names:
        status = GAP
    elif any_non_compliant:
        status = NON_COMPLIANT
    elif all_evaluated:
        status = COMPLIANT
    else:
        status = NOT_EVALUATED
    return status, policies, resources, last


def _control_view(control: dict[str, Any], by_name: dict[str, dict[str, Any]]) -> dict[str, Any]:
    status, policies, resources, last = _control_status(control, by_name)
    return {
        "id": control["id"],
        "title": control["title"],
        "description": control["description"],
        "status": status,
        "gap": not control["policies"],
        "mapped_policy_count": len(control["policies"]),
        "evaluated_policies": sum(1 for p in policies if p["evaluated"]),
        "resources_matched": resources,
        "last_execution_at": last,
        "policies": policies,
    }


def _totals(controls: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {COMPLIANT: 0, NON_COMPLIANT: 0, NOT_EVALUATED: 0, GAP: 0}
    for c in controls:
        counts[c["status"]] += 1
    total = len(controls)
    mapped = sum(1 for c in controls if not c["gap"])
    return {
        "controls": total,
        "compliant": counts[COMPLIANT],
        "non_compliant": counts[NON_COMPLIANT],
        "not_evaluated": counts[NOT_EVALUATED],
        "gap": counts[GAP],
        "mapped": mapped,
        "unmapped": total - mapped,
        "coverage": round(mapped / total, 4) if total else 0.0,
    }


def framework_posture(
    session, framework_id: str, *, frameworks_dir: Path | None = None
) -> dict[str, Any] | None:
    """Per-control posture for a framework, or ``None`` if the framework is unknown.

    Returns ``{framework, version, title, controls: [...], totals: {...}}``. Each
    control carries its rolled-up ``status`` (compliant / non_compliant /
    not_evaluated / gap), the mapped policies and their individual statuses.
    """
    fwk = load_framework(framework_id, frameworks_dir)
    if fwk is None:
        return None
    by_name = _posture_by_name(session)
    controls = [_control_view(c, by_name) for c in fwk["controls"]]
    return {
        "framework": fwk["name"],
        "version": fwk["version"],
        "title": fwk["title"],
        "controls": controls,
        "totals": _totals(controls),
    }


# --------------------------------------------------------------------------- #
# Evidence bundle + flat export rows
# --------------------------------------------------------------------------- #
def _stamp(generated_at: Any) -> str:
    if generated_at is None:
        return datetime.now(UTC).isoformat()
    if isinstance(generated_at, datetime):
        return generated_at.isoformat()
    return str(generated_at)


def _policy_matches(session, name: str) -> list[dict[str, Any]]:
    """The resources a policy currently flags (its latest per-subscription matches)."""
    policy = repo.get_policy_by_name(session, name)
    if policy is None:
        return []
    return repo.policy_matched_resources(session, policy["id"])


def evidence_bundle(
    session,
    framework_id: str,
    *,
    frameworks_dir: Path | None = None,
    generated_at: Any = None,
) -> dict[str, Any] | None:
    """Auditor evidence bundle: control → policy → matched resources → status + times.

    Timestamped at the bundle level (``generated_at``) and per policy
    (``last_execution_at``). Each control's ``status`` is computed identically to
    :func:`framework_posture`, so the bundle **reconciles** with posture. ``None``
    for an unknown framework.
    """
    fwk = load_framework(framework_id, frameworks_dir)
    if fwk is None:
        return None
    by_name = _posture_by_name(session)
    views = [_control_view(c, by_name) for c in fwk["controls"]]
    controls = []
    for view in views:
        policies = [
            {**p, "matches": _policy_matches(session, p["policy_name"])} for p in view["policies"]
        ]
        controls.append({**view, "policies": policies})
    return {
        "framework": fwk["name"],
        "version": fwk["version"],
        "title": fwk["title"],
        "generated_at": _stamp(generated_at),
        "controls": controls,
        "totals": _totals(views),
    }


def evidence_rows(
    session,
    framework_id: str,
    *,
    frameworks_dir: Path | None = None,
    generated_at: Any = None,
) -> list[dict[str, Any]]:
    """Flatten the evidence bundle to one row per control-policy for CSV/JSON export.

    A gap control (no mapped policy) still emits a single row flagged ``is_gap`` so
    coverage gaps appear in the export, never silently absent. Returns ``[]`` for an
    unknown framework.
    """
    fwk = load_framework(framework_id, frameworks_dir)
    if fwk is None:
        return []
    by_name = _posture_by_name(session)
    stamp = _stamp(generated_at)
    rows: list[dict[str, Any]] = []
    for control in fwk["controls"]:
        status, policies, _resources, _last = _control_status(control, by_name)
        base = {
            "framework": fwk["name"],
            "framework_version": fwk["version"],
            "control_id": control["id"],
            "control_title": control["title"],
            "control_status": status,
            "is_gap": not control["policies"],
            "generated_at": stamp,
        }
        if not policies:
            rows.append(
                {
                    **base,
                    "policy_name": "",
                    "policy_status": status,
                    "resources_matched": 0,
                    "last_execution_at": None,
                }
            )
            continue
        for pol in policies:
            rows.append(
                {
                    **base,
                    "policy_name": pol["policy_name"],
                    "policy_status": pol["status"],
                    "resources_matched": pol["resources_matched"],
                    "last_execution_at": pol["last_execution_at"],
                }
            )
    return rows
