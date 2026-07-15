import type { AISummary, Recommendation } from "./api";
import type { Loadable } from "./loadable";

export interface SavingsFigure {
  amount: number;
  currency?: string;
  /**
   * Human-readable provenance for the figure — "from AI summary" when the
   * AI-reconciled total is used, or "summed from N recommendations" for the
   * fallback. Surfaced next to the KPI so the number is never a black box.
   */
  basis: string;
}

/**
 * The headline "potential monthly savings" figure and where it came from. Prefers
 * the AI-reconciled total, but falls back to summing the loaded recommendations so
 * an `/api/summary` outage doesn't blank the KPI alongside the summary prose.
 * Returns `null` when neither source has a usable value yet.
 *
 * IMPORTANT: this figure spans ALL clouds — neither `/api/summary` nor
 * `/api/recommendations` is provider-scoped — so a caller under an active cloud
 * filter MUST label it as unscoped rather than imply it matches the filtered cost.
 */
export function deriveSavings(
  summary: Loadable<AISummary | null>,
  recs: Loadable<Recommendation[]>,
): SavingsFigure | null {
  if (
    summary.state === "ok" &&
    summary.data &&
    typeof summary.data.total_potential_savings === "number"
  ) {
    return {
      amount: summary.data.total_potential_savings,
      currency: summary.data.currency,
      basis: "from AI summary",
    };
  }
  if (recs.state === "ok" && recs.data.length > 0) {
    const amount = recs.data.reduce((s, r) => s + (r.est_monthly_savings || 0), 0);
    const n = recs.data.length;
    return {
      amount,
      currency: recs.data[0]?.currency,
      basis: `summed from ${n} recommendation${n === 1 ? "" : "s"}`,
    };
  }
  return null;
}
