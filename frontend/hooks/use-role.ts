// SPDX-License-Identifier: FSL-1.1-ALv2
"use client";

import { useSyncExternalStore } from "react";
import { getSession, subscribe, type Role } from "@/lib/session";
import { can, refusalReason, type Capability } from "@/lib/authz";

/**
 * The signed-in identity's role, and whether it may do a given thing.
 *
 * `useSyncExternalStore` rather than `useState` + `useEffect`, because `lib/session` is exactly the
 * external mutable store that API is for: the role arrives from `POST /auth/refresh` during
 * `AuthBoundary`'s bootstrap, which can settle before a component's effect runs. The effect version
 * of this hook would render once with a stale snapshot and then correct itself, so a control would
 * visibly appear or disappear after paint. This subscribes and reads in one step, so the first
 * render already sees whatever the store holds.
 *
 * `getServerSnapshot` returns `null` because there is no session on the server: these pages are
 * client components behind `AuthBoundary`, and pretending to know a role during SSR would produce
 * markup that hydration then has to replace.
 */
function readRole(): Role | null {
  return getSession().user?.role ?? null;
}

function serverRole(): Role | null {
  return null;
}

export function useRole(): Role | null {
  return useSyncExternalStore(subscribe, readRole, serverRole);
}

/**
 * A capability check bound to the current session.
 *
 * Returns both the boolean and the sentence, because every call site needs both: one to decide
 * whether to render the control, the other to say why it is not there. Splitting them into two
 * hooks would let a component use the first and forget the second, which is how a control ends up
 * silently absent.
 */
export function useCapability(capability: Capability): { allowed: boolean; reason: string | null } {
  const role = useRole();
  return { allowed: can(role, capability), reason: refusalReason(role, capability) };
}
