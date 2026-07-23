export const API_BASE = process.env.NEXT_PUBLIC_API_BASE || "http://localhost:8000";
export const GRAFANA_BASE = process.env.NEXT_PUBLIC_GRAFANA_BASE || "http://localhost:3000";

/**
 * Error thrown for a non-2xx response. Carries the HTTP `status` and the parsed
 * JSON `body` (when present) so callers can surface, e.g., a 422 validation
 * payload inline instead of just a status string. `message` keeps the historic
 * `"<METHOD> <path> → <status>"` format so `String(err)` stays readable.
 */
export class ApiError extends Error {
  status: number;
  body: any;
  constructor(method: string, path: string, status: number, body: any) {
    super(`${method} ${path} → ${status}`);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
  }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    method,
    headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
    cache: "no-store",
    // Default "same-origin" credentials: a same-origin production deploy sends the
    // session cookie automatically, while cross-origin mock dev (CORS `*`) is unaffected.
  });
  if (!res.ok) {
    let parsed: any = undefined;
    try {
      parsed = await res.json();
    } catch {
      /* no JSON body */
    }
    throw new ApiError(method, path, res.status, parsed);
  }
  return res.json();
}

export const apiGet = <T = any>(path: string): Promise<T> => request<T>("GET", path);
export const apiPost = <T = any>(path: string, body?: unknown): Promise<T> =>
  request<T>("POST", path, body);
export const apiPut = <T = any>(path: string, body?: unknown): Promise<T> =>
  request<T>("PUT", path, body);
export const apiDelete = <T = any>(path: string): Promise<T> => request<T>("DELETE", path);

export interface Subscription {
  subscription_id: string;
  display_name: string;
  environment?: string | null; // Development | QA | Prod | Sandbox | null (unclassified)
  tenant_id?: string | null;
  client_id?: string | null;
  has_credentials: boolean;
  enabled: boolean;
  is_default: boolean;
}

export interface Recommendation {
  id: number;
  resource_id: string;
  category: string;
  action: string;
  current_sku?: string | null;
  recommended_sku?: string | null;
  risk: string;
  confidence: number;
  est_monthly_savings: number;
  currency?: string;
  source: string;
  priority: number;
  rationale?: string | null;
  status: string;
}

// Commitment coverage & RI/Savings-Plan recommendations (M14.1).
export interface CommitmentCoverage {
  provider?: string;
  sku_family: string;
  region: string;
  eligible_monthly: number;
  committed_monthly: number;
  coverage_pct: number;
  utilization_pct?: number | null;
  currency?: string;
}

export interface CommitmentInventory {
  commitment_id: string;
  provider?: string;
  kind: string;
  display_name?: string | null;
  scope?: string;
  region?: string | null;
  sku_family?: string | null;
  term: string;
  utilization_pct: number;
  expiry_date?: string | null;
  currency?: string;
}

export interface CommitmentData {
  coverage: CommitmentCoverage[];
  commitments: CommitmentInventory[];
  recommendations: Recommendation[];
}

