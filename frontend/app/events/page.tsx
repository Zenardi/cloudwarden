"use client";

import { Fragment, useCallback, useEffect, useState } from "react";
import { fetchRecentEvents, RecentEvent, shortId } from "../lib/api";

function ts(value?: string | null): string {
  if (!value) return "—";
  return value.replace("T", " ").slice(0, 19);
}

function statusClass(status: string): string {
  if (status === "succeeded") return "approved";
  if (status === "failed") return "rejected";
  return "";
}

// Strip the fully-qualified prefix from an event type for a compact label.
function eventLabel(eventType: string): string {
  const parts = eventType.split(".");
  return parts[parts.length - 1] || eventType;
}

const PAGE = 25;

export default function Events() {
  const [events, setEvents] = useState<RecentEvent[]>([]);
  const [offset, setOffset] = useState(0);
  const [err, setErr] = useState("");

  const load = useCallback(async () => {
    try {
      setEvents(await fetchRecentEvents(PAGE, offset));
      setErr("");
    } catch (e) {
      setErr(String(e));
    }
  }, [offset]);

  useEffect(() => {
    load();
  }, [load]);

  return (
    <>
      <h1>Events</h1>
      <p className="sub">
        Real-time enforcement feed — recent Azure Event Grid deliveries and the event-mode
        policy runs they triggered (M6). Newest-first.
      </p>

      <div className="history-controls">
        <button onClick={() => load()}>Refresh</button>
        <button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>
          ‹ Newer
        </button>
        <button disabled={events.length < PAGE} onClick={() => setOffset(offset + PAGE)}>
          Older ›
        </button>
        <span className="muted">
          {offset + 1}–{offset + events.length}
        </span>
      </div>

      {err && <div className="err">{err}</div>}

      <table>
        <thead>
          <tr>
            <th>Event</th>
            <th>Resource</th>
            <th>Subscription</th>
            <th>Received</th>
            <th>Triggered runs</th>
          </tr>
        </thead>
        <tbody>
          {events.map((e) => (
            <Fragment key={e.event_id}>
              <tr>
                <td title={e.event_type}>{eventLabel(e.event_type)}</td>
                <td title={e.resource_id ?? ""}>{e.resource_id ? shortId(e.resource_id) : "—"}</td>
                <td className="muted">{e.subscription_id ? shortId(e.subscription_id) : "—"}</td>
                <td className="muted">{ts(e.received_at)}</td>
                <td>
                  {e.triggered_executions.length === 0 ? (
                    <span className="muted">none</span>
                  ) : (
                    e.triggered_executions.map((x) => (
                      <span key={x.execution_id} className={`badge ${statusClass(x.status)}`}>
                        {x.status}
                      </span>
                    ))
                  )}
                </td>
              </tr>
            </Fragment>
          ))}
          {events.length === 0 && !err && (
            <tr>
              <td colSpan={5} className="muted">
                No events yet.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </>
  );
}
