# Implementation Phases — AI-Powered DevOps Automation Platform

> **Purpose:** This document divides the project into 5 clearly bounded phases. Each phase has explicit deliverables, dependencies, and completion criteria. The AI IDE MUST build phases in order and must NOT skip ahead.

---

## Phase 0: Foundation & Project Scaffolding

**Goal:** Set up the entire project skeleton with working build pipelines and developer environment. No features yet — just infrastructure.

**Estimated Duration:** 2-3 weeks

### Deliverables

#### 0.1 Repository Structure
- [ ] Create monorepo layout as defined in PRD Section 8
- [ ] Set up `agent/`, `backend/`, `frontend/`, `docs/`, `.github/` directories
- [ ] Create root `Makefile` with common commands (build, test, lint, clean)
- [ ] Create root `docker-compose.yml` for full-stack development
- [ ] Create `.env.example` with all required environment variables
- [ ] Create `.gitignore` (Go binaries, Python caches, node_modules, .env, IDE files)
- [ ] Set up **pre-commit framework** with Gitleaks, Ruff, gofmt hooks

#### 0.2 Go Agent Scaffold
- [ ] Initialize Go module: `go mod init github.com/org/ai-devops-agent`
- [ ] Create `cmd/agent/main.go` — thin entry point
- [ ] Create `internal/` subdirectories: connection, docker, k8s, scanner, executor, validator, policy, fileops, iac, devtools, telemetry, mcp
- [ ] Use **constructor injection** pattern (not wire/uber-fx) for DI
- [ ] Implement **graceful shutdown** pattern with signal.NotifyContext + errgroup
- [ ] Add core dependencies: `github.com/coder/websocket` (WebSocket), `github.com/docker/docker/client` (Docker API), `k8s.io/client-go` (K8s API), `go.uber.org/zap` (logging), `github.com/spf13/cobra` (CLI), `github.com/fsnotify/fsnotify` (file watching), `github.com/minio/selfupdate` (auto-update), `github.com/sergi/go-diff` (diff generation), `github.com/mark3labs/mcp-go` (MCP server), `github.com/tree-sitter/go-tree-sitter` (AST parsing)
- [ ] Set up `golangci-lint` configuration
- [ ] Set up GitHub Actions CI for Go: lint + test + build
- [ ] Configure **GoReleaser** with Cosign signing + Syft SBOM + SLSA provenance
- [ ] Set up Cosign keyless signing configuration
- [ ] Set up Syft for CycloneDX SBOM generation per release

#### 0.3 Python Backend Scaffold
- [ ] Initialize FastAPI project structure in `backend/` using **domain-driven modular monolith** layout
- [ ] Set up `src/core/` with config, logging, **async database session management (expire_on_commit=False)**
- [ ] Create `src/main.py` with health check endpoint, **lifespan events**, **middleware stack**
- [ ] Set up PostgreSQL + pgvector in docker-compose
- [ ] Set up Alembic for migrations (including pgvector column detection)
- [ ] Set up **pytest + pytest-asyncio + httpx** for async integration tests (coverage >70% goal)
- [ ] Create Dockerfile with multi-stage build
- [ ] Set up `ruff` configuration (lint + format)
- [ ] Set up `pip-audit` in CI for dependency vulnerability scanning

#### 0.4 Next.js Frontend Scaffold
- [ ] Initialize Next.js 16 project with App Router
- [ ] Set up shadcn/ui with base theme
- [ ] Create layout with sidebar navigation, header, theme toggle
- [ ] Set up TanStack Query and Zustand
- [ ] Create **API client wrapper with RFC 9457 error handling**
- [ ] Set up `vitest` and `@testing-library/react`
- [ ] Set up **Playwright** for E2E testing
- [ ] Set up **k6** for load testing
- [ ] Set up **React Hook Form + Zod** as form handling standard
- [ ] Set up **pnpm** as the package manager
- [ ] Set up ESLint and Prettier

#### 0.5 MCP Gateway Integration (Core Architecture — Stateless, July 2026 Final Spec)
- [ ] Set up **MCP Gateway** in the backend using `Mcp-Method` + `Mcp-Name` header routing
- [ ] Implement **OAuth 2.1/OIDC auth** with `iss` parameter validation per RFC 9207
- [ ] Implement **OPA policy enforcement** at the gateway (filter tools by agent blast radius)
- [ ] Implement **TTL-based caching** on tool lists (respect `ttlMs` from servers)
- [ ] Implement **W3C Trace Context** propagation across all MCP server calls
- [ ] Create **base MCP server template** using `mark3labs/mcp-go` for Go agent tools
- [ ] Create **base MCP server template** in Python (FastAPI) for backend-hosted tools
- [ ] Implement **Tasks Extension** lifecycle (`tasks/get`, `tasks/update`, `tasks/cancel`)
- [ ] Implement **MCP Apps** support for sandboxed iframe UIs (approval forms, dashboards)

#### 0.6 GitOps Workflow (P0)
- [ ] Set up Git client library in the Go agent
- [ ] Implement PR creation flow (branch → commit → push → PR)
- [ ] Implement PR review status polling

