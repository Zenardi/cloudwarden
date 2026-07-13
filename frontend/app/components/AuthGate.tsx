"use client";

import { usePathname, useRouter } from "next/navigation";
import type { ReactNode } from "react";
import { useEffect, useState } from "react";
import { AUTH_ENABLED, fetchMe } from "../lib/auth";

/**
 * Gates the app behind SSO login when `NEXT_PUBLIC_AUTH_ENABLED` is set (M11.3).
 *
 * With gating off (the default) it renders children immediately — no network, so
 * local/mock dev and the route smoke tests are unaffected. With gating on it checks
 * `/api/authz/me`; an anonymous caller is redirected to `/login` (which is never
 * gated, to avoid a redirect loop).
 */
export default function AuthGate({ children }: { children: ReactNode }) {
  const [ready, setReady] = useState(!AUTH_ENABLED);
  const pathname = usePathname();
  const router = useRouter();

  useEffect(() => {
    if (!AUTH_ENABLED || pathname === "/login") {
      setReady(true);
      return;
    }
    let active = true;
    fetchMe().then((me) => {
      if (!active) return;
      if (me.principal) setReady(true);
      else router.replace("/login");
    });
    return () => {
      active = false;
    };
  }, [pathname, router]);

  if (!ready) return null;
  return <>{children}</>;
}
