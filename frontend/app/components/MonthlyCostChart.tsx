"use client";

import { useEffect, useRef, useState } from "react";
import { apiGet, money } from "../lib/api";
import { nextRadioIndex } from "../lib/roving";

const MONTH_OPTIONS = [3, 6, 12] as const;
type MonthsRange = (typeof MONTH_OPTIONS)[number];

interface MonthlyPoint {
  month: string; // "YYYY-MM"
  cost: number;
  currency?: string;
}
interface MonthlyResp {
  months: number;
  currency: string;
  series: MonthlyPoint[];
}

/** "2026-07" → { short: "Jul", long: "July 2026" }. */
function fmtMonth(ym: string): { short: string; long: string } {
  const [y, m] = ym.split("-").map(Number);
  if (!y || !m) return { short: ym, long: ym };
  const d = new Date(Date.UTC(y, m - 1, 1));
  return {
    short: d.toLocaleString("en-US", { month: "short", timeZone: "UTC" }),
    long: d.toLocaleString("en-US", { month: "long", year: "numeric", timeZone: "UTC" }),
  };
}

/**
 * A monthly amortized-spend bar chart with a 3 / 6 / 12-month segmented control
 * (WAI-ARIA radiogroup, roving tabindex — same pattern as the trend RangeControl).
 * Bars are height-scaled to the peak month; each carries its full month + amount
 * as an aria-label so the chart is legible to assistive tech, not colour-only.
 */
export function MonthlyCostChart({ provider = "all" }: { provider?: string }) {
  const [months, setMonths] = useState<MonthsRange>(6);
  const [data, setData] = useState<MonthlyResp | null>(null);
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(true);
  const radios = useRef<(HTMLButtonElement | null)[]>([]);

  useEffect(() => {
    let live = true;
    setLoading(true);
    apiGet<MonthlyResp>(`/api/costs/monthly?months=${months}&provider=${provider}`)
      .then((d) => {
        if (live) {
          setData(d);
          setErr("");
        }
      })
      .catch((e) => live && setErr(String(e)))
      .finally(() => live && setLoading(false));
    return () => {
      live = false;
    };
  }, [months, provider]);

  const series = data?.series ?? [];
  const peak = series.reduce((m, p) => Math.max(m, p.cost || 0), 0);
  const currency = data?.currency;

  return (
    <section className="panel" aria-labelledby="mcc-h">
      <div className="mcc-head">
        <h2 className="panel-title" id="mcc-h">
          Monthly cost
        </h2>
        <div className="control-group">
          <span className="control-label" id="mcc-range-label">
            Period
          </span>
          <div className="range-control" role="radiogroup" aria-labelledby="mcc-range-label">
            {MONTH_OPTIONS.map((m, i) => (
              <button
                key={m}
                ref={(el) => {
                  radios.current[i] = el;
                }}
                type="button"
                role="radio"
                className="range-opt"
                aria-checked={months === m}
                tabIndex={months === m ? 0 : -1}
                disabled={loading}
                onClick={() => setMonths(m)}
                onKeyDown={(e) => {
                  const n = nextRadioIndex(e.key, i, MONTH_OPTIONS.length);
                  if (n === null) return;
                  e.preventDefault();
                  setMonths(MONTH_OPTIONS[n]);
                  radios.current[n]?.focus();
                }}
              >
                {m}m
              </button>
            ))}
          </div>
        </div>
      </div>

      {err && <div className="err">{err}</div>}
      {loading ? (
        <div className="skeleton-row" style={{ height: 180 }} />
      ) : series.length === 0 ? (
        <p className="muted">
          No monthly data yet — collected cost history spans less than a month. More months
          appear here as cost data accumulates over time.
        </p>
      ) : (
        <div className="mcc-chart" role="group" aria-label={`Monthly amortized cost, last ${months} months`}>
          {series.map((p) => {
            const label = fmtMonth(p.month);
            // Clamp to [0,1]: a net-credit month (cost < 0) would give a negative height.
            const h = peak > 0 ? Math.max(0, Math.min(1, p.cost / peak)) : 0;
            return (
              <div className="mcc-col" key={p.month}>
                <div className="mcc-plot">
                  <div
                    className="mcc-bar"
                    style={{ ["--h" as string]: h.toFixed(3) }}
                    role="img"
                    aria-label={`${label.long}: ${money(p.cost, currency)}`}
                    title={`${label.long}: ${money(p.cost, currency)}`}
                  />
                </div>
                <div className="mcc-val">{money(p.cost, currency)}</div>
                <div className="mcc-xlabel">{label.short}</div>
              </div>
            );
          })}
        </div>
      )}
    </section>
  );
}