#### 0.7 Plan Analyzer (P0)
- [ ] Create validation pipeline skeleton
- [ ] Implement semantic analysis module
- [ ] Connect validation pipeline to approval workflow

#### 0.8 OpenTofu Switch (P0)
- [ ] Install OpenTofu in Docker development environment
- [ ] Create OpenTofu runner module in Go agent (with timeout, output streaming, signal handling)
- [ ] Test `tofu validate` and `tofu plan` programmatic execution

#### 0.9 Model Routing Configuration (P0)
- [ ] Configure **model routing tier definitions** for all 6 tiers
- [ ] Model tiers: GPT-5.6 Sol (high/coding), Claude Fable 5 (high/analysis), Grok 4.5 (medium), Sonnet 5 & DeepSeek V4 (medium/value), Gemini 3 Flash (low/logs)
- [ ] Implement **fallback cascade** (primary → secondary → cross-vendor → self-hosted → template)
- [ ] Implement **circuit breaker** per model endpoint (5 failures in 30s → OPEN → HALF-OPEN after 60s)
- [ ] Implement **BYO-Key** architecture with Infisical vault for per-tenant LLM keys
- [ ] Implement **semantic caching** (L1 exact-match → L2 similarity >0.95 → L3 prefix cache via Redis)

### Completion Criteria
- [ ] `make build` succeeds for all three components
- [ ] `make test` passes (with placeholder tests)
- [ ] `make lint` passes
- [ ] `docker-compose up` starts all services
- [ ] Health check endpoint returns 200
- [ ] Frontend loads at localhost:3000
- [ ] Go binary compiles for Windows, macOS, Linux (amd64 + arm64)
- [ ] GoReleaser pipeline produces signed + SBOM-attested binaries
- [ ] Pre-commit hooks pass on all files
- [ ] MCP Gateway responds to `tools/list` and `tools/call` requests
- [ ] MCP Tasks lifecycle works (create → poll → cancel)
- [ ] OAuth 2.1/OIDC issuer validation blocks unauthorized requests
- [ ] Plan Analyzer returns results for sample input
- [ ] SQLModel models defined with pgvector column support (HNSW index)
- [ ] CycloneDX SBOM generated for Go agent build
- [ ] Cosign keyless signing verified on release artifact
- [ ] Model routing fallback cascade functions end-to-end
- [ ] Circuit breaker trips on simulated failures

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
> * `frontend/app/(shell)/projects/page.tsx` mapped every project to `readinessScore: 0`, a hardcoded
>   literal, so every project displayed a zero regardless of its real score. The list now reports
>   whether anything is indexed, in words; the score is computed once, on the detail screen.
> * the readiness screen's explanatory panel said the score was derived from stored settings "not from
>   a walk of its working tree" and described FIVE categories. Both had been false since the engine
>   moved to index-derived scoring — six categories, computed from `file_tree` and `file_contents`.
>   The home page's route list and `generation/service.py::_render`'s docstring had drifted the same
>   way and are corrected too.
>
> The remaining 84 boxes stay unticked, unchanged, for the reason given above.
>
> | § | State | Evidence, or the gap |
> |:--|:--|:--|
> | 1.1 Agent pairing & connection | verified end to end | The journey pairs a real agent over mTLS and runs signed commands. `session.heartbeat` is a notification, so liveness is a WebSocket **ping** rather than the inbound-silence timeout written below — that timeout dropped every healthy session on a 90-second cycle. |
> | 1.2 Multi-project workspace | verified, in a browser | Projects created and read through the API in journey steps 2 and 5, and now CREATED BY CLICKING in the onboarding walk's step 1. FR-01 through FR-05 all have a surface: a create form (a browser cannot report a directory's absolute path, so the path is typed and the form says why), server-side search, tags, per-user favourites, and archive plus an honest delete. **Favourites are per USER**, which needed a table: `projects.settings.favourite` has existed since revision `0009` and is per project, so in a tenant with two people one person starring a project would reorder the other's list. Delete counts thirteen cascading tables before it runs and reports them, and the audit rows SURVIVE — revision `0007` gave `audit_events.project_id` no foreign key for exactly that reason, and that intent is now asserted rather than only documented. |
> | 1.3 Codebase analysis engine | verified | A real scan of `backend/src` persists 141 files, 1857 dependency edges (243 resolved), 977 chunks; the fixture yields 7 files with genuine 1024-d BGE-M3 vectors. **Watch mode now exists and is proven live:** `forgeops-agent watch --project <id>` runs fsnotify → a real debounce → an incremental submit, and one edit of a module two files import gave `submitted 3 file(s) in the closure of depdemo/lib.js`. The fan-out's exact set is pinned deterministically — a change re-indexes the file, both direct importers and the TRANSITIVE one, and not the file that imports nothing. The agent self-triggers rather than waiting to be told, for the same reason `scan` is a verb: §2.2.1 confines `send_command` to `governance/`, so a backend-initiated re-index would be a governance decision per keystroke, and the agent already owns its workspace. Two defects were found by running it: `DebouncedWatcher` accepted a `debounceMs` it never read, and a DELETION produced an empty report the backend refused with 422 (a deleted file is absent from the fresh scan the closure is derived from, so finding its dependants would need the previous graph — deletions now trigger a full re-index). |
> | 1.4 Deployment readiness | verified | Scored from the index with `projects.settings` empty, over this section's six weighted categories. `settings` may only REFINE, through `ignore_globs`. The six categories now EXPAND in the browser into the individual checks behind each score, each naming the indexed path that satisfied it and FR-19's "why it matters" — a category at 40 with no visible evidence is indistinguishable from a bug in the scorer. Building it found that the two sides spell a category differently: the breakdown's keys are model field names (`containerization_score`) and a check's `category` is the category itself (`containerization`), so the panel rendered twelve rows for six categories until they were reconciled. Caught by the live onboarding walk, not by a fixture — the fixture carried the misconception. |
> | 1.5 AI generation & validation | verified, with a stated limit | `served_from` reaches `provider`, `l1` and `l2` on real calls, and the browser now observes 149 strictly increasing painted lengths (criterion 13). **The cascade and the circuit breaker are now proven across two GENUINELY SEPARATE live endpoints**, not against doubles: a second model server (`ollama-secondary`, its own container and port, sharing the weights volume) is registered as the `self_hosted` tier's secondary, and stopping the primary produced the full documented lifecycle — four attempts falling through `qwen3-coder-next:error → qwen3-coder-standby:success` with the breaker closed, the **5th failure inside 30 s opening it**, subsequent attempts recording `skipped(circuit_breaker_open)` with latency dropping 4.6 s → 0.7 s because no connection is attempted, **`half_open` after the 60 s cooldown**, and a successful probe closing it and returning traffic to the primary. `generation_runs.endpoint_id` records which endpoint answered. **The limit, stated rather than implied: only self-hosted endpoints have ever served a live call.** `LLM_KEY_*` are placeholders, so the five hosted vendor tiers remain unconfigured pending keys — the cascade is proven across real ENDPOINTS, not across vendors. |
> | 1.6 Change approval centre | verified | Journey steps 8–9, and 13 for revert. A blocked revert escalates to approval rather than being refused, which is what makes §3.6's `applied → reverted` edge reachable at all. REVERT IS NOW REACHABLE FROM THE UI, which it was not for two reasons rather than one: `ApprovalCenter` narrowed its mutation type to `approve|reject`, AND the list was filtered to `pending_approval`, so an applied change set never appeared on the screen at all. An escalated revert is presented as an outcome rather than an error — `approval-required` is registered at 202, inside the 2xx range, so the body arrives as a success and is narrowed explicitly. A per-project change history timeline explains all thirteen states, keeping `rolled_back` (an apply that undid itself) distinct from `reverted` (a deliberate reversal through the chokepoint). |
> | 1.7 Policy engine | verified, and one fabrication removed | 102 tests over OPA and the policy-evaluation surface, plus a full editor over the new `GET /api/v1/policies` — a list route that did not exist, which is why a policy screen was unbuildable and `/policies` was a read-only wall of templates. **`POST /policies/{id}/test` used to fabricate its verdict**: with no `opa` on PATH it returned `"allow" if input["action"] == "allow_me" else "deny"`, a decision on a security surface that no policy engine computed. It raises the registered `503 dryrun-unavailable` now, reports the query it evaluated and the evaluator's own version, and distinguishes an UNDEFINED rule from a deny. The test that covered it asserted the synthesised values, so it passed with or without OPA installed; the binary is now in the backend image and in the backend CI job, both by COPY from the digest-pinned image Compose already runs. The policy READ path was also not tenant-scoped — any authenticated caller could read, rewrite or delete another tenant's rules by id — and answers the non-disclosing 403 now. |
> | 1.8 Secret management | verified | Encryption at rest, redaction before LLM context, deploy-time injection; Q-12, Q-24, Q-28. The vault can now add, rotate and delete, and write-only is STRUCTURAL rather than promised: the value inputs are uncontrolled so no secret enters React state, the DOM node is cleared before the request is awaited so a failed write clears it too, and no response shape in that module carries a value in either direction. Deleting a reference does not reach into Infisical, and the confirmation says so — this platform does not own that store. |
> | 1.9 Audit logging | verified | Unbroken hash chain asserted in journey step 12. **Known limitation:** `GovernanceAction` is a closed vocabulary with no `applied` action, so an audit reader cannot ask "was this applied?" and must consult `change_sets` — Q-04 allows one row per transit. |
> | 1.10 Governance control plane | verified | Every mutation passes the chokepoint; `check-chokepoint.sh` proves it over both runtimes by parsing the import graph and reports "both halves clean". |
> | 1.11 Auth integration | verified | Real Authentik OIDC in journey step 1. The agent authenticates as a DEVICE on both factors, which `require_principal` cannot express. |

