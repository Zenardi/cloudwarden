export const API_BASE = process.env.NEXT_PUBLIC_API_BASE || "http://localhost:8000";
export const GRAFANA_BASE = process.env.NEXT_PUBLIC_GRAFANA_BASE || "http://localhost:3000";

export async function apiGet<T = any>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, { cache: "no-store" });
  if (!res.ok) throw new Error(`GET ${path} → ${res.status}`);
  return res.json();
}

export async function apiPost<T = any>(path: string, body?: unknown): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) throw new Error(`POST ${path} → ${res.status}`);
  return res.json();
}

export async function apiDelete<T = any>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, { method: "DELETE" });
  if (!res.ok) throw new Error(`DELETE ${path} → ${res.status}`);
  return res.json();
}

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
