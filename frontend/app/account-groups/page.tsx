"use client";

import { useCallback, useEffect, useState } from "react";
import {
  AccountGroup,
  ApiError,
  apiDelete,
  apiGet,
  apiPost,
  Subscription,
} from "../lib/api";

export default function AccountGroups() {
  const [groups, setGroups] = useState<AccountGroup[]>([]);
  const [subs, setSubs] = useState<Subscription[]>([]);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [addSel, setAddSel] = useState<Record<number, string>>({});
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState("");

  const load = useCallback(async () => {
    try {
      const [gs, ss] = await Promise.all([
        apiGet<AccountGroup[]>("/api/account-groups"),
        apiGet<Subscription[]>("/api/subscriptions"),
      ]);
      setGroups(gs);
      setSubs(ss);
    } catch (e) {
      setErr(String(e));
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function create() {
    setErr("");
    if (!name.trim()) {
      setErr("Name is required.");
      return;
    }
    setBusy("create");
    try {
      await apiPost("/api/account-groups", {
        name: name.trim(),
        description: description.trim() || null,
      });
      setName("");
      setDescription("");
      await load();
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        setErr(`An account group named "${name.trim()}" already exists.`);
      } else {
        setErr(String(e));
      }
    } finally {
      setBusy("");
    }
  }

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

  const removeGroup = (g: AccountGroup) => {
    if (!window.confirm(`Delete account group "${g.name}"? Member subscriptions are kept.`)) return;
    return act(`del:${g.id}`, () => apiDelete(`/api/account-groups/${g.id}`));
  };

  const addSub = (g: AccountGroup) => {
    const sid = addSel[g.id];
    if (!sid) return;
    return act(`add:${g.id}`, async () => {
      await apiPost(`/api/account-groups/${g.id}/subscriptions/${sid}`);
      setAddSel((prev) => ({ ...prev, [g.id]: "" }));
    });
  };

  const removeSub = (g: AccountGroup, subscriptionId: string) =>
    act(`rm:${g.id}:${subscriptionId}`, () =>
      apiDelete(`/api/account-groups/${g.id}/subscriptions/${subscriptionId}`),
    );

  return (
    <>
      <h1>Account Groups</h1>
      <p className="sub">
        Organize subscriptions into named <strong>account groups</strong> (à la Stacklet account
        groups) so policies can target logical sets of accounts. A subscription can belong to any
        number of groups; deleting a group never deletes the member subscriptions.
      </p>

      {err && <div className="err">{err}</div>}

      <div className="panel-form">
        <h2 style={{ marginTop: 0 }}>New account group</h2>
        <div className="form-grid">
          <div className="field">
            <label>Name *</label>
            <input value={name} placeholder="production" onChange={(e) => setName(e.target.value)} />
          </div>
          <div className="field">
            <label>Description</label>
            <input
              value={description}
              placeholder="(optional)"
              onChange={(e) => setDescription(e.target.value)}
            />
          </div>
        </div>
        <div className="form-actions">
          <button className="primary" onClick={create} disabled={busy === "create"}>
            {busy === "create" ? "Creating…" : "Create group"}
          </button>
        </div>
      </div>

      <table>
        <thead>
          <tr>
            <th>Group</th>
            <th>Subscriptions</th>
            <th>Actions</th>
          </tr>
        </thead>
        <tbody>
          {groups.map((g) => {
            const members = new Set(g.subscriptions.map((s) => s.subscription_id));
            const candidates = subs.filter((s) => !members.has(s.subscription_id));
            return (
              <tr key={g.id}>
                <td>
                  <div>{g.name}</div>
                  {g.description && <div className="hint">{g.description}</div>}
                  <div className="muted" style={{ marginTop: 4 }}>
                    {g.subscription_count}{" "}
                    {g.subscription_count === 1 ? "subscription" : "subscriptions"}
                  </div>
                </td>
                <td>
                  <div className="chips">
                    {g.subscriptions.map((s) => (
                      <span className="chip" key={s.subscription_id}>
                        {s.display_name}
                        <button
                          title="Remove from group"
                          onClick={() => removeSub(g, s.subscription_id)}
                          disabled={busy === `rm:${g.id}:${s.subscription_id}`}
                        >
                          ×
                        </button>
                      </span>
                    ))}
                    {g.subscriptions.length === 0 && <span className="muted">No subscriptions</span>}
                  </div>
                  <div className="member-add">
                    <select
                      value={addSel[g.id] || ""}
                      onChange={(e) => setAddSel((prev) => ({ ...prev, [g.id]: e.target.value }))}
                    >
                      <option value="">Add a subscription…</option>
                      {candidates.map((s) => (
                        <option key={s.subscription_id} value={s.subscription_id}>
                          {s.display_name}
                        </option>
                      ))}
                    </select>
                    <button
                      onClick={() => addSub(g)}
                      disabled={!addSel[g.id] || busy === `add:${g.id}`}
                    >
                      Add
                    </button>
                  </div>
                </td>
                <td>
                  <div className="row-actions">
                    <button
                      className="reject"
                      onClick={() => removeGroup(g)}
                      disabled={busy === `del:${g.id}`}
                    >
                      Delete
                    </button>
                  </div>
                </td>
              </tr>
            );
          })}
          {groups.length === 0 && !err && (
            <tr>
              <td colSpan={3} className="muted">
                No account groups yet. Create one above.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </>
  );
}
