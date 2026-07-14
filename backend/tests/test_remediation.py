"""Remediation guardrails + executor dry-run (pure, offline — no Azure/DB)."""

from __future__ import annotations

from cloudwarden.config import Settings
from cloudwarden.remediation import executor, guardrails

RID = "/subscriptions/s/resourceGroups/rg-app/providers/Microsoft.Compute/virtualMachines/vm-web-01"


def _settings(**kw) -> Settings:
    base = dict(
        allowed_resource_groups="rg-app",
        exclude_tag="finops:exclude",
        remediation_enabled=True,
    )
    base.update(kw)
    return Settings(**base)


def test_guard_allows_listed_rg() -> None:
    result = guardrails.check(RID, {}, _settings())
    assert result.allowed and not result.reasons


def test_guard_blocks_unlisted_rg() -> None:
    result = guardrails.check(RID, {}, _settings(allowed_resource_groups="rg-other"))
    assert not result.allowed and any("allow-list" in r for r in result.reasons)


def test_guard_blocks_exclude_tag() -> None:
    result = guardrails.check(RID, {"finops": "exclude"}, _settings(allowed_resource_groups="*"))
    assert not result.allowed and any("excluded by tag" in r for r in result.reasons)


def test_guard_wildcard_allows_any() -> None:
    assert guardrails.check(RID, {}, _settings(allowed_resource_groups="*")).allowed


def test_guard_empty_allowlist_denies() -> None:
    assert not guardrails.check(RID, {}, _settings(allowed_resource_groups="")).allowed


def test_resource_group_of() -> None:
    assert guardrails.resource_group_of(RID) == "rg-app"


def test_executor_dry_run_preview() -> None:
    res = executor.execute(
        "resize", RID, {"recommended_sku": "Standard_D2s_v5"}, _settings(), dry_run=True
    )
    assert res["dry_run"] is True and res["executed"] is False
    assert "resize" in res["message"] and "Standard_D2s_v5" in res["message"]


def test_executor_unsupported_action_not_executed() -> None:
    res = executor.execute("delete_plan", RID, {}, _settings(), dry_run=False)
    assert res["executed"] is False and "manually" in res["message"]
