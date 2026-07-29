import { describe, expect, it } from "vitest";
import { makeQueryClient } from "@/components/providers/query-provider";
import { ApiProblemError, ApiTransportError } from "@/lib/api";

describe("QueryProvider retry policy", () => {
  const client = makeQueryClient();
  const retryFn = client.getDefaultOptions().queries?.retry as (
    failureCount: number,
    error: Error,
  ) => boolean;

  it("never retries 4xx errors", () => {
    const error = new ApiProblemError({
      type: "urn:test",
      title: "Bad Request",
      status: 400,
    });
    expect(retryFn(0, error)).toBe(false);
    expect(retryFn(1, error)).toBe(false);
  });

  it("never retries 404 errors", () => {
    const error = new ApiProblemError({
      type: "urn:test",
      title: "Not Found",
      status: 404,
    });
    expect(retryFn(0, error)).toBe(false);
  });

  it("never retries 422 errors", () => {
    const error = new ApiProblemError({
      type: "urn:test",
      title: "Unprocessable",
      status: 422,
    });
    expect(retryFn(0, error)).toBe(false);
  });

  it("retries 5xx errors up to 2 times", () => {
    const error = new ApiProblemError({
      type: "urn:test",
      title: "Server Error",
      status: 500,
    });
    expect(retryFn(0, error)).toBe(true);
    expect(retryFn(1, error)).toBe(true);
    expect(retryFn(2, error)).toBe(false);
  });

  it("retries transport errors up to 2 times", () => {
    const error = new ApiTransportError({
      type: "urn:client:transport-error",
      title: "Network request failed",
      status: 0,
    });
    expect(retryFn(0, error)).toBe(true);
    expect(retryFn(1, error)).toBe(true);
    expect(retryFn(2, error)).toBe(false);
  });
});
