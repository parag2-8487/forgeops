// SPDX-License-Identifier: Apache-2.0

const BACKEND_BASE_URL = process.env.NEXT_PUBLIC_BACKEND_URL || "http://localhost:8000";

export async function proxyFetch(path: string, options: RequestInit = {}): Promise<Response> {
  const url = `${BACKEND_BASE_URL}${path.startsWith("/") ? path : `/${path}`}`;
  const token = typeof window !== "undefined" ? localStorage.getItem("forgeops_auth_token") : null;

  const headers = new Headers(options.headers || {});
  if (token && !headers.has("Authorization")) {
    headers.set("Authorization", `Bearer ${token}`);
  }
  if (!headers.has("Content-Type") && options.body && typeof options.body === "string") {
    headers.set("Content-Type", "application/json");
  }

  return fetch(url, {
    ...options,
    headers,
  });
}
