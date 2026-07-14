"""Compliance posture (M9.1): ``v_governance_posture`` + repo helper + API.

Written test-first (TDD). DB-backed (the ``db`` testcontainers fixture) against
seeded ``PolicyExecution`` rows. Posture is a *current-state* snapshot: the
**latest** execution per ``(policy, subscription)`` decides that pair's posture —
``compliant`` when it matched nothing, ``non_compliant`` when it matched ≥1
resource. The repo helper rolls those pairs up three ways — by policy, by
subscription and by collection — plus a ``totals`` block. With nothing executed
yet the totals are zeroed and the group lists empty — never an error. ``GET
/api/governance/posture`` is a thin read over the same helper.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from cloudwarden.api.main import app
from cloudwarden.storage import repository as repo
from cloudwarden.storage.db import session_scope


def _make_policy(session, name: str) -> int:
    return repo.create_policy(
        session,
        name=name,
        resource_type="azure.vm",
        spec={"policies": [{"name": name, "resource": "azure.vm"}]},
    )["id"]


def _seed(
    session,
    *,
    execution_id: str,
    policy_id: int,
    subscription_id: str,
    resources_matched: int = 0,
    status: str = "succeeded",
) -> None:
    """Open then close an execution — newer runs must get lexicographically-greater
    ids (``e1`` < ``e2`` …) so the latest-per-pair ordering is deterministic even
    when same-transaction seeds share a ``started_at``."""
    repo.create_policy_execution(
        session,
        execution_id=execution_id,
        policy_id=policy_id,
        subscription_id=subscription_id,
    )
    repo.finish_policy_execution(
        session, execution_id, status=status, resources_matched=resources_matched
    )


# --------------------------------------------------------------------------- #
# Repo helper — grouping & counts
# --------------------------------------------------------------------------- #
def test_posture_empty_zeroed(db) -> None:
    with session_scope() as s:
        posture = repo.governance_posture(s)
    assert posture["totals"] == {
        "compliant": 0,
        "non_compliant": 0,
        "violations": 0,
        "evaluated": 0,
    }
    assert posture["by_policy"] == []
    assert posture["by_subscription"] == []
    assert posture["by_collection"] == []


def test_posture_counts_by_policy(db) -> None:
    with session_scope() as s:
        p_clean = _make_policy(s, "clean")
        p_bad = _make_policy(s, "bad")
        _seed(s, execution_id="e1", policy_id=p_clean, subscription_id="sub-a", resources_matched=0)
        _seed(s, execution_id="e2", policy_id=p_bad, subscription_id="sub-a", resources_matched=3)

    with session_scope() as s:
        by_policy = {r["policy_name"]: r for r in repo.governance_posture(s)["by_policy"]}

    assert by_policy["clean"]["compliant"] == 1
    assert by_policy["clean"]["non_compliant"] == 0
    assert by_policy["clean"]["violations"] == 0
    assert by_policy["bad"]["compliant"] == 0
    assert by_policy["bad"]["non_compliant"] == 1
    assert by_policy["bad"]["violations"] == 3


def test_posture_grouped_by_subscription(db) -> None:
    with session_scope() as s:
        pid = _make_policy(s, "p1")
        _seed(s, execution_id="e1", policy_id=pid, subscription_id="sub-a", resources_matched=0)
        _seed(s, execution_id="e2", policy_id=pid, subscription_id="sub-b", resources_matched=2)

    with session_scope() as s:
        by_sub = {r["subscription_id"]: r for r in repo.governance_posture(s)["by_subscription"]}

    assert by_sub["sub-a"]["compliant"] == 1
    assert by_sub["sub-a"]["non_compliant"] == 0
    assert by_sub["sub-b"]["non_compliant"] == 1
    assert by_sub["sub-b"]["violations"] == 2


def test_posture_grouped_by_collection(db) -> None:
    with session_scope() as s:
        pid = _make_policy(s, "p1")
        cid = repo.create_collection(s, name="prod")["id"]
        repo.add_policy_to_collection(s, cid, pid)
        _seed(s, execution_id="e1", policy_id=pid, subscription_id="sub-a", resources_matched=5)

    with session_scope() as s:
        by_coll = repo.governance_posture(s)["by_collection"]

    assert len(by_coll) == 1
    assert by_coll[0]["collection_name"] == "prod"
    assert by_coll[0]["non_compliant"] == 1
    assert by_coll[0]["violations"] == 5
    assert by_coll[0]["evaluated"] == 1


def test_posture_after_executions(db) -> None:
    with session_scope() as s:
        p1 = _make_policy(s, "p1")
        p2 = _make_policy(s, "p2")
        _seed(s, execution_id="e1", policy_id=p1, subscription_id="sub-a", resources_matched=0)
        _seed(s, execution_id="e2", policy_id=p2, subscription_id="sub-a", resources_matched=4)
        _seed(s, execution_id="e3", policy_id=p2, subscription_id="sub-b", resources_matched=1)

    with session_scope() as s:
        totals = repo.governance_posture(s)["totals"]

    assert totals["evaluated"] == 3
    assert totals["compliant"] == 1
    assert totals["non_compliant"] == 2
    assert totals["violations"] == 5


def test_posture_uses_latest_execution(db) -> None:
    # A re-run supersedes the older posture for the same (policy, subscription).
    with session_scope() as s:
        pid = _make_policy(s, "p1")
        _seed(s, execution_id="e1", policy_id=pid, subscription_id="sub-a", resources_matched=9)
        _seed(s, execution_id="e2", policy_id=pid, subscription_id="sub-a", resources_matched=0)

    with session_scope() as s:
        posture = repo.governance_posture(s)

    assert posture["totals"] == {
        "compliant": 1,
        "non_compliant": 0,
        "violations": 0,
        "evaluated": 1,
    }


# --------------------------------------------------------------------------- #
# API endpoint
# --------------------------------------------------------------------------- #
def test_posture_api_empty_returns_zeroed(db) -> None:
    resp = TestClient(app).get("/api/governance/posture")
    assert resp.status_code == 200
    body = resp.json()
    assert body["totals"] == {
        "compliant": 0,
        "non_compliant": 0,
        "violations": 0,
        "evaluated": 0,
    }
    assert body["by_policy"] == []
    assert body["by_subscription"] == []
    assert body["by_collection"] == []


def test_posture_api_returns_counts(db) -> None:
    with session_scope() as s:
        pid = _make_policy(s, "p1")
        _seed(s, execution_id="e1", policy_id=pid, subscription_id="sub-a", resources_matched=2)

    body = TestClient(app).get("/api/governance/posture").json()
    assert body["totals"]["non_compliant"] == 1
    assert body["totals"]["violations"] == 2
    assert body["by_policy"][0]["policy_name"] == "p1"