#### 1.1 Agent Pairing & Connection (JSON-RPC 2.0 over WSS)
- [ ] Implement **JSON-RPC 2.0 protocol** over WSS (structured `method`, `params`, `id`, `error` schema)
- [ ] Implement message types: `session.connect`, `session.heartbeat`, `command.execute`, `command.result`, `command.progress`, `approval.request`, `approval.response`, `agent.error`, `agent.status`
- [ ] Implement WSS connection manager with auto-reconnect (exponential backoff: start 1s, max 60s, jitter 0.5x)
- [ ] Implement mTLS + JWT authentication handshake
- [ ] Implement pairing code flow (6-char code → revocable device token, 5-min expiry)
- [ ] Implement heartbeat mechanism (every 30s, timeout after 90s)
- [ ] Implement command envelope protocol with `approval_id`, `policy_context`, `signature`
- [ ] Implement operation whitelist validation (named operations only — never arbitrary shell)
- [ ] Implement agent-side policy evaluation (defense in depth using OPA Wasm embedded)
- [ ] Implement **command envelope schema with HMAC-SHA256 signature** for integrity

#### 1.2 Multi-Project Workspace
- [ ] Backend: CRUD API for projects (import from GitHub, local path)
- [ ] Backend: Project settings (LLM budget, policies basic)
- [x] Frontend: Project list view with search, tags, favorites — `app/(shell)/projects/page.tsx`; every filter is a query parameter on `GET /projects`, asserted on the query string by `__tests__/project-workspace.test.tsx` because that is the only observable distinguishing server-side filtering from the browser-side version
- [x] Frontend: Project detail page — `app/(shell)/projects/[projectId]/page.tsx`, the first caller `GET /projects/{id}` has ever had; `__tests__/project-workspace.test.tsx::the project detail page`
- [ ] Frontend: Recent activity feed per project
- [ ] Agent: Register project directory, watch for changes

