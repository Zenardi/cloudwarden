"""Azure SDK footprint contract (issue #129, M13.6).

The backend image ships only the ``azure-mgmt-*`` provider SDKs CloudWarden
actually runs; the Dockerfile builder prunes the rest (~214 MB). The KEEP list is
``backend/azure_mgmt_keep.txt`` — the single source of truth shared by the
Dockerfile prune step and this test.

This is the **drift guard**: it recomputes, from the real c7n-azure resource
registry, the set of ``azure.mgmt.<provider>`` SDKs required by (a) every resource
type our packs/policies reference and (b) every ``azure.mgmt.*`` imported directly
by ``cloudwarden``, and asserts that set is fully covered by the KEEP list. So if a
new pack introduces, say, ``azure.batch`` whose SDK is pruned, this test fails
until ``azure_mgmt_keep.txt`` is updated — before the image ever ships broken.

Offline and deterministic: the needed-set is computed in a clean subprocess (no
cross-test ``sys.modules`` contamination); no DB, Azure, or network is touched.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("c7n_azure")

_BACKEND = Path(__file__).resolve().parents[1]
_KEEP_FILE = _BACKEND / "azure_mgmt_keep.txt"
_CLOUDWARDEN = _BACKEND / "cloudwarden"

# Resource types are written as `resource: azure.<type>` (YAML packs) or
# `"resource": "azure.<type>"` (JSON/py specs).
_RESOURCE_RE = re.compile(r"""resource["']?\s*:\s*["']?azure\.([a-z0-9]+)""")
# Direct SDK imports in cloudwarden source: `from azure.mgmt.<provider> import ...`.
_DIRECT_RE = re.compile(r"azure\.mgmt\.([a-z0-9]+)")

# Computed in a CLEAN interpreter so prior tests' azure imports can't leak in.
_NEEDED_SNIPPET = r"""
import importlib, sys
types = sys.argv[1:]
import c7n_azure.entry  # noqa: F401 - side-effecting registration
from c7n.resources import load_resources
load_resources(("azure.*",))
from c7n.provider import clouds
res = clouds["azure"].resources
# c7n-azure core modules every live session/query/tag path executes.
for mod in ("c7n_azure.session", "c7n_azure.query", "c7n_azure.tags",
            "c7n_azure.provider", "c7n_azure.utils", "c7n_azure.filters"):
    try:
        importlib.import_module(mod)
    except Exception:
        pass
# Each of our resource types -> its SDK `service` module.
for t in types:
    klass = res.get(t)
    if klass is None:
        continue
    svc = getattr(getattr(klass, "resource_type", None), "service", None)
    if svc:
        try:
            importlib.import_module(svc)
        except Exception:
            pass
needed = sorted(
    {m.split(".")[2] for m in list(sys.modules)
     if m.startswith("azure.mgmt.") and m.count(".") >= 2}
)
print(" ".join(needed))
"""


def _read_keep() -> list[str]:
    lines = _KEEP_FILE.read_text().splitlines()
    return [ln.strip() for ln in lines if ln.strip() and not ln.lstrip().startswith("#")]


def _iter_source_text():
    for path in _CLOUDWARDEN.rglob("*"):
        if path.suffix in {".py", ".yml", ".yaml", ".json"} and path.is_file():
            yield path.read_text(encoding="utf-8", errors="replace")


def _resource_types() -> set[str]:
    types: set[str] = set()
    for text in _iter_source_text():
        types.update(_RESOURCE_RE.findall(text))
    return types


def _direct_mgmt_imports() -> set[str]:
    found: set[str] = set()
    for path in _CLOUDWARDEN.rglob("*.py"):
        found.update(_DIRECT_RE.findall(path.read_text(encoding="utf-8", errors="replace")))
    return found


def _installed_providers() -> set[str]:
    import azure.mgmt as _m

    providers: set[str] = set()
    for base in _m.__path__:
        for child in Path(base).iterdir():
            if child.is_dir() and child.name != "__pycache__":
                providers.add(child.name)
    return providers


def _needed_via_registry(resource_types: set[str]) -> set[str]:
    proc = subprocess.run(
        [sys.executable, "-c", _NEEDED_SNIPPET, *sorted(resource_types)],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc.returncode == 0, f"registry subprocess failed:\n{proc.stderr}"
    return set(proc.stdout.split())


# --------------------------------------------------------------------------- #
# The KEEP list is well-formed
# --------------------------------------------------------------------------- #
def test_keep_file_exists_and_is_well_formed() -> None:
    keep = _read_keep()
    assert keep, "azure_mgmt_keep.txt has no provider entries"
    assert keep == sorted(keep), "keep list must be alphabetically sorted"
    assert len(keep) == len(set(keep)), "keep list has duplicates"
    assert all(re.fullmatch(r"[a-z0-9]+", p) for p in keep), keep


# --------------------------------------------------------------------------- #
# Every kept provider is a real, importable azure.mgmt SDK
# --------------------------------------------------------------------------- #
def test_kept_providers_are_importable() -> None:
    import importlib

    for provider in _read_keep():
        # Raises ImportError if the provider name is bogus (a typo would ship a
        # keep-list that silently fails to protect a real dependency).
        importlib.import_module(f"azure.mgmt.{provider}")


# --------------------------------------------------------------------------- #
# The KEEP list COVERS every azure.mgmt SDK CloudWarden actually needs (drift)
# --------------------------------------------------------------------------- #
def test_keep_list_covers_runtime_azure_mgmt_footprint() -> None:
    keep = set(_read_keep())
    needed = _needed_via_registry(_resource_types()) | _direct_mgmt_imports()
    # Only azure.mgmt providers are pruned; ignore anything not installed under
    # azure/mgmt (e.g. azure.monitor.query lives in a separate package).
    needed &= _installed_providers()
    missing = needed - keep
    assert not missing, (
        f"azure_mgmt_keep.txt is missing SDKs CloudWarden needs: {sorted(missing)} "
        f"— add them or the pruned image will break at runtime"
    )


# --------------------------------------------------------------------------- #
# The prune actually removes something (the keep list is a strict subset)
# --------------------------------------------------------------------------- #
def test_prune_is_material() -> None:
    pruned = _installed_providers() - set(_read_keep())
    # If nothing is pruned the whole exercise is a no-op; guard against that.
    assert len(pruned) >= 10, f"expected a material prune, only {len(pruned)} providers dropped"
