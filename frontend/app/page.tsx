"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import type { ReactNode } from "react";
import { apiGet, API_BASE, GRAFANA_BASE, money, shortId } from "./lib/api";
import type { AISummary, Posture, Recommendation } from "./lib/api";

/** Latest governance/FinOps run — the subset the Overview surfaces (see backend `runs`). */
interface RunLatest {
  run_id?: string | null;
  status?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  mock?: boolean | null;
}

/** One row of a cost breakdown view (`v_cost_by_type` / `v_cost_by_region`). */
interface CostSlice {
  resource_type?: string | null;
  location?: string | null;
  cost?: number | null;
  currency?: string | null;
}

/** `/api/costs/summary` — total plus the by-dimension breakdowns it returns inline. */
interface CostSummary {
  total?: number;
  currency?: string;
  by_region?: CostSlice[];
  by_type?: CostSlice[];
}

/**
 * A fetch that is still in flight, succeeded with data, or failed. Modelling the
 * failure explicitly is the whole point: the previous `.catch(() => fallback)`
 * pattern made every error look like real (empty) data, so a down backend showed
 * a fabricated $0.00 and the error banner was dead code.
 */
type Loadable<T> =
  | { state: "loading" }
  | { state: "ok"; data: T }
  | { state: "error"; message: string };

const LOADING = { state: "loading" } as const;

async function load<T>(path: string): Promise<Loadable<T>> {
  try {
    return { state: "ok", data: await apiGet<T>(path) };
  } catch (e) {
    return { state: "error", message: e instanceof Error ? e.message : String(e) };
  }
}

/** Human relative time from an ISO stamp ("2 hours ago"), i18n-safe. Null if unparseable. */
function timeAgo(iso?: string | null): string | null {
  if (!iso) return null;
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return null;
  const secs = Math.round((then - Date.now()) / 1000); // negative = in the past
  const abs = Math.abs(secs);
  const rtf = new Intl.RelativeTimeFormat("en", { numeric: "auto" });
  const units: [Intl.RelativeTimeFormatUnit, number][] = [
    ["year", 31536000],
    ["month", 2592000],
    ["week", 604800],
    ["day", 86400],
    ["hour", 3600],
    ["minute", 60],
    ["second", 1],
  ];
  for (const [unit, s] of units) {
    if (abs >= s || unit === "second") return rtf.format(Math.round(secs / s), unit);
  }
  return null;
}

/** Absolute, localized timestamp ("Jul 14, 2026, 5:26 PM") for a precise-on-hover
 * companion to the relative "as of" label. Null when unparseable. */
