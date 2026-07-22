"""FastAPI application exposing cost, recommendations, runs and health.

Grafana reads the SQL views directly from Postgres; this API serves the Next.js
UI and on-demand pipeline triggers. It is intentionally thin — all queries live
in the repository.
"""

from __future__ import annotations

import logging
import secrets
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError

from .. import reporting
from ..authz import audit, oidc, rbac, teams
from ..config import get_settings
from ..custodian import engine as custodian
from ..custodian.engine import CustodianRunner
from ..custodian.eventmode import handle_event
from ..events.assetdb import apply_asset_event
from ..events.ingestion import (
    handle_subscription_validation,
    normalize_event,
    verify_event_grid_key,
)
from ..models import (
    AccountGroupCreate,
    AssetQuery,
    BindingIn,
    BindingNotificationIn,
    BindingUpdate,
    CollectionCreate,
    NotificationChannelIn,
    NotificationChannelUpdate,
    NotificationTemplateIn,
    NotificationTemplateUpdate,
    PolicyCreate,
    PolicyUpdate,
    ValidateRequest,
    ValidateResult,
)
from ..notify.dispatch import KNOWN_TRANSPORTS
from ..packs import registry as packs
from ..providers import aws as aws_provider
from ..providers import gcp as gcp_provider
from ..providers import registry as providers_registry
from ..remediation import approval as remediation
from ..resilience import REGISTRY
from ..storage import repository as repo
from ..storage.db import init_db, session_scope

logger = logging.getLogger("cloudwarden.api")


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        init_db()
        from ..config import get_settings

        with session_scope() as session:
            settings = get_settings()
            repo.ensure_default_subscription(session, settings)
            rbac.seed_default_roles(session, bootstrap_admin=settings.rbac_bootstrap_admin or None)
    except Exception:  # noqa: BLE001 - endpoints will surface DB errors individually
        logger.exception("init_db failed at startup")
    yield


app = FastAPI(title="CloudWarden", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "sources": REGISTRY.snapshot()}


_COST_PROVIDERS = {"azure", "aws", "gcp"}


def _cost_scope(days: int, provider: str) -> tuple[int, str | None]:
    """Clamp ``days`` to 1..365 and normalize/validate ``provider`` for cost
    scoping (#116). Empty/"all" → None (all clouds); an unknown provider → 400."""
    normalized = (provider or "all").strip().lower()
    if normalized in ("", "all"):
        prov: str | None = None
    elif normalized in _COST_PROVIDERS:
        prov = normalized
    else:
        raise HTTPException(status_code=400, detail=f"invalid provider: {provider}")
    return max(1, min(days, 365)), prov


@app.get("/api/costs/summary")
def costs_summary(days: int = 30, provider: str = "all") -> dict[str, Any]:
    days, prov = _cost_scope(days, provider)
    with session_scope() as session:
        return {
            "total": repo.total_cost(session, days=days, provider=prov),
            "by_type": repo.cost_by_type(session, days=days, provider=prov),
            "by_region": repo.cost_by_region(session, days=days, provider=prov),
        }


@app.get("/api/costs/by-type")
def costs_by_type(days: int = 30, provider: str = "all") -> list[dict[str, Any]]:
    days, prov = _cost_scope(days, provider)
    with session_scope() as session:
        return repo.cost_by_type(session, days=days, provider=prov)


@app.get("/api/costs/by-region")
def costs_by_region(days: int = 30, provider: str = "all") -> list[dict[str, Any]]:
    days, prov = _cost_scope(days, provider)
    with session_scope() as session:
        return repo.cost_by_region(session, days=days, provider=prov)


@app.get("/api/costs/by-resource")
def costs_by_resource(limit: int = 50) -> list[dict[str, Any]]:
    with session_scope() as session:
        return repo.cost_by_resource(session, limit=limit)


@app.get("/api/costs/trend")
def costs_trend(days: int = 30) -> dict[str, Any]:
    days = max(1, min(days, 365))
    with session_scope() as session:
        return repo.cost_trend(session, days=days)


@app.get("/api/costs/monthly")
def costs_monthly(months: int = 6, provider: str = "all") -> dict[str, Any]:
    """Amortized spend bucketed by calendar month (for the Overview monthly chart)."""
    months = max(1, min(months, 24))
    prov = None if provider in ("", "all") else provider
    with session_scope() as session:
        return repo.cost_monthly(session, months=months, provider=prov)


@app.get("/api/recommendations")
def recommendations() -> list[dict[str, Any]]:
    with session_scope() as session:
        return repo.latest_recommendations(session)


class Decision(BaseModel):
    decision: str  # approve | reject
    actor: str | None = None


@app.post(
    "/api/recommendations/{rec_id}/decision",
    dependencies=[Depends(rbac.require_permission("recommendation:decide"))],
)
def decide_recommendation(rec_id: int, body: Decision) -> dict[str, Any]:
    status = {"approve": "approved", "reject": "rejected"}.get(body.decision)
    if status is None:
        raise HTTPException(status_code=400, detail="decision must be 'approve' or 'reject'")
    with session_scope() as session:
        ok = repo.decide_recommendation(session, rec_id, status, body.actor)
    if not ok:
        raise HTTPException(status_code=404, detail="recommendation not found")
    return {"id": rec_id, "status": status}


# --------------------------------------------------------------------------- #
# Governance-as-code: policy validation + Custodian schema (M1.3)
# --------------------------------------------------------------------------- #
def get_custodian_runner() -> CustodianRunner | None:
    """Injection seam for the Custodian engine.

    Returns ``None`` so the engine falls back to its cached ``LiveCustodianRunner``
    (real, offline c7n for validate/schema). Tests override this dependency with a
    ``FakeCustodianRunner`` to keep the API suite fully offline.
    """
    return None


def get_token_verifier() -> oidc.TokenVerifier | None:
    """Injection seam for the OIDC token verifier (M11.3).

    Returns ``None`` so verification falls back to the verifier built from settings
    (a static public key, or the issuer's JWKS endpoint). Tests override this with a
    static-key verifier to stay fully offline.
    """
    return None


def get_oidc_client() -> oidc.OIDCClient | None:
    """Injection seam for the OIDC client (login/callback, M11.3).

    Returns ``None`` so the flow falls back to the HTTP client built from settings.
    Tests override this with a fake client so no identity provider is contacted.
    """
    return None


def get_aws_sts_client() -> aws_provider.StsClient | None:
    """Injection seam for the AWS STS client used to validate onboarded accounts (M12.2).

    Returns ``None`` so the provider builds a live boto3 STS client from the
    request's credentials. Tests override this with a fake STS client so onboarding
    never reaches AWS.
    """
    return None


def get_gcp_client() -> gcp_provider.ResourceManagerClient | None:
    """Injection seam for the GCP Resource Manager client used to validate projects (M12.3).

    Returns ``None`` so the provider builds a live client from the request's
    service-account credentials. Tests override this with a fake client so
    onboarding never reaches GCP.
    """
    return None


@app.post("/api/policies/validate")
def validate_policy(
    body: ValidateRequest,
    runner: Annotated[CustodianRunner | None, Depends(get_custodian_runner)] = None,
) -> ValidateResult:
    """Dry-run validate a Cloud Custodian policy spec — never persists, never 500s.

    A malformed body (no ``policies`` list) is bad input → ``400``. A well-formed
    spec is validated by the engine and reported as ``{valid, errors}`` with
    ``200`` even when invalid (validation ran and produced errors).
    """
    spec = body.spec
    if not isinstance(spec, dict) or not spec.get("policies"):
        raise HTTPException(status_code=400, detail="malformed policy: expected a 'policies' list")
    try:
        result = custodian.validate_policy(spec, runner=runner)
    except Exception as exc:  # noqa: BLE001 - degrade to 400, never surface a 500
        raise HTTPException(status_code=400, detail=f"validation failed: {exc}") from exc
    return ValidateResult(valid=bool(result.get("valid")), errors=list(result.get("errors") or []))


