"""SSO / OIDC authentication (M11.3).

Written test-first (TDD). Real RS256 crypto, fully offline: a module-scoped RSA
keypair signs test tokens (via PyJWT) and the verifier checks them against the public
key — no identity provider is ever contacted. The login/callback flow uses an
**injected fake OIDC client**, and the token verifier is either injected or built from
a static public key in settings (`oidc_public_key`), so nothing here touches the
network.

Three layers are exercised:

* **Verification core** — `verify_token` / `principal_from_claims` / session issue &
  verify, in isolation.
* **Login/callback** — `login_url` and `handle_callback` with a fake client.
* **API end-to-end** — a valid OIDC bearer authenticates and its subject flows into
  RBAC; an expired/invalid token is 401; the callback issues a session; auth endpoints
  are 404 when OIDC is disabled (local/mock dev).
"""

from __future__ import annotations

import time

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from azure_finops.authz import oidc
from azure_finops.config import Settings

ISSUER = "https://idp.example.com"
CLIENT_ID = "finops-client"


def _keypair() -> tuple[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    pub = (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    return priv, pub


_PRIV_PEM, _PUB_PEM = _keypair()
_OTHER_PRIV_PEM, _OTHER_PUB_PEM = _keypair()


def _token(
    sub: str = "alice@corp",
    *,
    exp_delta: int = 3600,
    key: str | None = None,
    iss: str = ISSUER,
    aud: str = CLIENT_ID,
    **extra,
) -> str:
    now = int(time.time())
    payload = {"sub": sub, "iss": iss, "aud": aud, "iat": now, "exp": now + exp_delta, **extra}
    return jwt.encode(payload, key or _PRIV_PEM, algorithm="RS256")


def _settings(**over) -> Settings:
    base = dict(
        oidc_enabled=True,
        oidc_issuer=ISSUER,
        oidc_client_id=CLIENT_ID,
        oidc_client_secret="shh",
        oidc_redirect_uri="https://app.example.com/api/auth/callback",
        oidc_public_key=_PUB_PEM,
        session_secret="session-signing-secret-0123456789abcdef",
    )
    base.update(over)
    return Settings(**base)


def _static_verifier() -> oidc.StaticKeyVerifier:
    return oidc.StaticKeyVerifier(public_key=_PUB_PEM, issuer=ISSUER, audience=CLIENT_ID)


def _request(headers: dict | None = None, cookies: dict | None = None) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    if cookies:
        raw.append((b"cookie", "; ".join(f"{k}={v}" for k, v in cookies.items()).encode()))
    return Request({"type": "http", "method": "GET", "path": "/", "headers": raw})


# --------------------------------------------------------------------------- #
# Token verification core (isolated)
# --------------------------------------------------------------------------- #
def test_valid_token_authenticates() -> None:
    claims = oidc.verify_token(_token("alice@corp"), _settings(), verifier=_static_verifier())

    assert claims["sub"] == "alice@corp"
    assert oidc.principal_from_claims(claims, _settings()) == "alice@corp"


def test_expired_token_401() -> None:
    with pytest.raises(HTTPException) as exc:
        oidc.verify_token(_token(exp_delta=-30), _settings(), verifier=_static_verifier())
    assert exc.value.status_code == 401


def test_invalid_signature_401() -> None:
    forged = _token(key=_OTHER_PRIV_PEM)  # signed by a different key

    with pytest.raises(HTTPException) as exc:
        oidc.verify_token(forged, _settings(), verifier=_static_verifier())
    assert exc.value.status_code == 401


def test_wrong_audience_401() -> None:
    with pytest.raises(HTTPException) as exc:
        oidc.verify_token(_token(aud="someone-else"), _settings(), verifier=_static_verifier())
    assert exc.value.status_code == 401


def test_wrong_issuer_401() -> None:
    with pytest.raises(HTTPException) as exc:
        oidc.verify_token(_token(iss="https://evil"), _settings(), verifier=_static_verifier())
    assert exc.value.status_code == 401


def test_principal_from_claims_uses_configured_claim() -> None:
    claims = {"sub": "abc-123", "email": "bob@corp", "preferred_username": "bob"}

    assert oidc.principal_from_claims(claims, _settings()) == "abc-123"  # default: sub
    assert oidc.principal_from_claims(claims, _settings(oidc_principal_claim="email")) == "bob@corp"


def test_principal_from_claims_missing_is_none() -> None:
    assert oidc.principal_from_claims({"email": "x"}, _settings()) is None


# --------------------------------------------------------------------------- #
# Default verifier construction (static key + JWKS), still offline
# --------------------------------------------------------------------------- #
def test_default_verifier_static_key_verifies() -> None:
    # settings carries oidc_public_key → a StaticKeyVerifier, no network.
    claims = oidc.verify_token(_token("carol@corp"), _settings())

    assert claims["sub"] == "carol@corp"


def test_default_verifier_jwks_path(monkeypatch) -> None:
    class _FakeKey:
        key = _PUB_PEM

    class _FakeJWKClient:
        def __init__(self, uri):
            self.uri = uri

        def get_signing_key_from_jwt(self, token):
            return _FakeKey()

    monkeypatch.setattr(oidc.jwt, "PyJWKClient", _FakeJWKClient)
    # No static key → falls back to the JWKS verifier (client monkeypatched offline).
    claims = oidc.verify_token(_token("dave@corp"), _settings(oidc_public_key=""))

    assert claims["sub"] == "dave@corp"


# --------------------------------------------------------------------------- #
# Session issue / verify
# --------------------------------------------------------------------------- #
def test_issue_and_verify_session_roundtrip() -> None:
    token = oidc.issue_session("alice@corp", _settings())

    claims = oidc.verify_session(token, _settings())
    assert claims["sub"] == "alice@corp"


def test_expired_session_401() -> None:
    token = oidc.issue_session("alice@corp", _settings(), ttl=-5)

    with pytest.raises(HTTPException) as exc:
        oidc.verify_session(token, _settings())
    assert exc.value.status_code == 401


def test_tampered_session_401() -> None:
    other = _settings(session_secret="a-different-secret-0123456789abcdef-xyz")
    token = oidc.issue_session("alice@corp", other)

    with pytest.raises(HTTPException) as exc:
        oidc.verify_session(token, _settings())
    assert exc.value.status_code == 401


# --------------------------------------------------------------------------- #
# principal_from_request — bearer / cookie / none
# --------------------------------------------------------------------------- #
def test_principal_from_request_bearer() -> None:
    req = _request(headers={"Authorization": f"Bearer {_token('erin@corp')}"})

    assert oidc.principal_from_request(req, _settings(), verifier=_static_verifier()) == "erin@corp"


def test_principal_from_request_session_cookie() -> None:
    session = oidc.issue_session("frank@corp", _settings())
    req = _request(cookies={oidc.SESSION_COOKIE: session})

    assert oidc.principal_from_request(req, _settings()) == "frank@corp"


def test_principal_from_request_no_token_is_none() -> None:
    assert oidc.principal_from_request(_request(), _settings()) is None


def test_principal_from_request_invalid_bearer_401() -> None:
    req = _request(headers={"Authorization": f"Bearer {_token(exp_delta=-1)}"})

    with pytest.raises(HTTPException) as exc:
        oidc.principal_from_request(req, _settings(), verifier=_static_verifier())
    assert exc.value.status_code == 401


# --------------------------------------------------------------------------- #
# Login / callback (fake IdP client)
# --------------------------------------------------------------------------- #
class _FakeClient:
    """A stand-in OIDC client: hands back a canned id_token for the code ``good``."""

    def __init__(self, token: str) -> None:
        self._token = token

    def authorization_url(self, *, state: str) -> str:
        return f"{ISSUER}/authorize?client_id={CLIENT_ID}&state={state}"

    def exchange_code(self, *, code: str) -> str:
        if code != "good":
            raise ValueError("invalid authorization code")
        return self._token


def test_login_url_contains_client_and_state() -> None:
    url = oidc.login_url(_settings(), state="xyz", client=oidc._default_client(_settings()))

    assert url.startswith(ISSUER)
    assert CLIENT_ID in url and "state=xyz" in url


def test_callback_issues_session() -> None:
    client = _FakeClient(_token("gina@corp"))

    result = oidc.handle_callback(
        _settings(), code="good", client=client, verifier=_static_verifier()
    )

    assert result["principal"] == "gina@corp"
    # The issued session really authenticates.
    assert oidc.verify_session(result["session"], _settings())["sub"] == "gina@corp"


def test_callback_invalid_code_401() -> None:
    client = _FakeClient(_token())

    with pytest.raises(HTTPException) as exc:
        oidc.handle_callback(_settings(), code="bad", client=client, verifier=_static_verifier())
    assert exc.value.status_code == 401


def test_callback_token_without_principal_claim_401() -> None:
    # A valid token, but the configured principal claim (email) is absent → 401.
    client = _FakeClient(_token("has-sub-only"))
    settings = _settings(oidc_principal_claim="email")

    with pytest.raises(HTTPException) as exc:
        oidc.handle_callback(settings, code="good", client=client, verifier=_static_verifier())
    assert exc.value.status_code == 401


# --------------------------------------------------------------------------- #
# API end-to-end
# --------------------------------------------------------------------------- #
def _enable(monkeypatch, **over) -> None:
    from azure_finops.config import get_settings

    env = {
        "RBAC_ENABLED": "1",
        "OIDC_ENABLED": "1",
        "OIDC_ISSUER": ISSUER,
        "OIDC_CLIENT_ID": CLIENT_ID,
        "OIDC_CLIENT_SECRET": "shh",
        "OIDC_REDIRECT_URI": "https://app/api/auth/callback",
        "OIDC_PUBLIC_KEY": _PUB_PEM,
        "SESSION_SECRET": "session-signing-secret-0123456789abcdef",
    }
    env.update(over)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()


VALID_SPEC = {"policies": [{"name": "oidc-p", "resource": "azure.vm"}]}


def test_auth_disabled_allows_local(db) -> None:
    """DoD: with OIDC off (default), local/mock dev needs no token — write still works."""
    from azure_finops.api.main import app

    resp = TestClient(app).post(
        "/api/policies",
        json={"name": "local-p", "resource_type": "azure.vm", "spec": VALID_SPEC},
    )

    assert resp.status_code == 201


def test_api_valid_oidc_token_authenticates_rbac(db, monkeypatch) -> None:
    """A valid bearer's subject flows into RBAC: bound to admin → the write is allowed."""
    from azure_finops.api.main import app
    from azure_finops.authz import rbac
    from azure_finops.storage import repository as repo
    from azure_finops.storage.db import session_scope

    _enable(monkeypatch)
    with session_scope() as s:
        rbac.seed_default_roles(s)
        repo.assign_role(s, principal="alice@corp", role_name="admin")

    resp = TestClient(app).post(
        "/api/policies",
        json={"name": "oidc-admin-p", "resource_type": "azure.vm", "spec": VALID_SPEC},
        headers={"Authorization": f"Bearer {_token('alice@corp')}"},
    )

    assert resp.status_code == 201


def test_api_expired_token_401(db, monkeypatch) -> None:
    from azure_finops.api.main import app

    _enable(monkeypatch)

    resp = TestClient(app).post(
        "/api/policies",
        json={"name": "oidc-exp-p", "resource_type": "azure.vm", "spec": VALID_SPEC},
        headers={"Authorization": f"Bearer {_token(exp_delta=-10)}"},
    )

    assert resp.status_code == 401


def test_api_invalid_token_on_read_401(db, monkeypatch) -> None:
    from azure_finops.api.main import app

    _enable(monkeypatch)

    resp = TestClient(app).get(
        "/api/policies", headers={"Authorization": f"Bearer {_token(key=_OTHER_PRIV_PEM)}"}
    )

    assert resp.status_code == 401


def test_api_login_returns_authorization_url(db, monkeypatch) -> None:
    from azure_finops.api.main import app, get_oidc_client

    _enable(monkeypatch)
    app.dependency_overrides[get_oidc_client] = lambda: _FakeClient(_token())
    try:
        resp = TestClient(app).get("/api/auth/login", follow_redirects=False)
    finally:
        app.dependency_overrides.pop(get_oidc_client, None)

    assert resp.status_code == 200
    assert ISSUER in resp.json()["authorization_url"]


def test_api_callback_sets_session(db, monkeypatch) -> None:
    from azure_finops.api.main import app, get_oidc_client, get_token_verifier

    _enable(monkeypatch)
    app.dependency_overrides[get_oidc_client] = lambda: _FakeClient(_token("hank@corp"))
    app.dependency_overrides[get_token_verifier] = _static_verifier
    try:
        client = TestClient(app)
        cb = client.get("/api/auth/callback", params={"code": "good", "state": "s"})
        assert cb.status_code == 200
        assert cb.json()["principal"] == "hank@corp"
        assert oidc.SESSION_COOKIE in cb.cookies
        # The session cookie authenticates a follow-up call.
        me = client.get("/api/authz/me")
        assert me.json()["principal"] == "hank@corp"
    finally:
        app.dependency_overrides.pop(get_oidc_client, None)
        app.dependency_overrides.pop(get_token_verifier, None)


def test_api_callback_bad_code_401(db, monkeypatch) -> None:
    from azure_finops.api.main import app, get_oidc_client, get_token_verifier

    _enable(monkeypatch)
    app.dependency_overrides[get_oidc_client] = lambda: _FakeClient(_token())
    app.dependency_overrides[get_token_verifier] = _static_verifier
    try:
        resp = TestClient(app).get("/api/auth/callback", params={"code": "nope", "state": "s"})
    finally:
        app.dependency_overrides.pop(get_oidc_client, None)
        app.dependency_overrides.pop(get_token_verifier, None)

    assert resp.status_code == 401


def test_api_auth_endpoints_404_when_disabled(db) -> None:
    """DoD: auth is disabled via config for local mock dev — the routes are inert."""
    from azure_finops.api.main import app

    client = TestClient(app)
    assert client.get("/api/auth/login", follow_redirects=False).status_code == 404
    assert client.get("/api/auth/callback", params={"code": "good"}).status_code == 404


def test_api_logout_clears_session(db, monkeypatch) -> None:
    from azure_finops.api.main import app
    from azure_finops.config import get_settings

    _enable(monkeypatch)
    # Build the session via the app's own settings to guarantee the same secret.
    session = oidc.issue_session("ivy@corp", get_settings())
    client = TestClient(app)
    client.cookies.set(oidc.SESSION_COOKIE, session)

    before = client.get("/api/authz/me").json()
    out = client.post("/api/auth/logout")
    assert before["principal"] == "ivy@corp"
    assert out.status_code == 200
