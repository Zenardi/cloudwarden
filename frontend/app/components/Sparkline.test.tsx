import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Sparkline } from "./Sparkline";

describe("Sparkline", () => {
  it("Sparkline_renders_polyline_and_is_aria_hidden", () => {
    const { container } = render(<Sparkline values={[1, 2, 3]} />);

    const svg = container.querySelector("svg");
    expect(svg).not.toBeNull();
    expect(svg).toHaveAttribute("aria-hidden", "true"); // decorative — off the a11y tree

    const poly = container.querySelector("polyline");
    expect(poly).not.toBeNull();
    expect(poly?.getAttribute("points")?.trim().split(/\s+/)).toHaveLength(3);
  });

  it("renders nothing for an empty series", () => {
    const { container } = render(<Sparkline values={[]} />);
    expect(container.querySelector("svg")).toBeNull();
  });
});
