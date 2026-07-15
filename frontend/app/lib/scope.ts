import { RANGE_OPTIONS, type RangeDays } from "../components/RangeControl";
import { PROVIDER_SCOPES, type ProviderScope } from "../components/ScopeControls";

// The canonical defaults the Overview opens on; also the values omitted from the
// URL so the unfiltered view keeps a clean, shareable `/` (no query string).
export const DEFAULT_DAYS: RangeDays = 30;
export const DEFAULT_PROVIDER: ProviderScope = "all";

export interface Scope {
  days: RangeDays;
  provider: ProviderScope;
}

/**
 * Parse the Overview scope (date range + cloud) from a URL query string. Anything
 * missing or outside the allowed sets falls back to the default, so a hand-edited
 * or stale link can never put the page into an invalid state.
 */
export function parseScope(search: string): Scope {
  const q = new URLSearchParams(search);
  const d = Number(q.get("days"));
  const days = (RANGE_OPTIONS as readonly number[]).includes(d) ? (d as RangeDays) : DEFAULT_DAYS;
  const p = q.get("provider");
  const provider =
    p && (PROVIDER_SCOPES as readonly string[]).includes(p) ? (p as ProviderScope) : DEFAULT_PROVIDER;
  return { days, provider };
}

/**
 * Serialize the scope to a shareable query string ("?days=90&provider=aws"), or
 * "" when both are default so the canonical view stays a clean `/`. `parseScope`
 * round-trips its output. Values come from fixed enums, so no escaping is needed.
 */
export function scopeToQuery(days: RangeDays, provider: ProviderScope): string {
  const q = new URLSearchParams();
  if (days !== DEFAULT_DAYS) q.set("days", String(days));
  if (provider !== DEFAULT_PROVIDER) q.set("provider", provider);
  const s = q.toString();
  return s ? `?${s}` : "";
}
