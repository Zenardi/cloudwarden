"""GCP cloud provider (M12.3) — onboarding, execution and ingestion.

The third cloud behind the M12.1 :class:`providers.base.CloudProvider` seam.
Unlike AWS (native to c7n core), GCP resource types live in the separate
``c7n_gcp`` package, which pulls the heavy ``google-*`` client tree. To keep the
runtime image (and its Trivy surface) minimal, ``c7n_gcp`` / ``google-*`` are an
**optional live-only extra** that is *not* installed by default: onboarding,
policy dry-runs and asset ingestion all work fully offline through **injected**
clients and the ``gcp_assets`` fixture (mirroring the AWS provider). The live
paths (real Resource-Manager credential checks, c7n-gcp execution/session) are
lazily imported and marked ``# pragma: no cover``.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from ..azure._fixtures import load_fixture
from ..azure.context import AccountContext
from ..config import get_settings
from ..models import ResourceRecord

# The fixture resource ids embed this placeholder project. On ingestion we rewrite
# it to the onboarded project so multi-project runs produce distinct (non-colliding)
# resource ids — the GCP analogue of ``azure._fixtures.retarget``.
GCP_PLACEHOLDER_PROJECT = "example-project-123456"
DEFAULT_REGION = "us-central1"
_FIXTURE_NAME = "gcp_assets"


class InvalidCredentialsError(ValueError):
    """Raised when a GCP project's credentials fail validation (bad/expired/mismatched)."""


@runtime_checkable
class ResourceManagerClient(Protocol):
    """The one mockable seam onboarding talks to instead of the live GCP API."""

    def get_project(self, project_id: str) -> dict:
        """Return the project's ``{projectId, projectNumber, lifecycleState}`` (or raise)."""


@runtime_checkable
class GcpPolicyRunner(Protocol):
    """Injectable c7n-gcp execution seam (tests inject a fake; default is fixture-backed)."""

    def run(self, spec: dict, project_id: str, region: str, dry_run: bool) -> dict:
        """Evaluate a policy and return the matched-resource result dict."""


def _retarget(value: str, project_id: str) -> str:
    """Rewrite the placeholder project segment of a resource id."""
    if not value or not project_id or project_id == GCP_PLACEHOLDER_PROJECT:
        return value
    return value.replace(GCP_PLACEHOLDER_PROJECT, project_id)


def _load_items() -> list[dict]:
    """Load the recorded GCP resources (offline)."""
    return load_fixture(_FIXTURE_NAME).get("resources", [])


def _record_from_item(item: dict, project_id: str) -> ResourceRecord:
    """Map one fixture resource to a provider-neutral :class:`ResourceRecord` (provider='gcp')."""
    return ResourceRecord(
        resource_id=_retarget(item["resource_id"], project_id),
        name=item.get("name", ""),
        type=item.get("type", ""),
        location=item.get("region", ""),
        resource_group="",  # GCP has no resource-group concept
        subscription_id=project_id,
        provider="gcp",
        tags=item.get("tags", {}),
        power_state=item.get("state"),
        config=item.get("config", {}),
    )


def _mock_run_result(spec: dict, project_id: str, region: str, dry_run: bool) -> dict:
    """Shape a matched-resource result from the fixture, filtered by the policy's resource type."""
    policy_def = (spec.get("policies") or [{}])[0]
    wanted = policy_def.get("resource")
    resources = [
        {**item, "resource_id": _retarget(item["resource_id"], project_id)}
        for item in _load_items()
        if item.get("type") == wanted
    ]
    return {
        "policy_name": policy_def.get("name"),
        "resource_type": wanted,
        "region": region,
        "dry_run": dry_run,
        "matched": len(resources),
        "resources": resources,
    }


class LiveGcpPolicyRunner:
    """Default runner: fixture-backed in ``FINOPS_MOCK=1``, real c7n-gcp otherwise."""

    def run(self, spec: dict, project_id: str, region: str, dry_run: bool) -> dict:
        if get_settings().finops_mock:
            return _mock_run_result(spec, project_id, region, dry_run)
        return self._run_live(spec, project_id, region, dry_run)  # pragma: no cover

    def _run_live(  # pragma: no cover - requires live GCP + optional c7n-gcp extra
        self, spec: dict, project_id: str, region: str, dry_run: bool
    ) -> dict:
        from c7n.config import Config
        from c7n.loader import PolicyLoader
        from c7n.resources import load_resources

        load_resources(("gcp.*",))
        config = Config.empty(dryrun=dry_run, region=region, project_id=project_id)
        collection = PolicyLoader(config).load_data(spec, "memory://custodian")
        matched: list[Any] = []
        for policy in collection:
            matched.extend(policy.run() or [])
        policy_def = (spec.get("policies") or [{}])[0]
        return {
            "policy_name": policy_def.get("name"),
            "resource_type": policy_def.get("resource"),
            "region": region,
            "dry_run": dry_run,
            "matched": len(matched),
            "resources": matched,
        }


