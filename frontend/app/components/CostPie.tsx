"use client";

import { money } from "../lib/api";

export interface PieSlice {
  key: string;
  label: string;
  value: number;
}

// Categorical palette tuned for the dark surface — distinct hues; "Other" muted.
const PIE_COLORS = [
  "#4f8cff", // accent blue
  "#3fb968", // green
  "#e0a44a", // amber
  "#c17ee0", // purple
  "#4bc8d2", // teal
  "#e86b6d", // red
];
const OTHER_COLOR = "#6b7a99";

/**
 * Hand-rolled donut (share-of-total) in the app's SVG/CSS chart style. Slices are
 * capped to the top `max` + an aggregated "Other" — pies mislead past ~6 slices.
 * The legend carries label · value · % as text so meaning is never conveyed by
 * colour alone (WCAG 1.4.1 — the fallback pie charts otherwise lack).
 */
export function CostPie({
  items,
  currency,
  max = 5,
}: {
  items: PieSlice[];
  currency?: string;
  max?: number;
}) {
  const clean = items
    .filter((s) => (s.value || 0) > 0)
    .sort((a, b) => b.value - a.value);
  const total = clean.reduce((s, r) => s + r.value, 0);
  if (total <= 0) return <p className="panel-empty">No cost data to chart.</p>;

  const top = clean.slice(0, max);
  const rest = clean.slice(max);
  const restVal = rest.reduce((s, r) => s + r.value, 0);
  // "Remaining N" — distinct from any real resource-type the app already labels
  // "Other" (avoids two identically-named slices when both are present).
  const slices =
    restVal > 0
      ? [...top, { key: "__rest", label: `Remaining ${rest.length}`, value: restVal }]
      : top;

  // Donut via the circumference-100 trick (r = 15.915 → C ≈ 100), so dasharray maps
  // straight to percentages. `25 - cumulative` rotates each arc's start to 12 o'clock.
  let cumulative = 0;
  const arcs = slices.map((s, i) => {
    const pct = (s.value / total) * 100;
    const color = s.key === "__rest" ? OTHER_COLOR : PIE_COLORS[i % PIE_COLORS.length];
    const arc = { ...s, pct, color, dash: `${pct} ${100 - pct}`, offset: 25 - cumulative };
    cumulative += pct;
    return arc;
  });

  const summary = arcs.map((a) => `${a.label} ${Math.round(a.pct)}%`).join(", ");

  return (
    <div className="pie">
      <div className="pie-donut">
        <svg viewBox="0 0 42 42" className="pie-svg" role="img" aria-label={`Cost share by type: ${summary}`}>
          <circle cx="21" cy="21" r="15.915" fill="none" stroke="var(--panel2)" strokeWidth="5" />
          {arcs.map((a) => (
            <circle
              key={a.key}
              cx="21"
              cy="21"
              r="15.915"
              fill="none"
              stroke={a.color}
              strokeWidth="5"
              strokeDasharray={a.dash}
              strokeDashoffset={a.offset}
            />
          ))}
        </svg>
        <div className="pie-center">
          <div className="pie-center-val">{money(total, currency)}</div>
          <div className="pie-center-lbl">total</div>
        </div>
      </div>
      <ul className="pie-legend">
        {arcs.map((a) => (
          <li key={a.key} className="pie-legend-row">
            <span className="pie-swatch" style={{ background: a.color }} aria-hidden="true" />
            <span className="pie-legend-label">{a.label}</span>
            <span className="pie-legend-val">
              {money(a.value, currency)} · {a.pct < 1 ? "<1" : Math.round(a.pct)}%
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}
