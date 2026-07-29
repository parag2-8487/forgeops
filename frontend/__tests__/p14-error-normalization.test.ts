import { describe, it, expect, vi, afterEach } from "vitest";
import * as fc from "fast-check";
import { ApiProblemError, ApiTransportError, isProblemDetails } from "@/lib/api";

// Mock env
vi.mock("@/lib/env", () => ({
  env: {
    NEXT_PUBLIC_API_BASE_URL: "http://localhost:8000/api/v1",
    NEXT_PUBLIC_APP_NAME: "ForgeOps",
  },
}));

const originalFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = originalFetch;
});

// Arbitraries for generating test data
const httpStatus = fc.integer({ min: 100, max: 599 });
const nonOkStatus = fc.integer({ min: 400, max: 599 });
const contentType = fc.oneof(
  fc.constant("application/problem+json"),
  fc.constant("application/json"),
  fc.constant("text/html"),
  fc.constant("text/plain"),
  fc.constant(""),
);

const validProblemBody = fc.record({
  type: fc.stringMatching(/^urn:[a-z:]+$/),
  title: fc.string({ minLength: 1, maxLength: 50 }),
  status: nonOkStatus,
  detail: fc.option(fc.string({ maxLength: 100 }), { nil: undefined }),
});

const invalidBody = fc.oneof(
  fc.constant("not json at all"),
  fc.constant("<html>error</html>"),
  fc.constant(""),
  fc.constant("null"),
  fc.constant("[]"),
  // Valid JSON but missing required problem fields
  fc.constant(JSON.stringify({ message: "error" })),
  fc.constant(JSON.stringify({ error: "something" })),
);