_default_runner: GcpPolicyRunner | None = None


def _get_default_runner() -> GcpPolicyRunner:
    global _default_runner
    if _default_runner is None:
        _default_runner = LiveGcpPolicyRunner()
    return _default_runner


class GcpProvider:
    """:class:`CloudProvider` implementation for Google Cloud Platform."""

    name = "gcp"

    def __init__(self) -> None:
        self._registered = False

    # --- CloudProvider interface (M12.1) ---------------------------------- #
    def register_resources(self) -> None:  # pragma: no cover - optional c7n-gcp extra
        """Register ``gcp.*`` c7n resource types (requires the optional c7n-gcp extra)."""
        if self._registered:
            return
        from c7n.resources import load_resources

        load_resources(("gcp.*",))
        self._registered = True

    def resource_registry(self) -> Any:  # pragma: no cover - optional c7n-gcp extra
        """Return the c7n GCP resource registry (keys un-prefixed, e.g. ``instance``)."""
        from c7n.provider import clouds

        return clouds[self.name].resources

    def account_context(
        self,
        *,
        account_id: str,
        credential: Any | None = None,
        display_name: str | None = None,
    ) -> AccountContext:
        """Build a GCP per-run account context (an ``AccountContext``)."""
        return AccountContext(
            account_id=account_id,
            provider=self.name,
            credential=credential,
            display_name=display_name,
        )

    def default_account_id(self, settings: Any) -> str:
        """The default GCP project id from settings."""
        return settings.gcp_project_id

    def build_session(self, account_id: str) -> Any:  # pragma: no cover - live network
        """Build a live c7n GCP session for a project."""
        from c7n_gcp.session import Session

        return Session(project_id=account_id)

    def preventive_translator(self) -> Any:
        """The GCP Organization Policy translator for preventive guardrails (M14.10)."""
        from .preventive import gcp_orgpolicy

        return gcp_orgpolicy

    # --- M12.3: onboarding, execution, ingestion -------------------------- #
    def validate_project(
        self,
        *,
        project_id: str,
        credential: dict | None = None,
        client: ResourceManagerClient | None = None,
    ) -> dict[str, Any]:
        """Validate a GCP project's credentials via Resource Manager ``get_project``.

        Uses the injected ``client`` when given (tests, no network); otherwise
        builds a live client from the service-account ``credential``. Raises
        :class:`InvalidCredentialsError` when the call fails or the reported
        project does not match the project being onboarded.
        """
        rm = client if client is not None else self._default_client(credential)
        try:
            project = rm.get_project(project_id)
        except Exception as exc:  # noqa: BLE001 - surfaced as a structured onboarding error
            raise InvalidCredentialsError(f"GCP credential validation failed: {exc}") from exc
        reported = project.get("projectId")
        if project_id and reported and reported != project_id:
            raise InvalidCredentialsError(
                f"caller project {reported!r} does not match onboarded project {project_id!r}"
            )
        return {
            "project_id": reported,
            "project_number": project.get("projectNumber"),
            "state": project.get("lifecycleState"),
        }

    def _default_client(  # pragma: no cover - live GCP + optional google-* extra
        self, credential: dict | None
    ) -> ResourceManagerClient:
        from google.oauth2 import service_account  # type: ignore
        from googleapiclient import discovery  # type: ignore

        creds = None
        if credential and credential.get("service_account_info"):
            creds = service_account.Credentials.from_service_account_info(
                credential["service_account_info"]
            )
        crm = discovery.build("cloudresourcemanager", "v1", credentials=creds)

        class _Adapter:
            def get_project(self, project_id: str) -> dict:
                return crm.projects().get(projectId=project_id).execute()

        return _Adapter()

    def collect_assets(
        self, *, project_id: str, source: list[dict] | None = None
    ) -> list[ResourceRecord]:
        """Ingest GCP resources for a project as ``ResourceRecord``s (provider='gcp').

        ``source`` overrides the recorded fixture (used by the live collector once
        it exists); by default the offline ``gcp_assets`` fixture is used.
        """
        items = source if source is not None else _load_items()
        return [_record_from_item(item, project_id) for item in items]

    def run_policy(
        self,
        spec: dict,
        *,
        project_id: str,
        region: str | None = None,
        dry_run: bool = True,
        runner: GcpPolicyRunner | None = None,
    ) -> dict:
        """Evaluate a c7n gcp policy against a project (fixture-backed unless live)."""
        runner = runner if runner is not None else _get_default_runner()
        return runner.run(spec, project_id, region or DEFAULT_REGION, dry_run)
