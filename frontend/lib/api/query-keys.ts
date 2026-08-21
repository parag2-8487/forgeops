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
    // Added with `GET /api/v1/projects`. The limit is part of the key because two pages of
    // different sizes are different responses, and sharing a key would serve one from the other's
    // cache entry.
    list: (limit: number) => [...queryKeys.projects.all, "list", limit] as const,
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
  approvals: {
    all: ["approvals"] as const,
    // Added with the review UI. The status is part of the key because the list endpoint filters on
    // it server-side, so two filters are two different responses.
    list: (status: string) => [...queryKeys.approvals.all, "list", status] as const,
    detail: (id: string) => [...queryKeys.approvals.all, "detail", id] as const,
  },
  devices: {
    all: ["devices"] as const,
    // Added with `GET /api/v1/agents/devices`, the read surface pairing never had.
    list: () => [...queryKeys.devices.all, "list"] as const,
    detail: (id: string) => [...queryKeys.devices.all, "detail", id] as const,
  },
  secrets: {
    all: ["secrets"] as const,
    // Scoped to a project because `GET /api/v1/secrets` REQUIRES `project_id`; a key that ignored it
    // would serve one project's references from another's cache entry.
    list: (projectId: string) => [...queryKeys.secrets.all, "list", projectId] as const,
  },
} as const;
