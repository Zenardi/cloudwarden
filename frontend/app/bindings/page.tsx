"use client";

import { useCallback, useEffect, useState } from "react";
import {
  AccountGroup,
  ApiError,
  apiDelete,
  apiGet,
  apiPost,
  apiPut,
  Binding,
  BindingRunResult,
  Collection,
  PolicyExecution,
} from "../lib/api";

function ts(value?: string | null): string {
  if (!value) return "—";
  return value.replace("T", " ").slice(0, 19);
}

function statusClass(status?: string): string {
  if (status === "succeeded" || status === "completed") return "approved";
  if (status === "failed") return "rejected";
  return "";
}

export default function Bindings() {
  const [bindings, setBindings] = useState<Binding[]>([]);
  const [collections, setCollections] = useState<Collection[]>([]);
  const [groups, setGroups] = useState<AccountGroup[]>([]);
  const [executions, setExecutions] = useState<PolicyExecution[]>([]);
  const [runResults, setRunResults] = useState<Record<number, BindingRunResult>>({});
  const [collectionId, setCollectionId] = useState("");
  const [groupId, setGroupId] = useState("");
  const [schedule, setSchedule] = useState("");
  const [mode, setMode] = useState("pull");
  const [dryRun, setDryRun] = useState(true);
  const [enabled, setEnabled] = useState(true);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState("");

  const load = useCallback(async () => {
    try {
      const [bs, cols, grps, execs] = await Promise.all([
        apiGet<Binding[]>("/api/bindings"),
        apiGet<Collection[]>("/api/collections"),
        apiGet<AccountGroup[]>("/api/account-groups"),
        apiGet<PolicyExecution[]>("/api/policy-executions"),
      ]);
      setBindings(bs);
      setCollections(cols);
      setGroups(grps);
      setExecutions(execs);
      setErr("");
    } catch (e) {
      setErr(String(e));
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const colName = (id: number) => collections.find((c) => c.id === id)?.name ?? `#${id}`;
  const grpName = (id: number) => groups.find((g) => g.id === id)?.name ?? `#${id}`;

  // Executions arrive newest-first, so the first match is a binding's latest run.
  const lastRun = (bindingId: number): PolicyExecution | undefined =>
    executions.find((e) => e.binding_id === bindingId);

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

  async function create() {
    setErr("");
    if (!collectionId || !groupId) {
      setErr("Select an existing collection and account group.");
      return;
    }
    setBusy("create");
    try {
      await apiPost("/api/bindings", {
        collection_id: Number(collectionId),
        account_group_id: Number(groupId),
        schedule: schedule.trim() || null,
        mode,
        dry_run: dryRun,
        enabled,
      });
      setCollectionId("");
      setGroupId("");
      setSchedule("");
      setMode("pull");
      setDryRun(true);
      setEnabled(true);
      await load();
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) {
        setErr("The selected collection or account group no longer exists.");
      } else if (e instanceof ApiError && e.status === 400) {
        setErr("Invalid mode — must be pull or event.");
      } else {
        setErr(String(e));
      }
    } finally {
      setBusy("");
    }
  }

  const update = (b: Binding, changes: Partial<Binding>) =>
    act(`upd:${b.id}`, () => apiPut(`/api/bindings/${b.id}`, changes));

  const remove = (b: Binding) => {
    if (
      !window.confirm(
        `Delete this binding (${colName(b.collection_id)} → ${grpName(b.account_group_id)})?`,
      )
    )
      return;
    return act(`del:${b.id}`, () => apiDelete(`/api/bindings/${b.id}`));
  };

  const run = (b: Binding) =>
    act(`run:${b.id}`, async () => {
      const result = await apiPost<BindingRunResult>(`/api/bindings/${b.id}/run`);
      setRunResults((prev) => ({ ...prev, [b.id]: result }));
    });

  const canCreate = collectionId !== "" && groupId !== "";

  return (
    <>
      <h1>Bindings</h1>
      <p className="sub">
        A <strong>binding</strong> runs a policy <strong>collection</strong> against an{" "}
        <strong>account group</strong> on a schedule (à la Stacklet). Create one, edit its config
        inline, or run it now — executions are tagged with the binding.
      </p>

      {err && <div className="err">{err}</div>}

      <div className="panel-form">
        <h2 style={{ marginTop: 0 }}>New binding</h2>
        <div className="form-grid">
          <div className="field">
            <label>Collection *</label>
            <select value={collectionId} onChange={(e) => setCollectionId(e.target.value)}>
              <option value="">Select a collection…</option>
              {collections.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label>Account group *</label>
            <select value={groupId} onChange={(e) => setGroupId(e.target.value)}>
              <option value="">Select an account group…</option>
              {groups.map((g) => (
                <option key={g.id} value={g.id}>
                  {g.name}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label>Schedule (cron)</label>
            <input
              value={schedule}
              placeholder="0 2 * * *"
              onChange={(e) => setSchedule(e.target.value)}
            />
          </div>
          <div className="field">
            <label>Mode</label>
            <select value={mode} onChange={(e) => setMode(e.target.value)}>
              <option value="pull">pull</option>
              <option value="event">event</option>
            </select>
          </div>
          <div className="field">
            <label className="check">
              <input type="checkbox" checked={dryRun} onChange={(e) => setDryRun(e.target.checked)} />{" "}
              Dry run
            </label>
          </div>
          <div className="field">
            <label className="check">
              <input
                type="checkbox"
                checked={enabled}
                onChange={(e) => setEnabled(e.target.checked)}
              />{" "}
              Enabled
            </label>
          </div>
        </div>
        <div className="form-actions">
          <button className="primary" onClick={create} disabled={!canCreate || busy === "create"}>
            {busy === "create" ? "Creating…" : "Create binding"}
          </button>
          {!canCreate && <span className="hint">Select a collection and an account group.</span>}
        </div>
      </div>

      <table>
        <thead>
          <tr>
            <th>Collection</th>
            <th>Account group</th>
            <th>Schedule</th>
            <th>Mode</th>
            <th>Dry run</th>
            <th>Enabled</th>
            <th>Last run</th>
            <th>Actions</th>
          </tr>
        </thead>
        <tbody>
          {bindings.map((b) => {
            const last = lastRun(b.id);
            const rr = runResults[b.id];
            return (
              <tr key={b.id}>
                <td>{colName(b.collection_id)}</td>
                <td>{grpName(b.account_group_id)}</td>
                <td>
                  <input
                    key={`sch:${b.id}:${b.updated_at ?? ""}`}
                    defaultValue={b.schedule ?? ""}
                    placeholder="—"
                    style={{ minWidth: 110 }}
                    onBlur={(e) => {
                      const v = e.target.value.trim();
                      if (v !== (b.schedule ?? "")) update(b, { schedule: v || null });
                    }}
                  />
                </td>
                <td>
                  <select value={b.mode} onChange={(e) => update(b, { mode: e.target.value })}>
                    <option value="pull">pull</option>
                    <option value="event">event</option>
                  </select>
                </td>
                <td>
                  <input
                    type="checkbox"
                    checked={b.dry_run}
                    onChange={(e) => update(b, { dry_run: e.target.checked })}
                  />
                </td>
                <td>
                  <input
                    type="checkbox"
                    checked={b.enabled}
                    onChange={(e) => update(b, { enabled: e.target.checked })}
                  />
                </td>
                <td>
                  {rr ? (
                    <span className={`badge ${statusClass(rr.status)}`} title={rr.reason ?? ""}>
                      {rr.status}
                      {rr.status === "completed" ? ` · ${rr.executions.length} exec` : ""}
                    </span>
                  ) : last ? (
                    <>
                      <span className={`badge ${statusClass(last.status)}`}>{last.status}</span>
                      <div className="muted" style={{ marginTop: 2 }}>
                        {ts(last.started_at)}
                      </div>
                    </>
                  ) : (
                    <span className="muted">never</span>
                  )}
                </td>
                <td>
                  <div className="row-actions">
                    <button
                      className="primary"
                      onClick={() => run(b)}
                      disabled={busy === `run:${b.id}`}
                    >
                      {busy === `run:${b.id}` ? "Running…" : "Run"}
                    </button>
                    <button
                      className="reject"
                      onClick={() => remove(b)}
                      disabled={busy === `del:${b.id}`}
                    >
                      Delete
                    </button>
                  </div>
                </td>
              </tr>
            );
          })}
          {bindings.length === 0 && !err && (
            <tr>
              <td colSpan={8} className="muted">
                No bindings yet. Create one above to run a collection against an account group.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </>
  );
}