export interface Policy {
  id: number;
  name: string;
  resource_type: string;
  spec: Record<string, any>;
  description?: string | null;
  enabled: boolean;
  version: number;
  source: string;
  validation_status?: string;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface ValidationResult {
  valid: boolean;
  errors: string[];
}

export interface PolicyVersion {
  policy_id: number;
  version: number;
  name: string;
  resource_type: string;
  spec: Record<string, any>;
  description?: string | null;
  actor?: string | null;
  created_at?: string | null;
}

export interface PolicyDiff {
  from_version: number;
  to_version: number;
  changed_fields: string[];
  changes: Record<string, { old: any; new: any }>;
}

/** A policy's version history, newest-first. */
export const listPolicyVersions = (id: number): Promise<PolicyVersion[]> =>
  apiGet<PolicyVersion[]>(`/api/policies/${id}/versions`);

/** Field-level diff between two stored versions of a policy. */
export const diffPolicyVersions = (id: number, from: number, to: number): Promise<PolicyDiff> =>
  apiGet<PolicyDiff>(`/api/policies/${id}/versions/diff?from_version=${from}&to_version=${to}`);

export interface PolicyExecution {
  execution_id: string;
  policy_id: number;
  subscription_id?: string | null;
  binding_id?: number | null;
  status: string;
  started_at?: string | null;
  finished_at?: string | null;
  resources_matched: number;
  actions_taken: any[];
  error?: string | null;
}

export interface PolicyMatch {
  resource_id: string;
  resource_type?: string | null;
  matched_at?: string | null;
  action_taken?: string | null;
  action_result?: Record<string, any>;
}

export interface CollectionPolicyRef {
  id: number;
  name: string;
  resource_type: string;
  enabled: boolean;
}

export interface Collection {
  id: number;
  name: string;
  description?: string | null;
  policy_count: number;
  policies: CollectionPolicyRef[];
  created_at?: string | null;
  updated_at?: string | null;
}

export interface AccountGroupMember {
  subscription_id: string;
  display_name: string;
  enabled: boolean;
}

export interface AccountGroup {
  id: number;
  name: string;
  description?: string | null;
  subscription_count: number;
  subscriptions: AccountGroupMember[];
  created_at?: string | null;
  updated_at?: string | null;
}

// --- Bindings (M5.2/M5.3/M5.4) — collection × account group + execution ----- //

export interface Binding {
  id: number;
  collection_id: number;
  account_group_id: number;
  schedule?: string | null;
  mode: string;
  dry_run: boolean;
  enabled: boolean;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface BindingRunExecution {
  execution_id: string;
  policy_id: number;
  subscription_id?: string | null;
  status: string;
  resources_matched?: number;
  error?: string | null;
}

export interface BindingRunResult {
  binding_id: number;
  status: string; // "completed" | "skipped"
  dry_run?: boolean;
  reason?: string;
  executions: BindingRunExecution[];
}

// --- Real-time events status feed (M6.4) ------------------------------------ //

/** One event-mode policy execution reactively triggered by a delivery (M6.2). */
export interface TriggeredExecution {
  execution_id: string;
  policy_id: number;
  status: string;
  mode: string;
  started_at?: string | null;
}

/** A recent Event Grid delivery plus the executions it triggered (M6.4 feed). */
export interface RecentEvent {
  event_id: string;
  event_type: string;
  subject: string;
  resource_id?: string | null;
  subscription_id?: string | null;
  event_time?: string | null;
  received_at?: string | null;
  status: string;
  triggered_executions: TriggeredExecution[];
}

/** Recent deliveries newest-first, paginated, each with its triggered runs (M6.4). */
export const fetchRecentEvents = (limit = 50, offset = 0): Promise<RecentEvent[]> =>
  apiGet<RecentEvent[]>(`/api/events/recent?limit=${limit}&offset=${offset}`);

export interface AISummary {
  executive_summary: string;
  total_potential_savings: number;
  currency: string;
  provider?: string;
  model?: string;
}

/** One day of the cost-trend series (`/api/costs/trend`, #113). */
export interface CostTrendPoint {
  date: string; // ISO YYYY-MM-DD
  cost: number;
}

/**
 * Amortized cost for the current window vs the immediately prior window of equal
 * length, plus a daily series. `delta_pct` is `null` when the prior window is
 * empty (no bogus % on a first-ever period). Mirrors the backend response (#113).
 */
export interface CostTrend {
  days: number;
  currency: string;
  total: number;
  prior_total: number;
  delta: number;
  delta_pct: number | null;
  series: CostTrendPoint[];
}

/** Fetch the cost trend for the last `days` days (clamped 1–365 server-side). */
export const getCostTrend = (days = 30): Promise<CostTrend> =>
  apiGet<CostTrend>(`/api/costs/trend?days=${days}`);

/**
 * Query string for the day/cloud-scoped cost endpoints (`/api/costs/summary`,
 * `by-type`, `by-region` — #116). Always sends `days`; omits `provider` for the
 * "all clouds" scope. Values go through URLSearchParams (encoded, injection-safe).
 */
export function costScopeQuery(days: number, provider: string): string {
  const qs = new URLSearchParams({ days: String(days) });
  if (provider && provider !== "all") qs.set("provider", provider);
  return `?${qs.toString()}`;
}

export function money(v: number | null | undefined, currency = "USD"): string {
  const n = typeof v === "number" ? v : 0;
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency,
    maximumFractionDigits: 2,
  }).format(n);
}

export function shortId(resourceId: string): string {
  const parts = (resourceId || "").split("/");
  return parts[parts.length - 1] || resourceId;
}

// --- AssetDB explorer (M4.5) — consumes the M4.2/M4.3/M4.4 APIs ------------- //

/** The clouds AssetDB/governance can span (M12 multi-cloud). "all" = no filter. */
export const PROVIDERS = ["azure", "aws", "gcp"] as const;
export type Provider = (typeof PROVIDERS)[number];

