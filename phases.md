# Implementation Phases — AI-Powered DevOps Automation Platform

> **Purpose:** This document divides the project into 5 clearly bounded phases. Each phase has explicit deliverables, dependencies, and completion criteria. The AI IDE MUST build phases in order and must NOT skip ahead.

---

## Phase 0: Foundation & Project Scaffolding

**Goal:** Set up the entire project skeleton with working build pipelines and developer environment. No features yet — just infrastructure.

**Estimated Duration:** 2-3 weeks

### Deliverables

#### 0.1 Repository Structure

- [x] Create monorepo layout as defined in PRD Section 8
- [x] Set up `agent/`, `backend/`, `frontend/`, `docs/`, `.github/` directories
- [x] Create root `Makefile` with common commands (build, test, lint, clean)
- [x] Create root `docker-compose.yml` for full-stack development
- [x] Create `.env.example` with all required environment variables
- [x] Create `.gitignore` (Go binaries, Python caches, node_modules, .env, IDE files)
- [x] Set up **pre-commit framework** with Gitleaks, Ruff, gofmt hooks

#### 0.2 Go Agent Scaffold

- [x] Initialize Go module: `go mod init github.com/org/ai-devops-agent`
- [x] Create `cmd/agent/main.go` — thin entry point
- [x] Create `internal/` subdirectories: connection, docker, k8s, scanner, executor, validator, policy, fileops, iac, devtools, telemetry, mcp
- [x] Use **constructor injection** pattern (not wire/uber-fx) for DI
- [x] Implement **graceful shutdown** pattern with signal.NotifyContext + errgroup
- [x] Add core dependencies: `github.com/coder/websocket` (WebSocket), `github.com/docker/docker/client` (Docker API), `k8s.io/client-go` (K8s API), `go.uber.org/zap` (logging), `github.com/spf13/cobra` (CLI), `github.com/fsnotify/fsnotify` (file watching), `github.com/minio/selfupdate` (auto-update), `github.com/sergi/go-diff` (diff generation), `github.com/mark3labs/mcp-go` (MCP server), `github.com/tree-sitter/go-tree-sitter` (AST parsing)
- [x] Set up `golangci-lint` configuration
- [x] Set up GitHub Actions CI for Go: lint + test + build
- [x] Configure **GoReleaser** with Cosign signing + Syft SBOM + SLSA provenance
- [x] Set up Cosign keyless signing configuration
- [x] Set up Syft for CycloneDX SBOM generation per release

#### 0.3 Python Backend Scaffold

- [x] Initialize FastAPI project structure in `backend/` using **domain-driven modular monolith** layout
- [x] Set up `src/core/` with config, logging, **async database session management (expire_on_commit=False)**
- [x] Create `src/main.py` with health check endpoint, **lifespan events**, **middleware stack**
- [x] Set up PostgreSQL + pgvector in docker-compose
- [x] Set up Alembic for migrations (including pgvector column detection)
- [x] Set up **pytest + pytest-asyncio + httpx** for async integration tests (coverage >70% goal)
- [x] Create Dockerfile with multi-stage build
- [x] Set up `ruff` configuration (lint + format)
- [x] Set up `pip-audit` in CI for dependency vulnerability scanning

#### 0.4 Next.js Frontend Scaffold

- [x] Initialize Next.js 16 project with App Router
- [x] Set up shadcn/ui with base theme
- [x] Create layout with sidebar navigation, header, theme toggle
- [x] Set up TanStack Query and Zustand
- [x] Create **API client wrapper with RFC 9457 error handling**
- [x] Set up `vitest` and `@testing-library/react`
- [x] Set up **Playwright** for E2E testing
- [x] Set up **k6** for load testing
- [x] Set up **React Hook Form + Zod** as form handling standard
- [x] Set up **pnpm** as the package manager
- [x] Set up ESLint and Prettier

#### 0.5 MCP Gateway Integration (Core Architecture — Stateless, July 2026 Final Spec)

- [x] Set up **MCP Gateway** in the backend using `Mcp-Method` + `Mcp-Name` header routing
- [x] Implement **OAuth 2.1/OIDC auth** with `iss` parameter validation per RFC 9207
- [x] Implement **OPA policy enforcement** at the gateway (filter tools by agent blast radius)
- [x] Implement **TTL-based caching** on tool lists (respect `ttlMs` from servers)
- [x] Implement **W3C Trace Context** propagation across all MCP server calls
- [x] Create **base MCP server template** using `mark3labs/mcp-go` for Go agent tools
- [x] Create **base MCP server template** in Python (FastAPI) for backend-hosted tools
- [x] Implement **Tasks Extension** lifecycle (`tasks/get`, `tasks/update`, `tasks/cancel`)
- [x] Implement **MCP Apps** support for sandboxed iframe UIs (approval forms, dashboards)

#### 0.6 GitOps Workflow (P0)

- [x] Set up Git client library in the Go agent
- [x] Implement PR creation flow (branch → commit → push → PR)
- [x] Implement PR review status polling

#### 0.7 Plan Analyzer (P0)

- [x] Create validation pipeline skeleton
- [x] Implement semantic analysis module
- [x] Connect validation pipeline to approval workflow

#### 0.8 OpenTofu Switch (P0)

- [x] Install OpenTofu in Docker development environment
- [x] Create OpenTofu runner module in Go agent (with timeout, output streaming, signal handling)
- [x] Test `tofu validate` and `tofu plan` programmatic execution

#### 0.9 Model Routing Configuration (P0)

- [x] Configure **model routing tier definitions** for all 6 tiers
- [x] Model tiers: GPT-5.6 Sol (high/coding), Claude Fable 5 (high/analysis), Grok 4.5 (medium), Sonnet 5 & DeepSeek V4 (medium/value), Gemini 3 Flash (low/logs)
- [x] Implement **fallback cascade** (primary → secondary → cross-vendor → self-hosted → template)
- [x] Implement **circuit breaker** per model endpoint (5 failures in 30s → OPEN → HALF-OPEN after 60s)
- [x] Implement **BYO-Key** architecture with Infisical vault for per-tenant LLM keys
- [x] Implement **semantic caching** (L1 exact-match → L2 similarity >0.95 → L3 prefix cache via Redis)

### Completion Criteria

- [x] `make build` succeeds for all three components
- [x] `make test` passes (with placeholder tests)
- [x] `make lint` passes
- [x] `docker-compose up` starts all services
- [x] Health check endpoint returns 200
- [x] Frontend loads at localhost:3000
- [x] Go binary compiles for Windows, macOS, Linux (amd64 + arm64)
- [x] GoReleaser pipeline produces signed + SBOM-attested binaries
- [x] Pre-commit hooks pass on all files
- [x] MCP Gateway responds to `tools/list` and `tools/call` requests
- [x] MCP Tasks lifecycle works (create → poll → cancel)
- [x] OAuth 2.1/OIDC issuer validation blocks unauthorized requests
- [x] Plan Analyzer returns results for sample input
- [x] SQLModel models defined with pgvector column support (HNSW index)
- [x] CycloneDX SBOM generated for Go agent build
- [x] Cosign keyless signing verified on release artifact
- [x] Model routing fallback cascade functions end-to-end
- [x] Circuit breaker trips on simulated failures

> **Both boxes above are now ticked, and the note that used to be here was wrong.** It said "this branch
> has never cut a tag". The repository has three: `v0.0.1-rc1`, `v0.0.1-rc2` and `v0.0.1-rc3`, all
> released on 2026-07-29, and `.github/workflows/release.yml` ran to completion for each. The claim was
> made from reading the workflow rather than from asking the remote, which is exactly the mistake the
> evidence table exists to prevent.
>
> **The evidence, produced from a developer machine with no shared secret.** `v0.0.1-rc3` carries all six
> targets — windows, darwin and linux × amd64 and arm64 — plus `.deb` and `.rpm`, and every artifact has a
> `.sig`, a `.pem`, a `.sbom.json`, an `.intoto.jsonl` and an `.att.sigstore.json` beside it.
>
> - **Signed:** `cosign verify-blob` against `forgeops-agent_0.0.1-rc3_windows_amd64.zip`, with
>   `--certificate-identity-regexp` pinned to this repository's `release.yml` on a `v*` tag and
>   `--certificate-oidc-issuer https://token.actions.githubusercontent.com`, answered **`Verified OK`**.
> - **SBOM-attested:** that artifact's `.sbom.json` is CycloneDX `specVersion` 1.6 with **91 components**.
> - **Real binary:** the `.exe` inside the archive runs and reports
>   `forgeops-agent 0.0.1-rc3 (commit: 7f5213f65931b80f6abd6a16baa46c808e723e75, built: 2026-07-29T16:15:30Z)`.
>
> Criterion 16's self-verification runs before any provenance step, so a provenance failure cannot mask it.

### Excluded (for this phase)

- ❌ Any feature logic (analysis, generation, deployment)
- ❌ UI beyond shell layout
- ❌ Database migrations beyond initial schema
- ❌ Authentication

---

## Phase 1: MVP Core — Analysis, Generation, & Approval

**Goal:** Build the core value proposition: scan a codebase, score readiness, generate missing configs, validate them, and apply with approval.

**Estimated Duration:** 8-12 weeks

### Deliverables

