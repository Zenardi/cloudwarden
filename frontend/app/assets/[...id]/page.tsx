"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import {
  Asset,
  AssetEvent,
  AssetRelationship,
  getAsset,
  getAssetHistory,
  getAssetRelationships,
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
  const [loading, setLoading] = useState(true);
  const [notFound, setNotFound] = useState(false);
  const [err, setErr] = useState("");

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

      <h2>Config</h2>
      <pre className="policy-editor" style={{ minHeight: "auto" }}>
        {JSON.stringify(asset.config ?? {}, null, 2)}
      </pre>
    </>
  );
}
