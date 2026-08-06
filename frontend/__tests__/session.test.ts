// SPDX-License-Identifier: Apache-2.0
import { describe, it, expect, beforeEach } from "vitest";
import { getSession, setSession, clearSession } from "../lib/session";

describe("Frontend Session Management", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it("returns unauthenticated session by default", () => {
    const session = getSession();
    expect(session.isAuthenticated).toBe(false);
    expect(session.token).toBeNull();
  });

  it("stores and retrieves authenticated session", () => {
    setSession("fake-jwt-token", { id: "u1", username: "alice", role: "admin" });
    const session = getSession();
    expect(session.isAuthenticated).toBe(true);
    expect(session.token).toBe("fake-jwt-token");
    expect(session.user?.username).toBe("alice");
  });

  it("clears session on logout", () => {
    setSession("fake-jwt-token", { id: "u1", username: "alice", role: "admin" });
    clearSession();
    const session = getSession();
    expect(session.isAuthenticated).toBe(false);
  });
});
