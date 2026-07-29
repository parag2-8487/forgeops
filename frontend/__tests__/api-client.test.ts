import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { ApiProblemError, ApiTransportError, isProblemDetails } from "@/lib/api";

// Mock the env module
vi.mock("@/lib/env", () => ({
  env: {
    NEXT_PUBLIC_API_BASE_URL: "http://localhost:8000/api/v1",
    NEXT_PUBLIC_APP_NAME: "ForgeOps",
  },
}));

describe("isProblemDetails", () => {
  it("returns true for valid ProblemDetails", () => {
    expect(isProblemDetails({ type: "urn:test", title: "Error", status: 400 })).toBe(true);
  });

  it("returns true for ProblemDetails with optional fields", () => {
    expect(
      isProblemDetails({
        type: "urn:test",
        title: "Error",
        status: 400,
        detail: "Something",
        instance: "/path",
        trace_id: "abc",
        errors: [],
      }),
    ).toBe(true);
  });

  it("returns false for null", () => {
    expect(isProblemDetails(null)).toBe(false);
  });

  it("returns false for undefined", () => {
    expect(isProblemDetails(undefined)).toBe(false);
  });

  it("returns false for non-objects", () => {
    expect(isProblemDetails("string")).toBe(false);
    expect(isProblemDetails(123)).toBe(false);
  });

  it("returns false for missing type", () => {
    expect(isProblemDetails({ title: "Error", status: 400 })).toBe(false);
  });

  it("returns false for missing title", () => {
    expect(isProblemDetails({ type: "urn:test", status: 400 })).toBe(false);
  });

  it("returns false for missing status", () => {
    expect(isProblemDetails({ type: "urn:test", title: "Error" })).toBe(false);
  });
});

describe("ApiProblemError", () => {
  it("creates error with problem details", () => {
    const problem = { type: "urn:test", title: "Bad Request", status: 400 };
    const error = new ApiProblemError(problem);
    expect(error).toBeInstanceOf(Error);
    expect(error.name).toBe("ApiProblemError");
    expect(error.message).toBe("Bad Request (400)");
    expect(error.problem).toBe(problem);
  });

  it("fieldErrors maps pointer to detail, stripping #/ prefix", () => {
    const error = new ApiProblemError({
      type: "urn:test",
      title: "Validation",
      status: 422,
      errors: [
        { pointer: "#/name", detail: "Required" },
        { pointer: "#/nested/field", detail: "Invalid" },
      ],
    });
    expect(error.fieldErrors).toEqual({
      name: "Required",
      "nested/field": "Invalid",
    });
  });

  it("fieldErrors returns empty object when no errors", () => {
    const error = new ApiProblemError({
      type: "urn:test",
      title: "Error",
      status: 500,
    });
    expect(error.fieldErrors).toEqual({});
  });
});

describe("ApiTransportError", () => {
  it("is a subclass of ApiProblemError", () => {
    const error = new ApiTransportError({
      type: "urn:client:transport-error",
      title: "Network request failed",
      status: 0,
    });
    expect(error).toBeInstanceOf(ApiProblemError);
    expect(error).toBeInstanceOf(ApiTransportError);
    expect(error.name).toBe("ApiTransportError");
  });
});

describe("api client (request function)", () => {
  const originalFetch = globalThis.fetch;

  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    globalThis.fetch = originalFetch;
    vi.useRealTimers();
  });

  it("handles 204 No Content by returning undefined", async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(new Response(null, { status: 204 }));

    // Dynamic import to get the module with mocked env
    const { api } = await import("@/lib/api/client");
    const result = await api.get("/test");
    expect(result).toBeUndefined();
  });

  it("parses valid problem+json error responses", async () => {
    const problem = {
      type: "urn:error:not-found",
      title: "Not Found",
      status: 404,
      detail: "Resource not found",
    };
    globalThis.fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(problem), {
        status: 404,
        headers: { "content-type": "application/problem+json" },
      }),
    );

    const { api } = await import("@/lib/api/client");
    try {
      await api.get("/test");
      expect.fail("should have thrown");
    } catch (e) {
      expect(e).toBeInstanceOf(ApiProblemError);
      expect((e as ApiProblemError).problem.status).toBe(404);
      expect((e as ApiProblemError).problem.type).toBe("urn:error:not-found");
    }
  });

  it("synthesises problem for non-conforming error bodies preserving real HTTP status", async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(
      new Response("<html>Bad Gateway</html>", {
        status: 502,
        statusText: "Bad Gateway",
        headers: { "content-type": "text/html" },
      }),
    );

    const { api } = await import("@/lib/api/client");
    try {
      await api.get("/test");
    } catch (e) {
      expect(e).toBeInstanceOf(ApiProblemError);
      const problem = (e as ApiProblemError).problem;
      expect(problem.status).toBe(502);
      expect(problem.type).toBe("urn:client:unexpected-error-shape");
    }
  });

  it("throws ApiTransportError on network failure", async () => {
    globalThis.fetch = vi.fn().mockRejectedValue(new TypeError("Failed to fetch"));

    const { api } = await import("@/lib/api/client");
    try {
      await api.get("/test");
    } catch (e) {
      expect(e).toBeInstanceOf(ApiTransportError);
      expect(e).toBeInstanceOf(ApiProblemError);
      expect((e as ApiTransportError).problem.status).toBe(0);
      expect((e as ApiTransportError).problem.detail).toBe("Failed to fetch");
    }
  });

  it("builds URL from NEXT_PUBLIC_API_BASE_URL", async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );

    const { api } = await import("@/lib/api/client");
    await api.get("/health");
    expect(globalThis.fetch).toHaveBeenCalledWith(
      "http://localhost:8000/api/v1/health",
      expect.anything(),
    );
  });
});
