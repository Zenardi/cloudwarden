import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { CostTrend as CostTrendData } from "../lib/api";
import { CostTrend } from "./CostTrend";

function trendData(over: Partial<CostTrendData> = {}): CostTrendData {
  return {
    days: 30,
    currency: "USD",
    total: 100,
    prior_total: 80,
    delta: 20,
    delta_pct: 25,
    series: [
      { date: "2026-07-01", cost: 10 },
      { date: "2026-07-02", cost: 20 },
    ],
    ...over,
  };
}

describe("CostTrend", () => {
  it("CostTrend_renders_up_arrow_and_accessible_label", () => {
    render(<CostTrend trend={{ state: "ok", data: trendData({ delta_pct: 7.5 }) }} />);
    expect(screen.getByText("▲")).toBeInTheDocument();
    expect(screen.getByRole("img")).toHaveAccessibleName(/up 7\.5%/i);
  });

  it("CostTrend_renders_down_arrow_for_negative", () => {
    render(<CostTrend trend={{ state: "ok", data: trendData({ delta_pct: -3.2 }) }} />);
    expect(screen.getByText("▼")).toBeInTheDocument();
    expect(screen.getByRole("img")).toHaveAccessibleName(/down 3\.2%/i);
  });

  it("cost_kpi_hides_delta_gracefully_on_trend_error", () => {
    const { container } = render(<CostTrend trend={{ state: "error", message: "boom" }} />);
    expect(container).toBeEmptyDOMElement(); // never a fabricated delta on failure
  });

  it("renders nothing while the trend is still loading", () => {
    const { container } = render(<CostTrend trend={{ state: "loading" }} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("shows n/a (and no sparkline) when there is no prior period", () => {
    render(<CostTrend trend={{ state: "ok", data: trendData({ delta_pct: null, series: [] }) }} />);
    expect(screen.getByText(/n\/a/i)).toBeInTheDocument();
    expect(document.querySelector("polyline")).toBeNull();
  });
});