@app.get("/api/custodian/schema")
def custodian_schema(
    resource_type: str | None = None,
    runner: Annotated[CustodianRunner | None, Depends(get_custodian_runner)] = None,
) -> dict[str, Any]:
    """List Azure resource types (no arg) or return one type's filters/actions/schema.

    An unknown ``resource_type`` is bad input → ``400``; any engine error degrades
    to ``400`` rather than a ``500``.
    """
    try:
        result = custodian.get_schema(resource_type, runner=runner)
    except Exception as exc:  # noqa: BLE001 - degrade to 400, never surface a 500
        raise HTTPException(status_code=400, detail=f"schema lookup failed: {exc}") from exc
    if isinstance(result, dict) and result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])
    return result


# --------------------------------------------------------------------------- #
# Policy CRUD (M2.1) — validate-on-write governance-as-code
# --------------------------------------------------------------------------- #
def _policy_view(policy: dict[str, Any]) -> dict[str, Any]:
    """Every persisted policy passed validate-on-write, so surface its status."""
    return {**policy, "validation_status": "valid"}


def _require_valid_spec(spec: dict[str, Any], runner: CustodianRunner | None) -> None:
    """Reject a spec that fails Custodian validation with ``422`` (no persistence)."""
    result = custodian.validate_policy(spec, runner=runner)
    if not result.get("valid"):
        raise HTTPException(
            status_code=422,
            detail={"message": "policy validation failed", "errors": result.get("errors") or []},
        )


@app.get("/api/policies")
def list_policies(request: Request, enabled: bool | None = None) -> list[dict[str, Any]]:
    """List policies, optionally filtered by ``?enabled=true``/``false``.

    Team-scoped (M11.2) when RBAC is enabled: a member sees only their team's policies,
    an admin sees all. With RBAC off the listing is unscoped (backward-compatible).
    """
    rbac_enabled = get_settings().rbac_enabled
    principal = rbac.principal_from_request(request)
    with session_scope() as session:
        team_ids = teams.visible_team_ids(session, principal, rbac_enabled=rbac_enabled)
        policies = repo.list_policies(session, enabled_only=enabled is True, team_ids=team_ids)
    if enabled is False:
        policies = [p for p in policies if not p["enabled"]]
    return [_policy_view(p) for p in policies]


@app.get("/api/policies/{policy_id}")
def get_policy(policy_id: int, request: Request) -> dict[str, Any]:
    """Fetch one policy. ``403`` for a non-admin reaching across teams (M11.2)."""
    rbac_enabled = get_settings().rbac_enabled
    principal = rbac.principal_from_request(request)
    with session_scope() as session:
        policy = repo.get_policy(session, policy_id)
        if policy is None:
            raise HTTPException(status_code=404, detail="policy not found")
        teams.ensure_policy_access(session, principal, policy, rbac_enabled=rbac_enabled)
    return _policy_view(policy)


@app.post(
    "/api/policies",
    status_code=201,
    dependencies=[Depends(rbac.require_permission("policy:write"))],
)
def create_policy(
    body: PolicyCreate,
    request: Request,
    runner: Annotated[CustodianRunner | None, Depends(get_custodian_runner)] = None,
) -> dict[str, Any]:
    """Validate the spec, then persist. ``422`` if invalid (no row), ``409`` on dup name.

    Assigns the owning team (M11.2) when RBAC is enabled: the caller's ``body.team`` if
    given (must be admin/member — ``403``/``404``), else derived from their membership.
    """
    _require_valid_spec(body.spec, runner)
    rbac_enabled = get_settings().rbac_enabled
    principal = rbac.principal_from_request(request)
    try:
        with session_scope() as session:
            team_id = teams.resolve_owning_team(
                session, principal, requested_team=body.team, rbac_enabled=rbac_enabled
            )
            created = repo.create_policy(
                session,
                name=body.name,
                resource_type=body.resource_type,
                spec=body.spec,
                description=body.description,
                source=body.source,
                team_id=team_id,
            )
            audit.record(
                session,
                actor=principal,
                action="policy.create",
                target_type="policy",
                target_id=str(created["id"]),
                after=created,
            )
    except IntegrityError as exc:
        raise HTTPException(status_code=409, detail="policy name already exists") from exc
    return _policy_view(created)


@app.put(
    "/api/policies/{policy_id}", dependencies=[Depends(rbac.require_permission("policy:write"))]
)
def update_policy(
    policy_id: int,
    body: PolicyUpdate,
    request: Request,
    runner: Annotated[CustodianRunner | None, Depends(get_custodian_runner)] = None,
) -> dict[str, Any]:
    """Apply a partial update (re-validating ``spec`` when supplied). ``404``/``409``.

    A non-admin may only update policies owned by their team (``403`` cross-team, M11.2).
    """
    if body.spec is not None:
        _require_valid_spec(body.spec, runner)
    rbac_enabled = get_settings().rbac_enabled
    principal = rbac.principal_from_request(request)
    try:
        with session_scope() as session:
            existing = repo.get_policy(session, policy_id)
            if existing is None:
                raise HTTPException(status_code=404, detail="policy not found")
            teams.ensure_policy_access(session, principal, existing, rbac_enabled=rbac_enabled)
            updated = repo.update_policy(
                session,
                policy_id,
                name=body.name,
                resource_type=body.resource_type,
                spec=body.spec,
                description=body.description,
            )
            audit.record(
                session,
                actor=principal,
                action="policy.update",
                target_type="policy",
                target_id=str(policy_id),
                before=existing,
                after=updated,
            )
    except IntegrityError as exc:
        raise HTTPException(status_code=409, detail="policy name already exists") from exc
    return _policy_view(updated)


@app.delete(
    "/api/policies/{policy_id}", dependencies=[Depends(rbac.require_permission("policy:write"))]
)
def delete_policy(policy_id: int, request: Request) -> dict[str, Any]:
    """Delete a policy. A non-admin may only delete their team's policies (``403``, M11.2)."""
    rbac_enabled = get_settings().rbac_enabled
    principal = rbac.principal_from_request(request)
    with session_scope() as session:
        existing = repo.get_policy(session, policy_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="policy not found")
        teams.ensure_policy_access(session, principal, existing, rbac_enabled=rbac_enabled)
        repo.delete_policy(session, policy_id)
        audit.record(
            session,
            actor=principal,
            action="policy.delete",
            target_type="policy",
            target_id=str(policy_id),
            before=existing,
        )
    return {"id": policy_id, "deleted": True}


@app.post(
    "/api/policies/{policy_id}/enabled",
    dependencies=[Depends(rbac.require_permission("policy:write"))],
)
def set_policy_enabled(policy_id: int, request: Request, enabled: bool = True) -> dict[str, Any]:
    principal = rbac.principal_from_request(request)
    with session_scope() as session:
        updated = repo.set_policy_enabled(session, policy_id, enabled)
        if updated is None:
            raise HTTPException(status_code=404, detail="policy not found")
        audit.record(
            session,
            actor=principal,
            action="policy.enable" if enabled else "policy.disable",
            target_type="policy",
            target_id=str(policy_id),
            after=updated,
        )
    return _policy_view(updated)


@app.get("/api/policies/{policy_id}/versions")
def list_policy_versions(policy_id: int) -> list[dict[str, Any]]:
    """List a policy's version history newest-first. ``404`` when the policy is missing."""
    with session_scope() as session:
        versions = repo.list_versions(session, policy_id)
    if versions is None:
        raise HTTPException(status_code=404, detail="policy not found")
    return versions


