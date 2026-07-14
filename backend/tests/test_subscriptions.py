"""Multi-subscription management: repository CRUD, per-sub credentials, collector
retargeting, orchestrator fan-out, and the API endpoints.

DB-backed tests use the `db` fixture (throwaway Postgres); the rest run offline.
"""

from __future__ import annotations

from types import SimpleNamespace

from cloudwarden.azure.context import SubscriptionContext, resolve_subscription_id

SUB_A = "11111111-1111-1111-1111-111111111111"
SUB_B = "22222222-2222-2222-2222-222222222222"
PLACEHOLDER = "00000000-0000-0000-0000-000000000000"


# --------------------------------------------------------------------------- #
# auth.credential_for
# --------------------------------------------------------------------------- #
def test_credential_for_falls_back_to_shared_sp(monkeypatch) -> None:
    import cloudwarden.auth as auth

    monkeypatch.setattr(auth, "read_credential", lambda: "SHARED")
    assert auth.credential_for(None, None, None) == "SHARED"
    assert auth.credential_for("t", None, "s") == "SHARED"  # client_id missing → shared


def test_credential_for_builds_dedicated_sp(monkeypatch) -> None:
    import cloudwarden.auth as auth

    monkeypatch.setattr(auth, "_make_credential", lambda t, c, s: ("MADE", t, c, s))
    assert auth.credential_for("tid", "cid", "sec") == ("MADE", "tid", "cid", "sec")


def test_credential_for_defaults_tenant_to_env(monkeypatch) -> None:
    import cloudwarden.auth as auth
    from cloudwarden.config import get_settings

    monkeypatch.setenv("AZURE_TENANT_ID", "env-tenant")
    get_settings.cache_clear()
    monkeypatch.setattr(auth, "_make_credential", lambda t, c, s: t)
    assert auth.credential_for(None, "cid", "sec") == "env-tenant"


# --------------------------------------------------------------------------- #
# SubscriptionContext helper
# --------------------------------------------------------------------------- #
def test_resolve_subscription_id() -> None:
    assert resolve_subscription_id(None, "default") == "default"
    assert resolve_subscription_id(SubscriptionContext("abc"), "default") == "abc"


# --------------------------------------------------------------------------- #
# Collector retargeting (mock mode)
# --------------------------------------------------------------------------- #
def test_inventory_retargets_to_subscription() -> None:
    from cloudwarden.azure.inventory import collect_inventory

    recs = collect_inventory(subscription=SubscriptionContext(SUB_A))
    assert recs and all(SUB_A in r.resource_id for r in recs)
    assert all(r.subscription_id == SUB_A for r in recs)


def test_cost_retargets_to_subscription() -> None:
    from cloudwarden.azure.cost import collect_cost

    rows = collect_cost(subscription=SubscriptionContext(SUB_A))
    assert rows and all(r.subscription_id == SUB_A for r in rows)
    assert all(r.resource_id is None or SUB_A in r.resource_id for r in rows)


def test_metrics_retargets_to_subscription() -> None:
    from cloudwarden.azure.inventory import collect_inventory
    from cloudwarden.azure.metrics import collect_metrics

    ctx = SubscriptionContext(SUB_A)
    resources = collect_inventory(subscription=ctx)
    samples = collect_metrics(resources, subscription=ctx)
    assert samples and all(SUB_A in s.resource_id for s in samples)


def test_advisor_retargets_to_subscription() -> None:
    from cloudwarden.azure.advisor import collect_advisor

    recs = collect_advisor(subscription=SubscriptionContext(SUB_A))
    assert any(r.get("resource_id") and SUB_A in r["resource_id"] for r in recs)


def test_collectors_default_to_env_subscription() -> None:
    from cloudwarden.azure.inventory import collect_inventory

    recs = collect_inventory()  # no subscription → env placeholder, no rewrite
    assert recs and all(PLACEHOLDER in r.resource_id for r in recs)


# --------------------------------------------------------------------------- #
# Repository CRUD
# --------------------------------------------------------------------------- #
def test_ensure_default_subscription_seeds_once(db) -> None:
    from cloudwarden.config import get_settings
    from cloudwarden.storage import repository as repo
    from cloudwarden.storage.db import session_scope

    with session_scope() as s:
        repo.ensure_default_subscription(s, get_settings())
        repo.ensure_default_subscription(s, get_settings())  # idempotent
    with session_scope() as s:
        subs = repo.list_subscriptions(s)
    assert len(subs) == 1
    assert subs[0]["is_default"] is True
    assert subs[0]["subscription_id"] == PLACEHOLDER
    assert "client_secret" not in subs[0]  # secret never leaves the DB


