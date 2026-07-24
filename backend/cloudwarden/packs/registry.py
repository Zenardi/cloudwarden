"""Policy-pack registry — discover bundled packs and install them (M10.1, M10.2).

A *policy pack* is a curated, versioned bundle of Cloud Custodian policies shipped
as YAML under this package. Two layouts are supported:

* **single-file** (M10.1) — one ``<name>.yaml`` under ``packs/`` declaring ``name`` /
  ``version`` / ``description`` and an inline ``policies`` list (each entry the shape
  of one ``policies:`` item);
* **directory** (M10.2) — a ``packs/<slug>/`` folder with a ``pack.yaml`` manifest
  (metadata + a ``policies`` enumeration of ``{name, description}``) plus one or more
  sibling ``*.yml`` files holding the actual c7n specs. The registry assembles the
  manifest metadata with the specs loaded from the files.

The registry:

* :func:`list_packs` / :func:`get_pack` — discover the bundled YAML (offline, no DB);
* :func:`install_pack` — validate every policy through the engine, then materialize
  the (upserted) policies plus a collection (named by the pack's optional
  ``collection``, else its ``name``), recording the installed version in
  ``installed_packs``.

Install is **atomic on validation**: every policy is validated up front, so a pack
with any invalid policy reports the error and writes nothing. Re-installing the
same version is idempotent (upsert-by-name + get-or-create collection + a single
``installed_packs`` row). The engine ``runner`` seam is injectable so tests stay
fully offline.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from ..custodian.engine import CustodianRunner, validate_policy
from ..governance import frameworks as _frameworks
from ..storage import repository as repo
from ..storage.db import session_scope

logger = logging.getLogger("cloudwarden.packs.registry")

# Bundled packs live alongside this module (single-file YAML or a subdir + manifest).
PACKS_DIR = Path(__file__).resolve().parent

_PACK_EXTS = {".yml", ".yaml"}
_MANIFEST = "pack.yaml"


def _parse(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _load_dir_pack(subdir: Path) -> dict[str, Any] | None:
    """Assemble a directory pack: ``pack.yaml`` manifest + sibling policy files.

    Returns the manifest metadata with ``policies`` set to the c7n specs loaded from
    every sibling ``*.yml`` (except the manifest) and ``manifest`` set to the
    enumeration declared in ``pack.yaml``. ``None`` if the manifest lacks a name.
    """
    manifest = _parse(subdir / _MANIFEST)
    if not isinstance(manifest, dict) or not manifest.get("name"):
        return None
    specs: list[dict[str, Any]] = []
    for path in sorted(subdir.iterdir()):
        if path.name == _MANIFEST or path.suffix.lower() not in _PACK_EXTS:
            continue
        data = _parse(path)
        if isinstance(data, dict):
            specs.extend(data.get("policies") or [])
    pack = dict(manifest)
    pack["manifest"] = manifest.get("policies") or []
    pack["policies"] = specs
    return pack


def _load_packs(packs_dir: Path | None) -> dict[str, dict[str, Any]]:
    """Parse every pack in ``packs_dir`` into a ``{name: pack}`` mapping.

    Discovers single-file packs (``*.yaml`` directly in the directory) and directory
    packs (subfolders containing a ``pack.yaml`` manifest). Entries that don't parse
    to a mapping with a ``name`` are ignored (a stray, non-pack YAML is not an error).
    """
    directory = packs_dir if packs_dir is not None else PACKS_DIR
    if not directory.is_dir():
        return {}
    packs: dict[str, dict[str, Any]] = {}
    for path in sorted(directory.iterdir()):
        if path.is_dir():
            if (path / _MANIFEST).is_file():
                pack = _load_dir_pack(path)
                if pack is not None:
                    packs[pack["name"]] = pack
            continue
        if path.suffix.lower() not in _PACK_EXTS:
            continue
        data = _parse(path)
        if not isinstance(data, dict) or not data.get("name"):
            continue
        packs[data["name"]] = data
    return packs


def _pack_summary(pack: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": pack["name"],
        "version": str(pack.get("version") or ""),
        "title": pack.get("title") or pack["name"],
        "description": pack.get("description") or "",
        "policy_count": len(pack.get("policies") or []),
    }


def list_packs(packs_dir: Path | None = None) -> list[dict[str, Any]]:
    """List discoverable packs (name/version/title/description/policy_count), sorted."""
    packs = _load_packs(packs_dir)
    return [_pack_summary(packs[name]) for name in sorted(packs)]


def get_pack(name: str, packs_dir: Path | None = None) -> dict[str, Any] | None:
    """Return the full parsed pack (with its ``policies``), or ``None`` if unknown."""
    return _load_packs(packs_dir).get(name)


def install_pack(
    name: str,
    runner: CustodianRunner | None = None,
    packs_dir: Path | None = None,
) -> dict[str, Any]:
    """Install a pack: validate all policies, then materialize policies + a collection.

    Never raises — returns a report ``{ok, pack, version, collection_id, added,
    updated, unchanged, policies, errors, error}``. On an unknown pack or any
    invalid policy, ``ok`` is ``False`` and **nothing** is persisted.
    """
    report: dict[str, Any] = {
        "ok": False,
        "pack": name,
        "version": None,
        "collection_id": None,
        "added": 0,
        "updated": 0,
        "unchanged": 0,
        "policies": [],
        "errors": [],
        "error": None,
    }

    pack = get_pack(name, packs_dir=packs_dir)
    if pack is None:
        report["error"] = f"unknown pack: {name}"
        return report

    version = str(pack.get("version") or "")
    policies = pack.get("policies") or []
    report["version"] = version

    # Validate every policy up front so an invalid pack installs nothing (atomic).
    errors: list[dict[str, Any]] = []
    for policy in policies:
        spec = {"policies": [policy]}
        validation = validate_policy(spec, runner=runner)
        if not validation.get("valid"):
            errors.append({"policy": policy.get("name"), "errors": validation.get("errors") or []})
    if errors:
        report["errors"] = errors
        report["error"] = f"pack '{name}' has invalid policies"
        return report

    with session_scope() as session:
        collection = repo.get_or_create_collection(
            session, name=pack.get("collection") or name, description=pack.get("description")
        )
        collection_id = collection["id"]
        for policy in policies:
            spec = {"policies": [policy]}
            outcome = repo.upsert_policy_by_name(
                session,
                name=policy["name"],
                resource_type=policy.get("resource", ""),
                spec=spec,
                description=policy.get("description"),
                source="pack",
            )
            report[outcome] += 1
            stored = repo.get_policy_by_name(session, policy["name"])
            repo.add_policy_to_collection(session, collection_id, stored["id"])
        repo.upsert_installed_pack(session, name=name, version=version, collection_id=collection_id)
        report["collection_id"] = collection_id
        report["policies"] = [p["name"] for p in policies]

    report["ok"] = True
    return report


# --------------------------------------------------------------------------- #
# Compliance framework overlays (M14.13) — versioned via the same registry.
# --------------------------------------------------------------------------- #
def list_frameworks(frameworks_dir: Path | None = None) -> list[dict[str, Any]]:
    """List installable compliance framework overlays (delegates to the loader)."""
    return _frameworks.list_frameworks(frameworks_dir)


def get_framework(name: str, frameworks_dir: Path | None = None) -> dict[str, Any] | None:
    """Return the full parsed framework overlay, or ``None`` if unknown."""
    return _frameworks.load_framework(name, frameworks_dir)


def install_framework(name: str, frameworks_dir: Path | None = None) -> dict[str, Any]:
    """Install a framework overlay: record its version + control→policy mappings.

    Never raises — returns ``{ok, framework, version, controls, mapped, gaps,
    error}``. Unlike a pack, a framework maps to **existing** policies, so nothing
    new is materialized: the overlay's version is recorded in ``installed_frameworks``
    and its control mappings replace any prior rows in ``framework_controls`` (so the
    Grafana per-framework posture view can read them). Re-installing is idempotent.
    ``ok`` is ``False`` (and nothing is written) for an unknown framework.
    """
    report: dict[str, Any] = {
        "ok": False,
        "framework": name,
        "version": None,
        "controls": 0,
        "mapped": 0,
        "gaps": 0,
        "error": None,
    }

    fwk = _frameworks.load_framework(name, frameworks_dir)
    if fwk is None:
        report["error"] = f"unknown framework: {name}"
        return report

    controls = fwk["controls"]
    gaps = sum(1 for c in controls if not c["policies"])
    mapped = len(controls) - gaps
    version = str(fwk.get("version") or "")

    mapping_rows: list[dict[str, Any]] = []
    for ordinal, control in enumerate(controls):
        if control["policies"]:
            for policy_name in control["policies"]:
                mapping_rows.append(
                    {
                        "control_id": control["id"],
                        "title": control["title"],
                        "policy_name": policy_name,
                        "ordinal": ordinal,
                    }
                )
        else:
            mapping_rows.append(
                {
                    "control_id": control["id"],
                    "title": control["title"],
                    "policy_name": None,
                    "ordinal": ordinal,
                }
            )

    with session_scope() as session:
        repo.upsert_installed_framework(
            session,
            name=name,
            version=version,
            title=fwk.get("title") or name,
            description=fwk.get("description") or "",
            control_count=len(controls),
            mapped_count=mapped,
            gap_count=gaps,
        )
        repo.replace_framework_controls(session, name, mapping_rows)

    report.update(
        {"ok": True, "version": version, "controls": len(controls), "mapped": mapped, "gaps": gaps}
    )
    return report
