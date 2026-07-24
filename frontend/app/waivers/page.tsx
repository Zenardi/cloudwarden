"use client";

import { useCallback, useEffect, useState } from "react";
import {
  apiGet,
  approveWaiver,
  listWaivers,
  Policy,
  rejectWaiver,
  requestWaiver,
  Waiver,
  WAIVER_SCOPES,
  WaiverScope,
} from "../lib/api";

/** State → badge colour (active = granted, pending = awaiting approval, else muted). */
function stateColor(state: string): string {
  if (state === "active") return "#30a46c";
  if (state === "pending") return "#f5a524";
  if (state === "rejected") return "#e5484d";
  return "#8b8d98"; // expired
}

/** ``YYYY-MM-DD`` a fortnight out — the default expiry for a new waiver. */
function defaultExpiry(): string {
  const d = new Date();
  d.setDate(d.getDate() + 14);
  return d.toISOString().slice(0, 10);
}

export default function Waivers() {
  const [waivers, setWaivers] = useState<Waiver[]>([]);
  const [policies, setPolicies] = useState<Policy[]>([]);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState("");

  // request form
  const [policyId, setPolicyId] = useState<string>("");
  const [scopeType, setScopeType] = useState<WaiverScope>("policy");
  const [scopeValue, setScopeValue] = useState("");
  const [justification, setJustification] = useState("");
  const [expires, setExpires] = useState<string>(defaultExpiry());

  const load = useCallback(async () => {
    try {
      const [{ waivers: ws }, ps] = await Promise.all([
        listWaivers(),
        apiGet<Policy[]>("/api/policies"),
      ]);
      setWaivers(ws);
      setPolicies(ps);
    } catch (e) {
      setErr(String(e));
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function act(key: string, fn: () => Promise<unknown>) {
    setBusy(key);
    setErr("");
    try {
      await fn();
      await load();
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy("");
    }
  }

  function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!policyId) {
      setErr("select a policy to waive");
      return;
    }
    // Send expiry at end of day UTC so a same-day expiry is still in the future.
    const expiresAt = new Date(`${expires}T23:59:59Z`).toISOString();
    act("create", async () => {
      await requestWaiver({
        policy_id: Number(policyId),
        justification,
        expires_at: expiresAt,
        scope_type: scopeType,
        // scope_value is ignored for a whole-policy waiver.
        scope_value: scopeType === "policy" ? null : scopeValue || null,
      });
      setJustification("");
      setScopeValue("");
    });
  }

  const policyName = (id: number) => policies.find((p) => p.id === id)?.name ?? `#${id}`;

  return (
    <div className="page">
      <header className="page-head">
        <h1>Waivers</h1>
        <p className="muted">
          Scoped, justified, approved, <strong>expiring</strong> exceptions to a policy. A matched
          resource covered by an active waiver is recorded as <strong>waived</strong> (never
          enforced); when the waiver expires the finding re-surfaces automatically.
        </p>
      </header>

      {err && <div className="err">{err}</div>}

      <section className="card">
        <h2>Request a waiver</h2>
        <form className="form-grid" onSubmit={submit}>
          <label>
            Policy
            <select value={policyId} onChange={(e) => setPolicyId(e.target.value)} required>
              <option value="">Select a policy…</option>
              {policies.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            </select>
          </label>
          <label>
            Scope
            <select value={scopeType} onChange={(e) => setScopeType(e.target.value as WaiverScope)}>
              {WAIVER_SCOPES.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          </label>
          {scopeType !== "policy" && (
            <label>
              {scopeType === "tag" ? "Tag (key=value)" : scopeType.replace("_", " ")}
              <input
                value={scopeValue}
                onChange={(e) => setScopeValue(e.target.value)}
                placeholder={scopeType === "tag" ? "env=sandbox" : "resource id / group name"}
                required
              />
            </label>
          )}
          <label>
            Expires
            <input
              type="date"
              value={expires}
              onChange={(e) => setExpires(e.target.value)}
              required
            />
          </label>
          <label className="wide">
            Justification
            <input
              value={justification}
              onChange={(e) => setJustification(e.target.value)}
              placeholder="Why this exception is needed"
              required
            />
          </label>
          <button type="submit" className="primary" disabled={busy === "create"}>
            {busy === "create" ? "Requesting…" : "Request waiver"}
          </button>
        </form>
      </section>

      <section className="card">
        <h2>Waivers ({waivers.length})</h2>
        {waivers.length === 0 ? (
          <p className="muted">No waivers yet.</p>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Policy</th>
                <th>Scope</th>
                <th>Justification</th>
                <th>State</th>
                <th>Expires</th>
                <th>Requester</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {waivers.map((w) => (
                <tr key={w.id}>
                  <td>{policyName(w.policy_id)}</td>
                  <td>
                    {w.scope_type}
                    {w.scope_value ? `: ${w.scope_value}` : ""}
                  </td>
                  <td>{w.justification}</td>
                  <td>
                    <span className="badge" style={{ background: stateColor(w.state) }}>
                      {w.state}
                    </span>
                  </td>
                  <td>{w.expires_at ? w.expires_at.slice(0, 10) : "—"}</td>
                  <td>{w.requester ?? "—"}</td>
                  <td className="row-actions">
                    {w.state === "pending" && (
                      <>
                        <button
                          className="primary"
                          disabled={busy === `a-${w.id}`}
                          onClick={() => act(`a-${w.id}`, () => approveWaiver(w.id))}
                        >
                          Approve
                        </button>
                        <button
                          className="reject"
                          disabled={busy === `r-${w.id}`}
                          onClick={() => act(`r-${w.id}`, () => rejectWaiver(w.id))}
                        >
                          Reject
                        </button>
                      </>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </div>
  );
}
