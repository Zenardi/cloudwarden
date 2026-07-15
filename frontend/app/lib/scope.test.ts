import { describe, expect, it } from "vitest";
import { DEFAULT_DAYS, DEFAULT_PROVIDER, parseScope, scopeToQuery } from "./scope";

describe("parseScope", () => {
  it("returns defaults for an empty query string", () => {
    expect(parseScope("")).toEqual({ days: DEFAULT_DAYS, provider: DEFAULT_PROVIDER });
  });

  it("reads a valid days + provider pair", () => {
    expect(parseScope("?days=90&provider=aws")).toEqual({ days: 90, provider: "aws" });
  });

  it("accepts each allowed range on its own", () => {
    expect(parseScope("?days=7").days).toBe(7);
    expect(parseScope("?days=30").days).toBe(30);
    expect(parseScope("?days=90").days).toBe(90);
  });

  it("falls back to the default range for an out-of-set or non-numeric days", () => {
    expect(parseScope("?days=15").days).toBe(DEFAULT_DAYS);
    expect(parseScope("?days=abc").days).toBe(DEFAULT_DAYS);
    expect(parseScope("?days=").days).toBe(DEFAULT_DAYS);
  });

  it("falls back to the default provider for an unknown cloud", () => {
    expect(parseScope("?provider=oracle").provider).toBe(DEFAULT_PROVIDER);
    expect(parseScope("?provider=").provider).toBe(DEFAULT_PROVIDER);
  });

  it("parses provider independently of days", () => {
    expect(parseScope("?provider=azure")).toEqual({ days: DEFAULT_DAYS, provider: "azure" });
  });
});

describe("scopeToQuery", () => {
  it("emits an empty string for the default (canonical) view", () => {
    expect(scopeToQuery(DEFAULT_DAYS, DEFAULT_PROVIDER)).toBe("");
  });

  it("omits whichever value is default", () => {
    expect(scopeToQuery(90, "all")).toBe("?days=90");
    expect(scopeToQuery(30, "azure")).toBe("?provider=azure");
  });

  it("emits both when both are non-default", () => {
    expect(scopeToQuery(7, "gcp")).toBe("?days=7&provider=gcp");
  });
});

describe("round-trip", () => {
  it("parseScope reverses scopeToQuery for every combination", () => {
    for (const days of [7, 30, 90] as const) {
      for (const provider of ["all", "azure", "aws", "gcp"] as const) {
        expect(parseScope(scopeToQuery(days, provider))).toEqual({ days, provider });
      }
    }
  });
});
