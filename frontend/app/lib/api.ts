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
  source: string;
  priority: number;
  rationale?: string | null;
  status: string;
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

export interface AISummary {
  executive_summary: string;
  total_potential_savings: number;
  currency: string;
  provider?: string;
  model?: string;
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
