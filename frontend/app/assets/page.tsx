"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { Asset, buildAssetQuery, PROVIDERS, queryAssets, shortId } from "../lib/api";

const PAGE_SIZE = 50;

export default function Assets() {
  const router = useRouter();
  const [provider, setProvider] = useState("all");
  const [type, setType] = useState("");
  const [location, setLocation] = useState("");
  const [contains, setContains] = useState("");
  const [tagKey, setTagKey] = useState("");
  const [tagValue, setTagValue] = useState("");
  const [offset, setOffset] = useState(0);
  const [assets, setAssets] = useState<Asset[]>([]);
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const q = buildAssetQuery({
        provider,
        type,
        location,
        contains,
        tagKey,
        tagValue,
        limit: PAGE_SIZE,
        offset,
      });
      setAssets(await queryAssets(q));
      setErr("");
    } catch (e) {
      setErr(String(e));
      setAssets([]);
    } finally {
      setLoading(false);
    }
  }, [provider, type, location, contains, tagKey, tagValue, offset]);

  useEffect(() => {
    load();
  }, [load]);

  // Applying filters restarts pagination from the first page.
  function apply(e: FormEvent) {
    e.preventDefault();
    setOffset(0);
  }

  const page = Math.floor(offset / PAGE_SIZE) + 1;
  const hasPrev = offset > 0;
  const hasNext = assets.length === PAGE_SIZE;

  return (
    <>
      <h1>Assets</h1>
      <p className="sub">
        Query the AssetDB inventory and drill into a single asset&apos;s config,
        relationships and change history.
      </p>

      <form className="history-controls" onSubmit={apply}>
        <div className="field">
          <label htmlFor="f-provider">Cloud</label>
          <select id="f-provider" value={provider} onChange={(e) => setProvider(e.target.value)}>
            <option value="all">All clouds</option>
            {PROVIDERS.map((p) => (
              <option key={p} value={p}>
                {p}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label htmlFor="f-type">Type</label>
          <input
            id="f-type"
            value={type}
            placeholder="microsoft.compute/virtualmachines"
            onChange={(e) => setType(e.target.value)}
          />
        </div>
        <div className="field">
          <label htmlFor="f-location">Location</label>
          <input
            id="f-location"
            value={location}
            placeholder="eastus"
            onChange={(e) => setLocation(e.target.value)}
          />
        </div>
        <div className="field">
          <label htmlFor="f-contains">Id contains</label>
          <input
            id="f-contains"
            value={contains}
            placeholder="vm-web"
            onChange={(e) => setContains(e.target.value)}
          />
        </div>
        <div className="field">
          <label htmlFor="f-tagk">Tag key</label>
          <input id="f-tagk" value={tagKey} onChange={(e) => setTagKey(e.target.value)} />
        </div>
        <div className="field">
          <label htmlFor="f-tagv">Tag value</label>
          <input id="f-tagv" value={tagValue} onChange={(e) => setTagValue(e.target.value)} />
        </div>
        <button type="submit" className="primary">
          Search
        </button>
      </form>

      {err && <div className="err">{err}</div>}

      <table>
        <thead>
          <tr>
            <th>Name</th>
            <th>Cloud</th>
            <th>Type</th>
            <th>Location</th>
            <th>State</th>
            <th className="num">Tags</th>
            <th>Resource id</th>
          </tr>
        </thead>
        <tbody>
          {assets.map((a) => (
            <tr
              key={a.resource_id}
              className="clickable"
              onClick={() => router.push(`/assets${a.resource_id}`)}
            >
              <td>{a.name || shortId(a.resource_id)}</td>
              <td>
                <span className="badge">{a.provider ?? "azure"}</span>
              </td>
              <td className="muted">{a.type ?? "—"}</td>
              <td>{a.location ?? "—"}</td>
              <td>{a.state ? <span className="badge">{a.state}</span> : "—"}</td>
              <td className="num">{Object.keys(a.tags ?? {}).length}</td>
              <td className="muted" title={a.resource_id}>
                {shortId(a.resource_id)}
              </td>
            </tr>
          ))}
          {assets.length === 0 && !err && (
            <tr>
              <td colSpan={7} className="muted">
                {loading ? "Loading…" : "No assets match this query."}
              </td>
            </tr>
          )}
        </tbody>
      </table>

      <div className="form-actions">
        <button type="button" onClick={() => setOffset(offset - PAGE_SIZE)} disabled={!hasPrev}>
          ← Prev
        </button>
        <span className="muted">Page {page}</span>
        <button type="button" onClick={() => setOffset(offset + PAGE_SIZE)} disabled={!hasNext}>
          Next →
        </button>
      </div>
    </>
  );
}
