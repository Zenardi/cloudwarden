"""Quality-gate contract: coverage floor + mutation testing (issue #52, M13.2).

Executable spec for the two CI quality gates:

  * **Coverage** — `pyproject.toml` pins `fail_under = 95` and the CI backend job
    enforces it explicitly (`--cov-fail-under=95`).
  * **Mutation testing** — a CI job runs `mutmut` over the core governance modules
    (`analysis`, `custodian`, `remediation`), `mutmut` is pinned in
    `requirements-dev.txt`, and its config targets those modules.

Deterministic and offline: parses `pyproject.toml`, `.github/workflows/ci.yml`,
`backend/setup.cfg` and `backend/requirements-dev.txt`. No network, no DB.
"""

from __future__ import annotations

import configparser
import re
import tomllib
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PYPROJECT = _REPO_ROOT / "pyproject.toml"
_CI_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci.yml"
_MUTMUT_CFG = _REPO_ROOT / "backend" / "setup.cfg"
_DEV_REQS = _REPO_ROOT / "backend" / "requirements-dev.txt"

# The core governance modules mutation testing must target (issue #52).
_CORE_MODULES = ("analysis", "custodian", "remediation")


def _ci_run_scripts() -> list[str]:
    """Every step's ``run:`` script across all jobs in the CI workflow."""
    workflow = yaml.safe_load(_CI_WORKFLOW.read_text())
    return [
        step["run"]
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
        if isinstance(step.get("run"), str)
    ]


def test_coverage_fail_under_is_95() -> None:
    # Arrange
    data = tomllib.loads(_PYPROJECT.read_text())
    # Act
    fail_under = data["tool"]["coverage"]["report"]["fail_under"]
    # Assert — the backend line-coverage floor is 95%.
    assert fail_under == 95, f"coverage fail_under must be 95, got {fail_under!r}"


def test_ci_runs_coverage_gate() -> None:
    # Arrange
    scripts = "\n".join(_ci_run_scripts())
    # Act / Assert — CI runs pytest with coverage AND enforces the 95% floor.
    assert "--cov=cloudwarden" in scripts, "CI must run pytest with `--cov=cloudwarden`."
    assert "--cov-fail-under=95" in scripts, (
        "CI must fail the build under 95% coverage (`--cov-fail-under=95`)."
    )


def test_ci_runs_mutation_job() -> None:
    # Arrange / Act — is there a step anywhere that runs mutmut?
    runs_mutmut = any("mutmut run" in script for script in _ci_run_scripts())
    # Assert
    assert runs_mutmut, "CI must have a job that runs `mutmut run` (issue #52)."


def test_mutation_config_targets_core_modules() -> None:
    # Arrange — the mutmut config lives in backend/setup.cfg.
    assert _MUTMUT_CFG.exists(), "backend/setup.cfg must exist with a [mutmut] section."
    cfg = configparser.ConfigParser()
    cfg.read(_MUTMUT_CFG)
    # Act
    assert cfg.has_section("mutmut"), "backend/setup.cfg needs a [mutmut] section."
    blob = "\n".join(value for _, value in cfg.items("mutmut"))
    # Assert — every core governance module is a mutation target.
    for module in _CORE_MODULES:
        assert module in blob, f"mutmut config must target the {module!r} core module."


def test_mutmut_pinned() -> None:
    # Arrange
    lines = _DEV_REQS.read_text().splitlines()
    # Act — an exact `mutmut==x.y.z` pin (not a floor / range).
    pinned = [line for line in lines if re.match(r"\s*mutmut==\d", line)]
    # Assert
    assert pinned, "mutmut must be pinned as `mutmut==x.y.z` in requirements-dev.txt."
