// SPDX-License-Identifier: FSL-1.1-ALv2
import { beforeEach, describe, expect, it, vi } from "vitest";
import { clearSession, getAccessToken, getSession, setSession, subscribe } from "../lib/session";

const USER = { subject: "auth0|abc", sessionId: "s-1", role: null };

beforeEach(() => {
  clearSession();
  localStorage.clear();
});

describe("the session starts empty", () => {
  it("reports no user and not authenticated", () => {
    const session = getSession();
    expect(session.user).toBeNull();
    expect(session.isAuthenticated).toBe(false);
    expect(getAccessToken()).toBeNull();
  });
});

describe("setSession", () => {
  it("makes the token readable and the session authenticated", () => {
    setSession("access-token-value", USER);
    expect(getAccessToken()).toBe("access-token-value");
    expect(getSession().isAuthenticated).toBe(true);
    expect(getSession().user).toEqual(USER);
  });

  it("replaces the previous token rather than accumulating", () => {
    setSession("first", USER);
    setSession("second", { subject: "auth0|xyz", sessionId: "s-2", role: null });
    expect(getAccessToken()).toBe("second");
    expect(getSession().user?.subject).toBe("auth0|xyz");
  });
});

describe("clearSession", () => {
  it("removes the token and the user", () => {
    setSession("access-token-value", USER);
    clearSession();
    expect(getAccessToken()).toBeNull();
    expect(getSession().user).toBeNull();
    expect(getSession().isAuthenticated).toBe(false);
  });
});

/**
 * The reason this module was rewritten. The previous version persisted the bearer token to
 * `localStorage`, which any script on the origin can read — so one XSS anywhere exfiltrates a
 * credential valid for its full lifetime. Durability was the only thing that bought, and the
 * backend's `httpOnly` refresh cookie buys it without the exposure.
 *
 * Asserted rather than trusted to the comment, because "we moved it into memory" is exactly the
 * kind of claim that quietly regresses the next time someone wants a session to survive a reload.
 */
describe("the access token never reaches web storage", () => {
  it("writes nothing to localStorage", () => {
    const setItem = vi.spyOn(Storage.prototype, "setItem");
    setSession("secret-bearer-token", USER);

    expect(setItem).not.toHaveBeenCalled();
    expect(localStorage.length).toBe(0);
    // Belt and braces: scan every value rather than only the key the old code happened to use, so
    // storing it under a different name is caught too.
    const stored = Object.keys(localStorage).map((k) => localStorage.getItem(k));
    expect(stored.join("|")).not.toContain("secret-bearer-token");
    setItem.mockRestore();
  });

  it("writes nothing to sessionStorage either", () => {
    setSession("secret-bearer-token", USER);
    expect(sessionStorage.length).toBe(0);
  });
});

describe("subscribe", () => {
  it("notifies on set and on clear, and stops after unsubscribing", () => {
    const listener = vi.fn();
    const unsubscribe = subscribe(listener);

    setSession("t", USER);
    expect(listener).toHaveBeenCalledTimes(1);

    clearSession();
    expect(listener).toHaveBeenCalledTimes(2);

    unsubscribe();
    setSession("t2", USER);
    // The AuthBoundary unsubscribes on unmount; a listener that kept firing would set state on an
    // unmounted tree.
    expect(listener).toHaveBeenCalledTimes(2);
  });
});