export interface Asset {
  resource_id: string;
  subscription_id?: string | null;
  provider?: string | null;
  resource_group?: string | null;
  name?: string | null;
  type?: string | null;
  location?: string | null;
  sku?: string | null;
  tags: Record<string, string>;
  config: Record<string, any>;
  state?: string | null;
  first_seen?: string | null;
  last_seen?: string | null;
}

export interface AssetFilter {
  column: string;
  op?: "eq" | "ne" | "contains" | "in";
  value: any;
}

export interface AssetQuery {
  filters?: AssetFilter[];
  tags?: Record<string, string>;
  limit?: number;
  offset?: number;
}

export interface AssetRelationship {
  id: number;
  source_id: string;
  target_id: string;
  kind: string;
  direction: "inbound" | "outbound";
  neighbor: string;
  created_at?: string | null;
}

export interface AssetEvent {
  id: number;
  resource_id: string;
  subscription_id?: string | null;
  event_type: string;
  data: Record<string, any>;
  at?: string | null;
}

export interface AssetQueryInputs {
  provider?: string;
  type?: string;
  location?: string;
  contains?: string;
  tagKey?: string;
  tagValue?: string;
  limit?: number;
  offset?: number;
}

/**
 * Build an injection-safe {@link AssetQuery} from explorer form fields. Empty
 * fields are omitted; a tag pair contributes only when both key and value are set.
 * `provider` (M12.4) scopes to one cloud — empty/"all" spans every cloud.
 * `contains` matches anywhere in the resource id. Values are always sent as bound
 * parameters server-side (never interpolated), so the query is injection-safe.
 */
export function buildAssetQuery(opts: AssetQueryInputs): AssetQuery {
  const filters: AssetFilter[] = [];
  if (opts.provider && opts.provider !== "all")
    filters.push({ column: "provider", op: "eq", value: opts.provider });
  if (opts.type) filters.push({ column: "type", op: "contains", value: opts.type });
  if (opts.location) filters.push({ column: "location", op: "contains", value: opts.location });
  if (opts.contains) filters.push({ column: "resource_id", op: "contains", value: opts.contains });
  const tags: Record<string, string> = {};
  if (opts.tagKey && opts.tagValue) tags[opts.tagKey] = opts.tagValue;
  return { filters, tags, limit: opts.limit ?? 50, offset: opts.offset ?? 0 };
}

/** Run a structured asset query (M4.2). */
export const queryAssets = (q: AssetQuery): Promise<Asset[]> =>
  apiPost<Asset[]>("/api/assets/query", q);

/** Fetch a single asset by exact id via the query API; `null` when not found. */
export async function getAsset(resourceId: string): Promise<Asset | null> {
  const rows = await queryAssets({
    filters: [{ column: "resource_id", op: "eq", value: resourceId }],
    limit: 1,
  });
  return rows[0] ?? null;
}

/** An asset's relationship edges, both directions (M4.3). */
export const getAssetRelationships = (resourceId: string): Promise<AssetRelationship[]> =>
  apiGet<AssetRelationship[]>(`/api/assets${resourceId}/relationships`);

/** An asset's change-history timeline, newest-first (M4.4). */
export const getAssetHistory = (resourceId: string): Promise<AssetEvent[]> =>
  apiGet<AssetEvent[]>(`/api/assets${resourceId}/history`);

// --------------------------------------------------------------------------- //
// Notifications: channels, templates, per-binding wiring (M8.4)
// --------------------------------------------------------------------------- //

/** The transport kinds a channel may declare (mirrors the backend registry). */
export const TRANSPORTS = ["webhook", "slack", "email", "teams", "jira", "servicenow"] as const;
export type TransportKind = (typeof TRANSPORTS)[number];

