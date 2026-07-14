"""GitOps policy sync — import Cloud Custodian policies from a Git repository.

:func:`sync_policies` clones/pulls the configured repo (``GITOPS_REPO_URL`` /
``GITOPS_BRANCH`` / ``GITOPS_POLICY_PATH``), parses the policy YAML/JSON files,
validates each policy through the engine, and **upserts by name** with
``source='gitops'``. Invalid files are skipped and reported (non-fatal), the sync
is idempotent (a no-op re-sync writes nothing), and any clone/pull failure is
returned as a structured error rather than raised.

The ``git_client`` seam (a :class:`GitClient`) is injectable so tests point the
sync at a temp fixture directory — no real clone, no network. The default
:class:`LiveGitClient` shells out to ``git``; that live path is out of scope for
unit tests (mock mode never configures a repo).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import yaml

from ..config import get_settings
from ..resilience import REGISTRY
from ..storage import repository as repo
from ..storage.db import session_scope
from .engine import CustodianRunner, validate_policy

logger = logging.getLogger("cloudwarden.custodian.gitops")

_POLICY_EXTS = {".yml", ".yaml", ".json"}


@runtime_checkable
class GitClient(Protocol):
    """Produces a local checkout of a repo — the one mockable seam for sync."""

    def clone_or_pull(self, repo_url: str, branch: str) -> str:
        """Clone or update ``repo_url`` at ``branch``; return the local path."""


class LiveGitClient:
    """Clones/pulls via the system ``git`` binary into a cache dir under APP_DATA_DIR."""

    def clone_or_pull(self, repo_url: str, branch: str) -> str:  # pragma: no cover - needs git+net
        import hashlib
        import subprocess

        key = hashlib.sha256(repo_url.encode("utf-8")).hexdigest()[:16]
        dest = Path(get_settings().app_data_dir) / "gitops" / key
        if (dest / ".git").is_dir():
            subprocess.run(
                ["git", "-C", str(dest), "fetch", "--depth", "1", "origin", branch],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", str(dest), "reset", "--hard", f"origin/{branch}"],
                check=True,
                capture_output=True,
            )
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ["git", "clone", "--depth", "1", "--branch", branch, repo_url, str(dest)],
                check=True,
                capture_output=True,
            )
        return str(dest)


def _default_git_client() -> GitClient:
    return LiveGitClient()


def _policy_files(policy_dir: Path) -> list[Path]:
    if not policy_dir.is_dir():
        return []
    return sorted(
        p for p in policy_dir.rglob("*") if p.is_file() and p.suffix.lower() in _POLICY_EXTS
    )


def sync_policies(
    git_client: GitClient | None = None, runner: CustodianRunner | None = None
) -> dict[str, Any]:
    """Sync policies from the configured Git repo. Never raises — returns a report.

    Report shape: ``{ok, added, updated, unchanged, skipped, errors, error}`` where
    ``errors`` is a per-file/-policy list and ``error`` is a top-level failure
    string (git/config) or ``None``.
    """
    settings = get_settings()
    report: dict[str, Any] = {
        "ok": False,
        "added": 0,
        "updated": 0,
        "unchanged": 0,
        "skipped": 0,
        "errors": [],
        "error": None,
    }

    if not settings.gitops_repo_url:
        report["error"] = "GITOPS_REPO_URL is not configured"
        return report

    client = git_client if git_client is not None else _default_git_client()
    try:
        checkout = client.clone_or_pull(settings.gitops_repo_url, settings.gitops_branch)
    except Exception as exc:  # noqa: BLE001 - non-fatal: report a structured error
        REGISTRY.set("gitops", ok=False, error=str(exc))
        report["error"] = f"git clone/pull failed: {exc}"
        return report

    checkout_path = Path(checkout)
    with session_scope() as session:
        for path in _policy_files(checkout_path / settings.gitops_policy_path):
            rel = str(path.relative_to(checkout_path))
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8"))
            except Exception as exc:  # noqa: BLE001 - skip unparseable file
                report["skipped"] += 1
                report["errors"].append({"file": rel, "error": f"YAML parse error: {exc}"})
                continue

            policies = data.get("policies") if isinstance(data, dict) else None
            if not isinstance(policies, list) or not policies:
                report["skipped"] += 1
                report["errors"].append({"file": rel, "error": "no 'policies' list found"})
                continue

            for policy in policies:
                name = policy.get("name") if isinstance(policy, dict) else None
                if not name:
                    report["skipped"] += 1
                    report["errors"].append({"file": rel, "error": "policy entry missing 'name'"})
                    continue
                spec = {"policies": [policy]}
                validation = validate_policy(spec, runner=runner)
                if not validation.get("valid"):
                    report["skipped"] += 1
                    report["errors"].append(
                        {"file": rel, "policy": name, "errors": validation.get("errors") or []}
                    )
                    continue
                outcome = repo.upsert_policy_by_name(
                    session,
                    name=name,
                    resource_type=policy.get("resource", ""),
                    spec=spec,
                    description=policy.get("description"),
                    source="gitops",
                )
                report[outcome] += 1

    REGISTRY.set("gitops", ok=True)
    report["ok"] = True
    return report
