// SPDX-License-Identifier: FSL-1.1-ALv2
"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { api } from "@/lib/api";
import { clearSession, getSession, subscribe } from "@/lib/session";
import { Button } from "@/components/ui/button";
import { useEffect } from "react";

/**
 * Sign out.
 *
 * CALLS THE SERVER FIRST, and that ordering is the whole point. Clearing the in-memory access token
 * alone would leave the `httpOnly` refresh cookie in the browser and the session row live in the
 * database — so the very next page load would call `POST /auth/refresh`, be handed a fresh token, and
 * sign the user straight back in. A "log out" that the app undoes by itself on the next navigation is
 * worse than none, because the user believes they have left.
 *
 * `POST /api/v1/auth/logout` revokes the session server-side and clears the cookie. It always answers
 * 200, including when there is no live session (§4.4) — deliberately, because the common case is
 * clicking log out after a token expired, and a 401 there would leave the cookie in place.
 *
 * The local state is cleared regardless of what the server said. If the network failed, the safe
 * assumption for the person at the keyboard is that they are logged out of this tab; the alternative
 * leaves a token in memory on a screen that says "signed out".
 */
export function SignOutButton() {
  const router = useRouter();
  const [signedIn, setSignedIn] = useState(() => getSession().isAuthenticated);
  const [pending, setPending] = useState(false);

  useEffect(() => subscribe(() => setSignedIn(getSession().isAuthenticated)), []);

  // Nothing to offer an anonymous visitor; the sign-in screen is its own route.
  if (!signedIn) return null;

  return (
    <Button
      variant="outline"
      size="sm"
      disabled={pending}
      onClick={async () => {
        setPending(true);
        try {
          await api.post("/auth/logout");
        } catch {
          // Deliberately swallowed. The server call is best-effort from the browser's point of view:
          // whether or not it succeeded, this tab must stop holding a credential.
        } finally {
          clearSession();
          setPending(false);
          // `replace`, not `push`: Back should not return to an authenticated screen that will
          // immediately redirect, which reads as the app fighting the user.
          router.replace("/login");
        }
      }}
    >
      {pending ? "Signing out…" : "Sign out"}
    </Button>
  );
}
