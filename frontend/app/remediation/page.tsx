"use client";

import { useEffect, useState } from "react";
import { apiGet, shortId } from "../lib/api";

function ts(value?: string | null): string {
  return value ? value.replace("T", " ").slice(0, 19) : "—";
}

export default function Remediation() {
  const [actions, setActions] = useState<any[]>([]);
  const [source, setSource] = useState("");
  const [err, setErr] = useState("");

  useEffect(() => {
    const qs = source ? `&source=${encodeURIComponent(source)}` : "";
    setErr("");
    apiGet<any[]>(`/api/remediation?limit=100${qs}`)
      .then(setActions)
      .catch((e) => setErr(String(e)));
  }, [source]);

  return (
    <>
      <h1>Remediation audit</h1>
      <p className="sub">
        Every remediation attempt is recorded here — FinOps recommendations and policy-driven
        actions alike. Dry-run is the default; real Azure writes require{" "}
        <code>REMEDIATION_ENABLED=true</code> plus the write service principal, and only for
        allow-listed resource groups (resources tagged <code>finops:exclude</code> or{" "}
        <code>custodian:exclude</code> are never touched).
      </p>

      <div className="history-controls">
        <div className="field">
          <label htmlFor="f-source">Source</label>
          <select id="f-source" value={source} onChange={(e) => setSource(e.target.value)}>
            <option value="">All sources</option>
            <option value="recommendation">recommendation</option>
            <option value="policy">policy</option>
            <option value="binding">binding</option>
          </select>
        </div>
      </div>

      {err && <div className="err">{err}</div>}
      <table>
        <thead>
          <tr>
            <th>When</th><th>Source</th><th>Action</th><th>Resource</th><th>Dry-run</th>
            <th>Status</th><th>Detail</th>
          </tr>
        </thead>
        <tbody>
          {actions.map((a, i) => (
            <tr key={i}>
              <td className="muted">{ts(a.requested_at)}</td>
              <td><span className="badge">{a.source || "—"}</span></td>
              <td>{a.action_type}</td>
              <td>{shortId(a.resource_id || "")}</td>
              <td>{a.dry_run ? "yes" : "no"}</td>
              <td>
                <span
                  className={`badge ${
                    a.status === "executed"
                      ? "approved"
                      : a.status === "failed" || a.status === "blocked"
                        ? "rejected"
                        : ""
                  }`}
                >
                  {a.status}
                </span>
              </td>
              <td className="muted">{a.error || ""}</td>
            </tr>
          ))}
          {actions.length === 0 && !err && (
            <tr>
              <td colSpan={7} className="muted">
                {source ? `No ${source}-sourced remediation attempts yet.` : "No remediation attempts yet."}
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </>
  );
}
