import { API_BASE } from "./api";

/**
 * Client-side auth gating is opt-in via `NEXT_PUBLIC_AUTH_ENABLED` (default off), so
 * local/mock dev and the existing UI render without a login. Turn it on only when the
 * backend has `OIDC_ENABLED=true` — otherwise the login endpoint 404s.
 */
export const AUTH_ENABLED = process.env.NEXT_PUBLIC_AUTH_ENABLED === "true";

export interface Me {
  principal: string | null;
  permissions: string[];
  rbac_enabled: boolean;
}

/** The caller's identity + permissions (never throws — anonymous on any error). */
export async function fetchMe(): Promise<Me> {
  try {
    const res = await fetch(`${API_BASE}/api/authz/me`, {
      credentials: "include",
      cache: "no-store",
    });
    if (!res.ok) return { principal: null, permissions: [], rbac_enabled: false };
    return (await res.json()) as Me;
  } catch {
    return { principal: null, permissions: [], rbac_enabled: false };
  }
}

/** The identity provider's authorization URL to begin the SSO login redirect. */
export async function fetchLoginUrl(): Promise<string> {
  const res = await fetch(`${API_BASE}/api/auth/login`, {
    credentials: "include",
    cache: "no-store",
  });
  if (!res.ok) throw new Error(`login unavailable (${res.status})`);
  return ((await res.json()) as { authorization_url: string }).authorization_url;
}

/** Clear the first-party session cookie. */
export async function logout(): Promise<void> {
  await fetch(`${API_BASE}/api/auth/logout`, { method: "POST", credentials: "include" });
}
