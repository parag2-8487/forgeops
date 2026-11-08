# ForgeOps Frontend Web Application

Modern Next.js 16 (React 19) management dashboard and real-time deployment control plane for ForgeOps.

## Overview

The ForgeOps frontend provides a unified web interface for managing codebases, running multi-target autonomous deployments, scoring production readiness, evaluating governance policies, and tracking tamper-evident audit trails.

- **URL (local):** `http://localhost:13000` (mapped from container port `3000`)
- **Framework:** Next.js 16 (App Router) + React 19 + TypeScript 5
- **Styling:** Tailwind CSS + Radix UI + Lucide React
- **Data Management:** TanStack Query v5 (React Query) + Zustand
- **Architecture:** Zero-leak session memory (`lib/session.ts`), typed query keys (`lib/api/query-keys.ts`), and RFC 9457 structured problem details handling.

---

## Page Hierarchy & Routes

All primary application pages are organized under the `(shell)` route group:

| Route                                             | Title                    | Description                                                                                                                       |
| :------------------------------------------------ | :----------------------- | :-------------------------------------------------------------------------------------------------------------------------------- |
| `/`                                               | **ForgeOps Dashboard**   | Executive control plane, platform & subsystem health, multi-target strategies, G1–G8 gate pipeline, and observability boundaries. |
| `/onboarding`                                     | **Getting Started**      | 8-step ordered onboarding guide from cold start to governed change execution.                                                     |
| `/projects`                                       | **Projects Hub**         | Multi-tenant repository workspaces, branch checkouts, tags, favorites, and codebase indexing.                                     |
| `/projects/[projectId]`                           | **Project Detail**       | In-depth project view with activity timeline, codebase symbols, and environment configurations.                                   |
| `/projects/[projectId]/autonomous-deploy`         | **Autonomous Deploy**    | Interactive multi-target deployment launcher (Docker, GitHub, Vercel) with browser port safety.                                   |
| `/projects/[projectId]/autonomous-deploy/[runId]` | **Live Run Console**     | Real-time SSE log streamer, progressive G1–G7 gate status badges, and G8 remediation viewer.                                      |
| `/readiness`                                      | **Deployment Readiness** | Multi-dimensional scoring across containerization, CI/CD, orchestration, env config, security, and IaC.                           |
| `/approvals`                                      | **Approvals Center**     | Cryptographically governed review center for pending change sets and diffs.                                                       |
| `/policies`                                       | **Policies**             | Open Policy Agent (OPA) and Cerbos policy inspection and verification.                                                            |
| `/vault`                                          | **Secret Vault**         | Scoped secret references with zero-token browser leakage.                                                                         |
| `/pairing`                                        | **Agent Pairing**        | Outbound WebSocket agent pairing codes, certificates, and heartbeat verification.                                                 |
| `/analysis`                                       | **Plan Analysis**        | Static AST analysis, blast radius estimation, and proposed modification trees.                                                    |
| `/models`                                         | **Model Tiers**          | AI provider registry, model failover tiers, latency tracking, and circuit breakers.                                               |
| `/audit`                                          | **Audit Trail**          | Tamper-evident, hash-chained cryptographic audit stream for full compliance.                                                      |
| `/settings/integrations`                          | **Integrations**         | Personal GitHub PAT and Vercel credentials management with zero token disclosure.                                                 |

---

## Architectural Principles & Observability Gaps

Every screen in ForgeOps reads a real backend endpoint; none renders sample data. Where a fact is not knowable from the API, the screen says so rather than showing a plausible value:

1. **Policy bundle publishing**: There is no read route reporting a tenant's active bundle, so the onboarding path reports the activated digest rather than guessing bundle state.
2. **Agent attestation**: Pairing displays certificates and heartbeats, distinguishing "never reported" from "stale", without claiming hardware-rooted attestation in Phase 1.
3. **Readiness score**: The project list reports whether an index exists; a full readiness score is only computed when requested. A project never scanned reads "not scanned" rather than zero.
4. **Model endpoint availability**: Model tiers report the registry's last observation and circuit-breaker state without probing vendor endpoints on page load.
5. **Role-based controls**: Actions not permitted by Cerbos/OPA roles are withheld rather than displayed and 403'd.

---

## Testing & Quality Assurance

```bash
# Run unit and integration tests (Vitest)
pnpm test

# Run specific route tests
pnpm test route-pages.test.tsx

# Run TypeScript typecheck
pnpm tsc --noEmit

# Run code style formatting & lint
pnpm prettier --check .
pnpm eslint .

# Run End-to-End browser tests (Playwright)
pnpm test:e2e
```
