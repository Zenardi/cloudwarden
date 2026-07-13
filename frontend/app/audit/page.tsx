"use client";

import { useCallback, useEffect, useState } from "react";
import { AuditEntry, listAudit } from "../lib/api";

/** Format an ISO timestamp compactly; fall back to the raw value. */
function when(iso?: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return isNaN(d.getTime()) ? iso : d.toLocaleString();
}

/** A short, readable summary of the before→after payloads for one entry. */
function summarize(entry: AuditEntry): string {
  const keys = new Set([...Object.keys(entry.before || {}), ...Object.keys(entry.after || {})]);
  const changed = [...keys].filter((k) => {
    const a = JSON.stringify((entry.before || {})[k]);
    const b = JSON.stringify((entry.after || {})[k]);
    return a !== b;
  });
  if (entry.action.endsWith(".create")) return "created";
  if (entry.action.endsWith(".delete")) return "deleted";
  return changed.length ? `changed: ${changed.join(", ")}` : "no field change";
}

export default function AuditPage() {
  const [entries, setEntries] = useState<AuditEntry[]>([]);
  const [actor, setActor] = useState("");
  const [targetType, setTargetType] = useState("");
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    setErr("");
    try {
      setEntries(await listAudit({ actor: actor.trim(), target_type: targetType.trim() }));
    } catch (e) {
      setErr(String(e));
    } finally {
      setLoading(false);
    }
  }, [actor, targetType]);

  useEffect(() => {
    load();
  }, [load]);

  return (
    <section>
      <h1>Audit log</h1>
      <p className="muted">
        Append-only trail of every mutating governance action — who did what, to which
        target, and the before/after state. Newest first.
      </p>

      <div className="field" style={{ display: "flex", gap: 8, flexWrap: "wrap", marginBottom: 12 }}>
        <input
          placeholder="Filter by actor"
          value={actor}
          onChange={(e) => setActor(e.target.value)}
        />
        <input
          placeholder="Filter by target type (e.g. policy)"
          value={targetType}
          onChange={(e) => setTargetType(e.target.value)}
        />
        <button onClick={load} disabled={loading}>
          {loading ? "Loading…" : "Apply filters"}
        </button>
      </div>

      {err ? <p className="err">{err}</p> : null}

      <table>
        <thead>
          <tr>
            <th>When</th>
            <th>Actor</th>
            <th>Action</th>
            <th>Target</th>
            <th>Change</th>
          </tr>
        </thead>
        <tbody>
          {entries.map((e) => (
            <tr key={e.id}>
              <td>{when(e.at)}</td>
              <td>{e.actor ?? <span className="muted">anonymous</span>}</td>
              <td>
                <span className="chip">{e.action}</span>
              </td>
              <td>
                {e.target_type}
                {e.target_id ? ` #${e.target_id}` : ""}
              </td>
              <td className="muted">{summarize(e)}</td>
            </tr>
          ))}
          {!entries.length && !loading ? (
            <tr>
              <td colSpan={5} className="muted">
                No audit entries.
              </td>
            </tr>
          ) : null}
        </tbody>
      </table>
    </section>
  );
}
