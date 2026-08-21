// SPDX-License-Identifier: FSL-1.1-ALv2
/**
 * The sign-in path (design.md §3.5, §12.6 step 1).
 *
 * Journey step 1 is "log in via OIDC" in a browser, and until this pass the shell had no sign-in
 * screen at all — but the deeper defect was in the API client, which sent no credential of any
 * kind: no `Authorization` header and no `credentials` option, so cookies were dropped on a
 * cross-origin fetch. Every authenticated panel was therefore guaranteed to 401 no matter how the
 * user had authenticated. These tests pin the credential path, not just the button.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { clearSession, getAccessToken, setSession } from "@/lib/session";

const mockReplace = vi.fn();
const mockSearchParams = { get: vi.fn() };

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: mockReplace, push: vi.fn() }),
  usePathname: () => "/readiness",
  useSearchParams: () => mockSearchParams,
}));

import LoginPage from "@/app/login/page";
import { AuthBoundary } from "@/components/layout/auth-boundary";
import { api, AUTH_HEADER, BEARER_SCHEME, refreshAccessToken } from "@/lib/api/client";

/** Built from the client's own constants, so the assertion cannot drift from the header it sends. */
const expectedAuth = (token: string) => [BEARER_SCHEME, token].join(" ");

const API_BASE = "http://localhost:8000/api/v1";

function jsonResponse(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => "application/json" },
    json: async () => body,
  } as unknown as Response;
}

function problemResponse(status: number) {
  return {
    ok: false,
    status,
    headers: { get: () => "application/problem+json" },
    json: async () => ({ type: "urn:x:unauthenticated", title: "Unauthenticated", status }),
  } as unknown as Response;
}

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  clearSession();
  mockReplace.mockClear();
  mockSearchParams.get.mockReset().mockReturnValue(null);
  fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("the sign-in screen", () => {
  it("renders exactly one h1 naming the application", () => {
    render(<LoginPage />);
    const headings = screen.getAllByRole("heading", { level: 1 });
    expect(headings).toHaveLength(1);
    expect(headings[0]).toHaveTextContent(/sign in to/i);
  });

  it("collects no credentials of its own", () => {
    render(<LoginPage />);
    // The substance of delegated authentication: a password field here would make this origin a
    // credential intermediary, which is exactly what authorization-code flow exists to avoid.
    expect(screen.queryByLabelText(/password/i)).not.toBeInTheDocument();
    expect(document.querySelector('input[type="password"]')).toBeNull();
    expect(document.querySelector("input")).toBeNull();
  });

  it("navigates to the backend's login endpoint, carrying next", async () => {
    mockSearchParams.get.mockImplementation((k: string) => (k === "next" ? "/readiness" : null));
    const assign = vi.fn();
    vi.stubGlobal("location", { assign, href: "http://localhost:3000/login" });

    render(<LoginPage />);
    await userEvent.click(screen.getByRole("button", { name: /single sign-on/i }));

    expect(assign).toHaveBeenCalledTimes(1);
    const target = assign.mock.calls[0][0] as string;
    // The BACKEND endpoint, because that is where the client secret and the PKCE verifier live.
    expect(target).toBe(`${API_BASE}/auth/login?next=%2Freadiness`);
  });

  it("refuses to carry an off-origin next through the round trip", async () => {
    mockSearchParams.get.mockImplementation((k: string) =>
      k === "next" ? "//evil.example/steal" : null,
    );
    const assign = vi.fn();
    vi.stubGlobal("location", { assign, href: "http://localhost:3000/login" });

    render(<LoginPage />);
    await userEvent.click(screen.getByRole("button", { name: /single sign-on/i }));

    // Reduced to "/" rather than forwarded. The backend's `_safe_next` is the real guard, but a
    // protocol-relative URL should not leave the browser either.
    expect(assign.mock.calls[0][0]).toBe(`${API_BASE}/auth/login?next=%2F`);
  });

  it("explains an expired session when it was sent here by one", () => {
    mockSearchParams.get.mockImplementation((k: string) => (k === "reason" ? "expired" : null));
    render(<LoginPage />);
    expect(screen.getByRole("status")).toHaveTextContent(/session ended/i);
  });
});

