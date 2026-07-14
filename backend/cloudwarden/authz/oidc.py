"""SSO / OIDC authentication (M11.3).

Identity layer that feeds the RBAC principal (:mod:`cloudwarden.authz.rbac`). When
``OIDC_ENABLED`` is set, an API request carries identity as either:

* an **OIDC bearer token** in ``Authorization: Bearer <jwt>`` — an id/access token the
  identity provider issued, verified here (RS256 signature + ``exp``/``iss``/``aud``);
* a **first-party session** cookie (``finops_session``) — a short-lived HS256 JWT we
  mint after a successful login/callback, so the browser needn't re-present the IdP
  token on every call.

Either way the verified **subject** becomes the RBAC principal. Verification is done
with PyJWT (already a vetted dependency) against a static public key (``oidc_public_key``)
or the issuer's JWKS endpoint. Both the token *verifier* and the OIDC *client* are
injectable, so the whole flow is exercised offline — no identity provider is contacted
in tests. Enforcement is gated by ``OIDC_ENABLED`` (off by default), keeping local/mock
dev and the existing suites unauthenticated.
"""

from __future__ import annotations

import json
import time
import urllib.request
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import urlencode

import jwt
from fastapi import HTTPException, Request

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..config import Settings

# The cookie carrying our first-party session JWT.
SESSION_COOKIE = "finops_session"
# Issuer stamped on (and required of) our own session tokens.
SESSION_ISSUER = "cloudwarden"
# Default session lifetime — 8 hours.
SESSION_TTL_SECONDS = 8 * 3600
# Signing/verification algorithms.
OIDC_ALGORITHMS = ["RS256"]
SESSION_ALGORITHM = "HS256"
BEARER_PREFIX = "Bearer "


class TokenVerifier(Protocol):
    """Verifies an OIDC bearer token and returns its validated claims (or raises)."""

    def verify(self, token: str) -> dict[str, Any]: ...


class OIDCClient(Protocol):
    """Talks to the identity provider: build the authorize URL, exchange a code."""

    def authorization_url(self, *, state: str) -> str: ...

    def exchange_code(self, *, code: str) -> str: ...


# --------------------------------------------------------------------------- #
# Token verifiers (static key / JWKS) — real RS256, offline-testable
# --------------------------------------------------------------------------- #
@dataclass
class StaticKeyVerifier:
    """Verify RS256 tokens against a known PEM public key (no network)."""

    public_key: str
    issuer: str
    audience: str

    def verify(self, token: str) -> dict[str, Any]:
        return jwt.decode(
            token,
            self.public_key,
            algorithms=OIDC_ALGORITHMS,
            audience=self.audience,
            issuer=self.issuer,
        )


@dataclass
class JWKSVerifier:
    """Verify RS256 tokens against keys fetched from the issuer's JWKS endpoint."""

    jwks_uri: str
    issuer: str
    audience: str

    def verify(self, token: str) -> dict[str, Any]:
        signing_key = jwt.PyJWKClient(self.jwks_uri).get_signing_key_from_jwt(token)
        return jwt.decode(
            token,
            signing_key.key,
            algorithms=OIDC_ALGORITHMS,
            audience=self.audience,
            issuer=self.issuer,
        )


def _default_verifier(settings: Settings) -> TokenVerifier:
    """Build the verifier from settings: a static key if provided, else JWKS."""
    if settings.oidc_public_key:
        return StaticKeyVerifier(
            public_key=settings.oidc_public_key,
            issuer=settings.oidc_issuer,
            audience=settings.oidc_audience,
        )
    return JWKSVerifier(
        jwks_uri=settings.oidc_jwks_uri,
        issuer=settings.oidc_issuer,
        audience=settings.oidc_audience,
    )


def verify_token(
    token: str, settings: Settings, *, verifier: TokenVerifier | None = None
) -> dict[str, Any]:
    """Return a token's validated claims, or raise ``401`` on any failure.

    Signature, expiry, issuer and audience are all checked; an expired, tampered, or
    wrong-audience/issuer token is rejected with ``401`` — never a ``500``.
    """
    verifier = verifier or _default_verifier(settings)
    try:
        return verifier.verify(token)
    except Exception as exc:  # noqa: BLE001 - any verification failure is a 401
        raise HTTPException(status_code=401, detail=f"invalid token: {exc}") from exc


def principal_from_claims(claims: dict[str, Any], settings: Settings) -> str | None:
    """Extract the principal from verified claims using the configured claim name."""
    value = claims.get(settings.oidc_principal_claim or "sub")
    return str(value) if value else None


