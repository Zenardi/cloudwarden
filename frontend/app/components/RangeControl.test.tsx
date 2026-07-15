import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { RangeControl } from "./RangeControl";

describe("RangeControl", () => {
  it("range_control_changes_days_and_refetches", () => {
    const onChange = vi.fn();
    render(<RangeControl value={30} onChange={onChange} />);

    // The active option reflects the current window.
    expect(screen.getByRole("button", { name: "30d" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "7d" })).toHaveAttribute("aria-pressed", "false");

    // Choosing another window calls back with that day count (the page wires this to a refetch).
    fireEvent.click(screen.getByRole("button", { name: "90d" }));
    expect(onChange).toHaveBeenCalledWith(90);
  });

  it("disables every option while a trend fetch is in flight", () => {
    render(<RangeControl value={7} onChange={() => {}} disabled />);
    for (const label of ["7d", "30d", "90d"]) {
      expect(screen.getByRole("button", { name: label })).toBeDisabled();
    }
  });
});