def test_upsert_create_update_and_secret_semantics(db) -> None:
    from cloudwarden.storage import repository as repo
    from cloudwarden.storage.db import session_scope

    with session_scope() as s:
        first = repo.upsert_subscription(
            s, subscription_id=SUB_A, display_name="A", client_id="cid", client_secret="secret1"
        )
        assert first["is_default"] is True and first["has_credentials"] is True
        second = repo.upsert_subscription(s, subscription_id=SUB_B, display_name="B")
        assert second["is_default"] is False and second["has_credentials"] is False

    # update: secret=None keeps the existing secret
    with session_scope() as s:
        repo.upsert_subscription(s, subscription_id=SUB_A, display_name="A2", client_id="cid")
        rec = repo.get_subscription(s, SUB_A)
        assert rec.display_name == "A2" and rec.client_secret == "secret1"

    # update: secret="" clears
    with session_scope() as s:
        repo.upsert_subscription(
            s, subscription_id=SUB_A, display_name="A2", client_id="cid", client_secret=""
        )
        assert repo.get_subscription(s, SUB_A).client_secret is None

    # update: secret="new" sets
    with session_scope() as s:
        repo.upsert_subscription(
            s, subscription_id=SUB_A, display_name="A2", client_id="cid", client_secret="new"
        )
        assert repo.get_subscription(s, SUB_A).client_secret == "new"


def test_set_default_and_enabled_filter(db) -> None:
    from cloudwarden.storage import repository as repo
    from cloudwarden.storage.db import session_scope

    with session_scope() as s:
        repo.upsert_subscription(s, subscription_id=SUB_A, display_name="A")
        repo.upsert_subscription(s, subscription_id=SUB_B, display_name="B", enabled=False)
        assert repo.set_default_subscription(s, SUB_B) is True
        assert repo.set_default_subscription(s, "nope") is False
    with session_scope() as s:
        by_id = {x["subscription_id"]: x for x in repo.list_subscriptions(s)}
        assert by_id[SUB_B]["is_default"] is True and by_id[SUB_A]["is_default"] is False
        enabled = [r.subscription_id for r in repo.enabled_subscriptions(s)]
        assert enabled == [SUB_A]  # B is disabled


def test_delete_reassigns_default(db) -> None:
    from cloudwarden.storage import repository as repo
    from cloudwarden.storage.db import session_scope

    with session_scope() as s:
        repo.upsert_subscription(s, subscription_id=SUB_A, display_name="A")  # default
        repo.upsert_subscription(s, subscription_id=SUB_B, display_name="B")
        assert repo.delete_subscription(s, SUB_A) is True
        assert repo.delete_subscription(s, "nope") is False
    with session_scope() as s:
        subs = repo.list_subscriptions(s)
        assert len(subs) == 1 and subs[0]["subscription_id"] == SUB_B
        assert subs[0]["is_default"] is True  # default reassigned to the survivor


# --------------------------------------------------------------------------- #
# Orchestrator fan-out
# --------------------------------------------------------------------------- #
def test_context_from_record_live_credential(monkeypatch) -> None:
    import cloudwarden.auth as auth
    import cloudwarden.orchestrator as orch

    monkeypatch.setattr(auth, "credential_for", lambda t, c, s: ("CRED", t, c, s))
    rec = SimpleNamespace(
        subscription_id=SUB_A,
        display_name="A",
        tenant_id="t",
        client_id="c",
        client_secret="s",
    )
    ctx = orch._context_from_record(rec, mock=False)
    assert ctx.credential == ("CRED", "t", "c", "s")
    # mock mode never builds a credential
    assert orch._context_from_record(rec, mock=True).credential is None


def test_run_one_subscription_unknown(db) -> None:
    from cloudwarden.orchestrator import run_one_subscription

    assert run_one_subscription("does-not-exist", mock=True) is None