> **Status of the 91 boxes below, recorded rather than ticked.**
>
> They are left unticked on purpose. Each would have to be exercised on its own to be ticked
> honestly, and a tick meaning "this looks done" is worse than no tick — it is the state these
> documents were already in, and it is what made them useless. What CAN be said with evidence is
> stated per subsection here; the Completion Criteria at the end of this phase carry the per-item
> evidence.
>
> **SEVEN BOXES ARE NOW TICKED, and the exception is the rule working rather than an erosion of it.**
> The seven are the §1.2, §1.4, §1.6, §1.7 and §1.8 FRONTEND deliverables. Each carries, inline, the
> screen that implements it and the test that proves it — which is exactly the bar the paragraph above
> sets and could not previously be met, because those seven had no screen at all. They were the
> visible half of a larger gap: the backend served 48 routes and the frontend called 13, so the engine
> was built and tested while the browser was a read-only window onto a fraction of it. Closing that is
> the pass those ticks record.
>
> Two things found by building it belong here rather than in a commit message, because both are
> examples of the failure this document's own preamble exists to prevent — a claim that outlived the
> behaviour it described:
>
> - `frontend/app/(shell)/projects/page.tsx` mapped every project to `readinessScore: 0`, a hardcoded
>   literal, so every project displayed a zero regardless of its real score. The list now reports
>   whether anything is indexed, in words; the score is computed once, on the detail screen.
> - the readiness screen's explanatory panel said the score was derived from stored settings "not from
>   a walk of its working tree" and described FIVE categories. Both had been false since the engine
>   moved to index-derived scoring — six categories, computed from `file_tree` and `file_contents`.
>   The home page's route list and `generation/service.py::_render`'s docstring had drifted the same
>   way and are corrected too.
>
> The remaining 84 boxes stay unticked, unchanged, for the reason given above.
>
> | §                              | State                                 | Evidence, or the gap                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
> | :----------------------------- | :------------------------------------ | :------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
> | 1.1 Agent pairing & connection | verified end to end                   | The journey pairs a real agent over mTLS and runs signed commands. `session.heartbeat` is a notification, so liveness is a WebSocket **ping** rather than the inbound-silence timeout written below — that timeout dropped every healthy session on a 90-second cycle.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
> | 1.2 Multi-project workspace    | verified, in a browser                | Projects created and read through the API in journey steps 2 and 5, and now CREATED BY CLICKING in the onboarding walk's step 1. FR-01 through FR-05 all have a surface: a create form (a browser cannot report a directory's absolute path, so the path is typed and the form says why), server-side search, tags, per-user favourites, and archive plus an honest delete. **Favourites are per USER**, which needed a table: `projects.settings.favourite` has existed since revision `0009` and is per project, so in a tenant with two people one person starring a project would reorder the other's list. Delete counts thirteen cascading tables before it runs and reports them, and the audit rows SURVIVE — revision `0007` gave `audit_events.project_id` no foreign key for exactly that reason, and that intent is now asserted rather than only documented.                                                                                                                                                                                                                                                                                                                                                                                     |
> | 1.3 Codebase analysis engine   | verified                              | A real scan of `backend/src` persists 141 files, 1857 dependency edges (243 resolved), 977 chunks; the fixture yields 7 files with genuine 1024-d BGE-M3 vectors. **Watch mode now exists and is proven live:** `forgeops-agent watch --project <id>` runs fsnotify → a real debounce → an incremental submit, and one edit of a module two files import gave `submitted 3 file(s) in the closure of depdemo/lib.js`. The fan-out's exact set is pinned deterministically — a change re-indexes the file, both direct importers and the TRANSITIVE one, and not the file that imports nothing. The agent self-triggers rather than waiting to be told, for the same reason `scan` is a verb: §2.2.1 confines `send_command` to `governance/`, so a backend-initiated re-index would be a governance decision per keystroke, and the agent already owns its workspace. Two defects were found by running it: `DebouncedWatcher` accepted a `debounceMs` it never read, and a DELETION produced an empty report the backend refused with 422 (a deleted file is absent from the fresh scan the closure is derived from, so finding its dependants would need the previous graph — deletions now trigger a full re-index).                                       |
> | 1.4 Deployment readiness       | verified                              | Scored from the index with `projects.settings` empty, over this section's six weighted categories. `settings` may only REFINE, through `ignore_globs`. The six categories now EXPAND in the browser into the individual checks behind each score, each naming the indexed path that satisfied it and FR-19's "why it matters" — a category at 40 with no visible evidence is indistinguishable from a bug in the scorer. Building it found that the two sides spell a category differently: the breakdown's keys are model field names (`containerization_score`) and a check's `category` is the category itself (`containerization`), so the panel rendered twelve rows for six categories until they were reconciled. Caught by the live onboarding walk, not by a fixture — the fixture carried the misconception.                                                                                                                                                                                                                                                                                                                                                                                                                                        |
> | 1.5 AI generation & validation | verified, with a stated limit         | `served_from` reaches `provider`, `l1` and `l2` on real calls, and the browser now observes 149 strictly increasing painted lengths (criterion 13). **The cascade and the circuit breaker are now proven across two GENUINELY SEPARATE live endpoints**, not against doubles: a second model server (`ollama-secondary`, its own container and port, sharing the weights volume) is registered as the `self_hosted` tier's secondary, and stopping the primary produced the full documented lifecycle — four attempts falling through `qwen3-coder-next:error → qwen3-coder-standby:success` with the breaker closed, the **5th failure inside 30 s opening it**, subsequent attempts recording `skipped(circuit_breaker_open)` with latency dropping 4.6 s → 0.7 s because no connection is attempted, **`half_open` after the 60 s cooldown**, and a successful probe closing it and returning traffic to the primary. `generation_runs.endpoint_id` records which endpoint answered. **The limit, stated rather than implied: only self-hosted endpoints have ever served a live call.** `LLM_KEY_*` are placeholders, so the five hosted vendor tiers remain unconfigured pending keys — the cascade is proven across real ENDPOINTS, not across vendors. |
> | 1.6 Change approval centre     | verified                              | Journey steps 8–9, and 13 for revert. A blocked revert escalates to approval rather than being refused, which is what makes §3.6's `applied → reverted` edge reachable at all. REVERT IS NOW REACHABLE FROM THE UI, which it was not for two reasons rather than one: `ApprovalCenter` narrowed its mutation type to `approve                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 | reject`, AND the list was filtered to `pending_approval`, so an applied change set never appeared on the screen at all. An escalated revert is presented as an outcome rather than an error — `approval-required`is registered at 202, inside the 2xx range, so the body arrives as a success and is narrowed explicitly. A per-project change history timeline explains all thirteen states, keeping`rolled_back`(an apply that undid itself) distinct from`reverted` (a deliberate reversal through the chokepoint). |
> | 1.7 Policy engine              | verified, and one fabrication removed | 102 tests over OPA and the policy-evaluation surface, plus a full editor over the new `GET /api/v1/policies` — a list route that did not exist, which is why a policy screen was unbuildable and `/policies` was a read-only wall of templates. **`POST /policies/{id}/test` used to fabricate its verdict**: with no `opa` on PATH it returned `"allow" if input["action"] == "allow_me" else "deny"`, a decision on a security surface that no policy engine computed. It raises the registered `503 dryrun-unavailable` now, reports the query it evaluated and the evaluator's own version, and distinguishes an UNDEFINED rule from a deny. The test that covered it asserted the synthesised values, so it passed with or without OPA installed; the binary is now in the backend image and in the backend CI job, both by COPY from the digest-pinned image Compose already runs. The policy READ path was also not tenant-scoped — any authenticated caller could read, rewrite or delete another tenant's rules by id — and answers the non-disclosing 403 now.                                                                                                                                                                                      |
> | 1.8 Secret management          | verified                              | Encryption at rest, redaction before LLM context, deploy-time injection; Q-12, Q-24, Q-28. The vault can now add, rotate and delete, and write-only is STRUCTURAL rather than promised: the value inputs are uncontrolled so no secret enters React state, the DOM node is cleared before the request is awaited so a failed write clears it too, and no response shape in that module carries a value in either direction. Deleting a reference does not reach into Infisical, and the confirmation says so — this platform does not own that store.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
> | 1.9 Audit logging              | verified                              | Unbroken hash chain asserted in journey step 12. **Known limitation:** `GovernanceAction` is a closed vocabulary with no `applied` action, so an audit reader cannot ask "was this applied?" and must consult `change_sets` — Q-04 allows one row per transit.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
> | 1.10 Governance control plane  | verified                              | Every mutation passes the chokepoint; `check-chokepoint.sh` proves it over both runtimes by parsing the import graph and reports "both halves clean".                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
> | 1.11 Auth integration          | verified                              | Real Authentik OIDC in journey step 1. The agent authenticates as a DEVICE on both factors, which `require_principal` cannot express.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |

#### 1.1 Agent Pairing & Connection (JSON-RPC 2.0 over WSS)

- [x] Implement **JSON-RPC 2.0 protocol** over WSS (structured `method`, `params`, `id`, `error` schema)
- [x] Implement message types: `session.connect`, `session.heartbeat`, `command.execute`, `command.result`, `command.progress`, `approval.request`, `approval.response`, `agent.error`, `agent.status`
- [x] Implement WSS connection manager with auto-reconnect (exponential backoff: start 1s, max 60s, jitter 0.5x)
- [x] Implement mTLS + JWT authentication handshake
- [x] Implement pairing code flow (6-char code → revocable device token, 5-min expiry)
- [x] Implement heartbeat mechanism (every 30s, timeout after 90s)
- [x] Implement command envelope protocol with `approval_id`, `policy_context`, `signature`
- [x] Implement operation whitelist validation (named operations only — never arbitrary shell)
- [x] Implement agent-side policy evaluation (defense in depth using OPA Wasm embedded)
- [x] Implement **command envelope schema with HMAC-SHA256 signature** for integrity

#### 1.2 Multi-Project Workspace

- [x] Backend: CRUD API for projects (import from GitHub, local path)
- [x] Backend: Project settings (LLM budget, policies basic)
- [x] Frontend: Project list view with search, tags, favorites — `app/(shell)/projects/page.tsx`; every filter is a query parameter on `GET /projects`, asserted on the query string by `__tests__/project-workspace.test.tsx` because that is the only observable distinguishing server-side filtering from the browser-side version
- [x] Frontend: Project detail page — `app/(shell)/projects/[projectId]/page.tsx`, the first caller `GET /projects/{id}` has ever had; `__tests__/project-workspace.test.tsx::the project detail page`
- [x] Frontend: Recent activity feed per project
- [x] Agent: Register project directory, watch for changes

#### 1.3 Codebase Analysis Engine

