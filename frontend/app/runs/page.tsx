"use client";

import { useCallback, useEffect, useState } from "react";
import { apiGet, apiPost } from "../lib/api";

function ts(value?: string | null): string {
  if (!value) return "—";
  return value.replace("T", " ").slice(0, 19);
}

export default function Runs() {
  const [runs, setRuns] = useState<any[]>([]);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      setRuns(await apiGet("/api/runs?limit=20"));
    } catch (e) {
      setErr(String(e));
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function trigger() {
    setBusy(true);
    try {
      await apiPost("/api/runs?mock=true");
      await load();
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <h1>Runs</h1>
      <p className="sub">Pipeline runs (collect → analyze → recommend → store).</p>
      <p>
        <button className="primary" onClick={trigger} disabled={busy}>
          {busy ? "Running…" : "Trigger run (mock)"}
        </button>
      </p>
      {err && <div className="err">{err}</div>}

      <table>
        <thead>
          <tr><th>Run</th><th>Status</th><th>Mock</th><th>Started</th><th>Finished</th></tr>
        </thead>
        <tbody>
          {runs.map((r, i) => (
            <tr key={i}>
              <td>{r.run_id}</td>
              <td>
                <span
                  className={`badge ${
                    r.status === "succeeded" ? "approved" : r.status === "failed" ? "rejected" : ""
                  }`}
                >
                  {r.status}
                </span>
              </td>
              <td>{r.mock ? "yes" : "no"}</td>
              <td className="muted">{ts(r.started_at)}</td>
              <td className="muted">{ts(r.finished_at)}</td>
            </tr>
          ))}
          {runs.length === 0 && !err && (
            <tr><td colSpan={5} className="muted">No runs yet.</td></tr>
          )}
        </tbody>
      </table>
    </>
  );
}
