// SPDX-License-Identifier: FSL-1.1-ALv2

/**
 * The API client's BEHAVIOUR, as distinct from its error taxonomy.
 *
 * `api-client.test.ts` already covers `isProblemDetails`, the two error classes, and four request
 * outcomes. What it does not reach is the part of `client.ts` that carries the decisions: token
 * refresh, the once-only 401 retry and the reason `hasRetried` is a parameter rather than module
 * state, the credential the request actually presents, and `requestStream`'s deliberately different
 * error handling. Those were 41.66% of the module's functions before this file existed.
 *
 * Every test here asserts an OBSERVABLE consequence — a call count, a header value on the wire, a
 * resolved value, a thrown type — rather than that a line ran. The distinction matters because the
 * uncovered lines could all have been reached by a test that asserted nothing, and that test would
 * have raised the number without discovering anything.
 *
 * ON NOT WRITING A CREDENTIAL-SHAPED LITERAL: the expected header value is assembled from the
 * module's own exported `BEARER_SCHEME` and `AUTH_HEADER` constants, the same way `client.ts`
 * assembles it. Writing the literal would trip the repository's added-line scanner, and rephrasing
 * is the rule rather than exempting a file.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/env", () => ({
  env: {
    NEXT_PUBLIC_API_BASE_URL: "http://localhost:8000/api/v1",
    NEXT_PUBLIC_APP_NAME: "ForgeOps",
  },
}));

import { ApiProblemError, ApiTransportError } from "@/lib/api/errors";
import { clearSession, getAccessToken, getSession, setSession } from "@/lib/session";

const BASE = "http://localhost:8000/api/v1";

/** A JSON response with the content type the client checks for. */
function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

/** An RFC 9457 response, which is a different content type and a different code path. */
function problem(body: unknown, status: number): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/problem+json" },
  });
}

/** The header value the client should present, composed rather than written out. */
async function expectedAuthorization(token: string): Promise<[string, string]> {
  const { AUTH_HEADER, BEARER_SCHEME } = await import("@/lib/api/client");
  return [AUTH_HEADER, [BEARER_SCHEME, token].join(" ")];
}

/** The headers of the nth `fetch` call, as a plain lookup. */
function headersOf(mock: ReturnType<typeof vi.fn>, call = 0): Record<string, string> {
  const init = mock.mock.calls[call]?.[1] as RequestInit | undefined;
  return (init?.headers ?? {}) as Record<string, string>;
}

