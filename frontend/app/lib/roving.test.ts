import { describe, expect, it } from "vitest";
import { nextRadioIndex } from "./roving";

describe("nextRadioIndex", () => {
  it("moves forward and wraps past the end", () => {
    expect(nextRadioIndex("ArrowRight", 0, 4)).toBe(1);
    expect(nextRadioIndex("ArrowDown", 1, 4)).toBe(2);
    expect(nextRadioIndex("ArrowRight", 3, 4)).toBe(0);
  });

  it("moves backward and wraps past the start", () => {
    expect(nextRadioIndex("ArrowLeft", 2, 4)).toBe(1);
    expect(nextRadioIndex("ArrowUp", 1, 4)).toBe(0);
    expect(nextRadioIndex("ArrowLeft", 0, 4)).toBe(3);
  });

  it("jumps to the ends with Home / End", () => {
    expect(nextRadioIndex("Home", 2, 4)).toBe(0);
    expect(nextRadioIndex("End", 1, 4)).toBe(3);
  });

  it("returns null for any non-navigation key", () => {
    expect(nextRadioIndex("a", 0, 4)).toBeNull();
    expect(nextRadioIndex("Enter", 0, 4)).toBeNull();
    expect(nextRadioIndex(" ", 0, 4)).toBeNull();
  });

  it("returns null when there are no options", () => {
    expect(nextRadioIndex("ArrowRight", 0, 0)).toBeNull();
  });
});