- [x] Agent: **Language detection** — tiered detection (package manager → extension → shebang → content heuristics)
- [x] Agent: **Dependency graph builder** — resolve imports/requires across files for cross-file RAG
- [x] Agent: Recursive file tree scanner (respects .gitignore + .dockerignore)
- [x] Agent: File size/type filters (skip binaries >1MB, node_modules, .git)
- [x] Agent: **Tree-sitter AST parsing** using `github.com/tree-sitter/go-tree-sitter` (official Go bindings)
- [x] Agent: **cAST semantic chunking** — bottom-up grouping (statements → functions → classes), constraint-based splitting, density optimization
- [x] Agent: **Metadata enrichment** — file path, function signature, class hierarchy, dependency references
- [x] Agent: Generate vector embeddings using Voyage Code 3 (API) or BGE-M3 (local) with hybrid sparse-dense (BM25 + vector) indexing
- [x] Agent: Store embeddings in pgvector via backend API (use **HNSW index** with tuned `ef_search`)
- [x] Agent: **Cold start discovery mode** — lightweight heuristic analysis first, async full indexing in background
- [x] Agent: **Watch mode** via fsnotify with fan-out/fan-in concurrency for incremental scanning
- [x] Backend: Codebase Index API (CRUD for file tree, embeddings, symbol table)
- [x] Backend: Implement **dependency-graph-aware incremental scanning** (re-index changed files + their dependants)

#### 1.4 Deployment Readiness Analysis

- [x] Backend: Scoring engine with weighted categories
- [x] Categories: Containerization, CI/CD, Orchestration, Env Config, Security, IaC
- [x] Backend: Checklist checks (Dockerfile exists, multi-stage, non-root, etc.)
- [x] Backend: Plain-language report generation with "why it matters"
- [x] Frontend: Readiness score display (0-100) with radar chart
- [x] Frontend: Detailed category breakdown with expandable items — `features/readiness/ReadinessBreakdown.tsx`, each category expanding into its checks with the indexed path that satisfied it and FR-19's "why it matters"; `__tests__/route-pages.test.tsx::Readiness category breakdown`
- [x] Frontend: Actionable recommendations list

#### 1.5 AI File Generation & Validation Pipeline

- [x] Backend: AI engine with RAG from Codebase Index (hybrid sparse-dense retrieval)
- [x] Backend: **6-tier model routing** with fallback cascade:
  - High: GPT-5.6 Sol (primary), Claude Fable 5 (backup) — architecture, multi-file generation
  - Medium: Grok 4.5, Claude Sonnet 5, DeepSeek V4 — Dockerfile, CI/CD, analysis
  - Low: Gemini 3 Flash — log analysis, formatting
  - Self-hosted: GLM-5.2, Qwen3-Coder-Next — air-gapped sensitive codebases
- [x] Backend: **Circuit breaker** per model endpoint (5 failures/30s → OPEN → 60s → HALF-OPEN)
- [x] Backend: **Fallback cascade**: Primary → Cross-vendor → Self-hosted → Safe Template Library
- [x] Backend: Structured output schemas using Pydantic v2 strict mode
- [x] Backend: Integration with MCP servers for tool access via **MCP Gateway**
- [x] Backend: Use **SSE (Server-Sent Events)** with FastAPI native `EventSourceResponse` (in-tree since 0.139.2; no `sse-starlette` dependency) for streaming LLM token responses
  - Event types: `status`, `token`, `progress`, `validation`, `complete`, `error`
- [x] Backend: Implement **tiered semantic caching** with Redis:
  - L1: Exact-match prompt hash → `GET`/`SET`
  - L2: Semantic similarity (>0.95) via Redis Vector Search
  - L3: Prompt prefix cache (system prompts, docs)
- [x] Backend: **Safe Default Template Library** — hardcoded, verified templates for 8+ languages:
  - Node.js, Python, Go, Rust, Java/Kotlin, Ruby, PHP, .NET
  - Each: Dockerfile, K8s Deployment+Service+Ingress, GitHub Actions CI, Helm, OpenTofu
  - Used when AI fails after max 3 retries
- [x] Backend: **Evaluation pipeline** for AI outputs:
  - Deterministic: syntax checks, schema validation, Trivy scan
  - Rubric (LLM-as-Judge): best practice compliance, security posture, cost efficiency
- [x] Backend: **Cold start progressive UX** — show partial results as they become available
- [x] Agent: Dockerfile validation (`docker compose config`)
- [x] Agent: K8s manifest validation (`kubectl --dry-run=server`)
- [x] Agent: OpenTofu validation (`tofu validate`, `tofu plan`)
- [x] Agent: YAML schema validation (`yamllint` + JSON Schema)
- [x] Agent: Helm validation (`helm lint`, `helm template --validate`)
- [x] Backend: Validation-feedback loop (max 3 iterations, then safe template fallback)
- [x] Backend: Plan Analyzer (semantic check on generated plans)
- [x] Generated artifacts: Dockerfiles, docker-compose, K8s Deployments + Services
- [x] Generated artifacts: GitHub Actions workflows, Helm charts
- [x] Generated artifacts: OpenTofu configs, `.env.example`, README docs

#### 1.6 Change Approval Center

- [x] Backend: Change-set CRUD API (create, validate, approve, reject, apply)
- [x] Backend: Automatic timestamped backup before apply
- [x] Backend: Atomic all-or-nothing change application
- [x] Frontend: Diff preview (side-by-side and unified)
- [x] Frontend: Approval/reject buttons with comment field
- [x] Frontend: Change history timeline per project — `features/approvals/ChangeHistoryTimeline.tsx`, with all thirteen §3.6 states explained (a test pins the set against the CHECK constraint revision `0010` added); `__tests__/codebase-and-history.test.tsx`
- [x] Agent: Backup-before-mutate implementation
- [x] Agent: Atomic file operations (transactional writes)

#### 1.7 Policy Engine (Basic)

- [x] Backend: OPA integration for policy evaluation
- [x] Backend: Policy CRUD API
- [x] Backend: Pre-defined policy templates (scheduling, file restrictions)
- [x] Agent: Mirror policy rules locally for zero-trust enforcement
- [x] Frontend: Policy list and editor UI — `features/policies/PolicyEditor.tsx` plus `app/(shell)/policies/page.tsx`, full CRUD over the new `GET /api/v1/policies`, with `opa check`'s own message rendered verbatim and a Test action reporting the decision, the query and the evaluator; `__tests__/policy-editor.test.tsx`
- [x] Frontend: Policy violation display with explanation — `components/ui/governance-refusal.tsx`, keyed on the stable RFC 9457 `type` and asserted an exact mirror of the registry's governance subset; `__tests__/governance-refusal.test.tsx`
- [x] Implemented policies: "Never deploy on Fridays", "Never edit package.json", "Require approval for production"

#### 1.8 Secret Management (Basic)

- [x] Backend: Integrate Infisical for encrypted secret storage
- [x] Backend: Secret CRUD API (per project, per environment)
- [x] Agent: Secret scanning during codebase analysis (Gitleaks)
- [x] Agent: Secret redaction before LLM context
- [x] Agent: Deploy-time secret injection (environment variables)
- [x] Frontend: Secret vault UI (add, edit, delete, list) — `features/vault/SecretVault.tsx`; write-only is structural (uncontrolled inputs, cleared before the request is awaited, no value field in either direction) and `__tests__/vault-write.test.tsx` asserts the shape rather than the screen

#### 1.9 Audit Logging

- [x] Backend: Immutable audit log for all actions
- [x] Fields: who, what, when, why, before/after state
- [x] Frontend: Audit log viewer
- [x] Ensure agent-side operations are also logged

#### 1.10 Agent Governance Control Plane (P1 Architecture)

- [x] Backend: Implement **unified Governance Control Plane** — a single enforced chokepoint routing every mutating action through: policy evaluation → approval gate → change-set compilation → blast-radius check (Semantic Plan Analyzer) → audit record → rollback handle
- [x] Backend: No agent mutation bypasses this layer — it is the trust moat
- [x] Agent: OPA compiled to **Wasm** embedded in the Go agent binary for the agent-side half of the double policy evaluation (Cerbos v0.54.0 stays as the backend app RBAC sidecar)
- [x] Agent: SPIFFE/SPIRE **X.509-SVID + mTLS** with attestation (namespace + service-account + image-digest) for workload identity — no long-lived agent keys; JWT-SVID only for crossing L7 proxies

#### 1.11 Auth Integration

- [x] Set up Authentik or Keycloak container
- [x] Implement OIDC/OAuth2 login flow
- [x] Implement JWT token management
- [x] Implement device/agent token flow
- [x] Implement basic RBAC (admin, developer, viewer)

### Completion Criteria

> **How these are ticked.** A box is ticked only where something was RUN and its output recorded;
> the evidence is named on the line so a reader can re-run it rather than trust the tick. Two are
> deliberately left open with the reason stated. "the journey" means
> `frontend/e2e/journey.spec.ts`, which was observed at 13/13 twice back to back with no cleanup
> between the runs (10.7 min then 1.4 min — the second faster because the semantic cache serves the
> repeated prompt, which is itself the evidence for the last criterion below).

- [x] User can install agent, pair with dashboard, import a project — journey steps 1–4: a real
      `forgeops-agent` binary pairs over mTLS with a 6-character code and the device reaches `active`
- [x] Agent scans codebase and produces readiness score — journey step 5 runs
      `forgeops-agent scan --project <id>`; the score is computed from `file_tree`/`file_contents`
      with `projects.settings` empty, and `evaluated_paths` equals the file count the scan reported
- [x] AI generates Dockerfile and K8s manifests from real project — journey step 6 through the
      six-tier router to a live `qwen2.5-coder:1.5b`; `generation_runs.served_from='provider'`,
      `endpoint_id='qwen3-coder-next'`, 287 completion tokens. The four artifacts are derived from
      the `projects` row, not from keyword-sniffing the prompt
- [x] Generated files pass validation pipeline — journey step 6's `validation` event, plus the
      `Templates Library Validation Pipeline` workflow
