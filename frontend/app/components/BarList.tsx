import { money } from "../lib/api";

export interface BarItem {
  key: string;
  label: string;
  /** Optional muted secondary text shown after the label (e.g. a resource's type/region). */
  sub?: string;
  value: number;
  currency?: string;
}

/**
 * A ranked share-of-total horizontal bar chart (reuses the `.bar-*` CSS the
 * Overview "Cost drivers" panel uses). Each row shows a label, its formatted
 * amount + share, and a fill scaled to its share of the largest value — so the
 * longest bar always fills the track and smaller ones read proportionally.
 *
 * The amount/share text carries the meaning (never colour alone); the fill is a
 * transform-scaled div (GPU-composited) with the share exposed to assistive tech
 * via the row's aria-label.
 */
export function BarList({ items, max = 15 }: { items: BarItem[]; max?: number }) {
  const rows = items.slice(0, max);
  const total = items.reduce((s, r) => s + (r.value || 0), 0);
  const peak = rows.reduce((m, r) => Math.max(m, r.value || 0), 0);
  if (rows.length === 0) return <p className="muted">No data.</p>;

  return (
    <div className="bars">
      {rows.map((r) => {
        const share = total > 0 ? r.value / total : 0;
        // Clamp to [0,1]: net-credit rows (cost < 0) would otherwise flip the bar.
        const fill = peak > 0 ? Math.max(0, Math.min(1, r.value / peak)) : 0;
        const pct = share > 0 && Math.round(share * 100) === 0 ? "<1" : Math.round(share * 100);
        return (
          <div
            className="bar-row"
            key={r.key}
            role="img"
            aria-label={`${r.label}${r.sub ? `, ${r.sub}` : ""}: ${money(r.value, r.currency)}, ${pct}% of total`}
          >
            <span className="bar-label" title={r.sub ? `${r.label} · ${r.sub}` : r.label}>
              {r.label}
              {r.sub && <span className="bar-sub"> · {r.sub}</span>}
            </span>
            <span className="bar-val">
              {money(r.value, r.currency)} · {pct}%
            </span>
            <div className="bar-track" aria-hidden="true">
              <div className="bar-fill" style={{ ["--fill" as string]: fill.toFixed(3) }} />
            </div>
          </div>
        );
      })}
    </div>
  );
}
