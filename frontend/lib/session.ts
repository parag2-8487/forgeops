// SPDX-License-Identifier: Apache-2.0

export interface Session {
  token: string | null;
  user: {
    id: string;
    username: string;
    role: string;
  } | null;
  isAuthenticated: boolean;
}

export function getSession(): Session {
  if (typeof window === "undefined") {
    return { token: null, user: null, isAuthenticated: false };
  }
  const token = localStorage.getItem("forgeops_auth_token");
  const userJson = localStorage.getItem("forgeops_user");
  const user = userJson ? JSON.parse(userJson) : null;

  return {
    token,
    user,
    isAuthenticated: Boolean(token),
  };
}

export function setSession(token: string, user: { id: string; username: string; role: string }): void {
  if (typeof window !== "undefined") {
    localStorage.setItem("forgeops_auth_token", token);
    localStorage.setItem("forgeops_user", JSON.stringify(user));
  }
}

export function clearSession(): void {
  if (typeof window !== "undefined") {
    localStorage.removeItem("forgeops_auth_token");
    localStorage.removeItem("forgeops_user");
  }
}