#### 1.3 Codebase Analysis Engine
- [ ] Agent: **Language detection** — tiered detection (package manager → extension → shebang → content heuristics)
- [ ] Agent: **Dependency graph builder** — resolve imports/requires across files for cross-file RAG
- [ ] Agent: Recursive file tree scanner (respects .gitignore + .dockerignore)
- [ ] Agent: File size/type filters (skip binaries >1MB, node_modules, .git)
- [ ] Agent: **Tree-sitter AST parsing** using `github.com/tree-sitter/go-tree-sitter` (official Go bindings)
- [ ] Agent: **cAST semantic chunking** — bottom-up grouping (statements → functions → classes), constraint-based splitting, density optimization
- [ ] Agent: **Metadata enrichment** — file path, function signature, class hierarchy, dependency references
- [ ] Agent: Generate vector embeddings using Voyage Code 3 (API) or BGE-M3 (local) with hybrid sparse-dense (BM25 + vector) indexing
- [ ] Agent: Store embeddings in pgvector via backend API (use **HNSW index** with tuned `ef_search`)
- [ ] Agent: **Cold start discovery mode** — lightweight heuristic analysis first, async full indexing in background
- [ ] Agent: **Watch mode** via fsnotify with fan-out/fan-in concurrency for incremental scanning
- [ ] Backend: Codebase Index API (CRUD for file tree, embeddings, symbol table)
- [ ] Backend: Implement **dependency-graph-aware incremental scanning** (re-index changed files + their dependants)

#### 1.4 Deployment Readiness Analysis
- [ ] Backend: Scoring engine with weighted categories
- [ ] Categories: Containerization, CI/CD, Orchestration, Env Config, Security, IaC
- [ ] Backend: Checklist checks (Dockerfile exists, multi-stage, non-root, etc.)
- [ ] Backend: Plain-language report generation with "why it matters"
- [ ] Frontend: Readiness score display (0-100) with radar chart
- [x] Frontend: Detailed category breakdown with expandable items — `features/readiness/ReadinessBreakdown.tsx`, each category expanding into its checks with the indexed path that satisfied it and FR-19's "why it matters"; `__tests__/route-pages.test.tsx::Readiness category breakdown`
- [ ] Frontend: Actionable recommendations list

#### 1.5 AI File Generation & Validation Pipeline
- [ ] Backend: AI engine with RAG from Codebase Index (hybrid sparse-dense retrieval)
- [ ] Backend: **6-tier model routing** with fallback cascade:
  - High: GPT-5.6 Sol (primary), Claude Fable 5 (backup) — architecture, multi-file generation
  - Medium: Grok 4.5, Claude Sonnet 5, DeepSeek V4 — Dockerfile, CI/CD, analysis
  - Low: Gemini 3 Flash — log analysis, formatting
  - Self-hosted: GLM-5.2, Qwen3-Coder-Next — air-gapped sensitive codebases
- [ ] Backend: **Circuit breaker** per model endpoint (5 failures/30s → OPEN → 60s → HALF-OPEN)
- [ ] Backend: **Fallback cascade**: Primary → Cross-vendor → Self-hosted → Safe Template Library
- [ ] Backend: Structured output schemas using Pydantic v2 strict mode
- [ ] Backend: Integration with MCP servers for tool access via **MCP Gateway**
- [ ] Backend: Use **SSE (Server-Sent Events)** with FastAPI native `EventSourceResponse` (in-tree since 0.139.2; no `sse-starlette` dependency) for streaming LLM token responses
  - Event types: `status`, `token`, `progress`, `validation`, `complete`, `error`
- [ ] Backend: Implement **tiered semantic caching** with Redis:
  - L1: Exact-match prompt hash → `GET`/`SET`
  - L2: Semantic similarity (>0.95) via Redis Vector Search
  - L3: Prompt prefix cache (system prompts, docs)