@app.get("/api/policies/{policy_id}/versions/diff")
def diff_policy_versions(
    policy_id: int,
    from_version: Annotated[int, Query(ge=1)],
    to_version: Annotated[int, Query(ge=1)],
) -> dict[str, Any]:
    """Field-level diff between two stored versions. ``404`` if the policy/version is absent."""
    with session_scope() as session:
        diff = repo.diff_policy_versions(session, policy_id, from_version, to_version)
    if diff is None:
        raise HTTPException(status_code=404, detail="policy or version not found")
    return diff


@app.post("/api/policies/sync", dependencies=[Depends(rbac.require_permission("policy:write"))])
def sync_policies(
    runner: Annotated[CustodianRunner | None, Depends(get_custodian_runner)] = None,
) -> dict[str, Any]:
    """Sync policies from the configured Git repo (GitOps). Returns a structured
    report (``ok`` + added/updated/unchanged/skipped counts + errors) and never
    surfaces a ``500`` for a git/validation failure."""
    from ..custodian import gitops

    return gitops.sync_policies(runner=runner)


@app.post(
    "/api/policies/{policy_id}/dryrun",
    dependencies=[Depends(rbac.require_permission("policy:run"))],
)
def dryrun_policy(
    policy_id: int,
    subscription_id: str | None = None,
    runner: Annotated[CustodianRunner | None, Depends(get_custodian_runner)] = None,
) -> dict[str, Any]:
    """Evaluate a stored policy against Azure in **dry-run** — matches, never mutates.

    Looks the policy up by id (``404`` if missing) and, when ``subscription_id`` is
    given, resolves that subscription into a run context (``404`` if unknown);
    otherwise the engine targets the default subscription. Delegates to
    ``engine.run_policy(dry_run=True)`` (mock-backed offline) and returns the matched
    resources — no remediation action is ever executed.
    """
    from ..azure.context import SubscriptionContext
    from ..config import get_settings
    from ..orchestrator import _context_from_record

    context: SubscriptionContext | None = None
    with session_scope() as session:
        policy = repo.get_policy(session, policy_id)
        if policy is None:
            raise HTTPException(status_code=404, detail="policy not found")
        if subscription_id:
            record = repo.get_subscription(session, subscription_id)
            if record is None:
                raise HTTPException(status_code=404, detail="subscription not found")
            context = _context_from_record(record, get_settings().finops_mock)

    result = custodian.run_policy(policy["spec"], subscription=context, dry_run=True, runner=runner)
    resources = result.get("resources") or []
    return {
        "policy_id": policy_id,
        "policy_name": policy["name"],
        "subscription_id": context.subscription_id
        if context
        else get_settings().azure_subscription_id,
        "dry_run": True,
        "matched": result.get("matched", len(resources)),
        "resources": resources,
    }


# --------------------------------------------------------------------------- #
# Policy collections (M2.3) — many-to-many grouping
# --------------------------------------------------------------------------- #
@app.get("/api/collections")
def list_collections() -> list[dict[str, Any]]:
    with session_scope() as session:
        return repo.list_collections(session)


@app.post(
    "/api/collections",
    status_code=201,
    dependencies=[Depends(rbac.require_permission("collection:write"))],
)
def create_collection(body: CollectionCreate) -> dict[str, Any]:
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    try:
        with session_scope() as session:
            return repo.create_collection(session, name=name, description=body.description)
    except IntegrityError as exc:
        raise HTTPException(status_code=409, detail="collection name already exists") from exc


@app.get("/api/collections/{collection_id}")
def get_collection(collection_id: int) -> dict[str, Any]:
    with session_scope() as session:
        collection = repo.get_collection(session, collection_id)
    if collection is None:
        raise HTTPException(status_code=404, detail="collection not found")
    return collection


@app.delete(
    "/api/collections/{collection_id}",
    dependencies=[Depends(rbac.require_permission("collection:write"))],
)
def delete_collection(collection_id: int) -> dict[str, Any]:
    """Delete a collection (and its memberships) — member policies are preserved."""
    with session_scope() as session:
        ok = repo.delete_collection(session, collection_id)
    if not ok:
        raise HTTPException(status_code=404, detail="collection not found")
    return {"id": collection_id, "deleted": True}


@app.post(
    "/api/collections/{collection_id}/policies/{policy_id}",
    dependencies=[Depends(rbac.require_permission("collection:write"))],
)
def add_policy_to_collection(collection_id: int, policy_id: int) -> dict[str, Any]:
    with session_scope() as session:
        collection = repo.add_policy_to_collection(session, collection_id, policy_id)
    if collection is None:
        raise HTTPException(status_code=404, detail="collection or policy not found")
    return collection


@app.delete(
    "/api/collections/{collection_id}/policies/{policy_id}",
    dependencies=[Depends(rbac.require_permission("collection:write"))],
)
def remove_policy_from_collection(collection_id: int, policy_id: int) -> dict[str, Any]:
    with session_scope() as session:
        collection = repo.remove_policy_from_collection(session, collection_id, policy_id)
    if collection is None:
        raise HTTPException(status_code=404, detail="collection or membership not found")
    return collection


# --------------------------------------------------------------------------- #
# Policy packs (M10.1) — installable, versioned bundles of curated policies
# --------------------------------------------------------------------------- #
class PackEnabled(BaseModel):
    enabled: bool


@app.get("/api/packs")
def list_packs() -> list[dict[str, Any]]:
    """List bundled packs available to install (name/version/description/policy_count)."""
    return packs.list_packs()


@app.get("/api/packs/installed")
def list_installed_packs() -> list[dict[str, Any]]:
    """List packs already installed, with their tracked version and enabled state."""
    with session_scope() as session:
        return repo.list_installed_packs(session)


@app.post(
    "/api/packs/{name}/install", dependencies=[Depends(rbac.require_permission("pack:install"))]
)
def install_pack(
    name: str,
    runner: Annotated[CustodianRunner | None, Depends(get_custodian_runner)] = None,
) -> dict[str, Any]:
    """Install a bundled pack: materialize its validated policies into a collection.

    An unknown pack is ``404``; a pack whose policies fail validation is ``422``
    (nothing is persisted). A successful install returns the install report.
    """
    report = packs.install_pack(name, runner=runner)
    if not report["ok"]:
        if report["errors"]:
            raise HTTPException(
                status_code=422,
                detail={"message": report["error"], "errors": report["errors"]},
            )
        raise HTTPException(status_code=404, detail=report["error"])
    return report


@app.post(
    "/api/packs/{name}/enabled", dependencies=[Depends(rbac.require_permission("pack:install"))]
)
def set_pack_enabled(name: str, body: PackEnabled) -> dict[str, Any]:
    """Enable/disable an installed pack — toggles its member policies' binding eligibility."""
    with session_scope() as session:
        result = repo.set_pack_enabled(session, name, body.enabled)
    if result is None:
        raise HTTPException(status_code=404, detail="pack not installed")
    return result


# --------------------------------------------------------------------------- #
# RBAC (M11.1) — roles, permissions, role bindings + the require_permission guard
# --------------------------------------------------------------------------- #
class RoleBindingIn(BaseModel):
    principal: str
    role: str


@app.get("/api/authz/me")
def authz_me(request: Request) -> dict[str, Any]:
    """The caller's principal (``X-Principal``) and resolved permissions.

    Unauthenticated callers get ``{principal: null, permissions: []}``. Always ``200``
    so the UI can decide what to render without handling an error.
    """
    principal = rbac.principal_from_request(request)
    permissions: list[str] = []
    if principal is not None:
        with session_scope() as session:
            permissions = sorted(repo.resolve_permissions(session, principal))
    return {
        "principal": principal,
        "permissions": permissions,
        "rbac_enabled": get_settings().rbac_enabled,
    }


@app.get("/api/authz/roles")
def list_roles() -> list[dict[str, Any]]:
    """List all roles with their permission grants."""
    with session_scope() as session:
        return repo.list_roles(session)


@app.get("/api/authz/role-bindings")
def list_role_bindings(principal: str | None = None) -> list[dict[str, Any]]:
    """List role bindings, optionally filtered to one ``?principal=``."""
    with session_scope() as session:
        return repo.list_role_bindings(session, principal=principal)


