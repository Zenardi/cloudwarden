"""M12.2 — AWS onboarding & execution (second cloud behind the M12.1 seam).

Everything is exercised with **injected** STS clients / offline fixtures — no
test ever reaches AWS. Covers onboarding + credential validation, a c7n aws
policy dry-run matching fixture resources, and AWS asset ingestion into AssetDB
tagged ``provider='aws'``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from azure_finops.azure.context import AccountContext
from azure_finops.models import ResourceRecord
from azure_finops.providers import base, registry
from azure_finops.providers.aws import AwsProvider, InvalidCredentialsError


# --- Fake STS clients (never touch AWS) ------------------------------------ #
class _FakeSts:
    """A stand-in STS client that records calls and returns a fixed identity."""

    def __init__(self, account: str = "123456789012") -> None:
        self.account = account
        self.calls = 0

    def get_caller_identity(self) -> dict:
        self.calls += 1
        return {
            "Account": self.account,
            "Arn": f"arn:aws:iam::{self.account}:role/finops-read",
            "UserId": "AROAEXAMPLE:finops",
        }


class _BoomSts:
    """An STS client whose call fails (bad/expired credentials)."""

    def get_caller_identity(self) -> dict:
        raise RuntimeError("The security token included in the request is invalid")


# --------------------------------------------------------------------------- #
# Registry + interface
# --------------------------------------------------------------------------- #
def test_registry_returns_aws_provider() -> None:
    provider = registry.get("aws")
    assert provider.name == "aws"
    assert isinstance(provider, AwsProvider)


def test_aws_provider_implements_interface() -> None:
    provider = registry.get("aws")
    assert isinstance(provider, base.CloudProvider)
    for attr in (
        "register_resources",
        "resource_registry",
        "account_context",
        "default_account_id",
        "build_session",
        "validate_account",
        "collect_assets",
        "run_policy",
    ):
        assert callable(getattr(provider, attr))


def test_aws_account_context_is_provider_aws() -> None:
    ctx = registry.get("aws").account_context(account_id="123456789012", display_name="Prod")
    assert isinstance(ctx, AccountContext)
    assert ctx.provider == "aws"
    assert ctx.account_id == "123456789012"


def test_aws_default_account_id_reads_settings() -> None:
    settings = SimpleNamespace(aws_account_id="999988887777")
    assert registry.get("aws").default_account_id(settings) == "999988887777"


# --------------------------------------------------------------------------- #
# Onboarding + credential validation (injected client — no live AWS)
# --------------------------------------------------------------------------- #
def test_aws_account_onboard() -> None:
    # Arrange
    sts = _FakeSts("123456789012")
    # Act
    identity = registry.get("aws").validate_account(account_id="123456789012", client=sts)
    # Assert
    assert identity["account_id"] == "123456789012"
    assert identity["arn"].endswith("role/finops-read")


def test_aws_uses_injected_client() -> None:
    # Arrange — a spy STS; if the provider went live it would never be called.
    sts = _FakeSts("123456789012")
    # Act
    registry.get("aws").validate_account(account_id="123456789012", client=sts)
    # Assert — the injected client did the work.
    assert sts.calls == 1


def test_aws_onboard_invalid_credentials_error() -> None:
    with pytest.raises(InvalidCredentialsError):
        registry.get("aws").validate_account(account_id="123456789012", client=_BoomSts())


def test_aws_onboard_account_mismatch_error() -> None:
    # STS reports a different account than the one being onboarded → reject.
    with pytest.raises(InvalidCredentialsError):
        registry.get("aws").validate_account(
            account_id="000000000000", client=_FakeSts("123456789012")
        )


# --------------------------------------------------------------------------- #
# c7n aws policy dry-run — matches from the fixture, offline
# --------------------------------------------------------------------------- #
def test_aws_policy_dryrun_matches_fixture() -> None:
    spec = {"policies": [{"name": "running-ec2", "resource": "aws.ec2"}]}
    result = registry.get("aws").run_policy(spec, account_id="123456789012", dry_run=True)
    assert result["dry_run"] is True
    assert result["matched"] > 0
    assert result["resource_type"] == "aws.ec2"
    assert all(r["type"] == "aws.ec2" for r in result["resources"])


def test_aws_policy_dryrun_unknown_resource_matches_nothing() -> None:
    spec = {"policies": [{"name": "none", "resource": "aws.kinesis"}]}
    result = registry.get("aws").run_policy(spec, account_id="123456789012")
    assert result["matched"] == 0
    assert result["resources"] == []


def test_aws_run_policy_uses_injected_runner() -> None:
    calls: list[dict] = []

    class _Runner:
        def run(self, spec, account_id, region, dry_run):
            calls.append({"account_id": account_id, "dry_run": dry_run})
            return {"matched": 1, "resources": [{"type": "aws.ec2"}], "dry_run": dry_run}

    result = registry.get("aws").run_policy(
        {"policies": [{"name": "p", "resource": "aws.ec2"}]},
        account_id="123456789012",
        runner=_Runner(),
    )
    assert result["matched"] == 1
    assert calls == [{"account_id": "123456789012", "dry_run": True}]


# --------------------------------------------------------------------------- #
# Asset ingestion — provider='aws'
# --------------------------------------------------------------------------- #
def test_aws_collect_assets_are_tagged_aws() -> None:
    records = registry.get("aws").collect_assets(account_id="111122223333")
    assert records, "fixture should yield AWS assets"
    assert all(isinstance(r, ResourceRecord) for r in records)
    assert all(r.provider == "aws" for r in records)
    assert all(r.type.startswith("aws.") for r in records)
    # The account id is threaded onto the account (subscription) field and the ARN.
    assert all(r.subscription_id == "111122223333" for r in records)
    assert all("111122223333" in r.resource_id for r in records)


def test_aws_assets_ingested_provider_aws(db) -> None:
    from azure_finops.models import AssetFilter, AssetQuery
    from azure_finops.storage import repository as repo
    from azure_finops.storage.db import session_scope

    provider = registry.get("aws")
    records = provider.collect_assets(account_id="111122223333")

    with session_scope() as session:
        new_ids = repo.upsert_assets(session, records)
    assert new_ids, "first ingestion inserts all AWS assets"

    with session_scope() as session:
        rows = repo.query_assets(
            session,
            AssetQuery(filters=[AssetFilter(column="provider", op="eq", value="aws")]),
        )
    assert rows, "assets are queryable by provider=aws"
    assert all(row["provider"] == "aws" for row in rows)
    assert len(rows) == len(records)


# --------------------------------------------------------------------------- #
# c7n core registers AWS resource types (AWS is native to c7n — no c7n-aws pkg)
# --------------------------------------------------------------------------- #
def test_aws_resource_registry_has_ec2() -> None:
    provider = registry.get("aws")
    provider.register_resources()
    assert "ec2" in provider.resource_registry().keys()


def test_aws_register_resources_is_idempotent() -> None:
    # A second call short-circuits on the guard (no re-registration).
    provider = registry.get("aws")
    provider.register_resources()
    provider.register_resources()  # no-op
    assert provider._registered is True


# --------------------------------------------------------------------------- #
# API — onboarding / ingestion / dry-run endpoints (injected STS, mock mode)
# --------------------------------------------------------------------------- #
def test_get_aws_sts_client_seam_defaults_none() -> None:
    # The seam returns None so the provider falls back to a live boto3 client.
    from azure_finops.api.main import get_aws_sts_client

    assert get_aws_sts_client() is None


def test_aws_onboard_endpoint_requires_account_id(db) -> None:
    from fastapi.testclient import TestClient

    from azure_finops.api.main import app

    with TestClient(app) as c:
        resp = c.post("/api/aws/accounts", json={"account_id": "  ", "display_name": "x"})
        assert resp.status_code == 400, resp.text


def test_aws_onboard_endpoint(db) -> None:
    from fastapi.testclient import TestClient

    from azure_finops.api.main import app, get_aws_sts_client

    app.dependency_overrides[get_aws_sts_client] = lambda: _FakeSts("111122223333")
    try:
        with TestClient(app) as c:
            resp = c.post(
                "/api/aws/accounts",
                json={"account_id": "111122223333", "display_name": "AWS Prod"},
            )
            assert resp.status_code == 201, resp.text
            body = resp.json()
            assert body["account"]["provider"] == "aws"
            assert body["identity"]["account_id"] == "111122223333"
            # The account shows up in the unified accounts list tagged aws.
            subs = {s["subscription_id"]: s["provider"] for s in c.get("/api/subscriptions").json()}
            assert subs["111122223333"] == "aws"
    finally:
        app.dependency_overrides.pop(get_aws_sts_client, None)


def test_aws_onboard_endpoint_invalid_credentials(db) -> None:
    from fastapi.testclient import TestClient

    from azure_finops.api.main import app, get_aws_sts_client

    app.dependency_overrides[get_aws_sts_client] = lambda: _BoomSts()
    try:
        with TestClient(app) as c:
            resp = c.post(
                "/api/aws/accounts",
                json={"account_id": "111122223333", "display_name": "x"},
            )
            assert resp.status_code == 400, resp.text
    finally:
        app.dependency_overrides.pop(get_aws_sts_client, None)


def test_aws_ingest_endpoint(db) -> None:
    from fastapi.testclient import TestClient

    from azure_finops.api.main import app

    with TestClient(app) as c:
        resp = c.post("/api/aws/accounts/111122223333/ingest")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["provider"] == "aws"
        assert body["assets"] > 0
        # Idempotent: a second ingestion inserts no new assets.
        again = c.post("/api/aws/accounts/111122223333/ingest").json()
        assert again["new"] == 0


def test_aws_policy_dryrun_endpoint(db) -> None:
    from fastapi.testclient import TestClient

    from azure_finops.api.main import app

    with TestClient(app) as c:
        resp = c.post(
            "/api/aws/policies/dryrun",
            json={
                "account_id": "111122223333",
                "spec": {"policies": [{"name": "running-ec2", "resource": "aws.ec2"}]},
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["matched"] > 0
        assert all(r["type"] == "aws.ec2" for r in body["resources"])
