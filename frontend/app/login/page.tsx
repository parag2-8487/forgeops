// SPDX-License-Identifier: FSL-1.1-ALv2
"use client";

import { Suspense, useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import { env } from "@/lib/env";
import { Button } from "@/components/ui/button";

/**
 * The sign-in screen (design.md §3.5, §12.6 step 1).
 *
 * Outside `(shell)` on purpose: the shell renders the sidebar and header for a signed-in operator,
 * and wrapping a sign-in page in navigation for an app you cannot yet use is both odd and a source
 * of dead links.
 *
 * This page holds NO credential logic. There is no username field, no password field and no token
 * handling, because the browser must authenticate at the IdP rather than here — sending a secret to
 * this origin would make the application a credential intermediary, which is the whole thing
 * authorization-code flow exists to avoid. The button navigates to the backend's `/auth/login`,
 * which builds the PKCE challenge, stores the pending verifier and 302s to Authentik.
 *
 * A full navigation rather than `fetch`: the response is a cross-origin redirect to the IdP, which
 * has to become the browser's own location for the user to see a login form and for the IdP to set
 * its own session cookie.
 *
 * SPLIT INTO TWO COMPONENTS BECAUSE `useSearchParams` FORCES IT. Next.js cannot statically prerender
 * a component that reads the query string, and without a Suspense boundary `next build` fails
 * outright — "useSearchParams() should be wrapped in a suspense boundary". Neither the dev server
 * nor the unit tests surface that: it appears only in a production build, which is why the
 * boundary is here rather than discovered in a deployment.
 */
function SignInPanel() {
  const params = useSearchParams();
  const [signingIn, setSigningIn] = useState(false);

  // Preserved across the round trip so a deep link survives sign-in. `_safe_next` on the backend
  // reduces it to a same-origin absolute path, so this cannot be steered off-origin — but it is
  // also constrained here so an obviously bad value never leaves the browser.
  const raw = params.get("next");
  const next = raw && raw.startsWith("/") && !raw.startsWith("//") ? raw : "/";
  const reason = params.get("reason");

  const loginPath = process.env.NEXT_PUBLIC_OIDC_LOGIN_PATH ?? "/auth/login";
  const target = `${env.NEXT_PUBLIC_API_BASE_URL}${loginPath}?next=${encodeURIComponent(next)}`;

  // Reset if the user comes back via the history stack, so the button is not stuck disabled.
  useEffect(() => {
    const restore = () => setSigningIn(false);
    window.addEventListener("pageshow", restore);
    return () => window.removeEventListener("pageshow", restore);
  }, []);

  return (
    <div className="w-full max-w-md space-y-6 rounded-lg border border-border bg-background p-8">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Sign in to {env.NEXT_PUBLIC_APP_NAME}</h1>
        <p className="mt-2 text-sm text-muted-foreground">
          Authentication is delegated to the identity provider. This screen collects nothing.
        </p>
      </div>

      {reason === "expired" ? (
        <div
          role="status"
          className="rounded-md border border-amber-500/40 bg-amber-500/5 p-3 text-sm"
        >
          Your session ended and could not be renewed. Signing in again will restore it.
        </div>
      ) : null}

      <Button
        className="w-full"
        disabled={signingIn}
        onClick={() => {
          setSigningIn(true);
          // Full navigation, not fetch: the IdP must become the browser's own location.
          window.location.assign(target);
        }}
      >
        {signingIn ? "Redirecting to the identity provider…" : "Continue with single sign-on"}
      </Button>

      <p className="text-xs text-muted-foreground">
        You will be returned to <code>{next}</code> once the identity provider has verified you. No
        secret is ever sent to this application: it receives an authorization code, exchanges it
        server-side, and never sees your credentials.
      </p>
    </div>
  );
}

export default function LoginPage() {
  return (
    <main className="flex min-h-dvh items-center justify-center p-6">
      <Suspense
        fallback={
          <div role="status" aria-live="polite" className="text-sm text-muted-foreground">
            Preparing sign-in…
          </div>
        }
      >
        <SignInPanel />
      </Suspense>
    </main>
  );
}