- [x] User can view diff, approve, and apply changes — journey steps 8 and 9, both diff view modes
- [x] Files are applied atomically with backup — journey steps 10 and 11: artifacts on disk match the
      hashes the backend recorded, and a rollback manifest exists for every overwritten file
- [x] Policies are enforced (block Friday deploys, require approvals) — 102 tests across
      `test_governance_policy_opa.py` and `test_policy_evaluations.py`
- [x] Secrets are stored encrypted and injected at deploy time — `test_0006_secrets.py`,
      `test_secrets_api.py`, and properties Q-12 (redaction), Q-24 (secret absence), Q-28 (injection
      confinement)
- [x] All actions are logged in immutable audit trail — journey step 12 asserts the hash chain is
      unbroken with `lag(hash) OVER (ORDER BY seq)`; properties Q-04 and Q-05
- [x] End-to-end test: import Node.js project → generate Dockerfile + K8s → approve → apply — the
      journey, over `tests/e2e/fixture-project` (a real Node service; the scanner detects
      `javascript`)
- [x] Test coverage ≥ 70% — measured PER COMPONENT, each the way its own CI gate measures it, because
      that is how the criterion is written and an aggregate would let one component hide behind
      another. **Backend 85.77%** (2557 passed), enforced by `--cov-fail-under=70` in
      `backend/pyproject.toml`'s addopts so every pytest run applies it, not only CI. **Agent 76.7%**
      by `scripts/check-coverage.sh 70`, which merges one profile with `-coverpkg=./internal/...` so a
      package covered only by another package's tests still counts. **Frontend 95.81%** statements /
      85.34% branch / 95.47% functions / 95.92% lines from `vitest --coverage` over 20 test files.
      Note the tracking record was stale in BOTH directions — it said frontend 37.04% and agent 78.8%
      — and the frontend thresholds have been raised from 70 to 90/90/90/80, because a floor of 70
      against 95.81% permits 25 points of silent decay rather than gating anything
- [x] HNSW indexes created on pgvector embedding columns for production performance — both confirmed
      from `pg_indexes`: `ix_embeddings_embedding_hnsw` and `ix_embeddings_local_embedding_hnsw`, each
      `USING hnsw (embedding vector_cosine_ops) WITH (m='16', ef_construction='64')`
- [x] SSE streaming verified: LLM tokens stream to frontend without WebSocket overhead — proven at
      three levels now. The six §7.4 event types and their order are asserted on the real stream
      (journey step 7, property Q-26). A defect that had made this untrue of the FRONTEND was found
      and fixed: `token` events carry no `path` — they cannot, since the file is unknown until
      `parse_artifacts` runs — and `GeneratorWizard` buffered only pathed tokens, so it dropped every
      one and rendered nothing, while every server-side test passed. And the browser half is now
      landed: `frontend/e2e/sse-paint.spec.ts` installs a `MutationObserver` before the run starts and
      recorded **149 strictly increasing text lengths** in `#stream-output` on a real generation —
      24, 48, 49, 51 … 493, 497 — with `status` first, sixty-odd `token` events, `progress` present,
      no `error`, and `complete` terminal. A `MutationObserver` rather than a timer because the
      earlier 500 ms sampler raced the stream: sampler and stream are independent clocks, so a
      shorter interval narrows the window without closing it. Watching the SSE frames instead would
      have proven only that bytes arrived, which was TRUE while the paint bug was live
- [x] Redis semantic caching operational: repeated LLM prompts return cached responses —
      `generation_runs` rows recording `served_from='l1'` for an identical prompt and `'l2'` for a
      near-duplicate above the 0.95 cosine threshold, both with `iterations_used = 0`. Also visible
      on the live path: a second journey run costs 1.4 min against 10.7

### Excluded (for this phase)

- ❌ Multi-environment management
- ❌ Docker/K8s management dashboards
- ❌ Deployment automation
- ❌ AI Command Center (NL commands)
- ❌ Monitoring/observability
- ❌ Self-healing
- ❌ Learning history

---

## Phase 2: Deploy, Manage, Observe & Self-Heal

**Goal:** Add deployment automation, environment management, Docker and Kubernetes dashboards, durable
workflows, the AI Command Center, notifications, GitOps and progressive delivery, the OTel two-tier
observability stack, AI troubleshooting and root-cause analysis, guard-railed self-healing, AI learning
memory, and knowledge base mode.

**Estimated Duration:** 18-26 weeks

> **This phase is the merger of the former Phase 2 (Deploy, Manage & Command — 74 boxes, 11
> subsections) and the former Phase 3 (Observe, Troubleshoot & Self-Heal — 52 boxes, 6 subsections).**
> The former Phase 4 became Phase 3 and the former Phase 5 became Phase 4. The merge is not a
> convenience: self-healing reads what the observability stack reports and acts through the deployment
> and rollback machinery, so shipping one without the other produces either a dashboard nothing acts on
> or an actor with nothing to read. 126 boxes became 123 — three pairs were combined into single boxes
> that satisfy both, and every combination is named where it appears. The duration is the honest sum of
> the two estimates rather than the larger of them.

### Build order

The order below is derived from what reads what, not from the section numbering. It is recorded here
because a phase this size is built over many sittings and the reasoning has to survive between them.

**Chosen order: 2.1 → 2.2 → 2.3 → 2.4 → 2.7a → 2.10 → 2.11 → 2.12, with 2.4a, 2.5, 2.6, 2.7, 2.8, 2.9,
2.13 and 2.14 interleaved where their prerequisites land.**

1. **2.1 Multi-Environment Management is first** because every other deliverable in the phase
   _references_ an environment. A deployment goes TO one, a promotion BETWEEN two, a rollback restores
   what one held, progressive delivery shifts traffic WITHIN one, and the Command Center's "deploy to
   staging" names one. Building any of those first would mean inventing a placeholder for the thing they
   all point at — and a placeholder on a runtime path is this repository's most-repeated defect. There is
   a second, sharper reason: `requires_approval` decides whether a later mutation needs a human. Every
   deployment guard in the phase inherits that value, so if it defaulted the wrong way, every one of them
   would inherit the hole.
2. **2.2 Deployment Automation** next, because it is the first actual mutation of the user's machine in
   this phase and therefore the first new chokepoint transit. The agent operations it needs — image build
   and push, manifest apply with health verification, OpenTofu apply — are the vocabulary 2.3, 2.4, 2.7a
   and 2.12 all call. Its durable-execution and circuit-breaker boxes belong WITH it rather than after:
   retry semantics chosen once a pipeline exists are retrofitted onto something that already assumed they
   were absent.
3. **2.3 Rollback & Release Timeline** third, because it reads deployment history, which does not exist
   until 2.2 writes it. Ordering it earlier would produce a timeline of nothing.
4. **2.4 Docker/Kubernetes Management** fourth. Its operation proxy is the same signing path 2.2 opens, so
   it is cheap once 2.2 is real and speculative before it. Its resource-utilisation view is the first
   place the "never reported / stale / healthy" tri-state must be got right, and 2.10's panels copy it.
5. **2.7a Argo Rollouts progressive delivery** fifth: it needs a deployment to make progressive and a
   rollback to abort into. Both arrive in 2.2 and 2.3.
6. **2.10 Observability (OTel, Prometheus/Mimir, Loki, Grafana)** sixth, and deliberately not earlier even
   though it is independent of 2.2. It is what 2.11 and 2.12 READ; building a reader before its source
   produces a diagnostic surface with nothing behind it, which is the shape that most invites a fabricated
   number.
7. **2.11 AI troubleshooting and RCA** seventh, because it reads 2.10's series and logs.
8. **2.12 Guard-railed self-healing is last, and last for a reason.** It needs BOTH chains: it reads what
   2.10/2.11 report and acts through 2.2/2.3's deployment and rollback machinery. It is also the only
   deliverable in the phase that mutates the user's infrastructure with no human in the loop for its safe
   tier, so it must be built when both the thing it reads and the thing it acts through are real and
   exercised — never against either one's placeholder. The two-tier split is a safety boundary, not a
   feature flag: which actions are safe has to be settled before anything auto-executes.

The remainder attach where their inputs land: **2.4a Inngest** with 2.2 (it IS the durable engine that
deliverable needs); **2.5 AI Command Center** after 2.2 and 2.4, since its supported commands are exactly
those operations and it must not grow a second path to them; **2.6 Notifications** after 2.2, so there is
an event worth sending; **2.7 ArgoCD**, **2.8 service mesh** and **2.9 local dev tools** after 2.2's agent
vocabulary exists; **2.13 AI learning history / Reflector** after 2.11, whose outcomes it learns from; and
**2.14 knowledge base mode** last, since it depends on nothing in the phase and blocks nothing in it.

### Deliverables

#### 2.1 Multi-Environment Management

- [x] Backend: Environment CRUD API (Dev, Test, Staging, Prod + custom) — revision `0023`, `src/environments/`,
      `GET/POST /projects/{id}/environments`, `PATCH`/`DELETE /{environment_id}`. `dump-openapi.py` reports 72
      paths (was 68); `check-route-auth.py` examined 91 routes across 76 paths and found every one behind a
      principal. Kinds are a closed set enforced by a database CHECK, not a free-text column.
- [x] Backend: Environment-specific variables, secrets, K8s contexts — `environment_variables` with a CHECK
      that a secret's clear column is NULL and a plain variable's sealed column is NULL, so the database
      refuses a row that claims protection and is readable. Sealing is AES-256-GCM under HKDF label
      `forgeops-environment-secret-v1` with the **environment id as the AAD**, asserted by
      `test_environments.py::test_a_sealed_value_does_not_open_under_another_environment`: a value lifted from
      staging's row into production's fails to open rather than decrypting into the wrong context. A read
      reports `value: null, is_secret: true` — withheld is distinguishable from absent.
- [x] Backend: Environment-specific approval requirements — `requires_approval`, defaulting to **true** in the
      schema and in the service, and a production environment cannot waive it at all (the refusal names
      `custom` as the alternative, because a refusal with no route forward reads as a bug). This is what
      finally gives `GovernanceChokepoint._evaluate_policy`'s `environment` argument a value: it has existed
      since Phase 1 with no caller ever supplying one. `requirement_for` returns **true for an unknown name**
      — a typo, a deleted environment or a Command Center instruction naming something that never existed all
      land on "ask a human". 13 tests in `tests/integration/test_environments.py`, all passing against the
      real database.