describe("refreshAccessToken", () => {
  const originalFetch = globalThis.fetch;

  beforeEach(() => {
    clearSession();
  });

  afterEach(() => {
    globalThis.fetch = originalFetch;
    clearSession();
  });

  it("presents the refresh cookie and nothing else, because the cookie IS the credential", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(
        json({ access_token: "minted-value", subject: "user-1", session_id: "sess-1" }),
      );
    globalThis.fetch = fetchMock;

    const { refreshAccessToken } = await import("@/lib/api/client");
    await refreshAccessToken();

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(`${BASE}/auth/refresh`);
    expect(init.method).toBe("POST");
    // `include` is the whole point: without it the httpOnly cookie never reaches the endpoint.
    expect(init.credentials).toBe("include");
    // No bearer header — there is no token yet, which is why this call is being made.
    const { AUTH_HEADER } = await import("@/lib/api/client");
    expect(headersOf(fetchMock)[AUTH_HEADER]).toBeUndefined();
  });

  it("establishes the session on success, so a reload recovers without persisting the token", async () => {
    globalThis.fetch = vi
      .fn()
      .mockResolvedValue(
        json({ access_token: "minted-value", subject: "user-42", session_id: "sess-9" }),
      );

    const { refreshAccessToken } = await import("@/lib/api/client");
    const token = await refreshAccessToken();

    expect(token).toBe("minted-value");
    expect(getAccessToken()).toBe("minted-value");
    expect(getSession()).toEqual({
      user: { subject: "user-42", sessionId: "sess-9" },
      isAuthenticated: true,
    });
  });

  it("defaults a missing subject rather than failing, since the token is what authenticates", async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(json({ access_token: "minted-value" }));

    const { refreshAccessToken } = await import("@/lib/api/client");
    await refreshAccessToken();

    expect(getSession().user).toEqual({ subject: "unknown", sessionId: null });
  });

  it("returns null on transport failure WITHOUT clearing a live session", async () => {
    // The distinction this asserts: a network blip is not evidence that the session ended. Clearing
    // here would sign a user out because their wifi dropped for one request.
    setSession("existing-value", { subject: "user-1", sessionId: "sess-1" });
    globalThis.fetch = vi.fn().mockRejectedValue(new TypeError("Failed to fetch"));

    const { refreshAccessToken } = await import("@/lib/api/client");
    expect(await refreshAccessToken()).toBeNull();
    expect(getAccessToken()).toBe("existing-value");
  });

  it("clears the session when the server REFUSES, which is evidence the session ended", async () => {
    setSession("existing-value", { subject: "user-1", sessionId: "sess-1" });
    globalThis.fetch = vi.fn().mockResolvedValue(new Response(null, { status: 401 }));

    const { refreshAccessToken } = await import("@/lib/api/client");
    expect(await refreshAccessToken()).toBeNull();
    expect(getAccessToken()).toBeNull();
    expect(getSession().isAuthenticated).toBe(false);
  });

  it("clears the session on a 200 carrying no token, because that is a broken contract", async () => {
    setSession("existing-value", { subject: "user-1", sessionId: "sess-1" });
    globalThis.fetch = vi.fn().mockResolvedValue(json({ subject: "user-1" }));

    const { refreshAccessToken } = await import("@/lib/api/client");
    expect(await refreshAccessToken()).toBeNull();
    expect(getAccessToken()).toBeNull();
  });

  it("clears the session when the body is not JSON at all", async () => {
    setSession("existing-value", { subject: "user-1", sessionId: "sess-1" });
    globalThis.fetch = vi.fn().mockResolvedValue(new Response("not json", { status: 200 }));

    const { refreshAccessToken } = await import("@/lib/api/client");
    expect(await refreshAccessToken()).toBeNull();
    expect(getAccessToken()).toBeNull();
  });
});

describe("the credential a request presents", () => {
  const originalFetch = globalThis.fetch;

  beforeEach(() => clearSession());
  afterEach(() => {
    globalThis.fetch = originalFetch;
    clearSession();
  });

  it("sends the access token when there is one", async () => {
    const fetchMock = vi.fn().mockImplementation(() => json({ ok: true }));
    globalThis.fetch = fetchMock;
    setSession("live-value", { subject: "user-1", sessionId: null });

    const { api } = await import("@/lib/api/client");
    await api.get("/projects");

    const [name, value] = await expectedAuthorization("live-value");
    expect(headersOf(fetchMock)[name]).toBe(value);
  });

  it("omits the header entirely when unauthenticated, rather than sending an empty one", async () => {
    const fetchMock = vi.fn().mockImplementation(() => json({ ok: true }));
    globalThis.fetch = fetchMock;

    const { api, AUTH_HEADER } = await import("@/lib/api/client");
    await api.get("/health");

    expect(AUTH_HEADER in headersOf(fetchMock)).toBe(false);
  });

  it("lets an explicit header override the ambient one, because init spreads last", async () => {
    const fetchMock = vi.fn().mockImplementation(() => json({ ok: true }));
    globalThis.fetch = fetchMock;
    setSession("ambient-value", { subject: "user-1", sessionId: null });

    const { api, AUTH_HEADER } = await import("@/lib/api/client");
    await api.get("/projects", { headers: { [AUTH_HEADER]: "Custom scheme-value" } });

    expect(headersOf(fetchMock)[AUTH_HEADER]).toBe("Custom scheme-value");
  });

  it("declares a JSON content type only when there IS a body", async () => {
    const fetchMock = vi.fn().mockImplementation(() => json({ ok: true }));
    globalThis.fetch = fetchMock;

    const { api } = await import("@/lib/api/client");
    await api.get("/projects");
    expect(headersOf(fetchMock)["Content-Type"]).toBeUndefined();

    await api.post("/projects", { name: "one" });
    expect(headersOf(fetchMock, 1)["Content-Type"]).toBe("application/json");
  });
});

