"use client";

import { useEffect, useState } from "react";
import { apiGet, listAnomalies, money, shortId, type CostAnomaly } from "../lib/api";
import { prettyType } from "../lib/format";
import { BarList, type BarItem } from "../components/BarList";
import { CostPie, type PieSlice } from "../components/CostPie";

interface Slice {
  resource_type?: string | null;
  location?: string | null;
  resource_id?: string;
  cost: number;
  currency?: string;
}

export default function Costs() {
  const [byType, setByType] = useState<Slice[]>([]);
  const [byRegion, setByRegion] = useState<Slice[]>([]);
  const [byRes, setByRes] = useState<Slice[]>([]);
  const [anomalies, setAnomalies] = useState<CostAnomaly[]>([]);
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    (async () => {
      try {
        const [t, r, res] = await Promise.all([
          apiGet<Slice[]>("/api/costs/by-type"),
          apiGet<Slice[]>("/api/costs/by-region"),
          apiGet<Slice[]>("/api/costs/by-resource?limit=25"),
        ]);
        setByType(t);
        setByRegion(r);
        setByRes(res);
        try {
          // Anomalies are RBAC-gated (anomaly:read) and optional — a denial or
          // empty result must not break the cost explorer.
          setAnomalies(await listAnomalies({ limit: 25 }));
        } catch {
          /* gated or unavailable — leave the anomalies panel hidden */
        }
      } catch (e) {
        setErr(String(e));
      } finally {
        setLoading(false);
      }
    })();
  }, []);

  // Severity chip colours (inline so the panel needs no new stylesheet rules).
  const sevColor: Record<string, string> = {
    critical: "#b91c1c",
    high: "#c2410c",
    medium: "#a16207",
    low: "#4d7c0f",
  };

  // Keys include currency because by-type/by-region rows are grouped by
  // (dimension, currency) — a multi-currency tenant would otherwise collide.
  const typeItems: BarItem[] = byType.map((r) => ({
    key: `${r.resource_type ?? "other"}::${r.currency ?? ""}`,
    label: prettyType(r.resource_type),
    value: r.cost,
    currency: r.currency,
  }));
  const regionItems: BarItem[] = byRegion.map((r) => ({
    key: `${r.location ?? "none"}::${r.currency ?? ""}`,
    label: r.location || "(unassigned)",
    value: r.cost,
    currency: r.currency,
  }));
  const resItems: BarItem[] = byRes.map((r, i) => ({
    key: r.resource_id ?? String(i),
    label: r.resource_id ? shortId(r.resource_id) : "(unassigned)",
    sub: [prettyType(r.resource_type), r.location].filter(Boolean).join(" · ") || undefined,
    value: r.cost,
    currency: r.currency,
  }));

  // Pie composition = cost share by resource type (the app's biggest breakdown).
  const pieCurrency = byType.find((r) => r.currency)?.currency ?? undefined;
  const pieItems: PieSlice[] = byType
    .filter((r) => typeof r.cost === "number" && (r.cost as number) > 0)
    .map((r, i) => ({
      key: r.resource_type ?? `other-${i}`,
      label: prettyType(r.resource_type),
      value: r.cost as number,
    }));

  return (
    <>
      <h1>Cost explorer</h1>
      <p className="sub">Amortized spend over the last 30 days.</p>
      {err && <div className="err">{err}</div>}

      {anomalies.length > 0 && (
        <section className="panel" aria-labelledby="anom-h" style={{ marginBottom: 16 }}>
          <h2 className="panel-title" id="anom-h">
            Cost anomalies
          </h2>
          <p className="sub">
            Days where spend broke sharply from its robust, weekday-aware baseline — with
            the top contributor that drove each spike.
          </p>
          <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "grid", gap: 6 }}>
            {anomalies.map((a) => {
              const top = a.contributors?.[0];
              const factor = a.expected > 0 ? a.actual / a.expected : 0;
              return (
                <li
                  key={a.id}
                  style={{
                    display: "flex",
                    flexWrap: "wrap",
                    alignItems: "baseline",
                    gap: 10,
                    fontSize: 13,
                  }}
                >
                  <span
                    style={{
                      background: sevColor[a.severity] ?? "#4b5563",
                      color: "#fff",
                      borderRadius: 4,
                      padding: "1px 7px",
                      fontSize: 11,
                      textTransform: "uppercase",
                      letterSpacing: 0.4,
                    }}
                  >
                    {a.severity}
                  </span>
                  <strong>
                    {a.scope_type}:{" "}
                    {a.scope_value.startsWith("/") ? shortId(a.scope_value) : a.scope_value}
                  </strong>
                  <span className="muted">{a.usage_date}</span>
                  <span>
                    {money(a.actual, a.currency)} vs ~{money(a.expected, a.currency)}
                  </span>
                  {factor > 0 && <span className="muted">×{factor.toFixed(1)}</span>}
                  {top && <span className="muted">driver: {shortId(String(top.child))}</span>}
                </li>
              );
            })}
          </ul>
        </section>
      )}

      <div className="costs-grid">
        <section className="panel" aria-labelledby="ct-h">
          <h2 className="panel-title" id="ct-h">
            By resource type
          </h2>
          {loading ? <div className="skeleton-row" /> : <BarList items={typeItems} max={12} />}
        </section>

        <section className="panel" aria-labelledby="cres-h">
          <h2 className="panel-title" id="cres-h">
            Top resources
          </h2>
          {loading ? <div className="skeleton-row" /> : <BarList items={resItems} max={12} />}
        </section>

        <section className="panel" aria-labelledby="cshare-h">
          <h2 className="panel-title" id="cshare-h">
            Cost share by type
          </h2>
          {loading ? (
            <div className="skeleton-row" />
          ) : (
            <CostPie items={pieItems} currency={pieCurrency} />
          )}
        </section>

        <section className="panel" aria-labelledby="cr-h">
          <h2 className="panel-title" id="cr-h">
            By region
          </h2>
          {loading ? <div className="skeleton-row" /> : <BarList items={regionItems} max={12} />}
        </section>
      </div>
    </>
  );
}