- [x] Backend: Promotion flows between environments — the rule (`promote_from`) and the DEPLOYMENT it
      authorises. `POST /projects/{id}/releases/promote` deploys the source's last STABLE manifest set
      to the next environment, and the TARGET's approval requirement governs — promoting into
      production asks a human even when staging did not, asserted in `test_releases.py`. Refuses, with
      the reason, when the source is last in the pipeline or has nothing stable: promoting an
      unverified deployment would carry a broken state forward under the word "promote". 7 tests.
      Superseded detail: the RULE was built and tested
      (`promote_from`, `GET /{environment_id}/promotion`): ordered by `position`, each environment promotes to
      the next, the target's approval requirement governs, a delete closes the gap so "the next one" cannot
      depend on deletion history, and the last environment refuses with a reason instead of succeeding
      emptily. The DEPLOYMENT a promotion authorises is 2.2 and is not built, so this box stays open: a
      promotion that cannot deploy is half the deliverable.
- [x] Frontend: Environment management UI — `features/environments/EnvironmentManager.tsx`, mounted on the
      project detail route. 14 tests in `__tests__/environments.test.tsx`; full suite 591 passed at
      95.29/84.2/91.94/95.63 against the 90/90/90/80 gate. Three properties are pinned rather than assumed:
      the approval consequence is rendered **in words** on every row and in every selector option (never as a
      bare checkbox state); "none configured" and "could not load" are different sentences; and an untouched
      approval waiver sends `null`, not `false` — the same shape on the wire and the opposite meaning. Its own
      test caught a defect in it: the variables panel rendered an empty list while loading, which reads as
      "this environment has no variables".
- [x] Frontend: Environment selector throughout dashboard — `EnvironmentSelector` is now mounted on
      **four** screens rather than one: the environment manager, the deployment dashboard, the Kubernetes
      dashboard (where the selection decides whether a scale or restart inherits an environment's approval
      requirement) and the project detail page that hosts them. That is what made this tickable — the claim
      is about the deployment, and the screens now exist. It is deliberately NOT on the Docker dashboard: a
      Docker action is against the operator's own machine, which no environment row describes, and offering
      a selector there would imply an environment governs it. 4 tests of its own, plus the Kubernetes suite
      asserting the selected environment reaches the request.

#### 2.2 Deployment Automation

- [ ] Agent: Container image build and push to registry (OCI-compliant) — not built. Separate credentials
      and separate failure modes, and a registry push produces the one value a deployment record should pin
      (an image digest); doing it badly would put a fabricated digest on a runtime path.
- [x] Agent: K8s manifest apply with health check verification — `agent/internal/executor/deployment.go`,
      operation `deployment.apply_manifests`, mutating and approval-required, `timeoutDeploy` 15 minutes.
      `kubectl` through an argument vector: no shell, and no `--prune` — pruning is an unbounded delete
      driven by a label selector nobody reviewed, and it belongs in 2.7's GitOps box where the diff is
      visible first. **The verification half is why this is one operation and not two**: it applies, then
      waits for every pod-bearing workload to converge, and reports `applied` or `degraded`, which are
      separate facts because "the API server accepted the objects" is not "anything is running". Only
      Deployment, StatefulSet and DaemonSet are waited on — `rollout status` on a Service exits non-zero,
      so treating one as waitable would fail every manifest set containing a Service. Tests in
      `deployment_test.go`; the whole agent suite passes `-race` at 73.5% statements against the 70 gate.
      **Its own test caught a real defect in it**: the first version put a 10-minute health wait inside a
      3-minute operation budget, which could never complete and would have reported "the operation timed
      out" instead of naming the workload and its replica count. **Now run against a real cluster on every
      push** — `deployment_cluster_test.go` against kind in `Kubernetes & SPIRE CI`, and locally in 57s:
      apply, real health verification, a real `degraded` from an image tag that cannot be pulled, a second
      `degraded` from a CPU request no node can satisfy, and a rollback that restores the previous image —
      each read back from the cluster with `kubectl get -o jsonpath` rather than from the operation's own
      report, because a report is this code's opinion and the point is to check the opinion against the
      object. The earlier "not yet run against a real cluster" caveat is therefore withdrawn.
- [ ] Agent: OpenTofu apply with state management — not built. State locking makes it a different problem
      from an idempotent `kubectl apply`, and `iac.Runner` still exposes no `apply`.
- [x] Backend: Deployment record CRUD — revision `0024`, `src/deployments/`,
      `GET/POST /projects/{id}/deployments`, `GET /{deployment_id}`, `GET /rollback-target`.
      `check-route-auth.py` examined 95 routes across 79 paths and found every one behind a principal;
      `dump-openapi.py` reports 75 paths. The row is written **before** the chokepoint is asked, so a
      deployment blocked at the gate still has a record saying so — writing it only on success is how
      "nothing happened and nothing says why" gets built. `healthy` is nullable deliberately: `null` means
      nothing verified it, `false` means the workloads were checked and were not ready.
- [x] Backend: Stable-state snapshot per successful deploy — `deployments.stable`, set on **health** and
      not on apply, with `ck_deployments_stable_implies_healthy` enforcing it below the service because the
      service is one writer and a support UPDATE is another. `GET /rollback-target` answers with the newest
      stable deployment, or `{"target": null}` and a reason. 16 tests in `test_deployments.py`, including
      one that applies a healthy deployment, then a degraded one, and asserts the rollback target is still
      the earlier healthy one — the invariant the whole of 2.3 rests on. A second report for a settled
      deployment is refused, because delivery is at-least-once and a redelivered `degraded` must not
      un-stable a row a rollback is targeting.
- [x] Backend: Live log streaming during deployment (SSE with `log` event type) — `src/deployments/logs.py`.
      `log` joined the closed SSE vocabulary (distinct from `progress`, which carries a percentage while
      a log line carries text). Revision `0026` records `change_sets.command_id` at delivery, which is
      what lets one deployment's stream find the agent's per-command Redis channel — and incidentally
      gives the audit chain a correlation it previously left to matching timestamps. An UNDELIVERED
      deployment is refused before a stream opens, because an empty stream reads as "nothing is
      happening" rather than "a human has not approved this". A keep-alive is a comment frame, not a
      `log` event, so an operator's log does not gain a blank line every fifteen seconds. 5 tests,
      including one holding the channel name equal to the hub's — if those drift the stream subscribes
      to nobody and renders an empty log for a working deployment, the quietest possible failure.
- [ ] Backend: **Durable execution** for deployment workflows (one durable engine at P2 - Inngest, or Temporal if replay/history demands; not a multi-hop migration)
- [ ] Backend: **Circuit breaker** pattern for deployment pipeline (fail-fast on validation errors) — not
      built. A breaker chosen before there is a pipeline to break is a guess about which failures repeat.
- [x] Frontend: Deployment dashboard with progress indicators —
      `features/deployments/DeploymentDashboard.tsx`, mounted on the project route and using
      `EnvironmentSelector`, so §2.1's selector is now on a second screen. 13 tests; full suite 604 passed
      at 95.3/84.07/92/95.63. **`degraded` is rendered as its own state**, and `healthy: null` as "no
      workload has been verified yet" rather than as a failure — an interface that showed those alike would
      tell an operator that a rollout still in flight had already failed. The rollback panel renders the
      absence of a stable state as an absence, with no button: offering a rollback to a degraded deployment
      would restore a broken state while reporting success.
- [x] Frontend: Deployment results with structured logs — `features/deployments/DeploymentResult.tsx`.
      The agent's report is rendered as its PARTS (applied objects, each workload's readiness and wait,
      the kubectl version) rather than as raw JSON, so the distinctions in it survive to the screen: a
      workload with `ready: false` and one with no entry read differently, and a deployment that applied
      with nothing waited on says so rather than looking successful. The live stream is opened only for
      a delivered deployment, and a failed stream is its own state — not silence.

#### 2.3 Rollback & Release Timeline

- [x] Backend: Deployment history with full version metadata — `GET /releases/timeline`, newest first,
      with the environment name, manifest set, digest, cluster context and both timestamps. `healthy`
      stays nullable on the wire so an in-flight rollout is not marked failed.
- [x] Backend: Diff between any two deployments (image, manifests, configs) — `GET /releases/diff`,
      comparing manifest sets, images (through the manifest set), cluster context, namespace and
      health. `identical_manifests` comes from the order-independent digest, so the same set deployed
      in a different order is not reported as a change.
- [x] Backend: Rollback to any previous deployment — `POST /releases/rollback`, a DEPLOYMENT through the
      chokepoint rather than a path of its own. Only a `stable` deployment may be a target, and one
      belonging to another environment is refused: rolling back to a degraded deployment would restore
      a broken state while reporting success. Verified against a real cluster in
      `deployment_cluster_test.go`, which applies, degrades, and rolls back to the previous image.
- [x] Frontend: Timeline visualization with deployment markers — `features/releases/ReleaseTimeline.tsx`,
      mounted on the project page. A rollback button exists only for a deployment the server called
      stable, and where it would be, an unstable deployment states why instead. 10 tests.
- [x] Frontend: Side-by-side deployment comparison — the same component; added, removed and unchanged
      manifests, and the health transition in words (`converged → did not converge`), which is the
      fact an operator comparing two releases is usually after. Asks for nothing until two DIFFERENT
      deployments are chosen.

#### 2.4 Docker Management Dashboard

