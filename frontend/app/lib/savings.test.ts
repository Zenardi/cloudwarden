import { describe, expect, it } from "vitest";
import { deriveSavings } from "./savings";
import type { AISummary, Recommendation } from "./api";
import type { Loadable } from "./loadable";

function rec(over: Partial<Recommendation>): Recommendation {
  return {
    id: 1,
    resource_id: "/subs/x/rg/y/vm/z",
    category: "rightsizing",
    action: "resize_vm",
    risk: "low",
    confidence: 0.9,
    est_monthly_savings: 0,
    source: "ai",
    priority: 1,
    status: "open",
    ...over,
  };
}

const ok = <T,>(data: T): Loadable<T> => ({ state: "ok", data });
const loading = { state: "loading" } as const;
const error = { state: "error", message: "boom" } as const;

describe("deriveSavings", () => {
  it("prefers the AI-reconciled total and labels its basis", () => {
    const summary = ok<AISummary | null>({
      executive_summary: "…",
      total_potential_savings: 1234.5,
      currency: "USD",
    });
    const s = deriveSavings(summary, ok<Recommendation[]>([]));
    expect(s).toEqual({ amount: 1234.5, currency: "USD", basis: "from AI summary" });
  });

  it("falls back to summing recommendations when the summary has no total", () => {
    const summary = ok<AISummary | null>(null);
    const recs = ok<Recommendation[]>([
      rec({ id: 1, est_monthly_savings: 100, currency: "EUR" }),
      rec({ id: 2, est_monthly_savings: 50 }),
      rec({ id: 3, est_monthly_savings: 25 }),
    ]);
    const s = deriveSavings(summary, recs);
    expect(s).toEqual({ amount: 175, currency: "EUR", basis: "summed from 3 recommendations" });
  });

  it("uses the singular noun for a single recommendation", () => {
    const recs = ok<Recommendation[]>([rec({ est_monthly_savings: 42 })]);
    const s = deriveSavings(ok<AISummary | null>(null), recs);
    expect(s?.basis).toBe("summed from 1 recommendation");
  });

  it("treats a missing est_monthly_savings as zero in the sum", () => {
    const recs = ok<Recommendation[]>([
      rec({ id: 1, est_monthly_savings: undefined as unknown as number }),
      rec({ id: 2, est_monthly_savings: 10 }),
    ]);
    expect(deriveSavings(ok<AISummary | null>(null), recs)?.amount).toBe(10);
  });

  it("prefers the AI total even when recommendations are also present", () => {
    const summary = ok<AISummary | null>({
      executive_summary: "…",
      total_potential_savings: 900,
      currency: "USD",
    });
    const recs = ok<Recommendation[]>([rec({ est_monthly_savings: 1 })]);
    expect(deriveSavings(summary, recs)?.basis).toBe("from AI summary");
  });

  it("returns null while sources are still loading", () => {
    expect(deriveSavings(loading, loading)).toBeNull();
  });

  it("returns null when the summary errors and there are no recommendations", () => {
    expect(deriveSavings(error, ok<Recommendation[]>([]))).toBeNull();
  });

  it("still sums recommendations when the summary itself errored", () => {
    const recs = ok<Recommendation[]>([rec({ est_monthly_savings: 60 })]);
    expect(deriveSavings(error, recs)?.amount).toBe(60);
  });
});
