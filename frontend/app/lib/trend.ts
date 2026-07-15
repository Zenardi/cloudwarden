/** Pure helpers for the Overview cost-trend UI (delta badge + sparkline). */

export type DeltaDirection = "up" | "down" | "flat" | "na";

export interface DeltaText {
  /** Glyph carrying direction on its own (paired with text — never colour alone). */
  arrow: string;
  direction: DeltaDirection;
  /** Compact on-screen line, e.g. "+7.5% vs prior 30d" or "vs prior 30d — n/a". */
  label: string;
  /** Full sentence for the badge's accessible name. */
  srLabel: string;
}

/**
 * Format `delta_pct` (from `/api/costs/trend`) into a direction-carrying badge.
 * Direction is conveyed by BOTH the arrow glyph and the sign, so colour is never
 * the only signal (WCAG 1.4.1). `pct === null` (empty prior window) degrades to
 * "n/a" so a first-ever period never shows a bogus percentage.
 */
export function formatDelta(pct: number | null, days: number): DeltaText {
  const window = `prior ${days}d`;
  if (pct === null) {
    return {
      arrow: "•",
      direction: "na",
      label: `vs ${window} — n/a`,
      srLabel: `No prior ${days}-day period to compare against.`,
    };
  }
  const magnitude = `${Math.round(Math.abs(pct) * 10) / 10}%`;
  if (pct > 0) {
    return {
      arrow: "▲",
      direction: "up",
      label: `+${magnitude} vs ${window}`,
      srLabel: `Up ${magnitude} versus the ${window}.`,
    };
  }
  if (pct < 0) {
    return {
      arrow: "▼",
      direction: "down",
      label: `-${magnitude} vs ${window}`,
      srLabel: `Down ${magnitude} versus the ${window}.`,
    };
  }
  return {
    arrow: "→",
    direction: "flat",
    label: `${magnitude} vs ${window}`,
    srLabel: `Unchanged versus the ${window}.`,
  };
}

/**
 * Build the `points` attribute for an SVG `<polyline>` from a daily cost series,
 * scaled to fill a `width`×`height` viewBox (with `pad` breathing room top and
 * bottom so peaks/troughs don't touch the edges). Higher cost sits higher on
 * screen (smaller y). Degrades cleanly:
 *   - empty series -> "" (caller renders no chart)
 *   - single point or an all-equal series -> a flat baseline spanning the width
 *     (avoids divide-by-zero / NaN).
 */
export function sparklinePath(values: number[], width = 100, height = 24, pad = 2): string {
  if (values.length === 0) return "";
  const round = (n: number) => Math.round(n * 100) / 100;
  const midY = round(height / 2);
  const baseline = `0,${midY} ${round(width)},${midY}`;
  if (values.length === 1) return baseline;

  const min = Math.min(...values);
  const max = Math.max(...values);
  if (max === min) return baseline;

  const innerH = height - pad * 2;
  const last = values.length - 1;
  return values
    .map((v, i) => {
      const x = round((i / last) * width);
      const y = round(pad + (1 - (v - min) / (max - min)) * innerH);
      return `${x},${y}`;
    })
    .join(" ");
}
