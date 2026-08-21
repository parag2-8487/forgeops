// SPDX-License-Identifier: FSL-1.1-ALv2

/**
 * The access token, held in memory only, plus the non-secret profile fields drawn from it.
 *
 * WHY THIS IS NOT `localStorage` ANY MORE
 * The first version of this module wrote the bearer token to `localStorage` under
 * `forgeops_auth_token`. Nothing ever read it — `lib/api/client.ts` sent no credential at all —
 * so it was an unused store rather than a live weakness, but it was the wrong shape to start
 * wiring against for two reasons.
 *
 * First, `localStorage` is readable by any script on the origin, so one XSS anywhere in the app
 * exfiltrates a bearer token valid for its full lifetime. Second, and the reason the tradeoff is
 * avoidable rather than merely unfortunate: the backend already sets an `httpOnly` session cookie
 * on `/auth/callback`, and `POST /auth/refresh` exchanges that cookie for a fresh access token.
 * A page reload can therefore recover a session without the token ever having been persisted
 * anywhere a script can reach. Durability was the only thing `localStorage` was buying, and the
 * cookie buys it more safely.
 *
 * So the long-lived credential lives in a cookie no script can read, and the short-lived one lives
 * in a module variable that dies with the tab.
 */

export interface SessionUser {
  /** The `sub` claim. Not a display name — it is the subject identifier. */
  subject: string;
  sessionId: string | null;
}

export interface Session {
  user: SessionUser | null;
  isAuthenticated: boolean;
}

/** Deliberately not exported. Reachable only through the functions below. */
let accessToken: string | null = null;
let currentUser: SessionUser | null = null;

/** Notified on every change, so a React tree can re-render without polling. */
const listeners = new Set<() => void>();

function emit(): void {
  for (const listener of listeners) listener();
}

export function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function getAccessToken(): string | null {
  return accessToken;
}

export function getSession(): Session {
  return { user: currentUser, isAuthenticated: accessToken !== null };
}

/** Called by the auth bootstrap after `/auth/refresh` returns, and by nothing else. */
export function setSession(token: string, user: SessionUser): void {
  accessToken = token;
  currentUser = user;
  emit();
}

export function clearSession(): void {
  accessToken = null;
  currentUser = null;
  emit();
}