- [ ] Backend: **Safe Default Template Library** — hardcoded, verified templates for 8+ languages:
  - Node.js, Python, Go, Rust, Java/Kotlin, Ruby, PHP, .NET
  - Each: Dockerfile, K8s Deployment+Service+Ingress, GitHub Actions CI, Helm, OpenTofu
  - Used when AI fails after max 3 retries
- [ ] Backend: **Evaluation pipeline** for AI outputs:
  - Deterministic: syntax checks, schema validation, Trivy scan
  - Rubric (LLM-as-Judge): best practice compliance, security posture, cost efficiency
- [ ] Backend: **Cold start progressive UX** — show partial results as they become available
- [ ] Agent: Dockerfile validation (`docker compose config`)
- [ ] Agent: K8s manifest validation (`kubectl --dry-run=server`)
- [ ] Agent: OpenTofu validation (`tofu validate`, `tofu plan`)
- [ ] Agent: YAML schema validation (`yamllint` + JSON Schema)
- [ ] Agent: Helm validation (`helm lint`, `helm template --validate`)
- [ ] Backend: Validation-feedback loop (max 3 iterations, then safe template fallback)
- [ ] Backend: Plan Analyzer (semantic check on generated plans)
- [ ] Generated artifacts: Dockerfiles, docker-compose, K8s Deployments + Services
- [ ] Generated artifacts: GitHub Actions workflows, Helm charts
- [ ] Generated artifacts: OpenTofu configs, `.env.example`, README docs

#### 1.6 Change Approval Center
- [ ] Backend: Change-set CRUD API (create, validate, approve, reject, apply)
- [ ] Backend: Automatic timestamped backup before apply
- [ ] Backend: Atomic all-or-nothing change application
- [ ] Frontend: Diff preview (side-by-side and unified)
- [ ] Frontend: Approval/reject buttons with comment field
- [x] Frontend: Change history timeline per project — `features/approvals/ChangeHistoryTimeline.tsx`, with all thirteen §3.6 states explained (a test pins the set against the CHECK constraint revision `0010` added); `__tests__/codebase-and-history.test.tsx`
- [ ] Agent: Backup-before-mutate implementation
- [ ] Agent: Atomic file operations (transactional writes)

#### 1.7 Policy Engine (Basic)
- [ ] Backend: OPA integration for policy evaluation
- [ ] Backend: Policy CRUD API
- [ ] Backend: Pre-defined policy templates (scheduling, file restrictions)
- [ ] Agent: Mirror policy rules locally for zero-trust enforcement
- [x] Frontend: Policy list and editor UI — `features/policies/PolicyEditor.tsx` plus `app/(shell)/policies/page.tsx`, full CRUD over the new `GET /api/v1/policies`, with `opa check`'s own message rendered verbatim and a Test action reporting the decision, the query and the evaluator; `__tests__/policy-editor.test.tsx`
- [x] Frontend: Policy violation display with explanation — `components/ui/governance-refusal.tsx`, keyed on the stable RFC 9457 `type` and asserted an exact mirror of the registry's governance subset; `__tests__/governance-refusal.test.tsx`
- [ ] Implemented policies: "Never deploy on Fridays", "Never edit package.json", "Require approval for production"

#### 1.8 Secret Management (Basic)
- [ ] Backend: Integrate Infisical for encrypted secret storage
- [ ] Backend: Secret CRUD API (per project, per environment)
- [ ] Agent: Secret scanning during codebase analysis (Gitleaks)
- [ ] Agent: Secret redaction before LLM context
- [ ] Agent: Deploy-time secret injection (environment variables)
- [x] Frontend: Secret vault UI (add, edit, delete, list) — `features/vault/SecretVault.tsx`; write-only is structural (uncontrolled inputs, cleared before the request is awaited, no value field in either direction) and `__tests__/vault-write.test.tsx` asserts the shape rather than the screen

#### 1.9 Audit Logging
- [ ] Backend: Immutable audit log for all actions
- [ ] Fields: who, what, when, why, before/after state
- [ ] Frontend: Audit log viewer
- [ ] Ensure agent-side operations are also logged

#### 1.10 Agent Governance Control Plane (P1 Architecture)
- [ ] Backend: Implement **unified Governance Control Plane** — a single enforced chokepoint routing every mutating action through: policy evaluation → approval gate → change-set compilation → blast-radius check (Semantic Plan Analyzer) → audit record → rollback handle
- [ ] Backend: No agent mutation bypasses this layer — it is the trust moat
- [ ] Agent: OPA compiled to **Wasm** embedded in the Go agent binary for the agent-side half of the double policy evaluation (Cerbos v0.54.0 stays as the backend app RBAC sidecar)
- [ ] Agent: SPIFFE/SPIRE **X.509-SVID + mTLS** with attestation (namespace + service-account + image-digest) for workload identity — no long-lived agent keys; JWT-SVID only for crossing L7 proxies

#### 1.11 Auth Integration
- [ ] Set up Authentik or Keycloak container
- [ ] Implement OIDC/OAuth2 login flow
- [ ] Implement JWT token management
- [ ] Implement device/agent token flow
- [ ] Implement basic RBAC (admin, developer, viewer)

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

