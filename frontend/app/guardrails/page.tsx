"use client";

import { useCallback, useEffect, useState } from "react";
import {
  apiGet,
  applyGuardrail,
  GUARDRAIL_PROVIDERS,
  GuardrailApplyResult,
  GuardrailPreview,
  GuardrailProvider,
  Policy,
  previewGuardrail,
} from "../lib/api";

/** Native construct label per provider — shown as the what-if target. */
const NATIVE_LABEL: Record<GuardrailProvider, string> = {
  azure: "Azure Policy",
  aws: "AWS Service Control Policy",
  gcp: "GCP Organization Policy",
};

export default function Guardrails() {
  const [policies, setPolicies] = useState<Policy[]>([]);
  const [policyId, setPolicyId] = useState<string>("");
  const [provider, setProvider] = useState<GuardrailProvider>("azure");
  const [scope, setScope] = useState("");
  const [preview, setPreview] = useState<GuardrailPreview | null>(null);
  const [applied, setApplied] = useState<GuardrailApplyResult | null>(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState("");

  const load = useCallback(async () => {
    try {
      setPolicies(await apiGet<Policy[]>("/api/policies"));
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
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy("");
    }
  }

  function onPreview(e: React.FormEvent) {
    e.preventDefault();
    if (!policyId) {
      setErr("select a policy to translate");
      return;
    }
    setApplied(null);
    act("preview", async () => {
      const result = await previewGuardrail({
        policy_id: Number(policyId),
        provider,
        scope: scope || null,
      });
      setPreview(result);
    });
  }

  function onApply() {
    if (!policyId) return;
    act("apply", async () => {
      const result = await applyGuardrail({
        policy_id: Number(policyId),
        provider,
        scope: scope || null,
        // Dry-run-first: the backend guardrails must also permit a real write.
        dry_run: true,
      });
      setApplied(result);
    });
  }

  return (
    <div className="page">
      <header className="page-head">
        <h1>Preventive guardrails</h1>
        <p className="muted">
          Translate an authored policy into a provider&apos;s native <strong>deny</strong>{" "}
          construct — Azure Policy, AWS SCP, or GCP Org Policy — so a non-compliant resource is
          blocked <strong>at creation</strong>. Preview the what-if effect, then apply behind the
          same remediation guardrails (<code>REMEDIATION_ENABLED</code> + allow-list + write SP).
        </p>
      </header>

      {err && <div className="err">{err}</div>}

      <section className="card">
        <h2>Translate &amp; preview</h2>
        <form className="form-grid" onSubmit={onPreview}>
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
            Provider
            <select
              value={provider}
              onChange={(e) => setProvider(e.target.value as GuardrailProvider)}
            >
              {GUARDRAIL_PROVIDERS.map((p) => (
                <option key={p} value={p}>
                  {NATIVE_LABEL[p]}
                </option>
              ))}
            </select>
          </label>
          <label>
            Scope (optional)
            <input
              value={scope}
              onChange={(e) => setScope(e.target.value)}
              placeholder="subscription / OU-root / organization"
            />
          </label>
          <button type="submit" className="primary" disabled={busy === "preview"}>
            {busy === "preview" ? "Translating…" : "Preview (what-if)"}
          </button>
        </form>
      </section>

      {preview && (
        <section className="card">
          <h2>What-if</h2>
          {preview.expressible ? (
            <>
              <p>
                <span className="badge" style={{ background: "#30a46c" }}>
                  {preview.kind}
                </span>{" "}
                expressible as <strong>{NATIVE_LABEL[preview.provider as GuardrailProvider]}</strong>
                {preview.scope ? ` at ${preview.scope.level} ${preview.scope.target}` : ""}.
              </p>
              <pre
                className="policy-editor"
                style={{ overflowX: "auto", maxHeight: 360, minHeight: "auto" }}
              >
                {JSON.stringify(preview.definition, null, 2)}
              </pre>
              <button className="primary" onClick={onApply} disabled={busy === "apply"}>
                {busy === "apply" ? "Applying…" : "Apply (dry-run-first)"}
              </button>
            </>
          ) : (
            <p>
              <span className="badge" style={{ background: "#e5484d" }}>
                not expressible
              </span>{" "}
              {preview.reason}
            </p>
          )}
        </section>
      )}

      {applied && (
        <section className="card">
          <h2>Apply result</h2>
          <p>
            <span
              className="badge"
              style={{
                background: applied.applied ? "#30a46c" : applied.error ? "#e5484d" : "#f5a524",
              }}
            >
              {applied.applied ? "applied" : applied.error ? "error" : "dry-run"}
            </span>{" "}
            {applied.blocked && "blocked by guardrails — "}
            {applied.error ?? String(applied.result)}
          </p>
        </section>
      )}
    </div>
  );
}
