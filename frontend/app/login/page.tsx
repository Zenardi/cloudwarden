"use client";

import { useState } from "react";
import { fetchLoginUrl } from "../lib/auth";

/**
 * SSO login page (M11.3). "Sign in" asks the backend for the identity provider's
 * authorization URL and redirects the browser there; after the IdP round-trip the
 * `/api/auth/callback` sets a first-party session cookie and the app is unlocked.
 */
export default function LoginPage() {
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function signIn() {
    setBusy(true);
    setError(null);
    try {
      window.location.href = await fetchLoginUrl();
    } catch (e) {
      setError(String(e));
      setBusy(false);
    }
  }

  return (
    <section className="card">
      <h1>Sign in</h1>
      <p>Authenticate with your organization&apos;s identity provider to continue.</p>
      <button onClick={signIn} disabled={busy}>
        {busy ? "Redirecting…" : "Sign in with SSO"}
      </button>
      {error ? <p style={{ color: "crimson" }}>{error}</p> : null}
    </section>
  );
}
