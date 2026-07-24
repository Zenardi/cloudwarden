"""AWS cloud provider (M12.2) — onboarding, execution and ingestion.

The second cloud behind the M12.1 :class:`providers.base.CloudProvider` seam.
AWS is **native to Cloud Custodian core** (the already-installed ``c7n`` package
registers ``aws.*`` resource types — there is no separate ``c7n-aws`` package),
and ``boto3`` ships transitively with ``c7n``; so this adds no new image
dependency and no new Trivy surface.

Everything stays offline in tests: onboarding validates credentials through an
**injected** STS client (``get_caller_identity``), and both policy dry-runs and
asset ingestion are backed by the ``aws_assets`` fixture — mirroring the
Azure engine's mock-fixture pattern. Live paths (real boto3/c7n sessions) are
lazily imported and marked ``# pragma: no cover``.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from ..azure._fixtures import load_fixture
from ..azure.context import AccountContext
from ..config import get_settings
from ..models import ResourceRecord

# The fixture ARNs / bucket names embed this placeholder account. On ingestion we
# rewrite it to the onboarded account so multi-account runs produce distinct
# (non-colliding) resource ids — the AWS analogue of ``azure._fixtures.retarget``.
AWS_PLACEHOLDER_ACCOUNT = "123456789012"
DEFAULT_REGION = "us-east-1"
_FIXTURE_NAME = "aws_assets"


class InvalidCredentialsError(ValueError):
    """Raised when an AWS account's credentials fail validation (bad/expired/mismatched)."""


@runtime_checkable
class StsClient(Protocol):
    """The one mockable seam onboarding talks to instead of boto3 STS directly."""

    def get_caller_identity(self) -> dict:
        """Return the caller's ``{Account, Arn, UserId}`` (or raise on bad creds)."""


@runtime_checkable
class AwsPolicyRunner(Protocol):
    """Injectable c7n-aws execution seam (tests inject a fake; default is fixture-backed)."""

    def run(self, spec: dict, account_id: str, region: str, dry_run: bool) -> dict:
        """Evaluate a policy and return the matched-resource result dict."""


def _retarget(value: str, account_id: str) -> str:
    """Rewrite the placeholder account segment of an ARN / bucket name."""
    if not value or not account_id or account_id == AWS_PLACEHOLDER_ACCOUNT:
        return value
    return value.replace(AWS_PLACEHOLDER_ACCOUNT, account_id)


def _load_items() -> list[dict]:
    """Load the recorded AWS resources (offline)."""
    return load_fixture(_FIXTURE_NAME).get("resources", [])


def _record_from_item(item: dict, account_id: str) -> ResourceRecord:
    """Map one fixture resource to a provider-neutral :class:`ResourceRecord` (provider='aws')."""
    return ResourceRecord(
        resource_id=_retarget(item["resource_id"], account_id),
        name=item.get("name", ""),
        type=item.get("type", ""),
        location=item.get("region", ""),
        resource_group="",  # AWS has no resource-group concept
        subscription_id=account_id,
        provider="aws",
        tags=item.get("tags", {}),
        power_state=item.get("state"),
        config=item.get("config", {}),
    )


