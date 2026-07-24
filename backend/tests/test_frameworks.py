"""M14.13 — compliance framework overlays & auditor evidence export.

Written test-first (TDD). A *framework overlay* is a versioned YAML mapping each
control to zero-or-more existing policies (SOC 2 / ISO 27001 / PCI / NIST). Per-
control posture rolls up the mapped policies' latest pass/fail; a control with
**no** mapped policy is a coverage **gap** (never counted compliant). The evidence
bundle exports control → policy → matched resources → status + run timestamps and
**reconciles** with posture. Frameworks install/version via the pack registry.

Fixture mappings (a temp ``frameworks_dir``) and seeded ``PolicyExecution`` /
``PolicyMatch`` rows keep every DB-backed case isolated and repeatable. Loader,
list and unknown-id cases need no DB.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from cloudwarden.api.main import app
from cloudwarden.governance import frameworks as fw
from cloudwarden.models import PolicyMatch
from cloudwarden.packs import registry as packs
from cloudwarden.storage import repository as repo
from cloudwarden.storage.db import session_scope

SHIPPED = ("soc2", "iso27001", "pci", "nist80053")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
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
    matches: list[PolicyMatch] | None = None,
    status: str = "succeeded",
) -> None:
    """Open → (optionally match) → close an execution. Lexicographically-greater
    ids (``e1`` < ``e2`` …) keep the latest-per-pair ordering deterministic when
    same-transaction seeds share a ``started_at``."""
    repo.create_policy_execution(
        session, execution_id=execution_id, policy_id=policy_id, subscription_id=subscription_id
    )
    if matches:
        repo.insert_policy_matches(session, execution_id, matches)
    repo.finish_policy_execution(
        session, execution_id, status=status, resources_matched=resources_matched
    )


def _write_framework(tmp_path: Path, spec: dict) -> Path:
    """Materialize a fixture overlay YAML in a temp ``frameworks/`` dir; return the dir."""
    d = tmp_path / "frameworks"
    d.mkdir(exist_ok=True)
    (d / f"{spec['name']}.yaml").write_text(yaml.safe_dump(spec, sort_keys=False))
    return d


def _control(spec_controls: list[dict], control_id: str) -> dict:
    return next(c for c in spec_controls if c["id"] == control_id)


# --------------------------------------------------------------------------- #
# 1. Mapping loads (no DB)
# --------------------------------------------------------------------------- #
def test_framework_mapping_loads() -> None:
    fwk = fw.load_framework("soc2")
    assert fwk is not None
    assert fwk["name"] == "soc2"
    assert str(fwk["version"])  # a non-empty version string
    # Ordered controls, each with an id and a (possibly empty) policies list.
    assert fwk["controls"], "soc2 must ship controls"
    first = fwk["controls"][0]
    assert first["id"]
    assert isinstance(first["policies"], list)
    # At least one control maps to an existing (CIS) policy — a real overlay.
    assert any(c["policies"] for c in fwk["controls"])


def test_load_unknown_framework_returns_none() -> None:
    assert fw.load_framework("does-not-exist") is None


def test_list_frameworks_includes_all_shipped() -> None:
    names = {f["name"] for f in fw.list_frameworks()}
    assert set(SHIPPED) <= names
    # Every summary carries a version and a control count.
    for f in fw.list_frameworks():
        assert str(f["version"])
        assert f["control_count"] >= 1


# --------------------------------------------------------------------------- #
# 2. Per-control posture rollup
# --------------------------------------------------------------------------- #
def test_control_compliant_when_mapped_policies_pass(db, tmp_path) -> None:
    spec = {
        "name": "fixture-fw",
        "version": "1.0.0",
        "title": "Fixture",
        "controls": [
            {"id": "A.1", "title": "Two clean policies", "policies": ["p-clean-a", "p-clean-b"]}
        ],
    }
    fdir = _write_framework(tmp_path, spec)
    with session_scope() as s:
        a = _make_policy(s, "p-clean-a")
        b = _make_policy(s, "p-clean-b")
        _seed(s, execution_id="e1", policy_id=a, subscription_id="sub-a", resources_matched=0)
        _seed(s, execution_id="e2", policy_id=b, subscription_id="sub-a", resources_matched=0)

    with session_scope() as s:
        posture = fw.framework_posture(s, "fixture-fw", frameworks_dir=fdir)

    control = posture["controls"][0]
    assert control["status"] == "compliant"
    assert control["gap"] is False
    assert posture["totals"]["compliant"] == 1
    assert posture["totals"]["non_compliant"] == 0


def test_control_noncompliant_when_any_mapped_fails(db, tmp_path) -> None:
    spec = {
        "name": "fixture-fw",
        "version": "1.0.0",
        "title": "Fixture",
        "controls": [
            {"id": "A.1", "title": "One fails", "policies": ["p-clean", "p-bad"]},
        ],
    }
    fdir = _write_framework(tmp_path, spec)
    with session_scope() as s:
        clean = _make_policy(s, "p-clean")
        bad = _make_policy(s, "p-bad")
        _seed(s, execution_id="e1", policy_id=clean, subscription_id="sub-a", resources_matched=0)
        _seed(s, execution_id="e2", policy_id=bad, subscription_id="sub-a", resources_matched=4)

    with session_scope() as s:
        posture = fw.framework_posture(s, "fixture-fw", frameworks_dir=fdir)

    control = posture["controls"][0]
    assert control["status"] == "non_compliant"
    assert control["resources_matched"] == 4
    assert posture["totals"]["non_compliant"] == 1


def test_unmapped_control_flagged_as_gap(db, tmp_path) -> None:
    spec = {
        "name": "fixture-fw",
        "version": "1.0.0",
        "title": "Fixture",
        "controls": [
            {"id": "GAP.1", "title": "No policy yet", "policies": []},
            {"id": "OK.1", "title": "Clean", "policies": ["p-ok"]},
        ],
    }
    fdir = _write_framework(tmp_path, spec)
    with session_scope() as s:
        ok = _make_policy(s, "p-ok")
        _seed(s, execution_id="e1", policy_id=ok, subscription_id="sub-a", resources_matched=0)

    with session_scope() as s:
        posture = fw.framework_posture(s, "fixture-fw", frameworks_dir=fdir)

    by_id = {c["id"]: c for c in posture["controls"]}
    # A gap is a gap — never compliant — even alongside a passing sibling control.
    assert by_id["GAP.1"]["status"] == "gap"
    assert by_id["GAP.1"]["gap"] is True
    assert by_id["GAP.1"]["status"] != "compliant"
    assert by_id["OK.1"]["status"] == "compliant"
    assert posture["totals"]["gap"] == 1


def test_control_not_evaluated_when_mapped_policy_never_ran(db, tmp_path) -> None:
    # Mapped but with no execution yet: honest "not_evaluated" — not compliant, not a gap.
    spec = {
        "name": "fixture-fw",
        "version": "1.0.0",
        "title": "Fixture",
        "controls": [{"id": "A.1", "title": "Never ran", "policies": ["p-never"]}],
    }
    fdir = _write_framework(tmp_path, spec)
    with session_scope() as s:
        _make_policy(s, "p-never")  # exists, but no execution seeded

    with session_scope() as s:
        posture = fw.framework_posture(s, "fixture-fw", frameworks_dir=fdir)

    control = posture["controls"][0]
    assert control["status"] == "not_evaluated"
    assert control["gap"] is False


def test_framework_posture_unknown_returns_none(db) -> None:
    with session_scope() as s:
        assert fw.framework_posture(s, "nope") is None


# --------------------------------------------------------------------------- #
# 3. Evidence bundle
# --------------------------------------------------------------------------- #
def test_evidence_bundle_includes_matches_and_timestamps(db, tmp_path) -> None:
    spec = {
        "name": "fixture-fw",
        "version": "2.0.0",
        "title": "Fixture",
        "controls": [{"id": "A.1", "title": "Flagged", "policies": ["p-flagged"]}],
    }
    fdir = _write_framework(tmp_path, spec)
    with session_scope() as s:
        pid = _make_policy(s, "p-flagged")
        _seed(
            s,
            execution_id="e1",
            policy_id=pid,
            subscription_id="sub-a",
            resources_matched=1,
            matches=[PolicyMatch(resource_id="/sub/rg/vm-1", resource_type="azure.vm")],
        )

    with session_scope() as s:
        bundle = fw.evidence_bundle(s, "fixture-fw", frameworks_dir=fdir)

    assert bundle["generated_at"]  # the bundle is timestamped
    control = bundle["controls"][0]
    policy = control["policies"][0]
    assert policy["policy_name"] == "p-flagged"
    assert policy["last_execution_at"]  # the run's timestamp is carried
    ids = [m["resource_id"] for m in policy["matches"]]
    assert "/sub/rg/vm-1" in ids


def test_evidence_reconciles_with_posture(db, tmp_path) -> None:
    spec = {
        "name": "fixture-fw",
        "version": "1.0.0",
        "title": "Fixture",
        "controls": [
            {"id": "C.OK", "title": "Clean", "policies": ["p-ok"]},
            {"id": "C.BAD", "title": "Fails", "policies": ["p-bad"]},
            {"id": "C.GAP", "title": "Gap", "policies": []},
        ],
    }
    fdir = _write_framework(tmp_path, spec)
    with session_scope() as s:
        ok = _make_policy(s, "p-ok")
        bad = _make_policy(s, "p-bad")
        _seed(s, execution_id="e1", policy_id=ok, subscription_id="sub-a", resources_matched=0)
        _seed(s, execution_id="e2", policy_id=bad, subscription_id="sub-a", resources_matched=2)

    with session_scope() as s:
        posture = fw.framework_posture(s, "fixture-fw", frameworks_dir=fdir)
        bundle = fw.evidence_bundle(s, "fixture-fw", frameworks_dir=fdir)

    posture_status = {c["id"]: c["status"] for c in posture["controls"]}
    evidence_status = {c["id"]: c["status"] for c in bundle["controls"]}
    assert evidence_status == posture_status
    assert posture_status == {"C.OK": "compliant", "C.BAD": "non_compliant", "C.GAP": "gap"}


def test_evidence_rows_flatten_for_export(db, tmp_path) -> None:
    spec = {
        "name": "fixture-fw",
        "version": "1.0.0",
        "title": "Fixture",
        "controls": [
            {"id": "C.OK", "title": "Clean", "policies": ["p-ok"]},
            {"id": "C.GAP", "title": "Gap", "policies": []},
        ],
    }
    fdir = _write_framework(tmp_path, spec)
    with session_scope() as s:
        ok = _make_policy(s, "p-ok")
        _seed(s, execution_id="e1", policy_id=ok, subscription_id="sub-a", resources_matched=0)

    with session_scope() as s:
        rows = fw.evidence_rows(s, "fixture-fw", frameworks_dir=fdir)

    # Every row carries the export columns; the gap control still emits a (flagged) row.
    assert rows
    for row in rows:
        assert set(fw.EVIDENCE_COLUMNS) <= set(row)
    gap_rows = [r for r in rows if r["control_id"] == "C.GAP"]
    assert len(gap_rows) == 1
    assert gap_rows[0]["is_gap"] is True
    assert gap_rows[0]["control_status"] == "gap"
    ok_rows = [r for r in rows if r["control_id"] == "C.OK"]
    assert ok_rows and ok_rows[0]["policy_name"] == "p-ok"


# --------------------------------------------------------------------------- #
# 4. Install / version via the pack registry
# --------------------------------------------------------------------------- #
def test_framework_installs_via_pack_registry(db) -> None:
    shipped = fw.load_framework("soc2")
    report = packs.install_framework("soc2")
    assert report["ok"] is True
    assert report["version"] == str(shipped["version"])

    with session_scope() as s:
        installed = repo.get_installed_framework(s, "soc2")
        names = {f["name"] for f in repo.list_installed_frameworks(s)}

    assert installed is not None
    assert installed["version"] == str(shipped["version"])
    assert "soc2" in names
    # Control mappings are persisted (drives the Grafana per-framework panel).
    assert installed["control_count"] == len(shipped["controls"])


def test_registry_framework_discovery_helpers() -> None:
    # The registry is the discovery entry point (loads overlays like packs).
    assert {f["name"] for f in packs.list_frameworks()} >= set(SHIPPED)
    assert packs.get_framework("soc2")["name"] == "soc2"
    assert packs.get_framework("does-not-exist") is None


def test_install_unknown_framework_reports_error(db) -> None:
    report = packs.install_framework("does-not-exist")
    assert report["ok"] is False
    assert report["error"]


def test_reinstall_framework_is_idempotent(db) -> None:
    packs.install_framework("pci")
    packs.install_framework("pci")  # second install must not duplicate
    with session_scope() as s:
        assert sum(1 for f in repo.list_installed_frameworks(s) if f["name"] == "pci") == 1


# --------------------------------------------------------------------------- #
# 5. Repo helper — per-policy posture across subscriptions
# --------------------------------------------------------------------------- #
def test_policy_posture_by_name_rolls_across_subscriptions(db) -> None:
    with session_scope() as s:
        pid = _make_policy(s, "p1")
        _seed(s, execution_id="e1", policy_id=pid, subscription_id="sub-a", resources_matched=0)
        _seed(s, execution_id="e2", policy_id=pid, subscription_id="sub-b", resources_matched=3)

    with session_scope() as s:
        by_name = {r["policy_name"]: r for r in repo.policy_posture_by_name(s)}

    assert by_name["p1"]["non_compliant"] == 1
    assert by_name["p1"]["resources_matched"] == 3
    assert by_name["p1"]["evaluated"] == 2


# --------------------------------------------------------------------------- #
# 6. API endpoints
# --------------------------------------------------------------------------- #
def test_frameworks_api_lists_shipped(db) -> None:
    body = TestClient(app).get("/api/governance/frameworks").json()
    names = {f["name"] for f in body}
    assert set(SHIPPED) <= names


def test_framework_posture_api(db) -> None:
    # Seed the first mapped control of soc2 so a real control reports compliant.
    soc2 = fw.load_framework("soc2")
    mapped = next(c for c in soc2["controls"] if c["policies"])
    with session_scope() as s:
        for i, name in enumerate(mapped["policies"]):
            pid = _make_policy(s, name)
            _seed(
                s, execution_id=f"e{i}", policy_id=pid, subscription_id="sub-a", resources_matched=0
            )

    body = TestClient(app).get("/api/governance/frameworks/soc2/posture").json()
    by_id = {c["id"]: c for c in body["controls"]}
    assert by_id[mapped["id"]]["status"] == "compliant"
    assert body["totals"]["controls"] == len(soc2["controls"])


def test_framework_posture_api_unknown_404(db) -> None:
    assert TestClient(app).get("/api/governance/frameworks/nope/posture").status_code == 404


def test_framework_evidence_api_json(db) -> None:
    resp = TestClient(app).get("/api/governance/frameworks/soc2/evidence?format=json")
    assert resp.status_code == 200
    payload = resp.json()
    assert isinstance(payload, list)
    # Every shipped control id appears in the streamed evidence.
    soc2 = fw.load_framework("soc2")
    control_ids = {r["control_id"] for r in payload}
    assert {c["id"] for c in soc2["controls"]} <= control_ids


def test_framework_evidence_api_csv(db) -> None:
    resp = TestClient(app).get("/api/governance/frameworks/soc2/evidence?format=csv")
    assert resp.status_code == 200
    text = resp.text
    header = text.splitlines()[0]
    for col in fw.EVIDENCE_COLUMNS:
        assert col in header


def test_framework_evidence_api_bad_format_400(db) -> None:
    assert (
        TestClient(app).get("/api/governance/frameworks/soc2/evidence?format=xml").status_code
        == 400
    )


def test_framework_evidence_api_unknown_404(db) -> None:
    assert TestClient(app).get("/api/governance/frameworks/nope/evidence").status_code == 404


def test_framework_install_api(db) -> None:
    resp = TestClient(app).post("/api/governance/frameworks/iso27001/install")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    with session_scope() as s:
        assert repo.get_installed_framework(s, "iso27001") is not None


def test_framework_install_api_unknown_404(db) -> None:
    assert TestClient(app).post("/api/governance/frameworks/nope/install").status_code == 404


# --------------------------------------------------------------------------- #
# 7. Loader / evidence edge cases
# --------------------------------------------------------------------------- #
def test_load_framework_missing_dir_returns_none(tmp_path) -> None:
    assert fw.load_framework("soc2", frameworks_dir=tmp_path / "nope") is None


def test_list_frameworks_missing_dir_is_empty(tmp_path) -> None:
    assert fw.list_frameworks(frameworks_dir=tmp_path / "nope") == []


def test_list_frameworks_skips_non_yaml(tmp_path) -> None:
    d = tmp_path / "frameworks"
    d.mkdir()
    (d / "notes.txt").write_text("not a framework")
    (d / "fx.yaml").write_text(
        yaml.safe_dump({"name": "fx", "version": "1", "controls": [{"id": "A", "policies": []}]})
    )
    assert {f["name"] for f in fw.list_frameworks(frameworks_dir=d)} == {"fx"}


def test_evidence_bundle_unknown_returns_none(db) -> None:
    with session_scope() as s:
        assert fw.evidence_bundle(s, "nope") is None


def test_evidence_rows_unknown_returns_empty(db) -> None:
    with session_scope() as s:
        assert fw.evidence_rows(s, "nope") == []


def test_evidence_generated_at_override_and_unmapped_policy(db, tmp_path) -> None:
    # A control mapping to a policy with no DB row → listed with empty matches; and
    # an explicit generated_at (datetime and string) is carried through verbatim.
    spec = {
        "name": "fixture-fw",
        "version": "1.0.0",
        "title": "F",
        "controls": [{"id": "A.1", "title": "t", "policies": ["p-missing"]}],
    }
    fdir = _write_framework(tmp_path, spec)
    stamp = datetime(2026, 7, 24, tzinfo=UTC)
    with session_scope() as s:
        bundle = fw.evidence_bundle(s, "fixture-fw", frameworks_dir=fdir, generated_at=stamp)
        rows = fw.evidence_rows(
            s, "fixture-fw", frameworks_dir=fdir, generated_at="2026-07-24T00:00:00Z"
        )
    assert bundle["generated_at"] == stamp.isoformat()
    assert bundle["controls"][0]["policies"][0]["matches"] == []
    assert rows[0]["generated_at"] == "2026-07-24T00:00:00Z"
