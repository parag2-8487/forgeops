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
  /**
   * The authenticated principal's role, as `POST /auth/refresh` reports it.
   *
   * `null` when the server did not say. That is a real state rather than a defensive default: a
   * user deleted at the IdP while a session is live has no row for `load_user` to read, and an
   * older backend does not return the field at all. It is deliberately NOT defaulted to `viewer`,
   * because "we do not know" and "we know they are a viewer" want different treatment — the first
   * should let a control be attempted and report what the server says, the second should hide it.
   * `lib/authz.ts` is where that distinction is spent.
   */
  role: Role | null;
}

/**
 * The three roles `backend/src/auth/models.py::UserRole` defines.
 *
 * A union of literals rather than an enum, so an unrecognised value from the wire fails the narrow
 * in `asRole` and becomes `null` instead of silently becoming a role this build does not model.
 */
export type Role = "admin" | "developer" | "viewer";

const ROLES: readonly Role[] = ["admin", "developer", "viewer"];

/** Narrow an unknown wire value to a `Role`, or `null`. Never throws, never guesses. */
export function asRole(value: unknown): Role | null {
  return typeof value === "string" && (ROLES as readonly string[]).includes(value)
    ? (value as Role)
    : null;
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
