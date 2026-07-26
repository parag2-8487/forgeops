# PROGRESS

**Current phase:** Phase 0 — Foundation & Project Scaffolding
**Last updated:** 2026-07-26

This file is the project's durable progress record (design §18). It is updated in the same
commit as the work it describes, so a fresh clone answers "where are we?" without reading
history. Task statuses are exactly `done`, `in-progress` or `pending`; phase statuses are
exactly `completed`, `in-progress`, `not-started` or `blocked`.

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
| 0.1 | Repository Structure | monorepo layout per PRD §8 in the workspace root | done |
| 0.1 | Repository Structure | two-licence layout: root `LICENSE` FSL-1.1-ALv2, `agent/LICENSE` Apache-2.0, complete `agent/NOTICE` | done |
| 0.1 | Repository Structure | committed `.env.example` baseline and idempotent `scripts/init-env.sh` | done |
| 0.1 | Repository Structure | `.gitignore` plus pre-commit: gitleaks + ruff + gofmt + prettier + hygiene | done |
| 0.1 | Repository Structure | root `Makefile` initial targets and `bootstrap` toolchain verification | in-progress |
| 0.1 | Repository Structure | `docs/{architecture,api,development,deployment}.md` | done |
| 0.1 | Repository Structure | `docker-compose.yml` default profile | pending |
| 0.2 | Go Agent Scaffold | Go module `github.com/parag8487/ForgeOps/agent`, pinned deps, `.golangci.yml` | pending |
| 0.2 | Go Agent Scaffold | independent primitives: config, logging, telemetry, fileops, scanner watcher | pending |
| 0.2 | Go Agent Scaffold | transports and probes: WSS transport, docker/k8s doctor probes | pending |
| 0.2 | Go Agent Scaffold | final `internal/app` composition, Cobra CLI, graceful shutdown | pending |
| 0.2 | Go Agent Scaffold | GitHub Actions CI, GoReleaser + Cosign + Syft SBOM + SLSA provenance | pending |
| 0.3 | Python Backend Scaffold | `pyproject.toml` exact pins and hash-pinned locks | pending |
| 0.3 | Python Backend Scaffold | core primitives: config, logging, RFC 9457 errors, trace context, task seam | pending |
| 0.3 | Python Backend Scaffold | async DB engine/session, three SQLModel tables, `0001_initial` with pgvector HNSW | pending |
| 0.3 | Python Backend Scaffold | app factory, non-destructive lifespan, health and readiness probes | pending |
| 0.3 | Python Backend Scaffold | multi-stage Dockerfile and default Compose service | pending |
| 0.4 | Next.js Frontend Scaffold | package scaffold, exact pins, committed `pnpm-lock.yaml` | pending |
| 0.4 | Next.js Frontend Scaffold | shadcn primitives, providers, Zustand UI store, RHF + Zod standard | pending |
| 0.4 | Next.js Frontend Scaffold | RFC 9457-aware API client and validated public env contract | pending |
| 0.4 | Next.js Frontend Scaffold | accessible shell layout with one real Home link | pending |
| 0.4 | Next.js Frontend Scaffold | Vitest, Playwright shell spec, k6 health smoke, Dockerfile | pending |
| 0.5 | MCP Gateway Integration | registry, header routing, OIDC verification | pending |
| 0.5 | MCP Gateway Integration | OPA policy (`policies/mcp/gateway.rego`), Redis TTL tool cache, metadata resolver | pending |
| 0.5 | MCP Gateway Integration | `tools/list` and `tools/call` security-ordered paths | pending |
| 0.5 | MCP Gateway Integration | Tasks Extension state machine, MCP Apps sandbox hosting | pending |
| 0.5 | MCP Gateway Integration | Go and Python MCP server templates | pending |
| 0.6 | GitOps Workflow | Git/PR contracts and `TokenSource` seam | pending |
| 0.6 | GitOps Workflow | branch → commit → push → PR → poll flow | pending |
| 0.7 | Plan Analyzer | validation pipeline runner with syntax and schema stages | pending |
| 0.7 | Plan Analyzer | deterministic destructive-action and blast-radius analysis | pending |
| 0.7 | Plan Analyzer | approval seam and `POST /api/v1/analysis/plan` | pending |
| 0.8 | OpenTofu Switch | runner: bounded execution, streaming, signal propagation, env isolation | pending |
| 0.8 | OpenTofu Switch | null-provider fixture, six-platform lock, devtools image, `tools` profile | pending |
| 0.9 | Model Routing (a.k.a. "Phase 0.5" in the dependency graph) | six tiers and endpoint descriptor validation | pending |
| 0.9 | Model Routing (a.k.a. "Phase 0.5" in the dependency graph) | executable OpenAI-compatible endpoint adapter and registry | pending |
| 0.9 | Model Routing (a.k.a. "Phase 0.5" in the dependency graph) | circuit breaker | pending |
| 0.9 | Model Routing (a.k.a. "Phase 0.5" in the dependency graph) | tiered semantic cache | pending |
| 0.9 | Model Routing (a.k.a. "Phase 0.5" in the dependency graph) | BYO-key resolvers and the `vault` profile | pending |
| 0.9 | Model Routing (a.k.a. "Phase 0.5" in the dependency graph) | Redis/Lua per-caller token bucket and fallback cascade routes | pending |
| Progress record | Progress record | root `PROGRESS.md` durable progress record | in-progress |

