import type { CostTrend as CostTrendData } from "../lib/api";
import type { Loadable } from "../lib/loadable";
import { formatDelta } from "../lib/trend";
import { Sparkline } from "./Sparkline";

/**
 * The Cost KPI's trend row: a direction-carrying delta badge (arrow + sign +
 * text — never colour alone) beside an inline sparkline of the daily series.
 * Renders nothing unless the trend genuinely loaded, so a loading/failed fetch
 * never fabricates a delta on the KPI.
 */
export function CostTrend({ trend }: { trend: Loadable<CostTrendData> }) {
  if (trend.state !== "ok") return null;

  const { delta_pct, days, series } = trend.data;
  const delta = formatDelta(delta_pct, days);
  const values = series.map((p) => p.cost);

  return (
    <div className={`cost-trend dir-${delta.direction}`}>
      <span className="delta" role="img" aria-label={delta.srLabel}>
        <span className="delta-arrow" aria-hidden="true">
          {delta.arrow}
        </span>
        <span className="delta-text">{delta.label}</span>
      </span>
      <Sparkline values={values} />
    </div>
  );
}