# --------------------------------------------------------------------------- #
# First-party session tokens (HS256)
# --------------------------------------------------------------------------- #
def issue_session(
    subject: str, settings: Settings, *, ttl: int = SESSION_TTL_SECONDS, now: int | None = None
) -> str:
    """Mint a signed session JWT for ``subject`` (HS256, expires after ``ttl`` seconds)."""
    issued = now if now is not None else int(time.time())
    payload = {"sub": subject, "iss": SESSION_ISSUER, "iat": issued, "exp": issued + ttl}
    return jwt.encode(payload, settings.resolved_session_secret, algorithm=SESSION_ALGORITHM)


def verify_session(token: str, settings: Settings) -> dict[str, Any]:
    """Return a session token's claims, or raise ``401`` (expired / tampered / foreign)."""
    try:
        return jwt.decode(
            token,
            settings.resolved_session_secret,
            algorithms=[SESSION_ALGORITHM],
            issuer=SESSION_ISSUER,
        )
    except Exception as exc:  # noqa: BLE001 - any failure is a 401
        raise HTTPException(status_code=401, detail=f"invalid session: {exc}") from exc


# --------------------------------------------------------------------------- #
# Request → principal
# --------------------------------------------------------------------------- #
def _bearer_token(request: Request) -> str | None:
    auth = request.headers.get("authorization")
    if auth and auth.startswith(BEARER_PREFIX):
        return auth[len(BEARER_PREFIX) :].strip() or None
    return None


def principal_from_request(
    request: Request, settings: Settings, *, verifier: TokenVerifier | None = None
) -> str | None:
    """Resolve the caller principal from a session cookie or an OIDC bearer token.

    Prefers the first-party session cookie (cheap HS256 verify); otherwise verifies an
    ``Authorization: Bearer`` OIDC token. Returns ``None`` when no credential is present
    (anonymous); a **present but invalid** credential raises ``401``.
    """
    cookie = request.cookies.get(SESSION_COOKIE)
    if cookie:
        return verify_session(cookie, settings).get("sub")
    token = _bearer_token(request)
    if token:
        claims = verify_token(token, settings, verifier=verifier)
        return principal_from_claims(claims, settings)
    return None


# --------------------------------------------------------------------------- #
# Login / callback flow
# --------------------------------------------------------------------------- #
@dataclass
class HttpOIDCClient:
    """Production OIDC client: build the authorize URL and exchange a code for tokens."""

    authorization_endpoint: str
    token_endpoint: str
    client_id: str
    client_secret: str
    redirect_uri: str
    scopes: str

    def authorization_url(self, *, state: str) -> str:
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "scope": self.scopes,
            "state": state,
        }
        return f"{self.authorization_endpoint}?{urlencode(params)}"

    def exchange_code(self, *, code: str) -> str:  # pragma: no cover - network I/O
        data = urlencode(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.redirect_uri,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            }
        ).encode()
        req = urllib.request.Request(
            self.token_endpoint,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            payload = json.load(resp)
        return payload["id_token"]


def _default_client(settings: Settings) -> HttpOIDCClient:
    return HttpOIDCClient(
        authorization_endpoint=settings.oidc_authorization_endpoint,
        token_endpoint=settings.oidc_token_endpoint,
        client_id=settings.oidc_client_id,
        client_secret=settings.oidc_client_secret,
        redirect_uri=settings.oidc_redirect_uri,
        scopes=settings.oidc_scopes,
    )


def login_url(settings: Settings, *, state: str, client: OIDCClient | None = None) -> str:
    """The identity provider's authorization URL to redirect the browser to."""
    client = client or _default_client(settings)
    return client.authorization_url(state=state)


def handle_callback(
    settings: Settings,
    *,
    code: str,
    client: OIDCClient | None = None,
    verifier: TokenVerifier | None = None,
) -> dict[str, Any]:
    """Exchange an auth ``code`` for an id token, verify it, and issue a session.

    Returns ``{principal, session, claims}``. A code the IdP rejects (or a token that
    fails verification / carries no principal claim) is a ``401``.
    """
    client = client or _default_client(settings)
    try:
        id_token = client.exchange_code(code=code)
    except Exception as exc:  # noqa: BLE001 - a bad/expired code is a 401
        raise HTTPException(status_code=401, detail=f"code exchange failed: {exc}") from exc
    claims = verify_token(id_token, settings, verifier=verifier)
    principal = principal_from_claims(claims, settings)
    if not principal:
        raise HTTPException(status_code=401, detail="token carries no principal claim")
    return {"principal": principal, "session": issue_session(principal, settings), "claims": claims}
