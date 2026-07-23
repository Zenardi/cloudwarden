"use client";

import { useCallback, useEffect, useState } from "react";
import {
  ApiError,
  BUDGET_PERIODS,
  BUDGET_SCOPES,
  Budget,
  BudgetStatus,
  createBudget,
  deleteBudget,
  getBudgetStatus,
  listBudgets,
  updateBudget,
} from "../lib/api";

/** Parse a "50,80,100" threshold field into ordered actual-basis rules. */
function parseThresholds(raw: string) {
  return raw
    .split(",")
    .map((p) => parseFloat(p.trim()))
    .filter((p) => !Number.isNaN(p))
    .sort((a, b) => a - b)
    .map((pct) => ({ pct, basis: "actual" as const }));
}

function pctColor(pct: number): string {
  if (pct >= 100) return "#e5484d";
  if (pct >= 80) return "#f5a524";
  return "#30a46c";
}

export default function Budgets() {
  const [budgets, setBudgets] = useState<Budget[]>([]);
  const [statuses, setStatuses] = useState<Record<number, BudgetStatus>>({});
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState("");

  // create form
  const [name, setName] = useState("");
  const [amount, setAmount] = useState("");
  const [scopeType, setScopeType] = useState<string>("subscription");
  const [scopeValue, setScopeValue] = useState("");
  const [period, setPeriod] = useState<string>("monthly");
  const [thresholds, setThresholds] = useState("80,100");

  const load = useCallback(async () => {
    try {
      const bs = await listBudgets();
      setBudgets(bs);
      // Status per budget is best-effort — a failure (e.g. no cost yet) must not
      // blank the whole page.
      const results = await Promise.allSettled(bs.map((b) => getBudgetStatus(b.id)));
      const next: Record<number, BudgetStatus> = {};
      results.forEach((r, i) => {
        if (r.status === "fulfilled") next[bs[i].id] = r.value;
      });
      setStatuses(next);
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

  async function submit() {
    setErr("");
    const amt = parseFloat(amount);
    if (!name.trim() || Number.isNaN(amt)) {
      setErr("Budget name and a numeric amount are required.");
      return;
    }
    setBusy("create");
    try {
      await createBudget({
        name: name.trim(),
        amount: amt,
        scope_type: scopeType,
        scope_value: scopeValue.trim() || null,
        period,
        thresholds: parseThresholds(thresholds),
      });
      setName("");
      setAmount("");
      setScopeValue("");
      setThresholds("80,100");
      await load();
    } catch (e) {
      if (e instanceof ApiError && (e.status === 409 || e.status === 422)) {
        setErr(`Budget rejected: ${e.body?.detail ?? "invalid"}.`);
      } else {
        setErr(String(e));
      }
    } finally {
      setBusy("");
    }
  }

  const toggle = (b: Budget) =>
    act(`toggle:${b.id}`, () => updateBudget(b.id, { enabled: !b.enabled }));

  const remove = (b: Budget) => {
    if (!window.confirm(`Delete budget "${b.name}"?`)) return;
    return act(`del:${b.id}`, () => deleteBudget(b.id));
  };

  return (
    <>
      <h1>Budgets</h1>
      <p className="sub">
        Set spend <strong>budgets</strong> over a scope (subscription / account / group / tag /
        team) and a period, with ordered <strong>threshold rules</strong> (e.g. 80/100% of actual).
        Every pipeline run evaluates actual spend against each budget and fires a notification
        through the existing transports the first time a threshold is crossed — once per period, no
        alert storms.
      </p>

      {err && <div className="err">{err}</div>}

      {/* Create ------------------------------------------------------------- */}
      <div className="panel-form">
        <h2 style={{ marginTop: 0 }}>New budget</h2>
        <div className="form-grid">
          <div className="field">
            <label>Name *</label>
            <input value={name} placeholder="prod-monthly" onChange={(e) => setName(e.target.value)} />
          </div>
          <div className="field">
            <label>Amount *</label>
            <input value={amount} placeholder="10000" onChange={(e) => setAmount(e.target.value)} />
          </div>
          <div className="field">
            <label>Scope</label>
            <select value={scopeType} onChange={(e) => setScopeType(e.target.value)}>
              {BUDGET_SCOPES.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label>Scope value</label>
            <input
              value={scopeValue}
              placeholder="subscription id / group / tag=value"
              onChange={(e) => setScopeValue(e.target.value)}
            />
          </div>
          <div className="field">
            <label>Period</label>
            <select value={period} onChange={(e) => setPeriod(e.target.value)}>
              {BUDGET_PERIODS.map((p) => (
                <option key={p} value={p}>
                  {p}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label>Thresholds (%)</label>
            <input
              value={thresholds}
              placeholder="80,100"
              onChange={(e) => setThresholds(e.target.value)}
            />
          </div>
        </div>
        <div className="form-actions">
          <button className="primary" onClick={submit} disabled={busy === "create"}>
            {busy === "create" ? "Creating…" : "Create budget"}
          </button>
        </div>
      </div>

      <table>
        <thead>
          <tr>
            <th>Budget</th>
            <th>Scope</th>
            <th>Period</th>
            <th>Amount</th>
            <th>Spend vs budget</th>
            <th>Thresholds</th>
            <th>Enabled</th>
            <th>Actions</th>
          </tr>
        </thead>
        <tbody>
          {budgets.map((b) => {
            const st = statuses[b.id];
            const pct = st ? st.actual_pct : 0;
            const crossed = st ? st.crossed : [];
            return (
              <tr key={b.id}>
                <td>{b.name}</td>
                <td className="muted">
                  {b.scope_type}
                  {b.scope_value ? `: ${b.scope_value}` : ""}
                </td>
                <td>
                  <span className="badge">{b.period}</span>
                </td>
                <td>
                  {b.amount.toLocaleString()} {b.currency}
                </td>
                <td>
                  <div
                    title={st ? `${st.spend.toLocaleString()} ${b.currency} (${pct.toFixed(1)}%)` : "—"}
                    style={{
                      background: "var(--surface-2, #e6e6e6)",
                      borderRadius: 4,
                      height: 10,
                      minWidth: 120,
                      overflow: "hidden",
                    }}
                  >
                    <div
                      style={{
                        width: `${Math.min(pct, 100)}%`,
                        background: pctColor(pct),
                        height: "100%",
                      }}
                    />
                  </div>
                  <span className="muted">{st ? `${pct.toFixed(1)}%` : "no data"}</span>
                </td>
                <td>
                  {b.thresholds.map((t) => (
                    <span
                      key={`${t.pct}-${t.basis}`}
                      className="badge"
                      style={{
                        marginRight: 4,
                        opacity: crossed.includes(t.pct) ? 1 : 0.5,
                        borderColor: crossed.includes(t.pct) ? pctColor(t.pct) : undefined,
                      }}
                    >
                      {t.pct}%{t.basis === "forecast" ? " (fc)" : ""}
                    </span>
                  ))}
                </td>
                <td>
                  <label className="muted">
                    <input
                      type="checkbox"
                      checked={b.enabled}
                      onChange={() => toggle(b)}
                      disabled={busy === `toggle:${b.id}`}
                    />{" "}
                    {b.enabled ? "on" : "off"}
                  </label>
                </td>
                <td>
                  <div className="row-actions">
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
          {budgets.length === 0 && !err && (
            <tr>
              <td colSpan={8} className="muted">
                No budgets yet. Create one above.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </>
  );
}
