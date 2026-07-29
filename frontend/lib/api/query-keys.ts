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
} as const;
