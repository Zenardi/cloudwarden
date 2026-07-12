"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { apiGet, API_BASE, GRAFANA_BASE, money } from "./lib/api";
import type { AISummary } from "./lib/api";

export default function Overview() {
  const [summary, setSummary] = useState<AISummary | null>(null);
  const [total, setTotal] = useState<number>(0);
  const [run, setRun] = useState<any>(null);
  const [err, setErr] = useState<string>("");

  useEffect(() => {
    (async () => {
      try {
        const [s, c, r] = await Promise.all([
          apiGet<AISummary | null>("/api/summary").catch(() => null),
          apiGet<any>("/api/costs/summary").catch(() => ({ total: 0 })),
          apiGet<any>("/api/runs/latest").catch(() => null),
        ]);
        setSummary(s);
        setTotal(c?.total ?? 0);
        setRun(r);
      } catch (e) {
        setErr(String(e));
      }
    })();
  }, []);

  return (
    <>
      <h1>Overview</h1>
      <p className="sub">Azure cost, utilization, and optimization at a glance.</p>
      {err && (
        <div className="err">
          Cannot reach the API at {API_BASE} — is the backend running? ({err})
        </div>
      )}

      <div className="cards">
        <div className="card">
          <div className="label">Cost (30d, amortized)</div>
          <div className="value">{money(total)}</div>
        </div>
        <div className="card">
          <div className="label">Potential monthly savings</div>
          <div className="value green">{money(summary?.total_potential_savings ?? 0)}</div>
        </div>
        <div className="card">
          <div className="label">Last run</div>
          <div className="value" style={{ fontSize: 18 }}>{run?.status ?? "—"}</div>
          <div className="muted">{run?.run_id ?? "no runs yet"}</div>
        </div>
      </div>

      <h2>AI executive summary</h2>
      <div className="summary">
        {summary?.executive_summary || "No summary yet — trigger a run from the Runs page."}
        {summary?.provider && (
          <div className="muted" style={{ marginTop: 8 }}>
            via {summary.provider}/{summary.model}
          </div>
        )}
      </div>

      <h2>Dashboards</h2>
      <div className="links">
        <a href={`${GRAFANA_BASE}/d/finops-cost`} target="_blank" rel="noreferrer">
          Grafana — Cost Overview ↗
        </a>
        <a href={`${GRAFANA_BASE}/d/finops-recs`} target="_blank" rel="noreferrer">
          Grafana — Recommendations ↗
        </a>
        <Link href="/recommendations">Review recommendations →</Link>
      </div>
    </>
  );
}
