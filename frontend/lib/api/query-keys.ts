/**
 * Typed query-key factory for TanStack Query.
 * Centralised so Phase 1+ invalidation cannot drift into stringly-typed keys.
 */
export const queryKeys = {
  health: {
    all: ["health"] as const,
    status: () => [...queryKeys.health.all, "status"] as const,
    ready: () => [...queryKeys.health.all, "ready"] as const,
  },
  mcp: {
    all: ["mcp"] as const,
    servers: () => [...queryKeys.mcp.all, "servers"] as const,
    tools: (server: string) => [...queryKeys.mcp.all, "tools", server] as const,
  },
  ai: {
    all: ["ai"] as const,
    tiers: () => [...queryKeys.ai.all, "tiers"] as const,
  },
  projects: {
    all: ["projects"] as const,
    detail: (id: string) => [...queryKeys.projects.all, "detail", id] as const,
    activity: (id: string) => [...queryKeys.projects.all, "activity", id] as const,
    readiness: (id: string) => [...queryKeys.projects.all, "readiness", id] as const,
  },
  audit: {
    all: ["audit"] as const,
    events: (limit: number) => [...queryKeys.audit.all, "events", limit] as const,
  },
  policies: {
    all: ["policies"] as const,
    templates: () => [...queryKeys.policies.all, "templates"] as const,
  },
  secrets: {
    all: ["secrets"] as const,
    list: () => [...queryKeys.secrets.all, "list"] as const,
  },
} as const;