function fmtAbs(iso?: string | null): string | null {
  if (!iso) return null;
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return null;
  return new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

/** Elapsed wall-clock between two ISO stamps ("3m 12s"). Null unless both are valid. */
function duration(startIso?: string | null, endIso?: string | null): string | null {
  if (!startIso || !endIso) return null;
  const ms = new Date(endIso).getTime() - new Date(startIso).getTime();
  if (!Number.isFinite(ms) || ms < 0) return null;
  const s = Math.round(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return s % 60 ? `${m}m ${s % 60}s` : `${m}m`;
  return `${Math.floor(m / 60)}h ${m % 60}m`;
}

/** Run status → pill class, matching the /runs and /executions convention exactly. */
function runBadgeClass(status?: string | null): string {
  if (status === "succeeded") return "badge approved";
  if (status === "failed") return "badge rejected";
  return "badge";
}

/**
 * Azure resource-type id → a label an operator reads at a glance
 * ("microsoft.compute/virtualmachines" → "Virtual machines"). Falls back to a
 * title-cased tail so an unmapped type is still legible, never a raw slug.
 */
const TYPE_LABELS: Record<string, string> = {
  "microsoft.compute/virtualmachines": "Virtual machines",
  "microsoft.compute/disks": "Managed disks",
  "microsoft.web/serverfarms": "App Service plans",
  "microsoft.storage/storageaccounts": "Storage accounts",
  "microsoft.network/publicipaddresses": "Public IPs",
  "microsoft.sql/servers": "SQL servers",
  "microsoft.containerservice/managedclusters": "AKS clusters",
};
function prettyType(t?: string | null): string {
  if (!t) return "Other";
  const key = t.toLowerCase();
  if (TYPE_LABELS[key]) return TYPE_LABELS[key];
  const tail = key.split("/").pop() ?? key;
  return tail.charAt(0).toUpperCase() + tail.slice(1);
}

/**
 * Humanize a snake_case backend enum for the reading line — "delete_public_ip" →
 * "Delete public IP", "empty_asp" → "Empty App Service plan". Keeps the operator's
 * vocabulary; kills the raw DB slugs that CSS `capitalize` alone left underscored.
 */
function humanizeToken(s?: string | null): string {
  if (!s) return "";
  const spaced = s
    .replace(/_/g, " ")
    .replace(/\basp\b/gi, "App Service plan")
    .replace(/\bip\b/gi, "IP")
    .replace(/\bvm\b/gi, "VM");
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

/** Circular-arrows glyph for the Refresh control; spins while a fetch is in flight. */
function RefreshIcon() {
  return (
    <svg
      className="spin-ico"
      viewBox="0 0 24 24"
      width="15"
      height="15"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.9"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M21 12a9 9 0 1 1-2.64-6.36" />
      <path d="M21 4v5h-5" />
    </svg>
  );
}

/**
 * Renders a card's value across all three states. `renderOk` only runs once the
 * fetch has genuinely succeeded, so callers never have to defend against a
 * fabricated fallback — loading shows a skeleton, failure shows an em-dash.
 */
function CardValue<T>({
  loadable,
  renderOk,
}: {
  loadable: Loadable<T>;
  renderOk: (data: T) => ReactNode;
}) {
  if (loadable.state === "loading") {
    return <div className="skeleton skeleton-value" aria-hidden />;
  }
  if (loadable.state === "error") {
    return (
      <>
        <div className="value unavailable">—</div>
        <div className="card-note">Unavailable</div>
      </>
    );
  }
  return <>{renderOk(loadable.data)}</>;
}

/** Loading / error / empty scaffolding shared by the dense work-area panels. */
function PanelBody<T>({
  loadable,
  isEmpty,
  empty,
  children,
}: {
  loadable: Loadable<T>;
  isEmpty: (data: T) => boolean;
  empty: ReactNode;
  children: (data: T) => ReactNode;
}) {
  if (loadable.state === "loading") {
    return (
      <>
        <div className="skeleton skeleton-row" aria-hidden />
        <div className="skeleton skeleton-row" aria-hidden />
        <div className="skeleton skeleton-row" aria-hidden />
      </>
    );
  }
  if (loadable.state === "error") {
    return <div className="panel-empty">Couldn’t load — {loadable.message}</div>;
  }
  if (isEmpty(loadable.data)) {
    return <div className="panel-empty">{empty}</div>;
  }
  return <>{children(loadable.data)}</>;
}

export default function Overview() {
  const [summary, setSummary] = useState<Loadable<AISummary | null>>(LOADING);
  const [cost, setCost] = useState<Loadable<CostSummary>>(LOADING);
  const [run, setRun] = useState<Loadable<RunLatest | null>>(LOADING);
  const [recs, setRecs] = useState<Loadable<Recommendation[]>>(LOADING);
  const [posture, setPosture] = useState<Loadable<Posture>>(LOADING);

  const refresh = useCallback(() => {
    setSummary(LOADING);
    setCost(LOADING);
    setRun(LOADING);
    setRecs(LOADING);
    setPosture(LOADING);
    // Each request settles independently: a partial outage still shows what loaded.
    load<AISummary | null>("/api/summary").then(setSummary);
    load<CostSummary>("/api/costs/summary").then(setCost);
    load<RunLatest | null>("/api/runs/latest").then(setRun);
    load<Recommendation[]>("/api/recommendations").then(setRecs);
    load<Posture>("/api/governance/posture").then(setPosture);
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  // `r` re-pulls the page — the power-user path that avoids a full reload. Ignored
  // while typing in a field or when a modifier is held (leaves browser shortcuts alone).
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key !== "r" || e.metaKey || e.ctrlKey || e.altKey) return;
      const el = e.target as HTMLElement | null;
      const tag = el?.tagName?.toLowerCase();
      if (tag === "input" || tag === "textarea" || tag === "select" || el?.isContentEditable) return;
      e.preventDefault();
      refresh();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [refresh]);

  const states = [summary, cost, run, recs, posture];
  const failed = states.filter((s) => s.state === "error").length;
  const loading = states.some((s) => s.state === "loading");
  const allFailed = failed === states.length;

  // Real denominator for the savings ratio: only when the cost total actually loaded.
  const spendForRatio =
    cost.state === "ok" && typeof cost.data.total === "number" && cost.data.total > 0
      ? cost.data.total
      : null;

  const runId = run.state === "ok" && run.data?.run_id ? run.data.run_id : undefined;
  const asOf = run.state === "ok" ? timeAgo(run.data?.finished_at) : null;
  const asOfAbs = run.state === "ok" ? fmtAbs(run.data?.finished_at) : null;

  // Savings figure, decoupled from the AI summary fetch: prefer the AI-reconciled
  // total, but fall back to summing the loaded recommendations so a `/api/summary`
  // outage no longer blanks the savings KPI alongside the summary prose.
  const savings: { amount: number; currency?: string } | null =
    summary.state === "ok" &&
    summary.data &&
    typeof summary.data.total_potential_savings === "number"
      ? { amount: summary.data.total_potential_savings, currency: summary.data.currency }
      : recs.state === "ok" && recs.data.length > 0
        ? {
            amount: recs.data.reduce((s, r) => s + (r.est_monthly_savings || 0), 0),
            currency: recs.data[0]?.currency,
          }
        : null;
  const savingsPending = savings === null && (summary.state === "loading" || recs.state === "loading");

  return (
    <>
      <header className="page-head">
        <div>
          <h1>Overview</h1>
          <p className="sub">Cost, savings, and the latest governance run across your clouds.</p>
        </div>
        <div className="page-head-meta">
          {asOf && (
            <span className="as-of" title={asOfAbs ? `Last run finished ${asOfAbs}` : undefined}>
              Data as of {asOf}
            </span>
          )}
          <button
            type="button"
            className="btn-refresh"
            data-busy={loading}
            onClick={refresh}
            aria-label="Refresh data (shortcut: r)"
            title="Refresh (r)"
          >
            <RefreshIcon />
            Refresh
          </button>
        </div>
      </header>

      {failed > 0 && (
        <div className="err banner" role="alert">
          <div>
            <strong>
              {allFailed
                ? `Can’t reach the API at ${API_BASE}.`
                : "Some data couldn’t be loaded."}
            </strong>
            <div className="err-detail">
              {allFailed
                ? "Is the backend running?"
                : `${failed} of ${states.length} requests failed — showing what loaded.`}
            </div>
          </div>
          <button type="button" className="retry" onClick={refresh}>
            Retry
          </button>
        </div>
      )}

      <div className="cards kpis" aria-live="polite" aria-busy={loading}>
        <Link className="card kpi" href="/costs" aria-describedby="cost-amortized-caveat">
          <div
            className="label"
            title="Amortized: upfront reservation & commitment costs are spread evenly across the 30 days, not charged in a lump on the purchase date. Figures are estimates — Cost Management data lags ~8–24h and isn’t final until invoiced."
          >
            Cost (30d, amortized)
          </div>
          <span id="cost-amortized-caveat" className="sr-only">
            Amortized: upfront reservation and commitment costs are spread evenly across the 30 days.
            Figures are estimates — Cost Management data lags about 8 to 24 hours and isn’t final until
            invoiced.
          </span>
          <CardValue
            loadable={cost}
            renderOk={(d) => {
              if (typeof d.total !== "number") {
                return (
                  <>
                    <div className="value unavailable">—</div>
                    <div className="card-note">No cost data</div>
                  </>
                );
              }
              const regions = Array.isArray(d.by_region) ? d.by_region.length : 0;
              const types = Array.isArray(d.by_type) ? d.by_type.length : 0;
              const scope = [
                regions && `${regions} region${regions === 1 ? "" : "s"}`,
                types && `${types} type${types === 1 ? "" : "s"}`,
              ]
                .filter(Boolean)
                .join(" · ");
              return (
                <>
                  <div className="value">{money(d.total, d.currency)}</div>
                  <div className="card-note">{scope ? `${scope} · estimate` : "estimate"}</div>
                </>
              );
            }}
          />
          <span className="card-link">
            Cost breakdown <span aria-hidden="true">→</span>
          </span>
        </Link>

        <Link className="card kpi" href="/recommendations">
          <div className="label">Potential monthly savings</div>
          {savings ? (
            <>
              <div className="value green">{money(savings.amount, savings.currency)}</div>
              {spendForRatio != null && (
                <div className="card-note">
                  ≈{Math.round((savings.amount / spendForRatio) * 100)}% of 30-day spend
                </div>
              )}
            </>
          ) : savingsPending ? (
            <div className="skeleton skeleton-value" aria-hidden />
          ) : (
            <>
              <div className="value unavailable">—</div>
              <div className="card-note">No savings data</div>
            </>
          )}
          <span className="card-link">
            Recommendations <span aria-hidden="true">→</span>
          </span>
        </Link>

        <Link className="card kpi" href="/runs" title={runId ? `Run ${runId}` : undefined}>
          <div className="label">Last run</div>
          <CardValue
            loadable={run}
            renderOk={(d) => {
              if (!d) {
                return (
                  <div className="run-head">
                    <span className="badge">No runs yet</span>
                  </div>
                );
              }
              const ago = timeAgo(d.started_at);
              const dur = duration(d.started_at, d.finished_at);
              return (
                <>
                  <div className="run-head">
                    <span className={runBadgeClass(d.status)}>{d.status ?? "unknown"}</span>
                    {d.mock && <span className="badge">mock</span>}
                  </div>
                  <div className="card-note">
                    {ago ?? "Time unknown"}
                    {dur ? ` · ran ${dur}` : ""}
                  </div>
                </>
              );
            }}
          />
          <span className="card-link">
            Run history <span aria-hidden="true">→</span>
          </span>
        </Link>
      </div>

      <div className="overview-grid">
        <section className="panel" aria-labelledby="recs-h">
          <div className="panel-head">
            <h2 className="panel-title" id="recs-h">
              Recommended actions
            </h2>
            <Link className="panel-link" href="/recommendations">
              {recs.state === "ok" && recs.data.length > 0 ? (
                <>
                  All {recs.data.length} <span aria-hidden="true">→</span>
                </>
              ) : (
                <>
                  Open <span aria-hidden="true">→</span>
                </>
              )}
            </Link>
          </div>
          <div className="actions-body">
            <PanelBody
              loadable={recs}
              isEmpty={(d) => d.length === 0}
              empty="No recommendations yet — trigger a run from the Runs page."
            >
              {(d) =>
                d.slice(0, 5).map((r) => (
                  <Link className="action-row" href="/recommendations" key={r.id}>
                    <div className="action-main">
                      <div className="action-name" title={r.rationale ?? undefined}>
                        <span className="verb">{humanizeToken(r.action)}</span>{" "}
                        {shortId(r.resource_id)}
                        {r.recommended_sku ? ` → ${r.recommended_sku}` : ""}
                      </div>
                      <div className="action-sub">
                        <span className={`badge ${r.risk}`}>{r.risk} risk</span>
                        <span>{humanizeToken(r.category)}</span>
                        <span>{Math.round((r.confidence || 0) * 100)}% conf</span>
                      </div>
                    </div>
                    <div className="action-figs">
                      <div className="action-save">
                        {money(r.est_monthly_savings, r.currency)}
                        <span className="per">/mo</span>
                      </div>
                    </div>
                  </Link>
                ))
              }
            </PanelBody>
          </div>
          {recs.state === "ok" && recs.data.length > 0 && (
            <p className="card-note">Estimated monthly savings; see each item for caveats.</p>
          )}
        </section>

        <div className="overview-aside">
          <section className="panel" aria-labelledby="drivers-h">
            <div className="panel-head">
              <h2 className="panel-title" id="drivers-h">
                Cost drivers
              </h2>
              <Link className="panel-link" href="/costs">
                Breakdown <span aria-hidden="true">→</span>
              </Link>
            </div>
            <PanelBody
              loadable={cost}
              isEmpty={(d) => !Array.isArray(d.by_type) || d.by_type.length === 0}
              empty="No cost data yet."
            >
              {(d) => {
                const total =
                  typeof d.total === "number" && d.total > 0
                    ? d.total
                    : (d.by_type ?? []).reduce((s, r) => s + (r.cost || 0), 0);
                const rows = (d.by_type ?? [])
                  .filter((r) => typeof r.cost === "number")
                  .slice(0, 5);
                return (
                  <div className="bars">
                    {rows.map((r, i) => {
                      const share = total > 0 ? (r.cost as number) / total : 0;
                      return (
                        <div className="bar-row" key={r.resource_type ?? i}>
                          <span className="bar-label">{prettyType(r.resource_type)}</span>
                          <span className="bar-val">
                            {money(r.cost, r.currency ?? undefined)} ·{" "}
                            {share > 0 && Math.round(share * 100) === 0
                              ? "<1"
                              : Math.round(share * 100)}
                            %
                          </span>
                          <div className="bar-track">
                            <div
                              className="bar-fill"
                              style={{ ["--fill" as string]: share.toFixed(3) }}
                            />
                          </div>
                        </div>
                      );
                    })}
                  </div>
                );
              }}
            </PanelBody>
          </section>

          <section className="panel" aria-labelledby="posture-h">
            <div className="panel-head">
              <h2 className="panel-title" id="posture-h">
                Governance posture
              </h2>
              <Link className="panel-link" href="/compliance">
                Compliance <span aria-hidden="true">→</span>
              </Link>
            </div>
            <PanelBody
              loadable={posture}
              isEmpty={(d) => (d.totals?.evaluated ?? 0) === 0}
              empty="No policy evaluations yet — bind a collection to an account group to start."
            >
              {(d) => {
                const t = d.totals;
                const worst = [...(d.by_policy ?? [])]
                  .filter((p) => p.violations > 0)
                  .sort((a, b) => b.violations - a.violations)[0];
                return (
                  <>
                    <div className="posture">
                      <div className="posture-stat">
                        <span className="posture-num">{t.evaluated}</span>
                        <span className="posture-label">Evaluated</span>
                      </div>
                      <div className="posture-stat">
                        <span className={`posture-num${t.compliant > 0 ? " ok" : ""}`}>
                          {t.compliant}
                        </span>
                        <span className="posture-label">Compliant</span>
                      </div>
                      <div className="posture-stat">
                        <span className={`posture-num${t.violations > 0 ? " viol" : ""}`}>
                          {t.violations}
                        </span>
                        <span className="posture-label">Violations</span>
                      </div>
                    </div>
                    {worst && (
                      <div className="posture-note">
                        Worst: <strong>{worst.policy_name}</strong> — {worst.violations} violation
                        {worst.violations === 1 ? "" : "s"}.
                      </div>
                    )}
                  </>
                );
              }}
            </PanelBody>
          </section>
        </div>
      </div>

      <h2>AI executive summary</h2>
      <div aria-live="polite">
        {summary.state === "loading" ? (
        <div className="summary" aria-busy="true">
          <div className="skeleton skeleton-line" aria-hidden />
          <div className="skeleton skeleton-line" aria-hidden />
          <div className="skeleton skeleton-line short" aria-hidden />
        </div>
      ) : summary.state === "error" ? (
        <div className="summary summary-error">Couldn’t load the summary — {summary.message}</div>
      ) : summary.data?.executive_summary ? (
        <div className="summary">
          {summary.data.executive_summary}
          {summary.data.provider && (
            <div className="muted summary-meta">
              AI estimate · {summary.data.provider}/{summary.data.model}
            </div>
          )}
        </div>
      ) : (
        <div className="summary">
          No summary yet. Trigger a run from the <Link href="/runs">Runs</Link> page to generate one.
        </div>
        )}
      </div>

      <h2>Dashboards</h2>
      <div className="links">
        <a
          href={`${GRAFANA_BASE}/d/finops-cost`}
          target="_blank"
          rel="noreferrer"
          aria-label="Grafana — Cost Overview (opens in new tab)"
        >
          Grafana — Cost Overview ↗
        </a>
        <a
          href={`${GRAFANA_BASE}/d/finops-recs`}
          target="_blank"
          rel="noreferrer"
          aria-label="Grafana — Recommendations (opens in new tab)"
        >
          Grafana — Recommendations ↗
        </a>
        <Link href="/recommendations">
          Review recommendations <span aria-hidden="true">→</span>
        </Link>
      </div>
    </>
  );
}