## Completion criteria — Phase 0

| Criterion | Status | Evidence |
|:---|:---|:---|
| 1. `make build` succeeds for all three components | pending | — |
| 2. `make test` passes | pending | — |
| 3. `make lint` passes | pending | — |
| 4. `docker-compose up` starts all services | pending | — |
| 5. Health check endpoint returns 200 | pending | — |
| 6. Frontend loads at localhost:3000 | pending | — |
| 7. Go binary compiles for Windows/macOS/Linux × amd64+arm64 | pending | — |
| 8. GoReleaser produces signed + SBOM-attested binaries | pending | — |
| 9. Pre-commit hooks pass on all files | pending | — |
| 10. MCP Gateway responds to `tools/list` and `tools/call` | pending | — |
| 11. MCP Tasks lifecycle works (create → poll → cancel) | pending | — |
| 12. OAuth 2.1/OIDC issuer validation blocks unauthorized requests | pending | — |
| 13. Plan Analyzer returns results for sample input | pending | — |
| 14. SQLModel models defined with pgvector column support (HNSW index) | pending | — |
| 15. CycloneDX SBOM generated for the Go agent build | pending | — |
| 16. Cosign keyless signing verified on a release artifact | pending | — |
| 17. Model routing fallback cascade functions end-to-end | pending | — |
| 18. Circuit breaker trips on simulated failures | pending | — |

## Open questions requiring a decision

| # | Question | Blocking | Status |
|:---|:---|:---|:---|
| OQ-3 | Python logging library — no authority names one | no | stdlib `logging` + `dictConfig` + JSON formatter implemented, awaiting confirmation |
| OQ-4 | Property-based testing libraries (hypothesis / rapid / fast-check) | no | recommendation implemented, awaiting confirmation |
| OQ-6 | Windows process-tree termination for the OpenTofu runner | no | `taskkill /T /F` in Phase 0; Job Objects recorded as Phase 1 hardening |
| OQ-7 | PRD §6 mandates a GitHub App, but token minting is auth-adjacent | no | `EnvTokenSource` in Phase 0 behind `TokenSource`, awaiting confirmation |
| OQ-10 | D2 version `0.7+` (PRD §5) vs `2.x` (Tech-Stack) | no | deferred to the phase that adopts D2; no D2 dependency in Phase 0 |
| OQ-11 | `DEEP_RESEARCH_SYNTHESIS.md` is cited but absent from the workspace | no | PRD §2.1a and phases.md 0.5 used; nothing invented |
| OQ-13 | JWT/JWKS library for gateway OIDC verification | no | `pyjwt[crypto]` recommendation implemented, awaiting confirmation |
| OQ-15 | Include the nullable `tenant_id` seam in the initial migration? | no | included, nullable, with no RLS policies |
| OQ-16 | Durable engine choice (Temporal vs Inngest) at the P2 boundary | no | `TaskDispatcher` kept engine-neutral; decision deferred to Phase 2 |
| OQ-17 | Is backend >70 % coverage a Phase 0 gate or a goal? | no | reported goal in Phase 0, gate from Phase 1 |
| OQ-18 | Research §9 requires a build-rules document that does not exist | no | `docs/development.md` designated the build-rules home |
| OQ-20 | Source of the agent blast radius before agents exist | no | `MCP_AGENT_BLAST_RADIUS` (default `read_only`); Phase 1 derives it from attested identity |

## Decision log

| Date | Decision | Rationale | Authority |
|:---|:---|:---|:---|
| 2026-07-26 | D-14 — project is ForgeOps; Go module `github.com/parag8487/ForgeOps/agent` | real repo supersedes the `org` / `ai-devops-platform` placeholders; module path must match the repo exactly | design §17.1 D-14, §15.6 |
| 2026-07-26 | D-19 — repo licence `FSL-1.1-ALv2`; `agent/` `Apache-2.0` | BSL 1.1 would contradict the §E18 reasoning that drove the OpenTofu switch; FSL is fixed-text with a 2-year Apache conversion. `FSL-1.1-ALv2` is the registered SPDX id for that licence | design §17.1 D-19, NFR-31/32 |
| 2026-07-26 | D-1 — defer `tree-sitter/go-tree-sitter` to Phase 1 §1.3 | CGO conflicts with the `CGO_ENABLED=0` six-target build criterion; defers a dependency, not a capability | design §17.1 D-1, §15.7 |
| 2026-07-26 | D-2 — pgvector column 1536-d, HNSW, `model_id` provenance | matches Voyage Code 3; HNSW mandated by Research §0/§A0a; provenance keeps the Phase 1 multi-model options open | design §17.1 D-2 |
| 2026-07-26 | D-5 — `go-git/go-git/v5` + `google/go-github` for 0.6 | preserves the single-static-binary property; both permissively licensed; `TokenSource` seam untouched | design §17.1 D-5 |
