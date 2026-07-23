"use client";

import { useCallback, useEffect, useState } from "react";
import { apiGet, apiPost, money, shortId } from "../lib/api";
import type { CommitmentData, Recommendation } from "../lib/api";
import { resourceTypeFromId } from "../lib/format";

export default function Recommendations() {
  const [recs, setRecs] = useState<Recommendation[]>([]);
  const [commit, setCommit] = useState<CommitmentData | null>(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState<number | null>(null);
  const [msg, setMsg] = useState<string>("");

  const load = useCallback(async () => {
    try {
      setRecs(await apiGet<Recommendation[]>("/api/recommendations"));
    } catch (e) {
      setErr(String(e));
    }
    // Commitment coverage is a separate, RBAC-guarded read (M14.1): tolerate a
    // failure (e.g. 401 under RBAC) by simply hiding the panel, never blocking recs.
    try {
      setCommit(await apiGet<CommitmentData>("/api/finops/commitments"));
    } catch {
      setCommit(null);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function decide(id: number, decision: "approve" | "reject") {
    setBusy(id);
    try {
      await apiPost(`/api/recommendations/${id}/decision`, { decision, actor: "ui" });
      await load();
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(null);
    }
  }

  async function remediate(id: number) {
    setBusy(id);
    try {
      const res = await apiPost<any>(
        `/api/recommendations/${id}/remediate?dry_run=true&actor=ui`,
      );
      setMsg(`Remediation (${res.status}): ${res.message || res.action_type}`);
      await load();
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(null);
    }
  }

  const total = recs.reduce((s, r) => s + (r.est_monthly_savings || 0), 0);
  // Recs from a run share one billing currency; fall back to USD only when empty.
  const currency = recs.find((r) => r.currency)?.currency;
  const coverage = commit?.coverage ?? [];
  const commitments = commit?.commitments ?? [];
  const pct = (v?: number | null) => (v == null ? "—" : `${Math.round(v)}%`);

  return (
    <>
      <h1>Recommendations</h1>
      <p className="sub">
        Review and approve/reject. Approved items become eligible for guarded remediation
        (Phase 5). Total potential: <strong>{money(total, currency)}</strong>/mo.
      </p>
      {err && <div className="err">{err}</div>}
      {msg && <div className="summary" style={{ marginBottom: 14 }}>{msg}</div>}

      {(coverage.length > 0 || commitments.length > 0) && (
        <section style={{ marginBottom: 24 }}>
          <h2 style={{ fontSize: 18 }}>Commitment coverage</h2>
          <p className="sub">
            Reservation / Savings-Plan coverage of steady-state usage, and the utilization of
            existing commitments. Purchase &amp; waste recommendations appear in the table below
            (category <code>commitment</code>). Savings are caveated estimates.
          </p>
          {coverage.length > 0 && (
            <table>
              <thead>
                <tr>
                  <th>SKU family</th>
                  <th>Region</th>
                  <th className="num">Eligible/mo</th>
                  <th className="num">Committed/mo</th>
                  <th className="num">Coverage</th>
                  <th className="num">Utilization</th>
                </tr>
              </thead>
              <tbody>
                {coverage.map((c) => (
                  <tr key={`${c.sku_family}/${c.region}`}>
                    <td>{c.sku_family}</td>
                    <td className="muted">{c.region}</td>
                    <td className="num">{money(c.eligible_monthly, c.currency)}</td>
                    <td className="num">{money(c.committed_monthly, c.currency)}</td>
                    <td className="num">{pct(c.coverage_pct)}</td>
                    <td className="num">{pct(c.utilization_pct)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {commitments.length > 0 && (
            <table style={{ marginTop: 12 }}>
              <thead>
                <tr>
                  <th>Commitment</th>
                  <th>Kind</th>
                  <th>Family</th>
                  <th>Term</th>
                  <th className="num">Utilization</th>
                  <th>Expires</th>
                </tr>
              </thead>
              <tbody>
                {commitments.map((c) => (
                  <tr key={c.commitment_id}>
                    <td title={c.commitment_id}>{c.display_name || shortId(c.commitment_id)}</td>
                    <td className="muted">{c.kind}</td>
                    <td>{c.sku_family || "—"}</td>
                    <td>{c.term}</td>
                    <td className="num">{pct(c.utilization_pct)}</td>
                    <td className="muted">{c.expiry_date || "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
      )}

      <table>
        <thead>
          <tr>
            <th>#</th><th>Resource</th><th>Action</th><th>Target</th><th>Risk</th>
            <th className="num">Conf.</th><th className="num">Savings/mo</th>
            <th>Source</th><th>Status</th><th></th>
          </tr>
        </thead>
        <tbody>
          {recs.map((r) => (
            <tr key={r.id}>
              <td className="muted">{r.priority}</td>
              <td title={r.rationale || ""}>
                {shortId(r.resource_id)}
                <div className="muted" style={{ fontSize: 12 }}>
                  {resourceTypeFromId(r.resource_id)} · {r.category}
                </div>
              </td>
              <td>{r.action}</td>
              <td>{r.recommended_sku || "—"}</td>
              <td><span className={`badge ${r.risk}`}>{r.risk}</span></td>
              <td className="num">{Math.round((r.confidence || 0) * 100)}%</td>
              <td className="num">{money(r.est_monthly_savings, r.currency)}</td>
              <td className="muted">{r.source}</td>
              <td><span className={`badge ${r.status}`}>{r.status}</span></td>
              <td>
                <div className="row-actions">
                  <button
                    className="approve"
                    disabled={busy === r.id || r.status === "approved"}
                    onClick={() => decide(r.id, "approve")}
                  >
                    Approve
                  </button>
                  <button
                    className="reject"
                    disabled={busy === r.id || r.status === "rejected"}
                    onClick={() => decide(r.id, "reject")}
                  >
                    Reject
                  </button>
                  {r.status === "approved" && r.category !== "commitment" && (
                    <button
                      className="primary"
                      disabled={busy === r.id}
                      onClick={() => remediate(r.id)}
                    >
                      Remediate (dry-run)
                    </button>
                  )}
                </div>
              </td>
            </tr>
          ))}
          {recs.length === 0 && !err && (
            <tr>
              <td colSpan={10} className="muted">
                No recommendations. These span right-sizing (VMs with utilization
                metrics), idle/orphaned resources (disks, public IPs, App Service
                plans, Bastion, storage, container registries), commitment coverage
                (Reservations / Savings Plans) and Azure Advisor — the latest run
                found none. Trigger a run from the Runs page if you haven’t yet.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </>
  );
}
