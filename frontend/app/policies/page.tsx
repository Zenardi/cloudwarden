"use client";

import { useCallback, useEffect, useState } from "react";
import { ApiError, apiDelete, apiGet, apiPost, apiPut, Policy, ValidationResult } from "../lib/api";

const TEMPLATE = `{
  "policies": [
    {
      "name": "stopped-vms",
      "resource": "azure.vm",
      "filters": [
        {
          "type": "instance-view",
          "key": "statuses[].code",
          "op": "in",
          "value": "PowerState/deallocated"
        }
      ]
    }
  ]
}`;

/** Parse the editor text into a Custodian spec, or return a friendly error. */
function parseSpec(text: string): { spec?: Record<string, any>; error?: string } {
  let value: unknown;
  try {
    value = JSON.parse(text);
  } catch (e) {
    return { error: `Invalid JSON: ${e instanceof Error ? e.message : String(e)}` };
  }
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return { error: "Spec must be a JSON object with a `policies` array." };
  }
  const spec = value as Record<string, any>;
  if (!Array.isArray(spec.policies) || spec.policies.length === 0) {
    return { error: "Spec must contain a non-empty `policies` array." };
  }
  return { spec };
}

const resourceOf = (spec: Record<string, any>): string =>
  (spec.policies?.[0]?.resource as string) || "";

export default function Policies() {
  const [policies, setPolicies] = useState<Policy[]>([]);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [specText, setSpecText] = useState(TEMPLATE);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [validation, setValidation] = useState<ValidationResult | null>(null);
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

  function reset() {
    setName("");
    setDescription("");
    setSpecText(TEMPLATE);
    setEditingId(null);
    setValidation(null);
    setErr("");
  }

  async function validate() {
    setErr("");
    const { spec, error } = parseSpec(specText);
    if (error) {
      setValidation({ valid: false, errors: [error] });
      return;
    }
    setBusy("validate");
    try {
      setValidation(await apiPost<ValidationResult>("/api/policies/validate", { spec }));
    } catch (e) {
      // Malformed body (e.g. no `policies`) comes back as a 400 with a detail string.
      const detail = e instanceof ApiError ? e.body?.detail : undefined;
      setValidation({ valid: false, errors: [detail || String(e)] });
    } finally {
      setBusy("");
    }
  }

  async function save() {
    setErr("");
    setValidation(null);
    if (!name.trim()) {
      setErr("Name is required.");
      return;
    }
    const { spec, error } = parseSpec(specText);
    if (error) {
      setValidation({ valid: false, errors: [error] });
      return;
    }
    const payload = {
      name: name.trim(),
      resource_type: resourceOf(spec!),
      spec,
      description: description.trim() || null,
    };
    setBusy("save");
    try {
      if (editingId !== null) {
        await apiPut(`/api/policies/${editingId}`, payload);
      } else {
        await apiPost("/api/policies", payload);
      }
      reset();
      await load();
    } catch (e) {
      if (e instanceof ApiError && e.status === 422) {
        // Validation failed server-side — surface the errors inline, stay on the form.
        const errors: string[] = e.body?.detail?.errors ?? ["Policy failed validation."];
        setValidation({ valid: false, errors });
      } else if (e instanceof ApiError && e.status === 409) {
        setErr(`A policy named "${name.trim()}" already exists.`);
      } else {
        setErr(String(e));
      }
    } finally {
      setBusy("");
    }
  }

  function edit(p: Policy) {
    setEditingId(p.id);
    setName(p.name);
    setDescription(p.description || "");
    setSpecText(JSON.stringify(p.spec, null, 2));
    setValidation(null);
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

  const toggle = (p: Policy) =>
    act(`toggle:${p.id}`, () => apiPost(`/api/policies/${p.id}/enabled?enabled=${!p.enabled}`));

  const remove = (p: Policy) => {
    if (!window.confirm(`Delete policy "${p.name}"?`)) return;
    return act(`del:${p.id}`, () => apiDelete(`/api/policies/${p.id}`));
  };

  return (
    <>
      <h1>Policies</h1>
      <p className="sub">
        Author governance-as-code rules (Cloud Custodian policies). Every save is validated against
        the c7n schema first — an invalid policy is never stored. Use <strong>Validate</strong> to
        dry-check the spec without saving.
      </p>

      {err && <div className="err">{err}</div>}

      <div className="panel-form">
        <h2 style={{ marginTop: 0 }}>{editingId !== null ? "Edit policy" : "New policy"}</h2>
        <div className="form-grid">
          <div className="field">
            <label>Name *</label>
            <input
              value={name}
              placeholder="stopped-vms"
              onChange={(e) => setName(e.target.value)}
            />
          </div>
          <div className="field">
            <label>Description</label>
            <input
              value={description}
              placeholder="(optional)"
              onChange={(e) => setDescription(e.target.value)}
            />
          </div>
          <div className="field wide">
            <label>Policy spec (Cloud Custodian JSON)</label>
            <textarea
              className="policy-editor"
              value={specText}
              spellCheck={false}
              onChange={(e) => setSpecText(e.target.value)}
            />
            <span className="hint">
              A <code>{"{ \"policies\": [ … ] }"}</code> body. The resource type is taken from the
              first policy&apos;s <code>resource</code> (e.g. <code>azure.vm</code>).
            </span>
          </div>
        </div>

        {validation && (
          <div className={`validation ${validation.valid ? "ok" : "bad"}`}>
            {validation.valid ? (
              "✓ Valid — passes Cloud Custodian schema validation."
            ) : (
              <>
                ✗ Invalid:
                <ul>
                  {validation.errors.map((msg, i) => (
                    <li key={i}>{msg}</li>
                  ))}
                </ul>
              </>
            )}
          </div>
        )}

        <div className="form-actions">
          <button onClick={validate} disabled={busy === "validate"}>
            {busy === "validate" ? "Validating…" : "Validate"}
          </button>
          <button className="primary" onClick={save} disabled={busy === "save"}>
            {busy === "save" ? "Saving…" : editingId !== null ? "Update" : "Create"}
          </button>
          {editingId !== null && (
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
            <th>Resource type</th>
            <th>Source</th>
            <th>Status</th>
            <th>State</th>
            <th>Ver</th>
            <th>Actions</th>
          </tr>
        </thead>
        <tbody>
          {policies.map((p) => {
            const b = (k: string) => busy === `${k}:${p.id}`;
            return (
              <tr key={p.id}>
                <td>{p.name}</td>
                <td className="muted">{p.resource_type}</td>
                <td className="muted">{p.source}</td>
                <td>
                  <span className={`badge ${p.validation_status === "valid" ? "valid" : "invalid"}`}>
                    {p.validation_status || "unknown"}
                  </span>
                </td>
                <td>
                  <span className={`badge ${p.enabled ? "approved" : "rejected"}`}>
                    {p.enabled ? "enabled" : "disabled"}
                  </span>
                </td>
                <td className="num">{p.version}</td>
                <td>
                  <div className="row-actions">
                    <button onClick={() => edit(p)}>Edit</button>
                    <button onClick={() => toggle(p)} disabled={b("toggle")}>
                      {p.enabled ? "Disable" : "Enable"}
                    </button>
                    <button className="reject" onClick={() => remove(p)} disabled={b("del")}>
                      Delete
                    </button>
                  </div>
                </td>
              </tr>
            );
          })}
          {policies.length === 0 && !err && (
            <tr>
              <td colSpan={7} className="muted">
                No policies yet. Author one above.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </>
  );
}
