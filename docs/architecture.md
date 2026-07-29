# ForgeOps Architecture — Phase 0

Repository: `github.com/parag8487/ForgeOps`. This document describes what Phase 0
actually builds, what it deliberately leaves out, and the cross-cutting contracts every
later phase inherits. Authority: `.kiro/specs/phase-0-foundation/design.md` §1–§6.

## Scope boundary

Phase 0 is foundation and scaffolding. If a section of any document appears to describe
behaviour outside the table below, the table wins.

### In scope for Phase 0

| Deliverable | Content                                                                                                                                                                                     |
| :---------- | :------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 0.1         | Monorepo layout, root `Makefile`, `docker-compose.yml`, `.env.example`, `.gitignore`, pre-commit (Gitleaks + Ruff + gofmt + Prettier), the two-licence layout, `docs/`                      |
| 0.2         | Go module `github.com/parag8487/ForgeOps/agent`, thin `cmd/agent/main.go`, `internal/` tree, constructor-injection DI, graceful shutdown, GoReleaser + Cosign + Syft SBOM + SLSA provenance |
| 0.3         | FastAPI modular monolith: config, logging, async session, app factory, health/readiness, PostgreSQL 17 + pgvector, single Alembic revision, multi-stage Dockerfile                          |
| 0.4         | Next.js 16 App Router shell: sidebar + header + theme toggle, TanStack Query + Zustand, RFC 9457-aware API client, Vitest, Playwright, k6                                                   |
| 0.5         | Stateless MCP Gateway: header routing, OIDC token verification, OPA policy, TTL tool cache, W3C Trace Context, Go and Python MCP server templates, Tasks Extension, MCP Apps hosting        |
| 0.6         | Git and PR client in the agent (`go-git` locally, `go-github` for the PR REST API)                                                                                                          |
| 0.7         | Validation pipeline skeleton and the deterministic Semantic Plan Analyzer wired to the approval seam                                                                                        |
| 0.8         | OpenTofu 1.12.5 runner in the agent: timeout, output streaming, signal propagation, environment isolation, `validate` and `plan`                                                            |
| 0.9         | Six model routing tiers, endpoint registry, OpenAI-compatible adapter, fallback cascade, circuit breaker, BYO-key resolvers, tiered semantic cache, Redis/Lua per-caller limiter            |

### Explicitly out of scope

Feature logic (analysis, generation, deployment), UI beyond the shell, migrations beyond
`0001_initial`, the **general user authentication system** (see below), agent pairing /
mTLS / heartbeat / reconnect / command whitelist, job-queue infrastructure (seam only),
the OTel SDK and the observability stack, the safe default template library, tree-sitter
and cAST chunking (including the `tree-sitter/go-tree-sitter` dependency itself, decision
D-1), agent auto-update behaviour, RLS / PgBouncer / tenant middleware, the Governance
Control Plane, OPA-Wasm in the agent, and SPIFFE/SPIRE.

Structural directories mandated by the authoritative layout but unused in Phase 0 hold
only a non-code `README.md` or `.gitkeep`. They are not packages: no `doc.go`, no
importable `__init__.py`, no exported placeholder types.

## Authentication posture in Phase 0

Phase 0 verifies tokens at two surfaces only:

- the MCP Gateway routes under `/api/v1/mcp*`
- the costly completion seam `POST /api/v1/ai/complete`, where a verified `sub` keys the
  per-caller rate-limit bucket

**Phase 0 adds no general user authentication.** There is no login flow, no session or
refresh-token lifecycle, no user/team records, and no RBAC. Every other non-MCP route is
unauthenticated by deliberate decision. A resource server validating a bearer token is not
an authentication system; Phase 1 §1.11 issues tokens and applies authentication and RBAC
across all routes. `backend/src/auth/` is therefore a structural marker only.

Consequence for the deployment topology: see `docs/deployment.md`, which restricts Phase 0
to local development on a trusted machine.

## Tiers

```mermaid
graph TD
    subgraph T1["Tier 1 — Frontend (Next.js 16 + React 19)"]
        SHELL["App shell: sidebar + header + theme toggle"]
        APICL["RFC 9457-aware API client"]
        QSTATE["TanStack Query + Zustand"]
    end
    subgraph T2["Tier 2 — Backend (FastAPI modular monolith)"]
        API["REST /api/v1 + RFC 9457 problems"]
        HEALTH["/health, /health/ready, /api/v1/health"]
        MCPGW["MCP Gateway: routing, OIDC, OPA, TTL cache, trace, Tasks, Apps"]
        ROUTER["Model router: 6 tiers, cascade, breaker, limiter"]
        PLAN["Validation pipeline + Semantic Plan Analyzer"]
    end
    subgraph DATA["Data plane"]
        PG["PostgreSQL 17 + pgvector (HNSW, cosine)"]
        RD["Redis + Vector Search (L1/L2/L3 cache, Tasks store)"]
        OPA["OPA server (gateway policy)"]
    end
    subgraph T3["Tier 3 — Local agent (Go 1.26 single binary)"]
        MAIN["cmd/agent + internal/app composition"]
        GOMCP["Go MCP server template"]
        IAC["OpenTofu runner"]
        GIT["Git + PR client"]
        PROBE["doctor probes: docker, k8s"]
    end
    SHELL --> APICL --> API
    QSTATE --> APICL
    API --> HEALTH
    API --> MCPGW --> OPA
    MCPGW --> RD
    MCPGW --> GOMCP
    ROUTER --> RD
    API --> PG
    PLAN --> IAC
```

