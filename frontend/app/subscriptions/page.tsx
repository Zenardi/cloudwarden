"use client";

import { useCallback, useEffect, useState } from "react";
import { apiDelete, apiGet, apiPost, Subscription } from "../lib/api";

const EMPTY = {
  subscription_id: "",
  display_name: "",
  tenant_id: "",
  client_id: "",
  client_secret: "",
  enabled: true,
};

export default function Subscriptions() {
  const [subs, setSubs] = useState<Subscription[]>([]);
  const [form, setForm] = useState({ ...EMPTY });
  const [isEdit, setIsEdit] = useState(false);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState("");
  const [tests, setTests] = useState<
    Record<string, { ok: boolean; message: string; mock?: boolean; subscription_name?: string }>
  >({});

  const load = useCallback(async () => {
    try {
      setSubs(await apiGet<Subscription[]>("/api/subscriptions"));
    } catch (e) {
      setErr(String(e));
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  function reset() {
    setForm({ ...EMPTY });
    setIsEdit(false);
    setErr("");
  }

  async function save() {
    setErr("");
    if (!form.subscription_id.trim() || !form.display_name.trim()) {
      setErr("Subscription ID and display name are required.");
      return;
    }
    setBusy("save");
    try {
      // On edit, an empty secret means "keep existing" → send null, not "".
      const client_secret = isEdit && form.client_secret === "" ? null : form.client_secret;
      await apiPost("/api/subscriptions", { ...form, client_secret });
      reset();
      await load();
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy("");
    }
  }

  function edit(s: Subscription) {
    setForm({
      subscription_id: s.subscription_id,
      display_name: s.display_name,
      tenant_id: s.tenant_id || "",
      client_id: s.client_id || "",
      client_secret: "",
      enabled: s.enabled,
    });
    setIsEdit(true);
    setErr("");
    window.scrollTo({ top: 0, behavior: "smooth" });
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

  const toggle = (s: Subscription) =>
    act(`toggle:${s.subscription_id}`, () =>
      apiPost("/api/subscriptions", {
        subscription_id: s.subscription_id,
        display_name: s.display_name,
        tenant_id: s.tenant_id,
        client_id: s.client_id,
        client_secret: null,
        enabled: !s.enabled,
      })
    );

  const makeDefault = (s: Subscription) =>
    act(`default:${s.subscription_id}`, () =>
      apiPost(`/api/subscriptions/${s.subscription_id}/default`)
    );

  const remove = (s: Subscription) =>
    act(`del:${s.subscription_id}`, () => apiDelete(`/api/subscriptions/${s.subscription_id}`));

  const run = (s: Subscription) =>
    act(`run:${s.subscription_id}`, () =>
      apiPost(`/api/runs?mock=true&subscription_id=${encodeURIComponent(s.subscription_id)}`)
    );

  async function test(s: Subscription) {
    setBusy(`test:${s.subscription_id}`);
    setErr("");
    try {
      const r = await apiPost(`/api/subscriptions/${s.subscription_id}/test`);
      setTests((prev) => ({ ...prev, [s.subscription_id]: r }));
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy("");
    }
  }

  return (
    <>
      <h1>Subscriptions</h1>
      <p className="sub">
        Manage the Azure subscriptions this instance analyzes. Leave the credential fields blank to
        use the shared service principal from the environment; fill them in to use a dedicated SP
        (e.g. a different tenant).
      </p>

      {err && <div className="err">{err}</div>}

      <div className="panel-form">
        <h2 style={{ marginTop: 0 }}>{isEdit ? "Edit subscription" : "Add subscription"}</h2>
        <div className="form-grid">
          <div className="field">
            <label>Subscription ID *</label>
            <input
              value={form.subscription_id}
              disabled={isEdit}
              placeholder="00000000-0000-0000-0000-000000000000"
              onChange={(e) => setForm({ ...form, subscription_id: e.target.value })}
            />
          </div>
          <div className="field">
            <label>Display name *</label>
            <input
              value={form.display_name}
              placeholder="Production"
              onChange={(e) => setForm({ ...form, display_name: e.target.value })}
            />
          </div>
          <div className="field">
            <label>Tenant ID</label>
            <input
              value={form.tenant_id}
              placeholder="(optional — defaults to env tenant)"
              onChange={(e) => setForm({ ...form, tenant_id: e.target.value })}
            />
          </div>
          <div className="field">
            <label>Client ID (SP)</label>
            <input
              value={form.client_id}
              placeholder="(optional — blank = shared env SP)"
              onChange={(e) => setForm({ ...form, client_id: e.target.value })}
            />
          </div>
          <div className="field">
            <label>Client secret</label>
            <input
              type="password"
              value={form.client_secret}
              placeholder={isEdit ? "(leave blank to keep existing)" : "(optional)"}
              onChange={(e) => setForm({ ...form, client_secret: e.target.value })}
            />
            <span className="hint">Stored in the database. Prefer the shared env SP where possible.</span>
          </div>
          <div className="field">
            <label>Enabled</label>
            <label className="check">
              <input
                type="checkbox"
                checked={form.enabled}
                onChange={(e) => setForm({ ...form, enabled: e.target.checked })}
              />
              Include in scheduled / all-subscription runs
            </label>
          </div>
        </div>
        <div className="form-actions">
          <button className="primary" onClick={save} disabled={busy === "save"}>
            {busy === "save" ? "Saving…" : isEdit ? "Update" : "Add subscription"}
          </button>
          {isEdit && (
            <button onClick={reset} disabled={busy === "save"}>
              Cancel
            </button>
          )}
        </div>
      </div>

      <table>
        <thead>
          <tr>
            <th>Name</th>
            <th>Subscription ID</th>
            <th>Auth</th>
            <th>State</th>
            <th>Connection</th>
            <th>Actions</th>
          </tr>
        </thead>
        <tbody>
          {subs.map((s) => {
            const b = (k: string) => busy === `${k}:${s.subscription_id}`;
            return (
              <tr key={s.subscription_id}>
                <td>
                  {s.display_name}{" "}
                  {s.is_default && <span className="badge default">default</span>}
                </td>
                <td className="muted">{s.subscription_id}</td>
                <td>{s.has_credentials ? "Dedicated SP" : "Shared env SP"}</td>
                <td>
                  <span className={`badge ${s.enabled ? "approved" : "rejected"}`}>
                    {s.enabled ? "enabled" : "disabled"}
                  </span>
                </td>
                <td>
                  {b("test") ? (
                    <span className="muted">testing…</span>
                  ) : tests[s.subscription_id] ? (
                    <div>
                      <span
                        className={`badge ${tests[s.subscription_id].ok ? "approved" : "rejected"}`}
                      >
                        {tests[s.subscription_id].ok
                          ? tests[s.subscription_id].mock
                            ? "mock ok"
                            : "connected"
                          : "failed"}
                      </span>
                      <div className="hint" style={{ marginTop: 4, maxWidth: 220 }}>
                        {tests[s.subscription_id].message}
                      </div>
                    </div>
                  ) : (
                    <span className="muted">—</span>
                  )}
                </td>
                <td>
                  <div className="row-actions">
                    <button onClick={() => test(s)} disabled={b("test")}>
                      {b("test") ? "…" : "Test"}
                    </button>
                    <button onClick={() => run(s)} disabled={b("run")}>
                      {b("run") ? "…" : "Run"}
                    </button>
                    <button onClick={() => edit(s)}>Edit</button>
                    <button onClick={() => toggle(s)} disabled={b("toggle")}>
                      {s.enabled ? "Disable" : "Enable"}
                    </button>
                    {!s.is_default && (
                      <button onClick={() => makeDefault(s)} disabled={b("default")}>
                        Set default
                      </button>
                    )}
                    <button className="reject" onClick={() => remove(s)} disabled={b("del")}>
                      Delete
                    </button>
                  </div>
                </td>
              </tr>
            );
          })}
          {subs.length === 0 && !err && (
            <tr>
              <td colSpan={6} className="muted">
                No subscriptions yet. Add one above.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </>
  );
}