- [x] Agent: Docker Engine API wrapper (containers, images, volumes, networks) — `docker.inventory` in
      `agent/internal/executor/docker.go`, read-only and approval-free so a panel can refresh without
      minting an approval per second. Through the `docker` CLI with an argument vector, not a Go SDK: the
      CLI already resolves the daemon socket, TLS material and context from the operator's own
      configuration, which is the same reason `kubectl` is right for the deployment operation — the agent
      reaches exactly what the operator can reach and no envelope widens it. **Verified against a real
      daemon**, not only decided: `docker_real_test.go` starts a container and reads it back (5 tests,
      14.2s locally, and on the Linux runner in `Kubernetes & SPIRE CI`). That is where three properties
      of the real tool were found, each of which would have produced a plausible, empty, wrong panel —
      `docker ps --format json` emits one object per LINE and not an array, `docker stats` blocks forever
      without `--no-stream`, and a locally built image's digest reads as the literal `no value`. Every
      measured field is nullable and `stats_sampled` says whether a sample was taken, so "not measured"
      and "idle" are distinguishable at the wire level rather than by convention.
- [x] Backend: **Agent operation proxy for Docker AND Kubernetes named operations** (via agent MCP server) — _combined box: former 2.4 "Docker operation proxy" + former 3.1 "K8s operation proxy", which were the same mechanism over two resource families. One whitelist, one signing path, one chokepoint transit for every mutating call; satisfies both._
      `backend/src/hostops/routes.py`, one module for both families, which is what the box asks for and
      the only arrangement that keeps its promise — a `docker/` and a `kubernetes/` module would be two of
      each within a week, and the second copy is where drift lands. Five routes; `check-route-auth.py`
      examined 100 routes across 84 paths and found every one behind a principal; `dump-openapi.py` reports
      80 paths (was 75). The reads go through a new `GovernanceChokepoint.read_inventory` and the writes
      through one `transit_host_action` covering all three mutating operations, so the six stages exist
      once rather than in three copies that can drift. Revision `0025` widens the change-set operation
      vocabulary; `alembic check` reports no pending diff. 13 integration tests in `test_hostops_proxy.py`
      against the real chokepoint and database, including the two that matter most: a mutating action
      leaves a change-set row carrying a blast-radius **verdict and score** (which only the analyser
      produces, so their presence is the evidence the stage ran), and a read leaves **no** change-set row
      and an **empty** `approval_id`. `check-chokepoint.sh` still reports both halves clean, so the read
      path did not become a second way to reach an agent.
- [x] Frontend: Container list with status, logs, resource stats — all three now. Logs arrive through
      `docker.container_logs`, a read bounded in BOTH dimensions (`--tail` and `--since`), and the
      applied bounds plus a `truncated` flag travel to the screen: an unmarked tail lets a reader
      conclude an error never happened when it fell off the top. The panel distinguishes a failed read
      from a quiet container in words.
- [ ] Frontend: Container create/start/stop/restart/delete — start, stop, restart and delete are built,
      tested and travel the chokepoint. **Create is deliberately absent**, and this is a decision rather
      than an omission: creating a container means choosing an image, ports, mounts and a privilege level,
      and a create operation that accepted those would be the bind-mount-and-`--privileged` authority this
      catalogue has carefully avoided. It belongs with a reviewed spec (a compose file, a manifest), not
      with a form. Recorded here so the next person does not add it casually.
- [ ] Frontend: Image list with build/pull/push/remove — list, pull and remove are built and tested.
      **Build and push are not**, and they are the §2.2 "container image build and push to registry" box:
      a push needs registry credentials and produces the one value a deployment record should pin, an
      image digest, so doing it badly would put a fabricated digest on a runtime path.
- [ ] Frontend: **Resource utilisation view** — live container CPU/memory/network from the Docker probe AND cluster/application series from the metrics tier, distinguishing "never reported" from "stale" from "healthy" — _combined box: former 2.4 "Live resource monitoring (CPU, memory, network)" + former 3.2 "Resource utilization charts". Two panels showing the same quantity from two sources is how a stale number gets read as a live one; satisfies both._
      The Docker-probe half is done, including the tri-state this box names: `freshnessOf` classifies a
      reading as never-reported, stale-with-its-age, or current, and an unparsable timestamp resolves to
      never-reported rather than to now — the one mistake that would make a stale panel look live. The
      **metrics-tier half does not exist**, because §2.10 is not built. Ticking this now would claim a
      comparison between two sources when only one is there.

#### 2.4a Inngest Integration (Deployment Workflows)

- [ ] Backend: Set up Inngest for event-driven durable function execution
- [ ] Backend: Define deployment pipeline as Inngest functions (build → push → apply → verify)
- [ ] Backend: Implement approval-gated stages in Inngest workflows
- [ ] Backend: Integration with the Phase 1 async task runner (Inngest can enqueue ARQ/Dramatiq fire-and-forget tasks where needed)
- [ ] Backend: Wrap business logic in orchestrator-agnostic functions ("thin wrapper" pattern)

#### 2.5 AI Command Center

- [ ] Backend: Intent classifier (router: deploy, diagnostic, generate, policy, chat)
- [ ] Backend: NL → structured command pipeline (function calling)
- [ ] Backend: Multi-agent orchestrator (deploy agent, diagnostic agent, etc.)
- [ ] Backend: Defense-in-depth guard-rails (5 layers)
- [ ] Frontend: Command input with autocomplete
- [ ] Frontend: Command results display (structured + chat)
- [ ] Frontend: Command history per session
- [ ] Supported commands: "Deploy to staging", "Show pods", "Check logs", "Scale to 3 replicas", "Generate Dockerfile"

#### 2.6 Notification Center (Basic)

- [ ] Backend: Novu integration for multi-channel notifications
- [ ] Backend: Notification templates (deploy completed, failed, policy violated)
- [ ] Frontend: Notification bell with dropdown
- [ ] Frontend: Notification preferences per user
- [ ] Integration: Slack webhook, Discord webhook, Email (SMTP)

#### 2.7 ArgoCD GitOps Integration

- [ ] Backend: ArgoCD Application manifest generation (AI creates App of Apps pattern)
- [ ] Agent: Support `argocd app sync` via subprocess
- [ ] Agent: ArgoCD ApplicationSet template generation
- [ ] Backend: ArgoCD webhook integration for auto-sync

#### 2.7a Argo Rollouts — Progressive Delivery

- [ ] Backend: **Argo Rollouts** integration for canary and blue-green rollouts (progressive delivery is NOT native to ArgoCD)
- [ ] Backend: Gate canary promotions on **error-rate AND latency** (analysis templates backed by OTel/Prometheus metrics)
- [ ] Backend: Automatic rollback on either signal breaching its threshold
- [ ] Frontend: Progressive rollout visualization (canary weight, metrics, promotion history)

#### 2.7b Service Mesh

- [ ] Infra: Prefer **Cilium** (eBPF, sidecarless, Hubble observability) for the 10k-agent self-host fleet — lowest-overhead option
- [ ] Infra: **Istio Ambient** as fallback when rich L7/multi-cluster is needed
- [ ] Note: Linkerd stable releases are behind a Buoyant subscription — avoid for an OSS-values project

#### 2.8 Local Development Tools

- [ ] Agent: Run tests (npm test, pytest, go test)
- [ ] Agent: Run linters
- [ ] Agent: Build project
- [ ] Agent: Run Docker locally
- [ ] Agent: Run DB migrations
- [ ] Backend: Dev-tools command proxy
- [ ] Frontend: Dev-tools panel in project dashboard

#### 2.9 Kubernetes Management Dashboard _(former 3.1)_

- [x] Agent: K8s API wrapper for pods, deployments, services, namespaces, ingress, ConfigMaps, HPA —
      `kubernetes.inventory` in `agent/internal/executor/kubernetes.go`, all seven families, read-only and
      approval-free. Each family is read INDEPENDENTLY and one that cannot be read is named in
      `partial_reasons` with its cause, because a cluster where the operator may list pods but not
      ingresses is ordinary and refusing the whole read would make the dashboard useless for them — while
      an empty array would say "this cluster has no ingresses", which is the opposite fact.
      **ConfigMaps report key names only**: a ConfigMap regularly holds what should have been a Secret, and
      a panel that printed values would publish it to every viewer of the project. Asserted by searching
      the RAW payload for a value planted in a real cluster, not by inspecting the decoded struct.
      **Verified against a real cluster** (`kubernetes_cluster_test.go`, kind, in CI on every push), which
      is where a real defect was found that no decision test could reach: kubectl writes its version-skew
      warning to stderr, the runner merges the streams, and the buffer is therefore not a JSON document —
      the first version ignored the parse failure and refused **every** inventory on any cluster whose
      version differed from the operator's client. The second version trimmed to the first brace and failed
      when the warning landed AFTER the document. It now decodes the first complete JSON value, and
      `TestDecodeFirstJSONSurvivesAWarningOnEitherSide` pins all four arrangements.
- [x] Frontend: Pod list with status, logs, events — all three. `kubernetes.pod_detail` returns logs AND
      events from ONE operation, which is the design rather than a convenience: a pod that never
      scheduled has no logs and its events are the entire explanation, so two operations would make the
      panel ask twice and then choose which absence to believe. The panel says "no log output — the
      events below are the explanation" for exactly that case. Readiness stays a ratio and the waiting
      reason stays surfaced.
- [x] Frontend: Deployment management (scale, restart, rollback) — all three, through
      `kubernetes.workload_action` and the chokepoint. Confined to the same three pod-bearing kinds the
      deployment operation can VERIFY, because acting on something unverifiable would report the API
      server's acceptance as success; a Job, a CronJob and a bare Pod are all refused by name.
      **A restart is `rollout restart`, not a pod delete** — deleting pods bypasses the workload's own
      surge and availability settings, which is how a restart becomes an outage — and the real-cluster test
      asserts the workload's `metadata.generation` CHANGES across a restart, which is the assertion that
      actually distinguishes the two implementations. A scale is bounded at 100 and the refusal names the
      bound. Each action waits for convergence and reports `applied` or `degraded`. Verified end to end
      against kind: scale to 2 (read back from the cluster), **scale to 0** — where
      `status.readyReplicas` is ABSENT rather than 0, which is exactly why every count is a pointer —
      restart, and rollback.