def test_run_all_subscriptions_fans_out(db) -> None:
    from cloudwarden.orchestrator import run_all_subscriptions
    from cloudwarden.storage import repository as repo
    from cloudwarden.storage.db import session_scope

    # Two explicit enabled subscriptions (the empty-table seed is covered elsewhere).
    with session_scope() as s:
        repo.upsert_subscription(s, subscription_id=SUB_A, display_name="A")
        repo.upsert_subscription(s, subscription_id=SUB_B, display_name="B")

    result = run_all_subscriptions(mock=True)
    assert result["subscriptions"] == 2
    sub_ids = {r["subscription_id"] for r in result["runs"]}
    assert {SUB_A, SUB_B} == sub_ids

    with session_scope() as s:
        rows = repo._rows(s, "SELECT DISTINCT subscription_id FROM resources")
    seen = {r["subscription_id"] for r in rows}
    # each subscription produced its own (retargeted, non-colliding) resource ids
    assert SUB_A in seen and SUB_B in seen


def test_run_all_subscriptions_isolates_failure(db, monkeypatch) -> None:
    import cloudwarden.orchestrator as orch

    def boom(*a, **k):
        raise RuntimeError("collector down")

    monkeypatch.setattr(orch, "run_pipeline", boom)
    result = orch.run_all_subscriptions(mock=True)
    assert result["subscriptions"] >= 1
    assert all("error" in r for r in result["runs"])


# --------------------------------------------------------------------------- #
# API endpoints
# --------------------------------------------------------------------------- #
def test_subscription_api(db) -> None:
    from fastapi.testclient import TestClient

    from cloudwarden.api.main import app

    with TestClient(app) as c:  # context manager runs lifespan → seeds default
        subs = c.get("/api/subscriptions").json()
        assert len(subs) == 1 and subs[0]["is_default"] is True
        assert "client_secret" not in subs[0]

        created = c.post(
            "/api/subscriptions",
            json={
                "subscription_id": SUB_B,
                "display_name": "Prod",
                "client_id": "cid",
                "client_secret": "sec",
            },
        ).json()
        assert created["has_credentials"] is True and created["is_default"] is False

        bad = c.post("/api/subscriptions", json={"subscription_id": "", "display_name": ""})
        assert bad.status_code == 400

        assert c.post(f"/api/subscriptions/{SUB_B}/default").json()["is_default"] is True
        assert c.post("/api/subscriptions/nope/default").status_code == 404
        assert len(c.get("/api/subscriptions").json()) == 2

        one = c.post("/api/runs", params={"mock": True, "subscription_id": SUB_B}).json()
        assert one["subscription_id"] == SUB_B and "run_id" in one
        assert c.post("/api/runs", params={"subscription_id": "nope"}).status_code == 404

        assert c.delete(f"/api/subscriptions/{SUB_B}").json()["deleted"] is True
        assert c.delete("/api/subscriptions/nope").status_code == 404


def test_run_pipeline_reports_subscription_id(db) -> None:
    from cloudwarden.orchestrator import run_pipeline

    out = run_pipeline(mock=True, subscription=SubscriptionContext(SUB_A, display_name="A"))
    assert out["subscription_id"] == SUB_A and "run_id" in out


# --------------------------------------------------------------------------- #
# Connectivity check (test-connection)
# --------------------------------------------------------------------------- #
class _Resp:
    def __init__(self, status: int, payload: dict | None = None) -> None:
        self.status_code = status
        self._payload = payload or {}

    def json(self) -> dict:
        return self._payload


class _Http:
    def __init__(self, resp: _Resp | None = None, raise_exc: Exception | None = None) -> None:
        self._resp = resp
        self._raise = raise_exc

    def get(self, url: str, headers: dict | None = None) -> _Resp:
        if self._raise:
            raise self._raise
        return self._resp


class _Cred:
    def __init__(self, raise_exc: Exception | None = None) -> None:
        self._raise = raise_exc

    def get_token(self, scope: str) -> SimpleNamespace:
        if self._raise:
            raise self._raise
        return SimpleNamespace(token="tok")


def _go_live(monkeypatch) -> None:
    from cloudwarden.config import get_settings

    monkeypatch.setenv("FINOPS_MOCK", "0")
    get_settings.cache_clear()


def test_check_connection_mock() -> None:
    from cloudwarden.azure.connectivity import check_connection

    r = check_connection(SUB_A)
    assert r["ok"] is True and r["mock"] is True