export interface NotificationChannel {
  id: number;
  name: string;
  transport: string;
  target: string;
  config?: Record<string, any> | null;
  enabled: boolean;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface NotificationTemplate {
  id: number;
  name: string;
  subject?: string | null;
  body: string;
  format: string;
  description?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface BindingNotification {
  id: number;
  binding_id: number;
  channel_id: number;
  channel_name: string;
  channel_transport: string;
  template_id: number;
  template_name: string;
  created_at?: string | null;
}

export const listNotificationChannels = (): Promise<NotificationChannel[]> =>
  apiGet<NotificationChannel[]>("/api/notification-channels");

export const createNotificationChannel = (
  body: Pick<NotificationChannel, "name" | "transport" | "target"> &
    Partial<Pick<NotificationChannel, "config" | "enabled">>,
): Promise<NotificationChannel> =>
  apiPost<NotificationChannel>("/api/notification-channels", body);

export const updateNotificationChannel = (
  id: number,
  changes: Partial<NotificationChannel>,
): Promise<NotificationChannel> =>
  apiPut<NotificationChannel>(`/api/notification-channels/${id}`, changes);

export const deleteNotificationChannel = (id: number): Promise<unknown> =>
  apiDelete(`/api/notification-channels/${id}`);

export const listNotificationTemplates = (): Promise<NotificationTemplate[]> =>
  apiGet<NotificationTemplate[]>("/api/notification-templates");

export const createNotificationTemplate = (
  body: Pick<NotificationTemplate, "name" | "body"> &
    Partial<Pick<NotificationTemplate, "subject" | "format" | "description">>,
): Promise<NotificationTemplate> =>
  apiPost<NotificationTemplate>("/api/notification-templates", body);

export const updateNotificationTemplate = (
  id: number,
  changes: Partial<NotificationTemplate>,
): Promise<NotificationTemplate> =>
  apiPut<NotificationTemplate>(`/api/notification-templates/${id}`, changes);

export const deleteNotificationTemplate = (id: number): Promise<unknown> =>
  apiDelete(`/api/notification-templates/${id}`);

export const listBindingNotifications = (bindingId: number): Promise<BindingNotification[]> =>
  apiGet<BindingNotification[]>(`/api/bindings/${bindingId}/notifications`);

export const attachBindingNotification = (
  bindingId: number,
  body: { channel_id: number; template_id: number },
): Promise<BindingNotification> =>
  apiPost<BindingNotification>(`/api/bindings/${bindingId}/notifications`, body);

export const detachBindingNotification = (
  bindingId: number,
  notificationId: number,
): Promise<unknown> => apiDelete(`/api/bindings/${bindingId}/notifications/${notificationId}`);

// --------------------------------------------------------------------------- //
// Compliance explorer (M9.3): posture policy list → matched resources → asset
// --------------------------------------------------------------------------- //

/** One policy row from the governance posture rollup (M9.1). */
export interface PosturePolicy {
  policy_id: number;
  policy_name: string;
  compliant: number;
  non_compliant: number;
  violations: number;
  evaluated: number;
}

/** One cloud's posture rollup (M12.4 cross-cloud). */
export interface PostureProvider {
  provider: string;
  compliant: number;
  non_compliant: number;
  violations: number;
  evaluated: number;
}

export interface Posture {
  totals: { compliant: number; non_compliant: number; violations: number; evaluated: number };
  by_policy: PosturePolicy[];
  by_subscription: unknown[];
  by_collection: unknown[];
  by_provider: PostureProvider[];
}

/** A resource currently flagged by a policy (M9.3 drill-down). */
export interface MatchedResource {
  resource_id: string;
  resource_type?: string | null;
  subscription_id?: string | null;
  matched_at?: string | null;
}

/**
 * Governance compliance posture — the policy list + non-compliant counts (M9.1),
 * optionally scoped to one cloud (M12.4). `provider` empty/"all" spans every cloud.
 */
export const getGovernancePosture = (provider?: string): Promise<Posture> => {
  const qs = provider && provider !== "all" ? `?provider=${encodeURIComponent(provider)}` : "";
  return apiGet<Posture>(`/api/governance/posture${qs}`);
};

// --------------------------------------------------------------------------- //
// Audit log (M11.4): append-only trail of mutating governance actions
// --------------------------------------------------------------------------- //

export interface AuditEntry {
  id: number;
  actor: string | null;
  action: string;
  target_type: string;
  target_id: string | null;
  before: Record<string, any>;
  after: Record<string, any>;
  at?: string | null;
}

export interface AuditFilters {
  actor?: string;
  action?: string;
  target_type?: string;
  target_id?: string;
  limit?: number;
}

/** List audit entries newest-first, optionally filtered by actor / action / target. */
export function listAudit(filters: AuditFilters = {}): Promise<AuditEntry[]> {
  const qs = new URLSearchParams();
  if (filters.actor) qs.set("actor", filters.actor);
  if (filters.action) qs.set("action", filters.action);
  if (filters.target_type) qs.set("target_type", filters.target_type);
  if (filters.target_id) qs.set("target_id", filters.target_id);
  qs.set("limit", String(filters.limit ?? 100));
  return apiGet<AuditEntry[]>(`/api/audit?${qs.toString()}`);
}

/** Resources currently flagged by a policy — the compliance drill-down (M9.3). */
export const getPolicyMatchedResources = (policyId: number): Promise<MatchedResource[]> =>
  apiGet<MatchedResource[]>(`/api/governance/policies/${policyId}/matches`);
