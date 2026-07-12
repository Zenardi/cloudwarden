"use client";

import { useCallback, useEffect, useState } from "react";
import { ApiError, apiDelete, apiGet, apiPost, Collection, Policy } from "../lib/api";

export default function Collections() {
  const [collections, setCollections] = useState<Collection[]>([]);
  const [policies, setPolicies] = useState<Policy[]>([]);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [addSel, setAddSel] = useState<Record<number, string>>({});
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState("");

  const load = useCallback(async () => {
    try {
      const [cols, pols] = await Promise.all([
        apiGet<Collection[]>("/api/collections"),
        apiGet<Policy[]>("/api/policies"),
      ]);
      setCollections(cols);
      setPolicies(pols);
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
      await apiPost("/api/collections", { name: name.trim(), description: description.trim() || null });
      setName("");
      setDescription("");
      await load();
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        setErr(`A collection named "${name.trim()}" already exists.`);
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

  const removeCollection = (c: Collection) => {
    if (!window.confirm(`Delete collection "${c.name}"? Member policies are kept.`)) return;
    return act(`del:${c.id}`, () => apiDelete(`/api/collections/${c.id}`));
  };

  const addPolicy = (c: Collection) => {
    const pid = addSel[c.id];
    if (!pid) return;
    return act(`add:${c.id}`, async () => {
      await apiPost(`/api/collections/${c.id}/policies/${pid}`);
      setAddSel((prev) => ({ ...prev, [c.id]: "" }));
    });
  };

  const removePolicy = (c: Collection, policyId: number) =>
    act(`rm:${c.id}:${policyId}`, () => apiDelete(`/api/collections/${c.id}/policies/${policyId}`));

  return (
    <>
      <h1>Collections</h1>
      <p className="sub">
        Group policies into named <strong>collections</strong> (à la Stacklet policy collections).
        A policy can belong to any number of collections; deleting a collection never deletes the
        member policies.
      </p>

      {err && <div className="err">{err}</div>}

      <div className="panel-form">
        <h2 style={{ marginTop: 0 }}>New collection</h2>
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
            {busy === "create" ? "Creating…" : "Create collection"}
          </button>
        </div>
      </div>

      <table>
        <thead>
          <tr>
            <th>Collection</th>
            <th>Policies</th>
            <th>Actions</th>
          </tr>
        </thead>
        <tbody>
          {collections.map((c) => {
            const members = new Set(c.policies.map((p) => p.id));
            const candidates = policies.filter((p) => !members.has(p.id));
            return (
              <tr key={c.id}>
                <td>
                  <div>{c.name}</div>
                  {c.description && <div className="hint">{c.description}</div>}
                  <div className="muted" style={{ marginTop: 4 }}>
                    {c.policy_count} {c.policy_count === 1 ? "policy" : "policies"}
                  </div>
                </td>
                <td>
                  <div className="chips">
                    {c.policies.map((p) => (
                      <span className="chip" key={p.id}>
                        {p.name}
                        <button
                          title="Remove from collection"
                          onClick={() => removePolicy(c, p.id)}
                          disabled={busy === `rm:${c.id}:${p.id}`}
                        >
                          ×
                        </button>
                      </span>
                    ))}
                    {c.policies.length === 0 && <span className="muted">No policies</span>}
                  </div>
                  <div className="member-add">
                    <select
                      value={addSel[c.id] || ""}
                      onChange={(e) => setAddSel((prev) => ({ ...prev, [c.id]: e.target.value }))}
                    >
                      <option value="">Add a policy…</option>
                      {candidates.map((p) => (
                        <option key={p.id} value={p.id}>
                          {p.name} ({p.resource_type})
                        </option>
                      ))}
                    </select>
                    <button
                      onClick={() => addPolicy(c)}
                      disabled={!addSel[c.id] || busy === `add:${c.id}`}
                    >
                      Add
                    </button>
                  </div>
                </td>
                <td>
                  <div className="row-actions">
                    <button
                      className="reject"
                      onClick={() => removeCollection(c)}
                      disabled={busy === `del:${c.id}`}
                    >
                      Delete
                    </button>
                  </div>
                </td>
              </tr>
            );
          })}
          {collections.length === 0 && !err && (
            <tr>
              <td colSpan={3} className="muted">
                No collections yet. Create one above.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </>
  );
}
