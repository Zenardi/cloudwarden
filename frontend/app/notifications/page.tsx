"use client";

import { useCallback, useEffect, useState } from "react";
import {
  ApiError,
  NotificationChannel,
  NotificationTemplate,
  TRANSPORTS,
  createNotificationChannel,
  createNotificationTemplate,
  deleteNotificationChannel,
  deleteNotificationTemplate,
  listNotificationChannels,
  listNotificationTemplates,
  updateNotificationChannel,
} from "../lib/api";

export default function Notifications() {
  const [channels, setChannels] = useState<NotificationChannel[]>([]);
  const [templates, setTemplates] = useState<NotificationTemplate[]>([]);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState("");

  // channel form
  const [cName, setCName] = useState("");
  const [cTransport, setCTransport] = useState<string>("webhook");
  const [cTarget, setCTarget] = useState("");

  // template form
  const [tName, setTName] = useState("");
  const [tSubject, setTSubject] = useState("");
  const [tBody, setTBody] = useState("");

  const load = useCallback(async () => {
    try {
      const [cs, ts] = await Promise.all([
        listNotificationChannels(),
        listNotificationTemplates(),
      ]);
      setChannels(cs);
      setTemplates(ts);
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

  async function createChannel() {
    setErr("");
    if (!cName.trim() || !cTarget.trim()) {
      setErr("Channel name and target are required.");
      return;
    }
    setBusy("create-channel");
    try {
      await createNotificationChannel({
        name: cName.trim(),
        transport: cTransport,
        target: cTarget.trim(),
      });
      setCName("");
      setCTarget("");
      setCTransport("webhook");
      await load();
    } catch (e) {
      if (e instanceof ApiError && e.status === 400) {
        setErr(`Channel rejected: ${e.body?.detail ?? "invalid"}.`);
      } else {
        setErr(String(e));
      }
    } finally {
      setBusy("");
    }
  }

  async function createTemplate() {
    setErr("");
    if (!tName.trim() || !tBody.trim()) {
      setErr("Template name and body are required.");
      return;
    }
    setBusy("create-template");
    try {
      await createNotificationTemplate({
        name: tName.trim(),
        subject: tSubject.trim() || null,
        body: tBody,
      });
      setTName("");
      setTSubject("");
      setTBody("");
      await load();
    } catch (e) {
      if (e instanceof ApiError && e.status === 400) {
        setErr(`Template rejected: ${e.body?.detail ?? "invalid"}.`);
      } else {
        setErr(String(e));
      }
    } finally {
      setBusy("");
    }
  }

  const toggleChannel = (c: NotificationChannel) =>
    act(`toggle:${c.id}`, () => updateNotificationChannel(c.id, { enabled: !c.enabled }));

  const removeChannel = (c: NotificationChannel) => {
    if (!window.confirm(`Delete channel "${c.name}"?`)) return;
    return act(`del-c:${c.id}`, () => deleteNotificationChannel(c.id));
  };

  const removeTemplate = (t: NotificationTemplate) => {
    if (!window.confirm(`Delete template "${t.name}"?`)) return;
    return act(`del-t:${t.id}`, () => deleteNotificationTemplate(t.id));
  };

  return (
    <>
      <h1>Notifications</h1>
      <p className="sub">
        Manage the <strong>channels</strong> (where notifications go — Slack, email, Teams, Jira,
        ServiceNow or a generic webhook) and the <strong>templates</strong> (what they say, rendered
        from the policy-violation context in a sandboxed environment). Attach a channel + template to
        a binding so a policy violation fires a notification (à la Stacklet / c7n-mailer).
      </p>

      {err && <div className="err">{err}</div>}

      {/* Channels ------------------------------------------------------------ */}
      <div className="panel-form">
        <h2 style={{ marginTop: 0 }}>New channel</h2>
        <div className="form-grid">
          <div className="field">
            <label>Name *</label>
            <input value={cName} placeholder="ops-slack" onChange={(e) => setCName(e.target.value)} />
          </div>
          <div className="field">
            <label>Transport *</label>
            <select value={cTransport} onChange={(e) => setCTransport(e.target.value)}>
              {TRANSPORTS.map((t) => (
                <option key={t} value={t}>
                  {t}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label>Target *</label>
            <input
              value={cTarget}
              placeholder="https://hooks.slack.com/… or ops@corp.com"
              onChange={(e) => setCTarget(e.target.value)}
            />
          </div>
        </div>
        <div className="form-actions">
          <button className="primary" onClick={createChannel} disabled={busy === "create-channel"}>
            {busy === "create-channel" ? "Creating…" : "Create channel"}
          </button>
        </div>
      </div>

      <table>
        <thead>
          <tr>
            <th>Channel</th>
            <th>Transport</th>
            <th>Target</th>
            <th>Enabled</th>
            <th>Actions</th>
          </tr>
        </thead>
        <tbody>
          {channels.map((c) => (
            <tr key={c.id}>
              <td>{c.name}</td>
              <td>
                <span className="badge">{c.transport}</span>
              </td>
              <td className="muted">{c.target}</td>
              <td>
                <label className="muted">
                  <input
                    type="checkbox"
                    checked={c.enabled}
                    onChange={() => toggleChannel(c)}
                    disabled={busy === `toggle:${c.id}`}
                  />{" "}
                  {c.enabled ? "on" : "off"}
                </label>
              </td>
              <td>
                <div className="row-actions">
                  <button
                    className="reject"
                    onClick={() => removeChannel(c)}
                    disabled={busy === `del-c:${c.id}`}
                  >
                    Delete
                  </button>
                </div>
              </td>
            </tr>
          ))}
          {channels.length === 0 && !err && (
            <tr>
              <td colSpan={5} className="muted">
                No channels yet. Create one above.
              </td>
            </tr>
          )}
        </tbody>
      </table>

      {/* Templates ---------------------------------------------------------- */}
      <div className="panel-form" style={{ marginTop: 24 }}>
        <h2 style={{ marginTop: 0 }}>New template</h2>
        <div className="form-grid">
          <div className="field">
            <label>Name *</label>
            <input value={tName} placeholder="violation" onChange={(e) => setTName(e.target.value)} />
          </div>
          <div className="field">
            <label>Subject</label>
            <input
              value={tSubject}
              placeholder="[{{ policy_name }}] {{ count }} violation(s)"
              onChange={(e) => setTSubject(e.target.value)}
            />
          </div>
        </div>
        <div className="field" style={{ marginTop: 12 }}>
          <label>Body *</label>
          <textarea
            value={tBody}
            rows={4}
            placeholder="Policy {{ policy_name }} matched {{ resource_id }} ({{ count }} total)."
            onChange={(e) => setTBody(e.target.value)}
          />
        </div>
        <div className="form-actions">
          <button
            className="primary"
            onClick={createTemplate}
            disabled={busy === "create-template"}
          >
            {busy === "create-template" ? "Creating…" : "Create template"}
          </button>
        </div>
      </div>

      <table>
        <thead>
          <tr>
            <th>Template</th>
            <th>Subject</th>
            <th>Body</th>
            <th>Actions</th>
          </tr>
        </thead>
        <tbody>
          {templates.map((t) => (
            <tr key={t.id}>
              <td>{t.name}</td>
              <td className="muted">{t.subject || <span className="muted">—</span>}</td>
              <td className="muted" style={{ maxWidth: 380, whiteSpace: "pre-wrap" }}>
                {t.body}
              </td>
              <td>
                <div className="row-actions">
                  <button
                    className="reject"
                    onClick={() => removeTemplate(t)}
                    disabled={busy === `del-t:${t.id}`}
                  >
                    Delete
                  </button>
                </div>
              </td>
            </tr>
          ))}
          {templates.length === 0 && !err && (
            <tr>
              <td colSpan={4} className="muted">
                No templates yet. Create one above.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </>
  );
}