## Phase 2: Deploy, Manage & Command

**Goal:** Add deployment automation, environment management, Docker dashboard, AI Command Center, and notifications.

**Estimated Duration:** 8-12 weeks

### Deliverables

#### 2.1 Multi-Environment Management
- [ ] Backend: Environment CRUD API (Dev, Test, Staging, Prod + custom)
- [ ] Backend: Environment-specific variables, secrets, K8s contexts
- [ ] Backend: Environment-specific approval requirements
- [ ] Backend: Promotion flows between environments
- [ ] Frontend: Environment management UI
- [ ] Frontend: Environment selector throughout dashboard

#### 2.2 Deployment Automation
- [ ] Agent: Container image build and push to registry (OCI-compliant)
- [ ] Agent: K8s manifest apply with health check verification
- [ ] Agent: OpenTofu apply with state management
- [ ] Backend: Deployment record CRUD
- [ ] Backend: Stable-state snapshot per successful deploy
- [ ] Backend: Live log streaming during deployment (SSE with `log` event type)
- [ ] Backend: **Durable execution** for deployment workflows (one durable engine at P2 — Inngest, or Temporal if replay/history demands; not a multi-hop migration)
- [ ] Backend: **Circuit breaker** pattern for deployment pipeline (fail-fast on validation errors)
- [ ] Frontend: Deployment dashboard with progress indicators
- [ ] Frontend: Deployment results with structured logs

#### 2.3 Rollback & Release Timeline
- [ ] Backend: Deployment history with full version metadata
- [ ] Backend: Diff between any two deployments (image, manifests, configs)
- [ ] Backend: Rollback to any previous deployment
- [ ] Frontend: Timeline visualization with deployment markers
- [ ] Frontend: Side-by-side deployment comparison

#### 2.4 Docker Management Dashboard
- [ ] Agent: Docker Engine API wrapper (containers, images, volumes, networks)
- [ ] Backend: Docker operation proxy (via agent MCP server)
- [ ] Frontend: Container list with status, logs, resource stats
- [ ] Frontend: Container create/start/stop/restart/delete
- [ ] Frontend: Image list with build/pull/push/remove
- [ ] Frontend: Live resource monitoring (CPU, memory, network)

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
- [ ] Test coverage ≥ 70%

### Excluded (for this phase)
- ❌ K8s management dashboard
- ❌ Monitoring/observability dashboards
- ❌ AI troubleshooting / RCA
- ❌ Self-healing
- ❌ Learning history
- ❌ Visual pipeline designer

---

## Phase 3: Observe, Troubleshoot & Self-Heal

**Goal:** Add full observability, AI-powered troubleshooting, self-healing with guard-rails, and AI learning memory.

**Estimated Duration:** 10-14 weeks

### Deliverables

#### 3.1 Kubernetes Management Dashboard
- [ ] Agent: K8s API wrapper for pods, deployments, services, namespaces, ingress, ConfigMaps, HPA
- [ ] Backend: K8s operation proxy (via agent MCP server)
- [ ] Frontend: Pod list with status, logs, events
- [ ] Frontend: Deployment management (scale, restart, rollback)
- [ ] Frontend: Namespace explorer
- [ ] Frontend: Cluster info and node status
- [ ] Frontend: HPA configuration viewer

#### 3.2 OTel-Native Monitoring (Two-Tier Deployment)
- [ ] Deploy **OTel Collector two-tier architecture**:
  - **Tier 1 (Sidecar)**: Per-pod collectors — PII redaction, local buffering (`memory_limiter`), 50-100MB overhead
  - **Tier 2 (Gateway)**: Cluster-level collectors — **tail-based sampling** (stateful), load balancing (consistent hash), batch processing
- [ ] Backend: Configure OTel metrics, logs, traces instrumentation with **gen_ai.* semantic conventions**
- [ ] Backend: Implement **hybrid sampling**: head-based 10% for routine traffic + tail-based 100% for errors/outliers
- [ ] Backend: Implement **per-tenant cost tracking** via OTel custom metrics (`gen_ai.cost.total`)
- [ ] Prometheus: Metrics storage and PromQL queries
- [ ] **Grafana Mimir**: Long-term metrics storage with retention policies
- [ ] Loki: Log aggregation
- [ ] Grafana: Embedded dashboards (data sources: Prometheus/Mimir + Loki + Tempo)
- [ ] Frontend: Unified monitoring dashboard with exemplar support
- [ ] Frontend: Infrastructure health overview
- [ ] Frontend: Application metrics (request rate, latency, errors) with trace correlation
- [ ] Frontend: Resource utilization charts
- [ ] Frontend: **AI cost dashboard** per tenant per model

#### 3.3 AI Troubleshooting / Root-Cause Analysis
- [ ] Backend: Incident ingestion from build failures, deployment errors, K8s events, logs
- [ ] Backend: AI-powered log analysis (Gemini 3 Flash for high throughput)
- [ ] Backend: Root cause identification pipeline
- [ ] Backend: Fix suggestion generation (enters approval pipeline)
- [ ] Frontend: Incident list and detail view
- [ ] Frontend: RCA display (problem → location → fix)
- [ ] Frontend: Suggested fix with diff preview

