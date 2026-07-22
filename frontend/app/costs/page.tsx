"use client";

import { useEffect, useState } from "react";
import { apiGet, shortId } from "../lib/api";
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
      } catch (e) {
        setErr(String(e));
      } finally {
        setLoading(false);
      }
    })();
  }, []);

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
