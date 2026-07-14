"""Guardrails for policy-driven actions (M7.3) — block-by-default safety model.

Written test-first (TDD). The guardrail is a **pure function** — no Azure, no c7n,
no network — so the happy/edge paths are unit-tested directly against
``guardrails.check`` / ``guardrails.default_dry_run``. One DB-backed test proves
the guardrail is actually *enforced* in the approval flow (a disallowed action
type is hard-blocked, never executed).

Contract under test (Arrange–Act–Assert), each test one reason to fail:

* an allow-listed resource group with no exclude tag → allowed;
* a non-allow-listed resource group → blocked, with a reason;
* a resource carrying an exclude tag (``finops:exclude`` or the built-in
  ``custodian:exclude``) → blocked, never actioned;
* an action type not in the binding's allow-list → blocked;
* with guardrails unset, an action defaults to dry-run.
"""

from __future__ import annotations

from cloudwarden.config import Settings, get_settings
from cloudwarden.remediation import approval, guardrails
from cloudwarden.storage import schema
from cloudwarden.storage.db import session_scope

RID = "/subscriptions/s/resourceGroups/rg-app/providers/Microsoft.Compute/virtualMachines/vm-1"


def _settings(**kw) -> Settings:
    base = dict(
        allowed_resource_groups="rg-app",
        exclude_tag="finops:exclude",
        remediation_enabled=True,
    )
    base.update(kw)
    return Settings(**base)


# --------------------------------------------------------------------------- #
# resource-group allow-list + exclude tag
# --------------------------------------------------------------------------- #
def test_allowlisted_rg_allowed() -> None:
    result = guardrails.check(RID, {}, _settings())
    assert result.allowed and not result.reasons


def test_non_allowlisted_rg_blocked() -> None:
    result = guardrails.check(RID, {}, _settings(allowed_resource_groups="rg-other"))
    assert not result.allowed
    assert any("allow-list" in r for r in result.reasons)


def test_exclude_tag_blocks_action() -> None:
    result = guardrails.check(RID, {"finops": "exclude"}, _settings(allowed_resource_groups="*"))
    assert not result.allowed
    assert any("excluded by tag" in r for r in result.reasons)


def test_custodian_exclude_tag_blocks_action() -> None:
    """``custodian:exclude`` is honoured out of the box (Custodian's convention)."""
    result = guardrails.check(RID, {"custodian": "exclude"}, _settings(allowed_resource_groups="*"))
    assert not result.allowed
    assert any("excluded by tag" in r for r in result.reasons)


def test_blocking_reasons_accumulate() -> None:
    """A resource can fail more than one guardrail; every reason is reported."""
    result = guardrails.check(
        RID, {"custodian": "exclude"}, _settings(allowed_resource_groups="rg-other")
    )
    assert not result.allowed and len(result.reasons) >= 2


# --------------------------------------------------------------------------- #
# per-binding action allow-list
# --------------------------------------------------------------------------- #
def test_action_not_in_binding_allowlist_blocked() -> None:
    result = guardrails.check(
        RID, {}, _settings(allowed_resource_groups="*"), action="stop", allowed_actions=["tag"]
    )
    assert not result.allowed
    assert any("action type" in r and "stop" in r for r in result.reasons)


def test_action_in_binding_allowlist_allowed() -> None:
    result = guardrails.check(
        RID,
        {},
        _settings(allowed_resource_groups="*"),
        action="tag",
        allowed_actions=["tag", "stop"],
    )
    assert result.allowed and not result.reasons


def test_action_allowlist_case_insensitive() -> None:
    result = guardrails.check(
        RID, {}, _settings(allowed_resource_groups="*"), action="STOP", allowed_actions=["stop"]
    )
    assert result.allowed


def test_action_allowlist_falls_back_to_settings() -> None:
    """With no per-binding list, the global ``ALLOWED_ACTIONS`` setting applies."""
    result = guardrails.check(
        RID, {}, _settings(allowed_resource_groups="*", allowed_actions="tag"), action="stop"
    )
    assert not result.allowed
    assert any("action type" in r for r in result.reasons)


def test_empty_action_allowlist_permits_any_action() -> None:
    """An unset allow-list places no restriction on which actions may run."""
    result = guardrails.check(RID, {}, _settings(allowed_resource_groups="*"), action="stop")
    assert result.allowed


def test_action_allowlist_ignored_when_action_unspecified() -> None:
    """No action to evaluate ⇒ the action allow-list cannot block."""
    result = guardrails.check(
        RID, {}, _settings(allowed_resource_groups="*"), allowed_actions=["tag"]
    )
    assert result.allowed


# --------------------------------------------------------------------------- #
# dry-run default
# --------------------------------------------------------------------------- #
def test_defaults_to_dry_run_when_unset() -> None:
    """No allow-list configured ⇒ actions default to a safe dry-run."""
    assert guardrails.default_dry_run(_settings(allowed_resource_groups="")) is True


def test_defaults_to_dry_run_when_remediation_disabled() -> None:
    assert guardrails.default_dry_run(_settings(remediation_enabled=False)) is True


def test_configured_guardrails_permit_real_exec() -> None:
    """Fully configured (enabled + allow-list) ⇒ dry-run is not forced."""
    assert guardrails.default_dry_run(_settings()) is False


# --------------------------------------------------------------------------- #
# config parsing
# --------------------------------------------------------------------------- #
def test_allowed_actions_list_parses_and_trims() -> None:
    assert Settings(allowed_actions="tag, stop ,").allowed_actions_list == ["tag", "stop"]


def test_allowed_actions_list_empty_by_default() -> None:
    assert Settings().allowed_actions_list == []


def test_resource_group_of_none_when_absent() -> None:
    """An id without a resource-group segment yields ``None`` (no rg to match)."""
    assert guardrails.resource_group_of("/subscriptions/s/providers/Microsoft.X/y") is None


# --------------------------------------------------------------------------- #
# enforcement — the guardrail actually blocks in the approval flow (DB-backed)
# --------------------------------------------------------------------------- #
def _seed_match(resource_id: str = RID, resource_type: str = "azure.vm") -> int:
    from cloudwarden.storage import repository as repo

    with session_scope() as s:
        pid = repo.create_policy(
            s,
            name="guard-vms",
            resource_type="azure.vm",
            spec={"policies": [{"name": "guard-vms", "resource": "azure.vm", "actions": ["tag"]}]},
        )["id"]
        repo.create_policy_execution(s, execution_id="ex-1", policy_id=pid, subscription_id="sub-1")
        match = schema.PolicyMatch(
            execution_id="ex-1", resource_id=resource_id, resource_type=resource_type
        )
        s.add(match)
        s.flush()
        return match.id


def test_approve_blocked_by_action_allowlist(db, monkeypatch) -> None:
    """A real execution of an action type outside the allow-list is hard-blocked."""
    match_id = _seed_match()
    monkeypatch.setenv("REMEDIATION_ENABLED", "true")
    monkeypatch.setenv("ALLOWED_RESOURCE_GROUPS", "rg-app")
    monkeypatch.setenv("ALLOWED_ACTIONS", "tag")  # only 'tag' is permitted
    get_settings.cache_clear()

    with session_scope() as s:
        aid = approval.queue_policy_action(s, match_id, "stop", dry_run=False)["action_id"]

    with session_scope() as s:
        res = approval.approve_action(s, aid)

    assert res["status"] == "blocked"
    assert "action type" in (res["error"] or "")
    with session_scope() as s:  # never executed
        row = s.get(schema.RemediationAction, aid)
        assert row.status == "blocked" and not row.result
    get_settings.cache_clear()
