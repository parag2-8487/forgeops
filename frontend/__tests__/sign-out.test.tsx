// SPDX-License-Identifier: FSL-1.1-ALv2
/**
 * Signing out (design.md §3.5, §4.4).
 *
 * The property that matters is the ORDER, not the button. Clearing the in-memory token alone leaves
 * the `httpOnly` refresh cookie in the browser and the session row live in the database, so the next
 * page load calls `/auth/refresh`, is handed a fresh token, and signs the user back in. A log-out the
 * app quietly undoes is worse than none, because the person believes they have left.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { clearSession, getAccessToken, setSession } from "@/lib/session";

const { mockPost } = vi.hoisted(() => ({ mockPost: vi.fn() }));
const mockReplace = vi.fn();

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, api: { ...actual.api, post: mockPost } };
});

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: mockReplace, push: vi.fn() }),
  usePathname: () => "/",
}));

import { SignOutButton } from "@/components/layout/sign-out-button";
import { AppHeader } from "@/components/layout/app-header";

const USER = { subject: "auth0|abc123", sessionId: "s-1", role: null };

beforeEach(() => {
  clearSession();
  mockPost.mockReset().mockResolvedValue({ status: "logged_out" });
  mockReplace.mockClear();
});

describe("the sign-out control", () => {
  it("is not offered to an anonymous visitor", () => {
    render(<SignOutButton />);
    expect(screen.queryByRole("button", { name: /sign out/i })).not.toBeInTheDocument();
  });

  it("appears once a session exists", () => {
    setSession("token", USER);
    render(<SignOutButton />);
    expect(screen.getByRole("button", { name: /sign out/i })).toBeInTheDocument();
  });

  it("revokes the session on the SERVER, not just in the tab", async () => {
    setSession("token", USER);
    render(<SignOutButton />);
    await userEvent.click(screen.getByRole("button", { name: /sign out/i }));

    // Without this call the refresh cookie survives and the next load signs the user back in.
    await waitFor(() => expect(mockPost).toHaveBeenCalledWith("/auth/logout"));
  });

  it("clears the in-memory token and redirects to sign-in", async () => {
    setSession("token", USER);
    render(<SignOutButton />);
    await userEvent.click(screen.getByRole("button", { name: /sign out/i }));

    await waitFor(() => expect(getAccessToken()).toBeNull());
    // `replace`, so Back does not return to an authenticated screen that immediately redirects.
    expect(mockReplace).toHaveBeenCalledWith("/login");
  });

  it("still clears the tab when the server call fails", async () => {
    setSession("token", USER);
    mockPost.mockRejectedValue(new Error("network down"));
    render(<SignOutButton />);
    await userEvent.click(screen.getByRole("button", { name: /sign out/i }));

    // The safe assumption for the person at the keyboard is that they are logged out of this tab.
    // Leaving a token in memory behind a screen that says "signed out" is the worse failure.
    await waitFor(() => expect(getAccessToken()).toBeNull());
    expect(mockReplace).toHaveBeenCalledWith("/login");
  });
});

describe("the header names who is signed in", () => {
  it("shows nothing when anonymous", () => {
    render(<AppHeader />);
    expect(screen.queryByText(/signed in as/i)).not.toBeInTheDocument();
  });

  it("shows the OIDC subject, which is what the audit log records", () => {
    setSession("token", USER);
    render(<AppHeader />);
    expect(screen.getByText(/signed in as/i)).toBeInTheDocument();
    // The subject, not a friendly name: a display name here and an opaque id in the audit trail
    // would make the two impossible to line up by eye.
    expect(screen.getByText("auth0|abc123")).toBeInTheDocument();
  });
});
