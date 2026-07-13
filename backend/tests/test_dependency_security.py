"""Dependency-security guard & watch for issue #58 (c7n-pinned HIGH CVEs).

Cloud Custodian — ``c7n==0.9.51`` / ``c7n-azure==0.7.50`` (the latest releases) —
hard-pins (``==``) two packages that carry HIGH advisories:

  * ``cryptography==46.0.7`` → GHSA-537c-gmf6-5ccf (vulnerable OpenSSL bundled in the
    manylinux wheel); fixed in ``48.0.1``.
  * ``pyjwt==2.12.1``       → CVE-2026-48526 (auth bypass via forged JWT); fixed in
    ``2.13.0``.

They can't be bumped in ``requirements.txt`` without ``ResolutionImpossible`` against
those exact pins, so the shipped image (``backend/Dockerfile``) and the CI test env
apply a ``--no-deps`` override to the patched versions listed in
``backend/requirements-overrides-security.txt``. These tests:

  1. **Lock the mitigation in** — the *effective* cryptography/pyjwt must be patched,
     so the versions the test suite validates are the versions we actually ship.
  2. Act as a **watch** — the moment upstream c7n / c7n-azure relaxes the vulnerable
     pins, ``test_upstream_still_pins_the_vulnerable_versions`` fails, telling us to
     bump ``requirements.txt`` and delete the override (issue #58, preferred path).

Deterministic and offline: reads installed distribution metadata + repo files. No
network, no DB fixture.
"""

from __future__ import annotations

import importlib.metadata as md
from pathlib import Path

# Patched floors — the advisory "Fixed in" versions. Keep in sync with the override
# file (backend/requirements-overrides-security.txt).
CRYPTOGRAPHY_FIXED = (48, 0, 1)  # GHSA-537c-gmf6-5ccf
PYJWT_FIXED = (2, 13, 0)  # CVE-2026-48526

# The exact vulnerable pins c7n / c7n-azure still declare — the watch targets. When an
# entry here stops matching, upstream relaxed the pin and the clean fix is now possible.
VULNERABLE_PINS = {
    "c7n": {"cryptography==46.0.7"},
    "c7n-azure": {"cryptography==46.0.7", "pyjwt==2.12.1"},
}

_REPO_ROOT = Path(__file__).resolve().parents[2]
_OVERRIDES = _REPO_ROOT / "backend" / "requirements-overrides-security.txt"
_DOCKERFILE = _REPO_ROOT / "backend" / "Dockerfile"


def _installed(dist: str) -> tuple[int, ...]:
    """Numeric release tuple of an installed distribution, e.g. ``(48, 0, 1)``."""
    return tuple(int(p) for p in md.version(dist).split(".") if p.isdigit())


def _declared_pins(dist: str) -> set[str]:
    """Normalized ``name==x.y.z`` requirement strings a distribution declares."""
    return {r.split(";")[0].replace(" ", "").lower() for r in (md.requires(dist) or [])}


def test_effective_cryptography_is_patched() -> None:
    assert _installed("cryptography") >= CRYPTOGRAPHY_FIXED, (
        "cryptography must be >=48.0.1 (GHSA-537c-gmf6-5ccf). This environment must "
        "apply backend/requirements-overrides-security.txt — as the image does — so the "
        "test suite validates the versions we actually ship. See issue #58."
    )


def test_effective_pyjwt_is_patched() -> None:
    assert _installed("pyjwt") >= PYJWT_FIXED, (
        "pyjwt must be >=2.13.0 (CVE-2026-48526). Apply "
        "backend/requirements-overrides-security.txt in this environment. See issue #58."
    )


def test_upstream_still_pins_the_vulnerable_versions() -> None:
    # WATCH: when this fails, c7n / c7n-azure has relaxed a pin — do the clean fix:
    # bump backend/requirements.txt to the patched versions and delete the --no-deps
    # override (Dockerfile + CI step + overrides file). Issue #58, preferred path.
    for dist, expected in VULNERABLE_PINS.items():
        declared = _declared_pins(dist)
        for pin in expected:
            assert pin in declared, (
                f"{dist} no longer pins {pin!r} — upstream relaxed it. Bump "
                "backend/requirements.txt to cryptography>=48.0.1 / pyjwt>=2.13.0 and "
                "remove the --no-deps override. Issue #58."
            )


def test_override_file_is_single_source_and_wired_into_the_image() -> None:
    overrides = _OVERRIDES.read_text().lower()
    assert "cryptography" in overrides and "pyjwt" in overrides, (
        "backend/requirements-overrides-security.txt must list the two c7n-pinned "
        "packages we force-patch. Issue #58."
    )
    dockerfile = _DOCKERFILE.read_text()
    assert "requirements-overrides-security.txt" in dockerfile and "--no-deps" in dockerfile, (
        "backend/Dockerfile must apply the override file via --no-deps (single source "
        "of truth for the c7n CVE mitigation). Issue #58."
    )
