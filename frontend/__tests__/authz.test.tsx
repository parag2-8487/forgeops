// SPDX-License-Identifier: FSL-1.1-ALv2
/**
 * The client-side capability map, and the hook that binds it to the session.
 *
 * What is worth asserting here is not "does `can` return a boolean". It is the three decisions the
 * module makes that a reader should be able to challenge:
 *
 *  1. the map mirrors the backend's three role gates and nothing else;
 *  2. an UNKNOWN role permits, rather than refuses;
 *  3. a refusal comes with a sentence, not just a `false`.
 *
 * (2) is the one that looks wrong at first glance and is deliberate. See `lib/authz.ts`.
 */

import { describe, it, expect, afterEach } from "vitest";
import { renderHook, cleanup } from "@testing-library/react";
import {
  can,
  refusalReason,
  CAPABILITY_ROLES,
  CAPABILITY_REQUIREMENT,
  type Capability,
} from "@/lib/authz";
import { asRole } from "@/lib/session";
import { setSession, clearSession } from "@/lib/session";
import { useRole, useCapability } from "@/hooks/use-role";

afterEach(() => {
  clearSession();
  cleanup();
});

describe("asRole narrows the wire value", () => {
  it("accepts the three roles the backend defines", () => {
    expect(asRole("admin")).toBe("admin");
    expect(asRole("developer")).toBe("developer");
    expect(asRole("viewer")).toBe("viewer");
  });

  it("returns null for anything else, rather than a role this build does not model", () => {
    // A cast would let `CAPABILITY_ROLES[role]` be `undefined` and every check would throw on
    // `.includes`. Narrowing here is what keeps that impossible.
    for (const value of ["superuser", "ADMIN", "", null, undefined, 7, {}]) {
      expect(asRole(value)).toBeNull();
    }
  });
});

describe("the capability map mirrors the backend's role gates", () => {
  /**
   * The three role-gated routes in `backend/src`, as of this commit:
   *
   *   POST   /api/v1/agents/pairing-codes   require_role(ADMIN, DEVELOPER)
   *   DELETE /api/v1/agents/{device_id}     require_role(ADMIN)
   *   GET    /api/v1/audit/verify           require_role(ADMIN)
   *
   * Asserted as an exact set in both directions. A capability with no route behind it is dead text;
   * a route gate with no capability means the control is offered and 403s, which is the behaviour
   * this whole module exists to end.
   */
  const EXPECTED: Record<Capability, readonly string[]> = {
    mint_pairing_code: ["admin", "developer"],
    revoke_device: ["admin"],
    verify_audit_chain: ["admin"],
  };

  it("registers exactly the three gated actions", () => {
    expect(Object.keys(CAPABILITY_ROLES).sort()).toEqual(Object.keys(EXPECTED).sort());
  });

  it("grants each capability to exactly the roles the route requires", () => {
    for (const [capability, roles] of Object.entries(EXPECTED)) {
      expect([...CAPABILITY_ROLES[capability as Capability]].sort()).toEqual([...roles].sort());
    }
  });

  it("gives every capability a sentence to render when it is refused", () => {
    // A disabled control with no stated reason is the same dead end as a 403.
    for (const capability of Object.keys(CAPABILITY_ROLES) as Capability[]) {
      expect(CAPABILITY_REQUIREMENT[capability]).toMatch(/requires the/i);
    }
  });
});

describe("can", () => {
  it("lets an admin do everything gated", () => {
    for (const capability of Object.keys(CAPABILITY_ROLES) as Capability[]) {
      expect(can("admin", capability)).toBe(true);
    }
  });

  it("lets a developer mint a code but not revoke or verify", () => {
    expect(can("developer", "mint_pairing_code")).toBe(true);
    expect(can("developer", "revoke_device")).toBe(false);
    expect(can("developer", "verify_audit_chain")).toBe(false);
  });

  it("refuses a viewer everything gated", () => {
    for (const capability of Object.keys(CAPABILITY_ROLES) as Capability[]) {
      expect(can("viewer", capability)).toBe(false);
    }
  });

  it("PERMITS when the role is unknown, which is the deliberate direction", () => {
    // Failing closed is correct in an enforcement path. This is not one: the backend decides every
    // time and nothing here can widen anything. A false negative hides a control an admin is entitled
    // to and leaves them no route to the feature; a false positive costs one 403 that the surrounding
    // UI already renders properly.
    for (const capability of Object.keys(CAPABILITY_ROLES) as Capability[]) {
      expect(can(null, capability)).toBe(true);
    }
  });
});

describe("refusalReason", () => {
  it("is null when the action is allowed, so a caller can branch on it directly", () => {
    expect(refusalReason("admin", "revoke_device")).toBeNull();
    expect(refusalReason(null, "revoke_device")).toBeNull();
  });

  it("names the requirement and the role the caller actually has", () => {
    const reason = refusalReason("viewer", "revoke_device");
    expect(reason).toContain("requires the admin role");
    // Saying which role you ARE matters: "requires admin" alone leaves someone who believes they are
    // an admin with no way to tell that their session says otherwise.
    expect(reason).toContain("viewer");
  });
});

describe("useRole reads the live session", () => {
  it("returns null before a session exists", () => {
    const { result } = renderHook(() => useRole());
    expect(result.current).toBeNull();
  });

  it("returns the role the session carries", () => {
    setSession("t", { subject: "s", sessionId: null, role: "developer" });
    const { result } = renderHook(() => useRole());
    // `useSyncExternalStore` reads and subscribes in one step, so the FIRST render sees the store's
    // real contents. An effect-based hook would render `null` and then correct itself, which makes a
    // control visibly appear after paint.
    expect(result.current).toBe("developer");
  });

  it("follows a session change without a remount", () => {
    setSession("t", { subject: "s", sessionId: null, role: "viewer" });
    const { result, rerender } = renderHook(() => useRole());
    expect(result.current).toBe("viewer");

    setSession("t2", { subject: "s", sessionId: null, role: "admin" });
    rerender();
    expect(result.current).toBe("admin");
  });

  it("goes back to null when the session is cleared", () => {
    setSession("t", { subject: "s", sessionId: null, role: "admin" });
    const { result, rerender } = renderHook(() => useRole());
    clearSession();
    rerender();
    expect(result.current).toBeNull();
  });
});

describe("useCapability returns both the verdict and the reason", () => {
  it("allows and gives no reason for a permitted action", () => {
    setSession("t", { subject: "s", sessionId: null, role: "admin" });
    const { result } = renderHook(() => useCapability("revoke_device"));
    expect(result.current).toEqual({ allowed: true, reason: null });
  });

  it("refuses and gives a reason for a gated action", () => {
    setSession("t", { subject: "s", sessionId: null, role: "viewer" });
    const { result } = renderHook(() => useCapability("verify_audit_chain"));
    expect(result.current.allowed).toBe(false);
    // Both, from one hook, so a component cannot use the boolean and forget the explanation — which
    // is how a control ends up silently absent.
    expect(result.current.reason).toMatch(/requires the admin role/i);
  });
});
