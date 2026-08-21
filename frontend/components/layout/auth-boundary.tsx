// SPDX-License-Identifier: FSL-1.1-ALv2
"use client";

import { useEffect, useState } from "react";
import { useRouter, usePathname } from "next/navigation";
import { refreshAccessToken } from "@/lib/api/client";
import { getSession, subscribe } from "@/lib/session";

/**
 * Recovers a session on load, and sends an unauthenticated visitor to sign in.
 *
 * The access token lives in memory (see `lib/session.ts`), so it does not survive a reload. What
 * does survive is the `httpOnly` refresh cookie the backend set on `/auth/callback`, and
 * `POST /auth/refresh` exchanges it for a fresh access token. So a reload — and the redirect back
 * from the IdP, which is just a navigation to `next` — recovers the session by asking the server
 * rather than by having stored a bearer token where a script could read it.
 *
 * Three states, and the middle one matters: until the refresh attempt settles we know nothing, and
 * rendering the app would flash unauthenticated panels while rendering a redirect would throw away
 * a session the user has. So it renders neither.
 */
export function AuthBoundary({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();
  const [state, setState] = useState<"checking" | "authenticated" | "anonymous">(() =>
    getSession().isAuthenticated ? "authenticated" : "checking",
  );

  // Keeps this in step with a session cleared elsewhere — the client calls `clearSession()` when a
  // refresh fails, and without this subscription the tree would keep rendering panels that 401.
  useEffect(
    () => subscribe(() => setState(getSession().isAuthenticated ? "authenticated" : "anonymous")),
    [],
  );

  useEffect(() => {
    // Guarded on the state rather than re-reading the session synchronously and calling setState in
    // the effect body, which triggers a cascading render. The initialiser above already accounts
    // for an in-memory session, so reaching "checking" here means there is not one.
    if (state !== "checking") return;
    let cancelled = false;
    void refreshAccessToken().then((token) => {
      if (cancelled) return;
      setState(token ? "authenticated" : "anonymous");
    });
    return () => {
      cancelled = true;
    };
  }, [state]);

  useEffect(() => {
    if (state !== "anonymous") return;
    // `next` carries the deep link through the round trip, so signing in returns the operator to
    // the page they asked for rather than the dashboard.
    const next = encodeURIComponent(pathname || "/");
    router.replace(`/login?next=${next}`);
  }, [state, pathname, router]);

  if (state === "checking") {
    return (
      <div role="status" aria-live="polite" className="p-6 text-sm text-muted-foreground">
        Restoring your session…
      </div>
    );
  }

  if (state === "anonymous") {
    // The redirect is in flight. Announced rather than blank, so a screen reader is not left in
    // silence, and it deliberately does not render the app behind it.
    return (
      <div role="status" aria-live="polite" className="p-6 text-sm text-muted-foreground">
        Redirecting you to sign in…
      </div>
    );
  }

  return <>{children}</>;
}
