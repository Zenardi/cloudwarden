"""GitOps policy write-back — propose a policy change as a pull request (M14.8).

GitOps sync is read-only today: policies flow *from* git into CloudWarden (boot +
local fallback), but a policy edited in the UI never flows back. This closes the
loop. :func:`propose_policy_change`:

1. **serializes** the policy to its canonical repo YAML (:func:`serialize_policy`),
   which round-trips with the read-sync layout (:func:`gitops.policy_record_from_doc`)
   so a re-import produces the identical policy — no drift;
2. computes a per-policy branch name (``cloudwarden/policy-<name>-<version>``) and
   **refuses to target the default branch** — the reviewed PR remains the source of
   truth, nothing is pushed to the default branch directly;
3. delegates branch/commit/push/open-PR to an **injected** :class:`GitProvider`
   (git + GitHub/GitLab API), so tests run with no network. A provider failure is
   surfaced as :class:`WriteBackProviderError` with **no partial state**.

The provider **token is read from config and never logged**: it is passed to the
provider out-of-band (a keyword arg), never stored on :class:`ProposalRequest` /
:class:`ProposalResult`, so it can't leak through a repr or an audit payload.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

import yaml

from ..config import get_settings
from . import gitops

logger = logging.getLogger("cloudwarden.custodian.gitwriteback")


class WriteBackError(Exception):
    """Base for write-back failures. Messages never contain the provider token."""


class WriteBackConfigError(WriteBackError):
    """Misconfiguration or a refused unsafe target — missing token/repo, or a branch
    that would collide with the default branch. Maps to a client error (``400``)."""


class WriteBackProviderError(WriteBackError):
    """The provider (git/API) failed — surfaced cleanly, no PR opened, no partial
    state. Maps to a bad-gateway error (``502``)."""


@dataclass(frozen=True)
class ProposalRequest:
    """The unit of work handed to a :class:`GitProvider`.

    Deliberately holds **no token** — the secret is passed to
    :meth:`GitProvider.open_pull_request` as a separate keyword, so this object is
    safe to log or serialize.
    """

    repo_url: str
    base_branch: str
    head_branch: str
    path: str
    content: str
    commit_message: str
    title: str
    body: str


@dataclass(frozen=True)
class ProposalResult:
    """The outcome of a successful proposal — safe to audit / return to the UI."""

    pr_url: str
    branch: str
    base_branch: str
    path: str


@runtime_checkable
class GitProvider(Protocol):
    """The one mockable seam: create the branch, commit, push, and open the PR.

    Implementations MUST NOT push to ``request.base_branch`` directly — they commit
    to ``request.head_branch`` and open a PR *into* the base. Returns the PR URL.
    """

    def open_pull_request(self, request: ProposalRequest, *, token: str) -> str: ...


# --------------------------------------------------------------------------- #
# Serialization — canonical repo YAML (round-trips with the read-sync layout)
# --------------------------------------------------------------------------- #
def _canonical_entry(policy: dict[str, Any]) -> dict[str, Any]:
    """The single authored c7n policy dict written to the repo.

    Built from the stored ``spec``'s first (only) policy, with ``name`` / ``resource``
    / ``description`` reconciled to the record's fields so a re-import via
    :func:`gitops.policy_record_from_doc` reproduces the record exactly. Empty
    ``resource`` / absent ``description`` are *omitted* (read-sync reads them back as
    ``""`` / ``None``), keeping the round-trip exact.
    """
    entry: dict[str, Any] = {}
    spec = policy.get("spec")
    if isinstance(spec, dict):
        policies = spec.get("policies")
        if isinstance(policies, list) and policies and isinstance(policies[0], dict):
            entry = dict(policies[0])

    entry["name"] = policy["name"]

    resource = policy.get("resource_type") or ""
    if resource:
        entry["resource"] = resource
    else:
        entry.pop("resource", None)

    description = policy.get("description")
    if description is not None:
        entry["description"] = description
    else:
        entry.pop("description", None)

    return entry


def canonical_document(policy: dict[str, Any]) -> dict[str, Any]:
    """The full repo document for a policy: ``{"policies": [<entry>]}``."""
    return {"policies": [_canonical_entry(policy)]}


def serialize_policy(policy: dict[str, Any]) -> str:
    """Serialize a persisted policy to its canonical repo YAML.

    Round-trips with the read-sync layout: parsing the result and passing the entry
    through :func:`gitops.policy_record_from_doc` yields the same name / resource /
    spec / description (no drift on re-import).
    """
    return yaml.safe_dump(
        canonical_document(policy), sort_keys=False, default_flow_style=False, allow_unicode=True
    )


# --------------------------------------------------------------------------- #
# Propose — branch / commit / push / open-PR via the injected provider
# --------------------------------------------------------------------------- #
def branch_name(policy: dict[str, Any], settings: Any | None = None) -> str:
    """Per-policy proposal branch: ``<prefix><name>-<version>`` (sanitised name)."""
    settings = settings if settings is not None else get_settings()
    safe = str(policy["name"]).strip().replace("/", "-").replace("\\", "-")
    return f"{settings.gitops_writeback_branch_prefix}{safe}-{policy.get('version', 1)}"


def _resolve_repo_url(settings: Any) -> str:
    """The write-back repo — a dedicated URL if set, else the read-sync repo."""
    return settings.gitops_writeback_repo_url or settings.gitops_repo_url


def _pr_body(policy: dict[str, Any], actor: str | None) -> str:
    """Templated PR description — diff summary + author. Never contains the token."""
    return (
        f"Automated policy proposal from CloudWarden.\n\n"
        f"- **Policy:** `{policy['name']}` (v{policy.get('version', 1)})\n"
        f"- **Resource:** `{policy.get('resource_type') or 'n/a'}`\n"
        f"- **Proposed by:** {actor or 'unknown'}\n\n"
        f"Review and merge to adopt the change. Nothing is written to the default "
        f"branch directly — this PR is the source of truth."
    )


def propose_policy_change(
    policy: dict[str, Any],
    provider: GitProvider,
    *,
    actor: str | None = None,
    settings: Any | None = None,
) -> ProposalResult:
    """Propose ``policy``'s current state as a pull request. Never touches the default
    branch directly.

    Raises :class:`WriteBackConfigError` when the provider token or repo is missing,
    or when the computed branch would collide with the default branch (the unsafe
    target is refused *before* the provider is called — no partial state). Raises
    :class:`WriteBackProviderError` when the provider fails or returns no URL.
    """
    settings = settings if settings is not None else get_settings()

    token = settings.gitops_writeback_token
    if not token:
        raise WriteBackConfigError(
            "no provider token configured — set GITOPS_WRITEBACK_TOKEN to propose changes"
        )
    repo_url = _resolve_repo_url(settings)
    if not repo_url:
        raise WriteBackConfigError(
            "no write-back repo configured — set GITOPS_WRITEBACK_REPO_URL (or GITOPS_REPO_URL)"
        )

    base_branch = settings.gitops_branch
    head_branch = branch_name(policy, settings)
    if not head_branch or head_branch == base_branch:
        raise WriteBackConfigError(
            f"refusing to target the default branch ({base_branch!r}); "
            "a proposal must open a PR from a new branch"
        )

    request = ProposalRequest(
        repo_url=repo_url,
        base_branch=base_branch,
        head_branch=head_branch,
        path=gitops.repo_policy_path(policy["name"], settings),
        content=serialize_policy(policy),
        commit_message=f"policy({policy['name']}): propose update via CloudWarden",
        title=f"CloudWarden: propose update to policy '{policy['name']}'",
        body=_pr_body(policy, actor),
    )

    try:
        pr_url = provider.open_pull_request(request, token=token)
    except Exception as exc:  # noqa: BLE001 - surface any provider/network failure cleanly
        raise WriteBackProviderError(f"provider failed to open pull request: {exc}") from exc
    if not pr_url:
        raise WriteBackProviderError("provider returned no pull-request URL")

    logger.info(
        "policy proposal opened: policy=%s branch=%s pr=%s",
        policy["name"],
        request.head_branch,
        pr_url,
    )
    return ProposalResult(
        pr_url=pr_url, branch=request.head_branch, base_branch=base_branch, path=request.path
    )


# --------------------------------------------------------------------------- #
# Live provider — real git + GitHub/GitLab API. Needs git + network, so it is out
# of scope for the offline unit suite (tests inject a fake GitProvider).
# --------------------------------------------------------------------------- #
class LiveGitProvider:  # pragma: no cover - needs git + network
    """Clone the repo, commit the file on a new branch, push, open a PR via REST.

    ``provider`` selects the REST flavour (``github`` | ``gitlab``). The token is used
    for both the authenticated push and the API call and is **never logged**.
    """

    def __init__(self, settings: Any):
        self.settings = settings

    def open_pull_request(self, request: ProposalRequest, *, token: str) -> str:
        import hashlib
        import subprocess
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / hashlib.sha256(request.repo_url.encode()).hexdigest()[:12]
            subprocess.run(
                [
                    "git",
                    "clone",
                    "--depth",
                    "1",
                    "--branch",
                    request.base_branch,
                    request.repo_url,
                    str(dest),
                ],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", str(dest), "checkout", "-b", request.head_branch],
                check=True,
                capture_output=True,
            )
            target = dest / request.path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(request.content, encoding="utf-8")
            subprocess.run(
                ["git", "-C", str(dest), "add", request.path], check=True, capture_output=True
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(dest),
                    "-c",
                    "user.email=cloudwarden@localhost",
                    "-c",
                    "user.name=CloudWarden",
                    "commit",
                    "-m",
                    request.commit_message,
                ],
                check=True,
                capture_output=True,
            )
            push_url = self._authenticated_url(request.repo_url, token)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(dest),
                    "push",
                    push_url,
                    f"HEAD:refs/heads/{request.head_branch}",
                ],
                check=True,
                capture_output=True,
            )
        return self._open_pr(request, token)

    @staticmethod
    def _authenticated_url(repo_url: str, token: str) -> str:
        if repo_url.startswith("https://"):
            return "https://" + f"x-access-token:{token}@" + repo_url[len("https://") :]
        return repo_url

    def _open_pr(self, request: ProposalRequest, token: str) -> str:
        import json
        import urllib.request

        provider = (self.settings.gitops_provider or "github").lower()
        if provider == "gitlab":
            api, payload, header = self._gitlab_call(request, token)
        else:
            api, payload, header = self._github_call(request, token)
        req = urllib.request.Request(
            api,
            data=json.dumps(payload).encode(),
            method="POST",
            headers={"Content-Type": "application/json", **header},
        )
        with urllib.request.urlopen(req) as resp:  # noqa: S310 - fixed provider API host
            data = json.loads(resp.read().decode())
        return data.get("html_url") or data.get("web_url") or ""

    def _github_call(self, request: ProposalRequest, token: str):
        owner_repo = self._owner_repo(request.repo_url)
        api = f"https://api.github.com/repos/{owner_repo}/pulls"
        payload = {
            "title": request.title,
            "body": request.body,
            "head": request.head_branch,
            "base": request.base_branch,
        }
        return (
            api,
            payload,
            {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        )

    def _gitlab_call(self, request: ProposalRequest, token: str):
        import urllib.parse

        project = urllib.parse.quote(self._owner_repo(request.repo_url), safe="")
        api = f"https://gitlab.com/api/v4/projects/{project}/merge_requests"
        payload = {
            "title": request.title,
            "description": request.body,
            "source_branch": request.head_branch,
            "target_branch": request.base_branch,
        }
        return api, payload, {"PRIVATE-TOKEN": token}

    @staticmethod
    def _owner_repo(repo_url: str) -> str:
        tail = repo_url.rstrip("/")
        if tail.endswith(".git"):
            tail = tail[: -len(".git")]
        parts = tail.replace("https://", "").split("/", 1)
        return parts[1] if len(parts) > 1 else tail


def default_provider(settings: Any | None = None) -> GitProvider:  # pragma: no cover - live path
    """Build the live provider (git + GitHub/GitLab REST). Tests inject a fake instead."""
    return LiveGitProvider(settings if settings is not None else get_settings())