def test_check_connection_success(monkeypatch) -> None:
    from cloudwarden.azure.connectivity import check_connection

    _go_live(monkeypatch)
    http = _Http(_Resp(200, {"displayName": "Prod", "state": "Enabled"}))
    r = check_connection(SUB_A, credential=_Cred(), http=http)
    assert r["ok"] is True and r["subscription_name"] == "Prod" and r["state"] == "Enabled"


def test_check_connection_default_http_client(monkeypatch) -> None:
    import cloudwarden.azure.connectivity as conn

    _go_live(monkeypatch)

    class _CM:
        def __enter__(self) -> _Http:
            return _Http(_Resp(200, {"displayName": "X"}))

        def __exit__(self, *a) -> bool:
            return False

    monkeypatch.setattr(conn.httpx, "Client", lambda **k: _CM())
    r = conn.check_connection(SUB_A, credential=_Cred())
    assert r["ok"] is True and r["subscription_name"] == "X"


def test_check_connection_access_denied(monkeypatch) -> None:
    from cloudwarden.azure.connectivity import check_connection

    _go_live(monkeypatch)
    r = check_connection(SUB_A, credential=_Cred(), http=_Http(_Resp(403)))
    assert r["ok"] is False and "denied" in r["message"].lower()


def test_check_connection_not_found(monkeypatch) -> None:
    from cloudwarden.azure.connectivity import check_connection

    _go_live(monkeypatch)
    r = check_connection(SUB_A, credential=_Cred(), http=_Http(_Resp(404)))
    assert r["ok"] is False and "404" in r["message"]


def test_check_connection_other_status(monkeypatch) -> None:
    from cloudwarden.azure.connectivity import check_connection

    _go_live(monkeypatch)
    r = check_connection(SUB_A, credential=_Cred(), http=_Http(_Resp(500)))
    assert r["ok"] is False and "500" in r["message"]


def test_check_connection_token_failure(monkeypatch) -> None:
    from cloudwarden.azure.connectivity import check_connection

    _go_live(monkeypatch)
    r = check_connection(SUB_A, credential=_Cred(raise_exc=RuntimeError("no token")))
    assert r["ok"] is False and "Token acquisition failed" in r["message"]


def test_check_connection_request_failure(monkeypatch) -> None:
    from cloudwarden.azure.connectivity import check_connection

    _go_live(monkeypatch)
    r = check_connection(SUB_A, credential=_Cred(), http=_Http(raise_exc=RuntimeError("boom")))
    assert r["ok"] is False and "Request failed" in r["message"]


def test_test_subscription_endpoint_mock(db) -> None:
    from fastapi.testclient import TestClient

    from cloudwarden.api.main import app
    from cloudwarden.storage import repository as repo
    from cloudwarden.storage.db import session_scope

    with session_scope() as s:
        repo.upsert_subscription(s, subscription_id=SUB_A, display_name="A")
    c = TestClient(app)
    r = c.post(f"/api/subscriptions/{SUB_A}/test").json()
    assert r["ok"] is True and r["mock"] is True
    assert c.post("/api/subscriptions/nope/test").status_code == 404


def test_test_subscription_endpoint_live_credential(db, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    import cloudwarden.api.main as apimain
    from cloudwarden.config import get_settings
    from cloudwarden.storage import repository as repo
    from cloudwarden.storage.db import session_scope

    with session_scope() as s:
        repo.upsert_subscription(
            s, subscription_id=SUB_A, display_name="A", client_id="c", client_secret="x"
        )

    captured: dict = {}

    def fake_check(sid, credential=None, http=None):
        captured["sid"] = sid
        captured["cred"] = credential
        return {"ok": True, "message": "stub"}

    monkeypatch.setattr("cloudwarden.auth.credential_for", lambda t, c, sec: "DEDICATED")
    monkeypatch.setattr("cloudwarden.azure.connectivity.check_connection", fake_check)
    monkeypatch.setenv("FINOPS_MOCK", "0")
    get_settings.cache_clear()
    c = TestClient(apimain.app)
    r = c.post(f"/api/subscriptions/{SUB_A}/test").json()
    assert r["ok"] is True
    assert captured["cred"] == "DEDICATED" and captured["sid"] == SUB_A
    get_settings.cache_clear()
