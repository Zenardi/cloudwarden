"""M12.3 — GCP onboarding & execution (third cloud behind the M12.1 seam).

Everything is exercised with **injected** clients / offline fixtures — no test
ever reaches GCP. Covers project onboarding + credential validation, a c7n gcp
policy dry-run matching fixture resources, and GCP asset ingestion into AssetDB
tagged ``provider='gcp'``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from azure_finops.azure.context import AccountContext
from azure_finops.models import ResourceRecord
from azure_finops.providers import base, registry
from azure_finops.providers.gcp import GcpProvider, InvalidCredentialsError


# --- Fake Resource-Manager clients (never touch GCP) ----------------------- #
class _FakeGcp:
    """A stand-in Resource Manager client that records calls, returns a fixed project."""

    def __init__(self, project: str = "example-project-123456") -> None:
        self.project = project
        self.calls = 0

    def get_project(self, project_id: str) -> dict:
        self.calls += 1
        return {
            "projectId": self.project,
            "projectNumber": "418570161861",
            "lifecycleState": "ACTIVE",
        }


class _BoomGcp:
    """A client whose call fails (bad/expired service-account credentials)."""

    def get_project(self, project_id: str) -> dict:
        raise RuntimeError("invalid_grant: Invalid JWT Signature")


# --------------------------------------------------------------------------- #
# Registry + interface
# --------------------------------------------------------------------------- #
def test_registry_returns_gcp_provider() -> None:
    provider = registry.get("gcp")
    assert provider.name == "gcp"
    assert isinstance(provider, GcpProvider)


def test_gcp_provider_implements_interface() -> None:
    provider = registry.get("gcp")
    assert isinstance(provider, base.CloudProvider)
    for attr in (
        "register_resources",
        "resource_registry",
        "account_context",
        "default_account_id",
        "build_session",
        "validate_project",
        "collect_assets",
        "run_policy",
    ):
        assert callable(getattr(provider, attr))


def test_gcp_account_context_is_provider_gcp() -> None:
    ctx = registry.get("gcp").account_context(
        account_id="example-project-123456", display_name="Prod"
    )
    assert isinstance(ctx, AccountContext)
    assert ctx.provider == "gcp"
    assert ctx.account_id == "example-project-123456"


def test_gcp_default_account_id_reads_settings() -> None:
    settings = SimpleNamespace(gcp_project_id="my-project-999")
    assert registry.get("gcp").default_account_id(settings) == "my-project-999"


# --------------------------------------------------------------------------- #
# Onboarding + credential validation (injected client — no live GCP)
# --------------------------------------------------------------------------- #
def test_gcp_project_onboard() -> None:
    # Arrange
    client = _FakeGcp("example-project-123456")
    # Act
    identity = registry.get("gcp").validate_project(
        project_id="example-project-123456", client=client
    )
    # Assert
    assert identity["project_id"] == "example-project-123456"
    assert identity["project_number"] == "418570161861"
    assert identity["state"] == "ACTIVE"


def test_gcp_uses_injected_client() -> None:
    # Arrange — a spy client; if the provider went live it would never be called.
    client = _FakeGcp("example-project-123456")
    # Act
    registry.get("gcp").validate_project(project_id="example-project-123456", client=client)
    # Assert — the injected client did the work.
    assert client.calls == 1


def test_gcp_onboard_invalid_credentials_error() -> None:
    with pytest.raises(InvalidCredentialsError):
        registry.get("gcp").validate_project(project_id="example-project-123456", client=_BoomGcp())


def test_gcp_onboard_project_mismatch_error() -> None:
    # The client reports a different project than the one being onboarded → reject.
    with pytest.raises(InvalidCredentialsError):
        registry.get("gcp").validate_project(
            project_id="other-project-000", client=_FakeGcp("example-project-123456")
        )


# --------------------------------------------------------------------------- #
# c7n gcp policy dry-run — matches from the fixture, offline
# --------------------------------------------------------------------------- #
def test_gcp_policy_dryrun_matches_fixture() -> None:
    spec = {"policies": [{"name": "running-vms", "resource": "gcp.instance"}]}
    result = registry.get("gcp").run_policy(spec, project_id="example-project-123456", dry_run=True)
    assert result["dry_run"] is True
    assert result["matched"] > 0
    assert result["resource_type"] == "gcp.instance"
    assert all(r["type"] == "gcp.instance" for r in result["resources"])


def test_gcp_policy_dryrun_unknown_resource_matches_nothing() -> None:
    spec = {"policies": [{"name": "none", "resource": "gcp.pubsub-topic"}]}
    result = registry.get("gcp").run_policy(spec, project_id="example-project-123456")
    assert result["matched"] == 0
    assert result["resources"] == []


def test_gcp_run_policy_uses_injected_runner() -> None:
    calls: list[dict] = []

    class _Runner:
        def run(self, spec, project_id, region, dry_run):
            calls.append({"project_id": project_id, "dry_run": dry_run})
            return {"matched": 1, "resources": [{"type": "gcp.instance"}], "dry_run": dry_run}

    result = registry.get("gcp").run_policy(
        {"policies": [{"name": "p", "resource": "gcp.instance"}]},
        project_id="example-project-123456",
        runner=_Runner(),
    )
    assert result["matched"] == 1
    assert calls == [{"project_id": "example-project-123456", "dry_run": True}]


# --------------------------------------------------------------------------- #
# Asset ingestion — provider='gcp'
# --------------------------------------------------------------------------- #
def test_gcp_collect_assets_are_tagged_gcp() -> None:
    records = registry.get("gcp").collect_assets(project_id="prod-project-777")
    assert records, "fixture should yield GCP assets"
    assert all(isinstance(r, ResourceRecord) for r in records)
    assert all(r.provider == "gcp" for r in records)
    assert all(r.type.startswith("gcp.") for r in records)
    # The project id is threaded onto the account field and the resource id.
    assert all(r.subscription_id == "prod-project-777" for r in records)
    assert all("prod-project-777" in r.resource_id for r in records)


def test_gcp_assets_ingested_provider_gcp(db) -> None:
    from azure_finops.models import AssetFilter, AssetQuery
    from azure_finops.storage import repository as repo
    from azure_finops.storage.db import session_scope

    provider = registry.get("gcp")
    records = provider.collect_assets(project_id="prod-project-777")

    with session_scope() as session:
        new_ids = repo.upsert_assets(session, records)
    assert new_ids, "first ingestion inserts all GCP assets"

    with session_scope() as session:
        rows = repo.query_assets(
            session,
            AssetQuery(filters=[AssetFilter(column="provider", op="eq", value="gcp")]),
        )
    assert rows, "assets are queryable by provider=gcp"
    assert all(row["provider"] == "gcp" for row in rows)
    assert len(rows) == len(records)


# --------------------------------------------------------------------------- #
# API — onboarding / ingestion / dry-run endpoints (injected client, mock mode)
# --------------------------------------------------------------------------- #
def test_get_gcp_client_seam_defaults_none() -> None:
    from azure_finops.api.main import get_gcp_client

    assert get_gcp_client() is None


def test_gcp_onboard_endpoint_requires_project_id(db) -> None:
    from fastapi.testclient import TestClient

    from azure_finops.api.main import app

    with TestClient(app) as c:
        resp = c.post("/api/gcp/projects", json={"project_id": "  ", "display_name": "x"})
        assert resp.status_code == 400, resp.text


def test_gcp_onboard_endpoint(db) -> None:
    from fastapi.testclient import TestClient

    from azure_finops.api.main import app, get_gcp_client

    app.dependency_overrides[get_gcp_client] = lambda: _FakeGcp("prod-project-777")
    try:
        with TestClient(app) as c:
            resp = c.post(
                "/api/gcp/projects",
                json={"project_id": "prod-project-777", "display_name": "GCP Prod"},
            )
            assert resp.status_code == 201, resp.text
            body = resp.json()
            assert body["account"]["provider"] == "gcp"
            assert body["identity"]["project_id"] == "prod-project-777"
            subs = {s["subscription_id"]: s["provider"] for s in c.get("/api/subscriptions").json()}
            assert subs["prod-project-777"] == "gcp"
    finally:
        app.dependency_overrides.pop(get_gcp_client, None)


def test_gcp_onboard_endpoint_invalid_credentials(db) -> None:
    from fastapi.testclient import TestClient

    from azure_finops.api.main import app, get_gcp_client

    app.dependency_overrides[get_gcp_client] = lambda: _BoomGcp()
    try:
        with TestClient(app) as c:
            resp = c.post(
                "/api/gcp/projects",
                json={"project_id": "prod-project-777", "display_name": "x"},
            )
            assert resp.status_code == 400, resp.text
    finally:
        app.dependency_overrides.pop(get_gcp_client, None)


def test_gcp_ingest_endpoint(db) -> None:
    from fastapi.testclient import TestClient

    from azure_finops.api.main import app

    with TestClient(app) as c:
        resp = c.post("/api/gcp/projects/prod-project-777/ingest")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["provider"] == "gcp"
        assert body["assets"] > 0
        # Idempotent: a second ingestion inserts no new assets.
        again = c.post("/api/gcp/projects/prod-project-777/ingest").json()
        assert again["new"] == 0


def test_gcp_policy_dryrun_endpoint(db) -> None:
    from fastapi.testclient import TestClient

    from azure_finops.api.main import app

    with TestClient(app) as c:
        resp = c.post(
            "/api/gcp/policies/dryrun",
            json={
                "project_id": "prod-project-777",
                "spec": {"policies": [{"name": "running-vms", "resource": "gcp.instance"}]},
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["matched"] > 0
        assert all(r["type"] == "gcp.instance" for r in body["resources"])
