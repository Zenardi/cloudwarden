/**
 * A fetch that is still in flight, succeeded with data, or failed. Modelling the
 * failure explicitly is the point: a `.catch(() => fallback)` makes every error
 * look like real (empty) data, so a down backend would show a fabricated value.
 * Shared by the Overview page and the trend components.
 */
export type Loadable<T> =
  | { state: "loading" }
  | { state: "ok"; data: T }
  | { state: "error"; message: string };
