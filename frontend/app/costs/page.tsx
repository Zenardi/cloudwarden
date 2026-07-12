"use client";

import { useEffect, useState } from "react";
import { apiGet, money, shortId } from "../lib/api";

export default function Costs() {
  const [byType, setByType] = useState<any[]>([]);
  const [byRegion, setByRegion] = useState<any[]>([]);
  const [byRes, setByRes] = useState<any[]>([]);
  const [err, setErr] = useState("");

  useEffect(() => {
    (async () => {
      try {
        setByType(await apiGet("/api/costs/by-type"));
        setByRegion(await apiGet("/api/costs/by-region"));
        setByRes(await apiGet("/api/costs/by-resource?limit=25"));
      } catch (e) {
        setErr(String(e));
      }
    })();
  }, []);

  return (
    <>
      <h1>Cost explorer</h1>
      <p className="sub">Amortized spend over the last 30 days.</p>
      {err && <div className="err">{err}</div>}

      <h2>By resource type</h2>
      <table>
        <thead><tr><th>Type</th><th className="num">Cost</th></tr></thead>
        <tbody>
          {byType.map((r, i) => (
            <tr key={i}><td>{r.resource_type}</td><td className="num">{money(r.cost, r.currency)}</td></tr>
          ))}
        </tbody>
      </table>

      <h2>By region</h2>
      <table>
        <thead><tr><th>Region</th><th className="num">Cost</th></tr></thead>
        <tbody>
          {byRegion.map((r, i) => (
            <tr key={i}><td>{r.location}</td><td className="num">{money(r.cost, r.currency)}</td></tr>
          ))}
        </tbody>
      </table>

      <h2>Top resources</h2>
      <table>
        <thead><tr><th>Resource</th><th>Type</th><th>Region</th><th className="num">Cost</th></tr></thead>
        <tbody>
          {byRes.map((r, i) => (
            <tr key={i}>
              <td>{shortId(r.resource_id)}</td>
              <td className="muted">{r.resource_type}</td>
              <td>{r.location}</td>
              <td className="num">{money(r.cost, r.currency)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}