@app.post(
    "/api/authz/role-bindings",
    status_code=201,
    dependencies=[Depends(rbac.require_permission("rbac:admin"))],
)
def create_role_binding(body: RoleBindingIn) -> dict[str, Any]:
    """Bind a principal to a role (idempotent). ``404`` for an unknown role."""
    with session_scope() as session:
        result = repo.assign_role(session, principal=body.principal, role_name=body.role)
    if result is None:
        raise HTTPException(status_code=404, detail=f"unknown role: {body.role}")
    return result


@app.delete(
    "/api/authz/role-bindings",
    dependencies=[Depends(rbac.require_permission("rbac:admin"))],
)
def delete_role_binding(principal: str, role: str) -> dict[str, Any]:
    """Remove a principal's binding to a role. ``404`` if the binding is absent."""
    with session_scope() as session:
        ok = repo.remove_role_binding(session, principal, role)
    if not ok:
        raise HTTPException(status_code=404, detail="role binding not found")
    return {"principal": principal, "role": role, "deleted": True}


# --------------------------------------------------------------------------- #
# SSO / OIDC authentication (M11.3) — login/callback + first-party session
# --------------------------------------------------------------------------- #
def _require_oidc_enabled() -> Any:
    """404 the auth routes when OIDC is disabled (local/mock dev has no IdP)."""
    settings = get_settings()
    if not settings.oidc_enabled:
        raise HTTPException(status_code=404, detail="OIDC authentication is not enabled")
    return settings


@app.get("/api/auth/login")
def auth_login(
    client: Annotated[oidc.OIDCClient | None, Depends(get_oidc_client)] = None,
) -> dict[str, Any]:
    """Return the identity provider's authorization URL to redirect the browser to.

    ``404`` when OIDC is disabled. The opaque ``state`` is returned for the caller to
    round-trip back on the callback (CSRF guard).
    """
    settings = _require_oidc_enabled()
    state = secrets.token_urlsafe(16)
    return {
        "authorization_url": oidc.login_url(settings, state=state, client=client),
        "state": state,
    }


@app.get("/api/auth/callback")
def auth_callback(
    code: str,
    response: Response,
    state: str | None = None,
    client: Annotated[oidc.OIDCClient | None, Depends(get_oidc_client)] = None,
    verifier: Annotated[oidc.TokenVerifier | None, Depends(get_token_verifier)] = None,
) -> dict[str, Any]:
    """Exchange the auth ``code``, verify the token, and set a first-party session cookie.

    ``404`` when OIDC is disabled; ``401`` for a code the IdP rejects or a token that
    fails verification.
    """
    settings = _require_oidc_enabled()
    result = oidc.handle_callback(settings, code=code, client=client, verifier=verifier)
    response.set_cookie(
        oidc.SESSION_COOKIE,
        result["session"],
        httponly=True,
        samesite="lax",
        max_age=oidc.SESSION_TTL_SECONDS,
    )
    return {"principal": result["principal"]}


@app.post("/api/auth/logout")
def auth_logout(response: Response) -> dict[str, Any]:
    """Clear the first-party session cookie (idempotent)."""
    response.delete_cookie(oidc.SESSION_COOKIE)
    return {"logged_out": True}


