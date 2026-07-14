"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import {
  getGovernancePosture,
  getPolicyMatchedResources,
  MatchedResource,
  PostureProvider,
  PosturePolicy,
  PROVIDERS,
  shortId,
} from "../lib/api";

function ts(value?: string | null): string {
  if (!value) return "—";
  return value.replace("T", " ").slice(0, 19);
}

export default function Compliance() {
  const [provider, setProvider] = useState("all");
  const [policies, setPolicies] = useState<PosturePolicy[]>([]);
  const [byProvider, setByProvider] = useState<PostureProvider[]>([]);
  const [selected, setSelected] = useState<PosturePolicy | null>(null);
  const [resources, setResources] = useState<MatchedResource[]>([]);
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(true);
  const [drilling, setDrilling] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const posture = await getGovernancePosture(provider);
      // Non-compliant first, then most-evaluated — the investigation worklist.
      const rows = [...posture.by_policy].sort(
        (a, b) => b.non_compliant - a.non_compliant || b.violations - a.violations,
      );
      setPolicies(rows);
      setByProvider(posture.by_provider ?? []);
      setErr("");
    } catch (e) {
      setErr(String(e));
    } finally {
      setLoading(false);
    }
  }, [provider]);

  useEffect(() => {
    load();
  }, [load]);

  const drillInto = useCallback(async (p: PosturePolicy) => {
    setSelected(p);
    setDrilling(true);
    setResources([]);
    setErr("");
    try {
      setResources(await getPolicyMatchedResources(p.policy_id));
    } catch (e) {
      setErr(String(e));
    } finally {
      setDrilling(false);
    }
  }, []);

  return (
    <>
      <h1>Compliance</h1>
      <p className="sub">
        Drill from a <strong>policy</strong> into the <strong>resources it has flagged</strong>, then
        through to each resource&apos;s <strong>asset detail</strong> — investigate non-compliance à
        la Stacklet&apos;s compliance explorer. Counts come from the governance posture (latest
        execution per policy &amp; subscription).
      </p>

      <form className="history-controls" onSubmit={(e) => e.preventDefault()}>
        <div className="field">
          <label htmlFor="f-provider">Cloud</label>
          <select id="f-provider" value={provider} onChange={(e) => setProvider(e.target.value)}>
            <option value="all">All clouds</option>
            {PROVIDERS.map((p) => (
              <option key={p} value={p}>
                {p}
              </option>
            ))}
          </select>
        </div>
      </form>

      {byProvider.length > 0 && (
        <table style={{ marginBottom: "1rem" }}>
          <thead>
            <tr>
              <th>Cloud</th>
              <th>Compliant</th>
              <th>Non-compliant</th>
              <th>Violations</th>
              <th>Evaluated</th>
            </tr>
          </thead>
          <tbody>
            {byProvider.map((r) => (
              <tr key={r.provider}>
                <td>
                  <span className="badge">{r.provider}</span>
                </td>
                <td className="muted">{r.compliant}</td>
                <td>
                  <span className={r.non_compliant > 0 ? "badge rejected" : "badge"}>
                    {r.non_compliant}
                  </span>
                </td>
                <td className="muted">{r.violations}</td>
                <td className="muted">{r.evaluated}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {err && <div className="err">{err}</div>}

      <div className="form-grid" style={{ gridTemplateColumns: "minmax(0, 5fr) minmax(0, 7fr)" }}>
        {/* Policy list ----------------------------------------------------- */}
        <div className="field" style={{ minWidth: 0 }}>
          <h2 style={{ marginTop: 0 }}>Policies</h2>
          <table>
            <thead>
              <tr>
                <th>Policy</th>
                <th>Non-compliant</th>
                <th>Evaluated</th>
              </tr>
            </thead>
            <tbody>
              {policies.map((p) => (
                <tr
                  key={p.policy_id}
                  onClick={() => drillInto(p)}
                  style={{
                    cursor: "pointer",
                    background:
                      selected?.policy_id === p.policy_id ? "rgba(255,255,255,.05)" : undefined,
                  }}
                >
                  <td>{p.policy_name}</td>
                  <td>
                    <span className={p.violations > 0 ? "badge rejected" : "badge"}>
                      {p.violations}
                    </span>
                  </td>
                  <td className="muted">{p.evaluated}</td>
                </tr>
              ))}
              {policies.length === 0 && !loading && !err && (
                <tr>
                  <td colSpan={3} className="muted">
                    No policies evaluated yet. Run a policy or binding to populate posture.
                  </td>
                </tr>
              )}
              {loading && (
                <tr>
                  <td colSpan={3} className="muted">
                    Loading…
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>

        {/* Drill-down ------------------------------------------------------ */}
        <div className="field" style={{ minWidth: 0 }}>
          <h2 style={{ marginTop: 0 }}>
            {selected ? `Flagged resources — ${selected.policy_name}` : "Flagged resources"}
          </h2>
          {!selected && <p className="muted">Select a policy to list the resources it flagged.</p>}
          {selected && drilling && <p className="muted">Loading resources…</p>}
          {selected && !drilling && resources.length === 0 && !err && (
            <p className="muted">No non-compliant resources for this policy — compliant. 🎉</p>
          )}
          {selected && resources.length > 0 && (
            <table>
              <thead>
                <tr>
                  <th>Resource</th>
                  <th>Type</th>
                  <th>Subscription</th>
                  <th>Matched</th>
                </tr>
              </thead>
              <tbody>
                {resources.map((r) => (
                  <tr key={r.resource_id}>
                    <td>
                      <Link href={`/assets${r.resource_id}`} title={r.resource_id}>
                        {shortId(r.resource_id)}
                      </Link>
                    </td>
                    <td className="muted">{r.resource_type || "—"}</td>
                    <td className="muted">{r.subscription_id || "—"}</td>
                    <td className="muted">{ts(r.matched_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>
    </>
  );
}