def _mock_run_result(spec: dict, account_id: str, region: str, dry_run: bool) -> dict:
    """Shape a matched-resource result from the fixture, filtered by the policy's resource type."""
    policy_def = (spec.get("policies") or [{}])[0]
    wanted = policy_def.get("resource")
    resources = [
        {**item, "resource_id": _retarget(item["resource_id"], account_id)}
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


class LiveAwsPolicyRunner:
    """Default runner: fixture-backed in ``FINOPS_MOCK=1``, real c7n-aws otherwise."""

    def run(self, spec: dict, account_id: str, region: str, dry_run: bool) -> dict:
        if get_settings().finops_mock:
            return _mock_run_result(spec, account_id, region, dry_run)
        return self._run_live(spec, account_id, region, dry_run)  # pragma: no cover

    def _run_live(  # pragma: no cover - requires live AWS; unit tests use mock mode
        self, spec: dict, account_id: str, region: str, dry_run: bool
    ) -> dict:
        from c7n.config import Config
        from c7n.loader import PolicyLoader
        from c7n.resources import load_resources

        load_resources(("aws.*",))
        config = Config.empty(dryrun=dry_run, region=region, account_id=account_id)
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


_default_runner: AwsPolicyRunner | None = None


def _get_default_runner() -> AwsPolicyRunner:
    global _default_runner
    if _default_runner is None:
        _default_runner = LiveAwsPolicyRunner()
    return _default_runner


class AwsProvider:
    """:class:`CloudProvider` implementation for Amazon Web Services."""

    name = "aws"

    def __init__(self) -> None:
        self._registered = False

    # --- CloudProvider interface (M12.1) ---------------------------------- #
    def register_resources(self) -> None:
        """Register ``aws.*`` c7n resource types (native to c7n core; idempotent)."""
        if self._registered:
            return
        from c7n.resources import load_resources

        load_resources(("aws.*",))
        self._registered = True

    def resource_registry(self) -> Any:
        """Return the c7n AWS resource registry (keys un-prefixed, e.g. ``ec2``)."""
        from c7n.provider import clouds

        return clouds[self.name].resources

    def account_context(
        self,
        *,
        account_id: str,
        credential: Any | None = None,
        display_name: str | None = None,
    ) -> AccountContext:
        """Build an AWS per-run account context (an ``AccountContext``)."""
        return AccountContext(
            account_id=account_id,
            provider=self.name,
            credential=credential,
            display_name=display_name,
        )

    def default_account_id(self, settings: Any) -> str:
        """The default AWS account id from settings."""
        return settings.aws_account_id

    def build_session(self, account_id: str) -> Any:  # pragma: no cover - live network
        """Build a live c7n AWS session for an account."""
        from c7n.credentials import SessionFactory

        return SessionFactory(region=DEFAULT_REGION)()

    def preventive_translator(self) -> Any:
        """The AWS Service Control Policy translator for preventive guardrails (M14.10)."""
        from .preventive import aws_scp

        return aws_scp

    def collect_cost(self, *, account: Any | None = None, client: Any | None = None) -> list[Any]:
        """Collect amortized AWS cost rows via Cost Explorer (M14.11)."""
        from . import aws_cost

        return aws_cost.collect_cost(client=client, account=account)

    # --- M12.2: onboarding, execution, ingestion -------------------------- #
    def validate_account(
        self,
        *,
        account_id: str,
        credential: dict | None = None,
        client: StsClient | None = None,
    ) -> dict[str, Any]:
        """Validate an AWS account's credentials via STS ``get_caller_identity``.

        Uses the injected ``client`` when given (tests, no network); otherwise
        builds a live boto3 STS client from ``credential``. Raises
        :class:`InvalidCredentialsError` when the call fails or the caller's
        account does not match the account being onboarded.
        """
        sts = client if client is not None else self._default_sts_client(credential)
        try:
            identity = sts.get_caller_identity()
        except Exception as exc:  # noqa: BLE001 - surfaced as a structured onboarding error
            raise InvalidCredentialsError(f"AWS credential validation failed: {exc}") from exc
        reported = identity.get("Account")
        if account_id and reported and reported != account_id:
            raise InvalidCredentialsError(
                f"caller identity account {reported!r} does not match "
                f"onboarded account {account_id!r}"
            )
        return {
            "account_id": reported,
            "arn": identity.get("Arn"),
            "user_id": identity.get("UserId"),
        }

    def _default_sts_client(self, credential: dict | None) -> StsClient:  # pragma: no cover - live
        import boto3

        credential = credential or {}
        kwargs: dict[str, Any] = {"region_name": credential.get("region") or DEFAULT_REGION}
        if credential.get("access_key_id") and credential.get("secret_access_key"):
            kwargs["aws_access_key_id"] = credential["access_key_id"]
            kwargs["aws_secret_access_key"] = credential["secret_access_key"]
        return boto3.client("sts", **kwargs)

    def collect_assets(
        self, *, account_id: str, source: list[dict] | None = None
    ) -> list[ResourceRecord]:
        """Ingest AWS resources for an account as ``ResourceRecord``s (provider='aws').

        ``source`` overrides the recorded fixture (used by the live collector once
        it exists); by default the offline ``aws_assets`` fixture is used.
        """
        items = source if source is not None else _load_items()
        return [_record_from_item(item, account_id) for item in items]

    def run_policy(
        self,
        spec: dict,
        *,
        account_id: str,
        region: str | None = None,
        dry_run: bool = True,
        runner: AwsPolicyRunner | None = None,
    ) -> dict:
        """Evaluate a c7n aws policy against an account (fixture-backed unless live)."""
        runner = runner if runner is not None else _get_default_runner()
        return runner.run(spec, account_id, region or DEFAULT_REGION, dry_run)
