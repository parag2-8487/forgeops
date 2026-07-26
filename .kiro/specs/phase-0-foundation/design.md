# Design Document: Phase 0 — Foundation & Project Scaffolding

**Spec:** `phase-0-foundation`
**Project:** **ForgeOps** — <https://github.com/parag8487/ForgeOps>
**Go module path:** `github.com/parag8487/ForgeOps/agent`
**Licensing:** root repository = **`FSL-1.1-ALv2`** (Functional Source License 1.1, Apache 2.0 future licence, 2-year conversion); `agent/` (agent + CLI) = **`Apache-2.0`** (§2.4)
**Scope:** Phase 0 only, per `phases.md` § "Phase 0: Foundation & Project Scaffolding"
**Design artifacts:** High-Level Design (architecture, components, data models) + Low-Level Design (code, algorithms, formal specifications)
**Reference documents (read-only, never modified by this spec):** `AI-Powered-DevOps-Platform-Complete-Technical-Research.md`, `PRD.md`, `Tech-Stack-Analysis.md`, `phases.md`
**Last revised:** 2026-07-28 — task-plan review correction pass; preserves D-1, D-2, D-5, D-14 and D-19 while closing all Phase 0 design defects identified by the review

---

## 0. Overview

Phase 0 builds the skeleton of **ForgeOps** — the AI-Powered DevOps Automation Platform described by the reference documents, a self-hostable system that acts as an AI DevOps engineer. Phase 0 produces **no product feature logic**. It produces a monorepo where three components (Go agent, FastAPI backend, Next.js frontend) build, test, lint, containerise, and release reproducibly; where the MCP Gateway, model routing, plan analysis, GitOps and OpenTofu execution *foundations* exist and are provably exercisable; and where every cross-cutting contract that later phases depend on (error shape, API versioning, protocol choice, logging, config, DI, telemetry seams, supply-chain custody) is fixed and enforced.

The design deliberately documents the **overall target architecture** at a high level so Phase 0's foundations are shaped correctly for Phases 1–5 — but everything outside Phase 0 is marked **Architectural Context Only** and must not be implemented, scaffolded, or stubbed now.

### 0.1 Authority order (strict)

Every decision in this document cites its authority. Where authorities conflict, the following order governs:

1. `AI-Powered-DevOps-Platform-Complete-Technical-Research.md` **§0 Corrections & Updates (24 July 2026)** — supersedes everything, including the rest of that file.
2. The remainder of the research document.
3. `Tech-Stack-Analysis.md` — every technology choice is validated against this.
4. `phases.md` and `PRD.md`.

Where all four are silent or ambiguous for Phase 0, the gap is recorded in **§17 Open Questions** rather than guessed.

### 0.2 Non-negotiable corrections honoured by this design

| Correction | Authority | Where honoured |
|:---|:---|:---|
| WebSocket = `github.com/coder/websocket`; `nhooyr.io/websocket` is **deprecated** | Research §0, Tech-Stack §2 | §10.5, §16 |
| Go **1.26** | Research §0 | §10.1, §16 |
| SQLModel on SQLAlchemy 2.0 with **`expire_on_commit=False`** | Research §0, PRD §5 | §11.3 |
| **6-tier** model routing; **GPT-5.6 Sol** primary flagship, Claude Fable 5 analysis flagship | Research §0 | §11.7 |
| FastAPI **native `EventSourceResponse`** — no `sse-starlette` dependency | Research §0 | §7.4, §16 |
| **No Celery.** ARQ/Dramatiq at P1; exactly **one** durable engine at P2 behind an orchestrator-agnostic interface — interface discipline starts Day 1 | Research §0, §B6 | §7.9 (seam only) |
| **pgvector HNSW** by default, tune `hnsw.ef_search` at query time | Research §0, §A0a | §6.3 |
| Agent identity = **SPIFFE/SPIRE X.509-SVID + mTLS with attestation**; no long-lived agent keys | Research §0, §H31 | §14.3 (Phase 1; Phase 0 must not block it) |
| **Cerbos is not embeddable** in a single Go binary; agent-side eval = **OPA compiled to Wasm** | Research §B7 | §5.4, §14.3 |
| **MinIO server repo is archived** — `minio-go` client is fine | Research §H31 | §16.5 (no object storage in Phase 0) |
| Multi-tenant isolation = **PostgreSQL RLS** with **PgBouncer transaction-mode** pooling in mind | Research §0 | §6.5 |
| Semantic cache doubles as a **resilience layer** with a staleness flag | Research §A0c | §11.8 |
| SWE-bench numbers are **self-reported and scaffolding-dependent** — rank on SWE-bench Pro + internal golden dataset | Research §0, §C8 | §11.7.1 |
| Reranking: over-retrieve 3× then `voyage-rerank-2` | Research §C10 | Architectural context only |

### 0.3 Project identity, module path and licensing (settled)

The reference documents carry placeholders for the project's identity. The real repository now exists, so those placeholders are resolved here and used verbatim throughout this document. Full rationale and provenance are in the decision log (§17.1 D-14, D-19); the conflict-resolution consequence is in §15.6.

| Item | Value | Supersedes |
|:---|:---|:---|
| Project name | **ForgeOps** | PRD §8's `ai-devops-platform/` root directory name (a placeholder) |
| Repository | `https://github.com/parag8487/ForgeOps` | — |
| Owner / org | `parag8487` | `phases.md` §0.2's literal `org` placeholder |
| Go module path | **`github.com/parag8487/ForgeOps/agent`** | `phases.md` §0.2's `github.com/org/ai-devops-agent` |
| Repository licence | **`FSL-1.1-ALv2`** — Functional Source License 1.1, Apache 2.0 future licence, 2-year conversion | NFR-32's unresolved "FSL **or** BSL 1.1" |
| Agent + CLI licence | **`Apache-2.0`** | NFR-31 (already unambiguous) |
| Compose project name | `forgeops` | — |
| Problem-type registry base URI | `https://errors.forgeops.dev` | — |

**Module path case escaping.** Uppercase letters in a Go module path are legal. The module proxy and checksum database case-escape them, so `github.com/parag8487/ForgeOps/agent` appears as `github.com/parag8487/!forge!ops/agent` inside `$GOMODCACHE` and in `sum.golang.org` lookups. This is cosmetic, works correctly, and is **not** a reason to rename the repository or lowercase the module path — the module path must match the real repository path exactly, or `go get` breaks.

**Workspace placement.** The monorepo is built **directly in the existing workspace root**, which already holds the four reference documents at its top level. There is no nested `ForgeOps/` or `ai-devops-platform/` subdirectory — `README.md` and `PROGRESS.md` sit alongside `PRD.md`, `phases.md`, `Tech-Stack-Analysis.md` and `AI-Powered-DevOps-Platform-Complete-Technical-Research.md`.

Consequences for tooling, all in one place so nothing rewrites an input document:

