"""Asset query API (M4.2): filterable, injection-safe queries over AssetDB.

Written test-first (TDD). DB-backed (the ``db`` testcontainers fixture). Exercises
the allow-listed, fully-parameterized query builder (`repo.query_assets`) and the
`POST /api/assets/query` endpoint: filtering by type / region / subscription / tag
returns the right rows; an unknown column or operator is rejected with **400** and
never executed; a SQL-injection payload in a tag value is treated as a **literal**
(zero rows, the `assets` table untouched); and pagination caps ``limit`` with a
stable order.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from azure_finops import models as m
from azure_finops.api.main import app
from azure_finops.storage import repository as repo
from azure_finops.storage.db import session_scope


def _asset(
    resource_id: str,
    *,
    type: str = "microsoft.compute/virtualmachines",
    location: str = "eastus",
    subscription_id: str = "sub-1",
    name: str | None = None,
    tags: dict | None = None,
) -> m.ResourceRecord:
    return m.ResourceRecord(
        resource_id=resource_id,
        name=name or resource_id.split("/")[-1],
        type=type,
        location=location,
        resource_group="rg-1",
        subscription_id=subscription_id,
        sku="Standard_D2s_v5",
        tags=tags or {},
        power_state=None,
        config={},
    )


# --------------------------------------------------------------------------- #
# Filtering (repo builder)
# --------------------------------------------------------------------------- #
def test_query_by_type_returns_matches(db) -> None:
    with session_scope() as s:
        repo.upsert_assets(
            s,
            [
                _asset("/vm/1", type="microsoft.compute/virtualmachines"),
                _asset("/disk/1", type="microsoft.compute/disks"),
            ],
        )
    with session_scope() as s:
        rows = repo.query_assets(
            s,
            m.AssetQuery(
                filters=[m.AssetFilter(column="type", value="microsoft.compute/virtualmachines")]
            ),
        )
    assert {r["resource_id"] for r in rows} == {"/vm/1"}


def test_query_by_tag_key_value(db) -> None:
    with session_scope() as s:
        repo.upsert_assets(
            s,
            [_asset("/vm/1", tags={"env": "prod"}), _asset("/vm/2", tags={"env": "dev"})],
        )
    with session_scope() as s:
        rows = repo.query_assets(s, m.AssetQuery(tags={"env": "prod"}))
    assert {r["resource_id"] for r in rows} == {"/vm/1"}


def test_query_by_subscription_and_region(db) -> None:
    with session_scope() as s:
        repo.upsert_assets(
            s,
            [
                _asset("/a/1", subscription_id="sub-a", location="eastus"),
                _asset("/a/2", subscription_id="sub-b", location="eastus"),
                _asset("/a/3", subscription_id="sub-a", location="westus"),
            ],
        )
    with session_scope() as s:
        rows = repo.query_assets(
            s,
            m.AssetQuery(
                filters=[
                    m.AssetFilter(column="subscription_id", value="sub-a"),
                    m.AssetFilter(column="location", value="eastus"),
                ]
            ),
        )
    assert {r["resource_id"] for r in rows} == {"/a/1"}


def test_query_operators_ne_contains_in(db) -> None:
    with session_scope() as s:
        repo.upsert_assets(
            s,
            [
                _asset("/vm/web", name="web", type="microsoft.compute/virtualmachines"),
                _asset("/vm/batch", name="batch", type="microsoft.compute/virtualmachines"),
                _asset("/disk/1", type="microsoft.compute/disks"),
            ],
        )
    with session_scope() as s:
        ne = repo.query_assets(
            s,
            m.AssetQuery(
                filters=[m.AssetFilter(column="type", op="ne", value="microsoft.compute/disks")]
            ),
        )
        contains = repo.query_assets(
            s, m.AssetQuery(filters=[m.AssetFilter(column="name", op="contains", value="atc")])
        )
        in_ = repo.query_assets(
            s,
            m.AssetQuery(
                filters=[m.AssetFilter(column="type", op="in", value=["microsoft.compute/disks"])]
            ),
        )
    assert {r["resource_id"] for r in ne} == {"/vm/web", "/vm/batch"}
    assert {r["resource_id"] for r in contains} == {"/vm/batch"}
    assert {r["resource_id"] for r in in_} == {"/disk/1"}


# --------------------------------------------------------------------------- #
# Injection-safety & validation (via the API)
# --------------------------------------------------------------------------- #
def test_query_unknown_column_returns_400(db) -> None:
    resp = TestClient(app).post(
        "/api/assets/query",
        json={"filters": [{"column": "bogus; DROP TABLE assets", "value": "x"}]},
    )
    assert resp.status_code == 400


def test_query_unknown_operator_returns_400(db) -> None:
    resp = TestClient(app).post(
        "/api/assets/query", json={"filters": [{"column": "type", "op": "bogus", "value": "x"}]}
    )
    assert resp.status_code == 400


def test_query_in_requires_list_returns_400(db) -> None:
    resp = TestClient(app).post(
        "/api/assets/query", json={"filters": [{"column": "type", "op": "in", "value": "notalist"}]}
    )
    assert resp.status_code == 400


def test_query_injection_payload_is_literal_safe(db) -> None:
    with session_scope() as s:
        repo.upsert_assets(s, [_asset("/vm/1", tags={"env": "prod"})])

    payload = "' OR 1=1; DROP TABLE assets; --"
    resp = TestClient(app).post("/api/assets/query", json={"tags": {"env": payload}})
    assert resp.status_code == 200
    assert resp.json() == []  # literal value matches nothing

    with session_scope() as s:
        n = repo._rows(s, "SELECT count(*) AS n FROM assets")[0]["n"]
    assert n == 1  # table untouched — nothing executed


# --------------------------------------------------------------------------- #
# Pagination
# --------------------------------------------------------------------------- #
def test_query_pagination_caps_limit(db) -> None:
    with session_scope() as s:
        repo.upsert_assets(s, [_asset(f"/a/{i}") for i in range(5)])

    with session_scope() as s:
        page1 = repo.query_assets(s, m.AssetQuery(limit=2, offset=0))
        page2 = repo.query_assets(s, m.AssetQuery(limit=2, offset=2))
        huge = repo.query_assets(s, m.AssetQuery(limit=1_000_000))  # over the cap → clamped
    assert len(page1) == 2 and len(page2) == 2
    assert not ({r["resource_id"] for r in page1} & {r["resource_id"] for r in page2})
    assert len(huge) == 5  # clamped, returns all, no error
    # stable order: the same query twice yields the same order
    with session_scope() as s:
        again = repo.query_assets(s, m.AssetQuery(limit=2, offset=0))
    assert [r["resource_id"] for r in page1] == [r["resource_id"] for r in again]


def test_query_api_returns_assets(db) -> None:
    with session_scope() as s:
        repo.upsert_assets(s, [_asset("/vm/1", type="microsoft.compute/virtualmachines")])
    resp = TestClient(app).post(
        "/api/assets/query",
        json={"filters": [{"column": "type", "value": "microsoft.compute/virtualmachines"}]},
    )
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1 and rows[0]["resource_id"] == "/vm/1"