describe("the auth boundary", () => {
  it("mints an access token from the session cookie on mount", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({ access_token: "at-1", subject: "auth0|u1", session_id: "s-1" }),
    );

    render(
      <AuthBoundary>
        <p>protected content</p>
      </AuthBoundary>,
    );

    expect(await screen.findByText("protected content")).toBeInTheDocument();
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe(`${API_BASE}/auth/refresh`);
    expect(init.method).toBe("POST");
    // Without this the cookie is dropped and the refresh can never work.
    expect(init.credentials).toBe("include");
    expect(getAccessToken()).toBe("at-1");
  });

  it("announces the check rather than flashing the app", async () => {
    let release: (v: Response) => void = () => {};
    fetchMock.mockReturnValue(
      new Promise<Response>((resolve) => {
        release = resolve;
      }),
    );

    render(
      <AuthBoundary>
        <p>protected content</p>
      </AuthBoundary>,
    );

    // Neither the app nor a redirect while the answer is unknown: rendering children would flash
    // panels that 401, and redirecting would discard a session the user actually has.
    expect(screen.getByRole("status")).toHaveTextContent(/restoring your session/i);
    expect(screen.queryByText("protected content")).not.toBeInTheDocument();
    expect(mockReplace).not.toHaveBeenCalled();

    release(jsonResponse({ access_token: "at", subject: "s" }));
    await screen.findByText("protected content");
  });

  it("redirects an anonymous visitor to sign in, preserving the deep link", async () => {
    fetchMock.mockResolvedValue(problemResponse(401));

    render(
      <AuthBoundary>
        <p>protected content</p>
      </AuthBoundary>,
    );

    await waitFor(() => expect(mockReplace).toHaveBeenCalledWith("/login?next=%2Freadiness"));
    expect(screen.queryByText("protected content")).not.toBeInTheDocument();
  });

  it("renders immediately when a session is already in memory", async () => {
    setSession("existing", { subject: "auth0|u1", sessionId: "s-1" });

    render(
      <AuthBoundary>
        <p>protected content</p>
      </AuthBoundary>,
    );

    expect(screen.getByText("protected content")).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe("the API client's credential handling", () => {
  it("sends the bearer token and includes cookies", async () => {
    setSession("at-9", { subject: "s", sessionId: null });
    fetchMock.mockResolvedValue(jsonResponse({ ok: true }));

    await api.get("/projects");

    const [, init] = fetchMock.mock.calls[0];
    expect(init.headers[AUTH_HEADER]).toBe(expectedAuth("at-9"));
    expect(init.credentials).toBe("include");
  });

  it("sends no Authorization header when there is no token", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ ok: true }));
    await api.get("/projects");
    expect(fetchMock.mock.calls[0][1].headers[AUTH_HEADER]).toBeUndefined();
  });

  it("refreshes once and retries after a 401, then succeeds", async () => {
    setSession("stale", { subject: "s", sessionId: null });
    fetchMock
      .mockResolvedValueOnce(problemResponse(401))
      .mockResolvedValueOnce(jsonResponse({ access_token: "fresh", subject: "s" }))
      .mockResolvedValueOnce(jsonResponse({ projects: [] }));

    const result = await api.get<{ projects: unknown[] }>("/projects");

    expect(result).toEqual({ projects: [] });
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(fetchMock.mock.calls[1][0]).toBe(`${API_BASE}/auth/refresh`);
    // The retry must carry the NEW token; carrying the stale one would 401 again and the whole
    // exercise would be pointless.
    expect(fetchMock.mock.calls[2][1].headers[AUTH_HEADER]).toBe(expectedAuth("fresh"));
  });

  it("does not retry more than once, so a dead session cannot loop", async () => {
    setSession("stale", { subject: "s", sessionId: null });
    fetchMock
      .mockResolvedValueOnce(problemResponse(401))
      .mockResolvedValueOnce(problemResponse(401)) // the refresh itself fails
      .mockResolvedValue(problemResponse(401));

    await expect(api.get("/projects")).rejects.toThrow();
    // Original + refresh attempt. No third call: the refresh failed, so there is nothing to retry
    // with, and looping here is how a sign-in prompt becomes a hang.
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("clears the session when the refresh is refused", async () => {
    setSession("stale", { subject: "s", sessionId: null });
    fetchMock.mockResolvedValue(problemResponse(401));

    await refreshAccessToken();

    expect(getAccessToken()).toBeNull();
  });

  it("surfaces the real Problem after a failed retry rather than the refresh's", async () => {
    fetchMock
      .mockResolvedValueOnce(problemResponse(403))
      .mockResolvedValue(jsonResponse({ ok: true }));

    // A 403 is not a credential problem, so it must NOT trigger a refresh at all.
    await expect(api.get("/projects")).rejects.toMatchObject({ problem: { status: 403 } });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