# --------------------------------------------------------------------------- #
# Audit log (M11.4) — append-only trail of mutating governance actions
# --------------------------------------------------------------------------- #
@app.get("/api/audit")
def list_audit(
    actor: str | None = None,
    action: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """List audit entries newest-first, filterable by actor / action / target.

    Read-only: the audit trail is **append-only**, written as a side effect of mutating
    actions — there is deliberately no create/update/delete route here.
    """
    with session_scope() as session:
        return repo.list_audit_logs(
            session,
            actor=actor,
            action=action,
            target_type=target_type,
            target_id=target_id,
            limit=limit,
            offset=offset,
        )


# --------------------------------------------------------------------------- #
# Teams & membership (M11.2) — multi-tenancy scoping of governance resources
# --------------------------------------------------------------------------- #
class TeamCreate(BaseModel):
    name: str
    description: str | None = None


class TeamMemberIn(BaseModel):
    principal: str
    role: str = "member"


@app.get("/api/teams")
def list_teams() -> list[dict[str, Any]]:
    """List all teams (ungated read)."""
    with session_scope() as session:
        return repo.list_teams(session)


@app.get("/api/teams/{team_id}")
def get_team(team_id: int) -> dict[str, Any]:
    with session_scope() as session:
        team = repo.get_team(session, team_id)
    if team is None:
        raise HTTPException(status_code=404, detail="team not found")
    return team


@app.post(
    "/api/teams",
    status_code=201,
    dependencies=[Depends(rbac.require_permission("team:write"))],
)
def create_team(body: TeamCreate) -> dict[str, Any]:
    """Create a team (admin only). ``409`` on a duplicate name."""
    try:
        with session_scope() as session:
            return repo.create_team(session, name=body.name, description=body.description)
    except IntegrityError as exc:
        raise HTTPException(status_code=409, detail="team name already exists") from exc


@app.get("/api/teams/{team_id}/members")
def list_team_members(team_id: int) -> list[dict[str, Any]]:
    with session_scope() as session:
        return repo.list_team_members(session, team_id)


@app.post(
    "/api/teams/{team_id}/members",
    status_code=201,
    dependencies=[Depends(rbac.require_permission("team:write"))],
)
def add_team_member(team_id: int, body: TeamMemberIn) -> dict[str, Any]:
    """Add a principal to a team (admin only, idempotent). ``404`` for an unknown team."""
    with session_scope() as session:
        result = repo.add_team_member(
            session, team_id=team_id, principal=body.principal, role=body.role
        )
    if result is None:
        raise HTTPException(status_code=404, detail="team not found")
    return result


@app.delete(
    "/api/teams/{team_id}/members/{principal}",
    dependencies=[Depends(rbac.require_permission("team:write"))],
)
def remove_team_member(team_id: int, principal: str) -> dict[str, Any]:
    """Remove a principal from a team (admin only). ``404`` if the membership is absent."""
    with session_scope() as session:
        ok = repo.remove_team_member(session, team_id, principal)
    if not ok:
        raise HTTPException(status_code=404, detail="team membership not found")
    return {"team_id": team_id, "principal": principal, "removed": True}


# --------------------------------------------------------------------------- #
# Account groups (M5.1) — many-to-many grouping of subscriptions
# --------------------------------------------------------------------------- #
@app.get("/api/account-groups")
def list_account_groups() -> list[dict[str, Any]]:
    with session_scope() as session:
        return repo.list_account_groups(session)


@app.post(
    "/api/account-groups",
    status_code=201,
    dependencies=[Depends(rbac.require_permission("accountgroup:write"))],
)
def create_account_group(body: AccountGroupCreate) -> dict[str, Any]:
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    try:
        with session_scope() as session:
            return repo.create_account_group(session, name=name, description=body.description)
    except IntegrityError as exc:
        raise HTTPException(status_code=409, detail="account group name already exists") from exc


@app.get("/api/account-groups/{group_id}")
def get_account_group(group_id: int) -> dict[str, Any]:
    with session_scope() as session:
        group = repo.get_account_group(session, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="account group not found")
    return group


@app.delete(
    "/api/account-groups/{group_id}",
    dependencies=[Depends(rbac.require_permission("accountgroup:write"))],
)
def delete_account_group(group_id: int) -> dict[str, Any]:
    """Delete an account group (and its memberships) — member subscriptions are preserved."""
    with session_scope() as session:
        ok = repo.delete_account_group(session, group_id)
    if not ok:
        raise HTTPException(status_code=404, detail="account group not found")
    return {"id": group_id, "deleted": True}


@app.post(
    "/api/account-groups/{group_id}/subscriptions/{subscription_id}",
    dependencies=[Depends(rbac.require_permission("accountgroup:write"))],
)
def add_subscription_to_group(group_id: int, subscription_id: str) -> dict[str, Any]:
    with session_scope() as session:
        group = repo.add_subscription_to_group(session, group_id, subscription_id)
    if group is None:
        raise HTTPException(status_code=404, detail="account group or subscription not found")
    return group


@app.delete(
    "/api/account-groups/{group_id}/subscriptions/{subscription_id}",
    dependencies=[Depends(rbac.require_permission("accountgroup:write"))],
)
def remove_subscription_from_group(group_id: int, subscription_id: str) -> dict[str, Any]:
    with session_scope() as session:
        group = repo.remove_subscription_from_group(session, group_id, subscription_id)
    if group is None:
        raise HTTPException(status_code=404, detail="account group or membership not found")
    return group


# --------------------------------------------------------------------------- #
# Bindings (M5.2) — collection × account group + execution config
# --------------------------------------------------------------------------- #
@app.get("/api/bindings")
def list_bindings() -> list[dict[str, Any]]:
    with session_scope() as session:
        return repo.list_bindings(session)


@app.post(
    "/api/bindings",
    status_code=201,
    dependencies=[Depends(rbac.require_permission("binding:write"))],
)
def create_binding(body: BindingIn) -> dict[str, Any]:
    with session_scope() as session:
        try:
            binding = repo.create_binding(
                session,
                collection_id=body.collection_id,
                account_group_id=body.account_group_id,
                schedule=body.schedule,
                mode=body.mode,
                dry_run=body.dry_run,
                enabled=body.enabled,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    if binding is None:
        raise HTTPException(status_code=404, detail="collection or account group not found")
    return binding


@app.get("/api/bindings/{binding_id}")
def get_binding(binding_id: int) -> dict[str, Any]:
    with session_scope() as session:
        binding = repo.get_binding(session, binding_id)
    if binding is None:
        raise HTTPException(status_code=404, detail="binding not found")
    return binding


@app.put(
    "/api/bindings/{binding_id}", dependencies=[Depends(rbac.require_permission("binding:write"))]
)
def update_binding(binding_id: int, body: BindingUpdate) -> dict[str, Any]:
    changes = body.model_dump(exclude_unset=True)
    with session_scope() as session:
        try:
            binding = repo.update_binding(session, binding_id, changes)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    if binding is None:
        raise HTTPException(status_code=404, detail="binding not found")
    return binding


@app.delete(
    "/api/bindings/{binding_id}", dependencies=[Depends(rbac.require_permission("binding:write"))]
)
def delete_binding(binding_id: int) -> dict[str, Any]:
    with session_scope() as session:
        ok = repo.delete_binding(session, binding_id)
    if not ok:
        raise HTTPException(status_code=404, detail="binding not found")
    return {"id": binding_id, "deleted": True}


@app.post(
    "/api/bindings/{binding_id}/run", dependencies=[Depends(rbac.require_permission("binding:run"))]
)
def run_binding_endpoint(
    binding_id: int,
    runner: Annotated[CustodianRunner | None, Depends(get_custodian_runner)] = None,
) -> dict[str, Any]:
    """Execute a binding (M5.3): its collection's policies × its group's subscriptions.

    A disabled binding returns a ``skipped`` result; an unknown binding is ``404``.
    """
    from ..custodian.bindings import run_binding

    result = run_binding(binding_id, runner=runner)
    if result is None:
        raise HTTPException(status_code=404, detail="binding not found")
    return result


# --------------------------------------------------------------------------- #
# Real-time enforcement: Azure Event Grid ingestion (M6.1)
# --------------------------------------------------------------------------- #
@app.post("/api/events/azure")
async def ingest_azure_events(
    request: Request,
    response: Response,
    runner: Annotated[CustodianRunner | None, Depends(get_custodian_runner)] = None,
) -> dict[str, Any]:
    """Event Grid webhook: complete the one-time ``SubscriptionValidation`` handshake,
    authenticate the delivery (shared key), normalize + persist each resource event, then
    **reactively trigger** any matching event-mode policies (M6.2) and stream the change
    into the AssetDB (M6.3).

    Event Grid delivers a JSON array (not CloudEvents). Unauthenticated → ``403``;
    a non-JSON body → ``400``; unrecognized event types are silently skipped. When
    ``EVENT_MODE_ENABLED`` is off (M6.4) the delivery is accepted with ``202`` but
    stored/triggered nothing.
    """
    settings = get_settings()
    if not verify_event_grid_key(dict(request.headers), dict(request.query_params), settings):
        raise HTTPException(status_code=403, detail="invalid event grid key")
    if not settings.event_mode_enabled:
        response.status_code = 202
        return {"received": 0, "processed": 0, "detail": "event mode disabled"}
    try:
        payload = await request.json()
    except Exception as exc:  # noqa: BLE001 - any parse error is a bad body
        raise HTTPException(status_code=400, detail="invalid JSON body") from exc

    events = payload if isinstance(payload, list) else [payload]
    validation = handle_subscription_validation(events)
    if validation is not None:
        return validation

    normalized_events = []
    with session_scope() as session:
        for raw in events:
            normalized = normalize_event(raw)
            if normalized is None:
                continue
            repo.insert_event_log(session, normalized)
            normalized_events.append(normalized)
    for normalized in normalized_events:
        # M6.3: keep the AssetDB inventory current from the event stream.
        apply_asset_event(normalized)
        # M6.2: fire event-mode policies for each accepted delivery (no-op when none match).
        handle_event(normalized, runner=runner)
    return {"received": len(events), "processed": len(normalized_events)}


@app.get("/api/events")
def list_events(limit: int = 50) -> list[dict[str, Any]]:
    """Recent Event Grid deliveries, newest-first (audit / M6.4 status UI)."""
    with session_scope() as session:
        return repo.list_events(session, limit=limit)


@app.get("/api/events/recent")
def recent_events(
    limit: int = Query(20, ge=1, le=200), offset: int = Query(0, ge=0)
) -> list[dict[str, Any]]:
    """Status feed (M6.4): recent deliveries newest-first, paginated, each with the
    event-mode ``policy_executions`` it triggered. An empty feed is ``[]``, not an error."""
    with session_scope() as session:
        return repo.recent_events(session, limit=limit, offset=offset)


@app.get("/api/summary")
def latest_summary() -> dict[str, Any] | None:
    with session_scope() as session:
        return repo.latest_ai_summary(session)


@app.get("/api/runs/latest")
def latest_run() -> dict[str, Any] | None:
    with session_scope() as session:
        return repo.latest_run(session)


@app.get("/api/runs")
def runs(limit: int = 20) -> list[dict[str, Any]]:
    with session_scope() as session:
        return repo.list_runs(session, limit=limit)


@app.post("/api/runs", dependencies=[Depends(rbac.require_permission("run:trigger"))])
def trigger_run(mock: bool = False, subscription_id: str | None = None) -> dict[str, Any]:
    """Trigger a pipeline run. With no ``subscription_id`` this fans out across
    every enabled subscription; with one it runs just that subscription."""
    from ..orchestrator import run_all_subscriptions, run_one_subscription

    mock_flag = True if mock else None
    if subscription_id:
        result = run_one_subscription(subscription_id, mock=mock_flag)
        if result is None:
            raise HTTPException(status_code=404, detail="subscription not found")
        return result
    return run_all_subscriptions(mock=mock_flag)


# --------------------------------------------------------------------------- #
# Policy executions (M3.3 — pull-mode execution history & drill-down)
# --------------------------------------------------------------------------- #
@app.get("/api/policy-executions")
def policy_executions(
    policy_id: int | None = None,
    subscription_id: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """List executions newest-first, filtered by any combination of the args.

    Blank query-string filters (an "all" dropdown) normalize to no filter.
    """
    with session_scope() as session:
        return repo.list_policy_executions(
            session,
            policy_id=policy_id,
            subscription_id=subscription_id or None,
            status=status or None,
            limit=limit,
        )


@app.get("/api/policy-executions/{execution_id}")
def get_policy_execution(execution_id: str) -> dict[str, Any]:
    with session_scope() as session:
        execution = repo.get_policy_execution(session, execution_id)
    if execution is None:
        raise HTTPException(status_code=404, detail="execution not found")
    return execution


@app.get("/api/policy-executions/{execution_id}/matches")
def policy_execution_matches(execution_id: str) -> list[dict[str, Any]]:
    with session_scope() as session:
        if repo.get_policy_execution(session, execution_id) is None:
            raise HTTPException(status_code=404, detail="execution not found")
        return repo.list_policy_matches(session, execution_id)


@app.get("/api/governance/policy-health")
def policy_health() -> list[dict[str, Any]]:
    """Per-policy compliance & health aggregates (M3.4), across all subscriptions.

    Empty list when no policy has executed yet — never an error.
    """
    with session_scope() as session:
        return repo.policy_health(session)


def _provider_filter(provider: str | None) -> str | None:
    """Normalize a ``?provider=`` filter (M12.4). Empty / ``all`` → ``None`` (all clouds)."""
    if provider is None:
        return None
    normalized = provider.strip().lower()
    return None if normalized in ("", "all") else normalized


@app.get("/api/governance/posture")
def governance_posture(provider: str | None = Query(None)) -> dict[str, Any]:
    """Compliance posture (M9.1, M10.4, M12.4): compliant vs non-compliant counts grouped
    by policy, subscription, collection, CIS ``control_id`` and cloud ``provider``, from
    the latest execution per ``(policy, subscription)``.

    The response is ``{totals, by_policy, by_subscription, by_collection, by_control,
    by_provider}``. ``by_control`` rolls posture up by each policy's ``metadata.control_id``
    (framework framing, e.g. the CIS Azure pack); policies without one are excluded.
    ``?provider=azure|aws|gcp`` scopes the whole response to one cloud; omitting it (or
    ``?provider=all``) spans every cloud. With nothing executed yet the totals are zeroed
    and the group lists empty — the empty state is data, never an error.
    """
    with session_scope() as session:
        return repo.governance_posture(session, provider=_provider_filter(provider))


@app.get("/api/governance/execution-health")
def execution_health(provider: str | None = Query(None)) -> dict[str, Any]:
    """Policy execution health (M9.2, M12.4): the governance engine's own health.

    Returns ``{by_policy, by_binding, by_provider}`` — succeeded/failed counts, success
    rate, average wall-clock duration and last run, per policy, per binding and per cloud.
    ``?provider=azure|aws|gcp`` scopes ``by_policy``/``by_binding`` to that cloud and
    narrows ``by_provider`` to its row; omitting it (or ``?provider=all``) spans every
    cloud. With nothing executed yet the lists are empty — never an error.
    """
    with session_scope() as session:
        return repo.execution_health(session, provider=_provider_filter(provider))


@app.get("/api/governance/policies/{policy_id}/matches")
def policy_matched_resources(policy_id: int) -> list[dict[str, Any]]:
    """Resources currently flagged by a policy (M9.3) — the compliance-explorer
    drill-down: policy → matched resources → asset detail.

    Returns each subscription's latest execution's matches (``resource_id``,
    ``resource_type``, ``subscription_id``, ``matched_at``), newest first. ``404``
    when the policy does not exist; an empty list when it has no matches — never an
    error.
    """
    with session_scope() as session:
        if repo.get_policy(session, policy_id) is None:
            raise HTTPException(status_code=404, detail="policy not found")
        return repo.policy_matched_resources(session, policy_id)


@app.get("/api/governance/export")
def governance_export(fmt: str = Query("csv", alias="format")) -> StreamingResponse:
    """Stream the governance evidence (per-execution: policy, subscription, status,
    matches, timing) as CSV or JSON (M9.4).

    ``?format=csv`` returns a header row + one line per execution; ``?format=json``
    returns a JSON array of the same records. Any other ``format`` → ``400``. The
    response streams from a paginated cursor, so an arbitrarily large history is
    never loaded into memory at once. The session lives inside the streaming
    generator so it outlives the (lazy) response body.
    """
    if fmt not in reporting.EXPORT_FORMATS:
        raise HTTPException(status_code=400, detail=f"unsupported format: {fmt!r}")
    media_type = "text/csv" if fmt == "csv" else "application/json"
    headers = {"Content-Disposition": f'attachment; filename="governance-export.{fmt}"'}
    return StreamingResponse(
        reporting.stream_export_owning_session(fmt),
        media_type=media_type,
        headers=headers,
    )


@app.post("/api/assets/query")
def query_assets(body: AssetQuery) -> list[dict[str, Any]]:
    """Filter AssetDB via an allow-listed, injection-safe query (M4.2).

    An unknown filter column or operator is rejected with ``400`` and never executed;
    all values (including tag values) are bound as parameters.
    """
    with session_scope() as session:
        try:
            return repo.query_assets(session, body)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/assets/{resource_id:path}/relationships")
def asset_relationships(resource_id: str) -> list[dict[str, Any]]:
    """Return an asset's relationship edges — inbound and outbound (M4.3).

    Full Azure resource ids contain slashes, so ``resource_id`` is a path
    parameter; the leading slash is normalized back on so it matches the stored
    (leading-slash) asset id. Unknown ids simply yield an empty list.
    """
    normalized = "/" + resource_id.lstrip("/")
    with session_scope() as session:
        return repo.get_relationships(session, normalized)


@app.get("/api/assets/{resource_id:path}/history")
def asset_history(resource_id: str) -> list[dict[str, Any]]:
    """Return an asset's change timeline — its audit events, newest-first (M4.4).

    The timeline combines lifecycle (``created``) events with the ingested Azure
    Activity Log (``activity`` — actor / operation / timestamp). ``resource_id`` is a
    path parameter (Azure ids contain slashes); the leading slash is normalized back
    on to match the stored id. An unknown asset yields an empty list, not an error.
    """
    normalized = "/" + resource_id.lstrip("/")
    with session_scope() as session:
        return repo.get_asset_history(session, normalized)


# --------------------------------------------------------------------------- #
# Subscriptions (multi-subscription management)
# --------------------------------------------------------------------------- #
class SubscriptionIn(BaseModel):
    subscription_id: str
    display_name: str
    provider: str = "azure"  # owning cloud (M12.1); defaults to Azure
    tenant_id: str | None = None
    client_id: str | None = None
    client_secret: str | None = None  # None keeps existing, "" clears, else sets
    enabled: bool = True


@app.get("/api/subscriptions")
def list_subscriptions() -> list[dict[str, Any]]:
    with session_scope() as session:
        return repo.list_subscriptions(session)


@app.post(
    "/api/subscriptions", dependencies=[Depends(rbac.require_permission("subscription:write"))]
)
def upsert_subscription(body: SubscriptionIn) -> dict[str, Any]:
    sub_id = body.subscription_id.strip()
    if not sub_id or not body.display_name.strip():
        raise HTTPException(status_code=400, detail="subscription_id and display_name are required")
    with session_scope() as session:
        return repo.upsert_subscription(
            session,
            subscription_id=sub_id,
            display_name=body.display_name.strip(),
            provider=(body.provider or "azure").strip(),
            tenant_id=body.tenant_id,
            client_id=body.client_id,
            client_secret=body.client_secret,
            enabled=body.enabled,
        )


@app.delete(
    "/api/subscriptions/{subscription_id}",
    dependencies=[Depends(rbac.require_permission("subscription:write"))],
)
def delete_subscription(subscription_id: str) -> dict[str, Any]:
    with session_scope() as session:
        ok = repo.delete_subscription(session, subscription_id)
    if not ok:
        raise HTTPException(status_code=404, detail="subscription not found")
    return {"subscription_id": subscription_id, "deleted": True}


@app.post(
    "/api/subscriptions/{subscription_id}/default",
    dependencies=[Depends(rbac.require_permission("subscription:write"))],
)
def set_default_subscription(subscription_id: str) -> dict[str, Any]:
    with session_scope() as session:
        ok = repo.set_default_subscription(session, subscription_id)
    if not ok:
        raise HTTPException(status_code=404, detail="subscription not found")
    return {"subscription_id": subscription_id, "is_default": True}


@app.post(
    "/api/subscriptions/{subscription_id}/test",
    dependencies=[Depends(rbac.require_permission("subscription:write"))],
)
def test_subscription(subscription_id: str) -> dict[str, Any]:
    """Verify the subscription's credential can reach Azure and see the sub."""
    from ..azure.connectivity import check_connection
    from ..config import get_settings

    with session_scope() as session:
        rec = repo.get_subscription(session, subscription_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="subscription not found")

    credential = None
    if not get_settings().finops_mock and rec.client_id and rec.client_secret:
        from ..auth import credential_for

        credential = credential_for(rec.tenant_id, rec.client_id, rec.client_secret)
    result = check_connection(rec.subscription_id, credential=credential)
    # Sync the real Azure subscription name into the stored display name (only while
    # it is still the auto-generated placeholder — never overwrite a user's name).
    if result.get("ok") and result.get("subscription_name"):
        with session_scope() as session:
            repo.backfill_display_name(session, rec.subscription_id, result["subscription_name"])
    return result


# --------------------------------------------------------------------------- #
# AWS onboarding & execution (M12.2 — multi-cloud). Credentials are validated
# via STS get_caller_identity (injectable seam → offline in tests); AWS assets
# ingest into AssetDB tagged provider='aws'; policy dry-runs match the fixture.
# --------------------------------------------------------------------------- #
class AwsAccountIn(BaseModel):
    account_id: str
    display_name: str | None = None
    region: str | None = None
    role_arn: str | None = None
    access_key_id: str | None = None
    secret_access_key: str | None = None


class AwsDryRunIn(BaseModel):
    account_id: str
    spec: dict[str, Any]
    region: str | None = None


@app.post(
    "/api/aws/accounts",
    status_code=201,
    dependencies=[Depends(rbac.require_permission("subscription:write"))],
)
def onboard_aws_account(
    body: AwsAccountIn,
    sts: Annotated[aws_provider.StsClient | None, Depends(get_aws_sts_client)] = None,
) -> dict[str, Any]:
    """Onboard an AWS account: validate its credentials, then persist it (provider='aws')."""
    account_id = body.account_id.strip()
    if not account_id:
        raise HTTPException(status_code=400, detail="account_id is required")
    provider = providers_registry.get("aws")
    credential = {
        "region": body.region,
        "role_arn": body.role_arn,
        "access_key_id": body.access_key_id,
        "secret_access_key": body.secret_access_key,
    }
    try:
        identity = provider.validate_account(
            account_id=account_id, credential=credential, client=sts
        )
    except aws_provider.InvalidCredentialsError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    with session_scope() as session:
        account = repo.upsert_subscription(
            session,
            subscription_id=account_id,
            display_name=(body.display_name or f"AWS {account_id}").strip(),
            provider="aws",
        )
    return {"account": account, "identity": identity}


@app.post(
    "/api/aws/accounts/{account_id}/ingest",
    dependencies=[Depends(rbac.require_permission("subscription:write"))],
)
def ingest_aws_assets(account_id: str) -> dict[str, Any]:
    """Ingest an AWS account's resources into AssetDB (provider='aws'); returns counts."""
    provider = providers_registry.get("aws")
    records = provider.collect_assets(account_id=account_id)
    with session_scope() as session:
        repo.upsert_resources(session, records)
        new_ids = repo.upsert_assets(session, records)
        by_id = {r.resource_id: r for r in records}
        for rid in new_ids:
            rec = by_id[rid]
            repo.append_asset_event(
                session,
                resource_id=rid,
                subscription_id=rec.subscription_id,
                event_type="created",
                data=rec.config,
            )
    return {
        "provider": "aws",
        "account_id": account_id,
        "assets": len(records),
        "new": len(new_ids),
    }


@app.post("/api/aws/policies/dryrun")
def aws_policy_dryrun(body: AwsDryRunIn) -> dict[str, Any]:
    """Dry-run a c7n aws policy against an account; returns matched fixture resources."""
    provider = providers_registry.get("aws")
    return provider.run_policy(
        body.spec, account_id=body.account_id.strip(), region=body.region, dry_run=True
    )


# --------------------------------------------------------------------------- #
# GCP onboarding & execution (M12.3 — multi-cloud). Credentials are validated
# via Resource Manager get_project (injectable seam → offline in tests); GCP
# assets ingest into AssetDB tagged provider='gcp'; policy dry-runs match the
# fixture. Mirrors the AWS onboarding surface (M12.2).
# --------------------------------------------------------------------------- #
class GcpProjectIn(BaseModel):
    project_id: str
    display_name: str | None = None
    region: str | None = None
    service_account_info: dict[str, Any] | None = None


class GcpDryRunIn(BaseModel):
    project_id: str
    spec: dict[str, Any]
    region: str | None = None


@app.post(
    "/api/gcp/projects",
    status_code=201,
    dependencies=[Depends(rbac.require_permission("subscription:write"))],
)
def onboard_gcp_project(
    body: GcpProjectIn,
    client: Annotated[gcp_provider.ResourceManagerClient | None, Depends(get_gcp_client)] = None,
) -> dict[str, Any]:
    """Onboard a GCP project: validate its credentials, then persist it (provider='gcp')."""
    project_id = body.project_id.strip()
    if not project_id:
        raise HTTPException(status_code=400, detail="project_id is required")
    provider = providers_registry.get("gcp")
    credential = {"region": body.region, "service_account_info": body.service_account_info}
    try:
        identity = provider.validate_project(
            project_id=project_id, credential=credential, client=client
        )
    except gcp_provider.InvalidCredentialsError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    with session_scope() as session:
        account = repo.upsert_subscription(
            session,
            subscription_id=project_id,
            display_name=(body.display_name or f"GCP {project_id}").strip(),
            provider="gcp",
        )
    return {"account": account, "identity": identity}


@app.post(
    "/api/gcp/projects/{project_id}/ingest",
    dependencies=[Depends(rbac.require_permission("subscription:write"))],
)
def ingest_gcp_assets(project_id: str) -> dict[str, Any]:
    """Ingest a GCP project's resources into AssetDB (provider='gcp'); returns counts."""
    provider = providers_registry.get("gcp")
    records = provider.collect_assets(project_id=project_id)
    with session_scope() as session:
        repo.upsert_resources(session, records)
        new_ids = repo.upsert_assets(session, records)
        by_id = {r.resource_id: r for r in records}
        for rid in new_ids:
            rec = by_id[rid]
            repo.append_asset_event(
                session,
                resource_id=rid,
                subscription_id=rec.subscription_id,
                event_type="created",
                data=rec.config,
            )
    return {
        "provider": "gcp",
        "project_id": project_id,
        "assets": len(records),
        "new": len(new_ids),
    }


@app.post("/api/gcp/policies/dryrun")
def gcp_policy_dryrun(body: GcpDryRunIn) -> dict[str, Any]:
    """Dry-run a c7n gcp policy against a project; returns matched fixture resources."""
    provider = providers_registry.get("gcp")
    return provider.run_policy(
        body.spec, project_id=body.project_id.strip(), region=body.region, dry_run=True
    )


@app.post(
    "/api/recommendations/{rec_id}/remediate",
    dependencies=[Depends(rbac.require_permission("remediation:approve"))],
)
def remediate(rec_id: int, dry_run: bool = True, actor: str | None = None) -> dict[str, Any]:
    with session_scope() as session:
        try:
            return remediation.remediate(session, rec_id, actor=actor or "ui", dry_run=dry_run)
        except remediation.NotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except remediation.NotApproved as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/remediation")
def remediation_actions(limit: int = 100, source: str | None = None) -> list[dict[str, Any]]:
    """Unified remediation audit (M7.4). Optional ``source`` filter:
    ``recommendation`` | ``policy`` | ``binding``."""
    with session_scope() as session:
        return repo.list_remediation_actions(session, limit=limit, source=source)


# --------------------------------------------------------------------------- #
# Approval workflow for policy-driven actions (M7.2)
# --------------------------------------------------------------------------- #
class QueueActionRequest(BaseModel):
    action: str | dict[str, Any]  # a c7n action: "stop" or {"type": "tag", ...}
    actor: str | None = None
    dry_run: bool = False


@app.post(
    "/api/policy-matches/{match_id}/actions",
    dependencies=[Depends(rbac.require_permission("remediation:approve"))],
)
def queue_policy_action(match_id: int, body: QueueActionRequest) -> dict[str, Any]:
    """Queue a matched resource's action as **pending** approval — never executes."""
    with session_scope() as session:
        try:
            return remediation.queue_policy_action(
                session, match_id, body.action, actor=body.actor, dry_run=body.dry_run
            )
        except remediation.NotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:  # unresolvable action spec
            raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post(
    "/api/remediation/{action_id}/approve",
    dependencies=[Depends(rbac.require_permission("remediation:approve"))],
)
def approve_remediation(action_id: int, actor: str | None = None) -> dict[str, Any]:
    """Approve a pending action → guarded execution. 404 unknown, 409 already-decided."""
    with session_scope() as session:
        try:
            return remediation.approve_action(session, action_id, actor=actor or "ui")
        except remediation.NotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except remediation.AlreadyDecided as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post(
    "/api/remediation/{action_id}/reject",
    dependencies=[Depends(rbac.require_permission("remediation:approve"))],
)
def reject_remediation(action_id: int, actor: str | None = None) -> dict[str, Any]:
    """Reject a pending action — never executes. 404 unknown, 409 already-decided."""
    with session_scope() as session:
        try:
            return remediation.reject_action(session, action_id, actor=actor or "ui")
        except remediation.NotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except remediation.AlreadyDecided as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc


# --------------------------------------------------------------------------- #
# Notifications: channels + templates CRUD and per-binding wiring (M8.4)
# --------------------------------------------------------------------------- #
@app.get("/api/notification-channels")
def list_notification_channels() -> list[dict[str, Any]]:
    with session_scope() as session:
        return repo.list_notification_channels(session)


@app.post(
    "/api/notification-channels",
    status_code=201,
    dependencies=[Depends(rbac.require_permission("notification:write"))],
)
def create_notification_channel(body: NotificationChannelIn) -> dict[str, Any]:
    if body.transport not in KNOWN_TRANSPORTS:
        raise HTTPException(status_code=400, detail=f"unknown transport '{body.transport}'")
    if not body.target.strip():
        raise HTTPException(status_code=400, detail="target is required")
    try:
        with session_scope() as session:
            return repo.create_notification_channel(
                session,
                name=body.name,
                target=body.target,
                transport=body.transport,
                config=body.config,
                enabled=body.enabled,
            )
    except IntegrityError as exc:
        raise HTTPException(status_code=400, detail="channel name already exists") from exc


@app.get("/api/notification-channels/{channel_id}")
def get_notification_channel(channel_id: int) -> dict[str, Any]:
    with session_scope() as session:
        channel = repo.get_notification_channel(session, channel_id)
    if channel is None:
        raise HTTPException(status_code=404, detail="channel not found")
    return channel


@app.put(
    "/api/notification-channels/{channel_id}",
    dependencies=[Depends(rbac.require_permission("notification:write"))],
)
def update_notification_channel(channel_id: int, body: NotificationChannelUpdate) -> dict[str, Any]:
    changes = body.model_dump(exclude_unset=True)
    if "transport" in changes and changes["transport"] not in KNOWN_TRANSPORTS:
        raise HTTPException(status_code=400, detail=f"unknown transport '{changes['transport']}'")
    with session_scope() as session:
        channel = repo.update_notification_channel(session, channel_id, changes)
    if channel is None:
        raise HTTPException(status_code=404, detail="channel not found")
    return channel


@app.delete(
    "/api/notification-channels/{channel_id}",
    dependencies=[Depends(rbac.require_permission("notification:write"))],
)
def delete_notification_channel(channel_id: int) -> dict[str, Any]:
    with session_scope() as session:
        ok = repo.delete_notification_channel(session, channel_id)
    if not ok:
        raise HTTPException(status_code=404, detail="channel not found")
    return {"id": channel_id, "deleted": True}


@app.get("/api/notification-templates")
def list_notification_templates() -> list[dict[str, Any]]:
    with session_scope() as session:
        return repo.list_notification_templates(session)


@app.post(
    "/api/notification-templates",
    status_code=201,
    dependencies=[Depends(rbac.require_permission("notification:write"))],
)
def create_notification_template(body: NotificationTemplateIn) -> dict[str, Any]:
    try:
        with session_scope() as session:
            return repo.create_notification_template(
                session,
                name=body.name,
                body=body.body,
                subject=body.subject,
                format=body.format,
                description=body.description,
            )
    except IntegrityError as exc:
        raise HTTPException(status_code=400, detail="template name already exists") from exc


@app.get("/api/notification-templates/{template_id}")
def get_notification_template(template_id: int) -> dict[str, Any]:
    with session_scope() as session:
        template = repo.get_notification_template(session, template_id)
    if template is None:
        raise HTTPException(status_code=404, detail="template not found")
    return template


@app.put(
    "/api/notification-templates/{template_id}",
    dependencies=[Depends(rbac.require_permission("notification:write"))],
)
def update_notification_template(
    template_id: int, body: NotificationTemplateUpdate
) -> dict[str, Any]:
    changes = body.model_dump(exclude_unset=True)
    with session_scope() as session:
        template = repo.update_notification_template(session, template_id, changes)
    if template is None:
        raise HTTPException(status_code=404, detail="template not found")
    return template


@app.delete(
    "/api/notification-templates/{template_id}",
    dependencies=[Depends(rbac.require_permission("notification:write"))],
)
def delete_notification_template(template_id: int) -> dict[str, Any]:
    with session_scope() as session:
        ok = repo.delete_notification_template(session, template_id)
    if not ok:
        raise HTTPException(status_code=404, detail="template not found")
    return {"id": template_id, "deleted": True}


@app.get("/api/bindings/{binding_id}/notifications")
def list_binding_notifications(binding_id: int) -> list[dict[str, Any]]:
    with session_scope() as session:
        return repo.list_binding_notifications(session, binding_id)


@app.post(
    "/api/bindings/{binding_id}/notifications",
    status_code=201,
    dependencies=[Depends(rbac.require_permission("notification:write"))],
)
def create_binding_notification(binding_id: int, body: BindingNotificationIn) -> dict[str, Any]:
    with session_scope() as session:
        try:
            link = repo.create_binding_notification(
                session,
                binding_id=binding_id,
                channel_id=body.channel_id,
                template_id=body.template_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    if link is None:
        raise HTTPException(status_code=404, detail="binding, channel or template not found")
    return link


@app.delete(
    "/api/bindings/{binding_id}/notifications/{notification_id}",
    dependencies=[Depends(rbac.require_permission("notification:write"))],
)
def delete_binding_notification(binding_id: int, notification_id: int) -> dict[str, Any]:
    with session_scope() as session:
        ok = repo.delete_binding_notification(session, notification_id)
    if not ok:
        raise HTTPException(status_code=404, detail="notification attachment not found")
    return {"id": notification_id, "deleted": True}
