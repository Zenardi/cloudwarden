import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import Overview from "../page";
import { RefreshStatus } from "./RefreshStatus";

describe("RefreshStatus", () => {
  it("renders a visually-hidden polite status region carrying the message", () => {
    render(<RefreshStatus message="Data refreshed, as of just now." />);
    const region = screen.getByRole("status");
    expect(region).toHaveClass("sr-only");
    expect(region).toHaveTextContent("Data refreshed, as of just now.");
  });

  it("is present but empty before any refresh (nothing stale to announce)", () => {
    render(<RefreshStatus message="" />);
    expect(screen.getByRole("status")).toBeEmptyDOMElement();
  });
});

// --- Overview-level a11y regression guards (fetch mocked) -------------------- //

function mockFetch() {
  return vi.fn(async (url: string | URL) => {
    const u = String(url);
    const body = u.includes("/api/costs/trend")
      ? { days: 30, currency: "USD", total: 100, prior_total: 80, delta: 20, delta_pct: 25, series: [{ date: "2026-07-01", cost: 10 }] }
      : u.includes("/api/costs/summary")
        ? { total: 100, by_type: [], by_region: [] }
        : u.includes("/api/summary")
          ? { executive_summary: "All good.", total_potential_savings: 0, currency: "USD", provider: "stub", model: "m" }
          : u.includes("/api/runs/latest")
            ? { run_id: "r1", status: "succeeded", started_at: "2026-07-14T00:00:00Z", finished_at: "2026-07-14T00:01:00Z", mock: true }
            : u.includes("/api/recommendations")
              ? []
              : u.includes("/api/governance/posture")
                ? { totals: { compliant: 0, non_compliant: 0, violations: 0, evaluated: 0 }, by_policy: [], by_subscription: [], by_collection: [], by_provider: [] }
                : {};
    return { ok: true, status: 200, json: async () => body } as Response;
  });
}

describe("Overview a11y", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", mockFetch());
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("refresh_announces_single_status_message", async () => {
    render(<Overview />);
    // Exactly one polite status message, announced once — not the whole KPI trio.
    expect(await screen.findByText(/data refreshed/i)).toBeInTheDocument();
    expect(screen.getAllByRole("status")).toHaveLength(1);
  });

  it("kpi_container_has_no_aria_live", async () => {
    const { container } = render(<Overview />);
    await screen.findByText(/data refreshed/i);
    const kpis = container.querySelector(".cards.kpis");
    expect(kpis).not.toBeNull();
    expect(kpis).not.toHaveAttribute("aria-live"); // no wholesale re-read
    expect(kpis).toHaveAttribute("aria-busy"); // busy state retained
  });

  it("summary_container_has_no_aria_live", async () => {
    const { container } = render(<Overview />);
    const summary = await screen.findByText("All good.");
    expect(summary.closest(".summary")?.parentElement).not.toHaveAttribute("aria-live");
    // The single live region left in the tree is the dedicated status region.
    const live = container.querySelectorAll("[aria-live]");
    expect(live).toHaveLength(1);
    expect(live[0]).toHaveAttribute("role", "status");
  });

  it("amortization_caveat_still_screenreader_reachable", async () => {
    const { container } = render(<Overview />);
    await screen.findByText(/data refreshed/i);
    const caveat = document.getElementById("cost-amortized-caveat");
    expect(caveat).not.toBeNull();
    expect(caveat).toHaveClass("sr-only");
    expect(container.querySelector('[aria-describedby="cost-amortized-caveat"]')).not.toBeNull();
    await waitFor(() => expect(document.querySelector(".cards.kpis")).not.toBeNull());
  });
});
