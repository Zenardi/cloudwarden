"use client";

import { Fragment, useCallback, useEffect, useState } from "react";
import {
  apiGet,
  Policy,
  PolicyExecution,
  PolicyMatch,
  shortId,
  Subscription,
} from "../lib/api";

function ts(value?: string | null): string {
  if (!value) return "—";
  return value.replace("T", " ").slice(0, 19);
}

function statusClass(status: string): string {
  if (status === "succeeded") return "approved";
  if (status === "failed") return "rejected";
  return "";
}

export default function Executions() {
  const [execs, setExecs] = useState<PolicyExecution[]>([]);
  const [policies, setPolicies] = useState<Policy[]>([]);
  const [subs, setSubs] = useState<Subscription[]>([]);
  const [policyId, setPolicyId] = useState("");
  const [subId, setSubId] = useState("");
  const [status, setStatus] = useState("");
  const [expanded, setExpanded] = useState<string | null>(null);
  const [matches, setMatches] = useState<Record<string, PolicyMatch[]>>({});
  const [err, setErr] = useState("");

  const load = useCallback(async () => {
    const params = new URLSearchParams();
    if (policyId) params.set("policy_id", policyId);
    if (subId) params.set("subscription_id", subId);
    if (status) params.set("status", status);
    const qs = params.toString();
    try {
      setExecs(await apiGet<PolicyExecution[]>(`/api/policy-executions${qs ? `?${qs}` : ""}`));
      setErr("");
    } catch (e) {
      setErr(String(e));
    }
  }, [policyId, subId, status]);

  // Dropdown option sources (policy names, subscription labels) load once.
  useEffect(() => {
    apiGet<Policy[]>("/api/policies").then(setPolicies).catch(() => {});
    apiGet<Subscription[]>("/api/subscriptions").then(setSubs).catch(() => {});
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const policyName = (id: number): string =>
    policies.find((p) => p.id === id)?.name ?? String(id);

  const subLabel = (id?: string | null): string => {
    if (!id) return "—";
    return subs.find((s) => s.subscription_id === id)?.display_name ?? id;
  };

  // Toggle a row; lazily fetch its matches on first expand and cache them.
  async function toggle(executionId: string) {
    if (expanded === executionId) {
      setExpanded(null);
      return;
    }
    setExpanded(executionId);
    if (!matches[executionId]) {
      try {
        const rows = await apiGet<PolicyMatch[]>(
          `/api/policy-executions/${executionId}/matches`,
        );
        setMatches((prev) => ({ ...prev, [executionId]: rows }));
      } catch (e) {
        setErr(String(e));
      }
    }
  }

  return (
    <>
      <h1>Executions</h1>
      <p className="sub">Scheduled policy runs (pull mode) — click a row to see matched resources.</p>

      <div className="history-controls">
        <div className="field">
          <label htmlFor="f-policy">Policy</label>
          <select id="f-policy" value={policyId} onChange={(e) => setPolicyId(e.target.value)}>
            <option value="">All policies</option>
            {policies.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label htmlFor="f-sub">Subscription</label>
          <select id="f-sub" value={subId} onChange={(e) => setSubId(e.target.value)}>
            <option value="">All subscriptions</option>
            {subs.map((s) => (
              <option key={s.subscription_id} value={s.subscription_id}>
                {s.display_name}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label htmlFor="f-status">Status</label>
          <select id="f-status" value={status} onChange={(e) => setStatus(e.target.value)}>
            <option value="">All statuses</option>
            <option value="running">running</option>
            <option value="succeeded">succeeded</option>
            <option value="failed">failed</option>
          </select>
        </div>
      </div>

      {err && <div className="err">{err}</div>}

      <table>
        <thead>
          <tr>
            <th>Execution</th>
            <th>Policy</th>
            <th>Subscription</th>
            <th>Status</th>
            <th className="num">Matched</th>
            <th>Started</th>
            <th>Finished</th>
          </tr>
        </thead>
        <tbody>
          {execs.map((e) => (
            <Fragment key={e.execution_id}>
              <tr className="clickable" onClick={() => toggle(e.execution_id)}>
                <td>{expanded === e.execution_id ? "▾ " : "▸ "}{e.execution_id}</td>
                <td>{policyName(e.policy_id)}</td>
                <td>{subLabel(e.subscription_id)}</td>
                <td>
                  <span className={`badge ${statusClass(e.status)}`}>{e.status}</span>
                </td>
                <td className="num">{e.resources_matched}</td>
                <td className="muted">{ts(e.started_at)}</td>
                <td className="muted">{ts(e.finished_at)}</td>
              </tr>
              {expanded === e.execution_id && (
                <tr>
                  <td colSpan={7} className="exec-detail">
                    {e.error && <div className="err">{e.error}</div>}
                    <table>
                      <thead>
                        <tr>
                          <th>Resource</th>
                          <th>Type</th>
                          <th>Action</th>
                        </tr>
                      </thead>
                      <tbody>
                        {(matches[e.execution_id] ?? []).map((mm, i) => (
                          <tr key={i}>
                            <td title={mm.resource_id}>{shortId(mm.resource_id)}</td>
                            <td className="muted">{mm.resource_type ?? "—"}</td>
                            <td>{mm.action_taken ?? "—"}</td>
                          </tr>
                        ))}
                        {(matches[e.execution_id]?.length ?? 0) === 0 && (
                          <tr>
                            <td colSpan={3} className="muted">
                              No resources matched.
                            </td>
                          </tr>
                        )}
                      </tbody>
                    </table>
                  </td>
                </tr>
              )}
            </Fragment>
          ))}
          {execs.length === 0 && !err && (
            <tr>
              <td colSpan={7} className="muted">
                No executions yet.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </>
  );
}
