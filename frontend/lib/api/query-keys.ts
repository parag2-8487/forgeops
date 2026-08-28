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
    /**
     * The FILTERED list. Every filter is part of the key, for the same reason the limit is: a
     * search for "checkout" and a search for "billing" are different responses, and a key that
     * ignored the term would serve one from the other's cache entry — which looks exactly like a
     * search that does not work.
     */
    filtered: (filters: {
      limit: number;
      search: string;
      tags: readonly string[];
      favourite: boolean;
      archived: boolean;
    }) =>
      [
        ...queryKeys.projects.all,
        "filtered",
        filters.limit,
        filters.search,
        // Sorted, so `?tag=a&tag=b` and `?tag=b&tag=a` share one cache entry rather than fetching
        // the same page twice under two keys.
        [...filters.tags].sort().join(","),
        filters.favourite,
        filters.archived,
      ] as const,
    tags: () => [...queryKeys.projects.all, "tags"] as const,
    detail: (id: string) => [...queryKeys.projects.all, "detail", id] as const,
    activity: (id: string) => [...queryKeys.projects.all, "activity", id] as const,
    readiness: (id: string) => [...queryKeys.projects.all, "readiness", id] as const,
  },
  audit: {
    all: ["audit"] as const,
    events: (limit: number) => [...queryKeys.audit.all, "events", limit] as const,
    /**
     * `GET /api/v1/audit/verify`. `since_seq` is in the key because a verification from seq 0 and
     * one from seq 5000 are different claims about different ranges of the chain.
     */
    verify: (sinceSeq: number) => [...queryKeys.audit.all, "verify", sinceSeq] as const,
  },
  policies: {
    all: ["policies"] as const,
    templates: () => [...queryKeys.policies.all, "templates"] as const,
    /** `GET /api/v1/policies`, the list route that did not exist until the policy screen needed it. */
    list: (limit: number) => [...queryKeys.policies.all, "list", limit] as const,
    detail: (id: string) => [...queryKeys.policies.all, "detail", id] as const,
  },
  /**
   * The codebase index. Three routes that had no caller at all, which is why there was no way for a
   * user to see whether a project had ever been scanned.
   */
  codebase: {
    all: ["codebase"] as const,
    status: (projectId: string) => [...queryKeys.codebase.all, "status", projectId] as const,
    // The query is part of the key because the search runs server-side; a key that ignored it would
    // show one term's results for another's.
    symbols: (projectId: string, query: string) =>
      [...queryKeys.codebase.all, "symbols", projectId, query] as const,
    chunk: (projectId: string, chunkId: string) =>
      [...queryKeys.codebase.all, "chunk", projectId, chunkId] as const,
  },
  /** Secret references are per project; see `secrets.list` below for why that is load-bearing. */
  approvals: {
    all: ["approvals"] as const,
    // Added with the review UI. The status is part of the key because the list endpoint filters on
    // it server-side, so two filters are two different responses.
    list: (status: string) => [...queryKeys.approvals.all, "list", status] as const,
    /** Scoped by project as well, for the per-project change history timeline (§1.6). */
    history: (projectId: string) => [...queryKeys.approvals.all, "history", projectId] as const,
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
