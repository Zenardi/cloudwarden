"""Supply-chain gate contract for CI (issue #53, M13.3).

Executable spec for three supply-chain / credential gates in
``.github/workflows/ci.yml`` and the repo:

  * **SBOM** — CI generates a Software Bill of Materials for the backend image
    with ``syft`` and uploads it as a build artifact (``actions/upload-artifact``).
  * **Dependency pinning** — a hash-pinned ``backend/requirements.lock`` exists
    (every requirement carries a ``--hash=sha256:…``) and CI installs it with
    ``pip --require-hashes`` so a tampered wheel fails the build.
  * **Secret scanning** — CI runs ``gitleaks`` as a BLOCKING gate (fails on a
    detected secret) and ``.gitleaks.toml`` carries a documented allowlist.

Deterministic and offline: parses the workflow YAML and reads
``backend/requirements.lock`` / ``.gitleaks.toml``. No network, no Docker, no DB.
"""

from __future__ import annotations

from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CI_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci.yml"
_REQ_LOCK = _REPO_ROOT / "backend" / "requirements.lock"
_GITLEAKS_TOML = _REPO_ROOT / ".gitleaks.toml"


def _workflow() -> dict:
    return yaml.safe_load(_CI_WORKFLOW.read_text())


def _ci_run_scripts() -> list[str]:
    """Every step's ``run:`` script across all jobs in the CI workflow."""
    return [
        step["run"]
        for job in _workflow()["jobs"].values()
        for step in job.get("steps", [])
        if isinstance(step.get("run"), str)
    ]


def _ci_uses() -> list[str]:
    """Every step's ``uses:`` value across all jobs in the CI workflow."""
    return [
        step["uses"]
        for job in _workflow()["jobs"].values()
        for step in job.get("steps", [])
        if isinstance(step.get("uses"), str)
    ]


def _artifact_upload_paths() -> list[str]:
    """``with.path`` + ``with.name`` of every ``actions/upload-artifact`` step."""
    paths: list[str] = []
    for job in _workflow()["jobs"].values():
        for step in job.get("steps", []):
            uses = step.get("uses", "")
            if isinstance(uses, str) and uses.startswith("actions/upload-artifact"):
                with_block = step.get("with") or {}
                paths.append(str(with_block.get("path", "")))
                paths.append(str(with_block.get("name", "")))
    return paths


def _gitleaks_steps() -> list[tuple[str, dict]]:
    """``(job_name, step)`` for every step that invokes gitleaks (uses or run)."""
    found: list[tuple[str, dict]] = []
    for job_name, job in _workflow()["jobs"].items():
        for step in job.get("steps", []):
            uses = step.get("uses") if isinstance(step.get("uses"), str) else ""
            run = step.get("run") if isinstance(step.get("run"), str) else ""
            if "gitleaks" in uses.lower() or "gitleaks" in run.lower():
                found.append((job_name, step))
    return found


def _lock_logical_lines() -> list[str]:
    """Lock requirement lines with backslash-continuations collapsed to one line."""
    joined = _REQ_LOCK.read_text().replace("\\\n", " ")
    return [" ".join(line.split()) for line in joined.splitlines() if line.strip()]


# --- SBOM ---------------------------------------------------------------- #


def test_ci_generates_sbom() -> None:
    # Arrange / Act — a step generates an SBOM with syft.
    scripts = "\n".join(_ci_run_scripts())
    # Assert
    assert "syft" in scripts, "CI must generate an SBOM with `syft` (issue #53)."


def test_ci_uploads_sbom_artifact() -> None:
    # Arrange / Act — an upload-artifact step publishes the SBOM.
    referenced = " ".join(_artifact_upload_paths()).lower()
    # Assert — the uploaded artifact is the SBOM.
    assert "sbom" in referenced, "CI must upload the SBOM via actions/upload-artifact (issue #53)."


# --- Dependency pinning -------------------------------------------------- #


def test_requirements_lock_is_hash_pinned() -> None:
    # Arrange — the hash-pinned lock must exist.
    assert _REQ_LOCK.exists(), "backend/requirements.lock must exist (issue #53)."
    # Act — the pinned requirement lines (name==version ...).
    pinned = [line for line in _lock_logical_lines() if "==" in line.split(" ")[0]]
    # Assert — there are pins AND every one carries a sha256 hash.
    assert pinned, "requirements.lock must pin at least one dependency."
    for line in pinned:
        assert "--hash=sha256:" in line, (
            f"every locked dependency must be hash-pinned: {line.split(' ')[0]!r}"
        )


def test_ci_installs_hash_pinned_lock() -> None:
    # Arrange
    scripts = "\n".join(_ci_run_scripts())
    # Act / Assert — CI installs the lock with hash verification enforced.
    assert "requirements.lock" in scripts, "CI must install backend/requirements.lock."
    assert "--require-hashes" in scripts, (
        "CI must install the lock with `pip --require-hashes` so a tampered wheel fails."
    )


# --- Secret scanning ----------------------------------------------------- #


def test_ci_runs_gitleaks() -> None:
    # Arrange / Act
    steps = _gitleaks_steps()
    # Assert — CI runs a gitleaks secret scan.
    assert steps, "CI must run a `gitleaks` secret scan (issue #53)."


def test_gitleaks_fails_on_secret() -> None:
    # Arrange
    steps = _gitleaks_steps()
    assert steps, "expected a gitleaks step to guard the build."
    jobs = _workflow()["jobs"]
    # Act / Assert — the gitleaks gate is BLOCKING: neither the step nor its job
    # is `continue-on-error`, and a CLI invocation does not disable the exit code.
    for job_name, step in steps:
        assert step.get("continue-on-error") is not True, (
            "the gitleaks step must not set continue-on-error (it must block)."
        )
        assert jobs[job_name].get("continue-on-error") is not True, (
            f"job {job_name!r} running gitleaks must not be continue-on-error."
        )
        run = step.get("run") if isinstance(step.get("run"), str) else ""
        assert "--exit-code 0" not in run, (
            "gitleaks must keep a non-zero exit code so a detected secret fails CI."
        )


def test_gitleaks_allowlist_documented() -> None:
    # Arrange — the reviewed secret-scan config must exist.
    assert _GITLEAKS_TOML.exists(), ".gitleaks.toml must exist (issue #53)."
    text = _GITLEAKS_TOML.read_text()
    # Act / Assert — it declares an allowlist and documents it with comments.
    assert "[allowlist]" in text, ".gitleaks.toml must declare an [allowlist] section."
    assert any(line.strip().startswith("#") for line in text.splitlines()), (
        "the .gitleaks.toml allowlist must be documented with justification comments."
    )
