import { describe, expect, it } from "vitest";

import { formatDelta, sparklinePath } from "./trend";

describe("formatDelta", () => {
  it("formatDelta_positive_negative_zero", () => {
    const up = formatDelta(7.5, 30);
    expect(up.direction).toBe("up");
    expect(up.arrow).toBe("▲");
    expect(up.label).toContain("7.5%");
    expect(up.label).toContain("prior 30d");

    const down = formatDelta(-3.2, 7);
    expect(down.direction).toBe("down");
    expect(down.arrow).toBe("▼");
    expect(down.label).toContain("3.2%");
    expect(down.label).toContain("prior 7d");

    const flat = formatDelta(0, 90);
    expect(flat.direction).toBe("flat");
    expect(flat.label).toContain("0%");
  });

  it("formatDelta_null_pct_renders_not_available", () => {
    const na = formatDelta(null, 30);
    expect(na.direction).toBe("na");
    expect(na.label).toContain("n/a");
    expect(na.label).not.toMatch(/\d%/); // no bogus percentage
    expect(na.srLabel.length).toBeGreaterThan(0);
  });
});

describe("sparklinePath", () => {
  it("sparklinePath_empty_single_and_many_points", () => {
    expect(sparklinePath([])).toBe("");

    // A single datum degrades to a flat baseline spanning the width (2 coords).
    expect(sparklinePath([5], 100, 24).trim().split(/\s+/)).toHaveLength(2);

    // Many points -> exactly one coordinate per value.
    expect(sparklinePath([1, 2, 3, 4], 100, 24).split(/\s+/)).toHaveLength(4);

    // An all-equal series can't scale -> also a flat baseline, never NaN.
    const flat = sparklinePath([9, 9, 9], 100, 24);
    expect(flat).not.toMatch(/NaN/);
  });

  it("sparklinePath_scales_within_viewbox", () => {
    const w = 100;
    const h = 24;
    const pad = 2;
    const pts = sparklinePath([10, 30, 20], w, h, pad)
      .split(/\s+/)
      .map((p) => p.split(",").map(Number));

    for (const [x, y] of pts) {
      expect(x).toBeGreaterThanOrEqual(0);
      expect(x).toBeLessThanOrEqual(w);
      expect(y).toBeGreaterThanOrEqual(pad);
      expect(y).toBeLessThanOrEqual(h - pad);
    }
    // The largest value (index 1) sits at the top => smallest y.
    const ys = pts.map(([, y]) => y);
    expect(ys.indexOf(Math.min(...ys))).toBe(1);
    // Series spans the full width edge to edge.
    expect(pts[0][0]).toBe(0);
    expect(pts[pts.length - 1][0]).toBe(w);
  });
});
