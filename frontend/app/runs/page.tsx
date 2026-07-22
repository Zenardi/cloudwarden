"use client";

import { Fragment, useCallback, useEffect, useState } from "react";
import { apiGet, apiPost } from "../lib/api";

function ts(value?: string | null): string {
  if (!value) return "—";
  return value.replace("T", " ").slice(0, 19);
}

/** Elapsed wall-clock between start and finish, as "45s" / "1m 40s". */
function duration(start?: string | null, finish?: string | null): string {
  if (!start || !finish) return "—";
  const ms = new Date(finish).getTime() - new Date(start).getTime();
  if (!Number.isFinite(ms) || ms < 0) return "—";
  const secs = Math.round(ms / 1000);
  if (secs < 60) return `${secs}s`;
  return `${Math.floor(secs / 60)}m ${secs % 60}s`;
}

/** First non-empty line of a multi-line error, truncated for the table cell. */
function firstLine(text?: string | null, max = 70): string {
  if (!text) return "";
  const line = (text.split("\n").find((l) => l.trim()) ?? "").trim();
  return line.length > max ? `${line.slice(0, max - 1)}…` : line;
}

interface Run {
  run_id: string;
  status: string;
  mock?: boolean;
  started_at?: string | null;
  finished_at?: string | null;
  notes?: string | null;
  subscription_id?: string | null;
  provider_used?: string | null;
  model?: string | null;
  metric_lookback_days?: number | null;
  cost_lookback_days?: number | null;
}

export default function Runs() {
  const [runs, setRuns] = useState<Run[]>([]);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [expanded, setExpanded] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setRuns(await apiGet<Run[]>("/api/runs?limit=20"));
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
      await apiPost("/api/runs");
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
          {busy ? "Running…" : "Trigger run"}
        </button>
      </p>
      {err && <div className="err">{err}</div>}

      <table>
        <thead>
          <tr>
            <th>Run</th>
            <th>Status</th>
            <th>Mock</th>
            <th>Started</th>
            <th>Finished</th>
            <th>Duration</th>
            <th>Error</th>
          </tr>
        </thead>
        <tbody>
          {runs.map((r) => {
            const failed = r.status === "failed";
            const open = expanded === r.run_id;
            return (
              <Fragment key={r.run_id}>
                <tr
                  className="clickable"
                  onClick={() => setExpanded(open ? null : r.run_id)}
                >
                  <td>
                    {open ? "▾ " : "▸ "}
                    {r.run_id}
                  </td>
                  <td>
                    <span
                      className={`badge ${
                        r.status === "succeeded"
                          ? "approved"
                          : failed
                            ? "rejected"
                            : ""
                      }`}
                    >
                      {r.status}
                    </span>
                  </td>
                  <td>{r.mock ? "yes" : "no"}</td>
                  <td className="muted">{ts(r.started_at)}</td>
                  <td className="muted">{ts(r.finished_at)}</td>
                  <td className="muted">{duration(r.started_at, r.finished_at)}</td>
                  <td className="err-cell" title={failed ? r.notes ?? "" : ""}>
                    {failed ? firstLine(r.notes) || "—" : "—"}
                  </td>
                </tr>
                {open && (
                  <tr>
                    <td colSpan={7} className="exec-detail">
                      <table>
                        <tbody>
                          <tr>
                            <td className="muted">Run ID</td>
                            <td>{r.run_id}</td>
                          </tr>
                          <tr>
                            <td className="muted">Subscription</td>
                            <td>{r.subscription_id ?? "—"}</td>
                          </tr>
                          <tr>
                            <td className="muted">Status</td>
                            <td>
                              <span
                                className={`badge ${
                                  r.status === "succeeded"
                                    ? "approved"
                                    : failed
                                      ? "rejected"
                                      : ""
                                }`}
                              >
                                {r.status}
                              </span>
                            </td>
                          </tr>
                          <tr>
                            <td className="muted">Started</td>
                            <td>{ts(r.started_at)}</td>
                          </tr>
                          <tr>
                            <td className="muted">Finished</td>
                            <td>{ts(r.finished_at)}</td>
                          </tr>
                          <tr>
                            <td className="muted">Duration</td>
                            <td>{duration(r.started_at, r.finished_at)}</td>
                          </tr>
                          <tr>
                            <td className="muted">Mock</td>
                            <td>{r.mock ? "yes" : "no"}</td>
                          </tr>
                          <tr>
                            <td className="muted">Provider</td>
                            <td>{r.provider_used ?? "—"}</td>
                          </tr>
                          <tr>
                            <td className="muted">Model</td>
                            <td>{r.model ?? "—"}</td>
                          </tr>
                          <tr>
                            <td className="muted">Lookback (metric / cost)</td>
                            <td>
                              {r.metric_lookback_days ?? "—"} / {r.cost_lookback_days ?? "—"} days
                            </td>
                          </tr>
                        </tbody>
                      </table>
                      {r.notes && (
                        <div className="err" style={{ marginTop: 12 }}>
                          <pre className="run-error">{r.notes}</pre>
                        </div>
                      )}
                    </td>
                  </tr>
                )}
              </Fragment>
            );
          })}
          {runs.length === 0 && !err && (
            <tr>
              <td colSpan={7} className="muted">
                No runs yet.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </>
  );
}