#### 3.4 Self-Healing (Guard-Railed)
- [ ] Backend: Health monitoring (failed containers, crash-looping pods, high resource usage)
- [ ] Backend: Two-tier action model
  - Safe: auto-execute (restart crashed container), logged + reported
  - Risky: require approval (rollback, scaling, config changes)
- [ ] Backend: AI post-incident summary generation
- [ ] Backend: Long-term recommendation generation
- [ ] Frontend: Self-healing activity log
- [ ] Frontend: Post-incident summary display

#### 3.5 AI Learning History (Per-Project Memory)
- [ ] Backend: Feedback event logging (accepted/rejected suggestions)
- [ ] Backend: Two-tier memory architecture
  - Short-term: conversation history within session
  - Long-term: preference graph synthesized by Reflector Agent
- [ ] Backend: Periodic Reflector Agent that synthesizes Skill Files
- [ ] Backend: Skill file injection into LLM context
- [ ] Frontend: Learning history viewer (inspectable, editable)
- [ ] Frontend: Preference display (what AI has learned about this project)

#### 3.6 Knowledge Base Mode
- [ ] Backend: Question-answering pipeline with RAG from codebase, deployments, incidents
- [ ] Implemented topics: "Explain this Dockerfile", "Explain this error", "Best practices for..."
- [ ] Always uses current project as example (not generic)

