"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import {
  Asset,
  AssetEvent,
  AssetRelationship,
  DriftFinding,
  getAsset,
  getAssetHistory,
  getAssetRelationships,
  listDrift,
  rebaselineDrift,
  shortId,
} from "../../lib/api";

function ts(value?: string | null): string {
  if (!value) return "—";
  return value.replace("T", " ").slice(0, 19);
}

export default function AssetDetail() {
  const params = useParams();
  // Catch-all route: `id` is the resource-id path split on "/". Rebuild the stored
  // (leading-slash, lower-cased) id the M4.2/M4.3/M4.4 APIs expect.
  const resourceId = useMemo(() => {
    const raw = params?.id;
    const segments = Array.isArray(raw) ? raw : raw ? [raw] : [];
    return "/" + segments.map((s) => decodeURIComponent(s)).join("/");
  }, [params]);

  const [asset, setAsset] = useState<Asset | null>(null);
  const [rels, setRels] = useState<AssetRelationship[]>([]);
  const [history, setHistory] = useState<AssetEvent[]>([]);
  const [drift, setDrift] = useState<DriftFinding[]>([]);
  const [rebaselining, setRebaselining] = useState(false);
  const [loading, setLoading] = useState(true);
  const [notFound, setNotFound] = useState(false);
  const [err, setErr] = useState("");

  const loadDrift = async (rid: string) => {
    try {
      // Drift is RBAC-gated (drift:read) and optional — a denial must not break the page.
      setDrift(await listDrift({ resource_id: rid, status: "open" }));
    } catch {
      /* gated or unavailable — leave the drift section empty */
    }
  };

  const onRebaseline = async () => {
    setRebaselining(true);
    try {
      await rebaselineDrift(resourceId);
      await loadDrift(resourceId);
    } catch (e) {
      setErr(String(e));
    } finally {
      setRebaselining(false);
    }
  };

  useEffect(() => {
    if (resourceId === "/") return;
    let active = true;
    (async () => {
      setLoading(true);
      setNotFound(false);
      setErr("");
      try {
        const a = await getAsset(resourceId);
        if (!active) return;
        if (!a) {
          setNotFound(true);
          return;
        }
        setAsset(a);
        const [r, h] = await Promise.all([
          getAssetRelationships(resourceId),
          getAssetHistory(resourceId),
        ]);
        if (!active) return;
        setRels(r);
        setHistory(h);
        await loadDrift(resourceId);
      } catch (e) {
        if (active) setErr(String(e));
      } finally {
        if (active) setLoading(false);
      }
    })();
    return () => {
      active = false;
    };
  }, [resourceId]);

  if (loading) {
    return (
      <>
        <p className="sub">
          <Link href="/assets">← Assets</Link>
        </p>
        <p className="muted">Loading…</p>
      </>
    );
  }

  if (notFound) {
    return (
      <>
        <p className="sub">
          <Link href="/assets">← Assets</Link>
        </p>
        <h1>Asset not found</h1>
        <div className="panel-form">
          <p className="muted" title={resourceId}>
            No asset matches <code>{shortId(resourceId)}</code>. It may have been deleted, or
            never ingested into the AssetDB.
          </p>
        </div>
      </>
    );
  }

  if (err) {
    return (
      <>
        <p className="sub">
          <Link href="/assets">← Assets</Link>
        </p>
        <div className="err">{err}</div>
      </>
    );
  }

  if (!asset) return null;

  return (
    <>
      <p className="sub">
        <Link href="/assets">← Assets</Link>
      </p>
      <h1>{asset.name || shortId(asset.resource_id)}</h1>
      <p className="sub" title={asset.resource_id}>
        {asset.resource_id}
      </p>

      <div className="cards asset-facts">
        <div className="card asset-fact-wide">
          <div className="label">Type</div>
          <div className="value" style={{ fontSize: 15 }}>
            {asset.type ?? "—"}
          </div>
        </div>
        <div className="card">
          <div className="label">Location</div>
          <div className="value" style={{ fontSize: 15 }}>
            {asset.location ?? "—"}
          </div>
        </div>
        <div className="card">
          <div className="label">State</div>
          <div className="value" style={{ fontSize: 15 }}>
            {asset.state ?? "—"}
          </div>
        </div>
        <div className="card">
          <div className="label">Resource group</div>
          <div className="value" style={{ fontSize: 15 }}>
            {asset.resource_group ?? "—"}
          </div>
        </div>
      </div>

      {Object.keys(asset.tags ?? {}).length > 0 && (
        <>
          <h2>Tags</h2>
          <div className="chips">
            {Object.entries(asset.tags).map(([k, v]) => (
              <span className="chip" key={k}>
                {k}: {v}
              </span>
            ))}
          </div>
        </>
      )}

      <h2>Relationships</h2>
      <table>
        <thead>
          <tr>
            <th>Direction</th>
            <th>Kind</th>
            <th>Neighbour</th>
          </tr>
        </thead>
        <tbody>
          {rels.map((r) => (
            <tr key={r.id}>
              <td>
                <span className="badge">{r.direction}</span>
              </td>
              <td>{r.kind}</td>
              <td>
                <Link href={`/assets${r.neighbor}`} title={r.neighbor}>
                  {shortId(r.neighbor)}
                </Link>
              </td>
            </tr>
          ))}
          {rels.length === 0 && (
            <tr>
              <td colSpan={3} className="muted">
                No relationships.
              </td>
            </tr>
          )}
        </tbody>
      </table>

      <h2>Change history</h2>
      <table>
        <thead>
          <tr>
            <th>When</th>
            <th>Event</th>
            <th>Operation</th>
            <th>Actor</th>
          </tr>
        </thead>
        <tbody>
          {history.map((h) => (
            <tr key={h.id}>
              <td className="muted">{ts(h.at)}</td>
              <td>
                <span className="badge">{h.event_type}</span>
              </td>
              <td>{h.data?.operation ?? "—"}</td>
              <td className="muted">{h.data?.actor ?? "—"}</td>
            </tr>
          ))}
          {history.length === 0 && (
            <tr>
              <td colSpan={4} className="muted">
                No change history.
              </td>
            </tr>
          )}
        </tbody>
      </table>

      <div style={{ display: "flex", alignItems: "center", gap: 12, marginTop: 8 }}>
        <h2 style={{ margin: 0 }}>Configuration drift</h2>
        <button className="btn" onClick={onRebaseline} disabled={rebaselining}>
          {rebaselining ? "Re-baselining…" : "Re-baseline (accept)"}
        </button>
      </div>
      <p className="sub">
        Live config diffed against this resource&apos;s desired-state baseline. Re-baselining
        accepts the current config as the new intended state.
      </p>
      {drift.length === 0 ? (
        <p className="muted">No drift — the resource matches its baseline.</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Field</th>
              <th>Change</th>
              <th>Baseline</th>
              <th>Current</th>
            </tr>
          </thead>
          <tbody>
            {drift.flatMap((f) =>
              f.changes.map((c) => (
                <tr key={`${f.id}-${c.path}`}>
                  <td>
                    <code>{c.path}</code>
                  </td>
                  <td>
                    <span
                      className="badge"
                      style={{
                        background:
                          c.kind === "added"
                            ? "#166534"
                            : c.kind === "removed"
                              ? "#991b1b"
                              : "#a16207",
                        color: "#fff",
                      }}
                    >
                      {c.kind}
                    </span>
                  </td>
                  <td className="muted">{c.old === undefined ? "—" : JSON.stringify(c.old)}</td>
                  <td>{c.new === undefined ? "—" : JSON.stringify(c.new)}</td>
                </tr>
              )),
            )}
          </tbody>
        </table>
      )}

      <h2>Config</h2>
      <pre className="policy-editor" style={{ minHeight: "auto" }}>
        {JSON.stringify(asset.config ?? {}, null, 2)}
      </pre>
    </>
  );
}