- [x] Frontend: Namespace explorer — `features/hostops/KubernetesDashboard.tsx`. The namespace list comes
      from the cluster and every panel below re-scopes to the selection, including the cache key, so one
      namespace's pods can never be served from another's entry. "Every namespace" is an explicit option
      rather than the absence of a choice.
- [x] Frontend: Cluster info and node status — context, server version, and each node's kubelet version
      and allocatable CPU and memory. **Readiness is a tri-state in words**: `ready: null` renders as "has
      not reported", not as "NotReady". Showing an unreported condition as NotReady would page somebody for
      a reporting gap, and showing it as Ready would hide a real outage; the test asserts the two strings
      differ. A cluster reporting no nodes at all is called out as a symptom rather than rendered as a
      blank table, because a cluster that answered has at least one.
- [x] Frontend: HPA configuration viewer — target, min, max and current, each through the same
      `not reported` rule, so an autoscaler whose status the API server has not populated is not displayed
      as one currently running zero replicas.

> The former 3.1 "Backend: K8s operation proxy (via agent MCP server)" is not missing: it is the
> combined box in 2.4. Scale, restart and rollback are mutations and travel the chokepoint.

#### 2.10 OTel-Native Monitoring (Two-Tier Deployment) _(former 3.2)_

- [ ] Deploy **OTel Collector two-tier architecture**:
  - **Tier 1 (Sidecar)**: Per-pod collectors — PII redaction, local buffering (`memory_limiter`), 50-100MB overhead
  - **Tier 2 (Gateway)**: Cluster-level collectors — **tail-based sampling** (stateful), load balancing (consistent hash), batch processing
- [ ] Backend: Configure OTel metrics, logs, traces instrumentation with **gen_ai.\* semantic conventions**
- [ ] Backend: Implement **hybrid sampling**: head-based 10% for routine traffic + tail-based 100% for errors/outliers
- [ ] Backend: Implement **per-tenant cost tracking** via OTel custom metrics (`gen_ai.cost.total`)
- [ ] Prometheus: Metrics storage and PromQL queries
- [ ] **Grafana Mimir**: Long-term metrics storage with retention policies
- [ ] Loki: Log aggregation
- [ ] Grafana: Embedded dashboards (data sources: Prometheus/Mimir + Loki + Tempo)
- [ ] Frontend: Unified monitoring dashboard with exemplar support
- [ ] Frontend: Infrastructure health overview
- [ ] Frontend: Application metrics (request rate, latency, errors) with trace correlation
- [ ] Frontend: **AI cost dashboard** per tenant per model

> The former 3.2 "Frontend: Resource utilization charts" is not missing: it is the combined
> resource-utilisation box in 2.4.

#### 2.11 AI Troubleshooting / Root-Cause Analysis _(former 3.3)_

- [ ] Backend: Incident ingestion from build failures, deployment errors, K8s events, logs
- [ ] Backend: AI-powered log analysis (Gemini 3 Flash for high throughput, through the §0.5 routing cascade)
- [ ] Backend: Root cause identification pipeline
- [ ] Backend: Fix suggestion generation (enters approval pipeline)
- [ ] Frontend: Incident list and detail view
- [ ] Frontend: RCA display (problem → location → fix)
- [ ] Frontend: Suggested fix with diff preview

#### 2.12 Self-Healing (Guard-Railed) _(former 3.4)_

- [ ] Backend: Health monitoring (failed containers, crash-looping pods, high resource usage)
- [ ] Backend: Two-tier action model
  - Safe: auto-execute (restart crashed container), logged + reported
  - Risky: require approval (rollback, scaling, config changes)
- [ ] Backend: AI post-incident summary generation
- [ ] Backend: Long-term recommendation generation
- [ ] Frontend: Self-healing activity log
- [ ] Frontend: Post-incident summary display

#### 2.13 AI Learning History (Per-Project Memory) _(former 3.5)_

- [ ] Backend: Feedback event logging (accepted/rejected suggestions)
- [ ] Backend: Two-tier memory architecture
  - Short-term: conversation history within session
  - Long-term: preference graph synthesized by Reflector Agent
- [ ] Backend: Periodic Reflector Agent that synthesizes Skill Files
- [ ] Backend: Skill file injection into LLM context
- [ ] Frontend: Learning history viewer (inspectable, editable)
- [ ] Frontend: Preference display (what AI has learned about this project)

#### 2.14 Knowledge Base Mode _(former 3.6)_

- [ ] Backend: Question-answering pipeline with RAG from codebase, deployments, incidents
- [ ] Implemented topics: "Explain this Dockerfile", "Explain this error", "Best practices for..."
- [ ] Always uses current project as example (not generic)

### Completion Criteria