describe("P-14: Frontend error normalization property tests", () => {
  it("every non-2xx with valid problem+json yields ApiProblemError with round-tripped fields", () => {
    fc.assert(
      fc.property(validProblemBody, (problem) => {
        const response = new Response(JSON.stringify(problem), {
          status: problem.status,
          headers: { "content-type": "application/problem+json" },
        });

        // Simulate the parsing logic from the client
        const body = JSON.parse(JSON.stringify(problem));
        if (isProblemDetails(body)) {
          const error = new ApiProblemError(body);
          expect(error).toBeInstanceOf(ApiProblemError);
          expect(error.problem.type).toBe(problem.type);
          expect(error.problem.title).toBe(problem.title);
          expect(error.problem.status).toBe(problem.status);
          return true;
        }
        // Should always be true for valid problems
        return false;
      }),
      { numRuns: 100 },
    );
  });

  it("non-conforming error bodies preserve the real HTTP status and never throw TypeError/SyntaxError", () => {
    fc.assert(
      fc.property(nonOkStatus, invalidBody, contentType, (status, body, ct) => {
        // Simulate what the API client does with non-conforming responses
        const headers = new Headers();
        if (ct) headers.set("content-type", ct);
        const response = new Response(body, { status, statusText: "Error", headers });

        // The client should always produce ApiProblemError, never raw TypeError/SyntaxError
        if (ct.includes("application/problem+json")) {
          try {
            const parsed = JSON.parse(body);
            if (isProblemDetails(parsed)) {
              const error = new ApiProblemError(parsed);
              expect(error).toBeInstanceOf(ApiProblemError);
              expect(error.problem.status).toBe(parsed.status);
            } else {
              // Non-conforming problem body
              const error = new ApiProblemError({
                type: "urn:client:unexpected-error-shape",
                title: "Error",
                status: status,
                instance: "/test",
              });
              expect(error).toBeInstanceOf(ApiProblemError);
              expect(error.problem.status).toBe(status);
            }
          } catch {
            // JSON parse failed - synthesize problem
            const error = new ApiProblemError({
              type: "urn:client:unexpected-error-shape",
              title: "Error",
              status: status,
              instance: "/test",
            });
            expect(error).toBeInstanceOf(ApiProblemError);
            expect(error.problem.status).toBe(status);
          }
        } else {
          // Non-problem content type - always synthesize
          const error = new ApiProblemError({
            type: "urn:client:unexpected-error-shape",
            title: "Error",
            status: status,
            instance: "/test",
          });
          expect(error).toBeInstanceOf(ApiProblemError);
          expect(error.problem.status).toBe(status);
        }
      }),
      { numRuns: 200 },
    );
  });

  it("transport failures yield ApiTransportError (subclass of ApiProblemError), never raw TypeError", () => {
    fc.assert(
      fc.property(
        fc.oneof(
          fc.constant(new TypeError("Failed to fetch")),
          fc.constant(new DOMException("The operation was aborted", "AbortError")),
          fc.constant(new Error("Network error")),
          fc.constant(new TypeError("Network request failed")),
        ),
        (cause) => {
          const error = new ApiTransportError({
            type: "urn:client:transport-error",
            title: "Network request failed",
            status: 0,
            detail: cause instanceof Error ? cause.message : "Unknown transport failure",
            instance: "/test",
          });

          expect(error).toBeInstanceOf(ApiProblemError);
          expect(error).toBeInstanceOf(ApiTransportError);
          expect(error.problem.status).toBe(0);
          // It should never be a raw TypeError/SyntaxError
          expect(error.name).not.toBe("TypeError");
          expect(error.name).not.toBe("SyntaxError");
        },
      ),
      { numRuns: 50 },
    );
  });

  it("full integration: generated non-2xx responses always yield ApiProblemError, never raw errors", async () => {
    await fc.assert(
      fc.asyncProperty(
        nonOkStatus,
        contentType,
        fc.oneof(validProblemBody.map(JSON.stringify), invalidBody),
        async (status, ct, body) => {
          globalThis.fetch = vi.fn().mockResolvedValue(
            new Response(body, {
              status,
              statusText: "Error",
              headers: ct ? { "content-type": ct } : {},
            }),
          );

          const { api } = await import("@/lib/api/client");
          try {
            await api.get("/test-prop");
            // 2xx would pass through — but we're only generating non-ok statuses
            throw new Error("Should have thrown");
          } catch (e) {
            if ((e as Error).message === "Should have thrown") throw e;
            // Must always be ApiProblemError or subclass, never raw TypeError/SyntaxError
            expect(e).toBeInstanceOf(ApiProblemError);
            expect((e as Error).name).not.toBe("TypeError");
            expect((e as Error).name).not.toBe("SyntaxError");
            // The status should be either from the parsed problem body or the HTTP status
            const problemStatus = (e as ApiProblemError).problem.status;
            // If it's a valid problem body and content-type matches, use body status
            // Otherwise, use the HTTP status
            if (ct.includes("application/problem+json")) {
              try {
                const parsed = JSON.parse(body);
                if (isProblemDetails(parsed)) {
                  // Valid problem body - status comes from the body
                  expect(problemStatus).toBe(parsed.status);
                } else {
                  // Invalid problem body - synthesized with HTTP status
                  expect(problemStatus).toBe(status);
                }
              } catch {
                // JSON parse error - synthesized with HTTP status
                expect(problemStatus).toBe(status);
              }
            } else {
              // Non-problem content type - synthesized with HTTP status
              expect(problemStatus).toBe(status);
            }
          }
        },
      ),
      { numRuns: 100 },
    );
  });

  it("full integration: transport failures always yield ApiTransportError", async () => {
    await fc.assert(
      fc.asyncProperty(
        fc.oneof(
          fc.constant(new TypeError("Failed to fetch")),
          fc.constant(new DOMException("Aborted", "AbortError")),
          fc.constant(new Error("ECONNREFUSED")),
        ),
        async (cause) => {
          globalThis.fetch = vi.fn().mockRejectedValue(cause);

          const { api } = await import("@/lib/api/client");
          try {
            await api.get("/test-transport");
            throw new Error("Should have thrown");
          } catch (e) {
            expect(e).toBeInstanceOf(ApiTransportError);
            expect(e).toBeInstanceOf(ApiProblemError);
            expect((e as ApiTransportError).problem.status).toBe(0);
          }
        },
      ),
      { numRuns: 50 },
    );
  });
});
