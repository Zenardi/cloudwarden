"""Resource compliance explorer (M9.3): drill-down API + frontend wiring.

Written test-first (TDD). The ``/compliance`` Next.js page drills policy → matched
resources → asset detail, consuming the governance posture API (policy list +
non-compliant counts) and a new per-policy matched-resources endpoint. Backend
behaviour is DB-backed (the ``db`` fixture); the page/nav wiring is asserted
against the frontend source — there is no Node in CI, so ``next build`` + the route
curl cover the actual render, and these checks lock the wiring the page depends on.

The drill-down returns the resources flagged by each subscription's *latest*
execution of the policy — the current non-compliant set, whose size matches the
posture ``violations`` count — each linkable to its M4.5 AssetDB detail.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from azure_finops import models as m
from azure_finops.api.main import app
from azure_finops.storage import repository as repo
from azure_finops.storage.db import session_scope

_FRONTEND = Path(__file__).resolve().parents[2] / "frontend" / "app"


def _make_policy(session, name: str = "idle-vms") -> int:
    return repo.create_policy(
        session,
        name=name,
        resource_type="azure.vm",
        spec={"policies": [{"name": name, "resource": "azure.vm"}]},
    )["id"]


def _seed_match(session, *, policy_id, execution_id, subscription_id, resources) -> None:
    """Record an execution that matched ``resources`` (list of (id, type) tuples)."""
    repo.create_policy_execution(
        session,
        execution_id=execution_id,
        policy_id=policy_id,
        subscription_id=subscription_id,
    )
    repo.insert_policy_matches(
        session,
        execution_id,
        [m.PolicyMatch(resource_id=rid, resource_type=rtype) for rid, rtype in resources],
    )
    repo.finish_policy_execution(
        session, execution_id, status="succeeded", resources_matched=len(resources)
    )


# --------------------------------------------------------------------------- #
# Backing API — the data the /compliance page consumes
# --------------------------------------------------------------------------- #
def test_compliance_route_returns_200(db) -> None:
    client = TestClient(app)
    # Posture drives the policy list + non-compliant counts.
    assert client.get("/api/governance/posture").status_code == 200
    with session_scope() as s:
        pid = _make_policy(s)
    # The per-policy drill-down endpoint answers 200 for a real policy.
    assert client.get(f"/api/governance/policies/{pid}/matches").status_code == 200
    # And the page itself exists (its render is build-checked in CI).
    assert (_FRONTEND / "compliance" / "page.tsx").is_file()


def test_policy_drilldown_lists_resources(db) -> None:
    with session_scope() as s:
        pid = _make_policy(s)
        _seed_match(
            s,
            policy_id=pid,
            execution_id="e1",
            subscription_id="sub-a",
            resources=[("/vm/1", "azure.vm"), ("/vm/2", "azure.vm")],
        )

    rows = TestClient(app).get(f"/api/governance/policies/{pid}/matches").json()
    assert {r["resource_id"] for r in rows} == {"/vm/1", "/vm/2"}
    assert all(r["resource_type"] == "azure.vm" for r in rows)


def test_drilldown_spans_subscriptions(db) -> None:
    with session_scope() as s:
        pid = _make_policy(s)
        _seed_match(
            s,
            policy_id=pid,
            execution_id="e1",
            subscription_id="sub-a",
            resources=[("/vm/a", "azure.vm")],
        )
        _seed_match(
            s,
            policy_id=pid,
            execution_id="e2",
            subscription_id="sub-b",
            resources=[("/vm/b", "azure.vm")],
        )

    rows = TestClient(app).get(f"/api/governance/policies/{pid}/matches").json()
    assert {r["resource_id"] for r in rows} == {"/vm/a", "/vm/b"}


def test_drilldown_uses_latest_execution(db) -> None:
    # A re-run supersedes the older matches for the same (policy, subscription).
    with session_scope() as s:
        pid = _make_policy(s)
        _seed_match(
            s,
            policy_id=pid,
            execution_id="e1",
            subscription_id="sub-a",
            resources=[("/vm/old", "azure.vm")],
        )
        _seed_match(
            s,
            policy_id=pid,
            execution_id="e2",
            subscription_id="sub-a",
            resources=[("/vm/new", "azure.vm")],
        )

    rows = TestClient(app).get(f"/api/governance/policies/{pid}/matches").json()
    assert {r["resource_id"] for r in rows} == {"/vm/new"}


def test_resource_links_to_asset_detail(db) -> None:
    rid = "/subscriptions/s/resourcegroups/rg-1/providers/microsoft.compute/virtualmachines/vm-1"
    with session_scope() as s:
        pid = _make_policy(s)
        repo.upsert_assets(
            s,
            [
                m.ResourceRecord(
                    resource_id=rid,
                    name="vm-1",
                    type="microsoft.compute/virtualmachines",
                    location="eastus",
                    resource_group="rg-1",
                    subscription_id="s",
                    sku=None,
                    tags={},
                    power_state=None,
                    config={},
                )
            ],
        )
        _seed_match(
            s,
            policy_id=pid,
            execution_id="e1",
            subscription_id="s",
            resources=[(rid, "microsoft.compute/virtualmachines")],
        )

    rows = TestClient(app).get(f"/api/governance/policies/{pid}/matches").json()
    assert rows[0]["resource_id"] == rid
    # The matched resource id resolves to a real AssetDB detail (the link target).
    with session_scope() as s:
        assets = repo.query_assets(
            s, m.AssetQuery(filters=[m.AssetFilter(column="resource_id", value=rid)])
        )
    assert assets and assets[0]["resource_id"] == rid
    # And the page builds the M4.5 asset-detail link from the resource id.
    page = (_FRONTEND / "compliance" / "page.tsx").read_text()
    assert "/assets${" in page


def test_compliance_empty_state(db) -> None:
    client = TestClient(app)
    with session_scope() as s:
        pid = _make_policy(s)  # exists, but no executions/matches
    # A policy with no matches → empty list, not an error.
    assert client.get(f"/api/governance/policies/{pid}/matches").json() == []
    # An unknown policy → 404, not a 500.
    assert client.get("/api/governance/policies/999999/matches").status_code == 404


# --------------------------------------------------------------------------- #
# Frontend wiring (source-level — render is covered by next build + route curl)
# --------------------------------------------------------------------------- #
def test_nav_includes_compliance_link() -> None:
    nav = (_FRONTEND / "components" / "Nav.tsx").read_text()
    assert 'href="/compliance"' in nav
