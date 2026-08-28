// SPDX-License-Identifier: FSL-1.1-ALv2

/**
 * What the signed-in identity may do, mirrored from the backend's own route guards.
 *
 * WHY THIS EXISTS
 * Three routes are role-gated in `backend/src/`:
 *
 *   POST   /api/v1/agents/pairing-codes   require_role(ADMIN, DEVELOPER)
 *   DELETE /api/v1/agents/{device_id}     require_role(ADMIN)
 *   GET    /api/v1/audit/verify           require_role(ADMIN)
 *
 * The frontend modelled no roles at all, so a viewer was offered every one of those controls and
 * the first feedback was a 403 — a response that §4.2 makes deliberately uninformative, so the
 * user learns nothing except that something went wrong. A button whose first feedback is a refusal
 * is a broken button.
 *
 * WHAT THIS IS NOT
 * It is not authorisation. The backend decides, every time, and nothing here can widen anything:
 * hiding a control removes it from the page and removes it from nowhere else. The point is only
 * that the UI should not offer an action it can already tell will be refused.
 *
 * The map is DATA, and it names the route beside each capability, so a reader can check the mirror
 * against the original without leaving the file. A third role gate added on the backend without a
 * row here means the control is offered and 403s — which is the old behaviour, not a new failure —
 * and `__tests__/authz.test.ts` asserts the row set against the documented route list so the drift
 * is at least visible.
 *
 * THE UNKNOWN CASE IS DELIBERATE. `role === null` means the server did not tell us — a user deleted
 * at the IdP mid-session, or a backend that predates the field. Unknown resolves to PERMITTED, not
 * refused, and that is the right way round: a false negative hides a control an admin is entitled
 * to and leaves them with no route to the feature at all, whereas a false positive costs one 403
 * that the surrounding UI already renders properly. Failing closed is correct in an enforcement
 * path; this is not one.
 */

import type { Role } from "@/lib/session";

/** Every role-gated action the UI offers, keyed by what it does rather than by which route. */
export type Capability =
  /** `POST /api/v1/agents/pairing-codes` — admin or developer. */
  | "mint_pairing_code"
  /** `DELETE /api/v1/agents/{device_id}` — admin. */
  | "revoke_device"
  /** `GET /api/v1/audit/verify` — admin. */
  | "verify_audit_chain";

/**
 * Which roles satisfy each capability, and the route each mirrors.
 *
 * Written as the ALLOWED SET rather than as a minimum rank, because the backend's `require_role`
 * takes a set too. A rank comparison would work today only because the three roles happen to be
 * totally ordered, and would quietly break the first time a role is added that is not.
 */
export const CAPABILITY_ROLES: Readonly<Record<Capability, readonly Role[]>> = {
  mint_pairing_code: ["admin", "developer"],
  revoke_device: ["admin"],
  verify_audit_chain: ["admin"],
};

/** Human wording for a refusal, used where a control is disabled rather than removed. */
export const CAPABILITY_REQUIREMENT: Readonly<Record<Capability, string>> = {
  mint_pairing_code: "Minting a pairing code requires the admin or developer role.",
  revoke_device: "Revoking a device requires the admin role.",
  verify_audit_chain: "Verifying the audit chain requires the admin role.",
};

/**
 * Whether this role may perform this action.
 *
 * `null` — the role is not known — returns `true`. See the module docstring: this is a
 * presentation decision, and hiding a feature from someone entitled to it is the worse error.
 */
export function can(role: Role | null, capability: Capability): boolean {
  if (role === null) return true;
  return CAPABILITY_ROLES[capability].includes(role);
}

/**
 * Why an action is unavailable, or `null` when it is available.
 *
 * Returned as a sentence rather than a boolean so the calling component has something to render.
 * A disabled control with no stated reason is the same dead end as a 403.
 */
export function refusalReason(role: Role | null, capability: Capability): string | null {
  if (can(role, capability)) return null;
  return `${CAPABILITY_REQUIREMENT[capability]} You are signed in as ${role}.`;
}