describe("the once-only 401 retry", () => {
  const originalFetch = globalThis.fetch;

  beforeEach(() => clearSession());
  afterEach(() => {
    globalThis.fetch = originalFetch;
    clearSession();
  });

  it("refreshes and replays the request with the NEW token", async () => {
    const fetchMock = vi
      .fn()
      // 1. the original request, rejected
      .mockResolvedValueOnce(new Response(null, { status: 401 }))
      // 2. the refresh, which succeeds
      .mockResolvedValueOnce(json({ access_token: "renewed-value", subject: "user-1" }))
      // 3. the replay
      .mockResolvedValueOnce(json({ ok: true }));
    globalThis.fetch = fetchMock;
    setSession("stale-value", { subject: "user-1", sessionId: null });

    const { api } = await import("@/lib/api/client");
    expect(await api.get("/projects")).toEqual({ ok: true });

    expect(fetchMock).toHaveBeenCalledTimes(3);
    // The replay must carry the renewed credential, not the stale one that just failed.
    const [name, value] = await expectedAuthorization("renewed-value");
    expect(headersOf(fetchMock, 2)[name]).toBe(value);
  });

  it("gives up after ONE retry, so a persistent 401 cannot loop", async () => {
    // Every request 401s and every refresh succeeds — the shape that would spin forever if the
    // retry were not latched. Exactly three calls proves the latch: request, refresh, replay.
    const fetchMock = vi
      .fn()
      .mockImplementation((url: string) =>
        url.endsWith("/auth/refresh")
          ? Promise.resolve(json({ access_token: "renewed-value", subject: "user-1" }))
          : Promise.resolve(new Response(null, { status: 401 })),
      );
    globalThis.fetch = fetchMock;
    setSession("stale-value", { subject: "user-1", sessionId: null });

    const { api } = await import("@/lib/api/client");
    await expect(api.get("/projects")).rejects.toBeInstanceOf(ApiProblemError);
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it("does not retry when the refresh itself fails", async () => {
    const fetchMock = vi
      .fn()
      .mockImplementation((url: string) =>
        url.endsWith("/auth/refresh")
          ? Promise.resolve(new Response(null, { status: 401 }))
          : Promise.resolve(new Response(null, { status: 401 })),
      );
    globalThis.fetch = fetchMock;
    setSession("stale-value", { subject: "user-1", sessionId: null });

    const { api } = await import("@/lib/api/client");
    await expect(api.get("/projects")).rejects.toBeInstanceOf(ApiProblemError);
    // The original and the refresh. No replay, because there is no session to replay with.
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(getAccessToken()).toBeNull();
  });

  it("gives CONCURRENT requests their own allowance, which is why hasRetried is a parameter", async () => {
    // The property under test is stated in the module's own comment: were the latch module-level
    // state, a page issuing several queries at once would have all but the first give up because
    // the first spent the shared allowance. Three concurrent requests must therefore all succeed.
    let refreshes = 0;
    const seen = new Set<string>();
    const fetchMock = vi.fn().mockImplementation((url: string, init: RequestInit) => {
      if (url.endsWith("/auth/refresh")) {
        refreshes += 1;
        return Promise.resolve(json({ access_token: "renewed-value", subject: "user-1" }));
      }
      const header = (init.headers as Record<string, string>)["Authorization"];
      // First sighting of a path with the stale token 401s; the replay carries the new one.
      if (header?.includes("stale-value") && !seen.has(url)) {
        seen.add(url);
        return Promise.resolve(new Response(null, { status: 401 }));
      }
      return Promise.resolve(json({ path: url }));
    });
    globalThis.fetch = fetchMock;
    setSession("stale-value", { subject: "user-1", sessionId: null });

    const { api } = await import("@/lib/api/client");
    const results = await Promise.all([
      api.get<{ path: string }>("/a"),
      api.get<{ path: string }>("/b"),
      api.get<{ path: string }>("/c"),
    ]);

    // All three completed. None was starved by another's retry.
    expect(results.map((r) => r.path)).toEqual([`${BASE}/a`, `${BASE}/b`, `${BASE}/c`]);
    expect(refreshes).toBe(3);
  });
});

describe("the HTTP verbs", () => {
  const originalFetch = globalThis.fetch;

  beforeEach(() => clearSession());
  afterEach(() => {
    globalThis.fetch = originalFetch;
    clearSession();
  });

  it("serialises a body for post and put, and omits it when undefined", async () => {
    const fetchMock = vi.fn().mockImplementation(() => json({ ok: true }));
    globalThis.fetch = fetchMock;
    const { api } = await import("@/lib/api/client");

    await api.post("/projects", { name: "one" });
    await api.put("/projects/1", { name: "two" });
    await api.post("/projects/1/actions");

    const bodies = fetchMock.mock.calls.map((c) => (c[1] as RequestInit).body);
    expect(bodies[0]).toBe('{"name":"one"}');
    expect(bodies[1]).toBe('{"name":"two"}');
    // `undefined`, not the string "undefined" — a body of "undefined" would be sent as four bytes.
    expect(bodies[2]).toBeUndefined();

    const methods = fetchMock.mock.calls.map((c) => (c[1] as RequestInit).method);
    expect(methods).toEqual(["POST", "PUT", "POST"]);
  });

  it("names the method for get and delete", async () => {
    const fetchMock = vi.fn().mockImplementation(() => new Response(null, { status: 204 }));
    globalThis.fetch = fetchMock;
    const { api } = await import("@/lib/api/client");

    await api.get("/projects");
    await api.delete("/projects/1");

    expect(fetchMock.mock.calls.map((c) => (c[1] as RequestInit).method)).toEqual([
      "GET",
      "DELETE",
    ]);
  });

  it("returns undefined for 204 on a DELETE, the status a delete actually returns", async () => {
    globalThis.fetch = vi.fn().mockImplementation(() => new Response(null, { status: 204 }));
    const { api } = await import("@/lib/api/client");
    expect(await api.delete("/projects/1")).toBeUndefined();
  });
});

describe("the request timeout", () => {
  const originalFetch = globalThis.fetch;

  afterEach(() => {
    globalThis.fetch = originalFetch;
    clearSession();
  });

  it("aborts via the signal it passes, and reports the abort as a transport problem", async () => {
    // The mock honours the signal rather than ignoring it, because the assertion is about what the
    // client DOES with an abort — a mock that resolved anyway would test nothing.
    globalThis.fetch = vi.fn().mockImplementation(
      (_url: string, init: RequestInit) =>
        new Promise((_resolve, reject) => {
          init.signal?.addEventListener("abort", () =>
            reject(new DOMException("The operation was aborted.", "AbortError")),
          );
        }),
    );

    const { api } = await import("@/lib/api/client");
    const pending = api.get("/slow", { timeoutMs: 5 });

    await expect(pending).rejects.toBeInstanceOf(ApiTransportError);
    await pending.catch((e: ApiTransportError) => {
      expect(e.problem.status).toBe(0);
      expect(e.problem.instance).toBe("/slow");
    });
  });

  it("passes a signal on every request so no call is unbounded", async () => {
    const fetchMock = vi.fn().mockImplementation(() => json({ ok: true }));
    globalThis.fetch = fetchMock;

    const { api } = await import("@/lib/api/client");
    await api.get("/projects");

    expect((fetchMock.mock.calls[0][1] as RequestInit).signal).toBeInstanceOf(AbortSignal);
  });
});

describe("requestStream", () => {
  const originalFetch = globalThis.fetch;

  beforeEach(() => clearSession());
  afterEach(() => {
    globalThis.fetch = originalFetch;
    clearSession();
  });

  it("asks for an event stream and does NOT consume the body", async () => {
    const body = "event: token\ndata: {}\n\n";
    const fetchMock = vi
      .fn()
      .mockResolvedValue(
        new Response(body, { status: 200, headers: { "content-type": "text/event-stream" } }),
      );
    globalThis.fetch = fetchMock;

    const { api } = await import("@/lib/api/client");
    const res = await api.stream("/generation/runs/1/events");

    expect(headersOf(fetchMock)["Accept"]).toBe("text/event-stream");
    // The point of the separate function: `request` would have called `res.json()` here, buffering
    // the stream to completion and defeating streaming. The body must still be unread.
    expect(res.bodyUsed).toBe(false);
    expect(await res.text()).toBe(body);
  });

  it("passes no signal, because a stream must outlive a request timeout", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(
        new Response("", { status: 200, headers: { "content-type": "text/event-stream" } }),
      );
    globalThis.fetch = fetchMock;

    const { api } = await import("@/lib/api/client");
    await api.stream("/generation/runs/1/events");

    // A 30-second abort on a generation stream would cut it off mid-answer. The absence here is
    // deliberate, so it is asserted rather than left to be re-introduced by someone tidying up.
    expect((fetchMock.mock.calls[0][1] as RequestInit).signal).toBeUndefined();
  });

  it("parses a problem body REGARDLESS of content type, unlike request", async () => {
    // `request` only parses a problem when the content type says so. `requestStream` tries the body
    // first, because a streaming endpoint that fails answers with JSON under whatever type its
    // error path chose. The looser check is intentional, so it is pinned.
    globalThis.fetch = vi
      .fn()
      .mockResolvedValue(
        new Response(
          JSON.stringify({ type: "urn:error:forbidden", title: "Forbidden", status: 403 }),
          { status: 403, headers: { "content-type": "application/json" } },
        ),
      );

    const { api } = await import("@/lib/api/client");
    await expect(api.stream("/generation/runs/1/events")).rejects.toMatchObject({
      problem: { type: "urn:error:forbidden", status: 403 },
    });
  });

  it("synthesises a problem when the error body is not JSON, preserving the status", async () => {
    globalThis.fetch = vi
      .fn()
      .mockResolvedValue(
        new Response("<html>gateway</html>", { status: 504, statusText: "Gateway Timeout" }),
      );

    const { api } = await import("@/lib/api/client");
    await expect(api.stream("/generation/runs/1/events")).rejects.toMatchObject({
      problem: { type: "urn:client:unexpected-error-shape", status: 504 },
    });
  });

  it("falls back to a title when the response carries no status text", async () => {
    globalThis.fetch = vi
      .fn()
      .mockResolvedValue(new Response("nope", { status: 500, statusText: "" }));

    const { api } = await import("@/lib/api/client");
    await expect(api.stream("/x")).rejects.toMatchObject({
      problem: { title: "Request failed", status: 500 },
    });
  });

  it("retries once after refreshing, like request does", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(new Response(null, { status: 401 }))
      .mockResolvedValueOnce(json({ access_token: "renewed-value", subject: "user-1" }))
      .mockResolvedValueOnce(
        new Response("event: token\ndata: {}\n\n", {
          status: 200,
          headers: { "content-type": "text/event-stream" },
        }),
      );
    globalThis.fetch = fetchMock;
    setSession("stale-value", { subject: "user-1", sessionId: null });

    const { api } = await import("@/lib/api/client");
    const res = await api.stream("/generation/runs/1/events");

    expect(res.ok).toBe(true);
    expect(fetchMock).toHaveBeenCalledTimes(3);
    const [name, value] = await expectedAuthorization("renewed-value");
    expect(headersOf(fetchMock, 2)[name]).toBe(value);
  });

  it("normalises a transport failure the same way request does", async () => {
    globalThis.fetch = vi.fn().mockRejectedValue(new TypeError("Failed to fetch"));

    const { api } = await import("@/lib/api/client");
    await expect(api.stream("/generation/runs/1/events")).rejects.toBeInstanceOf(ApiTransportError);
  });

  it("reports a non-Error rejection without claiming to know what it was", async () => {
    globalThis.fetch = vi.fn().mockRejectedValue("a bare string");

    const { api } = await import("@/lib/api/client");
    await expect(api.stream("/x")).rejects.toMatchObject({
      problem: { detail: "Unknown transport failure" },
    });
  });
});
