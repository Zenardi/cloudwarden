"""Trivy CVE-gate contract for CI (issue #51, M13.1).

These tests are the executable spec for the security gate in
``.github/workflows/ci.yml``: CI must run ``trivy fs``, ``trivy image`` and
``trivy config``, and every scan must FAIL the build on HIGH/CRITICAL findings
(``--severity HIGH,CRITICAL --exit-code 1``). They also assert that
``.trivyignore`` exists and that every suppression it carries is justified.

Deterministic and offline: parses the workflow YAML and reads ``.trivyignore``.
No network, no Docker, no DB fixture — so this runs anywhere the repo is checked
out (the gate itself runs in CI, where Trivy is installed).
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CI_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci.yml"
_TRIVYIGNORE = _REPO_ROOT / ".trivyignore"

# A single Trivy scan sub-command we care about (dependency/image/IaC scans).
_SCAN_SUBCOMMANDS = ("fs", "image", "config")


def _run_scripts() -> list[str]:
    """Every step's ``run:`` script across all jobs in the CI workflow."""
    workflow = yaml.safe_load(_CI_WORKFLOW.read_text())
    scripts: list[str] = []
    for job in workflow["jobs"].values():
        for step in job.get("steps", []):
            run = step.get("run")
            if isinstance(run, str):
                scripts.append(run)
    return scripts


def _trivy_invocations(subcommand: str) -> list[str]:
    """Full ``trivy <subcommand> ...`` command strings found in any CI step.

    Backslash-newline continuations are collapsed first, so a command split
    across lines is returned as one string (matching how the shell runs it).
    """
    pattern = re.compile(rf"\btrivy\s+{subcommand}\b")
    invocations: list[str] = []
    for script in _run_scripts():
        joined = re.sub(r"\\\n\s*", " ", script)
        for line in joined.splitlines():
            normalized = " ".join(line.split())
            if pattern.search(normalized):
                invocations.append(normalized)
    return invocations


def _all_scan_invocations() -> list[str]:
    return [cmd for sub in _SCAN_SUBCOMMANDS for cmd in _trivy_invocations(sub)]


def test_ci_has_trivy_fs_step() -> None:
    # Arrange / Act
    fs_scans = _trivy_invocations("fs")
    # Assert — CI runs a filesystem (dependency) scan.
    assert fs_scans, "CI must run a `trivy fs` dependency scan (issue #51)."


def test_ci_has_trivy_image_step() -> None:
    # Arrange / Act
    image_scans = _trivy_invocations("image")
    # Assert — CI scans a built container image.
    assert image_scans, "CI must run a `trivy image` scan of the built image (issue #51)."


def test_ci_has_trivy_config_step() -> None:
    # Arrange / Act
    config_scans = _trivy_invocations("config")
    # Assert — CI scans IaC/config for misconfigurations.
    assert config_scans, "CI must run a `trivy config` IaC/misconfig scan (issue #51)."


def test_ci_trivy_fails_on_high_critical() -> None:
    # Arrange
    scans = _all_scan_invocations()
    assert scans, "expected at least one Trivy scan to guard the build."
    # Act / Assert — every scan gates on HIGH/CRITICAL and fails the build.
    for cmd in scans:
        assert "--severity HIGH,CRITICAL" in cmd, f"Trivy scan must gate on HIGH,CRITICAL: {cmd!r}"
        assert "--exit-code 1" in cmd, f"Trivy scan must fail the build with --exit-code 1: {cmd!r}"


def test_trivyignore_entries_have_justification() -> None:
    # Arrange — the reviewed suppression file must exist.
    assert _TRIVYIGNORE.exists(), (
        ".trivyignore must exist to document accepted CVE exceptions (issue #51)."
    )
    lines = _TRIVYIGNORE.read_text().splitlines()

    # Act / Assert — every suppression (a non-comment, non-blank line) must be
    # justified by a comment on the line immediately above it, the reviewable
    # trivy convention. With zero suppressions this holds vacuously (the gate is
    # clean); it starts failing the moment an unjustified id is added.
    for index, raw in enumerate(lines):
        entry = raw.strip()
        if not entry or entry.startswith("#"):
            continue
        preceding = [prev.strip() for prev in lines[:index] if prev.strip()]
        assert preceding and preceding[-1].startswith("#") and len(preceding[-1]) > 1, (
            f".trivyignore entry {entry!r} needs a justification comment directly above it."
        )
