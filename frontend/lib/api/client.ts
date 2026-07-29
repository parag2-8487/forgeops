import { env } from "@/lib/env";
import { ApiProblemError, ApiTransportError } from "./errors";
import { isProblemDetails, PROBLEM_CONTENT_TYPE } from "./problem";

const DEFAULT_TIMEOUT_MS = 30_000;

async function request<T>(
  path: string,
  init: RequestInit & { timeoutMs?: number } = {},
): Promise<T> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), init.timeoutMs ?? DEFAULT_TIMEOUT_MS);

  let res: Response;
  try {
    res = await fetch(`${env.NEXT_PUBLIC_API_BASE_URL}${path}`, {
      ...init,
      signal: controller.signal,
      headers: {
        Accept: "application/json",
        ...(init.body ? { "Content-Type": "application/json" } : {}),
        ...init.headers,
      },
    });
  } catch (cause) {
    // Network failure, DNS, abort. Normalise to a Problem so every consumer has
    // exactly one error type to handle.
    throw new ApiTransportError({
      type: "urn:client:transport-error",
      title: "Network request failed",
      status: 0,
      detail: cause instanceof Error ? cause.message : "Unknown transport failure",
      instance: path,
    });
  } finally {
    clearTimeout(timer);
  }

  if (res.status === 204) return undefined as T;

  const contentType = res.headers.get("content-type") ?? "";

  if (!res.ok) {
    if (contentType.includes(PROBLEM_CONTENT_TYPE)) {
      const body: unknown = await res.json().catch(() => null);
      if (isProblemDetails(body)) throw new ApiProblemError(body);
    }
    // Backend that did not honour the contract, or a proxy error page.
    // Preserve the REAL HTTP status.
    throw new ApiProblemError({
      type: "urn:client:unexpected-error-shape",
      title: res.statusText || "Request failed",
      status: res.status,
      instance: path,
    });
  }

  return (await res.json()) as T;
}

export const api = {
  get: <T>(p: string, i?: RequestInit & { timeoutMs?: number }) =>
    request<T>(p, { ...i, method: "GET" }),
  post: <T>(p: string, body?: unknown, i?: RequestInit & { timeoutMs?: number }) =>
    request<T>(p, {
      ...i,
      method: "POST",
      body: body === undefined ? undefined : JSON.stringify(body),
    }),
  put: <T>(p: string, body?: unknown, i?: RequestInit & { timeoutMs?: number }) =>
    request<T>(p, {
      ...i,
      method: "PUT",
      body: body === undefined ? undefined : JSON.stringify(body),
    }),
  delete: <T>(p: string, i?: RequestInit & { timeoutMs?: number }) =>
    request<T>(p, { ...i, method: "DELETE" }),
};
