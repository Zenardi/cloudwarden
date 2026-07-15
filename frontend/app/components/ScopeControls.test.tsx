import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { costScopeQuery } from "../lib/api";
import Overview from "../page";
import { ScopeControls } from "./ScopeControls";

describe("ScopeControls", () => {
  it("renders the cloud options as radios and marks the active one", () => {
    const onChange = vi.fn();
    render(<ScopeControls value="all" onChange={onChange} />);
    expect(screen.getByRole("radio", { name: "All" })).toHaveAttribute("aria-checked", "true");
    expect(screen.getByRole("radio", { name: "Azure" })).toHaveAttribute("aria-checked", "false");
    fireEvent.click(screen.getByRole("radio", { name: "AWS" }));
    expect(onChange).toHaveBeenCalledWith("aws");
  });

  it("moves selection with arrow keys and ignores other keys (roving radiogroup)", () => {
    const onChange = vi.fn();
    render(<ScopeControls value="all" onChange={onChange} />);
    fireEvent.keyDown(screen.getByRole("radio", { name: "All" }), { key: "ArrowRight" });
    expect(onChange).toHaveBeenCalledWith("azure");
    onChange.mockClear();
    fireEvent.keyDown(screen.getByRole("radio", { name: "All" }), { key: "a" });
    expect(onChange).not.toHaveBeenCalled();
  });

  it("keeps only the selected radio in the tab order (roving tabindex)", () => {
    render(<ScopeControls value="azure" onChange={() => {}} />);
    expect(screen.getByRole("radio", { name: "Azure" })).toHaveAttribute("tabindex", "0");
    expect(screen.getByRole("radio", { name: "All" })).toHaveAttribute("tabindex", "-1");
  });

  it("disables every option while a fetch is in flight", () => {
    render(<ScopeControls value="all" onChange={() => {}} disabled />);
    for (const label of ["All", "Azure", "AWS", "GCP"]) {
      expect(screen.getByRole("radio", { name: label })).toBeDisabled();
    }
  });
});

describe("costScopeQuery", () => {
  it("includes days and omits provider for 'all'", () => {
    expect(costScopeQuery(30, "all")).toBe("?days=30");
    expect(costScopeQuery(7, "aws")).toBe("?days=7&provider=aws");
  });
});

// --- Overview integration: changing the cloud re-scopes every panel --------- //

function mockFetch() {
  return vi.fn(async (url: string | URL) => {
    const u = String(url);
    const body = u.includes("/api/costs/trend")
      ? { days: 30, currency: "USD", total: 100, prior_total: 80, delta: 20, delta_pct: 25, series: [{ date: "2026-07-01", cost: 10 }] }
      : u.includes("/api/costs/summary")
        ? { total: 100, by_type: [], by_region: [] }
        : u.includes("/api/summary")
          ? { executive_summary: "All good.", total_potential_savings: 0, currency: "USD" }
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

describe("Overview scoping", () => {
  let fetchMock: ReturnType<typeof mockFetch>;
  beforeEach(() => {
    window.history.replaceState(null, "", "/"); // start each run from a clean URL
    fetchMock = mockFetch();
    vi.stubGlobal("fetch", fetchMock);
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("scope_controls_change_refetches_all_panels", async () => {
    render(<Overview />);
    await screen.findByText(/data refreshed/i); // initial load settled

    // Initial cost fetch carries the default scope (days=30, all clouds).
    const initial = fetchMock.mock.calls.map((c) => String(c[0]));
    expect(initial.some((u) => u.includes("/api/costs/summary") && u.includes("days=30"))).toBe(true);

    fetchMock.mockClear();
    fireEvent.click(screen.getByRole("radio", { name: "Azure" }));

    // Switching cloud re-pulls the scoped panels with the provider param.
    await waitFor(() => {
      const urls = fetchMock.mock.calls.map((c) => String(c[0]));
      expect(urls.some((u) => u.includes("/api/costs/summary") && u.includes("provider=azure"))).toBe(true);
      expect(urls.some((u) => u.includes("/api/governance/posture") && u.includes("provider=azure"))).toBe(true);
    });

    // …and the cloud scope is now persisted in the URL for reload / sharing.
    expect(window.location.search).toContain("provider=azure");
  });
});
