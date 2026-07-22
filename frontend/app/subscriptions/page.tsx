"use client";

import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { apiDelete, apiGet, apiPost, Subscription } from "../lib/api";

const EMPTY = {
  subscription_id: "",
  display_name: "",
  environment: "",
  tenant_id: "",
  client_id: "",
  client_secret: "",
  enabled: true,
};

// Subscription "kind" — lifecycle classification. Optional (blank = unclassified).
// Kept in sync with the backend's ENVIRONMENT_RECLAIM_FACTORS: it weights how much
// of a resource's idle/waste savings counts as potential savings.
const ENVIRONMENTS = ["Development", "QA", "Prod", "Sandbox"] as const;

type MenuItem = {
  label: string;
  onClick: () => void;
  danger?: boolean;
  disabled?: boolean;
};

/**
 * Row "more actions" overflow menu — collapses the per-row action buttons into a
 * single kebab trigger plus a dropdown. The dropdown is `position: fixed`, anchored
 * to the trigger's rect, so it escapes the table's `overflow: hidden` (which rounds
 * the table corners and would otherwise clip an absolutely-positioned child). It
 * closes on outside click, Escape, and scroll/resize — a fixed menu would otherwise
 * detach from its trigger as the page moves under it.
 */
function RowActionsMenu({
  label,
  items,
  open,
  onToggle,
  onClose,
}: {
  label: string;
  items: MenuItem[];
  open: boolean;
  onToggle: () => void;
  onClose: () => void;
}) {
  const btnRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState<{ left: number; top?: number; bottom?: number } | null>(null);

  // Anchor the fixed menu to the trigger; flip above when it would overflow below.
  useLayoutEffect(() => {
    if (!open || !btnRef.current) return;
    const r = btnRef.current.getBoundingClientRect();
    const MENU_W = 184;
    const estH = items.length * 38 + 12;
    const left = Math.max(8, r.right - MENU_W);
    const openUp = r.bottom + estH + 8 > window.innerHeight;
    setPos(
      openUp
        ? { left, bottom: window.innerHeight - r.top + 4 }
        : { left, top: r.bottom + 4 },
    );
  }, [open, items.length]);

  useEffect(() => {
    if (!open) return;
    const onDocDown = (e: MouseEvent) => {
      const t = e.target as Node;
      if (menuRef.current?.contains(t) || btnRef.current?.contains(t)) return;
      onClose();
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        onClose();
        btnRef.current?.focus();
      }
    };
    const dismiss = () => onClose();
    document.addEventListener("mousedown", onDocDown);
    document.addEventListener("keydown", onKey);
    window.addEventListener("scroll", dismiss, true);
    window.addEventListener("resize", dismiss);
    return () => {
      document.removeEventListener("mousedown", onDocDown);
      document.removeEventListener("keydown", onKey);
      window.removeEventListener("scroll", dismiss, true);
      window.removeEventListener("resize", dismiss);
    };
  }, [open, onClose]);

  // Move focus into the menu once positioned, so it's operable by keyboard.
  useEffect(() => {
    if (open && pos) {
      menuRef.current?.querySelector<HTMLButtonElement>("button:not([disabled])")?.focus();
    }
  }, [open, pos]);

  return (
    <>
      <button
        ref={btnRef}
        className="kebab-btn"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={label}
        onClick={onToggle}
      >
        <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
          <circle cx="12" cy="5" r="1.7" />
          <circle cx="12" cy="12" r="1.7" />
          <circle cx="12" cy="19" r="1.7" />
        </svg>
      </button>
      {open && pos && (
        <div
          ref={menuRef}
          className="menu"
          role="menu"
          aria-label={label}
          style={{ left: pos.left, top: pos.top, bottom: pos.bottom }}
        >
          {items.map((it, i) => (
            <button
              key={i}
              role="menuitem"
              className={it.danger ? "danger" : undefined}
              disabled={it.disabled}
              onClick={() => {
                it.onClick();
                onClose();
              }}
            >
              {it.label}
            </button>
          ))}
        </div>
      )}
    </>
  );
}

export default function Subscriptions() {
  const [subs, setSubs] = useState<Subscription[]>([]);
  const [form, setForm] = useState({ ...EMPTY });
  const [isEdit, setIsEdit] = useState(false);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState("");
  const [tests, setTests] = useState<
    Record<string, { ok: boolean; message: string; mock?: boolean; subscription_name?: string }>
  >({});
  // Which row's action menu is open (only one at a time). Keyed by subscription_id.
  const [menuFor, setMenuFor] = useState<string | null>(null);
  const closeMenu = useCallback(() => setMenuFor(null), []);

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
      environment: s.environment || "",
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
        environment: s.environment,
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
      apiPost(`/api/runs?subscription_id=${encodeURIComponent(s.subscription_id)}`)
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
            <label>Kind</label>
            <select
              value={form.environment}
              onChange={(e) => setForm({ ...form, environment: e.target.value })}
            >
              <option value="">Unclassified</option>
              {ENVIRONMENTS.map((env) => (
                <option key={env} value={env}>
                  {env}
                </option>
              ))}
            </select>
            <span className="hint">
              Weights potential savings — non-prod idle waste is safer to reclaim.
            </span>
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
            <th>Kind</th>
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
                <td>
                  {s.environment ? (
                    <span className={`badge env env-${s.environment.toLowerCase()}`}>
                      {s.environment}
                    </span>
                  ) : (
                    <span className="muted">—</span>
                  )}
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
                  <RowActionsMenu
                    label={`Actions for ${s.display_name}`}
                    open={menuFor === s.subscription_id}
                    onToggle={() =>
                      setMenuFor(menuFor === s.subscription_id ? null : s.subscription_id)
                    }
                    onClose={closeMenu}
                    items={[
                      {
                        label: b("test") ? "Testing…" : "Test connection",
                        onClick: () => test(s),
                        disabled: b("test"),
                      },
                      {
                        label: b("run") ? "Running…" : "Run analysis",
                        onClick: () => run(s),
                        disabled: b("run"),
                      },
                      { label: "Edit", onClick: () => edit(s) },
                      {
                        label: s.enabled ? "Disable" : "Enable",
                        onClick: () => toggle(s),
                        disabled: b("toggle"),
                      },
                      ...(!s.is_default
                        ? [
                            {
                              label: "Set as default",
                              onClick: () => makeDefault(s),
                              disabled: b("default"),
                            },
                          ]
                        : []),
                      {
                        label: "Delete",
                        onClick: () => remove(s),
                        danger: true,
                        disabled: b("del"),
                      },
                    ]}
                  />
                </td>
              </tr>
            );
          })}
          {subs.length === 0 && !err && (
            <tr>
              <td colSpan={7} className="muted">
                No subscriptions yet. Add one above.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </>
  );
}