- [ ] User can promote from dev → staging → production
- [ ] Deployment with real image build + push + apply works
- [ ] Rollback restores previous stable state
- [ ] Docker dashboard shows containers and stats
- [ ] AI Command Center understands "Deploy to staging" and executes
- [ ] Notifications sent on deploy complete/failure
- [ ] Local dev tools work (run tests, lint from dashboard)
- [ ] End-to-end test: scan project → deploy to staging → verify health → rollback
- [ ] Inngest workflows functional: deployment pipeline with approval gates completes end-to-end
- [ ] ArgoCD Application manifests generated and synced successfully
- [ ] K8s dashboard shows real pods, deployments, namespaces
- [ ] Metrics flowing from OTel → Prometheus → Grafana
- [ ] AI can analyze a failed deployment and identify root cause
- [ ] Failed container is auto-restarted (with log)
- [ ] AI generates post-incident summary
- [ ] AI learns from accepted/rejected suggestions (doesn't re-suggest rejected patterns)
- [ ] Knowledge base answers questions using project context
- [ ] End-to-end test: deploy → inject failure → AI detects → AI suggests fix → human approves
- [ ] Grafana Mimir storing long-term metrics with configured retention period
- [ ] Test coverage ≥ 75% — _combined criterion: former Phase 2 asked ≥ 70% and former Phase 3 asked ≥ 75%. Resolved to the STRICTER of the two, because a merged phase that shipped at the looser threshold would be a coverage reduction dressed as a merge. Backend, agent and frontend keep their existing higher gates (86%/77%/96-97-94-86); this is the phase floor, not a target._

### Excluded (for this phase)

Recomputed rather than concatenated. Every exclusion the former Phase 2 carried for Kubernetes
dashboards, monitoring, RCA, self-healing and learning history is **gone**, because all five are now
in-phase (2.9–2.13). What remains excluded is exactly what belongs to Phase 3 and Phase 4.

Deferred to Phase 3 (Scale, Collaborate & Polish):

- ❌ Visual pipeline designer
- ❌ AI architecture diagram generator
- ❌ Dependency health scanner (full, multi-ecosystem)
- ❌ Supply-chain & CI/CD security dashboard (VEX)
- ❌ Cost analysis (Infracost / Kubecost)
- ❌ Team collaboration — review requests, comment threads, full RBAC
- ❌ Backup & disaster recovery
- ❌ API Explorer
- ❌ Deployment analytics (DORA metrics)

Deferred to Phase 4 (Advanced & Ecosystem):

- ❌ Multi-agent collaboration and human orchestrator mode
- ❌ Air-gapped mode
- ❌ Backstage plugin
- ❌ Enterprise SSO (SAML/LDAP) and SOC2 compliance features
- ❌ "Deploy to…" one-click templates
- ❌ Platform SDK, webhooks and plugin architecture

---

## Phase 3: Scale, Collaborate & Polish

**Goal:** Add visual tools, team features, advanced analytics, and polish.

**Estimated Duration:** 12-16 weeks

### Deliverables

#### 3.1 Visual Pipeline Designer

- [ ] Frontend: React Flow integration for drag-and-drop pipeline editing
- [ ] Frontend: Stage nodes (Build, Test, Scan, Deploy, Health Check, Notify)
- [ ] Frontend: Connection edges between stages
- [ ] Frontend: YAML ↔ Visual round-trip (edit YAML → visual updates, edit visual → YAML updates)
- [ ] Backend: Pipeline YAML generator from visual graph
- [ ] Backend: GitHub Actions / Jenkins file export

#### 3.2 AI Architecture Diagram Generator

- [ ] Backend: Dependency graph builder from codebase index
- [ ] Backend: D2 markup generator from dependency graph + infra state
- [ ] Frontend: D2 rendering to SVG via D2 CLI (server-side)
- [ ] Frontend: Diagram types: architecture, ER, API flow, dependency graph, CI/CD flow
- [ ] Frontend: Export to SVG/PNG
- [ ] Frontend: Simple diagram editing (Mermaid fallback)

#### 3.3 Dependency Health Scanner (Full)

- [ ] Agent: Multi-ecosystem dependency parsing (npm, pip, Go, Maven, etc.)
- [ ] Agent: Trivy scan for vulnerabilities, outdated, deprecated, unused packages
- [ ] Backend: AI analysis of scan results
- [ ] Backend: Dependency update PR generation (Renovate-style)
- [ ] Frontend: Dependency health dashboard with filterable list
- [ ] Frontend: License compliance view

#### 3.4 Supply-Chain & CI/CD Security

- [ ] Agent: **SLSA Build Level 2+** compliance (signed provenance for every build)
- [ ] CI: Generate **CycloneDX SBOM** for every release via Syft
- [ ] CI: **Cosign keyless signing** via Sigstore (Fulcio/Rekor)
- [ ] CI: Push SBOM + attestations to **Rekor transparency log**
- [ ] CI: Run `go mod verify`, `pip-audit`, `pnpm audit` for dependency integrity
- [ ] CI: **Binary transparency** (all release artifacts logged to Rekor)
- [ ] Frontend: VEX (Vulnerability Exploitability Exchange) dashboard for end users

#### 3.5 Cost Analysis

- [ ] Backend: Infracost integration for pre-deployment cost estimation
- [ ] Backend: Kubecost integration for runtime cost visibility
- [ ] Backend: AI cost optimization suggestions
- [ ] Frontend: Cost estimate display pre-deployment
- [ ] Frontend: Cost savings recommendations with estimated savings

#### 3.6 Team Collaboration

- [ ] Backend: Full RBAC (Owner, Admin, Developer, Viewer)
- [ ] Backend: Cerbos integration for fine-grained permissions
- [ ] Backend: Review request system with threaded comments
- [ ] Backend: Approval history (who approved what, when)
- [ ] Frontend: Review request creation and management
- [ ] Frontend: Comment threads on diffs
- [ ] Frontend: Approval dashboard (pending, approved, rejected)

#### 3.7 Backup & Disaster Recovery

- [ ] Agent: Velero integration for K8s backup
- [ ] Agent: Docker volume backup script
- [ ] Backend: Backup schedule management with retention policies
- [ ] Backend: Workspace export (full platform state)
- [ ] Backend: Workspace restore from export
- [ ] Frontend: Backup management UI

#### 3.8 API Explorer

- [ ] Agent: Codebase scanning for API route definitions
- [ ] Backend: OpenAPI spec generation from scanned routes
- [ ] Frontend: Stoplight Elements integration for API viewer
- [ ] Frontend: GraphiQL for GraphQL APIs

#### 3.9 Deployment Analytics

- [ ] Backend: DORA metrics computation (deployment frequency, lead time, MTTR, change failure rate)
- [ ] Backend: Trend analysis (success rates, failure patterns)
- [ ] Frontend: Analytics dashboard with charts (ECharts)
- [ ] Frontend: Deployment timeline with success/failure indicators

### Completion Criteria

- [ ] Visual pipeline designer generates valid GitHub Actions YAML
- [ ] Architecture diagrams are generated from real codebase analysis
- [ ] Dependency health scanner finds vulnerabilities across ecosystems
- [ ] Cost estimates are shown before deployment
- [ ] Team members can review and approve changes
- [ ] Backups are scheduled and restorable
- [ ] API Explorer shows scanned API routes
- [ ] DORA metrics are displayed with trends
- [ ] End-to-end test: create pipeline visually → deploy → view analytics
- [ ] Test coverage ≥ 80%

### Excluded (for this phase)

Deferred to Phase 4 (Advanced & Ecosystem):

- ❌ Multi-agent team rooms (humans + AI collaborating)
- ❌ Air-gapped mode
- ❌ Backstage plugin
- ❌ Enterprise SSO (SAML/LDAP) and SOC2 compliance features
- ❌ "Deploy to…" one-click templates
- ❌ Platform SDK, webhooks and plugin architecture

---

## Phase 4: Advanced & Ecosystem

**Goal:** Enterprise features, ecosystem integration, and advanced AI capabilities.

**Estimated Duration:** Ongoing (post-launch)

### Deliverables

#### 4.1 Multi-Agent Collaboration

- [ ] Multiple AI agents working on same project with coordination
- [ ] Specialized agents: Analyzer, Generator, Safety Reviewer, Deployment Manager
- [ ] Human orchestrator mode

#### 4.2 Air-Gapped Mode

- [ ] Fully offline platform operation
- [ ] Local models only (Qwen3-Coder via Ollama)
- [ ] No cloud backend dependency

#### 4.3 Backstage Plugin

- [ ] Platform as a Backstage plugin
- [ ] Integration with Backstage catalog and software templates

#### 4.4 Enterprise SSO & Compliance

- [ ] SAML, LDAP integration
- [ ] SOC2 compliance features
- [ ] Audit export for compliance
- [ ] Data retention policies

#### 4.5 "Deploy to..." One-Click Templates

- [ ] Pre-built deployment profiles for popular stacks
- [ ] Next.js → Vercel, Django → Railway, Spring Boot → ECS

#### 4.6 Platform SDK

- [ ] REST API for third-party integration
- [ ] Webhook system for external triggers
- [ ] Plugin architecture for community extensions

### Completion Criteria

- [ ] Multiple AI agents can collaborate on a single project
- [ ] Platform operates fully offline with local models
- [ ] Backstage plugin is published
- [ ] Enterprise SSO works with major providers
- [ ] One-click deploy templates for 5+ popular stacks
- [ ] SDK is documented and usable
- [ ] Test coverage ≥ 85%

---

## Phase Dependency Graph

```
Phase 0: Foundation (Scaffolding + GoReleaser + MCP Gateway + Model Routing + Circuit Breaker)
    │
    ├──► Phase 0.5: Model Routing Config (Prerequisite for P1.5)
    │                (6 tiers, circuit breaker, fallback cascade, BYO-Key, semantic cache)
    │
    ▼
Phase 1: MVP Core (Analysis → Generation → Approval)
    │  └── P1.5 AI Generation depends on P0.5 Model Routing
    │
    ├──► Phase 2: Deploy, Manage, Observe & Self-Heal
    │          (Environments, Deployment, Rollback, Docker + K8s dashboards, Inngest, KEDA,
    │           Command Center, Notifications, ArgoCD, Argo Rollouts, Service Mesh, Dev Tools,
    │           OTel two-tier, Prometheus/Mimir/Loki/Grafana, RCA, Self-Healing, Learning, KB)
    │               │
    │               │  ORDER WITHIN THE PHASE, which is why the merge was made:
    │               │    2.1 Environments ─► 2.2 Deployment ─► 2.3 Rollback ─► 2.7a Progressive delivery
    │               │    2.10 Observability ─► 2.11 RCA ─► 2.12 Self-Healing ─► 2.13 Learning
    │               │    2.12 Self-Healing needs BOTH chains: it reads 2.10 and acts through 2.2/2.3
    │               │    2.7a canary gating reads 2.10's error-rate and latency series
    │               │    2.4a Inngest can start in parallel with 2.2
    │               ▼
    │          Phase 3: Scale & Polish (Visual Tools, Diagrams, Dependencies, Supply Chain,
    │               │                   Cost, Team, Backups, API Explorer, DORA Analytics)
    │               │  └── P3.4 Supply Chain depends on P0.2 GoReleaser foundation
    │               │  └── P3.9 DORA Analytics depends on P2.3 deployment history
    │               ▼
    │          Phase 4: Advanced (Ecosystem, Enterprise, Air-Gapped, MCP Apps)
    │
    └──► (Alternative path)
         Within Phase 2 the observability chain (2.9–2.14) can be started in parallel with the
         deployment chain (2.1–2.8) if observability is needed earlier; only 2.12 requires both.
         P3/P4 are sequential — each builds on the previous
```

**Key dependency notes:**

- P0.5 (Model Routing) is a **hard prerequisite** for P1.5 (AI Generation Pipeline with 6-tier routing)
- P0.2 (GoReleaser/Cosign/Syft/SLSA) is a **hard prerequisite** for P3.4 (Supply-Chain dashboard)
- P3.9 (DORA Analytics) depends on P2.3 (Deployment history) being available
- P2.4a (Inngest) can be started in parallel with P2.2 (Deployment Automation)
- P2.10 (OTel two-tier) depends on P2.4a (Inngest) for workflow orchestration
- P2.12 (Self-Healing) depends on P2.10 (what it reads) **and** on P2.2/P2.3 (what it acts through);
  it is the reason the two former phases are one phase

---

## Phase Risk Assessment

| Phase | Risk Level | Key Risks                                                                                                                                                                                           |                                                                                                                                                 Mitigation |
| :---: | :--------: | :-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------: |
|   0   |    Low     | Tooling incompatibility, CI configuration issues                                                                                                                                                    |                                                                                                                   Use well-established tools, pin versions |
|   1   |    High    | LLM output quality, validation loop reliability, security bugs                                                                                                                                      |                                                                                                 Extensive testing, validation-feedback loop, Plan Analyzer |
|   2   |    High    | Docker/K8s API complexity, deployment state management, telemetry pipeline complexity, self-healing safety — the merged phase carries both former risk profiles, and the highest of the two governs | Official SDKs, test with multiple orchestrators, two-tier action model, extensive dry-run testing, no auto-execution before the safe/risky split is proven |
|   3   |   Medium   | Feature scope creep, UI complexity                                                                                                                                                                  |                                                                                                               Clear scope boundaries, iterative UX testing |
|   4   |    Low     | Community adoption, plugin ecosystem                                                                                                                                                                |                                                                                                                                  engagement, documentation |

---

## Appendix A: Job Queue Evolution Strategy

| Phase        | Queue                                                | Rationale                                                                                                                                                                                                        |
| :----------- | :--------------------------------------------------- | :--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Phase 1**  | **ARQ or Dramatiq**                                  | Asyncio-native task runner for fire-and-forget AI tasks (analysis, generation); native fit for async FastAPI, no eventlet/gevent workaround (unlike Celery)                                                      |
| **Phase 2**  | **Inngest** (single durable engine, introduced once) | Async-native durable functions; ideal for approval-gated deployment/rollback/remediation pipelines; self-hostable                                                                                                |
| **Phase 3+** | **Temporal** (only if needed)                        | Stateful workflow-as-code; adopt ONLY if replay/history genuinely outgrows Inngest — not a planned migration. Phase 3 in the merged numbering, i.e. after the whole deploy-observe-heal phase has run on Inngest |

**Migration pattern:**

- Keep business logic in orchestrator-agnostic functions ("thin wrapper" pattern) from Day 1
- ARQ/Dramatiq handles P1 non-durable tasks; Inngest becomes the single durable engine at P2 — no multi-hop engine migration of the safety-critical path
- Temporal remains an optional escape hatch behind the same interface, never a scheduled rewrite

---

## Appendix B: Performance Optimization Checklist

- [ ] **pgvector HNSW indexes**: Default to HNSW (not IVFFlat) for all production vector search workloads
- [ ] **SSE for streaming**: Use Server-Sent Events for LLM token streaming (not WebSocket)
- [ ] **Redis semantic caching**: Tiered cache (exact-match → semantic → prefix) for LLM responses
- [ ] **Incremental scanning**: Dependency-graph-aware rescan (only changed files + dependants)
- [ ] **Connection pooling**: PgBouncer for PostgreSQL, Redis connection pool
- [ ] **CDN caching**: Static assets via CDN, API responses cached at edge where safe

---

_End of Phases Document — Build phases in order, complete each before starting the next._