### Completion Criteria
- [ ] K8s dashboard shows real pods, deployments, namespaces
- [ ] Metrics flowing from OTel → Prometheus → Grafana
- [ ] AI can analyze a failed deployment and identify root cause
- [ ] Failed container is auto-restarted (with log)
- [ ] AI generates post-incident summary
- [ ] AI learns from accepted/rejected suggestions (doesn't re-suggest rejected patterns)
- [ ] Knowledge base answers questions using project context
- [ ] End-to-end test: deploy → inject failure → AI detects → AI suggests fix → human approves
- [ ] Grafana Mimir storing long-term metrics with configured retention period
- [ ] Test coverage ≥ 75%

### Excluded (for this phase)
- ❌ Visual pipeline designer
- ❌ Architecture diagram generator
- ❌ Dependency health scanner
- ❌ Cost analysis
- ❌ Team collaboration (review requests)

---

## Phase 4: Scale, Collaborate & Polish

**Goal:** Add visual tools, team features, advanced analytics, and polish.

**Estimated Duration:** 12-16 weeks

### Deliverables

#### 4.1 Visual Pipeline Designer
- [ ] Frontend: React Flow integration for drag-and-drop pipeline editing
- [ ] Frontend: Stage nodes (Build, Test, Scan, Deploy, Health Check, Notify)
- [ ] Frontend: Connection edges between stages
- [ ] Frontend: YAML ↔ Visual round-trip (edit YAML → visual updates, edit visual → YAML updates)
- [ ] Backend: Pipeline YAML generator from visual graph
- [ ] Backend: GitHub Actions / Jenkins file export

#### 4.2 AI Architecture Diagram Generator
- [ ] Backend: Dependency graph builder from codebase index
- [ ] Backend: D2 markup generator from dependency graph + infra state
- [ ] Frontend: D2 rendering to SVG via D2 CLI (server-side)
- [ ] Frontend: Diagram types: architecture, ER, API flow, dependency graph, CI/CD flow
- [ ] Frontend: Export to SVG/PNG
- [ ] Frontend: Simple diagram editing (Mermaid fallback)

#### 4.3 Dependency Health Scanner (Full)
- [ ] Agent: Multi-ecosystem dependency parsing (npm, pip, Go, Maven, etc.)
- [ ] Agent: Trivy scan for vulnerabilities, outdated, deprecated, unused packages
- [ ] Backend: AI analysis of scan results
- [ ] Backend: Dependency update PR generation (Renovate-style)
- [ ] Frontend: Dependency health dashboard with filterable list
- [ ] Frontend: License compliance view

#### 4.4 Supply-Chain & CI/CD Security
- [ ] Agent: **SLSA Build Level 2+** compliance (signed provenance for every build)
- [ ] CI: Generate **CycloneDX SBOM** for every release via Syft
- [ ] CI: **Cosign keyless signing** via Sigstore (Fulcio/Rekor)
- [ ] CI: Push SBOM + attestations to **Rekor transparency log**
- [ ] CI: Run `go mod verify`, `pip-audit`, `pnpm audit` for dependency integrity
- [ ] CI: **Binary transparency** (all release artifacts logged to Rekor)
- [ ] Frontend: VEX (Vulnerability Exploitability Exchange) dashboard for end users

#### 4.5 Cost Analysis
- [ ] Backend: Infracost integration for pre-deployment cost estimation
- [ ] Backend: Kubecost integration for runtime cost visibility
- [ ] Backend: AI cost optimization suggestions
- [ ] Frontend: Cost estimate display pre-deployment
- [ ] Frontend: Cost savings recommendations with estimated savings

#### 4.6 Team Collaboration
- [ ] Backend: Full RBAC (Owner, Admin, Developer, Viewer)
- [ ] Backend: Cerbos integration for fine-grained permissions
- [ ] Backend: Review request system with threaded comments
- [ ] Backend: Approval history (who approved what, when)
- [ ] Frontend: Review request creation and management
- [ ] Frontend: Comment threads on diffs
- [ ] Frontend: Approval dashboard (pending, approved, rejected)

#### 4.7 Backup & Disaster Recovery
- [ ] Agent: Velero integration for K8s backup
- [ ] Agent: Docker volume backup script
- [ ] Backend: Backup schedule management with retention policies
- [ ] Backend: Workspace export (full platform state)
- [ ] Backend: Workspace restore from export
- [ ] Frontend: Backup management UI

#### 4.8 API Explorer
- [ ] Agent: Codebase scanning for API route definitions
- [ ] Backend: OpenAPI spec generation from scanned routes
- [ ] Frontend: Stoplight Elements integration for API viewer
- [ ] Frontend: GraphiQL for GraphQL APIs

#### 4.9 Deployment Analytics
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
- ❌ Multi-agent team rooms (humans + AI collaborating) — deferred
- ❌ Air-gapped mode — deferred
- ❌ Backstage plugin — deferred

---

## Phase 5: Advanced & Ecosystem

**Goal:** Enterprise features, ecosystem integration, and advanced AI capabilities.

**Estimated Duration:** Ongoing (post-launch)

### Deliverables

#### 5.1 Multi-Agent Collaboration
- [ ] Multiple AI agents working on same project with coordination
- [ ] Specialized agents: Analyzer, Generator, Safety Reviewer, Deployment Manager
- [ ] Human orchestrator mode

#### 5.2 Air-Gapped Mode
- [ ] Fully offline platform operation
- [ ] Local models only (Qwen3-Coder via Ollama)
- [ ] No cloud backend dependency

#### 5.3 Backstage Plugin
- [ ] Platform as a Backstage plugin
- [ ] Integration with Backstage catalog and software templates

#### 5.4 Enterprise SSO & Compliance
- [ ] SAML, LDAP integration
- [ ] SOC2 compliance features
- [ ] Audit export for compliance
- [ ] Data retention policies

#### 5.5 "Deploy to..." One-Click Templates
- [ ] Pre-built deployment profiles for popular stacks
- [ ] Next.js → Vercel, Django → Railway, Spring Boot → ECS

#### 5.6 Platform SDK
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
    ├──► Phase 2: Deploy & Manage (Environments, Docker, Command Center, Inngest, KEDA)
    │               │
    │               ▼
    │          Phase 3: Observe & Heal (OTel two-tier, K8s dashboard, Self-Healing, Learning)
    │               │  └── P3.2 OTel depends on P2.4a Inngest for workflow orchestration
    │               ▼
    │          Phase 4: Scale & Polish (Visual Tools, Team, Analytics, Cost, Backups)
    │               │  └── P4.4 Supply Chain depends on P0.2 GoReleaser foundation
    │               ▼
    │          Phase 5: Advanced (Ecosystem, Enterprise, Air-Gapped, MCP Apps)
    │
    └──► (Alternative path)
         P3 can be partially parallelized if observability is critical earlier
         P4/P5 are sequential — each builds on the previous
```

**Key dependency notes:**
- P0.5 (Model Routing) is a **hard prerequisite** for P1.5 (AI Generation Pipeline with 6-tier routing)
- P0.2 (GoReleaser/Cosign/Syft/SLSA) is a **hard prerequisite** for P4.4 (Supply-Chain dashboard)
- P4.9 (DORA Analytics) depends on P3.3 (Deployment history) being available
- P2.4a (Inngest) can be started in parallel with P2.2 (Deployment Automation)

---

## Phase Risk Assessment

| Phase | Risk Level | Key Risks | Mitigation |
|:---:|:---:|:---|---:|
| 0 | Low | Tooling incompatibility, CI configuration issues | Use well-established tools, pin versions |
| 1 | High | LLM output quality, validation loop reliability, security bugs | Extensive testing, validation-feedback loop, Plan Analyzer |
| 2 | Medium | Docker/K8s API complexity, deployment state management | Use official SDKs, test with multiple orchestrators |
| 3 | High | Telemetry pipeline complexity, self-healing safety | Two-tier action model, extensive dry-run testing |
| 4 | Medium | Feature scope creep, UI complexity | Clear scope boundaries, iterative UX testing |
| 5 | Low | Community adoption, plugin ecosystem | engagement, documentation |

---

## Appendix A: Job Queue Evolution Strategy

| Phase | Queue | Rationale |
|:---|:---|:---|
| **Phase 1** | **ARQ or Dramatiq** | Asyncio-native task runner for fire-and-forget AI tasks (analysis, generation); native fit for async FastAPI, no eventlet/gevent workaround (unlike Celery) |
| **Phase 2** | **Inngest** (single durable engine, introduced once) | Async-native durable functions; ideal for approval-gated deployment/rollback/remediation pipelines; self-hostable |
| **Phase 3+** | **Temporal** (only if needed) | Stateful workflow-as-code; adopt ONLY if replay/history genuinely outgrows Inngest — not a planned migration |

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

*End of Phases Document — Build phases in order, complete each before starting the next.*