| Tool | Required treatment of the four reference `.md` files |
|:---|:---|
| `.gitignore` | **Must not ignore them.** They are tracked inputs and belong in the repository. `.gitignore` covers build output, `.env`, caches — never these |
| `pre-commit` (`prettier`, `end-of-file-fixer`, `trailing-whitespace`) | Excluded via a top-level `exclude:` regex (§8.4), because these hooks *rewrite* files |
| `pre-commit` (`gitleaks`) | **Not** excluded — it only reads, and a secret in a reference document is still a secret |
| Ruff, golangci-lint | Never reach them (`.md` is outside both tools' file types), but the `backend/`-scoped Ruff `include` and the `agent/`-scoped golangci-lint invocation make that structural rather than accidental |
| Markdown link/lint tooling, if ever added | Must inherit the same four-file exclusion |
| CI `paths-filter` | The four files match no component filter, so editing them triggers no build (§8.3) |

Those four files are read-only inputs to this spec: no hook, formatter, or task may lint, reformat, or rewrite them.

---

## 1. Scope Boundary — In Scope Now vs Architectural Context Only

This is the controlling section of the document. If any later section appears to describe work outside the left column, the left column wins.

### 1.1 In scope for Phase 0

| Group | Deliverable | Authority |
|:---|:---|:---|
| **0.1** | Monorepo layout per PRD §8 in the ForgeOps repository root; `agent/`, `backend/`, `frontend/`, `docs/`, `.github/`; root `Makefile`, `docker-compose.yml`, `.env.example`, `.gitignore`; pre-commit framework with Gitleaks + Ruff + gofmt; two-licence layout — root `LICENSE` = FSL-1.1-ALv2 and `agent/LICENSE` = Apache-2.0, with SPDX identifiers in package metadata (§2.4) | phases.md 0.1, PRD §8, NFR-31/32, §17.1 D-19 |
| **0.2** | Go module `github.com/parag8487/ForgeOps/agent`; `cmd/agent/main.go` thin entry; `internal/` package tree; constructor-injection DI; graceful shutdown via `signal.NotifyContext` + `errgroup`; core deps pinned and exercised (tree-sitter excluded — §17.1 D-1); `golangci-lint`; GitHub Actions CI; GoReleaser + Cosign keyless + Syft CycloneDX SBOM + SLSA provenance | phases.md 0.2, §17.1 D-1/D-14 |
| **0.3** | FastAPI domain-driven modular monolith; `src/core/` config + logging + async session (`expire_on_commit=False`); `src/main.py` with health check, lifespan, middleware stack; PostgreSQL 17 + pgvector in compose; Alembic incl. pgvector column detection; pytest + pytest-asyncio + httpx async integration tests (>70% goal); multi-stage Dockerfile; `ruff`; `pip-audit` in CI | phases.md 0.3 |
| **0.4** | Next.js 16 App Router; shadcn/ui base theme; shell layout (sidebar + header + theme toggle); TanStack Query + Zustand; RFC 9457-aware API client wrapper; vitest + Testing Library; Playwright; k6; React Hook Form + Zod standard; pnpm; ESLint + Prettier | phases.md 0.4 |
| **0.5** | Stateless MCP Gateway: `Mcp-Method` + `Mcp-Name` header routing; OAuth 2.1/OIDC token validation with `iss` checking; OPA policy enforcement at the gateway; TTL caching honouring `ttlMs`; W3C Trace Context propagation; base MCP server template in Go (`mark3labs/mcp-go`) and in Python/FastAPI; Tasks Extension lifecycle; MCP Apps sandboxed-iframe support | phases.md 0.5, PRD §2.1a |
| **0.6** | Git client library in the Go agent — `go-git/go-git/v5` for local operations, `google/go-github` for the PR REST API (§17.1 D-5); PR creation flow (branch → commit → push → PR); PR review status polling | phases.md 0.6, §17.1 D-5 |
| **0.7** | Validation pipeline skeleton; **Semantic Plan Analyzer** module (destructive-action detection + blast-radius computation); connection of the pipeline to the approval workflow seam | phases.md 0.7, Research §5 #8, §5.1 P0 #3 |
| **0.8** | OpenTofu 1.12.5 in the Docker dev environment; OpenTofu runner module in the Go agent with context timeout, output streaming, signal propagation, environment isolation; `tofu validate` and `tofu plan` executed programmatically | phases.md 0.8, PRD §2.1 |
| **0.9** | 6 model routing tiers; validated endpoint registry; production `httpx` OpenAI-compatible adapter for hosted/self-hosted endpoints; explicit unavailable native protocols; fallback cascade; per-endpoint circuit breaker (5 failures / 30 s → OPEN → HALF-OPEN after 60 s); BYO-Key architecture with Infisical; tiered semantic cache; Redis/Lua per-caller limiter on `/api/v1/ai/complete` | phases.md 0.9, Research §0, §A0c |
| **Progress** | Root `PROGRESS.md` durable progress record | User requirement (§18) |

### 1.2 Explicitly excluded from Phase 0

`phases.md` Phase 0 "Excluded" list, expanded:

| Excluded | Notes |
|:---|:---|
| Any feature logic — analysis, generation, deployment | No readiness scoring, no artifact generation, no deploys |
| UI beyond the shell layout | No feature pages, no dashboards, no diff viewer |
| Database migrations beyond the initial schema | Exactly one Alembic revision (§6.2) |
| Authentication (the user authentication *system*) | See §15.2 for the precise resolution against 0.5's OIDC requirement |
| Agent pairing, mTLS handshake, heartbeat, reconnect/backoff, command whitelist, approval verification | Phase 1 §1.1 |
| Job queue infrastructure (ARQ/Dramatiq/Inngest/Temporal) | Seam only (§7.9) |
| OTel SDK, Collector, Prometheus, Loki, Tempo, Grafana | Phase 3; Phase 0 ships propagation + interface seams only (§7.8) |
| Safe Default Template Library (8 languages × 5 artifacts) | Phase 1 §1.5; Phase 0 provides the terminal cascade slot only (§11.7.3) |
| Tree-sitter / cAST semantic chunking **and the `tree-sitter/go-tree-sitter` dependency itself** | Phase 1 §1.3, per decision **D-1** (§17.1): CGO conflicts with the `CGO_ENABLED=0` six-target static build. `internal/scanner` is still created in Phase 0 with its interfaces and a real `fsnotify` watcher — a seam, not a stub |
| Agent auto-update behaviour | Phase 1+; the `minio/selfupdate` dependency is pinned and verified, not wired |
| RLS policies, PgBouncer, tenant middleware | Phase 1; Phase 0 leaves the column and transaction-scoped seam (§6.5) |
| Governance Control Plane, OPA-Wasm in agent, SPIFFE/SPIRE | Phase 1 §1.10 |

### 1.3 Structural artifacts, seams and stubs

Phase 0 distinguishes three things that must never be conflated:

| Kind | Phase 0 rule | Example |
|:---|:---|:---|
| **Structural artifact required by an authoritative layout** | May be tracked with `.gitkeep` or a non-code `README.md` that names the owning future phase. It is **not** an interface and must contain no importable/exported placeholder types or behaviour. | `internal/executor/`, deferred backend domain directories, `frontend/features/` |
| **Seam** | An interface plus at least one implementation genuinely useful in Phase 0 (a real transport, pure function, adapter, dev-mode implementation, or `doctor` probe). | `Transport` + `WSSTransport`; `ModelEndpoint` + `OpenAICompatibleEndpoint` |
| **Stub** | Placeholder code whose only purpose is future replacement. It is forbidden. | package-doc-only modules, exported future `Operation`/`Finding`/`Decision` types, fake provider adapters |

Go directories explicitly mandated by `phases.md` 0.2 but unused in Phase 0 (`executor`, `validator`, `policy`, `devtools`) contain only a non-code `README.md` (or `.gitkeep`) explaining the future owning phase. They contain no `doc.go`, `.go` files, exported types, or package behaviour. Deferred backend domains required by the PRD layout are tracked structurally without `__init__.py`, package docstrings, or other importable placeholder modules. `frontend/features/` is structural only and contains no feature placeholder.

Task-generation consequence: the repository-skeleton task creates those tracking files only and must not create a "package-doc/types" implementation task or tests for non-existent packages. A later owning phase replaces the structural marker with real code and a Phase-useful implementation. Every dependency listed in `phases.md` §0.2 — **with the single, reasoned exception of `tree-sitter/go-tree-sitter`, deferred to Phase 1 by decision D-1 (§17.1)** — is pinned in `go.mod` **and** exercised by Phase 0 behaviour, a test, or an `agent doctor` probe (§10.9). D-1 defers a dependency, not a Phase 0 capability: the real `fsnotify` watcher remains in scope, while no Phase 0 code pretends to parse an AST.

---

## 2. Architecture

### 2.1 Target system architecture — ForgeOps (scope-annotated)

Three tiers plus the MCP Gateway, per PRD §2.1 and Tech-Stack "Recommended Architecture Diagram". Solid boxes are built in Phase 0; dashed boxes are **Architectural Context Only**.

```mermaid
graph TD
    subgraph T1["TIER 1 — ForgeOps Frontend: Next.js 16 + React 19"]
        SHELL["App shell: sidebar + header + theme toggle<br/>IN SCOPE"]
        APICL["API client wrapper<br/>RFC 9457 aware<br/>IN SCOPE"]
        QSTATE["TanStack Query + Zustand<br/>IN SCOPE"]
        FEAT["Feature surfaces: projects, analysis,<br/>approval, deployment, monitoring<br/>PHASE 1 to 4"]
    end

    subgraph T2["TIER 2 — ForgeOps Backend: FastAPI 0.139.2 modular monolith"]
        API["REST API /api/v1 + RFC 9457 errors<br/>IN SCOPE"]
        HEALTH["Health and readiness endpoints<br/>IN SCOPE"]
        MCPGW["MCP Gateway: stateless<br/>header routing, OIDC, OPA, TTL cache,<br/>W3C trace, Tasks, Apps<br/>IN SCOPE"]
        ROUTER["Model Router: 6 tiers, real endpoint registry,<br/>OpenAI-compatible adapter, cascade,<br/>circuit breaker + per-caller limiter<br/>IN SCOPE"]
        PLAN["Validation pipeline +<br/>Semantic Plan Analyzer<br/>IN SCOPE"]
        PYMCP["Python MCP server template<br/>IN SCOPE"]
        WSHUB["WebSocket hub: JSON-RPC 2.0 over WSS<br/>PHASE 1"]
        AIENG["AI engine: LangGraph + LlamaIndex, RAG<br/>PHASE 1"]
        GOV["Governance Control Plane<br/>PHASE 1"]
        QUEUE["Task dispatcher: ARQ/Dramatiq P1,<br/>one durable engine P2<br/>SEAM ONLY IN PHASE 0"]
    end

    subgraph DATA["Data plane"]
        PG["PostgreSQL 17 + pgvector 0.8.5<br/>HNSW index<br/>IN SCOPE"]
        REDIS["Redis + Vector Search<br/>L1/L2/L3 cache, Tasks store<br/>IN SCOPE"]
        OPA["OPA server: gateway policy<br/>IN SCOPE"]
        VAULT["Infisical: BYO LLM keys<br/>IN SCOPE, optional profile"]
        PROVIDERS["OpenAI-compatible HTTP endpoints<br/>hosted or self-hosted<br/>IN SCOPE via real adapter"]
    end

    subgraph T3["TIER 3 — ForgeOps Local Agent: Go 1.26 single binary<br/>module github.com/parag8487/ForgeOps/agent"]
        MAIN["cmd/agent/main.go<br/>constructor DI + graceful shutdown<br/>IN SCOPE"]
        TRANSPORT["WSS transport: coder/websocket<br/>transport mechanics only<br/>IN SCOPE"]
        GOMCP["Go MCP server template<br/>mark3labs/mcp-go<br/>IN SCOPE"]
        IAC["OpenTofu runner: timeout, streaming,<br/>signals, env isolation<br/>IN SCOPE"]
        GIT["Git and PR client<br/>IN SCOPE"]
        PROBE["doctor probes: docker, k8s, fileops<br/>IN SCOPE"]
        EXEC["Command executor, whitelist,<br/>OPA Wasm, approval verify<br/>PHASE 1"]
        SCAN["Codebase scanner: tree-sitter AST + cAST<br/>PHASE 1 — dependency deferred by D-1"]
    end

    SHELL --> APICL --> API
    QSTATE --> APICL
    API --> HEALTH
    API --> MCPGW
    MCPGW --> OPA
    MCPGW --> REDIS
    MCPGW --> PYMCP
    MCPGW --> GOMCP
    ROUTER --> REDIS
    ROUTER --> VAULT
    ROUTER --> PROVIDERS
    PLAN --> IAC
    API --> PG
    MAIN --> TRANSPORT
    MAIN --> GOMCP
    MAIN --> IAC
    MAIN --> GIT
    MAIN --> PROBE
    TRANSPORT -.->|"outbound-only WSS, Phase 1 protocol"| WSHUB

    style FEAT stroke-dasharray: 6 4
    style WSHUB stroke-dasharray: 6 4
    style AIENG stroke-dasharray: 6 4
    style GOV stroke-dasharray: 6 4
    style QUEUE stroke-dasharray: 6 4
    style EXEC stroke-dasharray: 6 4
    style SCAN stroke-dasharray: 6 4
```

### 2.2 What actually runs after `docker-compose up` in Phase 0

Compose project name is `forgeops`, so every container, network and volume is namespaced `forgeops-*` / `forgeops_*` (§13.3).

```mermaid
graph LR
    DEV["Developer"] -->|"localhost:3000"| FE["frontend<br/>Next.js dev or prod build"]
    DEV -->|"localhost:8000"| BE["backend<br/>FastAPI + uvicorn"]
    FE -->|"/api/v1"| BE
    BE -->|"asyncpg"| PG["postgres<br/>pgvector/pgvector:pg17"]
    BE -->|"RESP + FT.SEARCH"| RD["redis<br/>redis-stack-server"]
    BE -->|"HTTP /v1/data"| OPA["opa<br/>run --server"]
    BE -.->|"profile vault"| INF["infisical"]
    AG["agent-dev<br/>profile tools<br/>OpenTofu 1.12.5"] -.->|"local exec"| TOFU["tofu validate / plan"]
```

`make up` runs `make init-env` and then starts exactly the **default profile**: `postgres`, `redis`, `opa`, `backend`, and `frontend`. The completion command is `docker compose up -d --wait`; criterion 4 means those five services and no optional profile. Direct unprofiled `docker compose up -d --wait` also works on a fresh clone because every service loads the committed `.env.example` baseline and treats `.env` as an optional override (§13.3). Health gate: `GET /health` returns 200 whenever the process event loop is alive; `GET /health/ready` returns 200 only when Postgres and Redis both answer.

Optional services are verified separately **after their owning implementation exists**: `docker compose --profile vault up -d --wait` for `infisical`, and `docker compose --profile tools up -d --wait` for `agent-dev`. Neither is implied by the unprofiled criterion, and neither service/profile may be declared in Compose before the task that supplies its image/configuration. The agent remains a **binary, not a default-profile service**; `agent-dev` is a tools-profile container carrying OpenTofu for IaC integration tests.

### 2.3 Monorepo structure mapped to Phase 0 deliverables

The repository is **ForgeOps** (`github.com/parag8487/ForgeOps`) and its *internal* layout follows PRD §8 exactly — PRD §8's `ai-devops-platform/` root directory name is a placeholder superseded by the real repository name (§0.3, §15.6). Additions beyond PRD §8 are marked `[+]` with a justification; nothing in PRD §8 is removed.

The tree below **is** the repository root — there is no nested project directory. The four reference documents already live at this level and stay there, read-only.

```
ForgeOps/                           # repository root == workspace root
├── AI-Powered-DevOps-Platform-Complete-Technical-Research.md   # reference, READ-ONLY
├── PRD.md                          # reference, READ-ONLY
├── Tech-Stack-Analysis.md          # reference, READ-ONLY
├── phases.md                       # reference, READ-ONLY
│                                   #   ^ all four excluded from every lint/format glob (§0.3, §8.4)
├── Makefile                        # 0.1 — build/test/lint/clean + init-env/up/down/migrate/sbom/lock
├── docker-compose.yml              # 0.1 — default profile + optional profiles, project `forgeops`
├── .env.example                    # 0.1 — committed baseline loaded by Compose (§13.1)
├── .env                            # local optional override, git-ignored; init-env never overwrites
├── .gitignore                      # 0.1
├── .pre-commit-config.yaml         # 0.1 — [+] gitleaks, ruff, gofmt, prettier, hygiene
├── LICENSE                         # 0.1 — FSL-1.1-ALv2, covers the repository (NFR-32, D-19)
├── PROGRESS.md                     # [+] durable progress record (§18)
├── README.md                       # 0.1 — states the two-licence split (§2.4)
├── scripts/                        # [+] POSIX shell helpers invoked by the Makefile (§13.4)
│   ├── init-env.sh                 # idempotent: copy baseline only when .env is absent
│   └── dev-up.sh                   # added only after backend + frontend compose services exist
│
├── agent/                          # TIER 3 — Go 1.26, module github.com/parag8487/ForgeOps/agent
│   ├── LICENSE                     # Apache-2.0 — governs agent + CLI, overrides root (NFR-31, D-19)
│   ├── NOTICE                      # [+] complete Apache project notice; no TODO/stub (§2.4)
│   ├── go.mod / go.sum             # 0.2 — module github.com/parag8487/ForgeOps/agent
│   ├── .golangci.yml               # 0.2
│   ├── .goreleaser.yaml            # 0.2
│   ├── Dockerfile                  # 0.8 — devtools target added with OpenTofu runner work
│   ├── cmd/agent/main.go           # 0.2 — final thin entry point after internal/app composition
│   ├── internal/
│   │   ├── config/                 # [+] typed env config, validated (§10.3)
│   │   ├── logging/                # [+] independent zap construction + redaction primitive
│   │   ├── app/                    # [+] FINAL composition stage: Run/Close, CLI wiring (§10.4)
│   │   ├── connection/             # 0.2 — Transport iface + coder/websocket impl
│   │   ├── docker/                 # 0.2 — Ping probe only
│   │   ├── k8s/                    # 0.2 — context/version probe only
│   │   ├── scanner/                # 0.2 — Watcher iface + fsnotify impl; no AST parsing (D-1)
│   │   ├── executor/README.md      # 0.2 — structural only; no Go package/types (future phase)
│   │   ├── validator/README.md     # 0.2 — structural only; no Go package/types (future phase)
│   │   ├── policy/README.md        # 0.2 — structural only; no Go package/types (future phase)
│   │   ├── fileops/                # 0.2 — atomic write + backup + unified diff
│   │   ├── iac/                    # 0.8 — OpenTofu runner (§10.6)
│   │   ├── git/                    # 0.6 — [+] go-git + go-github, per D-5 (§10.7)
│   │   ├── devtools/README.md      # 0.2 — structural only; no Go package/types (future phase)
│   │   ├── telemetry/              # 0.2 — TraceContext + Tracer seam (§7.8)
│   │   └── mcp/                    # 0.5 — Go MCP server template (§10.8)
│   ├── pkg/                        # PRD §8 — structural `.gitkeep`, no placeholder package
│   ├── testdata/                   # [+] tofu plan JSON fixture, signed-update fixture
│   └── testfixtures/tofu-null/     # [+] exact null-provider fixture + .terraform.lock.hcl
│
├── backend/                        # TIER 2 — FastAPI 0.139.2
│   ├── pyproject.toml              # [+] PEP 621 source of truth + exact direct pins (§16.2)
│   ├── requirements.lock           # [+] runtime, transitive hashes; Docker installs this only
│   ├── requirements-dev.lock       # [+] runtime + test/lint, transitive hashes; CI installs this
│   ├── Dockerfile                  # 0.3 — multi-stage, runtime lock + --require-hashes
│   ├── alembic.ini
│   ├── alembic/versions/0001_initial.py   # 0.3 — the ONLY migration (§6.2)
│   ├── src/
│   │   ├── main.py                 # 0.3 — app factory, non-destructive lifespan, middleware
│   │   ├── core/                   # 0.3 — config, logging, db, errors, trace, tasks seam
│   │   ├── auth/README.md          # PRD §8 — structural only; no importable package (§15.2)
│   │   ├── projects/               # PRD §8 — SQLModel model only (§6.2)
│   │   ├── analysis/
│   │   │   ├── models.py           # 0.3 — file_tree + embeddings SQLModel
│   │   │   └── plan_analyzer/      # 0.7 — pipeline + semantic analyzer (§11.9)
│   │   ├── generation/README.md    # structural only; owning future phase
│   │   ├── deployment/README.md    # structural only; owning future phase
│   │   ├── monitoring/README.md    # structural only; owning future phase
│   │   ├── incidents/README.md     # structural only; owning future phase
│   │   ├── policies/README.md      # structural only; owning future phase
│   │   ├── secrets/README.md       # structural only; owning future phase
│   │   ├── notifications/README.md # structural only; owning future phase
│   │   ├── websocket/README.md     # structural only; owning future phase
│   │   ├── ai/
│   │   │   ├── routing/            # 0.9 — registry, adapter, router, cascade, breaker (§11.7)
│   │   │   ├── rate_limit/         # 0.9 — Redis atomic token bucket (§11.7.5)
│   │   │   ├── cache/              # 0.9 — L1/L2/L3 semantic cache (§11.8)
│   │   │   └── keys/               # 0.9 — BYO-Key resolvers (§11.7.4)
│   │   ├── mcp/                    # 0.5 — [+] Gateway + server template (§11.4, §11.10)
│   └── tests/                      # 0.3 — unit + async integration + property tests
│
├── frontend/                       # TIER 1 — Next.js 16
│   ├── package.json / pnpm-lock.yaml
│   ├── app/                        # 0.4 — App Router (§12.1)
│   ├── components/{ui,layout,providers}/
│   ├── features/README.md          # PRD §8 — structural marker only; no feature placeholder
│   ├── lib/{api,env}/              # 0.4 — client wrapper, problem parsing
│   ├── hooks/  stores/
│   ├── e2e/                        # 0.4 — Playwright
│   └── load/                       # 0.4 — k6
│
├── policies/                       # [+] Rego for the gateway OPA server (§11.4)
├── docs/                           # 0.1 — architecture.md, api.md, development.md, deployment.md
└── .github/workflows/{ci.yml,release.yml}   # 0.2
```

**PRD §8 deviations, all additive and justified:** `internal/config`, `internal/app`, `internal/git` (required by 0.6), `internal/telemetry` is in PRD's list already; `backend/src/mcp/` (0.5 is a first-class Phase 0 deliverable and PRD §2.1a places the Gateway in Tier 2 but PRD §8 omits a module for it); `policies/`, `scripts/`, `pyproject.toml` plus the two generated lockfiles instead of an unlocked `requirements.txt` (§7.7), the two-licence file layout plus the complete `agent/NOTICE` (NFR-31/32, §2.4), `PROGRESS.md`, `.pre-commit-config.yaml`. Required but deferred directories are structural artifacts under §1.3, not packages or seams. The root directory *name* differs from PRD §8 because PRD §8's name was a placeholder (§0.3, §15.6) — that is a resolution, not a deviation.

### 2.4 Licensing layout — one repository, two licences

NFR-31 licenses the agent and CLI under Apache 2.0; NFR-32 licenses the backend platform under "FSL (Fair Source) **or** BSL 1.1" without choosing. Decision **D-19** (§17.1) selects the FSL variant whose future licence is Apache 2.0, with the two-year conversion.

**Exact naming matters here**, because two different names circulate for the same document:

| Form | Use it for |
|:---|:---|
| **`FSL-1.1-ALv2`** | The **registered SPDX short identifier**. This is what goes in `pyproject.toml`, `package.json`, SPDX headers, and anything a tool parses. See the [SPDX entry for Functional Source License, Version 1.1, ALv2 Future License](https://spdx.github.io/license-list-data/FSL-1.1-ALv2.html) |
| "Functional Source License 1.1, Apache 2.0 future licence" | Prose and `README.md`. FSL's own name is the **Functional** Source License; it is *a* Fair Source licence, which is a category, not the licence's name ([fsl.software](https://fsl.software/)) |
| `FSL-1.1-Apache-2.0` | **Do not use in metadata.** It is a descriptive alias, not a registered SPDX identifier — SPDX-consuming tooling (including Syft) will fail validation or report `UNKNOWN`. It appears here only so a reader who encounters it knows which licence is meant |

*(Licence-list facts summarised from the SPDX license list and fsl.software; content was rephrased for compliance with licensing restrictions.)*

Because one monorepo now carries two licences, the placement has to be unambiguous rather than implied.

| Path | Licence | Applies to |
|:---|:---|:---|
| `LICENSE` (repository root) | **FSL-1.1-ALv2** | Everything in the repository **except** paths that carry their own `LICENSE` |
| `agent/LICENSE` | **Apache-2.0** | The entire `agent/` subtree — the local agent and the CLI (NFR-31). A directory-level `LICENSE` overrides the root by the ordinary convention that the nearest licence governs |
| `agent/NOTICE` | Complete Apache project notice | Contains the project name (`ForgeOps Agent`), copyright owner/year, the standard statement that the subtree includes software developed by the ForgeOps project and is licensed under Apache-2.0, plus only those upstream NOTICE texts whose licences require reproduction. It contains no TODO, "stub", or prospective attribution placeholder |
| `backend/`, `frontend/`, `policies/`, `scripts/`, `docs/`, root tooling | FSL-1.1-ALv2 via the root `LICENSE` | No per-directory file needed; the root already covers them |

Because "the nearest `LICENSE` governs" is a convention rather than a rule, the split is stated **explicitly** in three machine- and human-readable places so no reader has to infer it:

1. **`README.md`** carries a `## Licence` section that names both licences, states which paths each covers, and gives the FSL change date semantics in one sentence.
2. **SPDX identifiers in package metadata**, so tooling and SBOM generators report the correct licence per component:

```toml
# backend/pyproject.toml  — FSL applies here
[project]
name = "forgeops-backend"
license = "FSL-1.1-ALv2"          # SPDX expression (PEP 639)
```

```jsonc
// frontend/package.json — FSL applies here
{ "name": "forgeops-frontend", "license": "FSL-1.1-ALv2", "private": true }
```

```go
// agent/**/*.go — every Go file header, Apache applies here
// SPDX-License-Identifier: Apache-2.0
```

3. **`docs/development.md`** repeats the split for contributors, since the licence a contribution lands under depends on which directory it touches.

`agent/NOTICE` is a release-ready licensing artifact, not a dependency list. Its exact required base content is:

```text
ForgeOps Agent
Copyright 2026 parag8487

This product includes software developed by the ForgeOps project.
The ForgeOps Agent and CLI are licensed under the Apache License, Version 2.0.
See the adjacent LICENSE file for the complete license terms.
```

Before release, direct and transitive dependencies linked into the agent are audited for upstream NOTICE-reproduction obligations; required notice text is appended verbatim beneath an `Upstream notices` heading with its source identified. If none require reproduction, release evidence records **"no upstream NOTICE reproduction required"** and no empty heading or placeholder is added. Machine-readable dependency names and licences belong in the CycloneDX SBOM, not in NOTICE.

Two boundaries worth stating plainly:

- **NFR-33's proprietary premium features are out of scope for Phase 0.** No third licence file is created now. When open-core features arrive, they get their own directory and their own `LICENSE`, following the same nearest-file-governs pattern.
- **FSL is not an OSI-approved open-source licence.** The reference documents describe ForgeOps as "fully open-source"; that is strictly accurate only for the Apache-2.0 agent and CLI. `README.md` therefore describes the backend as **"source-available, converting to Apache 2.0 after two years"** rather than "open source". This is a documentation-wording requirement, not a licensing change — see the residual note under D-19 (§17.1).

---

## 3. Sequence Diagrams — Phase 0 flows

### 3.1 MCP Gateway: security-ordered `tools/list` and `tools/call`

Routing remains a pure function of `Mcp-Method` + `Mcp-Name` (P-05), but bearer verification occurs before routing so an unauthenticated caller cannot use the registry as an oracle. Parsing needed for call authorization occurs only **after** header routing and never influences the selected upstream.

#### 3.1.1 `tools/list`: authenticate → route → obtain → filter → return

```mermaid
sequenceDiagram
    participant C as MCP Client
    participant G as MCP Gateway
    participant A as OIDC JWKS
    participant R as Redis TTL cache
    participant S as Target MCP server
    participant O as OPA server

    C->>G: POST /api/v1/mcp<br/>Mcp-Method: tools/list; Mcp-Name: docker
    G->>A: fetch JWKS when issuer cache misses
    G->>G: verify signature, exact iss allowlist, aud, exp, nbf
    alt token invalid
        G-->>C: 401 application/problem+json
    else token valid
        G->>G: route from headers only (body is not an input)
        G->>R: GET tool-list cache key
        alt Redis reports valid value with PTTL > 0
            R-->>G: cached unfiltered upstream list
        else miss, expired, or Redis unavailable
            G->>S: tools/list with traceparent
            S-->>G: tools plus ttlMs
            opt ttlMs > 0 and Redis available
                G->>R: SET PX min(ttlMs, MCP_CACHE_MAX_TTL_MS)
            end
        end
        G->>O: filter_tools(tools, claims, blast_radius)
        alt OPA unavailable or denies all
            O-->>G: empty allowed set (fail closed)
        else policy result
            O-->>G: allowed tool names
        end
        G-->>C: filtered tool list plus traceresponse
    end
```

The cache stores the upstream list, never a caller-specific filtered result; OPA filtering therefore runs on every response, including cache hits.

#### 3.1.2 `tools/call`: authenticate → route → resolve metadata → authorize → invoke

```mermaid
sequenceDiagram
    participant C as MCP Client
    participant G as MCP Gateway
    participant A as OIDC JWKS
    participant M as Tool metadata resolver
    participant O as OPA server
    participant S as Target MCP server

    C->>G: POST /api/v1/mcp<br/>Mcp-Method: tools/call; Mcp-Name: docker
    G->>A: fetch JWKS when issuer cache misses
    G->>G: verify signature, exact iss allowlist, aud, exp, nbf
    alt token invalid
        G-->>C: 401 application/problem+json
    else token valid
        G->>G: route from headers only (body is not an input)
        G->>G: parse called tool only after route is fixed
        G->>M: resolve tool metadata without executing handler
        M-->>G: declared annotations / not found
        G->>O: authorise_call(server, tool, metadata, claims, blast_radius)
        alt denied, OPA unavailable, or metadata unresolved
            O-->>G: deny
            Note over G,S: upstream invocation count remains zero
            G-->>C: 403 or 404 application/problem+json
        else allowed
            O-->>G: allow
            G->>S: tools/call with traceparent
            S-->>G: JSON-RPC response
            G-->>C: response plus traceresponse
        end
    end
```

The only operation before `authorise_call` that touches call content is metadata resolution from configured metadata or an already-valid Redis cache entry; it performs no upstream request and is side-effect-free. No denied call, policy error, unknown tool, or metadata failure may reach any upstream operation (P-05).

### 3.2 MCP Tasks Extension lifecycle — create, poll, cancel

```mermaid
sequenceDiagram
    participant C as MCP Client
    participant G as MCP Gateway
    participant R as Redis task store

    C->>G: tools/call with taskable true
    G->>R: create task, state submitted
    G-->>C: task id plus state submitted
    G->>R: transition to working
    C->>G: tasks/get with task id
    G->>R: read
    R-->>G: state working
    G-->>C: state working
    C->>G: tasks/cancel with task id
    G->>R: compare-and-set working to cancelled
    G-->>C: state cancelled
    C->>G: tasks/cancel again
    G-->>C: state cancelled, idempotent, no error
```

### 3.3 Model routing: OIDC → rate limit → cache → executable cascade

```mermaid
sequenceDiagram
    participant CALLER as Caller
    participant AUTH as OIDC verifier
    participant RL as Redis token bucket
    participant RT as ModelRouter
    participant CA as TieredCache
    participant RG as EndpointRegistry
    participant P1 as OpenAI-compatible primary
    participant P2 as OpenAI-compatible cross-provider
    participant SH as OpenAI-compatible self-hosted fixture

    CALLER->>AUTH: bearer token
    AUTH-->>CALLER: verified sub
    CALLER->>RL: consume(sub, /api/v1/ai/complete)
    alt Redis unavailable or bucket exhausted
        RL-->>CALLER: RFC 9457 503 or 429 + Retry-After
    else admitted
        RL->>RT: complete(tier, prompt)
        RT->>CA: lookup L1 then L2 then L3
        alt L1 or L2 hit
            CA-->>RT: cached response with served_from
            RT-->>CALLER: response, cached true
        else miss
            RT->>RG: ordered available endpoints for tier
            RG-->>RT: supported concrete adapters + unavailable reasons
            RT->>P1: complete(request, trace headers)
            P1--xRT: timeout
            RT->>P2: complete(request, trace headers)
            P2--xRT: malformed response
            RT->>SH: complete(request, trace headers)
            SH-->>RT: structured success
            RT->>CA: store L1 and L2
            RT-->>CALLER: response, degraded true, attempts recorded
        end
    end
```

OIDC verification and the atomic token-bucket admission happen before cache lookup or provider work. Unsupported `anthropic_native` / `google_native` endpoints are recorded as unavailable and skipped; they are never represented by a fake adapter.
### 3.4 OpenTofu runner: timeout, streaming, signal propagation

```mermaid
sequenceDiagram
    participant CALL as Caller
    participant RN as TofuRunner
    participant PG as Process group
    participant LOG as zap logger

    CALL->>RN: Plan with ctx deadline and workdir
    RN->>RN: build curated env allowlist
    RN->>PG: start tofu with new process group
    par stream stdout
        PG-->>RN: stdout lines
        RN->>LOG: info with stream stdout
    and stream stderr
        PG-->>RN: stderr lines
        RN->>LOG: warn with stream stderr
    end
    alt completes before deadline
        PG-->>RN: exit code
        RN-->>CALL: PlanResult with json plan
    else ctx cancelled or deadline
        RN->>PG: SIGTERM to negative pgid
        RN->>RN: wait grace period
        RN->>PG: SIGKILL to negative pgid
        RN-->>CALL: error context deadline exceeded
    end
```

### 3.5 Plan Analyzer: validation pipeline → approval seam

```mermaid
sequenceDiagram
    participant IN as Plan JSON from tofu plan
    participant PL as ValidationPipeline
    participant SY as SyntaxStage
    participant SC as SchemaStage
    participant SE as SemanticStage
    participant AP as ApprovalGate seam

    IN->>PL: run with PlanDocument
    PL->>SY: parse and normalise
    SY-->>PL: findings, fatal false
    PL->>SC: validate normalised shape
    SC-->>PL: findings
    PL->>SE: detect destructive actions, compute blast radius
    SE-->>PL: verdict allow warn or block plus score
    PL->>AP: submit verdict
    AP-->>PL: AUTO_OK or REQUIRES_APPROVAL or BLOCKED
    PL-->>IN: PipelineResult with findings, verdict, decision
```

### 3.6 GitOps: branch → commit → push → PR → poll

```mermaid
sequenceDiagram
    participant AG as Agent GitOps client
    participant LR as Local repo
    participant RM as Remote
    participant GH as GitHub REST

    AG->>LR: create branch from base
    AG->>LR: stage change-set paths
    AG->>LR: commit with deterministic message
    AG->>RM: push branch with token from TokenSource
    AG->>GH: create pull request
    GH-->>AG: pr number and url
    loop poll until terminal or timeout
        AG->>GH: get pr review state and mergeable state
        GH-->>AG: review state
    end
    AG-->>AG: PRStatus approved changes_requested or pending
```

---

## 4. Cross-Cutting Foundation Decisions

These are the contracts every later phase inherits. Changing them later is expensive, so they are fixed here.

### 4.1 Summary table

| Concern | Decision | Authority |
|:---|:---|:---|
| API versioning | URL-based `/api/v1/`; OpenAPI at `/api/v1/openapi.json` | Tech-Stack "Updated Immediate Action Items" Phase 1; PRD §5 |
| Error contract | **RFC 9457 Problem Details**, `application/problem+json`, on every non-2xx | PRD §5, Tech-Stack §"Error Format" |
| Agent ↔ backend protocol | **JSON-RPC 2.0 over WSS**, outbound-only | Research §0, PRD §2.1 |
| Streaming to browser | **SSE** via FastAPI native `EventSourceResponse`; never `sse-starlette` | Research §0, §A0b |
| CRUD | REST over HTTPS | PRD §2.1 |
| Config | 12-factor env vars; typed + validated for local correctness at startup; invalid project config fails fast, dependency reachability affects readiness | §7.1, §4.4 |
| Logging | Structured JSON; Go = `zap`; Python = stdlib `logging` + JSON formatter + `contextvars` correlation id | Research §A2 (zap); **OQ-3** for Python |
| DI | Go = **constructor injection**, no `wire`/`uber-fx`; Python = FastAPI `Depends` + `app.state`, no service locator | Research §0 "Constructor DI", PRD §5 |
| Telemetry | Phase 0 = W3C Trace Context propagation + `Tracer` seam. No OTel SDK, no Collector | phases.md 0.5 vs Phase 3 §3.2 → §7.8 |
| Task orchestration | `TaskDispatcher` Protocol from Day 1; Phase 0 ships `InlineDispatcher` only | Research §0 "interface discipline starts at Day 1" |
| Dependency pinning | Exact versions, lockfiles committed, no floating ranges, CI action SHAs pinned | phases.md Phase 0 risk row: "pin versions" |
| Multi-tenancy | PostgreSQL RLS (Phase 1); Phase 0 leaves a nullable `tenant_id` column and a transaction-scoped set-local seam | Research §0 |
| Vector index | pgvector **HNSW**, cosine; `hnsw.ef_search` tuned at query time | Research §0, §A0a |

### 4.2 RFC 9457 error contract

Every non-2xx response from the backend carries `Content-Type: application/problem+json` and this body:

```jsonc
{
  "type": "https://errors.forgeops.dev/validation-failed",           // stable URI, not a URL to fetch
  "title": "Request validation failed",                              // short, human, stable per type
  "status": 422,                                                     // MUST equal the HTTP status
  "detail": "Field 'tier' is not a recognised model tier.",           // instance-specific
  "instance": "/api/v1/ai/complete",                                 // the offending request path
  "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",                    // extension: W3C trace-id
  "errors": [                                                        // extension: field-level detail
    { "pointer": "#/tier", "detail": "unknown tier 'ultra'" }
  ]
}
```

Rules: `type` is a stable registry URI owned by the project and never resolved at runtime; `status` always equals the HTTP status code (property **P-09**); `detail` never contains secrets, tokens, connection strings, or stack traces; unhandled exceptions map to `type: .../internal` with a generic `detail` and the `trace_id` for correlation.

### 4.3 Middleware stack ordering (backend)

Outermost first. In Starlette, `add_middleware` prepends, so the registration order in code is the reverse of this table — the app factory documents this inline to prevent regression.

| # | Middleware | Why at this position |
|:-:|:---|:---|
| 1 | Starlette `ServerErrorMiddleware` | Must wrap everything so nothing escapes un-shaped |
| 2 | `RequestIdMiddleware` | Every log line and problem body needs an id, including auth failures |
| 3 | `TraceContextMiddleware` | Parses inbound `traceparent`/`tracestate`, seeds the context var, emits `traceresponse` |
| 4 | `AccessLogMiddleware` | Logs after ids exist, before business logic, records final status |
| 5 | `CORSMiddleware` | Must answer preflight without touching routing or DB |
| 6 | *(Phase 1)* `TenantContextMiddleware` | Deliberate gap — documented so Phase 1 inserts here, not elsewhere |
| 7 | Router / endpoint dependencies | Auth dependency (Phase 1) attaches per-route, not globally |

### 4.4 Health, startup and dependency contract

Probe endpoints are **unversioned** — they are an infrastructure contract for container orchestrators, not part of the public API surface, so they must not move when the API version bumps.

| Endpoint | Semantics | Success | Failure |
|:---|:---|:---|:---|
| `GET /health` | Liveness. Event loop accepts work. Touches **no** Postgres, Redis, OPA, or vendor dependency | `200 {"status":"ok","version":"<semver>","commit":"<sha>"}` even during a temporary dependency outage | Process is dead or wedged; no response |
| `GET /health/ready` | Readiness. Postgres `SELECT 1` + Redis `PING`, each with a 2 s timeout | `200 {"status":"ready","checks":{"postgres":"ok","redis":"ok"}}` | RFC 9457 `503`, `type: .../not-ready`, one `errors[]` item per failed/timed-out check |
| `GET /api/v1/health` | Versioned informational echo of liveness, also dependency-free | `200` while the event loop is healthy | Process-level failure only |

Lifespan fails fast for invalid configuration and failure to construct local resources (for example an invalid URL, TLS context, serializer, or client object). It **does not** abort startup solely because Postgres or Redis is unreachable. Engines/pools/clients are constructed lazily or non-destructively; initial probes log structured warnings. Redis semantic-index creation runs when Redis first becomes reachable and retries idempotently with bounded backoff; its initial failure never kills liveness. Runtime dependency failures affect readiness and the specific operation, not `/health`.

Phase 0 completion criterion 5 is proved in two modes: `/health` remains 200 with Postgres and Redis deliberately unavailable, while `/health/ready` returns the specified 503; with the default Compose profile healthy, both return 200. Compose uses `/health` for the backend container liveness check; the post-start gate polls `/health/ready` separately.

---

## 5. Component Decomposition and Responsibilities

### 5.1 Tier 3 — Local Agent (Go 1.26)

**Purpose in Phase 0:** prove that a single statically linked binary builds and cross-compiles for six targets, wires its dependency graph by constructor injection, shuts down deterministically, hosts an MCP server, runs OpenTofu safely, and performs Git/PR operations.

**Interface (Phase 0 surface):**

```go
// internal/app/app.go
type App struct { /* constructed fields only, no globals */ }

// Run blocks until ctx is cancelled or a subsystem fails permanently.
func (a *App) Run(ctx context.Context) error

// Close releases resources in reverse construction order. Idempotent.
func (a *App) Close() error
```

**Responsibilities:** process lifecycle and shutdown ordering; typed configuration; CLI surface (`run`, `doctor`, `version`, `mcp serve`); MCP tool hosting; OpenTofu subprocess supervision; Git/PR operations; atomic file operations with backups.

**Explicitly not responsible in Phase 0:** connecting to a backend, executing commands, evaluating policy, scanning codebases, self-updating.

### 5.2 Tier 2 — Backend (FastAPI 0.139.2 modular monolith)

**Purpose in Phase 0:** an ASGI application with a validated config, structured logging, an async DB session factory, a fixed middleware stack, health probes, the MCP Gateway, the model router, and the plan-analysis pipeline.

**Interface (Phase 0 surface):**

| Route | Method | Purpose |
|:---|:---|:---|
| `/health`, `/health/ready`, `/api/v1/health` | GET | §4.4 |
| `/api/v1/mcp` | POST | MCP Gateway ingress, routed by headers (§11.4) |
| `/api/v1/mcp/servers` | GET | Registry introspection, OPA-filtered |
| `/api/v1/mcp/apps/{name}` | GET | MCP App descriptor + sandboxed host page (§11.6) |
| `/api/v1/ai/tiers` | GET | Declared six tiers plus endpoint protocol, availability reason, and breaker state |
| `/api/v1/ai/complete` | POST | OIDC + atomic per-caller limiter, then real registry/cache/cascade/breaker end-to-end |
| `/api/v1/analysis/plan` | POST | Runs the validation pipeline over a submitted plan document |

Modular monolith rule: domains under `src/` may depend on `src/core/` and on their own package; **cross-domain imports are forbidden** and enforced by a Ruff `flake8-tidy-imports` banned-api rule. This is what keeps the monolith extractable later (Research §B5 "modular monolith first").

### 5.3 MCP Gateway (stateless)

Per PRD §2.1a and phases.md 0.5. Seven concerns, each an injected collaborator so any one can be swapped:

| Concern | Implementation | Notes |
|:---|:---|:---|
| Authentication | `OidcTokenVerifier` | Always first; JWKS cache, exact `iss` allowlist, `aud`, `exp`, `nbf` (§15.2) |
| Routing | `HeaderRouter` on `Mcp-Method` + `Mcp-Name` | Runs after auth; pure function of headers; body never parsed (P-05) |
| Metadata resolution | `ToolMetadataResolver` | For `tools/call`, resolves the called tool from configured metadata or an already-valid Redis tool-list entry; no upstream request and no handler execution. Missing metadata denies the call |
| Policy | `OpaGatewayPolicy` | Filters every `tools/list`; must authorize every `tools/call` before upstream dispatch |
| Caching | `TtlToolCache` on Redis | Redis TTL/PTTL is authoritative; `SET PX min(ttlMs,max)` (P-06) |
| Tracing | `TraceContextPropagator` | W3C `traceparent`/`tracestate` in, forwarded out |
| Tasks | `RedisTaskStore` + `TaskStateMachine` | `tasks/get`, `tasks/update`, `tasks/cancel` (P-10) |
| Apps | `McpAppRegistry` | Sandboxed iframe descriptors + strict CSP |

The gateway has two explicit orchestration paths and no generic "forward then authorize" path:

1. **`tools/list`:** `verify bearer/OIDC → route(headers) → cache GET or upstream tools/list → OPA filter_tools → return filtered list`.
2. **`tools/call`:** `verify bearer/OIDC → route(headers) → parse called tool → resolve metadata without execution → OPA authorise_call → only on allow invoke upstream → return response`.

`dispatch_upstream` is reachable only from the authorized branch. Denial, OPA failure, unknown metadata, or authorization exception returns without invoking it; tests inject a counting upstream and assert the count remains zero (P-05). Policy may inspect the parsed call after routing is fixed, but routing itself never receives the JSON-RPC body.

**Stateless** means: no per-connection server state in the gateway process. All shared state (tool cache, task records) lives in Redis, so any replica can serve any request.

### 5.4 Policy engine placement (correcting a common trap)

| Where | Engine | Phase |
|:---|:---|:---|
| MCP Gateway tool filtering | **OPA server** (sidecar/service, HTTP) | **0** |
| Backend application RBAC | **Cerbos v0.54.0** sidecar | 1+ |
| Agent-side double evaluation | **OPA compiled to Wasm, embedded in the Go binary** | 1 |
| Kubernetes admission | Kyverno | 2+ |

Cerbos is a sidecar/service and **cannot** be embedded in a single Go binary (Research §B7). Phase 0 must not introduce any assumption that the agent will embed Cerbos.

### 5.5 Tier 1 — Frontend (Next.js 16 + React 19)

**Purpose in Phase 0:** an App Router shell that loads at `localhost:3000`, renders sidebar + header + theme toggle, and calls the backend through an RFC 9457-aware client. The primary sidebar contains one real keyboard-accessible **Home** link to `/` with an active state; it contains no disabled or placeholder links for future features.

### 5.6 Executable model endpoint layer

`ModelRouter` depends on the `ModelEndpoint` Protocol, not on vendor SDKs. `EndpointRegistry` validates all configured endpoint descriptors and constructs only supported adapters. Phase 0 includes the production `OpenAICompatibleEndpoint` built on `httpx`, covering OpenAI-compatible hosted APIs and the configured self-hosted endpoint. It enforces an explicit timeout, structured request/response models, `SecretStr` key resolution, redacted typed errors, and W3C trace-header injection (§11.7).

`anthropic_native` and `google_native` are valid declarative protocols but their codecs/adapters belong to Phase 1. Such descriptors are exposed as `unavailable` with a stable reason and skipped by the cascade; no fake production adapter is allowed. This is the required seam-not-stub implementation: the common compatible protocol executes real requests in Phase 0, while unsupported protocols are honest data.

### 5.7 AI completion rate limiter

`RedisTokenBucketLimiter` protects only `POST /api/v1/ai/complete`; it is not the Phase 1 budget system. The key is `(verified OIDC sub, route)`. A Lua script performs refill, token consumption, state write and TTL update atomically so concurrent replicas cannot overspend. It runs after OIDC verification and before semantic-cache or provider work. Capacity, refill rate and fail mode are settings; this costly route requires `fail_closed`. Redis failure returns RFC 9457 503, while exhaustion returns RFC 9457 429 with `Retry-After` (§11.7.5).

---

## 6. Phase 0 Data Model

### 6.1 PRD §7 table groups — in scope vs deferred

| Group | Tables | Phase 0 |
|:---|:---|:---|
| **D1** Users, Teams & Projects | `users`, `teams`, `team_members`, `projects`, `project_tags`, `sessions`, `agent_devices` | **`projects` only** (minimal). The rest are identity/auth domain — excluded with authentication |
| **D2** Codebase Index | `file_tree`, `file_contents`, `embeddings`, `analysis_reports` | **`file_tree` + `embeddings`** — the minimum that proves the pgvector + HNSW pattern. `file_contents` and `analysis_reports` are analysis feature logic |
| **D3** Change-sets & Approvals | `change_sets`, `change_items`, `validations`, `approvals` | Deferred to Phase 1 (§1.6) |
| **D4** Deployments & Environments | all | Deferred to Phase 2 |
| **D5** Secret Vault | `secrets` | Deferred to Phase 1 (§1.8). Phase 0 BYO-Keys live in Infisical, not in Postgres |
| **D6** AI Learning History | `feedback_events`, `skill_files` | Deferred to Phase 3 |
| **D7** Policies | `policies`, `policy_evaluations` | Deferred to Phase 1. Phase 0 gateway policy is a Rego file on disk, not a DB row |
| **D8** Incidents & Telemetry | all | Deferred to Phase 3 |

Rationale: `phases.md` Phase 0 excludes "database migrations beyond initial schema", and the only schema-related completion criterion is *"SQLModel models defined with pgvector column support (HNSW index)"*. Three tables satisfy it with a coherent FK chain and no feature logic.

### 6.2 Initial schema — the single Alembic revision `0001_initial`

```mermaid
erDiagram
    projects ||--o{ file_tree : contains
    file_tree ||--o{ embeddings : chunked_into

    projects {
        uuid id PK
        uuid tenant_id "nullable seam, no RLS in Phase 0"
        text name
        text path
        text repo_url "nullable"
        jsonb settings "default '{}'"
        timestamptz created_at
        timestamptz updated_at
    }
    file_tree {
        uuid id PK
        uuid project_id FK
        text path
        text content_hash "sha256 hex"
        bigint size_bytes
        timestamptz last_modified
        timestamptz created_at
    }
    embeddings {
        uuid id PK
        uuid file_id FK
        uuid tenant_id "nullable seam"
        int chunk_index
        text chunk_text
        vector embedding "1536 dims"
        text model_id "provenance of the vector"
        timestamptz created_at
    }
```

SQLModel definitions (`expire_on_commit=False` is a session concern, §11.3):

```python
# backend/src/projects/models.py
import uuid
from datetime import datetime
from sqlmodel import SQLModel, Field
from sqlalchemy import Column, DateTime, func, text
from sqlalchemy.dialects.postgresql import JSONB

class Project(SQLModel, table=True):
    __tablename__ = "projects"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    # Seam for Phase 1 PostgreSQL RLS. Nullable now; NOT NULL + policies arrive
    # in the Phase 1 migration so there is no backfill of live rows later.
    tenant_id: uuid.UUID | None = Field(default=None, index=True)
    name: str = Field(max_length=200, index=True)
    path: str = Field(max_length=1024)
    repo_url: str | None = Field(default=None, max_length=1024)
    settings: dict = Field(default_factory=dict, sa_column=Column("settings", JSONB, nullable=False, server_default=text("'{}'::jsonb")))
    created_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False, server_default=func.now()))
    updated_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()))
```

```python
# backend/src/analysis/models.py
from pgvector.sqlalchemy import Vector
from sqlalchemy import Column, UniqueConstraint

# SETTLED by decision D-2 (§17.1): the Phase 0 vector column is fixed at 1536
# dimensions — Voyage Code 3, the primary API embedding model (Research §C10).
# BGE-M3's 1024-d self-hosted vectors are NOT stored in this column; the
# multi-model strategy (second table per dimension, or Matryoshka truncation to a
# common size) is decided in Phase 1. Every row carries model_id for provenance.
EMBEDDING_DIMS = 1536

class FileTreeEntry(SQLModel, table=True):
    __tablename__ = "file_tree"
    __table_args__ = (UniqueConstraint("project_id", "path", name="uq_file_tree_project_path"),)

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    project_id: uuid.UUID = Field(foreign_key="projects.id", index=True, ondelete="CASCADE")
    path: str = Field(max_length=1024)
    content_hash: str = Field(max_length=64)
    size_bytes: int
    last_modified: datetime
    created_at: datetime = Field(default_factory=datetime.utcnow)

class Embedding(SQLModel, table=True):
    __tablename__ = "embeddings"
    __table_args__ = (UniqueConstraint("file_id", "chunk_index", name="uq_embeddings_file_chunk"),)

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    file_id: uuid.UUID = Field(foreign_key="file_tree.id", index=True, ondelete="CASCADE")
    tenant_id: uuid.UUID | None = Field(default=None, index=True)
    chunk_index: int
    chunk_text: str
    # Provenance, mandated by D-2: which model produced this vector. Required so a
    # Phase 1 multi-model strategy can distinguish 1536-d from 1024-d sources.
    model_id: str = Field(max_length=100)  # e.g. "voyage-code-3"
    embedding: list[float] = Field(sa_column=Column("embedding", Vector(EMBEDDING_DIMS), nullable=False))
    created_at: datetime = Field(default_factory=datetime.utcnow)
```

### 6.3 pgvector + HNSW

Three things are **settled**, not pending, per decision **D-2** (§17.1):

1. **Dimension = 1536**, matching Voyage Code 3, the primary API embedding model.
2. **Index = HNSW** with cosine distance. IVFFlat is explicitly rejected for production vector search (Research §0, §A0a).
3. **`model_id` is stored on every vector row** as provenance, so a future multi-model strategy can tell a `voyage-code-3` vector from a `bge-m3` one without guessing. It is `NOT NULL` and indexed-by-inclusion in the uniqueness story only through `(file_id, chunk_index)`; its job is attribution, not identity.

Phase 1 follow-up (tracked, not open): enabling self-hosted BGE-M3 at 1024-d requires either a second table per dimension or Matryoshka truncation to a common size. Nothing in Phase 0 forecloses either option, because `model_id` already distinguishes the sources.

```sql
-- 0001_initial, executed before any vector column is created
CREATE EXTENSION IF NOT EXISTS vector;

-- HNSW is the Phase 0 default; IVFFlat is explicitly rejected for production
-- vector search (Research §0 and §A0a).
CREATE INDEX ix_embeddings_embedding_hnsw
    ON embeddings USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
```

`hnsw.ef_search` is a **query-time** knob, never baked into the index. The session helper sets it per query so recall/latency can be tuned without a reindex:

```python
# backend/src/core/db.py
async def with_ef_search(session: AsyncSession, ef_search: int) -> None:
    """Tune HNSW recall for the current transaction only (Research §A0a)."""
    await session.execute(text("SET LOCAL hnsw.ef_search = :v"), {"v": ef_search})
```

**Alembic + pgvector column detection.** Autogenerate does not know the `Vector` type by default, producing a spurious drop/recreate on every revision. `alembic/env.py` therefore registers the type and renders it explicitly:

```python
# alembic/env.py
from pgvector.sqlalchemy import Vector

def render_item(type_, obj, autogen_context):
    """Teach autogenerate to emit pgvector columns correctly."""
    if type_ == "type" and isinstance(obj, Vector):
        autogen_context.imports.add("from pgvector.sqlalchemy import Vector")
        return f"Vector({obj.dim})"
    return False

context.configure(
    connection=connection,
    target_metadata=SQLModel.metadata,
    render_item=render_item,
    compare_type=True,
    include_schemas=False,
)
```

### 6.4 Naming and migration conventions

A `MetaData(naming_convention=...)` is set in `src/core/db.py` (`ix_`, `uq_`, `ck_`, `fk_`, `pk_`) so Alembic never emits database-generated constraint names — otherwise Phase 1's migrations become environment-dependent. Revision id format: `NNNN_snake_case_summary`, monotonically numbered, linear history, no branches.

### 6.5 Multi-tenancy seam (Phase 1, prepared in Phase 0)

Research §0 fixes RLS over schema-per-tenant, with PgBouncer transaction-mode pooling. Two Phase 0 consequences, both cheap now and expensive later:

1. **Column seam** — `tenant_id UUID NULL` on `projects` and `embeddings`. Phase 1 sets `NOT NULL` and enables policies; no data backfill of live rows.
2. **Transaction-scoped tenant variable** — Phase 1 RLS must read `current_setting('app.tenant_id', true)` set via `SET LOCAL` **inside the transaction**, never a session-level `SET`, because transaction-mode pooling recycles connections between statements. The Phase 0 session helper (`with_ef_search`) already demonstrates the `SET LOCAL` pattern so Phase 1 inherits it rather than inventing a session-level variant.

Also recorded for Phase 1: with PgBouncer in transaction mode, asyncpg requires `statement_cache_size=0` (or a per-connection prepared-statement name salt). Noted here so the Phase 0 engine factory comments the constraint at the point of change.

---

## 7. Cross-Cutting Foundations in Detail

### 7.1 Configuration management

Both services use a **flat env-var namespace**, validated once at startup, reporting all project-configuration problems together (P-15). Scope matters: unknown keys in a project configuration source (`.env.example`, optional `.env`, or an explicit `Settings` input mapping) are errors; unrelated ambient OS variables such as `PATH`, shell variables, CI metadata, or editor variables are ignored. A normal host environment must never make the process fail merely because it contains unrelated keys. The Go loader likewise validates only ForgeOps keys it consumes and accumulates all missing/invalid ForgeOps values.

Compose owns loading `.env.example` plus optional `.env` (§13.3), so the backend normally receives declared values as environment variables. Direct local startup uses a small `load_project_dotenv` helper that parses the same two files, rejects names outside the repository-wide Phase 0 key inventory, then passes only backend fields to `Settings`. No dynaconf or viper is introduced.

Python uses `pydantic-settings` with ambient-env-safe source handling:

```python
# backend/src/core/config.py
from typing import Literal, Mapping
from pydantic import Field, PostgresDsn, RedisDsn, AnyHttpUrl, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    # Ambient environment is a normal process environment: only declared fields
    # are considered. Project dotenv files are parsed and key-checked separately.
    model_config = SettingsConfigDict(env_file=None, env_prefix="", extra="forbid", case_sensitive=False)

    app_env: str = Field(default="development", pattern="^(development|test|production)$")
    log_level: str = Field(default="INFO")
    api_prefix: str = Field(default="/api/v1")
    service_version: str = Field(default="0.0.0")
    git_commit: str = Field(default="unknown")

    database_url: PostgresDsn
    database_pool_size: int = Field(default=10, ge=1, le=100)
    redis_url: RedisDsn

    # MCP Gateway
    mcp_oidc_issuers: list[AnyHttpUrl] = Field(default_factory=list)
    mcp_oidc_audience: str
    mcp_oidc_jwks_ttl_seconds: int = Field(default=600, ge=60)
    mcp_cache_max_ttl_ms: int = Field(default=300_000, ge=0)
    opa_url: AnyHttpUrl

    # Model routing
    model_tier_config_path: str = Field(default="config/model-tiers.yaml")
    cb_failure_threshold: int = Field(default=5, ge=1)
    cb_window_seconds: int = Field(default=30, ge=1)
    cb_open_seconds: int = Field(default=60, ge=1)
    semantic_cache_threshold: float = Field(default=0.95, ge=0.0, le=1.0)
    outbound_http_timeout_seconds: float = Field(default=60.0, gt=0, le=300)
    ai_rate_limit_capacity: int = Field(default=20, ge=1)
    ai_rate_limit_refill_per_second: float = Field(default=0.2, gt=0)
    ai_rate_limit_fail_mode: Literal["fail_closed"] = "fail_closed"

    @field_validator("mcp_oidc_issuers")
    @classmethod
    def _require_issuer_in_production(cls, v, info):
        # An empty issuer allowlist would accept any iss — refuse to boot.
        if not v and info.data.get("app_env") == "production":
            raise ValueError("MCP_OIDC_ISSUERS must be non-empty when APP_ENV=production")
        return v

def get_settings(explicit: Mapping[str, object] | None = None) -> Settings:
    # Explicit/project mappings are strict: extra="forbid" rejects unknown keys.
    # With no explicit mapping, BaseSettings reads only declared names from the
    # ambient environment and correctly ignores unrelated OS variables.
    return Settings(**dict(explicit or {}))
```

`load_project_dotenv((".env.example", ".env"))` treats the baseline as required and the override as optional, validates every parsed name against `PROJECT_CONFIG_KEYS`, merges override values last, and then selects the backend fields for `get_settings`. Validation errors from unknown project keys and Pydantic field failures are accumulated into one startup report.

Go uses a hand-rolled typed loader (no viper — it is not in the authoritative stack):

```go
// internal/config/config.go
type Config struct {
    LogLevel        string
    LogFormat       string        // "json" | "console"
    BackendWSSURL   string        // empty => connection manager reports Disabled (Phase 0)
    ShutdownTimeout time.Duration
    Tofu            TofuConfig
    Git             GitConfig
    MCP             MCPConfig
}

// Load reads the environment and returns a fully-validated Config, or an error
// that enumerates every problem found. It never returns a partially-populated
// Config alongside an error.
func Load(getenv func(string) string) (*Config, error)
```

### 7.2 Structured logging

| Runtime | Library | Format | Required fields |
|:---|:---|:---|:---|
| Go agent | `go.uber.org/zap` (Research §A2) | JSON in production, console in dev | `ts`, `level`, `msg`, `caller`, `component`, `trace_id`, `span_id` |
| Backend | stdlib `logging` + `dictConfig` + JSON formatter | JSON always | `ts`, `level`, `logger`, `msg`, `request_id`, `trace_id`, `span_id` |

Correlation ids travel via `contextvars` in Python and explicit `context.Context` values in Go — never via globals or thread locals. Redaction is a logging-layer concern: a `SecretRedactingFilter` scrubs values matching known secret env-var names and bearer-token patterns before any record is emitted, because NFR-10 forbids secret leakage and logs are the easiest leak. **OQ-3** records that no Python logging library is named by any authority; stdlib is chosen to avoid adding an unsanctioned dependency.

### 7.3 Protocol choices

| Channel | Protocol | Phase 0 status |
|:---|:---|:---|
| Browser → backend, CRUD | REST over HTTPS, `/api/v1/` | Built |
| Backend → browser, streaming | SSE, FastAPI native `EventSourceResponse` | Contract fixed, one demonstration endpoint pattern documented; feature streams are Phase 1 |
| Agent ↔ backend | JSON-RPC 2.0 over WSS, outbound-only from the agent | Envelope types + transport built; handshake/heartbeat/reconnect are Phase 1 |
| Backend ↔ MCP servers | MCP over HTTP with `Mcp-Method`/`Mcp-Name` headers | Built |
| Agent-hosted MCP | MCP over stdio and HTTP/SSE via `mark3labs/mcp-go` | Built |

JSON-RPC 2.0 envelope, fixed now so Phase 1 cannot drift (shared shape, Go side shown):

```go
// internal/connection/jsonrpc.go
type Request struct {
    JSONRPC string          `json:"jsonrpc"`          // always "2.0"
    ID      *string         `json:"id,omitempty"`     // nil => notification
    Method  string          `json:"method"`           // e.g. "command.execute"
    Params  json.RawMessage `json:"params,omitempty"`
}

type Response struct {
    JSONRPC string          `json:"jsonrpc"`
    ID      *string         `json:"id"`
    Result  json.RawMessage `json:"result,omitempty"`
    Error   *RPCError       `json:"error,omitempty"` // Result and Error are mutually exclusive
}

type RPCError struct {
    Code    int             `json:"code"`
    Message string          `json:"message"`
    Data    json.RawMessage `json:"data,omitempty"`
}
```

### 7.4 SSE without `sse-starlette`

Research §0 is explicit: `EventSourceResponse` is in-tree since FastAPI 0.139.2 and `sse-starlette` must not be added. Phase 0 fixes the event-type vocabulary from Research §0 — `status`, `token`, `progress`, `validation`, `complete`, `error` — as an enum in `src/core/sse.py` so Phase 1 producers cannot invent divergent names. No feature stream is implemented.

### 7.5 Dependency-injection conventions

**Go — constructor injection, no framework** (Research §0; `wire`/`uber-fx` are explicitly rejected for startup time and binary size):

- every collaborator arrives as a constructor argument; no package-level singletons; no `init()` side effects;
- constructors return concrete types, accept interfaces;
- interfaces are declared in the **consumer** package, keeping packages independently testable;
- `main.go` is the only place that knows the whole graph.

**Python — FastAPI `Depends` + `app.state`:** long-lived resources (engine, sessionmaker, lazy Redis client, shared HTTP client, endpoint registry, router, gateway, limiter) are constructed non-destructively in the lifespan and stored on `app.state`; request-scoped access is via `Depends` providers that read `request.app.state`. No module-level clients, so tests can build an app with substituted collaborators.

### 7.6 Testing strategy and layout

| Component | Unit | Integration | Property-based | E2E / Load |
|:---|:---|:---|:---|:---|
| Agent | `go test ./...` with `-race -shuffle=on` (PRD §9) | `//go:build integration` tag; needs Docker/OpenTofu | `pgregory.net/rapid` (**OQ-4**) | — |
| Backend | `pytest` | `pytest-asyncio` + `httpx.AsyncClient` against the real app and a real Postgres/Redis | `hypothesis` (**OQ-4**) | — |
| Frontend | `vitest` + `@testing-library/react` | msw-style fetch interception on the API client | `fast-check` (**OQ-4**) | Playwright, k6 |

Layout: `agent/internal/<pkg>/<file>_test.go` (same package for internals, `_test` package for public surface); `backend/tests/{unit,integration,property}/` mirroring `src/`; `frontend/**/*.test.tsx` co-located, `frontend/e2e/*.spec.ts`, `frontend/load/*.js`.

Coverage goal for the backend is **>70 %** (phases.md 0.3). It is a *goal*, not a gate, in Phase 0 — a gate on scaffolding rewards test theatre. The gate arrives in Phase 1 where §1 sets ≥70 % as a completion criterion. `make test` prints coverage; CI uploads it.

### 7.7 Dependency pinning policy

| Ecosystem | Mechanism | Rule |
|:---|:---|:---|
| Go | `go.mod` exact versions + committed `go.sum` | No `latest`; `GOFLAGS=-mod=readonly`; `go mod verify` + `govulncheck` in CI |
| Python | `pyproject.toml` exact direct pins; `pip-tools==7.4.1`; committed hash-pinned `requirements.lock` + `requirements-dev.lock` | `pyproject.toml` is source of truth. `pip-compile --generate-hashes --output-file=requirements.lock pyproject.toml` selects runtime; `pip-compile --generate-hashes --extra dev --output-file=requirements-dev.lock pyproject.toml` selects runtime + test/lint. Docker installs runtime lock only with `pip install --require-hashes -r requirements.lock`; CI installs dev lock with the same flag. `make lock-backend` regenerates both; CI regenerates and fails on any diff, then runs `pip-audit` |
| Node | `package.json` exact versions + committed `pnpm-lock.yaml` | `pnpm install --frozen-lockfile` in CI; `pnpm audit` |
| Container images | tag **and** `@sha256:` digest in `docker-compose.yml` and Dockerfiles | Digest resolved at implementation; renovate updates it |
| GitHub Actions | pinned to a full commit SHA, never a tag | Tags are mutable; SHAs are not |

Authority: phases.md Phase 0 risk row mitigation is literally "Use well-established tools, pin versions".

### 7.8 Telemetry seams without an OTel SDK

`phases.md` 0.5 requires **W3C Trace Context propagation across all MCP server calls**, while OTel Collector/Prometheus/Loki/Tempo belong to Phase 3 §3.2. Resolution:

- Phase 0 implements a ~100-line `tracecontext` module in each runtime: parse and validate `traceparent` (version, 32-hex trace-id, 16-hex span-id, 8-bit flags), preserve `tracestate`, mint child span-ids, and inject headers on every outbound MCP/HTTP call.
- A `Tracer` interface exists with exactly one Phase 0 implementation, `NoopTracer`, which propagates context but records nothing.
- No exporters, no collector, no `gen_ai.*` semantic conventions, no sampling configuration. Phase 3 swaps in the OTel SDK behind `Tracer` and inherits a codebase that already threads context correctly — which is the expensive half of the work.

Invalid inbound `traceparent` is discarded and a fresh trace started (per the W3C spec), never propagated malformed.

### 7.9 Orchestrator-agnostic task seam

Research §0 requires exactly one durable engine introduced once at the P2 boundary behind a thin-wrapper interface, and states the interface discipline starts Day 1. Phase 0 therefore fixes the interface and ships no queue:

```python
# backend/src/core/tasks.py
from typing import Protocol, Any
from dataclasses import dataclass

@dataclass(frozen=True)
class TaskHandle:
    id: str
    dispatcher: str  # "inline" now; "arq"/"dramatiq" at P1; durable engine at P2

class TaskDispatcher(Protocol):
    """The only way business logic ever enqueues work.

    Phase 0: InlineDispatcher. Phase 1: ARQ or Dramatiq. Phase 2: exactly one
    durable engine (Temporal, or Inngest if self-host DX wins) — introduced ONCE.
    Business logic must never import an engine SDK directly (Research §0, §B6).
    """
    async def enqueue(self, name: str, payload: dict[str, Any], *, idempotency_key: str | None = None) -> TaskHandle: ...

class InlineDispatcher:
    """Executes the handler in-process, immediately. Development and Phase 0 only.

    Not durable, not retried, not a queue. It exists so the seam has a real
    implementation rather than a stub, and so Phase 1 can swap it out with a
    one-line change in the lifespan.
    """
```

A Ruff banned-api rule rejects any import of `celery`, `arq`, `dramatiq`, `temporalio`, or `inngest` outside `src/core/tasks.py`, making the seam mechanically enforced rather than aspirational. Celery is banned permanently (Research §0).

---

## 8. Build, CI and Release Architecture

### 8.1 Supply-chain chain of custody

```mermaid
graph LR
    SRC["Tagged commit<br/>v0.x.y"] --> GA["GitHub Actions release.yml<br/>id-token write"]
    GA --> GR["GoReleaser<br/>6 targets, CGO_ENABLED=0,<br/>-trimpath, mod_timestamp"]
    GR --> BIN["Binaries + nfpm deb/rpm + archives"]
    BIN --> SY["Syft<br/>CycloneDX JSON SBOM per artifact"]
    SY --> CO["Cosign keyless<br/>OIDC to Fulcio"]
    CO --> RK["Rekor transparency log<br/>inclusion proof"]
    CO --> ATT["SLSA provenance attestation"]
    ATT --> REL["GitHub Release<br/>artifacts + .sbom.json + .sig + .pem + provenance"]
    REL --> VER["cosign verify-blob<br/>--certificate-identity-regexp<br/>--certificate-oidc-issuer"]
```

Every link is verifiable by a third party with no shared secret: the signing identity is the workflow's OIDC identity, the certificate is issued by Fulcio, and the signature is logged in Rekor. Authority: PRD §9 Release Pipeline, Research §A4, Tech-Stack "Build Pipeline".

### 8.2 Cross-compilation matrix

| GOOS | GOARCH | Artifact |
|:---|:---|:---|
| linux | amd64, arm64 | tar.gz, deb, rpm |
| darwin | amd64, arm64 | tar.gz |
| windows | amd64, arm64 | zip |

Six targets, satisfying the completion criterion. **`CGO_ENABLED=0` for all of them** — which is precisely why the CGO-dependent `tree-sitter/go-tree-sitter` dependency is deferred to Phase 1 by decision **D-1** (§17.1). Phase 0's build stays pure-static with no cross-compilation toolchain (no zig cc, no `xx`); adopting one is Phase 1 §1.3's problem, at the point where AST parsing earns it.

GoReleaser configuration facts that follow from the module path (§0.3):

```yaml
# agent/.goreleaser.yaml — module github.com/parag8487/ForgeOps/agent
project_name: forgeops-agent
builds:
  - id: agent
    main: ./cmd/agent                 # package github.com/parag8487/ForgeOps/agent/cmd/agent
    binary: forgeops-agent
    env: [CGO_ENABLED=0]
    flags: [-trimpath]
    mod_timestamp: "{{ .CommitTimestamp }}"
    ldflags:
      - -s -w
      - -X main.version={{ .Version }}
      - -X main.commit={{ .FullCommit }}
      - -X main.date={{ .CommitDate }}
    goos:   [linux, darwin, windows]
    goarch: [amd64, arm64]
```

The `-X` targets are the `main` package's own variables (§10.2), so the injection paths are `main.version`, `main.commit`, `main.date` — resolved against `./cmd/agent` within the `github.com/parag8487/ForgeOps/agent` module. GoReleaser runs with its working directory set to `agent/`, because that is where `go.mod` lives in the monorepo.

### 8.3 CI pipeline (`.github/workflows/ci.yml`)

Mirrors PRD §9 minus the phases that need features:

```
0. paths-filter (dorny/paths-filter)  -> agent / backend / frontend / infra changed?
1. pre-commit run --all-files          -> gitleaks, ruff, gofmt, prettier, hygiene
2. lock-integrity                       -> regenerate Python locks (no diff); tofu init -lockfile=readonly on fixture
3. lint      -> golangci-lint | ruff check + ruff format --check | eslint + tsc --noEmit
4. test      -> go test -race -shuffle=on ./... | pytest (services: postgres, redis) | vitest --run
5. build     -> go build (matrix) | docker build backend | pnpm build with public build args
6. e2e       -> playwright (frontend served from the built output)
7. audit     -> go mod verify + govulncheck | pip-audit | pnpm audit
8. supply    -> goreleaser release --snapshot (validates the release config on every PR)
```

The backend job installs `requirements-dev.lock` with `--require-hashes`; its freshness step runs `make lock-backend` and fails if either lock differs. The backend image installs only `requirements.lock`. The OpenTofu integration job runs `tofu init -lockfile=readonly` and validates the six-platform provider lock. The frontend image build supplies a non-default browser URL as a build arg and tests the generated client. Compose smoke evidence starts from no `.env`, runs the direct unprofiled command, and asserts exactly the five default services; optional profile smoke commands are separate and appear only once their owning services exist.

Steps intentionally **absent** in Phase 0 and their owning phase: DeepEval LLM eval (Phase 2), Trivy image scan (Phase 4 §4.4 — `pip-audit`/`govulncheck`/`pnpm audit` cover Phase 0's dependency surface), k6 in CI (Phase 0 ships the k6 script and a `make load` target; wiring it as a CI gate needs a deployed target).

Concurrency: `group: ci-${{ github.ref }}`, `cancel-in-progress: true`. All actions pinned to commit SHAs (§7.7).

Monorepo working directories, fixed by the module path (§0.3). Every Go step runs inside `agent/` because that is where `go.mod` for `github.com/parag8487/ForgeOps/agent` lives:

```yaml
# .github/workflows/ci.yml (excerpt) — repository parag8487/ForgeOps
jobs:
  agent:
    defaults:
      run:
        working-directory: agent      # module github.com/parag8487/ForgeOps/agent
    steps:
      - uses: actions/checkout@<sha>
      - uses: actions/setup-go@<sha>
        with:
          go-version: "1.26"
          cache-dependency-path: agent/go.sum
      - run: golangci-lint run ./...
      - run: go test -race -shuffle=on ./...
      - run: go build ./...
```

The `paths-filter` step keys the `agent` job on `agent/**`, the `backend` job on `backend/**`, and the `frontend` job on `frontend/**`. The four reference markdown documents at the repository root match **no** filter, so editing them triggers no build (§0.3).

### 8.4 Pre-commit hooks (`.pre-commit-config.yaml`)

| Hook | Scope | Authority |
|:---|:---|:---|
| `gitleaks` | all files | phases.md 0.1, Research §F20 two-gate approach |
| `ruff` + `ruff-format` | `backend/**/*.py` | phases.md 0.1 |
| `gofmt` (plus `go vet` on changed packages) | `agent/**/*.go` | phases.md 0.1 |
| `prettier` | frontend + markdown + yaml, **minus the four reference documents** | PRD §9 step 0 |
| `end-of-file-fixer`, `trailing-whitespace`, `check-merge-conflict`, `check-yaml`, `check-added-large-files` | all, **minus the four reference documents** | PRD §9 "trailing-whitespace hooks" |

Because the repository root *is* the workspace root (§0.3), the four reference documents sit alongside `README.md` and would otherwise be reformatted by `prettier`, `end-of-file-fixer` and `trailing-whitespace`. A single top-level exclusion keeps them byte-identical:

```yaml
# .pre-commit-config.yaml
exclude: |
  (?x)^(
    AI-Powered-DevOps-Platform-Complete-Technical-Research\.md|
    PRD\.md|
    Tech-Stack-Analysis\.md|
    phases\.md
  )$
```

`gitleaks` is deliberately **not** excluded — a secret committed into a reference document is still a leak, and gitleaks only reads, never rewrites.

Completion criterion "Pre-commit hooks pass on all files" means `pre-commit run --all-files` is clean on a fresh clone — so the initial commit must already be formatted, and CI runs the same command.

---

## 9. Low-Level Design — Notation and Conventions

This is a polyglot monorepo, so the low-level design uses the **real language of each tier** rather than a single pseudocode notation. The user's request names all three explicitly, so no notation choice is being invented:

| Where | Notation | Authority for the language |
|:---|:---|:---|
| §10 Agent | **Go 1.26** | Research §0 (Go 1.26), §A1 |
| §11 Backend | **Python 3.x / FastAPI** | Research §B5, PRD §5 |
| §12 Frontend | **TypeScript / React** | Research §D14, PRD §5 |
| Appendix A (§19) | **Structured pseudocode** (`pascal` blocks) for cross-language algorithms with pre/post-conditions and loop invariants | Cross-cutting algorithms belong to no single runtime |
| Appendix B (§20) | Executable property statements + PBT library mapping | phases.md testing requirements |

Signatures shown are *contracts*, not finished implementations: exported names, parameter and return types, error semantics, and the doc comment that explains why the shape is what it is. Bodies appear only where the body *is* the design — shutdown ordering, environment isolation, the middleware registration inversion, the state machines.

Naming conventions: Go uses `internal/<domain>` with consumer-declared interfaces; Python uses `snake_case` modules under `src/<domain>/`; TypeScript uses `kebab-case` files with `PascalCase` components. Errors are typed and wrapped (`fmt.Errorf("...: %w", err)` in Go, custom exception classes in Python, discriminated unions in TypeScript) — never stringly-typed.

---

## 10. Low-Level Design — Go Agent

### 10.1 Module path, package layout and dependency direction

The module is initialised once, inside `agent/`, with the real repository path (§0.3, decision D-14):

```bash
cd agent
go mod init github.com/parag8487/ForgeOps/agent
go mod edit -go=1.26
```

Every internal import therefore reads `github.com/parag8487/ForgeOps/agent/internal/<pkg>`. The path is nested because the agent lives inside the ForgeOps monorepo rather than in a standalone repository — `go.mod` sits at `agent/go.mod`, not at the repository root, which is what makes `github.com/parag8487/ForgeOps/agent` the correct module path rather than `github.com/parag8487/ForgeOps`.

Dependencies point inward only; `internal/app` and `cmd/agent` are the only packages allowed to know about more than their own collaborators. Construction is staged topologically, with no temporary stubs:

1. independent primitives: `config`, logging, `telemetry`, `fileops`, and the real `scanner` watcher;
2. independent probes/transports as needed: Docker, Kubernetes, connection;
3. packages built on primitives: OpenTofu (`iac`), Git/PR (`git`), and MCP (`mcp`);
4. **only after those concrete constructors exist**, final `internal/app` composition, Cobra root/`doctor` wiring, and thin `cmd/agent/main.go`.

The structural-only `executor`, `validator`, `policy`, and `devtools` directories do not participate in the dependency graph. Compose follows the same rule: the backend/frontend health-gate script is added only after both services exist, and optional `vault`/`tools` services are declared only by their owning implementation tasks.

```mermaid
graph TD
    CMD["cmd/agent"] --> APP["internal/app"]
    APP --> CFG["internal/config"]
    APP --> LOG["internal/logging<br/>zap + redaction"]
    APP --> CONN["internal/connection"]
    APP --> MCP["internal/mcp"]
    APP --> IAC["internal/iac"]
    APP --> GIT["internal/git"]
    APP --> DOCK["internal/docker"]
    APP --> K8S["internal/k8s"]
    APP --> TEL["internal/telemetry"]
    IAC --> TEL
    GIT --> TEL
    MCP --> TEL
    CONN --> TEL
    IAC --> FOPS["internal/fileops"]
    GIT --> FOPS
```

### 10.2 `cmd/agent/main.go` — thin entry point

```go
// SPDX-License-Identifier: Apache-2.0
// Package main is the ForgeOps agent entry point.
// Module: github.com/parag8487/ForgeOps/agent
package main

import (
    "context"
    "errors"
    "fmt"
    "os"
    "os/signal"
    "syscall"

    "github.com/parag8487/ForgeOps/agent/internal/app"
    "github.com/parag8487/ForgeOps/agent/internal/config"
)

// Injected by GoReleaser ldflags.
var (
    version = "dev"
    commit  = "none"
    date    = "unknown"
)

func main() {
    if err := run(); err != nil {
        fmt.Fprintf(os.Stderr, "fatal: %v\n", err)
        os.Exit(1)
    }
}

func run() error {
    // Signal-aware root context: Ctrl-C / SIGTERM cancels ctx, which every
    // subsystem observes. stop() restores default signal handling so a second
    // signal kills a wedged process instead of being swallowed.
    ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
    defer stop()

    cfg, err := config.Load(os.Getenv)
    if err != nil {
        return fmt.Errorf("load config: %w", err)
    }

    a, err := app.New(cfg, app.BuildInfo{Version: version, Commit: commit, Date: date})
    if err != nil {
        return fmt.Errorf("build app: %w", err)
    }
    defer func() {
        if cerr := a.Close(); cerr != nil {
            fmt.Fprintf(os.Stderr, "shutdown: %v\n", cerr)
        }
    }()

    // cobra owns the CLI surface; the constructed App is passed in, never
    // reached for through a global.
    if err := app.NewRootCommand(a).ExecuteContext(ctx); err != nil && !errors.Is(err, context.Canceled) {
        return err
    }
    return nil
}
```

### 10.3 Final constructor-injection composition (`internal/app/app.go`)

This file is implemented only at construction stage 4 (§10.1), after every constructor it calls exists and is tested. No temporary fake collaborator or exported placeholder type is permitted.

```go
// New builds the entire dependency graph explicitly. There is no DI framework:
// no wire codegen, no uber-fx reflection (Research §0 — constructor injection
// is chosen for minimal startup cost and binary size).
func New(cfg *config.Config, bi BuildInfo) (*App, error) {
    logger, err := logging.New(cfg.LogLevel, cfg.LogFormat)
    if err != nil {
        return nil, fmt.Errorf("logger: %w", err)
    }

    tracer := telemetry.NewNoopTracer()                       // Phase 3 swaps in OTel behind this iface
    files  := fileops.New(logger.Named("fileops"))
    tofu   := iac.NewTofuRunner(cfg.Tofu, logger.Named("tofu"), tracer)
    gitc   := git.NewClient(cfg.Git, logger.Named("git"), tracer)
    mcpSrv := mcp.NewServer(mcp.Deps{Logger: logger.Named("mcp"), Tracer: tracer, Tofu: tofu, Files: files}, bi.Version)
    conn   := connection.NewManager(cfg.BackendWSSURL, logger.Named("conn"), tracer)
    docker := dockerx.New(logger.Named("docker"))
    kube   := k8sx.New(logger.Named("k8s"))

    a := &App{cfg: cfg, bi: bi, logger: logger, tracer: tracer,
        files: files, tofu: tofu, git: gitc, mcp: mcpSrv, conn: conn,
        docker: docker, k8s: kube}

    // Closers are registered in construction order; Close() walks them in
    // reverse (property P-07).
    a.closers = []namedCloser{
        {"connection", conn}, {"mcp", mcpSrv}, {"k8s", kube}, {"docker", docker},
        {"logger", loggerCloser{logger}},
    }
    return a, nil
}
```

### 10.4 Graceful shutdown — `signal.NotifyContext` + `errgroup`

```go
// Run starts every long-lived subsystem under a single errgroup bound to ctx.
// The first subsystem to return a non-nil error cancels the group, which
// unblocks the others; Run then returns that first error.
func (a *App) Run(ctx context.Context) error {
    g, gctx := errgroup.WithContext(ctx)

    g.Go(func() error { return a.mcp.Serve(gctx) })   // returns nil on gctx.Done
    g.Go(func() error { return a.conn.Serve(gctx) })  // Phase 0: returns ErrDisabled immediately when no URL

    err := g.Wait()
    if errors.Is(err, context.Canceled) || errors.Is(err, connection.ErrDisabled) {
        err = nil // expected termination paths
    }
    return err
}

// Close shuts subsystems down in reverse construction order, bounded by
// cfg.ShutdownTimeout, and is safe to call more than once.
func (a *App) Close() error {
    a.closeOnce.Do(func() {
        ctx, cancel := context.WithTimeout(context.Background(), a.cfg.ShutdownTimeout)
        defer cancel()
        for i := len(a.closers) - 1; i >= 0; i-- {
            c := a.closers[i]
            if err := c.CloseWithContext(ctx); err != nil {
                a.closeErr = errors.Join(a.closeErr, fmt.Errorf("%s: %w", c.name, err))
            }
        }
    })
    return a.closeErr
}
```

Ordering guarantee, asserted by property **P-07**: start order = construction order; stop order = exact reverse; every started component is closed exactly once; `Close` is idempotent; total shutdown time ≤ `ShutdownTimeout`.

### 10.5 Connection manager seam (`internal/connection`)

Phase 1 owns pairing, mTLS, JWT, heartbeat, reconnect backoff, and the command whitelist (phases.md §1.1). Phase 0 owns only the transport contract and a real `coder/websocket` implementation of it, so the dependency is genuinely exercised and the interface cannot drift.

```go
// Transport is raw framed message transport. It knows nothing about
// authentication, reconnection, heartbeats, or JSON-RPC semantics — those are
// Phase 1 concerns layered above this interface.
type Transport interface {
    Dial(ctx context.Context, url string, hdr http.Header) error
    Send(ctx context.Context, payload []byte) error
    Receive(ctx context.Context) ([]byte, error)
    Close(code websocket.StatusCode, reason string) error
}

// WSSTransport implements Transport over github.com/coder/websocket.
// nhooyr.io/websocket is DEPRECATED and must never be used (Research §0).
type WSSTransport struct { /* conn, logger, limits */ }

func NewWSSTransport(logger *zap.Logger) *WSSTransport

// ErrDisabled is returned by Manager.Serve when no backend URL is configured.
// In Phase 0 this is the normal path: the agent has no backend to talk to yet.
var ErrDisabled = errors.New("connection manager disabled: no backend URL configured")

type Manager struct { /* url, transport, logger, tracer */ }

func NewManager(url string, logger *zap.Logger, tracer telemetry.Tracer) *Manager
func (m *Manager) Serve(ctx context.Context) error
func (m *Manager) Close() error
```

Phase 0 verification: a `httptest` server upgrades a websocket, echoes a frame, and the test asserts round-trip plus clean close on context cancellation. This proves the ISC-licensed `coder/websocket` dependency links on every target without implementing any Phase 1 protocol.

### 10.6 OpenTofu runner (`internal/iac`)

The most safety-sensitive Phase 0 component: it spawns an external process with a timeout, streams its output, propagates signals to the whole process group, and refuses to leak the parent environment.

```go
type TofuConfig struct {
    BinaryPath      string        // default "tofu"
    DefaultTimeout  time.Duration // default 5m
    KillGrace       time.Duration // default 10s between SIGTERM and SIGKILL
    PluginCacheDir  string        // TF_PLUGIN_CACHE_DIR
    ExtraEnvAllow   []string      // additional env keys permitted through
    MaxLineBytes    int           // default 64KiB; longer lines are truncated, not buffered
}

type PlanResult struct {
    ExitCode     int
    HasChanges   bool            // tofu plan exit code 2 with -detailed-exitcode
    PlanJSON     json.RawMessage // from `tofu show -json <planfile>`
    Stdout       []string        // captured tail, bounded
    Stderr       []string
    Duration     time.Duration
}

type Runner interface {
    Validate(ctx context.Context, workdir string) (*ValidateResult, error)
    Plan(ctx context.Context, workdir string, opts PlanOptions) (*PlanResult, error)
}

// LineSink receives each output line as it is produced. Phase 0 wires it to zap;
// Phase 2 wires the same seam to an SSE `log` event stream.
type LineSink func(stream string, line string)
```

**Pinned integration fixture.** `agent/testfixtures/tofu-null/main.tf` declares `hashicorp/null` at exact version **`3.2.3`**. Its committed `.terraform.lock.hcl` includes provider checksums generated for all six supported targets: `linux_amd64`, `linux_arm64`, `darwin_amd64`, `darwin_arm64`, `windows_amd64`, `windows_arm64`. CI runs `tofu init -lockfile=readonly` and a lock freshness/integrity check that regenerates the six-platform lock in an isolated copy and fails on any diff. `TF_PLUGIN_CACHE_DIR` remains passed through the curated environment and is used by local/CI integration tests.

Four mechanisms, each with a cross-platform caveat:

**1. Timeout.** `exec.CommandContext(ctx, ...)` plus an explicit `context.WithTimeout(ctx, cfg.DefaultTimeout)` when the caller supplied no deadline. The runner never runs unbounded.

**2. Output streaming.** `StdoutPipe`/`StderrPipe` consumed by two goroutines using `bufio.Scanner` with an enlarged buffer capped at `MaxLineBytes`; each line is emitted to `LineSink` and logged with `zap.String("stream", ...)`. Both goroutines must finish before `cmd.Wait()` returns to avoid losing the tail.

**3. Signal propagation — the platform-split detail.** `Setpgid` is Unix-only, and the agent must build for Windows (completion criterion). Two build-tagged files:

```go
//go:build !windows
// internal/iac/procattr_unix.go

func setProcessGroup(cmd *exec.Cmd) {
    // New process group so we can signal tofu AND every provider plugin it
    // spawned; signalling only the parent orphans the children.
    cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
}

func terminateGroup(cmd *exec.Cmd, grace time.Duration) {
    pgid := -cmd.Process.Pid          // negative pid == the whole group
    _ = syscall.Kill(pgid, syscall.SIGTERM)
    if waitExit(cmd, grace) { return }
    _ = syscall.Kill(pgid, syscall.SIGKILL)
}
```

```go
//go:build windows
// internal/iac/procattr_windows.go

func setProcessGroup(cmd *exec.Cmd) {
    // Windows has no process groups in the POSIX sense. CREATE_NEW_PROCESS_GROUP
    // makes the child ignore Ctrl-C aimed at our console; the tree is torn down
    // explicitly in terminateGroup.
    cmd.SysProcAttr = &syscall.SysProcAttr{CreationFlags: windows.CREATE_NEW_PROCESS_GROUP}
}

func terminateGroup(cmd *exec.Cmd, grace time.Duration) {
    // taskkill /T kills the process tree. A Job Object would be stricter and is
    // recorded as a Phase 1 hardening item (see OQ-6).
    _ = exec.Command("taskkill", "/PID", strconv.Itoa(cmd.Process.Pid), "/T", "/F").Run()
}
```

**4. Environment isolation.** The child never inherits `os.Environ()`. `buildEnv` constructs an allowlist:

```go
// buildEnv returns the ONLY environment the tofu subprocess sees. Anything not
// on the allowlist is dropped, so provider credentials, LLM API keys, and CI
// secrets present in the agent's environment cannot leak into a plan, a log
// line, or a provider call (NFR-10, PRD §2.2 invariant 5).
func buildEnv(cfg TofuConfig, workdir string) []string {
    allow := []string{"PATH", "HOME", "USERPROFILE", "TMPDIR", "TEMP", "TMP", "SystemRoot", "ComSpec"}
    allow = append(allow, cfg.ExtraEnvAllow...)

    env := make([]string, 0, len(allow)+6)
    for _, k := range allow {
        if v, ok := os.LookupEnv(k); ok {
            env = append(env, k+"="+v)
        }
    }
    return append(env,
        "TF_IN_AUTOMATION=1",        // suppress CLI suggestions meant for humans
        "TF_INPUT=0",                // never block on an interactive prompt
        "NO_COLOR=1",                // keep captured lines parseable
        "TF_CLI_ARGS=",              // refuse inherited implicit CLI args
        "TF_PLUGIN_CACHE_DIR="+cfg.PluginCacheDir,
        "TF_DATA_DIR="+filepath.Join(workdir, ".terraform"),
    )
}
```

Property **P-12** asserts no key outside the allowlist ever reaches the child.

### 10.7 Git and PR client (`internal/git`)

`phases.md` 0.6 requires a Git client and a PR flow. No authoritative document names a Go Git library, so the choice is a design decision — **settled by D-5** (§17.1):

| Concern | Library | Version | Licence | Why |
|:---|:---|:---|:---|:---|
| Local Git operations — branch, stage, commit, push | **`github.com/go-git/go-git/v5`** | `v5.13.x` (pinned) | Apache-2.0 | Pure Go. Shelling out to a `git` executable would break the single-static-binary property that the whole agent design rests on, and would make behaviour depend on whatever `git` the user happens to have |
| PR creation + PR review-status polling | **`github.com/google/go-github`** | `v68.x` (pinned) | BSD-3-Clause | Maintained typed client for the GitHub REST API; `phases.md` §0.6 needs `POST /repos/{o}/{r}/pulls` and the review/mergeable read path, both of which are hand-rolled misery otherwise |

Both are permissively licensed, so neither constrains the two-licence layout (§2.4) — they link into the Apache-2.0 `agent/` subtree cleanly. Versions are pinned exactly in `go.mod`; §16.1 carries the inventory rows.

Construction follows the same constructor-injection rule as everything else in the agent (§7.5) — the libraries are wrapped, never leaked past this package:

```go
// Client is implemented by gitClient, which composes both libraries behind one
// interface so callers never import go-git or go-github directly. That keeps the
// PR-provider choice replaceable (GitLab, Gitea) without touching call sites.
func NewClient(cfg config.GitConfig, logger *zap.Logger, tracer telemetry.Tracer) Client

type gitClient struct {
    repos  *git.Repository        // github.com/go-git/go-git/v5 — local operations
    gh     *github.Client         // github.com/google/go-github  — PR REST API
    tokens TokenSource
    logger *zap.Logger
    tracer telemetry.Tracer
}
```

```go
type ChangeSet struct {
    BaseBranch string
    Branch     string   // e.g. "forgeops/chore-scaffold-20260726T120000Z"
    Paths      []string // repo-relative, must resolve inside the repo root
    Message    string
    Author     Signature
}

type Client interface {
    CreateBranch(ctx context.Context, repo string, base, branch string) error
    CommitPaths(ctx context.Context, repo string, cs ChangeSet) (Commit, error)
    Push(ctx context.Context, repo, branch string) error
    OpenPullRequest(ctx context.Context, req PullRequestRequest) (PullRequest, error)
    PullRequestStatus(ctx context.Context, owner, name string, number int) (PRStatus, error)
}

type PRStatus struct {
    Number         int
    State          string // "open" | "closed" | "merged"
    ReviewDecision string // "approved" | "changes_requested" | "review_required" | "pending"
    Mergeable      *bool
    HeadSHA        string
    UpdatedAt      time.Time
}

// TokenSource is the auth seam, and it stays intact under D-5: choosing go-github
// does not choose an auth mechanism. Phase 0 ships EnvTokenSource only.
// PRD §6 mandates a GitHub App eventually; App installation-token minting is an
// auth concern and therefore Phase 1 (see §15.2 and OQ-7). Phase 1 adds
// AppInstallationTokenSource behind this same interface — go-github accepts any
// http.Client, so the swap does not reach the call sites.
type TokenSource interface {
    Token(ctx context.Context) (string, error)
}

type EnvTokenSource struct{ EnvVar string } // e.g. GITHUB_TOKEN
```

Polling contract: `PollUntil(ctx, owner, name, number, interval, timeout)` polls with a fixed interval, honours `ctx`, treats `approved`/`changes_requested`/`closed`/`merged` as terminal, returns the last observed status on timeout, and respects HTTP 403 rate-limit responses by surfacing a typed `ErrRateLimited` with the reset time rather than hammering the API.

### 10.8 Go MCP server template (`internal/mcp`)

```go
type Deps struct {
    Logger *zap.Logger
    Tracer telemetry.Tracer
    Tofu   iac.Runner
    Files  fileops.Ops
}

// NewServer builds an MCP server over github.com/mark3labs/mcp-go exposing the
// Phase 0 tool set. New tools are added by appending a registration here; the
// transport and middleware are fixed.
func NewServer(d Deps, version string) *Server

// Serve runs stdio or HTTP/SSE transport depending on cfg, returning nil when
// ctx is cancelled.
func (s *Server) Serve(ctx context.Context) error
```

Phase 0 tool set — deliberately non-mutating, deliberately minimal:

| Tool | Input | Output | Why it exists in Phase 0 |
|:---|:---|:---|:---|
| `agent.health` | none | version, commit, uptime, platform | Proves `tools/list` + `tools/call` end-to-end |
| `agent.tofu.validate` | `workdir` | diagnostics | Proves the runner over MCP; read-only |
| `agent.tofu.plan` | `workdir`, `vars` | plan JSON, `has_changes` | Feeds the Plan Analyzer with real input; **plan only, never apply** |

Every handler wraps its work with trace-context extraction from MCP metadata, a per-call timeout, and structured logging. No tool in Phase 0 mutates state — `apply`, Docker, and K8s tools are Phase 1/2.

### 10.9 Dependency-exercise policy

Each `phases.md` §0.2 dependency is pinned and proven, never merely declared:

| Dependency | Phase 0 exercise | Feature behaviour added |
|:---|:---|:---|
| `coder/websocket` | round-trip test against `httptest` upgrade | none |
| `docker/docker/client` | `agent doctor` → `Ping` + server version | none |
| `k8s.io/client-go` | `agent doctor` → current kubecontext + server version | none |
| `go.uber.org/zap` | logger construction + level/format tests | none |
| `spf13/cobra` | `run`, `doctor`, `version`, `mcp serve` | none |
| `fsnotify/fsnotify` | `Watcher` iface + temp-dir create/modify/delete test | none |
| `minio/selfupdate` | unit test verifying a signed fixture against an embedded test public key | none — no self-replacement |
| `sergi/go-diff` | `fileops.UnifiedDiff(a, b)` pure function + tests | none |
| `mark3labs/mcp-go` | the MCP server template (§10.8) — a real 0.5 deliverable | in scope |
| `go-git/go-git/v5` | branch → stage → commit → push against a `httptest`-served bare repo fixture | in scope (0.6) |
| `google/go-github` | PR create + status poll against a recorded-response test server; rate-limit path asserted | in scope (0.6) |
| `golang.org/x/sync` | `errgroup` in `App.Run` | in scope |

`tree-sitter/go-tree-sitter` is **absent from this table by decision D-1** (§17.1): it is not in `go.mod` in Phase 0, so there is nothing to exercise. It joins the inventory in Phase 1 §1.3 alongside the CGO cross-compilation strategy that makes it viable.

The dependency exercise is accompanied by a licence/NOTICE audit. The audit records each linked dependency's licence and whether its upstream distribution carries a NOTICE that Apache-2.0 requires ForgeOps to reproduce. Required notices are appended to the complete `agent/NOTICE`; a verified empty result is captured as release evidence. The SBOM remains the exhaustive machine-readable inventory (§2.4).

### 10.10 Atomic file operations (`internal/fileops`)

Needed in Phase 0 because the scaffolding itself writes files and because PRD §2.2 invariants 6 and 7 (backup-before-mutate, atomic change-sets) are foundation, not feature.

```go
type Ops interface {
    // ApplyAtomic writes every entry or none. For each target it first writes a
    // timestamped backup, then writes to a temp file in the same directory,
    // fsyncs, and renames over the target. On any error it rolls every already-
    // renamed target back from its backup.
    ApplyAtomic(ctx context.Context, root string, entries []WriteEntry) (*ApplyReport, error)

    UnifiedDiff(before, after, label string) string
}

type WriteEntry struct {
    RelPath string
    Content []byte
    Mode    os.FileMode
}
```

Guarded by property **P-08**. Path handling: every `RelPath` is cleaned and verified to resolve inside `root` after symlink evaluation, and Phase 0 already enforces the PRD §2.2 blocklist (`~/.ssh`, `~/.aws`, `.env`, `*.pem`) at this layer so no later caller can bypass it.

---

## 11. Low-Level Design — Python Backend

### 11.1 Application factory, lifespan, middleware

```python
# backend/src/main.py
from contextlib import asynccontextmanager, suppress
from fastapi import FastAPI

@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings)
    log = logging.getLogger(__name__)

    # Construction validates local configuration but performs no mandatory
    # network handshake. pool_pre_ping/lazy clients reconnect on first use.
    engine = create_db_engine(settings)
    sessionmaker = create_sessionmaker(engine)           # expire_on_commit=False
    redis = create_redis_client(settings)                # no eager PING
    shared_http = httpx.AsyncClient(timeout=settings.outbound_http_timeout_seconds)

    mcp_registry = McpServerRegistry.from_settings(settings)
    verifier = OidcTokenVerifier(settings, http=shared_http)
    opa = OpaGatewayPolicy(settings.opa_url, http=shared_http)
    tool_cache = TtlToolCache(redis, max_ttl_ms=settings.mcp_cache_max_ttl_ms)
    tasks = RedisTaskStore(redis)
    gateway = McpGateway(mcp_registry, verifier, opa, tool_cache, tasks, tracer=NoopTracer())

    tier_config = load_tier_config(settings)
    keys = build_key_resolver(settings)
    endpoint_registry = EndpointRegistry.from_config(
        tier_config, keys=keys, http=shared_http, tracer=NoopTracer()
    )
    breakers = CircuitBreakerRegistry(settings)
    cache = TieredSemanticCache(redis, settings)
    router = ModelRouter(tier_config, endpoint_registry, breakers, cache)
    limiter = RedisTokenBucketLimiter(redis, settings)

    app.state.settings, app.state.engine, app.state.sessionmaker = settings, engine, sessionmaker
    app.state.redis, app.state.gateway, app.state.router = redis, gateway, router
    app.state.ai_limiter, app.state.dispatcher = limiter, InlineDispatcher()

    # Best-effort initial observations: dependency outage changes readiness, not
    # process liveness. Index creation retries idempotently in the background.
    for name, probe in {"postgres": lambda: verify_database(engine),
                        "redis": lambda: verify_redis(redis)}.items():
        try:
            await asyncio.wait_for(probe(), timeout=2.0)
        except Exception as exc:
            log.warning("dependency unavailable during startup", extra={"dependency": name,
                                                                        "error": redact(exc)})
    index_retry = asyncio.create_task(ensure_semantic_cache_index_with_retry(redis))

    log.info("startup complete", extra={"env": settings.app_env, "version": settings.service_version})
    try:
        yield
    finally:
        index_retry.cancel()
        with suppress(asyncio.CancelledError):
            await index_retry
        await gateway.aclose()
        await shared_http.aclose()
        await redis.aclose()
        await engine.dispose()
        log.info("shutdown complete")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="ForgeOps API",
        version=settings.service_version,
        lifespan=lifespan,
        openapi_url=f"{settings.api_prefix}/openapi.json",
        docs_url=f"{settings.api_prefix}/docs",
        redoc_url=None,
        default_response_class=ORJSONResponse,
    )

    # Starlette PREPENDS middleware, so registration order is the REVERSE of
    # execution order. Registering innermost-first yields the §4.3 stack.
    app.add_middleware(CORSMiddleware, **cors_kwargs(settings))   # executes 5th
    app.add_middleware(AccessLogMiddleware)                        # executes 4th
    app.add_middleware(TraceContextMiddleware)                     # executes 3rd
    app.add_middleware(RequestIdMiddleware)                        # executes 2nd
    # ServerErrorMiddleware is installed by Starlette outermost — executes 1st.

    install_problem_handlers(app)
    app.include_router(health_router)
    app.include_router(v1_router, prefix=settings.api_prefix)
    return app
```

Startup tests inject unreachable Postgres/Redis endpoints and assert lifespan yields successfully, `/health` remains 200, `/health/ready` identifies both failures with RFC 9457 503, and the index retry task does not escape. Invalid settings and local constructor failures still prevent startup.

### 11.2 RFC 9457 exception handlers

```python
# backend/src/core/errors.py
PROBLEM_CONTENT_TYPE = "application/problem+json"
TYPE_BASE = "https://errors.forgeops.dev"   # ForgeOps-owned registry URI; never resolved at runtime

class ProblemDetail(BaseModel):
    type: str
    title: str
    status: int
    detail: str | None = None
    instance: str | None = None
    trace_id: str | None = None
    errors: list[dict[str, str]] | None = None

class ProblemException(Exception):
    def __init__(self, *, status: int, type_suffix: str, title: str,
                 detail: str | None = None, errors: list[dict[str, str]] | None = None):
        self.problem = ProblemDetail(type=f"{TYPE_BASE}/{type_suffix}", title=title,
                                     status=status, detail=detail, errors=errors)

def install_problem_handlers(app: FastAPI) -> None:
    @app.exception_handler(ProblemException)
    async def _problem(request: Request, exc: ProblemException):
        return _render(request, exc.problem)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        return _render(request, ProblemDetail(
            type=f"{TYPE_BASE}/validation-failed", title="Request validation failed", status=422,
            detail="One or more fields failed validation.",
            errors=[{"pointer": "#/" + "/".join(str(p) for p in e["loc"][1:]), "detail": e["msg"]}
                    for e in exc.errors()]))

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException):
        return _render(request, ProblemDetail(
            type=f"{TYPE_BASE}/{slugify(HTTPStatus(exc.status_code).phrase)}",
            title=HTTPStatus(exc.status_code).phrase, status=exc.status_code,
            detail=exc.detail if isinstance(exc.detail, str) else None))

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        logging.getLogger(__name__).exception("unhandled exception")
        # No exception text in the body: it can carry connection strings or keys.
        return _render(request, ProblemDetail(
            type=f"{TYPE_BASE}/internal", title="Internal Server Error", status=500,
            detail="An unexpected error occurred. Quote the trace_id when reporting this."))

def _render(request: Request, problem: ProblemDetail) -> Response:
    problem.instance = request.url.path
    problem.trace_id = current_trace_id()
    return Response(content=problem.model_dump_json(exclude_none=True),
                    status_code=problem.status, media_type=PROBLEM_CONTENT_TYPE)
```

### 11.3 Async session management with `expire_on_commit=False`

```python
# backend/src/core/db.py
def create_db_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(
        str(settings.database_url),          # postgresql+asyncpg://...
        pool_size=settings.database_pool_size,
        max_overflow=5,
        pool_pre_ping=True,
        pool_recycle=1800,
        echo=False,
        # Phase 1 note: behind PgBouncer in transaction mode this needs
        # connect_args={"statement_cache_size": 0} (§6.5).
    )

def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        # MANDATED by Research §0. With the default True, attribute access after
        # commit triggers a lazy refresh — which raises MissingGreenlet in async
        # code and silently breaks response serialisation. Non-negotiable.
        expire_on_commit=False,
        autoflush=False,
    )

async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """Request-scoped session. Commits on success, rolls back on any exception."""
    factory: async_sessionmaker[AsyncSession] = request.app.state.sessionmaker
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
```

### 11.4 MCP Gateway — routing, auth, policy, cache, tracing

```python
# backend/src/mcp/gateway.py
MCP_METHOD_HEADER = "Mcp-Method"
MCP_NAME_HEADER   = "Mcp-Name"

@dataclass(frozen=True)
class Route:
    server: ServerDescriptor
    method: str
    kind: Literal["tools_list", "tools_call", "tasks", "other"]

class HeaderRouter:
    """Routes purely from headers. The JSON-RPC body is NEVER parsed here — that
    is the point of the July 2026 stateless gateway spec (PRD §2.1a) and the
    property that makes routing O(1) and body-independent (P-05)."""

    def route(self, headers: Mapping[str, str]) -> Route:
        method = headers.get(MCP_METHOD_HEADER, "").strip()
        name   = headers.get(MCP_NAME_HEADER, "").strip()
        if not method or not name:
            raise ProblemException(status=400, type_suffix="mcp-missing-routing-headers",
                                   title="Missing MCP routing headers",
                                   detail=f"Both {MCP_METHOD_HEADER} and {MCP_NAME_HEADER} are required.")
        server = self._registry.get(name)
        if server is None:
            raise ProblemException(status=404, type_suffix="mcp-unknown-server",
                                   title="Unknown MCP server", detail=f"No MCP server named '{name}'.")
        return Route(server=server, method=method, kind=_classify(method))
```

```python
# backend/src/mcp/auth.py
class OidcTokenVerifier:
    """OAuth 2.1 / OIDC bearer verification with strict issuer checking.

    RFC 9207 exists to stop authorization-server mix-up attacks; at a multi-tenant
    gateway the enforceable half is: the token's `iss` MUST be in an explicit
    allowlist, `aud` MUST name this gateway, and the signing key MUST come from
    that exact issuer's JWKS. A token signed by issuer A can therefore never be
    replayed against a resource that trusts issuer B.
    """

    async def verify(self, authorization: str | None) -> Claims:
        token = _require_bearer(authorization)                     # 401 otherwise
        unverified = jwt.get_unverified_claims(token)
        issuer = unverified.get("iss")
        if issuer not in self._allowed_issuers:                    # exact match, no prefix logic
            raise ProblemException(status=401, type_suffix="mcp-untrusted-issuer",
                                   title="Untrusted token issuer",
                                   detail="The token issuer is not in the configured allowlist.")
        key = await self._jwks(issuer).key_for(jwt.get_unverified_header(token)["kid"])
        claims = jwt.decode(token, key, algorithms=["RS256", "ES256"],
                            audience=self._audience, issuer=issuer,
                            options={"require": ["exp", "iat", "iss", "aud"]})
        return Claims(**claims)
```

```python
# backend/src/mcp/gateway.py
class McpGateway:
    async def handle(self, request: Request) -> Response:
        claims = await self.verifier.verify(request.headers.get("Authorization"))
        route = self.header_router.route(request.headers)  # no body argument: P-05

        if route.kind == "tools_list":
            tools = await self.tool_cache.get(route.server.name)
            if tools is None:
                upstream = await self.upstream.list_tools(route, trace=child_trace())
                tools = upstream.tools
                await self.tool_cache.put(route.server.name, tools, upstream.ttl_ms)
            allowed = await self.policy.filter_tools(
                server=route.server.name, tools=tools, claims=claims,
                blast_radius=self.agent_blast_radius,
            )
            return tools_list_response(allowed)

        if route.kind == "tools_call":
            call = parse_tools_call(await request.body())  # only after route is fixed
            metadata = await self.metadata.resolve_local_or_cached(
                route.server, call.tool
            )  # no upstream request and never executes
            await self.policy.authorise_call(
                server=route.server.name, tool=call.tool, metadata=metadata,
                claims=claims, blast_radius=self.agent_blast_radius,
            )
            # SECURITY BOUNDARY: the sole call-dispatch site is after successful
            # authorisation. Every exception/deny path returns before this line.
            return await self.upstream.call_tool(route, call, trace=child_trace())

        return await self.handle_non_tool_method(route, claims, request)
```

The `tools/call` unit/integration harness injects counters around every upstream operation. For every generated policy denial, OPA transport failure, unknown tool, missing metadata, malformed call, or unauthorized bearer, all counters are exactly zero (P-05). A separate allow test proves exactly one call invocation after authorization. There is no shared helper that forwards a tool call before policy.

```python
# backend/src/mcp/policy.py
class OpaGatewayPolicy:
    """Blast-radius tool filtering at the gateway (phases.md 0.5).

    filter_tools is fail-closed: if OPA is unreachable the tool list is empty,
    never unfiltered. A policy engine that fails open is not a policy engine.
    """
    async def filter_tools(self, *, server: str, tools: list[dict], claims: Claims,
                           blast_radius: str) -> list[dict]: ...
    async def authorise_call(self, *, server: str, tool: str, metadata: dict,
                             claims: Claims, blast_radius: str) -> None: ...  # raises 403
```

Starter Rego (`policies/mcp/gateway.rego`), Phase 0 scope — one readable rule set, not a policy library:

```rego
package mcp.gateway

import rego.v1

# Blast radius ordering: read_only < workspace < infrastructure
radius_rank := {"read_only": 0, "workspace": 1, "infrastructure": 2}

# Every tool declares the radius it needs; unknown tools default to the highest.
tool_radius(tool) := r if { r := tool.annotations.blast_radius } else := "infrastructure"

allow_tool(tool, agent_radius) if {
    radius_rank[tool_radius(tool)] <= radius_rank[agent_radius]
}

filter := [t | some t in input.tools; allow_tool(t, input.agent_blast_radius)]

allow if {
    some t in input.tools
    t.name == input.tool
    allow_tool(t, input.agent_blast_radius)
}
```

**Distributed TTL cache**, honouring the server-declared `ttlMs`:

```python
# backend/src/mcp/cache.py
class TtlToolCache:
    """Redis is the sole runtime expiry authority across gateway replicas.

    Runtime state stores the value with Redis PX expiry; it never stores a
    process-monotonic absolute timestamp because values from different processes
    are not comparable. A monotonic clock exists only in the pure test model.
    """

    def _effective_ttl_ms(self, server_ttl_ms: int | None) -> int:
        if server_ttl_ms is None or server_ttl_ms <= 0:
            return 0
        return min(server_ttl_ms, self._max_ttl_ms)

    async def get(self, server: str) -> list[dict] | None:
        key = self._key(server)
        # Atomic Lua/read helper returns a value only when PTTL(key) > 0. Redis
        # may delete lazily, so checking PTTL prevents serving a logically expired
        # value even if bytes have not yet been physically evicted.
        try:
            value, pttl = await self._redis.get_with_pttl(key)
        except RedisError:
            return None  # fail as a cache miss; policy still runs on upstream list
        return decode(value) if value is not None and pttl > 0 else None

    async def put(self, server: str, tools: list[dict], server_ttl_ms: int | None) -> None:
        ttl_ms = self._effective_ttl_ms(server_ttl_ms)
        if ttl_ms > 0:
            try:
                await self._redis.set(self._key(server), encode(tools), px=ttl_ms)
            except RedisError:
                pass  # response remains usable; no local fallback expiry state
```

Redis failure degrades `tools/list` caching to a miss; it never turns an expired value into a hit. Tests use both a pure injected-monotonic-clock reference model and a real Redis integration test covering `SET PX`, `PTTL`, cross-client visibility, no-cache for non-positive TTL, and never-serve-after-expiry (P-06). The monotonic timestamp is never serialized into shared runtime state.

**Trace propagation:** every outbound MCP request carries `traceparent` built from the inbound context with a freshly minted child span-id, plus the inbound `tracestate` verbatim. Responses carry `traceresponse`.

### 11.5 Tasks Extension state machine

```mermaid
stateDiagram-v2
    [*] --> submitted
    submitted --> working
    submitted --> cancelled
    working --> input_required
    working --> completed
    working --> failed
    working --> cancelled
    input_required --> working
    input_required --> cancelled
    completed --> [*]
    failed --> [*]
    cancelled --> [*]
```

```python
# backend/src/mcp/tasks.py
class TaskState(StrEnum):
    SUBMITTED = "submitted"; WORKING = "working"; INPUT_REQUIRED = "input_required"
    COMPLETED = "completed"; FAILED = "failed"; CANCELLED = "cancelled"

TERMINAL: frozenset[TaskState] = frozenset({TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED})

ALLOWED: Mapping[TaskState, frozenset[TaskState]] = {
    TaskState.SUBMITTED:      frozenset({TaskState.WORKING, TaskState.CANCELLED}),
    TaskState.WORKING:        frozenset({TaskState.INPUT_REQUIRED, TaskState.COMPLETED,
                                         TaskState.FAILED, TaskState.CANCELLED}),
    TaskState.INPUT_REQUIRED: frozenset({TaskState.WORKING, TaskState.CANCELLED}),
    TaskState.COMPLETED: frozenset(), TaskState.FAILED: frozenset(), TaskState.CANCELLED: frozenset(),
}

def can_transition(src: TaskState, dst: TaskState) -> bool:
    return dst in ALLOWED[src]

class RedisTaskStore:
    """Redis-backed so the gateway stays stateless and any replica can serve
    tasks/get. Transitions use a WATCH/MULTI compare-and-set: two concurrent
    tasks/update calls cannot both win (P-10)."""
    async def create(self, *, kind: str, owner: str) -> TaskRecord: ...
    async def get(self, task_id: str) -> TaskRecord: ...                  # 404 problem if absent
    async def update(self, task_id: str, dst: TaskState, **fields) -> TaskRecord: ...
    async def cancel(self, task_id: str) -> TaskRecord: ...               # idempotent when terminal
```

`tasks/cancel` on a task already in a terminal state returns that state with HTTP 200 and does **not** error — cancellation is idempotent.

### 11.6 MCP Apps (sandboxed iframe UIs)

Phase 0 delivers the hosting contract, not any app:

- `GET /api/v1/mcp/apps/{name}` returns a descriptor: `{name, title, entry_url, capabilities, csp}`.
- The host page is served with `Content-Security-Policy: default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; frame-ancestors 'self'` and the iframe carries `sandbox="allow-scripts allow-forms"` — deliberately **without** `allow-same-origin`, so the app cannot reach the parent origin's storage or cookies.
- The parent↔app channel is `postMessage` with a fixed envelope `{v: 1, type, requestId, payload}`; the parent validates `event.origin` against the descriptor's origin and drops anything else.
- Phase 0 ships one descriptor for the Go agent's `agent.health` tool purely to prove the plumbing. Approval forms and config editors are Phase 1+.

### 11.7 Model router — 6 tiers, cascade, circuit breaker

```python
# backend/src/ai/routing/tiers.py
class ModelTier(StrEnum):
    HIGH_CODING   = "high_coding"    # GPT-5.6 Sol      — architecture, multi-file generation
    HIGH_ANALYSIS = "high_analysis"  # Claude Fable 5   — deep analytical breadth
    MEDIUM        = "medium"         # Grok 4.5         — agentic coding at ~1/3 frontier cost
    MEDIUM_VALUE  = "medium_value"   # Claude Sonnet 5, DeepSeek V4
    LOW_LOGS      = "low_logs"       # Gemini 3 Flash   — high-throughput log analysis
    SELF_HOSTED   = "self_hosted"    # GLM-5.2, Qwen3-Coder-Next — air-gapped / sensitive
```

Six tiers exactly as Research §0 corrects the original four. Declarative configuration (§13.2) so a tier change is a config edit, not a code change.

#### 11.7.1 Benchmark discipline

Research §0 and §C8 warn that every published SWE-bench figure is self-reported and scaffolding-dependent. The tier config therefore stores **`rank_source`** and **`internal_golden_score`** fields and the router orders candidates by the *internal* score when present. No vendor leaderboard number is ever the tie-breaker, and the config schema has no field for one. The golden dataset itself is Phase 2 (DeepEval in CI); Phase 0 only guarantees the shape cannot encode vendor claims as truth.

#### 11.7.1a Executable endpoint registry and adapter

```python
# backend/src/ai/routing/endpoints.py
class EndpointProtocol(StrEnum):
    OPENAI_COMPATIBLE = "openai_compatible"
    ANTHROPIC_NATIVE = "anthropic_native"
    GOOGLE_NATIVE = "google_native"

@dataclass(frozen=True)
class CompletionRequest:
    model: str
    prompt: str
    params: CompletionParams

@dataclass(frozen=True)
class CompletionResponse:
    content: str
    provider_request_id: str | None
    usage: Usage | None

class ModelEndpoint(Protocol):
    endpoint_id: str
    provider_kind: str
    async def complete(self, request: CompletionRequest, *, trace: TraceContext) -> CompletionResponse: ...

class OpenAICompatibleEndpoint:
    """Production adapter for hosted OpenAI-compatible and self-hosted /v1 APIs.

    Uses injected httpx.AsyncClient; resolves the configured key as SecretStr at
    call time; sends a validated /chat/completions request with an explicit
    timeout and injected traceparent/tracestate; validates choices[0].message.content;
    and maps timeouts, non-2xx and malformed JSON to redacted typed errors.
    Error text and repr never include Authorization, key material, or prompt body.
    """
    endpoint_id: str
    provider_kind: str
    async def complete(self, request: CompletionRequest, *, trace: TraceContext) -> CompletionResponse: ...

@dataclass(frozen=True)
class EndpointAvailability:
    endpoint_id: str
    available: bool
    reason: str | None

class EndpointRegistry:
    @classmethod
    def from_config(cls, config: TierConfig, *, keys: KeyResolver,
                    http: httpx.AsyncClient, tracer: Tracer) -> "EndpointRegistry": ...
    def endpoint(self, endpoint_id: str) -> ModelEndpoint | None: ...
    def availability(self, endpoint_id: str) -> EndpointAvailability: ...
```

Every endpoint descriptor validates `id`, `provider`, `model`, absolute HTTP(S) `base_url`, `protocol`, timeout, and key reference. The registry constructs `OpenAICompatibleEndpoint` only for `openai_compatible`. Placeholder/blank keys from `.env.example` are treated as unavailable credentials, never sent to a vendor. In Phase 0, valid `anthropic_native` and `google_native` descriptors are retained but marked `available=false` with `reason="unsupported_protocol_phase_0"`; the cascade records `skipped_unavailable` and never substitutes a fake adapter. Invalid URLs/config still fail startup.

`POST /api/v1/ai/complete` calls this real registry. If the chain contains no supported/configured endpoint (including missing required key material), it returns the ordinary `EXHAUSTED` routing outcome with attempt reasons. Deterministic local HTTP fixture servers exercise: primary timeout/failure, cross-provider fallback, self-hosted success, trace-header injection, malformed response, redacted provider errors, and full exhaustion. CI needs no vendor network or real API key.

#### 11.7.2 Circuit breaker

```python
# backend/src/ai/routing/breaker.py
class BreakerState(StrEnum):
    CLOSED = "closed"; OPEN = "open"; HALF_OPEN = "half_open"

class CircuitBreaker:
    """Per-endpoint breaker. Thresholds fixed by phases.md 0.9:
    5 failures within a 30s sliding window -> OPEN; after 60s -> HALF_OPEN;
    one probe decides HALF_OPEN -> CLOSED or back to OPEN.

    A sliding window (deque of failure timestamps) is used rather than a plain
    counter so five failures spread over an hour never trip the breaker.
    """
    def __init__(self, endpoint_id: str, *, threshold: int = 5,
                 window_s: float = 30.0, open_s: float = 60.0,
                 clock: Callable[[], float] = time.monotonic) -> None: ...

    def state(self) -> BreakerState: ...       # performs the time-driven OPEN -> HALF_OPEN move
    def allows(self) -> bool: ...              # HALF_OPEN admits at most one in-flight probe
    def record_success(self) -> None: ...
    def record_failure(self) -> None: ...
```

```mermaid
stateDiagram-v2
    [*] --> CLOSED
    CLOSED --> OPEN : 5 failures within 30s window
    OPEN --> HALF_OPEN : 60s elapsed since opening
    HALF_OPEN --> CLOSED : probe succeeds, counters reset
    HALF_OPEN --> OPEN : probe fails, 60s timer restarts
    CLOSED --> CLOSED : success clears nothing, failure appended to window
```

#### 11.7.3 Fallback cascade

Order fixed by phases.md 0.9 and Research §0: **primary → secondary (same tier) → cross-vendor → self-hosted → safe template**.

```python
# backend/src/ai/routing/router.py
class RoutingOutcome(StrEnum):
    OK = "ok"; EXHAUSTED = "exhausted"

@dataclass(frozen=True)
class Attempt:
    endpoint_id: str
    result: Literal["success", "error", "skipped_open_breaker", "skipped_unavailable", "timeout", "malformed_response"]
    latency_ms: int
    reason: str | None = None

@dataclass(frozen=True)
class RoutingResult:
    outcome: RoutingOutcome
    endpoint_id: str | None
    content: str | None
    attempts: tuple[Attempt, ...]
    served_from: Literal["l1", "l2", "provider", None]
    degraded: bool           # true when a fallback below the primary served the request
    staleness_seconds: int | None

class ModelRouter:
    async def complete(self, *, tier: ModelTier, prompt: str, params: CompletionParams) -> RoutingResult:
        """Cache-first, then invoke concrete adapters from EndpointRegistry.
          - each candidate endpoint is considered at most once (P-02);
          - OPEN breakers and unavailable/unsupported protocols are skipped with reasons;
          - only a registry-provided real endpoint can be invoked;
          - the loop terminates in at most len(chain) attempts (P-02);
          - on exhaustion it returns outcome=EXHAUSTED, never raises a provider error.
        """
```

The terminal cascade slot is `TerminalFallback`, which returns a deterministic `RoutingOutcome.EXHAUSTED`. The **Safe Default Template Library** (8 languages × 5 artifact types) that will occupy this slot is Phase 1 §1.5 — Phase 0 fixes the slot's contract so Phase 1 plugs in without touching the router. This is a seam, not a stub: `TerminalFallback` is the correct Phase 0 behaviour, because Phase 0 has no artifact to template.

#### 11.7.4 BYO-Key with Infisical

```python
# backend/src/ai/keys/resolver.py
class KeyResolver(Protocol):
    async def resolve(self, *, tenant_id: str, provider: str) -> SecretStr: ...

class EnvKeyResolver:
    """Development default: reads e.g. LLM_KEY_OPENAI from the environment."""

class InfisicalKeyResolver:
    """Per-tenant BYO-Key from Infisical at path /{tenant_id}/llm/{provider}
    (phases.md 0.9). Tenants arrive in Phase 1; Phase 0 uses the single tenant
    id "default". Values are wrapped in SecretStr and never logged.
    """
```

Infisical is the optional `vault` profile and `EnvKeyResolver` is the default dev path. The Compose service is added only with the `InfisicalKeyResolver` implementation, then verified by a profile-specific integration command; it is never implied by default-profile completion evidence.

#### 11.7.5 Redis-backed atomic rate limiting

```python
# backend/src/ai/rate_limit/redis_bucket.py
@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    remaining: int
    retry_after_seconds: int

class RedisTokenBucketLimiter:
    """Atomic per-(OIDC sub, route) token bucket shared by all replicas."""
    async def consume(self, *, subject: str, route: str, cost: int = 1) -> RateLimitDecision: ...
```

The Redis Lua script takes `capacity`, `refill_per_second`, `now_ms`, and `cost`; loads `(tokens,last_refill_ms)`; refills with `min(capacity, tokens + elapsed*rate)`; conditionally subtracts cost; writes both values and a bounded key TTL; and returns `(allowed, remaining, retry_after_ms)` in one atomic operation. Production time comes from Redis `TIME` inside the script so replicas do not disagree. A pure reference model with an injected clock exists only for deterministic unit/property-style examples.

The `/api/v1/ai/complete` dependency order is fixed:

```text
OIDC verify → require claims.sub → RedisTokenBucketLimiter.consume
            → semantic cache → endpoint registry/router/provider
```

For this costly endpoint `AI_RATE_LIMIT_FAIL_MODE=fail_closed` is the only valid Phase 0 value. Redis transport/script failure yields RFC 9457 `503 .../rate-limit-unavailable`; no cache/provider work occurs. An exhausted bucket yields RFC 9457 `429 .../rate-limit-exceeded` and integer `Retry-After: ceil(seconds until one token)`. Tests compare the Lua result to the injected-clock reference model and use real Redis with concurrent clients to prove total successful consumes never exceed available tokens. This protection does not implement Phase 1 tenant budgets, quotas, billing, or reservations.

### 11.8 Tiered semantic cache

Layers per Research §A0c, with the resilience role from Research §0.

```python
# backend/src/ai/cache/tiered.py
class TieredSemanticCache:
    """L1 exact-match -> L2 semantic (cosine >= 0.95) -> L3 prompt-prefix.

    Precedence is strict: L2 is consulted only on an L1 miss, L3 only on an L2
    miss (P-04). When the provider layer is unreachable, L2 may serve its closest
    entry below threshold with degraded=True and a staleness flag — the cache as
    a resilience layer, not merely a cost optimisation (Research §0).
    """

    def l1_key(self, *, model_id: str, prompt: str, params: CompletionParams) -> str:
        # Canonicalise so semantically identical requests share a key: normalise
        # whitespace, sort params, then SHA-256. Never the raw prompt as a key.
        return "llmc:l1:" + sha256(canonical_json({"m": model_id, "p": normalise_ws(prompt),
                                                   "k": params.canonical()})).hexdigest()

    async def lookup(self, *, model_id: str, prompt: str, params: CompletionParams,
                     allow_stale: bool = False) -> CacheHit | None: ...
    async def store(self, *, model_id: str, prompt: str, params: CompletionParams,
                    response: str, embedding: Sequence[float] | None) -> None: ...
```

L2 uses Redis Vector Search. The index is created idempotently at startup:

```
FT.CREATE idx:llmc:l2 ON HASH PREFIX 1 llmc:l2:
  SCHEMA model TAG
         prompt TEXT NOOFFSETS
         response TEXT NOINDEX
         created_at NUMERIC SORTABLE
         embedding VECTOR HNSW 6 TYPE FLOAT32 DIM 1536 DISTANCE_METRIC COSINE
```

Search: `FT.SEARCH idx:llmc:l2 "(@model:{<id>})=>[KNN 5 @embedding $vec AS score]" ...`, then accept only `1 - score >= 0.95`. Note the dependency: Redis needs the query engine, hence `redis-stack-server` in compose (§13.3).

L3 (prefix) is a registry of stable, reusable context blocks — system prompts and doc snippets — keyed by content hash, so a provider's prompt-prefix caching can be exercised and the local block table stays deduplicated. Phase 0 registers exactly one block: the platform system preamble.

### 11.9 Validation pipeline and Semantic Plan Analyzer

```python
# backend/src/analysis/plan_analyzer/pipeline.py
class Severity(StrEnum):
    INFO = "info"; WARNING = "warning"; ERROR = "error"; FATAL = "fatal"

@dataclass(frozen=True)
class Finding:
    stage: str
    severity: Severity
    code: str
    message: str
    resource: str | None = None

class Stage(Protocol):
    name: str
    async def run(self, doc: PlanDocument, ctx: StageContext) -> list[Finding]: ...

class ValidationPipeline:
    """Ordered stages, short-circuit on FATAL, accumulate everything else.

    Phase 0 stages: Syntax -> Schema -> Semantic. The DryRun stage (delegated to
    the agent: kubectl --dry-run=server, docker compose config, helm template)
    is Phase 1 §1.5 and slots in before Semantic without changing this runner.
    """
    async def run(self, doc: PlanDocument) -> PipelineResult: ...
```

```python
# backend/src/analysis/plan_analyzer/semantic.py
class Action(StrEnum):
    CREATE = "create"; UPDATE = "update"; REPLACE = "replace"; DELETE = "delete"; NOOP = "no-op"

DESTRUCTIVE: frozenset[Action] = frozenset({Action.DELETE, Action.REPLACE})

# Weights are configuration, not magic numbers in code.
ACTION_WEIGHT: Mapping[Action, int] = {
    Action.NOOP: 0, Action.CREATE: 1, Action.UPDATE: 2, Action.REPLACE: 5, Action.DELETE: 8,
}
# Resource classes whose loss is unrecoverable get a multiplier.
CLASS_MULTIPLIER: Mapping[str, int] = {"stateful": 3, "network": 2, "iam": 3, "compute": 1, "unknown": 2}

@dataclass(frozen=True)
class BlastRadius:
    score: int
    destructive_count: int
    affected_resources: int
    stateful_deletions: tuple[str, ...]
    verdict: Literal["allow", "warn", "block"]

class SemanticPlanAnalyzer:
    """Answers 'what will this change actually DO?' — the layer that syntax
    checks and dry-runs both miss (Research §5 #8, §5.1 P0 #3).

    Deterministic and monotone: adding a destructive action can never lower the
    score or soften the verdict (P-11). No LLM is involved — a non-deterministic
    safety gate is not a safety gate.
    """
    def analyse(self, doc: PlanDocument) -> BlastRadius: ...
```

The **approval seam** (phases.md 0.7 "connect validation pipeline to approval workflow"):

```python
# backend/src/analysis/plan_analyzer/approval.py
class ApprovalDecision(StrEnum):
    AUTO_OK = "auto_ok"; REQUIRES_APPROVAL = "requires_approval"; BLOCKED = "blocked"

class ApprovalGate(Protocol):
    async def submit(self, verdict: BlastRadius, ctx: StageContext) -> ApprovalDecision: ...

class ThresholdApprovalGate:
    """Phase 0 implementation: a pure verdict -> decision mapping.
    allow -> AUTO_OK, warn -> REQUIRES_APPROVAL, block -> BLOCKED.

    The Change Approval Center that persists change-sets, renders diffs, and
    collects human approvals is Phase 1 §1.6, and the Governance Control Plane
    that makes this the single mandatory chokepoint is Phase 1 §1.10. Phase 0
    proves the wire exists and carries the right signal.
    """
```

Phase 0 input is a real `tofu show -json` plan produced by the agent's runner (§10.6) and checked in as `agent/testdata/plan-sample.json`, which satisfies the completion criterion "Plan Analyzer returns results for sample input" with genuine data rather than a synthetic fixture.

### 11.10 Python MCP server template

`backend/src/mcp/server_template.py` mounts an MCP server as a FastAPI sub-application, mirroring the Go template's shape so both sides feel the same: a `ToolSpec` list (name, description, JSON Schema input, `blast_radius` annotation, handler), a dispatcher validating input against the schema before invocation, trace-context extraction, per-tool timeout, and structured logging. Phase 0 registers one tool, `platform.health`, returning the same payload as `/health`. This is the template every Phase 1+ backend-hosted tool copies.

---

## 12. Low-Level Design — Frontend

### 12.1 App Router structure

```
frontend/
├── app/
│   ├── layout.tsx              # root: <html>, fonts, ThemeProvider, QueryProvider
│   ├── globals.css             # Tailwind v4 entry + shadcn CSS variables
│   ├── error.tsx               # route-level error boundary, renders ProblemDetails
│   ├── not-found.tsx
│   └── (shell)/
│       ├── layout.tsx          # sidebar + header + <main> composition
│       └── page.tsx            # "/" — shell landing; NO feature content
├── components/
│   ├── ui/                     # shadcn primitives: button, card, sheet, separator,
│   │                           #   dropdown-menu, sonner, form, skeleton
│   ├── layout/
│   │   ├── app-sidebar.tsx     # real Home link to `/`, active state; no placeholders
│   │   ├── app-header.tsx
│   │   └── theme-toggle.tsx
│   └── providers/
│       ├── query-provider.tsx
│       └── theme-provider.tsx
├── lib/
│   ├── api/{client.ts,problem.ts,errors.ts,query-keys.ts}
│   └── env.ts                  # zod-validated NEXT_PUBLIC_* surface
├── stores/ui-store.ts          # Zustand: client-only UI state
├── hooks/
├── e2e/shell.spec.ts           # Playwright
└── load/health.js              # k6
```

Notes: Next.js 16 replaces `middleware.ts` with `proxy.ts` (Tech-Stack §10). Phase 0 has no request-interception need — no auth, no rewrites — so **neither file is created**; Phase 1 adds `proxy.ts` when session handling arrives. The `features/` directory from PRD §8 is tracked by a non-code `README.md` that names Phase 1 as owner; it contains no feature placeholder or importable module (§1.3).

The sidebar navigation is intentionally minimal, not empty: it contains exactly one shell-level item, **Home**, linking to `/`. Future feature links do not appear disabled or as placeholders.

### 12.2 Shell layout composition

```tsx
// app/layout.tsx — root
export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <body className={cn(fontSans.variable, "min-h-dvh bg-background font-sans antialiased")}>
        <ThemeProvider attribute="class" defaultTheme="system" enableSystem disableTransitionOnChange>
          <QueryProvider>
            {children}
            <Toaster />
          </QueryProvider>
        </ThemeProvider>
      </body>
    </html>
  );
}
```

```tsx
// app/(shell)/layout.tsx — the only UI Phase 0 ships
export default function ShellLayout({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex min-h-dvh">
      {/* Landmarks and skip-link are not optional: accessibility is cheaper to
          build in at scaffold time than to retrofit over four phases. */}
      <a href="#main" className="sr-only focus:not-sr-only focus:absolute focus:p-2">
        Skip to main content
      </a>
      <AppSidebar />
      <div className="flex min-w-0 flex-1 flex-col">
        <AppHeader />
        <main id="main" tabIndex={-1} className="flex-1 overflow-y-auto p-6">
          {children}
        </main>
      </div>
    </div>
  );
}
```

`AppSidebar` renders `<nav aria-label="Primary"><a href="/" aria-current={pathname === "/" ? "page" : undefined}>Home</a></nav>` (or the equivalent Next `Link`). The link is reachable and activatable by keyboard, has a visible focus state, and exposes its active state via both `aria-current="page"` and styling. No disabled future-feature items are rendered.

Accessibility baseline fixed now: one `<h1>` per route, `<nav aria-label="Primary">`, focus-visible rings retained from the shadcn defaults, theme toggle is a real `<button>` with `aria-label` and announces state, sidebar collapse is keyboard operable, and the Playwright spec asserts the skip-link, Home link, active state, keyboard activation, and landmark structure.

### 12.3 RFC 9457-aware API client

```typescript
// lib/api/problem.ts
export interface ProblemDetails {
  type: string;
  title: string;
  status: number;
  detail?: string;
  instance?: string;
  trace_id?: string;
  errors?: Array<{ pointer: string; detail: string }>;
}

export const PROBLEM_CONTENT_TYPE = "application/problem+json";

/** Narrow an unknown body to ProblemDetails without trusting the server blindly. */
export function isProblemDetails(v: unknown): v is ProblemDetails {
  return (
    typeof v === "object" && v !== null &&
    typeof (v as ProblemDetails).type === "string" &&
    typeof (v as ProblemDetails).title === "string" &&
    typeof (v as ProblemDetails).status === "number"
  );
}
```

```typescript
// lib/api/errors.ts
export class ApiProblemError extends Error {
  constructor(readonly problem: ProblemDetails) {
    super(`${problem.title} (${problem.status})`);
    this.name = "ApiProblemError";
  }
  /** Field-level messages keyed by JSON pointer, for React Hook Form setError. */
  get fieldErrors(): Record<string, string> {
    return Object.fromEntries((this.problem.errors ?? []).map((e) => [e.pointer.replace(/^#\//, ""), e.detail]));
  }
}

/** Thrown when the network fails or the response is unparseable — synthesised
 *  into the same Problem shape so callers only ever handle one error type. */
export class ApiTransportError extends ApiProblemError {}
```

```typescript
// lib/api/client.ts
async function request<T>(path: string, init: RequestInit & { timeoutMs?: number } = {}): Promise<T> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), init.timeoutMs ?? 30_000);
  let res: Response;
  try {
    res = await fetch(`${env.NEXT_PUBLIC_API_BASE_URL}${path}`, {
      ...init,
      signal: controller.signal,
      headers: { Accept: "application/json", ...(init.body ? { "Content-Type": "application/json" } : {}), ...init.headers },
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
  get: <T>(p: string, i?: RequestInit) => request<T>(p, { ...i, method: "GET" }),
  post: <T>(p: string, body?: unknown, i?: RequestInit) =>
    request<T>(p, { ...i, method: "POST", body: body === undefined ? undefined : JSON.stringify(body) }),
};
```

### 12.4 TanStack Query vs Zustand boundaries

One rule, enforced by review and by a lint boundary on imports:

| State kind | Owner | Examples (Phase 0) | Never |
|:---|:---|:---|:---|
| Server-derived / remote | **TanStack Query** | health status, MCP server registry, model tiers | Never copied into Zustand |
| Client-only ephemeral UI | **Zustand** | sidebar collapsed, command palette open, active shell tab | Never holds server data |
| URL-derived | **App Router** (`searchParams`, path) | selected project id (Phase 1) | Never duplicated in either store |
| Form state | **React Hook Form** | any form (Phase 1+) | Never mirrored in Zustand |

```typescript
// stores/ui-store.ts
interface UiState {
  sidebarCollapsed: boolean;
  commandPaletteOpen: boolean;
  toggleSidebar: () => void;
  setCommandPaletteOpen: (open: boolean) => void;
}
// No server data. No async. No fetch. If a field would ever come from the API,
// it belongs in TanStack Query instead.
export const useUiStore = create<UiState>()(persist(/* ... */, { name: "ui", version: 1 }));
```

```typescript
// components/providers/query-provider.tsx
const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30_000,
      retry: (failureCount, error) =>
        // 4xx are the caller's fault: retrying is wasted work and hides bugs.
        error instanceof ApiProblemError && error.problem.status >= 400 && error.problem.status < 500
          ? false
          : failureCount < 2,
    },
  },
});
```

Query keys are centralised in `lib/api/query-keys.ts` as a typed factory so Phase 1+ invalidation cannot drift into stringly-typed keys.

### 12.5 React Hook Form + Zod standard

Phase 0 ships no forms, but fixes the pattern so all later forms are identical: a Zod schema is the single source of truth, `zodResolver` wires it to RHF, the shadcn `Form` primitives render it, and server-side validation failures are mapped from `ApiProblemError.fieldErrors` onto `setError` by JSON pointer. A single unit test asserts that mapping, which is the part everyone gets wrong.

### 12.6 Build-time public environment contract

`NEXT_PUBLIC_API_BASE_URL` and `NEXT_PUBLIC_APP_NAME` are browser-visible build inputs, not runtime secrets. `frontend/Dockerfile` accepts them as `ARG`, sets them as `ENV` in the builder stage before `pnpm build`, and Compose supplies `build.args` with defaults `http://localhost:8000/api/v1` and `ForgeOps`. Runtime environment values may be retained for diagnostics but cannot change the generated client bundle. A build test supplies a non-default browser URL and asserts the generated API client uses that exact URL; this prevents a green build that accidentally points browsers at `backend:8000` or ignores Compose build arguments.

---

## 13. Configuration Schemas

### 13.1 `.env.example` inventory

Grouped, every variable that any Phase 0 component reads. Secrets carry placeholder values only; the file is committed and `.env` is git-ignored.

```dotenv
# ─── Core ────────────────────────────────────────────────────────────────────
APP_ENV=development                       # development | test | production
LOG_LEVEL=INFO
LOG_FORMAT=console                        # console | json  (json in production)
SERVICE_VERSION=0.0.0
GIT_COMMIT=unknown

# ─── PostgreSQL 17 + pgvector 0.8.5 ─────────────────────────────────────────
POSTGRES_USER=forgeops
POSTGRES_PASSWORD=change-me-locally
POSTGRES_DB=forgeops
POSTGRES_PORT=5432
DATABASE_URL=postgresql+asyncpg://forgeops:change-me-locally@postgres:5432/forgeops
DATABASE_POOL_SIZE=10
# Query-time HNSW recall knob (Research §A0a). Never baked into the index.
PGVECTOR_HNSW_EF_SEARCH=40

# ─── Redis (vector search required for the L2 semantic cache) ───────────────
REDIS_URL=redis://redis:6379/0
REDIS_PORT=6379
REDIS_SEMANTIC_INDEX=idx:llmc:l2

# ─── Backend HTTP ───────────────────────────────────────────────────────────
BACKEND_PORT=8000
FRONTEND_PORT=3000
OPA_PORT=8181
API_PREFIX=/api/v1
CORS_ALLOW_ORIGINS=http://localhost:3000

# ─── MCP Gateway (0.5) ──────────────────────────────────────────────────────
MCP_OIDC_ISSUERS=https://auth.localhost/application/o/forgeops/
MCP_OIDC_AUDIENCE=forgeops-mcp-gateway
MCP_OIDC_JWKS_TTL_SECONDS=600
MCP_CACHE_MAX_TTL_MS=300000               # clamps any server-declared ttlMs
MCP_SERVER_REGISTRY_PATH=config/mcp-servers.yaml
MCP_AGENT_BLAST_RADIUS=read_only          # read_only | workspace | infrastructure
OPA_URL=http://opa:8181

# ─── Model routing (0.9) ────────────────────────────────────────────────────
MODEL_TIER_CONFIG_PATH=config/model-tiers.yaml
CB_FAILURE_THRESHOLD=5                    # phases.md 0.9: 5 failures
CB_WINDOW_SECONDS=30                      # ... within 30s -> OPEN
CB_OPEN_SECONDS=60                        # ... HALF-OPEN after 60s
SEMANTIC_CACHE_THRESHOLD=0.95             # Research §A0c: similarity > 0.95
SEMANTIC_CACHE_TTL_SECONDS=86400
EMBEDDING_MODEL_ID=voyage-code-3
EMBEDDING_DIMS=1536                       # SETTLED (D-2): must equal the pgvector column dimension
AI_RATE_LIMIT_CAPACITY=20                 # per verified OIDC sub + route
AI_RATE_LIMIT_REFILL_PER_SECOND=0.2
AI_RATE_LIMIT_FAIL_MODE=fail_closed       # only supported Phase 0 mode for costly completion
OUTBOUND_HTTP_TIMEOUT_SECONDS=60

# ─── BYO-Key (0.9) ──────────────────────────────────────────────────────────
LLM_KEY_RESOLVER=env                      # env | infisical
LLM_KEY_OPENAI=sk-placeholder
LLM_KEY_ANTHROPIC=sk-ant-placeholder
LLM_KEY_XAI=xai-placeholder
LLM_KEY_GOOGLE=placeholder
LLM_KEY_DEEPSEEK=placeholder
OPENAI_BASE_URL=https://api.openai.com/v1
XAI_BASE_URL=https://api.x.ai/v1
DEEPSEEK_BASE_URL=https://api.deepseek.com/v1
ANTHROPIC_BASE_URL=https://api.anthropic.com
GOOGLE_BASE_URL=https://generativelanguage.googleapis.com
SELF_HOSTED_BASE_URL=http://host.docker.internal:11434/v1   # configured OpenAI-compatible dev endpoint
INFISICAL_URL=http://infisical:8080       # only used when LLM_KEY_RESOLVER=infisical
INFISICAL_CLIENT_ID=placeholder
INFISICAL_CLIENT_SECRET=placeholder
INFISICAL_PROJECT_ID=placeholder

# ─── GitOps (0.6) ───────────────────────────────────────────────────────────
GITHUB_TOKEN=placeholder                  # Phase 0 EnvTokenSource; GitHub App is Phase 1 (OQ-7)
GITHUB_API_BASE_URL=https://api.github.com
GITHUB_REPO=parag8487/ForgeOps            # owner/name for the 0.6 PR flow
GIT_AUTHOR_NAME=forgeops-agent
GIT_AUTHOR_EMAIL=agent@forgeops.invalid
GIT_BRANCH_PREFIX=forgeops/
GIT_PR_POLL_INTERVAL_SECONDS=15
GIT_PR_POLL_TIMEOUT_SECONDS=900

# ─── OpenTofu (0.8) ─────────────────────────────────────────────────────────
TOFU_BINARY=tofu
TOFU_VERSION=1.12.5
TOFU_TIMEOUT_SECONDS=300
TOFU_KILL_GRACE_SECONDS=10
TF_PLUGIN_CACHE_DIR=/var/cache/tofu/plugins

# ─── Agent ──────────────────────────────────────────────────────────────────
AGENT_BACKEND_WSS_URL=                    # EMPTY in Phase 0: connection manager stays disabled
AGENT_SHUTDOWN_TIMEOUT_SECONDS=15
AGENT_MCP_TRANSPORT=stdio                 # stdio | http

# ─── Telemetry (propagation only in Phase 0; OTel SDK is Phase 3) ──────────
TRACE_PROPAGATION_ENABLED=true

# ─── Frontend (NEXT_PUBLIC_* is shipped to the browser: never a secret) ────
NEXT_PUBLIC_API_BASE_URL=http://localhost:8000/api/v1
NEXT_PUBLIC_APP_NAME=ForgeOps
```

### 13.2 Model tier configuration (`config/model-tiers.yaml`)

Declarative so tier order is configuration, while endpoint invocation is concrete. Every endpoint validates an absolute HTTP(S) `base_url` and `protocol: openai_compatible | anthropic_native | google_native`; `key_ref` names a `KeyResolver` lookup and is never the secret itself. There is **no vendor leaderboard score field** (§11.7.1).

```yaml
version: 1
tiers:
  high_coding:
    description: Architecture, multi-file generation
    primary: gpt-5.6-sol
    secondary: claude-fable-5
    cross_vendor: [grok-4.5]
    self_hosted: [glm-5.2]
  high_analysis:
    primary: claude-fable-5
    secondary: gpt-5.6-sol
    cross_vendor: [deepseek-v4]
    self_hosted: [glm-5.2]
  medium:
    primary: grok-4.5
    secondary: claude-sonnet-5
    cross_vendor: [deepseek-v4]
    self_hosted: [qwen3-coder-next]
  medium_value:
    primary: claude-sonnet-5
    secondary: deepseek-v4
    cross_vendor: [grok-4.5]
    self_hosted: [qwen3-coder-next]
  low_logs:
    primary: gemini-3-flash
    secondary: deepseek-v4
    cross_vendor: []
    self_hosted: [deepseek-v4-flash]
  self_hosted:
    primary: qwen3-coder-next
    secondary: glm-5.2
    cross_vendor: []
    self_hosted: []

endpoints:
  # OpenAI-compatible endpoints are executable in Phase 0.
  gpt-5.6-sol:
    provider: openai
    model: gpt-5.6-sol
    protocol: openai_compatible
    base_url: ${OPENAI_BASE_URL}
    key_ref: openai
    timeout_seconds: 60
    rank_source: internal_golden
    internal_golden_score: null
  grok-4.5:
    provider: xai
    model: grok-4.5
    protocol: openai_compatible
    base_url: ${XAI_BASE_URL}
    key_ref: xai
    timeout_seconds: 60
    rank_source: internal_golden
    internal_golden_score: null
  deepseek-v4:
    provider: deepseek
    model: deepseek-v4
    protocol: openai_compatible
    base_url: ${DEEPSEEK_BASE_URL}
    key_ref: deepseek
    timeout_seconds: 60
    rank_source: internal_golden
    internal_golden_score: null
  qwen3-coder-next: &self_hosted_qwen
    provider: self_hosted
    model: qwen3-coder-next
    protocol: openai_compatible
    base_url: ${SELF_HOSTED_BASE_URL}
    key_ref: null
    timeout_seconds: 60
    rank_source: unranked
    internal_golden_score: null
  glm-5.2:
    <<: *self_hosted_qwen
    model: glm-5.2
  deepseek-v4-flash:
    <<: *self_hosted_qwen
    model: deepseek-v4-flash

  # Valid configuration but explicitly unavailable in Phase 0; native codecs
  # arrive in Phase 1 and no fake adapter stands in for them.
  claude-fable-5: &anthropic_native
    provider: anthropic
    model: claude-fable-5
    protocol: anthropic_native
    base_url: ${ANTHROPIC_BASE_URL}
    key_ref: anthropic
    timeout_seconds: 60
    rank_source: internal_golden
    internal_golden_score: null
  claude-sonnet-5:
    <<: *anthropic_native
    model: claude-sonnet-5
  gemini-3-flash:
    provider: google
    model: gemini-3-flash
    protocol: google_native
    base_url: ${GOOGLE_BASE_URL}
    key_ref: google
    timeout_seconds: 60
    rank_source: internal_golden
    internal_golden_score: null
```

The loader expands only the documented `${NAME}` variables, rejects unknown fields/protocols/non-absolute URLs, verifies that every tier reference exists, and reports unsupported native protocols through `EndpointAvailability`. The route test substitutes deterministic local fixture base URLs; production defaults are never contacted in CI.

### 13.3 `docker-compose.yml` service topology

The repository pins **Docker Compose 2.24.7**, whose long-form `env_file.required` is supported. This is the one concrete fresh-clone mechanism—there is no implementation-dependent fallback:

```yaml
name: forgeops

x-service-env: &service-env
  - path: ./.env.example
    required: true
  - path: ./.env
    required: false

services:
  postgres:
    image: pgvector/pgvector:pg17@sha256:<committed-digest>
    env_file: *service-env
    ports: ["127.0.0.1:${POSTGRES_PORT:-5432}:5432"]
    volumes: ["pgdata:/var/lib/postgresql/data"]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U $${POSTGRES_USER} -d $${POSTGRES_DB}"]
      interval: 5s
      timeout: 3s
      retries: 20

  redis:
    image: redis/redis-stack-server:7.4.0-v3@sha256:<committed-digest>
    env_file: *service-env
    ports: ["127.0.0.1:${REDIS_PORT:-6379}:6379"]
    volumes: ["redisdata:/data"]
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 3s
      retries: 20

  opa:
    image: openpolicyagent/opa:1.4.2-rootless@sha256:<committed-digest>
    env_file: *service-env
    command: ["run", "--server", "--addr=0.0.0.0:8181", "--log-level=info", "/policies"]
    volumes: ["./policies:/policies:ro"]
    ports: ["127.0.0.1:${OPA_PORT:-8181}:8181"]
    healthcheck:
      test: ["CMD", "/opa", "version"]
      interval: 10s
      timeout: 3s
      retries: 5

  backend:
    build: { context: ./backend, target: runtime }
    env_file: *service-env
    ports: ["127.0.0.1:${BACKEND_PORT:-8000}:8000"]
    depends_on:
      postgres: { condition: service_started }
      redis:    { condition: service_started }
      opa:      { condition: service_started }
    healthcheck:
      # Liveness only: readiness is polled by scripts/dev-up.sh.
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=2)"]
      interval: 10s
      timeout: 5s
      retries: 10

  frontend:
    build:
      context: ./frontend
      target: runtime
      args:
        NEXT_PUBLIC_API_BASE_URL: ${NEXT_PUBLIC_API_BASE_URL:-http://localhost:8000/api/v1}
        NEXT_PUBLIC_APP_NAME: ${NEXT_PUBLIC_APP_NAME:-ForgeOps}
    env_file: *service-env
    ports: ["127.0.0.1:${FRONTEND_PORT:-3000}:3000"]
    depends_on:
      backend: { condition: service_healthy }

  # Added only by the owning BYO-key task; never part of unprofiled evidence.
  infisical:
    profiles: ["vault"]
    image: infisical/infisical:<exact-version>@sha256:<committed-digest>
    env_file: *service-env
    depends_on:
      postgres: { condition: service_started }
      redis:    { condition: service_started }

  # Added only after agent/Dockerfile:devtools and the tofu fixture exist.
  agent-dev:
    profiles: ["tools"]
    build: { context: ./agent, target: devtools }
    env_file: *service-env
    volumes: ["./agent:/workspace:rw", "tofucache:/var/cache/tofu/plugins"]
    command: ["sleep", "infinity"]

volumes: { pgdata: {}, redisdata: {}, tofucache: {} }
```

`.env.example` supplies container environment values even when `.env` is absent. Compose interpolation in ports/build arguments does not read `env_file`, so every such expression has an explicit safe local default; an optional repository-root `.env` still overrides interpolation in the normal Compose way and is also loaded last into each service. `scripts/init-env.sh` checks `[ -e .env ]` and exits successfully without writing when true; otherwise it enables POSIX noclobber and copies `.env.example` to `.env`, treating a concurrent creator as success. It never truncates, merges, or overwrites an existing file, so repeated calls leave that file byte-identical. `make up: init-env` is the explicit prerequisite, but direct unprofiled Compose startup remains valid on a fresh clone.

**Criterion 4 exact evidence:** `docker compose up -d --wait` starts and waits for exactly the default-profile services `postgres`, `redis`, `opa`, `backend`, `frontend`. Optional evidence is separate: `docker compose --profile vault up -d --wait` and `docker compose --profile tools up -d --wait`, each run only after that profile's owning implementation task. `scripts/dev-up.sh`, added after backend and frontend services exist, runs the default command and then polls `/health/ready`; it is not part of early data-plane scaffolding.

**Frontend build-time environment.** `frontend/Dockerfile` declares `ARG NEXT_PUBLIC_API_BASE_URL` and `ARG NEXT_PUBLIC_APP_NAME`, converts both to `ENV` in the build stage, and only then runs `pnpm build`. Runtime `environment`/`env_file` alone is explicitly insufficient because `NEXT_PUBLIC_*` is inlined. A container-build test inspects/executes the generated client bundle and proves requests use the supplied browser-reachable URL rather than a server-internal hostname or runtime-only value.

### 13.4 `Makefile` target contracts

Every target is `.PHONY`, prints what it is doing, and returns non-zero on any failure. Business logic lives in `scripts/*.sh` so the same commands work on Windows under Git Bash or WSL2 (the Makefile itself needs GNU make + a POSIX shell — documented in `docs/development.md`).

| Target | Contract | Side effects | Idempotent |
|:---|:---|:---|:---:|
| `help` | Default goal. Lists targets from `##` comments | none | yes |
| `bootstrap` | Installs/verifies pinned toolchains including Docker Compose 2.24.7, `pip-tools==7.4.1`, and `pre-commit`; does not silently rewrite locks | tool caches, git hooks | yes |
| `init-env` | Runs `scripts/init-env.sh`: copy `.env.example` to `.env` only when absent; never overwrite or merge an existing `.env` | may create `.env` once | yes |
| `lock-backend` | Regenerates runtime `requirements.lock` and dev `requirements-dev.lock` from `pyproject.toml` with `pip-compile --generate-hashes`; exact documented commands | lockfiles | yes |
| `check-tofu-lock` | Runs `tofu init -lockfile=readonly` and verifies the committed null-provider lock covers six target platforms without drift | provider cache only | yes |
| `build` | `build-agent build-backend build-frontend`. **Completion criterion** | binaries, images, `.next/` | yes |
| `build-agent` | `go build ./...` for the host + `GOOS/GOARCH` matrix on request | `agent/dist/` | yes |
| `build-backend` | Docker installs `requirements.lock` only with `--require-hashes`, then builds the multi-stage image | image tag | yes |
| `build-frontend` | `pnpm install --frozen-lockfile && pnpm build` with explicit `NEXT_PUBLIC_*` build args | `.next/` | yes |
| `test` | `test-agent test-backend test-frontend`; prints coverage. **Completion criterion** | none | yes |
| `test-agent` | `go test -race -shuffle=on ./...` (PRD §9) | none | yes |
| `test-backend` | `pytest` against compose-managed Postgres/Redis | creates + drops a test database | yes |
| `test-frontend` | `vitest --run` (never watch mode) | none | yes |
| `lint` | `golangci-lint run`, `ruff check` + `ruff format --check`, `eslint` + `tsc --noEmit`. **Completion criterion** | none | yes |
| `fmt` | `gofmt -w`, `ruff format`, `prettier --write` | rewrites files | yes |
| `up` / `down` | `up: init-env`, then `scripts/dev-up.sh` (`docker compose up -d --wait` for the default profile + readiness poll); `down` runs unprofiled `docker compose down`. Direct Compose remains fresh-clone-safe | containers, volumes; `.env` created only if absent | yes |
| `logs` | `docker compose logs -f --tail=100` | none | n/a |
| `migrate` | `alembic upgrade head` inside the backend container | schema change | yes |
| `migrate-new` | `alembic revision --autogenerate -m "$(m)"`; fails if `m` unset | new revision file | no |
| `sbom` | Syft CycloneDX SBOM for the agent build. **Completion criterion** | `dist/*.sbom.json` | yes |
| `release-snapshot` | `goreleaser release --snapshot --clean`; validates the release config without publishing | `dist/` | yes |
| `e2e` | Playwright against a built frontend + running backend | browser downloads on first run | yes |
| `load` | k6 smoke against `/health` | none | yes |
| `clean` | Removes `dist/`, `.next/`, `__pycache__`, `.pytest_cache`, coverage. Never touches `.env`, volumes, or lockfiles | deletes build output | yes |
| `verify-release` | `cosign verify-blob` + SBOM presence check on a published artifact. **Completion criterion** | none | yes |

`make build`, `make test`, `make lint` must each succeed for all three components — the first three Phase 0 completion criteria.

---

## 14. Security Considerations

### 14.1 What Phase 0 is responsible for

Phase 0 creates only foundation attack surface, but it fixes the security posture everything later inherits. Eight controls are in scope:

| Control | Implementation | Authority |
|:---|:---|:---|
| Secret scanning gate | Gitleaks in pre-commit **and** in CI, so a bypassed local hook is still caught | phases.md 0.1, Research §F20 |
| Dependency vulnerability gate | `govulncheck` + `go mod verify`, `pip-audit`, `pnpm audit` in CI | phases.md 0.3, PRD §9 |
| Supply-chain custody | GoReleaser → Syft CycloneDX → Cosign keyless → Rekor → SLSA provenance (§8.1) | PRD §9, NFR-13 |
| Subprocess environment isolation | Curated env allowlist for OpenTofu; parent secrets never inherited (§10.6) | NFR-10, PRD §2.2 |
| Path blocklist + atomic writes with backups | Enforced in `internal/fileops`, below every caller (§10.10) | PRD §2.2 invariants 6–8, NFR-11 |
| Fail-closed gateway policy | OPA unavailable ⇒ empty `tools/list`; denied/erroring `tools/call` returns before dispatch and upstream invocation count is zero (P-05) | phases.md 0.5 |
| Per-caller AI rate limit | Redis/Lua atomic token bucket after verified OIDC `sub`, before cache/provider; Redis failure fails closed | Phase 0 protection for exposed routing seam |

### 14.2 Network exposure — explicit warning

`docker-compose.yml` publishes local ports for the backend, frontend, Postgres, Redis and OPA. **Phase 0 does not add the Phase 1 user-authentication system to the general non-MCP API surface.** Most non-MCP routes therefore remain unauthenticated; the one narrow exception is `/api/v1/ai/complete`, which verifies OIDC solely to key the required abuse-protection bucket. Consequences, stated plainly:

- This topology is for **local development on a trusted machine only**. It must never be exposed to a network, and `docs/deployment.md` says so in the first paragraph.
- `CORS_ALLOW_ORIGINS` defaults to `http://localhost:3000` exactly — no wildcard, so a hostile page cannot drive the API from the developer's browser.
- Postgres and Redis publish to `127.0.0.1` bindings in the committed file (`127.0.0.1:5432:5432`) rather than `0.0.0.0`, so a laptop on a café network is not serving a database.
- The authenticated Phase 0 surfaces are `/api/v1/mcp*` and the costly `/api/v1/ai/complete` seam; both verify OIDC bearer tokens. Other non-MCP routes remain unauthenticated by the deliberate Phase 0 decision. Endpoint-specific verification does **not** pull the Phase 1 login/session/RBAC system forward.
- Phase 1 §1.11 adds Authentik/OIDC login, JWT lifecycle, and RBAC across all routes. Until then, no non-local deployment is supported.

### 14.3 Identity and policy roadmap (context — Phase 0 must not block it)

| Concern | Target | Phase | Phase 0 obligation |
|:---|:---|:---|:---|
| Agent workload identity | **SPIFFE/SPIRE X.509-SVID + mTLS**, attested on namespace + service-account + image-digest; no long-lived agent keys; JWT-SVID only across L7 proxies | 1 | The `Transport` interface accepts an `http.Header` and a `*tls.Config`-shaped seam, and **no** Phase 0 code persists or assumes a long-lived agent credential. `EnvTokenSource` exists only for the GitHub PR flow, never for agent identity |
| Agent-side policy evaluation | **OPA compiled to Wasm, embedded in the Go binary** | 1 | `internal/policy/` is only a non-code structural artifact in Phase 0; no Cerbos import, placeholder `Decision`/`Input`, or production policy code exists. Phase 1 creates the real consumer-owned interface with its useful Wasm implementation (Research §B7) |
| Backend app RBAC | Cerbos v0.54.0 sidecar | 1+ | No RBAC decisions are made in Phase 0 code, so none need unwinding |
| K8s admission | Kyverno | 2+ | none |
| Governance Control Plane | One enforced chokepoint: policy → approval → change-set → blast radius → audit → rollback | 1 | The Phase 0 `ApprovalGate` seam (§11.9) is the insertion point, and `fileops.ApplyAtomic` is already the only write path |

### 14.4 Threat notes specific to Phase 0

- **Config poisoning / prompt injection** (Research §H29): no repo file is fed to an LLM in Phase 0, so the vector is absent — but `/api/v1/ai/complete` exists to exercise the router. It is therefore gated behind the same OIDC requirement as the MCP routes and rate-limited per caller, so it cannot become an open LLM proxy on a developer's machine.
- **Log leakage:** the `SecretRedactingFilter` (§7.2) runs before any handler emits, and `tofu` output is streamed through the same logger, so a provider that echoes a credential is redacted at the boundary.
- **Rekor/Fulcio dependency:** keyless signing requires network access to Sigstore at release time. `make verify-release` is the check that custody actually worked; a release that cannot be verified is not a release.

---

## 15. Cross-Document Conflict Resolutions

Each conflict is recorded with the resolution and its consequence for Phase 0.

### 15.1 Phase placement of FR-97, FR-99, FR-100

**Conflict.** PRD §3.17 assigns FR-97 (Rollback Visualization & Release Timeline), FR-99 (Notification Center) and FR-100 (Local Development Tools) to Phase 4. `phases.md` §2.3, §2.6 and §2.8 assign the same capabilities to Phase 2.

**Resolution.** `phases.md` governs. It is the newer, more specific document (PRD is v2.0 dated 24 July 2026; `phases.md` carries the per-phase deliverable checklists that the build follows), and `phases.md` §2.3/§2.6/§2.8 list them as concrete Phase 2 deliverables with completion criteria. The PRD §3.17 rows should be read as "deferred beyond Phase 1", not as a binding phase number.

**Phase 0 consequence.** None. Recorded so the Phase 2 spec does not relitigate it. Recommended follow-up (not a Phase 0 task): annotate PRD §3.17 when that phase is specced.

### 15.2 Authentication in Phase 0 — "excluded" vs the OIDC requirement

**Conflict.** `phases.md` Phase 0 "Excluded" lists **authentication**. Yet §0.5 requires *"OAuth 2.1/OIDC auth with `iss` parameter validation per RFC 9207"* and the completion criteria require *"OAuth 2.1/OIDC issuer validation blocks unauthorized requests"*.

**Resolution — these are two different things, and Phase 0 builds only the second.**

| Built in Phase 0 | Deferred to Phase 1 §1.11 |
|:---|:---|
| **Token verification at the MCP Gateway and `/api/v1/ai/complete`**: parse a bearer JWT, fetch JWKS from the token's issuer, verify signature, enforce `iss` ∈ explicit allowlist, required `aud`, `exp`/`nbf`/`iat`; completion also requires `sub` for the limiter key | An identity provider deployment (Authentik or Keycloak container) |
| Rejection of unauthenticated/untrusted-issuer requests with `401` problem+json | Login flows, redirects, consent, the authorization-code exchange |
| An `iss` allowlist that is **required to be non-empty when `APP_ENV=production`**, so the check cannot be silently disabled | Session and refresh-token lifecycle, token rotation |
| The OIDC configuration surface in `.env.example` | User/team records (`users`, `teams`, `sessions` — deferred with D1, §6.1) |
| — | RBAC and Cerbos integration |
| — | Agent pairing codes and device tokens |
| — | General user authentication/RBAC on the remaining non-MCP routes |

In one sentence: **Phase 0 verifies tokens at the gateway and costly completion seam; Phase 1 issues them and applies user authentication broadly.** A resource server validating a bearer token is not an authentication *system*. The required PRD `src/auth/` path is tracked only by a non-code `README.md`; it is not an importable package and contains no Phase 0 auth placeholder (§1.3).

Consequence: the completion criterion is tested by asserting `401` for (a) no token, (b) a token from an issuer outside the allowlist, (c) an expired token, (d) a token whose `aud` does not name the gateway.

### 15.3 Numbering: §0.9 vs "Phase 0.5"

**Conflict.** The Model Routing Configuration deliverable is numbered **§0.9** in the `phases.md` Phase 0 deliverable list but referred to as **"Phase 0.5"** in the `phases.md` Phase Dependency Graph and in its dependency notes ("P0.5 (Model Routing) is a hard prerequisite for P1.5").

**Resolution.** They are the same deliverable. This spec uses **0.9** throughout, matching the deliverable checklist that the implementation checks off. The dependency note stands: 0.9 is a hard prerequisite for Phase 1 §1.5, which is why it lands in Phase 0 rather than Phase 1.

**Phase 0 consequence.** `PROGRESS.md` uses the `0.9` label with a parenthetical `(a.k.a. "Phase 0.5" in the dependency graph)` so neither reading loses the thread.

### 15.4 D2 version: `0.7+` vs `2.x`

**Conflict.** PRD §5 lists D2 at `0.7+`; the `Tech-Stack-Analysis.md` final table lists D2 at `2.x` and its §17 narrative says D2 "has reached major v2+".

**Resolution.** Not a Phase 0 concern — D2 is first needed by the AI Architecture Diagram Generator (Phase 4 §4.2), and the research §I34/§6 even recommends deferring that feature. Recorded as **OQ-10** to be settled by the phase that adopts it. This design uses **Mermaid** for its own diagrams, which both documents agree is the appropriate fallback/documentation choice.

**Phase 0 consequence.** No D2 dependency is added anywhere.

### 15.5 Missing referenced files

**Conflict.** Research §9 ("AI IDE Build Prompt") instructs the IDE to read four files "in order", the first being the build rules document — conventionally `rules.md`. That file **does not exist in this workspace**. The research document also references `DEEP_RESEARCH_SYNTHESIS.md` in seven places (§0 "New Architecture Patterns" cites it as the source for the MCP Gateway spec, constructor DI, cAST chunking, the fallback cascade, SSE event types, and the two-tier memory model); that file is **also absent**.

**Resolution.** Noted, not fabricated. No content is invented for either file. Where `DEEP_RESEARCH_SYNTHESIS.md` was the *only* cited source for a detail, this design uses whatever the research document itself states inline and marks any remaining gap as an open question rather than inferring the missing detail. Specifically affected: the finer points of the July 2026 MCP Gateway spec beyond PRD §2.1a's seven-row table (**OQ-11**).

**Phase 0 consequence.** Research §9's "read `rules.md` first" instruction cannot be satisfied. If a rules document exists outside this workspace it should be added; otherwise `docs/development.md` becomes the project's build-rules home. Flagged for the user rather than resolved unilaterally.

### 15.6 Project identity — placeholders superseded by the real repository

**Apparent conflict.** `phases.md` §0.2 says `go mod init github.com/org/ai-devops-agent`. PRD §8 draws the monorepo root as `ai-devops-platform/`. Neither matches the other, and neither matches anything that exists.

**Resolution — this is placeholder resolution, not contradiction.** `org` is a literal placeholder; no GitHub organisation is named `org`. `ai-devops-platform` is a descriptive working name for a directory, not an identity claim. The real repository is now **`https://github.com/parag8487/ForgeOps`**, so:

- the project name is **ForgeOps**, superseding PRD §8's `ai-devops-platform/` root directory name;
- the Go module path is **`github.com/parag8487/ForgeOps/agent`**, superseding `phases.md` §0.2's `github.com/org/ai-devops-agent`;
- PRD §8's *internal* structure is unchanged and still authoritative — `agent/`, `backend/`, `frontend/`, `docs/`, `.github/`, plus root `Makefile`, `docker-compose.yml`, `.env.example`, `.gitignore`, `README.md`, `PROGRESS.md`, `LICENSE`.

Neither reference document is contradicted by this, because neither asserted a real name. Both are read-only and stay unedited (§0.3).

**Phase 0 consequence.** The module path appears in every internal import (§10.2), in `go mod init` (§10.1), in `.goreleaser.yaml` and the `-ldflags -X` injection paths (§8.2), and in the CI workflow's `working-directory` (§8.3). Recorded as decision **D-14** (§17.1).

### 15.7 Deliberate deviation — tree-sitter deferred out of the Phase 0 dependency list

**Conflict.** `phases.md` §0.2 lists `github.com/tree-sitter/go-tree-sitter` among the core dependencies to add in Phase 0. The same phase's completion criteria require the Go binary to compile for Windows, macOS and Linux on amd64 and arm64 — six targets, which GoReleaser produces with `CGO_ENABLED=0` (§8.2). `go-tree-sitter` requires CGO. The two requirements cannot both be satisfied without adopting a CGO cross-compilation toolchain (zig cc or `xx`) in Phase 0.

**Resolution.** The completion criterion wins; the dependency moves to Phase 1 §1.3. This is a **deliberate, reasoned deviation** from `phases.md` §0.2's dependency list, recorded as decision **D-1** (§17.1) rather than silently absorbed. It is justified by a direct conflict with a Phase 0 completion criterion in the same document — where a deliverable list and a completion criterion disagree inside `phases.md` itself, the criterion is the testable statement and therefore governs.

**What is *not* deferred.** `internal/scanner` is still created in Phase 0 with its `Watcher` interface and a real `fsnotify` implementation, exercised by a temp-directory create/modify/delete test (§10.9). The deviation defers a **dependency**, not a **capability**: no Phase 0 deliverable, completion criterion, or seam is weakened, because nothing in Phase 0 parses an AST. Phase 1 §1.3 adds the dependency together with the CGO cross-build strategy at the point where AST parsing and cAST semantic chunking are actually used.

---

## 16. Dependencies and Version Pinning

Pinning discipline per §7.7: the version stated here is the authority-mandated floor; the exact patch (Go/Node) or digest (images) is resolved once at implementation and committed to the lockfile. No floating ranges anywhere.

### 16.1 Go agent

| Dependency | Version | License | Authority | Phase 0 role |
|:---|:---|:---|:---|:---|
| Go toolchain | **1.26** | BSD | Research §0, Tech-Stack §1 | Language |
| `github.com/coder/websocket` | 1.8.x | ISC | Research §0, §A2 | WSS transport. **Replaces the deprecated `nhooyr.io/websocket`** |
| `github.com/docker/docker/client` | 26.x | Apache 2.0 | Research §A2 | `doctor` ping probe |
| `k8s.io/client-go` | 0.31.x | Apache 2.0 | Research §A2 | `doctor` context/version probe |
| `go.uber.org/zap` | 1.27.x | MIT | Research §A2 | Structured logging |
| `github.com/spf13/cobra` | 1.8.x | Apache 2.0 | Research §A2 | CLI |
| `github.com/fsnotify/fsnotify` | 1.7.x | BSD | Research §A2 | Watcher seam |
| `github.com/minio/selfupdate` | 0.6.x | MIT | Research §A2 | Signature-verification test only |
| `github.com/sergi/go-diff` | 1.3.x | MIT | Research §A2 | `UnifiedDiff` |
| `github.com/mark3labs/mcp-go` | 0.15.x | MIT | PRD §5, Research §0 | MCP server template |
| `golang.org/x/sync` | latest tagged | BSD | Implied by phases.md 0.2 `errgroup` | Shutdown orchestration |
| `github.com/go-git/go-git/v5` | **v5.13.x** (pinned) | Apache-2.0 | **No authoritative document names a Go Git library — settled by decision D-5 (§17.1)** | Local Git operations for 0.6: branch, stage, commit, push. Chosen to preserve the single-static-binary property instead of shelling out to a `git` executable |
| `github.com/google/go-github` | **v68.x** (pinned) | BSD-3-Clause | **No authoritative document names a GitHub client — settled by decision D-5 (§17.1)**; PRD §6 mandates a GitHub App from Phase 1 | PR creation + PR review-status polling REST calls required by phases.md §0.6. Auth stays behind `TokenSource` (§10.7), so OQ-7's `EnvTokenSource` → `AppInstallationTokenSource` swap is unaffected |
| `pgregory.net/rapid` | 1.x | MPL 2.0 | **No authority — see OQ-4** | Property-based tests |

**Module path:** `github.com/parag8487/ForgeOps/agent`, declared in `agent/go.mod` (§10.1, decision D-14). Uppercase letters are legal in a module path; the proxy and checksum database case-escape them to `github.com/parag8487/!forge!ops/agent` in `$GOMODCACHE` and in `sum.golang.org`. Cosmetic only — do not lowercase the module path, because it must match the real repository path for `go get` to resolve.

**Licence compatibility:** both Git libraries are permissive (Apache-2.0 and BSD-3-Clause), so linking them into the Apache-2.0 `agent/` subtree (§2.4) raises no obligation beyond attribution in `agent/NOTICE`.

**Deferred out of Phase 0 by decision D-1 (§17.1):** `github.com/tree-sitter/go-tree-sitter` (0.10.x, MIT; PRD §5 and phases.md 0.2 both list it). It requires CGO, which conflicts with the `CGO_ENABLED=0` six-target static build (§8.2, §15.7). It is **not** in `go.mod` in Phase 0 and enters in Phase 1 §1.3 together with an explicit CGO cross-compilation strategy (zig cc or `xx`).

Tooling: `golangci-lint` 1.62+ (GPL-3.0, used as a tool, not linked), GoReleaser, Syft, Cosign.

### 16.2 Backend

`backend/pyproject.toml` uses the valid PEP 440 constraint **`requires-python = ">=3.13,<3.14"`** (never the invalid `3.13.*`) and exact `==` direct dependency pins. The concrete compatible Phase 0 pin set is:

| Dependency | Exact version | License | Authority |
|:---|:---|:---|:---|
| Python | `>=3.13,<3.14` | PSF | Phase 0 selected runtime |
| `fastapi` | `==0.139.2` | MIT | Research §0 — native `EventSourceResponse` |
| `uvicorn[standard]` | `==0.34.0` | BSD | FastAPI deployment |
| `sqlmodel` | `==0.0.39` | MIT | Research §0, PRD §5 |
| `sqlalchemy[asyncio]` | `==2.0.38` | MIT | Research §0 |
| `asyncpg` | `==0.30.0` | Apache-2.0 | Async Postgres driver |
| `pgvector` | `==0.3.6` | PostgreSQL | SQLAlchemy `Vector` type |
| `alembic` | `==1.14.1` | MIT | phases.md 0.3 |
| `pydantic` / `pydantic-settings` | `==2.10.6` / `==2.7.1` | MIT | Tech-Stack §3 |
| `redis` | `==5.2.1` | MIT | Cache, tasks, limiter |
| `httpx` | `==0.27.2` | BSD | PRD §5; real model adapter + OPA/OIDC HTTP |
| `orjson` | `==3.10.15` | MIT/Apache-2.0 | Response serialisation |
| `pyjwt[crypto]` | `==2.10.1` | MIT | OIDC verification (OQ-13 recommendation) |
| `pytest` / `pytest-asyncio` / `pytest-cov` | `==8.3.4` / `==0.25.3` / `==6.0.0` | MIT | phases.md 0.3, PRD §5 |
| `hypothesis` | `==6.125.3` | MPL-2.0 | PBT (OQ-4 recommendation) |
| `ruff` | `==0.9.7` | MIT | phases.md 0.3 |
| `pip-audit` | `==2.7.3` | Apache-2.0 | phases.md 0.3 |
| `pip-tools` | **`==7.4.1`** | BSD | Selected lock generator |

`pyproject.toml` is the sole dependency source of truth. `requirements.lock` contains runtime plus transitive hash pins; `requirements-dev.lock` contains runtime + `dev` extra plus transitive hash pins. They are generated with `pip-compile --generate-hashes --output-file=requirements.lock pyproject.toml` and `pip-compile --generate-hashes --extra dev --output-file=requirements-dev.lock pyproject.toml`. Docker installs only the runtime lock with `--require-hashes`; CI installs the dev lock with `--require-hashes`. `make lock-backend` runs both commands, and CI regenerates both and requires a clean diff before tests.

**Deliberately absent:** `sse-starlette` (Research §0 — redundant), `celery` (Research §0 — banned), `arq`/`dramatiq`/`temporalio`/`inngest` (Phase 1/2 behind the §7.9 seam), `opentelemetry-*` (Phase 3), `structlog` (OQ-3), `langchain`/`langgraph`/`llama-index` (Phase 1), and native Anthropic/Google SDKs/codecs (Phase 1; unsupported protocols are data, not stubs).

### 16.3 Frontend

| Dependency | Version | License | Authority |
|:---|:---|:---|:---|
| `next` | 16.x | MIT | Research §D14, PRD §5 |
| `react` / `react-dom` | 19.x | MIT | PRD §5 |
| `typescript` | 5.x | Apache 2.0 | Implied |
| `pnpm` | 10+ | MIT | Research §2, PRD §5 |
| `tailwindcss` | 4.x | MIT | Tech-Stack §13 |
| shadcn/ui + `@radix-ui/*` | latest pinned | MIT | Research §2 |
| `@tanstack/react-query` | 6.x | MIT | Research §2, PRD §5 |
| `zustand` | 5.x | MIT | Research §2 |
| `react-hook-form` + `zod` + `@hookform/resolvers` | latest pinned | MIT | Research §2, Tech-Stack §18 |
| `next-themes` | latest pinned | MIT | Required by the theme toggle; no authority names a library |
| `vitest` + `@testing-library/react` + `jsdom` | 2.x | MIT | phases.md 0.4 |
| `@playwright/test` | 1.50+ | Apache 2.0 | phases.md 0.4 |
| `fast-check` | 3.x | MIT | **No authority — see OQ-4** |
| `eslint` + `prettier` | latest pinned | MIT | phases.md 0.4 |
| k6 | latest | AGPL-3.0 (tool, not linked) | phases.md 0.4 |

**Deliberately absent in Phase 0:** ECharts, xterm.js, React Flow, CodeMirror 6, D2 — all belong to feature surfaces in Phases 1–4.

### 16.4 Container images and external tools

| Image / tool | Version | Notes |
|:---|:---|:---|
| `pgvector/pgvector:pg17` | PostgreSQL 17 + pgvector **0.8.5** | Pin digest. pgvector version verified at boot by a startup assertion |
| `redis/redis-stack-server` | 7.4.x | Query engine required for L2 vector search |
| `openpolicyagent/opa` | 1.x rootless | Gateway policy server |
| OpenTofu | **1.12.5** | Exact pin per PRD §5, Research §E18 |
| `hashicorp/null` fixture provider | **3.2.3** | Exact `required_providers` constraint; committed lock checksums for linux/darwin/windows amd64/arm64; CI `tofu init -lockfile=readonly` |
| Docker Compose | **2.24.7** | Exact developer/CI version; supports long-form optional `env_file.required` used by §13.3 |
| `infisical/infisical` | exact tag + digest when owning task adds profile | `vault` profile only |

### 16.5 Deliberately not introduced in Phase 0

- **Object storage.** No S3-compatible store is needed by any Phase 0 deliverable, so none is added. Recorded because the research corrects an earlier assumption: the `minio/minio` **server** repo is archived, so if object storage is ever needed the choice is SeaweedFS, Garage, or Ceph RGW; the `minio-go` **client** SDK remains fine. Nothing in Phase 0 depends on either.
- **PgBouncer, Cerbos, Authentik, Novu, Infracost, Trivy, Velero, ArgoCD, Argo Rollouts, Cilium, KEDA, CloudNativePG, Grafana stack, LangFuse, DeepEval** — all belong to Phases 1–4 in their own authority rows.
- **`github.com/tree-sitter/go-tree-sitter`** — deferred to Phase 1 §1.3 by decision D-1 (§16.1, §15.7). Listed here so its absence from `go.mod` reads as intentional rather than forgotten.

### 16.6 Project licensing and SBOM implications

The two-licence layout is specified in §2.4; this section records what it means for dependency and supply-chain tooling.

| Component | Licence | SPDX identifier | Where declared |
|:---|:---|:---|:---|
| Repository default (`backend/`, `frontend/`, `policies/`, `scripts/`, `docs/`, root tooling) | Functional Source License 1.1, Apache 2.0 future licence (2-year conversion) | `FSL-1.1-ALv2` | root `LICENSE`; `backend/pyproject.toml` `[project].license`; `frontend/package.json` `"license"` |
| Agent + CLI (`agent/`) | Apache License 2.0 | `Apache-2.0` | `agent/LICENSE`, `agent/NOTICE`, per-file `SPDX-License-Identifier` headers |
| Open-core premium features (NFR-33) | Proprietary | — | **Out of scope for Phase 0.** No third licence file is added now |

Why FSL rather than BSL 1.1, given NFR-32 offers both and Research §H30 leaves it open — the full rationale is in decision **D-19** (§17.1). The short version: Research §E18 rejects Terraform *because* HashiCorp relicensed it under BSL, calling that a wrong-message and long-term-licence-risk problem, and that reasoning is what drove the P0 adoption of OpenTofu. Licensing ForgeOps' own backend under BSL 1.1 would contradict the rationale behind one of the project's own P0 architecture changes. FSL is also fixed-text with a defined two-year Apache 2.0 conversion, whereas BSL 1.1 requires a bespoke per-instance "Additional Use Grant" and "Change Date" — a recurring source of interpretation ambiguity.

Consequences for the supply chain (§8.1):

- **Syft** reads `pyproject.toml`, `package.json` and Go module metadata, so the generated CycloneDX SBOM reports `FSL-1.1-ALv2` for backend/frontend components and `Apache-2.0` for the agent. Without the SPDX declarations it would report `UNKNOWN`, which is worse than either answer.
- `FSL-1.1-ALv2` **is** a registered SPDX licence identifier, so no `LicenseRef-` custom expression is required. The non-registered alias `FSL-1.1-Apache-2.0` must never appear in metadata — it would defeat the point of declaring a licence at all (§2.4).
- No Phase 0 dependency carries a copyleft obligation that reaches ForgeOps' own code: the only GPL/AGPL entries (`golangci-lint`, k6) are invoked as external tools and never linked (§16.1, §16.3).

---

## 17. Decisions and Open Questions

This section has two halves. §17.1 is the decision log: questions that were open, were answered, and whose reasoning must survive because later phases will ask "why is it like this?". §17.2 is what remains genuinely open.

**Status: no open question is blocking.** The five owner decisions D-1, D-2, D-5, D-14 and D-19 remain unchanged. The task-plan review also settled implementation facts that are now normative rather than open: optional-profile treatment (former OQ-8), `pip-tools==7.4.1` with two hash-pinned locks (former OQ-9), and Python `>=3.13,<3.14` (former OQ-12). The twelve questions remaining in §17.2 are non-blocking and do not weaken any corrected contract.

### 17.1 Decision log (ADR-style)

Each entry is an architecture decision with its rationale preserved. Decisions are numbered to match the open question they retire, so a reader following an old reference lands in the right place.

---

#### D-1 — Defer `tree-sitter/go-tree-sitter` to Phase 1

- **Status:** Accepted · **Date:** 2026-07-26 · **Decided by:** project owner (`parag8487`) · **Retires:** OQ-1 (was blocking)
- **Context.** `phases.md` §0.2 lists `github.com/tree-sitter/go-tree-sitter` among the Phase 0 core dependencies. It requires CGO. The Phase 0 completion criteria require the Go binary to compile for Windows, macOS and Linux on amd64 and arm64, which GoReleaser produces with `CGO_ENABLED=0` (§8.2). Adding the dependency in Phase 0 would force a CGO cross-compilation toolchain (zig cc or `xx`) into the foundation phase.
- **Decision.** The dependency is **not** added to `go.mod` in Phase 0. It moves to **Phase 1 §1.3**, together with an explicit CGO cross-compilation strategy, at the point where AST parsing and cAST semantic chunking are actually used. Phase 0 keeps a pure-static six-target build.
- **Rationale.** Where a deliverable list and a completion criterion disagree *inside the same document*, the criterion is the testable statement and therefore governs. Paying for a CGO toolchain in the phase that has no parser to run is cost without benefit, and it would put the most fragile part of the build matrix into the phase whose entire job is to make the build matrix reliable.
- **Scope discipline.** This defers a **dependency**, not a **capability**. `internal/scanner` is still created in Phase 0 with its `Watcher` interface and a real `fsnotify` implementation exercised by a create/modify/delete test — a genuine seam with Phase-0-useful behaviour, not a stub (§1.3). No Phase 0 deliverable or completion criterion loses anything.
- **Consequences.** §1.2 (excluded list), §1.3 (seam rule exception), §8.2 (build matrix), §10.9 (dependency-exercise policy — row removed), §15.7 (recorded as a reasoned deviation), §16.1 and §16.5 (inventory), Appendix E criterion 7.

---

#### D-2 — Phase 0 pgvector column is 1536-d with an HNSW index and a `model_id` provenance column

- **Status:** Accepted · **Date:** 2026-07-26 · **Decided by:** project owner (`parag8487`) · **Retires:** OQ-2 (was blocking)
- **Context.** A pgvector column has exactly one fixed dimension, and Phase 0 must create it in the single initial migration. Voyage Code 3 (the primary API embedding model) is 1536-d; BGE-M3 (the self-hosted option) is 1024-d (Research §C10).
- **Decision.** Fix the column at **1536 dimensions** with an **HNSW** index (`vector_cosine_ops`, `m = 16`, `ef_construction = 64`), and store a **`model_id`** column alongside every vector.
- **Rationale.** 1536 matches the primary model, so the common path needs no truncation or padding. HNSW is mandated by Research §0 and §A0a, which explicitly reject IVFFlat for production vector search — choosing it now avoids a reindex at scale (NFR-29). `model_id` is the cheap insurance: without provenance, a future multi-model store cannot tell which vectors came from which model, and the only recovery is re-embedding everything.
- **Deferred, deliberately.** The multi-model strategy is settled in **Phase 1** — either a second table per dimension, or Matryoshka truncation to a common size. Both remain available precisely because `model_id` exists.
- **Consequences.** §6.2 (`EMBEDDING_DIMS`, `Embedding.model_id`), §6.3 (stated as settled), §11.8 (the L2 Redis vector index uses the same 1536 `DIM`), §13.1 (`EMBEDDING_DIMS`), Appendix E criterion 14. Remains listed as a Phase 1 follow-up, not an open question.

---

#### D-5 — `go-git/go-git/v5` for local Git, `google/go-github` for the PR API

- **Status:** Accepted · **Date:** 2026-07-26 · **Decided by:** project owner (`parag8487`) · **Retires:** OQ-5 (was blocking)
- **Context.** `phases.md` §0.6 requires a Git client in the Go agent plus PR creation and PR review-status polling. **None** of the four authoritative documents names a Go Git library or a GitHub API client.
- **Decision.** `github.com/go-git/go-git/v5` (pinned `v5.13.x`, Apache-2.0) for local repository operations — branch, stage, commit, push. `github.com/google/go-github` (pinned `v68.x`, BSD-3-Clause) for the PR creation and review-status-polling REST calls.
- **Rationale.** go-git is pure Go, so the agent keeps its single-static-binary property; shelling out to a `git` executable would break that invariant and make behaviour depend on whichever `git` the user happens to have installed. go-github supplies typed access to the two REST surfaces §0.6 needs. Both are permissively licensed, so neither complicates the two-licence layout (§2.4) or adds an obligation beyond attribution in `agent/NOTICE`.
- **Boundary preserved.** The `TokenSource` seam is untouched: Phase 0 ships `EnvTokenSource` reading `GITHUB_TOKEN`, and Phase 1 adds `AppInstallationTokenSource` behind the same interface, because PRD §6 mandates a GitHub App. Choosing a client library does not choose an auth mechanism — **OQ-7 stays open and unaffected**.
- **Consequences.** §1.1 (0.6 row), §10.7 (interfaces and wrapper type), §10.9 (two new exercise rows), §16.1 (inventory with pins and licences).

---

#### D-14 — Project identity: ForgeOps, module path `github.com/parag8487/ForgeOps/agent`

- **Status:** Accepted · **Date:** 2026-07-26 · **Decided by:** project owner (`parag8487`) · **Retires:** OQ-14 (was blocking)
- **Context.** `phases.md` §0.2 said `go mod init github.com/org/ai-devops-agent` with a literal `org` placeholder; PRD §8 drew the monorepo root as `ai-devops-platform/`. The real repository now exists: **`https://github.com/parag8487/ForgeOps`**.
- **Decision.** The project is **ForgeOps**. The Go module path is **`github.com/parag8487/ForgeOps/agent`**. PRD §8's internal structure is unchanged and still authoritative; only its root *directory name* is superseded. The monorepo is built directly in the existing workspace root — no nested project directory — with the four reference documents remaining at the top level, read-only and excluded from every lint/format glob.
- **Rationale.** A Go module path is baked into every import and is painful to change after the first release, so it must match the real repository path exactly and be fixed before `go mod init`. The path is nested (`/agent`) because `go.mod` lives at `agent/go.mod` inside the monorepo, not at the repository root.
- **Module path case escaping.** Uppercase letters in a module path are legal. The module proxy and checksum database case-escape them, so the path appears as `github.com/parag8487/!forge!ops/agent` in `$GOMODCACHE` and in `sum.golang.org` lookups. This is cosmetic, works correctly, and is **not** a reason to rename the repository. Do not lowercase the module path — it must match the real repository path or `go get` fails to resolve.
- **Consequences.** §0.3, §2.1/§2.3 (diagrams and tree), §2.4, §8.2 (GoReleaser, `-ldflags -X` paths), §8.3 (CI `working-directory`), §10.1 (`go mod init`), §10.2 (imports), §11.1 (API title), §11.2 (`TYPE_BASE`), §13.1 (env inventory), §13.3 (compose project name), §15.6, §16.1.

---

#### D-19 — Licensing: `FSL-1.1-ALv2` for the repository, `Apache-2.0` for the agent and CLI

- **Status:** Accepted · **Date:** 2026-07-26 · **Decided by:** project owner (`parag8487`) · **Retires:** OQ-19 (was blocking)
- **Context.** NFR-31 licenses the agent and CLI under Apache 2.0 — unambiguous. NFR-32 licenses the backend platform under "FSL (Fair Source) **or** BSL 1.1" without choosing, and Research §H30 leaves the choice open. Phase 0 must commit a root `LICENSE` before the repository is published.
- **Decision.** Backend platform and repository default: the **FSL 1.1 variant whose future licence is Apache 2.0**, with the two-year conversion. Agent and CLI: **`Apache-2.0`**, committed immediately per NFR-31. Layout, SPDX identifiers and README wording are specified in §2.4; SBOM implications in §16.6.
- **Identifier correction, recorded deliberately.** The licence was requested as `FSL-1.1-Apache-2.0`. That string is a descriptive alias, **not** a registered SPDX identifier. The registered identifier for exactly this licence — FSL 1.1 with an Apache 2.0 future licence — is **`FSL-1.1-ALv2`**, and the licence's own name is the *Functional* Source License (Fair Source is the category it belongs to, not its name). Metadata therefore uses `FSL-1.1-ALv2` so Syft, SPDX validators and dependency scanners resolve it instead of reporting `UNKNOWN`. **The licence chosen is unchanged** — only the identifier string is corrected (§2.4).
- **Rationale — the deciding argument is internal consistency.** Research §E18 rejects Terraform *specifically because* HashiCorp relicensed it under BSL: using Terraform "sends the wrong message and creates long-term license risk". That reasoning is what drove the P0 decision to adopt OpenTofu. Licensing ForgeOps' own backend under BSL 1.1 would contradict the rationale behind one of the project's own P0 architecture changes — the project would be rejecting a dependency for a licence it had itself adopted.
- **Rationale — secondary.** FSL is a materially shorter and less ambiguous document. BSL 1.1 requires the licensor to author a bespoke "Additional Use Grant" and "Change Date" per instance, which is a recurring source of interpretation ambiguity; FSL's terms are fixed, with a defined two-year Apache 2.0 conversion.
- **Out of scope.** NFR-33's proprietary premium features are not part of Phase 0. No third licence file is added now.
- **Residual consideration, non-blocking, for the user.** FSL is **not** an OSI-approved open-source licence. The research documents' repeated framing of ForgeOps as "fully open-source" is therefore strictly accurate only for the Apache-2.0 agent and CLI. Recommendation: `README.md` should describe the backend as **"source-available, converting to Apache 2.0 after two years"** rather than "open source", to avoid an accuracy problem in the project's own marketing copy. This is a documentation-wording item, not a licensing change, and it does not reopen the decision.
- **Consequences.** §1.1 (0.1 row), §2.3 (tree), §2.4 (layout, SPDX, README wording), §16.6 (SBOM licence reporting).

---

### 17.2 Open questions (12 remaining — none blocking)

These are places where the four authoritative documents are silent, ambiguous, or internally inconsistent for Phase 0. Each carries a recommendation that this design already implements, so none of them gates the start of a task; they are listed so the choice stays visible and reversible rather than buried. Confirm or override at leisure.

| # | Question | Why it matters | Recommendation | Blocking? |
|:---|:---|:---|:---|:---|
| **OQ-3** | No authority names a Python logging library. | Choosing `structlog` adds an unsanctioned dependency; choosing stdlib costs a little ergonomics. | **stdlib `logging` + `dictConfig` + a JSON formatter.** Revisit only if it becomes painful. | No |
| **OQ-4** | No authority names property-based testing libraries, yet the correctness properties in Appendix B need them. | Three new dev dependencies across three ecosystems. | `hypothesis` (Python), `pgregory.net/rapid` (Go), `fast-check` (TypeScript) — all permissively licensed, all standard in their ecosystems. | No, but confirm before Appendix B is implemented |
| **OQ-6** | Windows process-tree termination for the OpenTofu runner. `Setpgid` is Unix-only; `taskkill /T /F` is a pragmatic equivalent but weaker than a Windows Job Object. | A leaked provider plugin process on Windows holds state locks. | Ship `taskkill` in Phase 0 and record Job Objects as a Phase 1 hardening item. | No |
| **OQ-7** | PRD §6 mandates a **GitHub App** (short-lived installation tokens), but registering and minting App tokens is auth-adjacent work that Phase 0 excludes. | Determines whether Phase 0's PR flow uses a PAT. | Phase 0 uses `EnvTokenSource` reading `GITHUB_TOKEN`; Phase 1 adds `AppInstallationTokenSource` behind the same interface. Confirm this is acceptable for the 0.6 completion check. | No |
| **OQ-10** | D2 version: `0.7+` (PRD §5) vs `2.x` (Tech-Stack final table). | Only matters from Phase 4 §4.2. | Leave open; resolve in the phase that adopts D2. Phase 0 adds no D2 dependency. | No |
| **OQ-11** | `DEEP_RESEARCH_SYNTHESIS.md` is cited seven times in Research §0 and is not in this workspace. | Details beyond the available documents and this review correction cannot be checked against that source. | The task-plan review's explicit gateway ordering and other corrections are authoritative and implemented here; for any remaining unspecified detail, use PRD §2.1a/phases.md 0.5 only unless the missing file is supplied. | No |
| **OQ-13** | No authority names a JWT/JWKS library for the gateway's OIDC verification. | Required by 0.5. | `pyjwt[crypto]` with a small JWKS cache keyed by issuer. `python-jose` is less actively maintained. | No |
| **OQ-15** | Should the initial migration include the nullable `tenant_id` seam (§6.5), or stay strictly minimal? | Adding it now avoids a Phase 1 backfill; omitting it keeps Phase 0 literally minimal. | Include it, nullable, with **no RLS policies**. It is a foundation decision, not a feature. Flagged in case strict minimality is preferred. | No |
| **OQ-16** | The durable engine choice (Temporal vs Inngest) is deliberately deferred to the P2 boundary, but `phases.md` Appendix A and §2.4a name **Inngest** while Research §0/§B6 and the Tech-Stack table lean **Temporal or Inngest**. | Not a Phase 0 decision, but the §7.9 interface must be neutral enough for either. | Keep `TaskDispatcher` neutral: no engine-specific concepts (no workflow ids, no signal/query semantics) leak into the Protocol. Decide at P2. | No |
| **OQ-17** | Is the backend's >70 % coverage figure a Phase 0 **gate** or a **goal**? `phases.md` 0.3 says "coverage >70% goal"; the Phase 0 completion criteria do not list coverage, while Phase 1 does. | A hard gate on scaffolding encourages tests that assert nothing. | Treat it as a reported goal in Phase 0 and a CI gate from Phase 1. | No |
| **OQ-18** | Research §9 instructs the IDE to read a build-rules document (conventionally `rules.md`) that does not exist in this workspace. | The build prompt's first instruction cannot be followed. | Supply the file, or designate `docs/development.md` as the rules home. Not fabricated here. | No |
| **OQ-20** | The gateway filters tools by **agent blast radius**, but agents do not exist until Phase 1. Where does the blast-radius value come from in Phase 0? | Determines whether the OPA input is real or configured. | Phase 0 reads it from `MCP_AGENT_BLAST_RADIUS` (default `read_only`); Phase 1 derives it from the attested agent identity. The Rego policy is written against the input field, so no policy change is needed later. | No |

---

## 18. Progress Tracking Artifact — `PROGRESS.md`

A root `PROGRESS.md` is a **Phase 0 deliverable** and the project's durable progress record. It is updated in the same commit as the work it describes, so a fresh clone always answers "where are we?" without reading history.

Required structure:

```markdown
# PROGRESS

**Current phase:** Phase 0 — Foundation & Project Scaffolding
**Last updated:** YYYY-MM-DD

## Phase status
| Phase | Name | Status |
|:---|:---|:---|
| 0 | Foundation & Project Scaffolding | in-progress |
| 1 | MVP Core — Analysis, Generation, Approval | not-started |
| 2 | Deploy, Manage & Command | not-started |
| 3 | Observe, Troubleshoot & Self-Heal | not-started |
| 4 | Scale, Collaborate & Polish | not-started |
| 5 | Advanced & Ecosystem | not-started |

## Current phase task list — Phase 0
| # | Deliverable group | Task | Status |
|:---|:---|:---|:---|
| 0.1 | Repository Structure | monorepo layout per PRD §8 | done |
| 0.1 | Repository Structure | pre-commit: gitleaks + ruff + gofmt | in-progress |
| ... | ... | ... | pending |
| 0.9 | Model Routing (a.k.a. "Phase 0.5" in the dependency graph) | circuit breaker | pending |

## Completion criteria — Phase 0
| Criterion | Status | Evidence |
|:---|:---|:---|
| `make build` succeeds for all three components | pending | — |
| ... | ... | ... |

## Open questions requiring a decision
| # | Question | Blocking | Status |
|:---|:---|:---|:---|
| OQ-4 | property-based testing libraries (hypothesis / rapid / fast-check) | no | recommendation implemented, awaiting confirmation |
| OQ-13 | JWT/JWKS library choice | no | PyJWT recommendation implemented, awaiting confirmation |

## Decision log
| Date | Decision | Rationale | Authority |
|:---|:---|:---|:---|
| 2026-07-26 | D-14 — project is ForgeOps; Go module `github.com/parag8487/ForgeOps/agent` | real repo supersedes the `org` / `ai-devops-platform` placeholders; module path must match the repo exactly | design §17.1 D-14, §15.6 |
| 2026-07-26 | D-19 — repo licence `FSL-1.1-ALv2`; `agent/` `Apache-2.0` | BSL 1.1 would contradict the §E18 reasoning that drove the OpenTofu switch; FSL is fixed-text with a 2-year Apache conversion. `FSL-1.1-ALv2` is the registered SPDX id for that licence | design §17.1 D-19, NFR-31/32 |
| 2026-07-26 | D-1 — defer `tree-sitter/go-tree-sitter` to Phase 1 §1.3 | CGO conflicts with the `CGO_ENABLED=0` six-target build criterion; defers a dependency, not a capability | design §17.1 D-1, §15.7 |
| 2026-07-26 | D-2 — pgvector column 1536-d, HNSW, `model_id` provenance | matches Voyage Code 3; HNSW mandated by Research §0/§A0a; provenance keeps the Phase 1 multi-model options open | design §17.1 D-2 |
| 2026-07-26 | D-5 — `go-git/go-git/v5` + `google/go-github` for 0.6 | preserves the single-static-binary property; both permissively licensed; `TokenSource` seam untouched | design §17.1 D-5 |
```

Rules: exactly three task statuses — `done`, `in-progress`, `pending`; exactly four phase statuses — `completed`, `in-progress`, `not-started`, `blocked`. Every Phase 0 deliverable from `phases.md` appears as at least one row, and every completion criterion appears with an evidence column (a command, a CI run, or an artifact path). Resolved open questions move to the decision log rather than being deleted, so the reasoning survives — the five rows above are the initial content, mirroring design §17.1.

---

## Appendix A (§19) — Algorithmic Pseudocode with Formal Specifications

Cross-language algorithms, specified with preconditions, postconditions and loop invariants. These are the statements Appendix B turns into property-based tests.

### A.1 Circuit breaker state evaluation

```pascal
ALGORITHM CircuitBreakerState(cb, now)
INPUT:  cb — breaker with fields: state, failures (ordered timestamps),
             opened_at, half_open_in_flight, threshold, window_s, open_s
        now — monotonic time
OUTPUT: state ∈ {CLOSED, OPEN, HALF_OPEN}

BEGIN
  ASSERT cb.threshold ≥ 1 AND cb.window_s > 0 AND cb.open_s > 0
  ASSERT monotonic_nondecreasing(cb.failures)

  IF cb.state = OPEN THEN
    IF now - cb.opened_at ≥ cb.open_s THEN
      cb.state ← HALF_OPEN
      cb.half_open_in_flight ← 0
    END IF
  END IF

  RETURN cb.state
END

ALGORITHM RecordFailure(cb, now)
BEGIN
  // Prune outside the sliding window FIRST so old failures can never trip the
  // breaker; a plain counter would eventually open on unrelated failures.
  WHILE cb.failures ≠ ∅ AND now - first(cb.failures) > cb.window_s DO
    INVARIANT ∀ t ∈ cb.failures : t ≤ now
    remove_first(cb.failures)
  END WHILE

  append(cb.failures, now)

  IF cb.state = HALF_OPEN THEN
    cb.state ← OPEN                  // a failed probe re-opens immediately
    cb.opened_at ← now
  ELSE IF cb.state = CLOSED AND |cb.failures| ≥ cb.threshold THEN
    cb.state ← OPEN
    cb.opened_at ← now
  END IF
END

ALGORITHM RecordSuccess(cb, now)
BEGIN
  IF cb.state = HALF_OPEN THEN
    cb.state ← CLOSED
    cb.failures ← ∅                  // full reset: the endpoint has recovered
  END IF
  cb.half_open_in_flight ← 0
END

ALGORITHM Allows(cb, now)
BEGIN
  s ← CircuitBreakerState(cb, now)
  IF s = CLOSED THEN RETURN true END IF
  IF s = OPEN   THEN RETURN false END IF
  // HALF_OPEN admits exactly one probe at a time.
  IF cb.half_open_in_flight = 0 THEN
    cb.half_open_in_flight ← 1
    RETURN true
  END IF
  RETURN false
END
```

**Preconditions:** `threshold ≥ 1`, `window_s > 0`, `open_s > 0`; `now` comes from a monotonic clock; `failures` is non-decreasing.
**Postconditions:** state ∈ {CLOSED, OPEN, HALF_OPEN}; `state = OPEN ⇒ opened_at ≤ now`; the only transition into CLOSED is a HALF_OPEN success; the only transition into HALF_OPEN is elapsed cooldown.
**Loop invariant (prune loop):** every timestamp remaining in `failures` lies within `(now − window_s, now]`, and the loop strictly decreases `|failures|` so it terminates.

### A.2 Fallback cascade over concrete endpoints

```pascal
ALGORITHM RouteWithCascade(tier_cfg, endpoint_registry, breakers, request)
INPUT:  tier_cfg — ordered endpoint ids {primary, secondary, cross_vendor[], self_hosted[]}
        endpoint_registry — id → CONCRETE ModelEndpoint | UNAVAILABLE(reason)
        breakers — endpoint_id → CircuitBreaker
OUTPUT: RoutingResult

BEGIN
  chain ← dedupe([tier_cfg.primary] ++ [tier_cfg.secondary]
                 ++ tier_cfg.cross_vendor ++ tier_cfg.self_hosted)
  ASSERT no_duplicates(chain)

  attempts ← []
  i ← 0

  WHILE i < |chain| DO
    INVARIANT |attempts| = i
    INVARIANT distinct(map(attempts, endpoint_id))
    INVARIANT every prior concrete invocation followed chain order

    endpoint_id ← chain[i]
    resolved ← endpoint_registry.Resolve(endpoint_id)

    IF resolved = UNAVAILABLE(reason) THEN
      append(attempts, Attempt(endpoint_id, SKIPPED_UNAVAILABLE, 0, reason))
    ELSE IF NOT Allows(breakers[endpoint_id], now()) THEN
      append(attempts, Attempt(endpoint_id, SKIPPED_OPEN_BREAKER, 0))
    ELSE
      // complete enforces its own timeout and maps malformed/non-2xx failures to
      // redacted typed results. It is a real adapter, never a fake native codec.
      r ← resolved.endpoint.complete(request, ChildTrace())
      IF r = SUCCESS THEN
        RecordSuccess(breakers[endpoint_id], now())
        append(attempts, Attempt(endpoint_id, SUCCESS, elapsed))
        RETURN RoutingResult(OK, endpoint_id, r.content, attempts,
                             degraded ← (i > 0))
      ELSE
        RecordFailure(breakers[endpoint_id], now())
        append(attempts, Attempt(endpoint_id, Classify(r), elapsed, Redact(r.reason)))
      END IF
    END IF

    i ← i + 1
  END WHILE

  RETURN RoutingResult(EXHAUSTED, ∅, ∅, attempts, degraded ← true)
END
```

**Preconditions:** `chain` is finite/duplicate-free; every id has a breaker and registry availability record; every concrete endpoint enforces a finite timeout.
**Postconditions:** terminates after considering at most `|chain|` ids; every concrete endpoint is invoked at most once and in order; unavailable/unsupported endpoints are recorded but never invoked; `OK` contains non-empty endpoint/content; no supported/configured endpoint yields deterministic `EXHAUSTED`; provider exceptions, keys and prompt bodies never escape.
**Loop invariant:** attempts correspond exactly to the first `i` distinct ids; only registry-provided concrete endpoints may have a non-skip result; `i` strictly increases.

### A.3 Tiered semantic cache lookup

```pascal
ALGORITHM CacheLookup(cache, model_id, prompt, params, provider_available)
OUTPUT: CacheHit(served_from, content, degraded, staleness_s) | MISS

BEGIN
  ASSERT cache.threshold ∈ [0, 1]

  // L1 — exact match. Strictly first: an exact hit must never be overridden by
  // a semantic near-match.
  k1 ← L1Key(model_id, normalise(prompt), canonical(params))
  v1 ← redis_get(k1)
  IF v1 ≠ ∅ THEN
    RETURN CacheHit(L1, v1.content, degraded ← false, staleness_s ← age(v1))
  END IF

  // L2 — semantic. Only reached on an L1 miss.
  e ← embed(prompt)
  IF e ≠ ∅ THEN
    best ← vector_knn(cache.index, model_id, e, k ← 5)
    IF best ≠ ∅ AND best.similarity ≥ cache.threshold THEN
      RETURN CacheHit(L2, best.content, degraded ← false, staleness_s ← age(best))
    END IF

    // Resilience path (Research §0): during a provider outage serve the closest
    // entry BELOW threshold, explicitly flagged as stale. Never silently.
    IF NOT provider_available AND best ≠ ∅ THEN
      RETURN CacheHit(L2, best.content, degraded ← true, staleness_s ← age(best))
    END IF
  END IF

  // L3 — prefix cache does not answer a request; it only shortens the prompt
  // sent to the provider, so a miss here is still a MISS.
  RETURN MISS
END
```

**Preconditions:** `threshold ∈ [0,1]`; `normalise` and `canonical` are deterministic and idempotent; the L2 index dimension equals `|embed(prompt)|`.
**Postconditions:** if an L1 entry exists, `served_from = L1`; L2 is consulted only after an L1 miss; a result with `similarity < threshold` is returned only when `provider_available = false`, and then always with `degraded = true`; `staleness_s ≥ 0` on every hit.

### A.4 Graceful shutdown ordering

```pascal
ALGORITHM Shutdown(components, timeout)
INPUT:  components — list in CONSTRUCTION order
OUTPUT: aggregated error or ∅

BEGIN
  ASSERT ∀ c ∈ components : c.started ⇒ NOT c.closed

  deadline ← now() + timeout
  errors ← []
  i ← |components| - 1

  WHILE i ≥ 0 DO
    INVARIANT ∀ j > i : components[j].closed = true
    INVARIANT ∀ j ≤ i : components[j].closed = false

    c ← components[i]
    remaining ← max(0, deadline - now())
    e ← c.Close(remaining)
    c.closed ← true
    IF e ≠ ∅ THEN append(errors, wrap(c.name, e)) END IF
    i ← i - 1
  END WHILE

  ASSERT ∀ c ∈ components : c.closed = true
  RETURN join(errors)
END
```

**Preconditions:** `components` is in construction order; no component has been closed; `timeout > 0`.
**Postconditions:** every component is closed exactly once, in exact reverse construction order; a failure in one close does not prevent the others; total elapsed time ≤ `timeout` plus the last component's unavoidable overshoot; calling `Shutdown` again is a no-op.
**Loop invariant:** all components with index > i are closed and all with index ≤ i are not; `i` strictly decreases.

### A.5 Atomic change-set application

```pascal
ALGORITHM ApplyAtomic(root, entries)
INPUT:  root — absolute directory; entries — list of (rel_path, content, mode)
OUTPUT: ApplyReport | error, with NO partial application

BEGIN
  // 1. Validate every path before touching the filesystem.
  FOR each e IN entries DO
    abs ← resolve_symlinks(join(root, clean(e.rel_path)))
    ASSERT is_within(abs, root)                      // no traversal escape
    ASSERT NOT matches_blocklist(abs)                // ~/.ssh, ~/.aws, .env, *.pem
  END FOR

  backups ← []
  written ← []

  TRY
    FOR each e IN entries DO
      INVARIANT |backups| = |written|
      INVARIANT ∀ w ∈ written : backup_exists_for(w)

      IF exists(abs(e)) THEN
        b ← copy_to_timestamped_backup(abs(e))       // backup BEFORE mutate
        append(backups, b)
      ELSE
        append(backups, NO_PREVIOUS)
      END IF

      tmp ← write_temp_in_same_dir(abs(e), e.content, e.mode)
      fsync(tmp)
      rename(tmp, abs(e))                            // atomic within a filesystem
      fsync_dir(dirname(abs(e)))
      append(written, abs(e))
    END FOR

    RETURN ApplyReport(written, backups)

  CATCH err
    // 2. Roll back in reverse order. Every written file is restored from its
    // backup, or deleted if it did not previously exist.
    FOR i FROM |written| - 1 DOWNTO 0 DO
      IF backups[i] = NO_PREVIOUS THEN delete(written[i])
      ELSE restore(backups[i], written[i])
      END IF
    END FOR
    RETURN error(err)
  END TRY
END
```

**Preconditions:** `root` exists and is a directory; `rel_path` values are unique after cleaning; the process can write to `root`.
**Postconditions:** either every entry is present with its new content and a backup exists for every pre-existing target, or **no** target differs from its pre-image; applying the identical change-set twice yields the same final content (idempotent in content, though it produces a second backup); no file outside `root` is ever touched.
**Loop invariant:** `|backups| = |written|`, and every path in `written` has a corresponding recoverable pre-image.

### A.6 Distributed TTL tool cache

```pascal
ALGORITHM TtlPut(redis, server, tools, server_ttl_ms, max_ttl_ms)
BEGIN
  ASSERT max_ttl_ms ≥ 0
  IF server_ttl_ms = ∅ OR server_ttl_ms ≤ 0 THEN RETURN END IF
  ttl ← min(server_ttl_ms, max_ttl_ms)
  IF ttl ≤ 0 THEN RETURN END IF
  Redis.SET(CacheKey(server), Encode(tools), PX = ttl)
END

ALGORITHM TtlGet(redis, server)
BEGIN
  // One atomic Redis operation/Lua script observes value and server-side PTTL.
  (value, pttl) ← Redis.GET_WITH_PTTL(CacheKey(server))
  IF value = ∅ OR pttl ≤ 0 THEN
    RETURN MISS
  END IF
  RETURN Decode(value)
END
```

**Preconditions:** `max_ttl_ms ≥ 0`; Redis is the shared runtime authority.
**Postconditions:** effective TTL = `min(server_ttl, max_ttl)`; non-positive TTL is never cached; a value is returned only while Redis reports `PTTL > 0`; after Redis reports expiry/missing, that entry is never served; no process-monotonic absolute expiry is serialized or compared across replicas. A pure reference model may use an injected monotonic clock in tests only.

### A.7 Blast-radius computation

```pascal
ALGORITHM ComputeBlastRadius(plan, action_weight, class_multiplier, thresholds)
INPUT:  plan — normalised list of (resource_id, resource_class, action)
OUTPUT: BlastRadius(score, destructive_count, affected, stateful_deletions, verdict)

BEGIN
  ASSERT ∀ a ∈ plan : a.action ∈ {CREATE, UPDATE, REPLACE, DELETE, NOOP}
  ASSERT ∀ w ∈ action_weight : w ≥ 0
  ASSERT ∀ m ∈ class_multiplier : m ≥ 1

  score ← 0 ; destructive ← 0 ; affected ← 0 ; stateful_deletes ← []

  FOR each a IN plan DO
    INVARIANT score ≥ 0 AND destructive ≥ 0 AND destructive ≤ affected + 1
    IF a.action = NOOP THEN CONTINUE END IF

    affected ← affected + 1
    w ← action_weight[a.action]
    m ← class_multiplier[a.resource_class]        // defaults to the highest on unknown
    score ← score + (w × m)

    IF a.action ∈ {DELETE, REPLACE} THEN
      destructive ← destructive + 1
      IF a.resource_class = STATEFUL THEN append(stateful_deletes, a.resource_id) END IF
    END IF
  END FOR

  // Verdict is a pure, monotone function of the accumulated evidence.
  IF stateful_deletes ≠ ∅ OR score ≥ thresholds.block THEN verdict ← BLOCK
  ELSE IF destructive > 0 OR score ≥ thresholds.warn  THEN verdict ← WARN
  ELSE                                                     verdict ← ALLOW
  END IF

  RETURN BlastRadius(score, destructive, affected, stateful_deletes, verdict)
END
```

**Preconditions:** plan is normalised; all weights ≥ 0; all multipliers ≥ 1; `thresholds.warn ≤ thresholds.block`.
**Postconditions:** deterministic — identical input yields identical output, with no LLM involvement; `score ≥ 0`; `destructive_count ≤ affected_resources`; **monotone** — appending any destructive action never decreases `score` and never softens `verdict` (ALLOW → WARN → BLOCK is one-way); any stateful deletion forces BLOCK.
**Loop invariant:** `score` is non-decreasing, `destructive ≤ affected`.

### A.8 MCP header routing

```pascal
ALGORITHM Route(registry, headers)
INPUT:  headers — case-insensitive map; body is NOT an input
OUTPUT: Route(server, method, kind) | error

BEGIN
  method ← trim(headers["Mcp-Method"])
  name   ← trim(headers["Mcp-Name"])

  IF method = "" OR name = "" THEN
    RETURN error(400, "mcp-missing-routing-headers")
  END IF

  server ← registry[name]
  IF server = ∅ THEN RETURN error(404, "mcp-unknown-server") END IF

  kind ← CASE method OF
           "tools/list"                                   : TOOLS_LIST
           "tools/call"                                   : TOOLS_CALL
           "tasks/get", "tasks/update", "tasks/cancel"    : TASKS
           OTHERWISE                                      : OTHER
         END CASE

  RETURN Route(server, method, kind)
END
```

**Preconditions:** `registry` is immutable for the duration of the request; header lookup is case-insensitive per HTTP.
**Postconditions:** the result is a pure function of `(Mcp-Name, Mcp-Method)` and the registry — the request body cannot influence it; unknown server names never fall through to a default; the function has no side effects.

### A.9 MCP gateway policy ordering

```pascal
ALGORITHM HandleToolsCall(request)
BEGIN
  claims ← VerifyBearerOIDC(request.Authorization)
  route ← Route(registry, request.headers)          // body-independent
  ASSERT route.kind = TOOLS_CALL

  call ← ParseToolsCall(request.body)               // route already fixed
  metadata ← ResolveLocalOrCachedMetadata(route.server, call.tool) // no upstream I/O
  decision ← OPA.AuthoriseCall(route.server, call.tool, metadata, claims)

  IF decision ≠ ALLOW THEN
    ASSERT UpstreamInvocationCount(request.id) = 0
    RETURN Problem(403, "mcp-call-denied")
  END IF

  response ← InvokeUpstream(route.server, call)
  RETURN response
END

ALGORITHM HandleToolsList(request)
BEGIN
  claims ← VerifyBearerOIDC(request.Authorization)
  route ← Route(registry, request.headers)
  ASSERT route.kind = TOOLS_LIST
  tools ← TtlGet(redis, route.server)
  IF tools = MISS THEN
    tools ← InvokeUpstreamList(route.server)
    TtlPut(redis, route.server, tools, tools.ttlMs, configured_max)
  END IF
  RETURN OPA.FilterTools(route.server, tools, claims)
END
```

**Preconditions:** metadata resolution is side-effect-free; `InvokeUpstream` is the only tool-handler dispatch operation.
**Postconditions:** every denied/erroring `tools/call` has upstream invocation count zero; every upstream tool-call invocation follows one successful authorization for the same route/tool/claims; `tools/list` policy filtering occurs after cache/upstream acquisition on every request; routing never reads the body.

---

## Appendix B (§20) — Correctness Properties for Property-Based Testing

Each property is stated as a universally quantified invariant with its generator, its target, and the library that will express it (subject to **OQ-4**). Properties marked ★ are the highest-value ones for Phase 0 because they guard behaviour that is easy to get subtly wrong and expensive to debug later.

| ID | Property | Target | Library |
|:---|:---|:---|:---|
| **P-01** ★ | ∀ sequences of (success \| failure \| tick) events: the breaker's state is always in {CLOSED, OPEN, HALF_OPEN}; it is never OPEN unless ≥ `threshold` failures occurred within the last `window_s`; OPEN→HALF_OPEN happens only when ≥ `open_s` has elapsed since `opened_at`; HALF_OPEN admits at most one concurrent probe; a HALF_OPEN success yields CLOSED with an empty failure window; no state change occurs without an event or a clock advance | `CircuitBreaker` (§11.7.2, A.1) | hypothesis (stateful `RuleBasedStateMachine`) |
| **P-02** ★ | ∀ finite configured chains, endpoint availability maps, and adapter behaviours: `RouteWithCascade` terminates after considering at most `\|chain\|` distinct endpoint ids; available endpoints are invoked at most once in declared order; unavailable/unsupported endpoints are recorded and never invoked; provider timeout/malformed/error results never escape; result is `OK` or `EXHAUSTED` | `EndpointRegistry` + `ModelRouter` (§11.7.1a, §11.7.3, A.2) | hypothesis |
| **P-03** | ∀ breaker/availability states: an endpoint whose breaker is OPEN is `skipped_open_breaker`; an endpoint without a supported/configured concrete adapter is `skipped_unavailable`; in both cases adapter invocation count is zero | `ModelRouter` | hypothesis |
| **P-04** ★ | ∀ (L1 content, L2 content, similarity, provider availability): if an L1 entry exists the hit is `served_from = L1`; L2 is consulted only on an L1 miss; a below-threshold match is served only when the provider is unavailable and then always with `degraded = true`; `staleness_seconds ≥ 0` on every hit | `TieredSemanticCache` (§11.8, A.3) | hypothesis |
| **P-05** ★ | (a) ∀ header maps h and bodies b₁,b₂: `route(h,b₁)=route(h,b₂)`; missing routing header → 400, unknown server → 404, never a default. (b) ∀ rejected/denied `tools/call` requests (invalid bearer, malformed call, unknown/missing metadata, OPA deny/error): every injected upstream-operation counter = **0**. For `ALLOW`, exactly one call dispatch occurs only after authorization for the same route/tool | `HeaderRouter` + `McpGateway` (§3.1, §5.3, §11.4, A.8–A.9) | hypothesis + async counting fake |
| **P-06** | ∀ server TTL/max TTL values: non-positive server TTL creates no key; positive effective TTL is `min(server_ttl,max_ttl)` via Redis `SET PX`. Across independent clients/replicas, a value is returned only while Redis `PTTL > 0`; after Redis reports missing/expired it is never served. Runtime state contains no process-monotonic absolute expiry; an injected clock is confined to the pure reference model | `TtlToolCache` (§11.4, A.6) | hypothesis reference model + Redis integration |
| **P-07** ★ | ∀ component lists and ∀ close-failure patterns: components are closed in exact reverse construction order; every started component is closed exactly once; a close failure does not abort the remaining closes; `Close` is idempotent; total shutdown ≤ configured timeout | `App.Close` (§10.4, A.4) | rapid |
| **P-08** ★ | ∀ change-sets and ∀ injected failure points: after `ApplyAtomic` either all targets hold their new content with a backup per pre-existing target, or every target byte-equals its pre-image; no path outside `root` is written; blocklisted paths are always rejected; re-applying an identical change-set leaves content unchanged | `fileops.ApplyAtomic` (§10.10, A.5) | rapid |
| **P-09** ★ | ∀ requests that produce a non-2xx response: `Content-Type` is `application/problem+json`; the body parses as `ProblemDetails`; `body.status = http_status`; `type`, `title`, `status` are always present; `detail` never matches a secret pattern (bearer token, `postgresql://`, `sk-`, PEM header) | every backend route (§4.2, §11.2) | hypothesis + httpx |
| **P-10** | ∀ transition sequences: only edges in `ALLOWED` are accepted; terminal states are absorbing (no transition leaves `completed`/`failed`/`cancelled`); `cancel` is idempotent and returns the existing terminal state without error; two concurrent updates cannot both succeed | `TaskStateMachine` (§11.5) | hypothesis (stateful) |
| **P-11** ★ | ∀ plans p and ∀ destructive actions a: `score(p ++ [a]) ≥ score(p)` and `verdict(p ++ [a]) ⊒ verdict(p)` under ALLOW ⊏ WARN ⊏ BLOCK; `analyse` is deterministic; `destructive_count ≤ affected_resources`; any stateful deletion forces BLOCK | `SemanticPlanAnalyzer` (§11.9, A.7) | hypothesis |
| **P-12** ★ | ∀ parent environments E and ∀ configs c: `keys(buildEnv(c)) ⊆ allowlist(c) ∪ fixed_tofu_keys`; ∀ k ∈ E where k ∉ allowlist: k does not appear in the child environment; `TF_IN_AUTOMATION=1` and `TF_INPUT=0` are always present | `iac.buildEnv` (§10.6) | rapid |
| **P-13** | ∀ inbound `traceparent` strings: a well-formed header yields a context whose trace-id is preserved and whose span-id differs from the parent's; a malformed header yields a fresh trace and is never propagated onward; `tracestate` is passed through unmodified when present | `tracecontext` (§7.8) | rapid + hypothesis |
| **P-14** | ∀ HTTP responses r delivered to the API client: a non-2xx always raises exactly one error type (`ApiProblemError` or its subclass) and never a raw `TypeError`/`SyntaxError`; a `problem+json` body round-trips into `ProblemDetails`; a non-conforming error body still produces an `ApiProblemError` carrying the real HTTP status | `lib/api/client.ts` (§12.3) | fast-check |
| **P-15** | ∀ project-source mappings (`.env` parse or explicit input): Settings/Go Load either returns a fully populated validated config or one error enumerating every unknown/missing/invalid **ForgeOps** key, never a partial config. ∀ ambient OS environments containing arbitrary unrelated keys (for example `PATH`, `HOME`, CI variables): those unrelated keys are ignored and cannot by themselves cause failure. Go validates only consumed ForgeOps keys | `core/config.py`, `internal/config` (§7.1) | hypothesis + rapid |

Not assigned a new property ID (the required set remains exactly P-01…P-15), but verified executably by focused examples/integration: OIDC signatures against a fixed JWKS; Redis token-bucket equivalence to an injected-clock reference model plus concurrent atomicity; local HTTP model fixtures for timeout/malformed/fallback/exhaustion; GoReleaser/Cosign/Syft output shape; Python/tofu lock freshness; default/profile-specific Compose health; frontend build-arg inlining; and shell accessibility/Home navigation.

---

## Appendix C (§21) — Error Handling

### C.1 Backend error scenarios

| Scenario | Condition | Response | Recovery |
|:---|:---|:---|:---|
| Config/local construction invalid at boot | Missing/unknown/invalid project config, invalid URL, or failure constructing a local resource | Process exits non-zero before binding, logging every configuration problem without secrets | Operator fixes project config or local resource |
| Postgres/Redis unreachable at boot | Initial best-effort probes fail | Startup **continues**; structured warning logged; `/health` = 200; `/health/ready` = RFC 9457 503 naming failed checks; Redis index creation retries idempotently | Automatic when dependency returns; no process restart required |
| Database unreachable at runtime | Pool checkout or query error | `503` problem+json, `type: .../dependency-unavailable`; `pool_pre_ping` discards dead connections; `/health` remains 200 | Automatic on the next request |
| Redis unreachable | Tool/semantic cache, task store, limiter, or readiness probe fails | caches degrade to miss; task operations return 503; **AI limiter returns 503 fail-closed before cache/provider work**; readiness 503; liveness 200 | Automatic when Redis recovers |
| OPA unreachable or denies call | Policy filter/authorization fails | `tools/list` returns empty; `tools/call` returns 403; upstream handler invocation count is zero | Automatic once OPA recovers |
| AI bucket exhausted | No token available for `(claims.sub, /api/v1/ai/complete)` | RFC 9457 `429 .../rate-limit-exceeded` with integer `Retry-After`; no cache/provider work | Retry after indicated delay |
| Invalid/absent MCP token | No bearer, bad signature, untrusted `iss`, wrong `aud`, expired | `401` problem+json; routing/metadata/upstream are not reached and invocation count is zero; detail does not reveal the failed cryptographic check | Caller re-authenticates |
| Unknown MCP server | `Mcp-Name` not in the registry | `404` problem+json | Caller corrects the header |
| Missing routing headers | Either header absent or blank | `400` problem+json | Caller corrects the request |
| MCP server timeout | Upstream exceeds the per-call deadline | `504` problem+json; the TTL cache is **not** poisoned with a partial result | Retry; cache still serves the last good list until expiry |
| No supported/configured model endpoint | Registry has only unsupported native protocols, missing keys, open breakers, timeouts, malformed responses, or errors | `200` with `outcome: "exhausted"` and redacted per-endpoint attempt reasons; no crash or fake adapter | Configure an OpenAI-compatible endpoint/key or wait for recovery |
| Plan document unparseable | Malformed plan JSON | `422` problem+json with `errors[]` pointing at the offending path | Caller resubmits |
| Unhandled exception | Any uncaught error | `500` problem+json with a generic `detail` and the `trace_id`; the full traceback goes to logs only | Operator correlates by `trace_id` |

### C.2 Agent error scenarios

| Scenario | Condition | Behaviour | Recovery |
|:---|:---|:---|:---|
| No backend URL configured | `AGENT_BACKEND_WSS_URL` empty | `connection.ErrDisabled`, treated as a normal Phase 0 path; `App.Run` continues serving MCP | n/a |
| OpenTofu binary missing | `exec.LookPath` fails | Typed `ErrTofuNotFound` naming the expected binary and version; `agent doctor` reports it as a failed check | Operator installs OpenTofu 1.12.5 |
| OpenTofu exceeds its timeout | Context deadline | SIGTERM to the process group, grace period, then SIGKILL; partial output already streamed is retained; error wraps `context.DeadlineExceeded` | Caller retries with a longer timeout |
| OpenTofu exits non-zero | Validation or plan error | `PlanResult.ExitCode` plus captured stderr; **exit code 2 from `-detailed-exitcode` is success-with-changes, not a failure** | Caller inspects diagnostics |
| Path outside the project root | Traversal or symlink escape | `fileops` rejects before any I/O with `ErrPathOutsideRoot` | Caller corrects the path |
| Blocklisted path | `~/.ssh`, `~/.aws`, `.env`, `*.pem` | Rejected with `ErrPathBlocked`; logged as a security event | Not recoverable by design |
| Partial change-set failure | Any write fails mid-apply | Full rollback from backups, original error returned wrapped (A.5) | Caller retries the whole set |
| Git push rejected | Non-fast-forward or auth failure | Typed `ErrPushRejected` / `ErrGitAuth`; the local branch is left intact for inspection | Operator resolves; no silent force-push, ever |
| GitHub rate limit | HTTP 403 with rate-limit headers | `ErrRateLimited` carrying the reset time; polling stops rather than hammering | Caller waits until reset |
| Shutdown signal during work | SIGINT/SIGTERM | Root context cancels, subsystems unwind in reverse order within the timeout (A.4) | n/a |

### C.3 Frontend error scenarios

| Scenario | Behaviour |
|:---|:---|
| Backend returns `problem+json` | Parsed into `ProblemDetails`, surfaced as a toast with `title` + `detail` and the `trace_id` in a copyable detail line |
| Backend returns a non-conforming error | Synthesised `ApiProblemError` carrying the real HTTP status, so no `undefined` reaches the UI |
| Network failure or timeout | `ApiTransportError` with `status: 0`; TanStack Query retries twice for 5xx/transport and **never** for 4xx |
| Render error in a route | `app/error.tsx` boundary renders a recoverable message with a reset action; the shell chrome stays usable |
| 404 | `app/not-found.tsx` inside the shell layout |

---

## Appendix D (§22) — Performance Considerations

Phase 0 has no user-facing performance requirement, but three NFR targets are *foundation-shaped* and cheap to protect now.

| NFR | Target | Phase 0 action |
|:---|:---|:---|
| NFR-06/07 | Dashboard load < 2 s initial, < 500 ms cached | Next.js 16 with Turbopack; the shell is a Server Component with no client-side data fetch on first paint; TanStack Query `staleTime` of 30 s prevents refetch storms |
| NFR-05 | Agent ↔ backend WSS latency < 100 ms | Not measurable in Phase 0 (no protocol), but `coder/websocket` with per-message read limits and no per-message allocation churn is the choice that makes it achievable |
| NFR-29 | pgvector to ~50 M vectors | HNSW from the first migration, so no reindex is needed at scale; `ef_search` as a query-time knob; `m = 16 / ef_construction = 64` as a balanced starting point |
| Phase 0 AI abuse bound | concurrent replicas cannot overspend a caller bucket | Redis-time Lua token bucket performs refill/consume atomically; costly-route Redis failure fails closed before cache/provider work |

Phase 0 build-time performance is a developer-experience concern worth defending: `paths-filter` avoids running all three components' pipelines on every change, `pnpm` gives a content-addressable store, `TF_PLUGIN_CACHE_DIR` avoids re-downloading OpenTofu providers on every test run, and the Docker builds are multi-stage with dependency layers ordered before source layers.

Known cost to accept: HNSW index builds are slower than IVFFlat. That is the documented trade for sub-10 ms queries at 99 %+ recall (Research §A0a), and Phase 0's data volume makes the build cost irrelevant.

---

## Appendix E (§23) — Phase 0 Completion Criteria Traceability

Every criterion from `phases.md` Phase 0, mapped to the design section that makes it achievable and the evidence that proves it.

| # | Completion criterion | Design | Evidence |
|:--|:---|:---|:---|
| 1 | `make build` succeeds for all three components | §13.4 | CI `build` job |
| 2 | `make test` passes | §7.6, §13.4 | CI `test` job |
| 3 | `make lint` passes | §13.4, §8.3 | CI `lint` job |
| 4 | `docker-compose up` starts all services | §2.2, §13.3 | Fresh clone: `docker compose up -d --wait` exits 0 for exactly default-profile `postgres`, `redis`, `opa`, `backend`, `frontend`; separate `--profile vault` / `--profile tools` evidence after owning tasks |
| 5 | Health check endpoint returns 200 | §4.4, §11.1 | `/health` is 200 with dependencies both up and deliberately down; `/health/ready` is 200 when ready and RFC 9457 503 naming failed checks during outage |
| 6 | Frontend loads at localhost:3000 | §12.1, §12.2, §12.6 | Playwright asserts keyboard-accessible active Home link; image built with a non-default `NEXT_PUBLIC_API_BASE_URL` proves generated client uses that browser URL |
| 7 | Go binary compiles for Windows/macOS/Linux × amd64+arm64 | §8.2, §17.1 **D-1** (tree-sitter deferred, so `CGO_ENABLED=0` holds) | GoReleaser snapshot build matrix, 6 artifacts |
| 8 | GoReleaser produces signed + SBOM-attested binaries | §8.1, §8.2 | `dist/` contains `.sig`, `.pem`, `.sbom.json` per artifact |
| 9 | Pre-commit hooks pass on all files | §8.4 | `pre-commit run --all-files` in CI |
| 10 | MCP Gateway responds to `tools/list` and `tools/call` | §3.1, §5.3, §11.4, §10.8, §11.10 | end-to-end tests prove list filtering and allowed call; P-05 generated denial/error cases assert upstream invocation count zero |
| 11 | MCP Tasks lifecycle works (create → poll → cancel) | §11.5, §3.2 | integration test + P-10 |
| 12 | OAuth 2.1/OIDC issuer validation blocks unauthorized requests | §3.1, §11.4, §15.2 | four-case 401 test: no token, untrusted `iss`, expired, wrong `aud`; counting upstream remains zero |
| 13 | Plan Analyzer returns results for sample input | §11.9, §3.5 | test over `agent/testdata/plan-sample.json` + P-11 |
| 14 | SQLModel models defined with pgvector column support (HNSW index) | §6.2, §6.3, §17.1 **D-2** (1536-d + `model_id`) | migration test asserting the `hnsw` index exists with `vector_cosine_ops` and the column is `vector(1536)` |
| 15 | CycloneDX SBOM generated for the Go agent build | §8.1, §13.4 (`make sbom`) | `dist/*.sbom.json` validates against the CycloneDX schema |
| 16 | Cosign keyless signing verified on a release artifact | §8.1, §13.4 (`make verify-release`) | `cosign verify-blob` exits 0 with the expected certificate identity |
| 17 | Model routing fallback cascade functions end-to-end | §11.7.1a–§11.7.5, §3.3 | deterministic local HTTP fixtures prove primary failure/timeout, cross-provider fallback, self-hosted success, malformed response, trace headers, unsupported skip and EXHAUSTED; no vendor key/network + P-02/P-03 |
| 18 | Circuit breaker trips on simulated failures | §11.7.2, A.1 | 5-failures-in-30 s test asserting OPEN, then HALF_OPEN after 60 s + P-01 |

### Deliverable coverage check

| Group | Covered in |
|:---|:---|
| 0.1 Repository Structure | §0.3, §1.3, §2.3, §2.4, §8.4, §13.1, §13.3, §13.4 |
| 0.2 Go Agent Scaffold | §8.1, §8.2, §8.3, §10 (all; staged order in §10.1), §2.4 NOTICE audit |
| 0.3 Python Backend Scaffold | §6, §7.1, §7.2, §7.6, §7.7, §11.1–§11.3, §13.3, §16.2 |
| 0.4 Next.js Frontend Scaffold | §12 (all, including Home navigation and build-time env) |
| 0.5 MCP Gateway Integration | §3.1, §5.3, §11.4, §11.5, §11.6, §11.10, §10.8, §7.8, P-05/P-06 |
| 0.6 GitOps Workflow | §3.6, §10.7 |
| 0.7 Plan Analyzer | §3.5, §11.9 |
| 0.8 OpenTofu Switch | §3.4, §10.6 (exact provider + six-platform lock), §13.3 |
| 0.9 Model Routing Configuration | §3.3, §5.6–§5.7, §11.7, §11.8, §13.2 |
| Progress record | §18 |

---

*End of Phase 0 design. Scope is bounded by §1; anything not listed in §1.1 is out of scope for this phase. The five owner decisions remain settled in §17.1 (D-1, D-2, D-5, D-14, D-19); the twelve questions remaining in §17.2 are **all non-blocking**. The review corrections introduce no unresolved blocker.*