The agent ships as a binary, not as a default Compose service. The `agent-dev` container
exists only under the `tools` profile to carry OpenTofu for IaC integration tests.

## What runs after `docker compose up -d --wait`

Compose project name is `forgeops`. The unprofiled default set is exactly five services:
`postgres`, `redis`, `opa`, `backend`, `frontend`. Optional profiles are separate and are
verified only after their owning implementation exists: `vault` (Infisical) and `tools`
(`agent-dev` with OpenTofu). Every service loads the committed `.env.example` as required
and an optional `.env` as an override, so a fresh clone starts without any manual step.

## Health versus readiness

The distinction is a contract, not a convenience, and the probe endpoints are unversioned
so they do not move when the API version bumps.

| Endpoint             | Meaning                                                                  | Dependency I/O | Behaviour during a PostgreSQL or Redis outage                         |
| :------------------- | :----------------------------------------------------------------------- | :------------- | :-------------------------------------------------------------------- |
| `GET /health`        | Liveness: the event loop accepts work                                    | none           | stays `200`                                                           |
| `GET /health/ready`  | Readiness: PostgreSQL `SELECT 1` + Redis `PING`, each with a 2 s timeout | yes            | RFC 9457 `503` with one `errors[]` item per failed or timed-out check |
| `GET /api/v1/health` | Versioned informational echo of liveness                                 | none           | stays `200`                                                           |

Startup fails fast on invalid configuration and on failures to construct local resources.
It does **not** abort merely because PostgreSQL or Redis is unreachable; clients and pools
are built non-destructively and the Redis semantic-index creation retries idempotently
with bounded backoff. The Compose backend health check uses liveness only; readiness is
polled separately by `scripts/dev-up.sh`.

## Error contract

Every non-2xx backend response is an RFC 9457 problem document with
`Content-Type: application/problem+json`; `status` in the body always equals the HTTP
status, and `detail` never carries secrets, tokens, connection strings, or stack traces.
Full contract and the route inventory live in `docs/api.md`.

## Middleware order

Outermost first: `ServerErrorMiddleware` → `RequestIdMiddleware` →
`TraceContextMiddleware` → `AccessLogMiddleware` → `CORSMiddleware` → _(Phase 1
`TenantContextMiddleware` inserts here)_ → router and endpoint dependencies. Starlette
prepends middleware, so registration order in the app factory is the reverse of execution
order.

## Data model

Phase 0 has exactly one Alembic revision, `0001_initial`, creating three tables:
`projects`, `file_tree_entries`, and `embeddings`. The embedding column is fixed at 1536
dimensions with an HNSW cosine index (`m=16`, `ef_construction=64`) and a required
`model_id` provenance column (decision D-2). `hnsw.ef_search` is set per transaction, never
baked into the index. A nullable `tenant_id` seam exists with no RLS policies.

## Telemetry

Phase 0 propagates W3C Trace Context in Go and Python and ships a `Tracer` seam with only
a `NoopTracer` implementation. No OTel SDK, Collector, or exporter is introduced.

## Licence split

One repository, two licences, by path:

| Path                        | SPDX identifier | Covers                                                   |
| :-------------------------- | :-------------- | :------------------------------------------------------- |
| `LICENSE` (repository root) | `FSL-1.1-ALv2`  | everything except paths carrying their own `LICENSE`     |
| `agent/LICENSE`             | `Apache-2.0`    | the whole `agent/` subtree — the local agent and the CLI |

`FSL-1.1-ALv2` is the registered SPDX short identifier and the only form used in package
metadata and SBOMs. In prose the licence is the Functional Source License 1.1 with an
Apache 2.0 future licence: the backend and frontend are **source-available, converting to
Apache 2.0 after two years**, which is not the same as open source. The agent and CLI are
Apache-2.0 and carry a complete `agent/NOTICE`. Contributors: see `docs/development.md`,
since the licence a change lands under depends on the directory it touches.

## Recorded decisions carried by Phase 0

| Decision | Summary                                                                                                                                                      |
| :------- | :----------------------------------------------------------------------------------------------------------------------------------------------------------- |
| D-1      | `tree-sitter/go-tree-sitter` deferred to Phase 1; CGO conflicts with the `CGO_ENABLED=0` six-target static build. The real `fsnotify` watcher stays in scope |
| D-2      | Phase 0 vector column is 1536-d with an HNSW index and a `model_id` provenance column                                                                        |
| D-5      | `go-git/go-git/v5` for local Git operations, `google/go-github` for the PR REST API                                                                          |
| D-14     | Project identity ForgeOps; module path `github.com/parag8487/ForgeOps/agent`                                                                                 |
| D-19     | `FSL-1.1-ALv2` for the repository, `Apache-2.0` for the agent and CLI                                                                                        |

## Reference documents

`AI-Powered-DevOps-Platform-Complete-Technical-Research.md`, `PRD.md`,
`Tech-Stack-Analysis.md`, and `phases.md` live at the repository root and are read-only
inputs. They are excluded from every mutating hook and formatter glob, and are still
scanned by Gitleaks.
