# Implementation Plan: Phase 0 — Foundation & Project Scaffolding

**Spec:** `phase-0-foundation`
**Project:** ForgeOps (`github.com/parag8487/ForgeOps`)
**Workflow:** design-first
**Planning authority:** `design.md`; no `requirements.md` is created or referenced.

## Overview

This plan converts the corrected Phase 0 design into incremental coding prompts. It stays inside Design §1.1, builds primitives before composition, adds optional Compose profiles only with their owning implementations, and finishes by wiring the already-implemented components together. No task introduces Phase 1+ product behavior.

## Plan-wide constraints

- The four authoritative root documents—`AI-Powered-DevOps-Platform-Complete-Technical-Research.md`, `PRD.md`, `Tech-Stack-Analysis.md`, and `phases.md`—are immutable inputs. Exclude them only from mutating hooks/formatters; continue scanning them with Gitleaks.
- Structural future directories contain only a non-code `README.md` or `.gitkeep`. Do not add exported placeholder types, `doc.go`, behavioral `__init__.py`, or package-docstring modules.
- Preserve decisions D-1, D-2, D-5, D-14, and D-19. In particular, `github.com/tree-sitter/go-tree-sitter` must not appear in the Phase 0 `agent/go.mod`.
- Use exact dependency versions, hash-pinned Python locks, digest-pinned images, SHA-pinned actions, and committed lockfiles.
- All 108 numbered executable leaves are mandatory, including property-based tests P-01 through P-15, unit/integration tests, default and optional-profile checks, release evidence, and completion-criteria evidence. Implementation tasks still land focused tests with their code.
- Completion-verification tasks may only execute or inspect behavior implemented earlier. They must not hide new implementation work; failures return to the owning task.

## Tasks

- [x] 1. Establish the repository boundary, licences, environment baseline, and durable records
  - [x] 1.1 Create the Phase 0 monorepo structure directly in the workspace root
    - Create the directories in Design §2.3 without a nested `ForgeOps/` directory. Track future-only Go, backend, and frontend directories with non-code `README.md` files or `.gitkeep` only.
    - Ensure `agent/internal/executor`, `validator`, `policy`, and `devtools` have no `.go` files; deferred backend domains have no `__init__.py`; `frontend/features` has no feature placeholder.
    - Add a repository-structure check that fails if a forbidden importable placeholder appears or if any authoritative root document is moved.
    - _Design: §0.3, §1.1–§1.3, §2.3; Deliverable: 0.1, 0.2, 0.3, 0.4_

  - [x] 1.2 Add the settled two-licence layout and project identity
    - Add root `LICENSE` as `FSL-1.1-ALv2`, `agent/LICENSE` as Apache-2.0, a complete base `agent/NOTICE` with the exact text from Design §2.4, and a root `README.md` that states the path-based licence split and two-year Apache conversion accurately.
    - Do not use `FSL-1.1-Apache-2.0`, describe the backend as open source, or leave TODO/stub/prospective-attribution text in `agent/NOTICE`.
    - Add an area-1 licence/identity check that validates only the artifacts created here: root `LICENSE`, `agent/LICENSE`, complete `agent/NOTICE`, and the root `README.md` identity/licence wording. Backend `pyproject.toml` metadata belongs to task 2.1, frontend `package.json` metadata belongs to task 6.1, and Go SPDX-header checks belong to task 3.1.
    - _Design: §0.3, §2.4, §16.6, §17.1 D-14, §17.1 D-19; Deliverable: 0.1, 0.2_

  - [x] 1.3 Add the committed environment baseline and idempotent initialization
    - Create `.env.example` with the complete Design §13.1 inventory and placeholder-only secret values; add `scripts/init-env.sh` using non-overwriting/noclobber semantics.
    - The script must create `.env` only when absent, treat a concurrent creator as success, and leave an existing `.env` byte-identical. `.env` remains an optional override.
    - Add a script-level test covering absent, repeated, pre-existing, and concurrent-create cases.
    - _Design: §2.2, §7.1, §13.1, §13.3; Deliverable: 0.1; Criterion: 4; Property: P-15_

  - [x] 1.4 Configure repository ignores and pre-commit hygiene
    - Add `.gitignore` for generated output, local `.env`, caches, and IDE state without ignoring licences, lockfiles, or the four authoritative documents.
    - Configure Gitleaks, backend-scoped Ruff/Ruff format, agent-scoped gofmt/go vet, Prettier, and hygiene hooks. Apply the top-level four-document exclusion only to mutating hooks; Gitleaks must still scan all four files.
    - Add configuration assertions for the exact exclusion set and for Gitleaks remaining unexcluded.
    - _Design: §0.3, §8.4, §14.1; Deliverable: 0.1; Criterion: 9_

  - [x] 1.5 Create the initial root Makefile and bootstrap contracts
    - Add `.PHONY` `help` as the default target, `bootstrap`, `init-env`, and `clean`; make `init-env` call `scripts/init-env.sh` and make `clean` preserve `.env`, lockfiles, and volumes.
    - `bootstrap` must verify the pinned toolchain, including Docker Compose 2.24.7 and `pip-tools==7.4.1`, without silently rewriting locks.
    - Add a Makefile contract check that enumerates targets and verifies destructive exclusions without running long-lived commands.
    - _Design: §13.4, §16.2, §16.4; Deliverable: 0.1_

  - [x] 1.6 Write the Phase 0 contributor and architecture documentation
    - Create `docs/architecture.md`, `docs/api.md`, `docs/development.md`, and `docs/deployment.md`; keep the four authoritative root documents untouched.
    - Document the scope boundary, licence split, GNU make/POSIX shell prerequisites, local-only Compose warning, health/readiness distinction, RFC 9457 contract, and the absence of general Phase 0 user authentication.
    - Add documentation checks for required warnings, route names, and licence identifiers.
    - _Design: §2.4, §4.2, §4.4, §14.2, §15.2, §17.2 OQ-18; Deliverable: 0.1_

  - [x] 1.7 Create the initial root `PROGRESS.md`
    - Use the exact sections and status vocabularies in Design §18. Include every deliverable 0.1–0.9, all 18 completion criteria with empty evidence fields, all non-blocking open questions, and decisions D-1/D-2/D-5/D-14/D-19.
    - Label 0.9 as the model-routing deliverable also called “Phase 0.5” in the source dependency graph.
    - Add a structure check that rejects missing deliverables/criteria, invalid statuses, or a missing decision row.
    - _Design: §15.3, §17, §18, Appendix E; Deliverable: Progress record_

  - [x] 1.8 Test the repository-boundary safeguards
    - Exercise the structure, area-1 licence/identity, `.gitignore`, hook-exclusion, environment-init, documentation, and `PROGRESS.md` checks added in tasks 1.1–1.7.
    - Prove the four authoritative documents are not rewritten by mutating hooks while Gitleaks still receives them, and prove no structural future directory exposes importable code. Validate only root `LICENSE`, `agent/LICENSE`, complete `agent/NOTICE`, and root `README.md` identity/licence wording here; do not require backend `pyproject.toml`, frontend `package.json`, or Go source metadata owned by tasks 2.1, 6.1, and 3.1.
    - _Design: §0.3, §1.3, §8.4, §18; Deliverable: 0.1, Progress record; Criterion: 9; Property: P-15_

- [x] 2. Establish backend packaging and independent core primitives
  - [x] 2.1 Define the exact backend package metadata and dependency source of truth
    - Create `backend/pyproject.toml` with `requires-python = ">=3.13,<3.14"`, exact `==` pins from Design §16.2, `pip-tools==7.4.1` as the lock generator, and `license = "FSL-1.1-ALv2"`.
    - Configure backend-scoped Ruff, async pytest, coverage reporting as a goal rather than a gate, and banned imports for cross-domain access and queue engines outside `src/core/tasks.py`.
    - Add metadata tests rejecting non-exact direct pins, an invalid Python constraint, disallowed dependencies, and a wrong licence expression.
    - _Design: §5.2, §7.6, §7.7, §7.9, §16.2; Deliverable: 0.3_

  - [x] 2.2 Add reproducible runtime and development lock workflows
    - Commit hash-pinned `requirements.lock` and `requirements-dev.lock` generated from `pyproject.toml`; runtime contains production dependencies only and dev contains runtime plus the dev extra.
    - Add `make lock-backend` with the two exact `pip-compile --generate-hashes` commands. Add a lock-freshness script that regenerates in an isolated location and fails on either diff.
    - Add checks proving Docker can consume only the runtime lock and CI can consume only the dev lock, both with `pip install --require-hashes`.
    - _Design: §7.7, §8.3, §13.4, §16.2; Deliverable: 0.3; Criterion: 1, 2, 3_

  - [x] 2.3 Implement strict project configuration loading without ambient-environment false positives
    - Implement `Settings`, `PROJECT_CONFIG_KEYS`, `load_project_dotenv`, and `get_settings(explicit=...)`. Validate and accumulate unknown keys from `.env.example`, optional `.env`, and explicit mappings, but read only declared names from the ambient OS environment.
    - Require a non-empty MCP issuer allowlist in production, validate all limiter/cache/model settings, and never return a partial settings object on error.
    - Add focused examples proving unknown project keys fail together while arbitrary `PATH`, `HOME`, CI, shell, and editor variables are ignored.
    - _Design: §7.1, §13.1, Appendix B P-15; Deliverable: 0.3; Property: P-15_

  - [x] 2.4 Implement structured logging and RFC 9457 problem primitives
    - Add contextvar-based JSON logging with required correlation fields and secret redaction. Implement `ProblemDetail`, `ProblemException`, stable ForgeOps type URIs, and handlers for validation, HTTP, domain, and unhandled errors.
    - Every renderer must set `application/problem+json`, match body status to HTTP status, include request path/trace id, and prevent exception text, bearer tokens, connection strings, keys, and PEM content from reaching clients.
    - Add unit tests for redaction, field presence, validation pointers, generic 500 details, and status/content-type equality.
    - _Design: §4.2, §7.2, §11.2, §14.4, Appendix C.1; Deliverable: 0.3; Property: P-09_

  - [x] 2.5 Implement backend W3C Trace Context primitives and middleware
    - Parse/validate `traceparent`, preserve `tracestate`, mint child span ids, inject outbound headers, expose contextvar accessors, and emit `traceresponse`; add only a `NoopTracer`, not an OTel SDK.
    - Malformed inbound values must start a fresh trace and must never be forwarded.
    - Add deterministic examples for valid preservation, child-span replacement, malformed reset, and exact tracestate pass-through.
    - _Design: §4.3, §7.8; Deliverable: 0.3, 0.5; Property: P-13_

  - [x] 2.6 Implement the engine-neutral task seam, SSE vocabulary, and core middleware
    - Add `TaskDispatcher`, `TaskHandle`, and a real `InlineDispatcher`; add the six-value SSE event enum using FastAPI’s native support and no `sse-starlette` dependency.
    - Add request-id/access-log middleware and a middleware-order probe preserving `ServerError → RequestId → TraceContext → AccessLog → CORS`, with the Phase 1 tenant insertion point documented.
    - Add tests that `InlineDispatcher` executes once in process, queue imports are rejected outside the seam, SSE values are exact, and middleware order is stable.
    - _Design: §4.3, §7.4, §7.9, §16.2; Deliverable: 0.3_

  - [x] 2.7 Run backend-core unit tests
    - Run the focused tests from tasks 2.1–2.6 with no database or Redis requirement.
    - Cover exact pins and locks, project-source strictness versus ambient-env tolerance, redaction, RFC 9457 rendering, trace reset/propagation, task dispatch, banned imports, SSE vocabulary, and middleware order.
    - _Design: §4.2–§4.3, §7.1–§7.9, §16.2; Deliverable: 0.3; Property: P-09, P-13, P-15_

- [x] 3. Build independent Go primitives before any application composition
  - [x] 3.1 Initialize the Go 1.26 module and structural package boundary
    - Create `agent/go.mod` with module `github.com/parag8487/ForgeOps/agent`, Go 1.26, exact pins for the Design §16.1 dependencies, committed `go.sum`, and `.golangci.yml`.
    - Add Apache-2.0 SPDX headers to Go files. Keep structural-only directories non-code and assert `github.com/tree-sitter/go-tree-sitter` is absent from both direct and transitive Phase 0 declarations.
    - Add a module-policy check for the exact module path, read-only module mode, SPDX headers, forbidden placeholder packages, and the tree-sitter exclusion.
    - _Design: §1.2–§1.3, §2.4, §10.1, §16.1, §17.1 D-1, §17.1 D-14; Deliverable: 0.2; Criterion: 7_

  - [x] 3.2 Implement the typed Go configuration loader
    - Implement `Config`, nested Tofu/Git/MCP configuration, and `Load(getenv)` with defaults, typed parsing, ForgeOps-key-only validation, and one joined error containing every problem.
    - Ignore unrelated ambient OS keys and never return a partial config with an error.
    - Add table examples for defaults, combined failures, invalid URLs/durations/enums, and unrelated environment variables.
    - _Design: §7.1, §10.3, Appendix B P-15; Deliverable: 0.2; Property: P-15_

  - [x] 3.3 Implement Go structured logging and redaction
    - Add independent zap construction for console/development and JSON/production modes with the required fields and logger naming.
    - Add a redacting core/filter for bearer tokens and configured secret values before encoding.
    - Add tests for level filtering, both formats, component fields, and absence of injected secrets in captured output.
    - _Design: §7.2, §10.3, §14.4; Deliverable: 0.2_

  - [x] 3.4 Implement Go trace propagation and the telemetry seam
    - Implement traceparent validation, tracestate preservation, child span generation, context helpers, and outbound header injection with only `NoopTracer` as the Phase 0 tracer.
    - Do not add OTel SDK/exporter/sampling dependencies.
    - Add examples for valid/invalid headers and child-context generation.
    - _Design: §7.8, §10.1; Deliverable: 0.2, 0.5; Property: P-13_

  - [x] 3.5 Implement atomic file operations, path guards, backups, and unified diffs
    - Implement `ApplyAtomic` with full pre-validation, symlink-aware root containment, blocklist enforcement, backup-before-mutate, same-directory temp files, fsync/rename, and reverse rollback on any failure.
    - Implement `UnifiedDiff` using `sergi/go-diff`; no caller may bypass the root/blocklist boundary.
    - Add deterministic failure-injection examples for traversal, symlink escape, each blocked path class, partial-write rollback, and idempotent content.
    - _Design: §10.10, Appendix A.5, Appendix C.2; Deliverable: 0.2; Property: P-08_

  - [x] 3.6 Implement the real fsnotify watcher seam
    - Add the consumer-owned `Watcher` interface and a real fsnotify implementation supporting create/modify/delete events and context-driven close.
    - Do not add AST parsing, cAST behavior, or a tree-sitter dependency.
    - Add temp-directory tests for event delivery, shutdown, and no events after close.
    - _Design: §1.2–§1.3, §10.9, §15.7, §17.1 D-1; Deliverable: 0.2_

  - [x] 3.7 Exercise self-update signature verification without implementing auto-update
    - Add a signed fixture and embedded test public key that exercise `minio/selfupdate` verification only; do not add download, replacement, restart, or scheduling behavior.
    - Test valid, tampered-content, and wrong-key cases.
    - _Design: §1.2, §10.9; Deliverable: 0.2_

  - [x] 3.8 Write property test P-08 for atomic change sets
    - Generate change sets and injected failure points; prove all-new-with-backups or exact pre-image restoration, root confinement, blocklist rejection, and content idempotence.
    - _Design: §10.10, Appendix A.5, Appendix B P-08; Deliverable: 0.2; Property: P-08_

  - [x] 3.9 Write cross-runtime property test P-13 for trace context
    - In Go and Python, generate valid and malformed headers; prove valid trace ids persist with new span ids, malformed input starts fresh, and tracestate is unchanged.
    - _Design: §7.8, Appendix B P-13; Deliverable: 0.2, 0.3, 0.5; Property: P-13_

  - [x] 3.10 Write cross-runtime property test P-15 for configuration strictness
    - Generate project-owned mappings with unknown/missing/invalid ForgeOps keys and ambient environments with unrelated keys; prove aggregated strict failure for the former and tolerance for the latter in Go and Python.
    - _Design: §7.1, Appendix B P-15; Deliverable: 0.2, 0.3; Property: P-15_

  - [x] 3.11 Run Go primitive tests and module-boundary checks
    - Run config, logging, telemetry, fileops, watcher, and signature-fixture tests with race/shuffle settings where applicable.
    - Confirm no test imports a structural-only package and the module-policy check still rejects tree-sitter.
    - _Design: §7.6, §10.9, §16.1, §17.1 D-1; Deliverable: 0.2; Property: P-08, P-13, P-15_

- [x] 4. Add only the default data-plane services and gateway policy
  - [x] 4.1 Create the initial Compose data plane
    - Create `docker-compose.yml` with project name `forgeops` and only `postgres`, `redis`, and `opa` at this stage, using digest-pinned images, named volumes, loopback port bindings, and Design §13.3 health checks.
    - Load committed `.env.example` as required and `.env` as optional for every service; give all interpolation expressions safe defaults.
    - Do not declare `backend`, `frontend`, `infisical`, `agent-dev`, `vault`, or `tools` yet. Add a static Compose assertion for this exact staged service set.
    - _Design: §2.2, §13.3, §14.2, §16.4; Deliverable: 0.1, 0.3, 0.5_

  - [x] 4.2 Implement the Phase 0 OPA gateway policy
    - Add `policies/mcp/gateway.rego` with radius ordering, highest-risk fallback for unknown metadata, per-tool filtering, and call allow rules.
    - Add Rego tests for read-only/workspace/infrastructure radii, unknown annotations, absent tools, and deny-by-default behavior.
    - _Design: §5.3–§5.4, §11.4, §17.2 OQ-20; Deliverable: 0.5_

  - [x] 4.3 Validate the staged data plane and policy
    - Validate Compose configuration without starting a watcher or long-running process; assert the staged three-service set, optional-env semantics, loopback bindings, pinned images, and health-check definitions.
    - Run the OPA policy unit tests and prove unknown tools do not gain lower blast radius.
    - _Design: §11.4, §13.3, §14.2; Deliverable: 0.1, 0.5_

- [x] 5. Implement backend persistence, initial schema, health semantics, and container
  - [x] 5.1 Implement async database/session primitives
    - Add the async engine and sessionmaker with `expire_on_commit=False`, `autoflush=False`, naming conventions, request-scoped commit/rollback, `SET LOCAL hnsw.ef_search`, and the PgBouncer transaction-mode note.
    - Construction must not require a successful database connection.
    - Add tests for post-commit attribute access, rollback on exceptions, naming conventions, and transaction-scoped `ef_search`.
    - _Design: §6.3–§6.5, §11.3; Deliverable: 0.3_

  - [x] 5.2 Implement the three Phase 0 SQLModel tables
    - Add `Project`, `FileTreeEntry`, and `Embedding` only, including nullable tenant seams, cascade foreign keys, deterministic constraint names, `EMBEDDING_DIMS = 1536`, required `model_id`, and `Vector(1536)`.
    - Do not add identity, approval, deployment, policy, or other future tables.
    - Add model-introspection tests for table/column/constraint names, nullability, cascades, and vector dimension.
    - _Design: §6.1–§6.3, §6.5, §17.1 D-2; Deliverable: 0.3; Criterion: 14_

  - [x] 5.3 Configure Alembic and write the only Phase 0 migration
    - Add async Alembic configuration with SQLModel metadata, type comparison, and pgvector render support. Write only `0001_initial`.
    - Create the vector extension before vector columns and the HNSW cosine index with `m=16`, `ef_construction=64`; do not add RLS policies or additional migrations.
    - Add migration-shape tests and a no-change autogenerate check that prevents spurious vector recreation.
    - _Design: §6.2–§6.4; Deliverable: 0.3; Criterion: 14_

  - [x] 5.4 Test the initial schema against PostgreSQL
    - Upgrade a real PostgreSQL/pgvector database, assert extension/version, three-table scope, `vector(1536)`, required `model_id`, HNSW/cosine index, and clean downgrade.
    - Run a transaction-local `hnsw.ef_search` round trip and verify a no-change migration diff.
    - _Design: §6.2–§6.4, §16.4, Appendix E criterion 14; Deliverable: 0.3; Criterion: 14_

  - [x] 5.5 Implement the backend app factory, non-destructive lifespan, and probes
    - Implement unversioned `/health`, `/health/ready`, and versioned `/api/v1/health`, middleware registration, problem handlers, and non-destructive construction of engine/session/Redis/HTTP clients.
    - `/health` must perform no dependency I/O and remain 200 during PostgreSQL/Redis outages. Lifespan must not abort solely because those dependencies are unreachable and must contain no eager startup-failing `SELECT 1` or Redis `PING`.
    - `/health/ready` must run independent 2-second PostgreSQL/Redis checks and return RFC 9457 503 with one error item per failed/timed-out dependency. Add tests for healthy, failed, and timed-out checks.
    - _Design: §4.3–§4.4, §11.1, Appendix C.1; Deliverable: 0.3; Criterion: 5; Property: P-09_

  - [x] 5.6 Test lifespan and health during dependency loss and recovery
    - Start the ASGI lifespan with unreachable PostgreSQL and Redis endpoints and prove startup yields, `/health` and `/api/v1/health` remain 200, and `/health/ready` is RFC 9457 503 naming both failures.
    - Restore dependencies and prove readiness becomes 200 without restarting the process; separately prove invalid local configuration still prevents startup.
    - _Design: §4.4, §11.1, Appendix C.1, Appendix E criterion 5; Deliverable: 0.3; Criterion: 5; Property: P-09_

  - [x] 5.7 Containerize the backend and add its default Compose service
    - Create a multi-stage backend Dockerfile that installs only `requirements.lock` with `--require-hashes`, runs as non-root, and exposes the runtime target.
    - Add `backend` to Compose using both env files, liveness-only `/health` container health check, and non-readiness-gating dependency construction; keep optional services absent.
    - Add image/config tests proving the dev lock is absent from the runtime image and dependency outage does not make the liveness health check fail.
    - _Design: §4.4, §7.7, §13.3, §16.2; Deliverable: 0.3; Criterion: 4, 5_

- [x] 6. Implement the accessible frontend shell, client contracts, and container
  - [x] 6.1 Scaffold the exact frontend package and build configuration
    - Create the Next.js 16/React 19/TypeScript/pnpm project with exact package pins, committed `pnpm-lock.yaml`, `private: true`, and `license: FSL-1.1-ALv2`.
    - Configure Tailwind v4, ESLint, Prettier, Vitest run mode, Testing Library, Playwright, fast-check, and Zod validation for both public build variables. Create neither `middleware.ts` nor `proxy.ts`.
    - Add package-policy tests for exact pins, licence, frozen-lock compatibility, forbidden future UI dependencies, and no middleware/proxy file.
    - _Design: §12.1, §16.3; Deliverable: 0.4_

  - [x] 6.2 Implement shadcn primitives, providers, UI state, and the form standard
    - Add only the Design §12.1 primitives, theme/query providers, and a Zustand store containing client-only shell state; server-derived data must remain in TanStack Query.
    - Add the React Hook Form + Zod helper pattern and RFC 9457 field-error-to-`setError` mapping without shipping a user-facing feature form.
    - Add tests for provider retry policy, theme state, store boundaries, persistence, and JSON-pointer field mapping.
    - _Design: §12.1, §12.4–§12.5; Deliverable: 0.4_

  - [x] 6.3 Implement the RFC 9457-aware API client and public environment contract
    - Add `ProblemDetails`, narrowing, `ApiProblemError`, `ApiTransportError`, timeout/abort support, 204 handling, typed query keys, and synthesized problems for network/non-conforming failures.
    - Build URLs only from validated `NEXT_PUBLIC_API_BASE_URL`; never use a server-internal Compose hostname in browser code.
    - Add examples for conforming problems, malformed error bodies, network aborts, 204, field errors, and the real HTTP status preservation.
    - _Design: §12.3, §12.6, Appendix C.3; Deliverable: 0.4; Property: P-14_

  - [x] 6.4 Build the shell with one real accessible Home link
    - Implement root/shell layouts, skip link, header, theme toggle, error/not-found routes, and a primary sidebar containing exactly one real `Home` link to `/` and no future-feature placeholders.
    - The link must be keyboard reachable/activatable, retain visible focus, show active styling, and set `aria-current="page"` at `/`; retain one route `<h1>`, labelled nav, and main landmark.
    - Add component tests for exact navigation contents, keyboard activation, focus visibility classes, active styling, `aria-current`, skip-link target, landmarks, and theme button semantics.
    - _Design: §5.5, §12.1–§12.2; Deliverable: 0.4; Criterion: 6_

  - [x] 6.5 Write property test P-14 for frontend error normalization
    - Generate HTTP statuses, content types, valid/invalid JSON bodies, and transport failures; prove every non-2xx yields only `ApiProblemError` or its subclass and never raw parsing/type exceptions.
    - Prove valid RFC 9457 bodies round-trip and non-conforming bodies retain the actual HTTP status.
    - _Design: §12.3, Appendix B P-14; Deliverable: 0.4; Property: P-14_

  - [x] 6.6 Run frontend unit and accessibility tests
    - Run shell, providers, state-boundary, form-error, API-client, theme, and accessibility component tests in single-run mode.
    - Assert the sidebar has exactly one Home destination and no disabled/future links.
    - _Design: §7.6, §12.1–§12.5; Deliverable: 0.4; Criterion: 6; Property: P-14_

  - [x] 6.7 Add Playwright shell coverage and a k6 health smoke script
    - Add Playwright checks for localhost load, skip-link focus, keyboard Home activation, active/`aria-current` state, landmarks, one heading, theme persistence, and no placeholder navigation.
    - Add a k6 `/health` smoke script and a non-gating `make load` integration point; do not add dashboard or feature-flow scenarios.
    - Add static test/config validation for Playwright single-run behavior and k6 target selection.
    - _Design: §7.6, §8.3, §12.2, §13.4; Deliverable: 0.4; Criterion: 6_

  - [x] 6.8 Containerize the frontend and add its default Compose service
    - Create a multi-stage Dockerfile with `ARG` and `ENV` for `NEXT_PUBLIC_API_BASE_URL` and `NEXT_PUBLIC_APP_NAME` in the builder stage before `pnpm build`.
    - Add the `frontend` Compose service with matching `build.args`, browser-safe defaults, backend health dependency, and runtime settings; optional profiles remain absent.
    - Add Dockerfile/Compose assertions that fail if either public variable is introduced only at runtime or after the build.
    - _Design: §12.6, §13.3; Deliverable: 0.4; Criterion: 4, 6_

  - [x] 6.9 Test frontend build-time URL inlining
    - Build with a non-default browser-reachable API URL and app name, inspect/execute the generated client, and prove requests use the supplied build URL rather than `backend:8000`, the default, or a runtime-only override.
    - Run the Playwright shell assertions against the built output.
    - _Design: §8.3, §12.6, §13.3, Appendix E criterion 6; Deliverable: 0.4; Criterion: 6_

- [x] 7. Complete fresh-clone default Compose behavior only after both app services exist
  - [x] 7.1 Add readiness polling and wire `make up` without changing direct Compose behavior
    - Add `scripts/dev-up.sh` only now: run `docker compose up -d --wait` for the unprofiled default set, then poll `/health/ready` with a bounded timeout and named failure output.
    - Make `up` depend on `init-env` and invoke the script; direct `docker compose up -d --wait` must remain valid with no `.env` present. Add `down` and bounded smoke helpers.
    - Add script tests for readiness success, timeout, and preservation of a pre-existing `.env`.
    - _Design: §2.2, §4.4, §13.3–§13.4; Deliverable: 0.1, 0.3, 0.4; Criterion: 4, 5_

  - [x] 7.2 Test exact fresh-clone default service topology
    - From a no-`.env` fixture, run/validate the direct unprofiled command and assert exactly `postgres`, `redis`, `opa`, `backend`, and `frontend` are selected and become healthy; assert no optional service/profile exists yet.
    - Prove `.env.example` supplies container values, interpolation defaults work, optional `.env` overrides when present, and repeated `make init-env` never changes existing bytes.
    - _Design: §2.2, §13.3, Appendix E criterion 4; Deliverable: 0.1, 0.3, 0.4; Criterion: 4; Property: P-15_

  - [x] 7.3 Test default-profile liveness/readiness outage semantics
    - With the backend running, make PostgreSQL and Redis unreachable and prove `/health` stays 200 while `/health/ready` returns RFC 9457 503 with failed/timed-out checks; restore them and prove readiness recovers.
    - Confirm Compose uses liveness, while `dev-up.sh` separately gates readiness.
    - _Design: §4.4, §13.3, Appendix E criterion 5; Deliverable: 0.3; Criterion: 5; Property: P-09_

- [x] 8. Add Go transports and probes after primitives and before concrete integration packages
  - [x] 8.1 Implement JSON-RPC envelopes and the real coder/websocket transport
    - Add request/response/error envelopes, `Transport`, `WSSTransport`, and a Phase 0 connection manager that returns `ErrDisabled` when no backend URL is configured.
    - Limit the work to framed transport mechanics: no pairing, mTLS identity, heartbeat, reconnect, command whitelist, or Phase 1 protocol behavior.
    - Add an `httptest` websocket echo/close test, context-cancellation test, frame-limit test, and dependency check rejecting deprecated `nhooyr.io/websocket`.
    - _Design: §7.3, §10.5, §15.7, §16.1; Deliverable: 0.2_

  - [x] 8.2 Implement read-only Docker and Kubernetes doctor probes
    - Add Docker ping/server-version and Kubernetes current-context/server-version probes only, each constructed with injected clients and returning structured pass/fail/skipped diagnostics.
    - Do not add mutation methods, command execution, dashboard behavior, or Phase 1 connection logic.
    - Add tests with fake daemon/discovery endpoints for success, unavailable, permission, and cancellation cases.
    - _Design: §10.9, §1.2; Deliverable: 0.2_

  - [x] 8.3 Run transport and probe tests
    - Run websocket round-trip/close tests and Docker/Kubernetes probe tests with race detection where supported.
    - Confirm transport/probes remain independently constructible and no final `internal/app` or Cobra wiring exists yet.
    - _Design: §7.5–§7.6, §10.1, §10.5, §10.9; Deliverable: 0.2_

- [x] 9. Implement the OpenTofu runner and only then add the tools profile
  - [x] 9.1 Implement the runner contract, bounded execution, and output streaming
    - Add `TofuConfig`, results/options, `Runner`, `LineSink`, `ErrTofuNotFound`, timeout fallback, bounded stdout/stderr capture, line truncation, and completion ordering that drains both streams before wait returns.
    - Add fake-binary tests for missing binary, timeout selection, interleaved streams, retained tail output, overlong lines, and exit-code capture.
    - _Design: §3.4, §10.6, Appendix C.2; Deliverable: 0.8_

  - [x] 9.2 Implement platform-specific process-tree termination
    - Add Unix process-group start/SIGTERM/grace/SIGKILL and Windows new-process-group/`taskkill /T /F` implementations in build-tagged files.
    - Add platform-appropriate tests proving a spawned child tree does not survive cancellation and the returned error wraps the context deadline; record Windows Job Objects only as future hardening.
    - _Design: §10.6, §17.2 OQ-6; Deliverable: 0.8_

  - [x] 9.3 Implement isolated OpenTofu child environments
    - Build the child environment only from the documented platform allowlist, explicit extra allow entries, and fixed `TF_*`/`NO_COLOR` keys; never pass `os.Environ()` wholesale.
    - Add examples proving parent credentials and unrelated variables are absent while required automation/cache/data-directory values are present.
    - _Design: §10.6, §14.1, Appendix B P-12; Deliverable: 0.8; Property: P-12_

  - [x] 9.4 Implement `Validate` and `Plan` with fake-binary and unit coverage
    - Execute `tofu validate -json`, `tofu plan -detailed-exitcode -out=...`, and `tofu show -json`; treat plan exit code 2 as success-with-changes and retain structured diagnostics.
    - Keep shared-sample generation out of this task because the exact real fixture and committed provider lock are not created until task 9.5; never implement `apply`.
    - Add fake-binary/unit tests for no-change, has-change, validation failure, malformed show output, cancellation, and bounded output.
    - _Design: §10.6, Appendix C.2; Deliverable: 0.8_

  - [x] 9.5 Add the exact provider fixture, six-platform lock, shared sample, devtools image, and tools profile
    - Create `agent/testfixtures/tofu-null` with exact `hashicorp/null` `3.2.3` and a committed `.terraform.lock.hcl` containing checksums for linux/darwin/windows on amd64/arm64.
    - After the fixture and lock exist, run the already-implemented task 9.4 `Plan` path against that exact fixture and commit its real `tofu show -json` output as `agent/testdata/plan-sample.json`; this is the sole shared sample consumed by tasks 14.1 and 14.4.
    - Add the agent `devtools` Docker target with OpenTofu 1.12.5, then—and only then—add `agent-dev` and `tofucache` under Compose profile `tools`.
    - Add fixture, lock-content, sample-shape, and Compose-profile assertions proving the committed sample came from the exact fixture and the tools service is excluded from unprofiled startup.
    - _Design: §10.6, §11.9, §13.3, §16.4, Appendix E criterion 13; Deliverable: 0.8; Criterion: 4, 7, 13_

  - [x] 9.6 Run real OpenTofu and lock-integrity integration tests
    - Run `tofu init -lockfile=readonly`, validate, and plan against the null fixture; regenerate the six-platform lock in an isolated copy and fail on any diff.
    - Verify `TF_PLUGIN_CACHE_DIR`, tools-profile startup, exact OpenTofu/provider versions, committed-sample validity/reproducibility through the already-implemented `Plan` path, and process cleanup without rewriting the task 9.5 sample.
    - _Design: §7.6, §8.3, §10.6, §13.3; Deliverable: 0.8; Criterion: 4, 13_

  - [x] 9.7 Write property test P-12 for environment isolation
    - Generate parent environments/config allowlists and prove child keys are always a subset of the allowed plus fixed keys, disallowed keys never appear, and mandatory automation keys always appear.
    - _Design: §10.6, Appendix B P-12; Deliverable: 0.8; Property: P-12_

- [x] 10. Implement the Git and PR client after shared Go primitives
  - [x] 10.1 Define the Git/PR contracts and token seam
    - Add the client/change-set/PR/status/signature types and `TokenSource` with only `EnvTokenSource`; wrap `go-git/go-git/v5` and `google/go-github` behind the consumer-facing package.
    - Preserve D-5 and leave GitHub App installation-token minting for Phase 1.
    - Add constructor/config tests proving no library type leaks through the public contract and missing tokens are redacted typed errors.
    - _Design: §3.6, §10.7, §17.1 D-5, §17.2 OQ-7; Deliverable: 0.6_

  - [x] 10.2 Implement local branch, stage, commit, and push operations
    - Verify every change-set path resolves inside the repository root; create a branch from the requested base, stage only listed paths, commit deterministically, and push without any force path.
    - Preserve the local branch on rejection and return typed auth/push errors.
    - Add a bare-repository fixture test for full branch→stage→commit→push, path escape, auth failure, and non-fast-forward rejection.
    - _Design: §3.6, §10.7, Appendix C.2; Deliverable: 0.6_

  - [x] 10.3 Implement PR creation, status retrieval, and bounded polling
    - Implement typed PR REST calls, terminal review-state polling, context/timeout handling, last-status return, and 403 rate-limit mapping with reset time and no hammering.
    - Add local HTTP fixture tests for request shape, status mapping, terminal detection, timeout, cancellation, and rate-limit stop.
    - _Design: §3.6, §10.7, Appendix C.2; Deliverable: 0.6_

  - [x] 10.4 Run the complete GitOps flow tests
    - Exercise branch→commit→push→PR→poll using only deterministic local repository/HTTP fixtures.
    - Prove no force push, external network call, or Phase 1 GitHub App implementation is introduced.
    - _Design: §3.6, §10.7; Deliverable: 0.6_

- [x] 11. Implement the Go MCP server after OpenTofu and file operations exist
  - [x] 11.1 Build the server and shared handler middleware
    - Implement `Deps`, `NewServer`, and context-driven stdio/HTTP-SSE serving over `mark3labs/mcp-go`, with trace extraction, child propagation, per-call timeout, and structured logging.
    - Use only constructors already implemented in tasks 3 and 9; do not add final app/CLI composition yet.
    - Add server lifecycle tests for transport selection, cancellation, timeout, and trace metadata.
    - _Design: §10.1, §10.8; Deliverable: 0.5_

  - [x] 11.2 Register only the three non-mutating Phase 0 tools
    - Register `agent.health`, `agent.tofu.validate`, and `agent.tofu.plan` with validated schemas and blast-radius annotations; expose no apply, Docker, Kubernetes, file-write, or future tool.
    - Add handler tests for health payload, validate diagnostics, plan result, invalid input, and cancellation.
    - _Design: §10.8, §11.4; Deliverable: 0.5; Criterion: 10_

  - [x] 11.3 Test the Go MCP protocol surface
    - Assert `tools/list` returns exactly the three annotated tools, allowed `agent.health` call succeeds, unknown tools return protocol errors, and cancellation aborts work.
    - _Design: §10.8, Appendix E criterion 10; Deliverable: 0.5; Criterion: 10_

- [x] 12. Implement the backend MCP gateway with separate security-ordered list and call paths
  - [x] 12.1 Implement registry loading, header routing, and OIDC verification
    - Add strict MCP server descriptors/registry, body-independent `HeaderRouter`, and per-issuer JWKS verification with exact issuer allowlist, audience, signature, exp/nbf/iat, and bearer handling.
    - Routing must occur only after successful bearer verification. Missing headers return 400 and unknown servers 404; neither may select a default.
    - Add fixed local JWKS tests for no token, bad signature, untrusted issuer, wrong audience, expired/not-yet-valid token, header routing, and body independence.
    - _Design: §3.1, §5.3, §11.4, §15.2, Appendix A.8; Deliverable: 0.5; Criterion: 12; Property: P-05_

  - [x] 12.2 Implement Redis-authoritative tool-list TTL caching
    - Use Redis `SET PX min(server_ttl_ms, max_ttl_ms)` and an atomic value+`PTTL` read. Return values only while Redis reports `PTTL > 0`; non-positive TTL creates no key.
    - Redis is the sole runtime expiry authority across replicas. Process monotonic timestamps may exist only in a pure test reference model and must never be serialized or used by runtime cache code.
    - Add unit examples for TTL clamp, non-positive TTL, Redis-error-as-miss, lazy expiry, and no local runtime fallback.
    - _Design: §5.3, §11.4, Appendix A.6; Deliverable: 0.5; Property: P-06_

  - [x] 12.3 Implement OPA policy, upstream transport, and side-effect-free metadata resolution
    - Implement fail-closed `filter_tools`/`authorise_call`, trace-propagating upstream list/call transport, and `ToolMetadataResolver` that resolves only configured metadata or an already-valid Redis cache entry.
    - Metadata resolution must perform no upstream request, execute no handler, and deny unresolved/unknown tools. Instrument list/call/other upstream operations independently for tests.
    - Add local OPA/upstream fixture tests for filtering, denial, OPA transport/error responses, metadata cache validity, and redacted timeouts.
    - _Design: §3.1, §5.3, §11.4, §14.1, Appendix A.9; Deliverable: 0.5; Property: P-05_

  - [x] 12.4 Implement the Redis Tasks Extension state machine
    - Add exact states/transitions, Redis records, compare-and-set update, create/get/update/cancel handlers, terminal absorption, and idempotent cancellation.
    - Add deterministic and concurrent tests for every allowed/forbidden edge, missing tasks, terminal cancel, and two-writer conflict.
    - _Design: §3.2, §11.5; Deliverable: 0.5; Criterion: 11; Property: P-10_

  - [x] 12.5 Implement MCP Apps sandbox hosting
    - Add descriptor/host endpoints, exact CSP, iframe `sandbox="allow-scripts allow-forms"` without same-origin, versioned postMessage envelopes, and descriptor-origin validation.
    - Ship only the `agent.health` descriptor; do not add approval forms, dashboards, or configuration editors.
    - Add tests for CSP, sandbox tokens, descriptor shape, accepted origin, and foreign-origin drop.
    - _Design: §11.6; Deliverable: 0.5_

  - [x] 12.6 Implement the Python MCP server template
    - Add `ToolSpec`, schema-before-dispatch validation, trace handling, timeout/logging middleware, and exactly one `platform.health` tool mounted as a FastAPI sub-application.
    - Add tests for list/call, invalid schema, timeout, trace propagation, and the exact one-tool scope.
    - _Design: §11.10; Deliverable: 0.5; Criterion: 10_

  - [x] 12.7 Implement and wire the `tools/list` gateway path
    - Enforce `verify bearer/OIDC → route from headers → Redis cache or upstream list → OPA filter on every response → return`; cache only the unfiltered upstream list.
    - OPA filtering must run on cache hits and misses; Redis failure becomes a cache miss; OPA failure returns an empty allowed set. Expose `/api/v1/mcp` and OPA-filtered `/api/v1/mcp/servers` through app-state dependencies.
    - Add ordered-collaborator tests proving the exact sequence, policy-on-hit, trace propagation, timeout handling, and no caller-specific cached result.
    - _Design: §3.1.1, §5.3, §11.4, Appendix A.9; Deliverable: 0.5; Criterion: 10; Property: P-05, P-06_

  - [x] 12.8 Implement and wire the `tools/call` gateway path
    - Enforce `verify bearer/OIDC → route from headers → parse called tool → resolve metadata locally/from already-valid cache without upstream I/O → OPA authorize → invoke upstream only on allow`.
    - Place the only call-dispatch site after successful authorization. Invalid bearer, malformed call, unresolved metadata, unknown tool, OPA denial, OPA error, and authorization exception must return before every upstream operation.
    - Add allow-path tests proving exactly one call after authorization and no generic verify/route/cache/upstream/policy helper is shared with the call path.
    - _Design: §3.1.2, §5.3, §11.4, Appendix A.9, Appendix C.1; Deliverable: 0.5; Criterion: 10, 12; Property: P-05_

  - [x] 12.9 Write property test P-05 for gateway routing and zero upstream work
    - Generate bodies for fixed headers to prove route body-independence. Generate invalid bearer, malformed call, missing/unresolved metadata, unknown tool, OPA deny/error, and authorization exceptions.
    - For every rejected/erroring call, assert every injected upstream-operation counter—list, call, metadata/network, and other transport—is zero. For allow, assert exactly one call dispatch after authorization for the same route/tool/claims.
    - _Design: §3.1, §11.4, Appendix A.8–A.9, Appendix B P-05; Deliverable: 0.5; Criterion: 10, 12; Property: P-05_

  - [x] 12.10 Write property and real-Redis integration coverage for P-06
    - Compare TTL/clamping/no-cache behavior to a pure injected-monotonic reference model, while runtime tests use Redis only.
    - Use two independent Redis clients to prove `SET PX`, positive `PTTL`, cross-client visibility, expiry, never-serve-after-expiry, no caching for non-positive TTL, and absence of serialized monotonic deadlines.
    - _Design: §11.4, Appendix A.6, Appendix B P-06; Deliverable: 0.5; Property: P-06_

  - [x] 12.11 Write stateful/concurrent property test P-10
    - Generate task transition sequences and concurrent updates; prove only declared edges succeed, terminal states absorb, cancellation is idempotent, and at most one competing update wins.
    - _Design: §11.5, Appendix B P-10; Deliverable: 0.5; Criterion: 11; Property: P-10_

  - [x] 12.12 Run end-to-end MCP gateway tests
    - Drive authenticated `tools/list` and allowed/denied `tools/call` through the gateway to deterministic Python and Go MCP fixtures, then run create→poll→cancel→cancel-again tasks flow.
    - Cover OPA filtering on cached and uncached lists, strict call ordering, all zero-counter rejection cases, trace headers, Redis TTL, issuer failures, and Apps sandbox headers.
    - _Design: §3.1–§3.2, §10.8, §11.4–§11.6, §11.10; Deliverable: 0.5; Criterion: 10, 11, 12; Property: P-05, P-06, P-10, P-13_

- [x] 13. Implement executable model routing, semantic cache, BYO keys, and fail-closed per-caller admission
  - [x] 13.1 Implement six-tier configuration and endpoint descriptor validation
    - Add exactly six `ModelTier` values and `config/model-tiers.yaml` from Design §13.2, including protocols, absolute base URLs, timeouts, key references, rank source, and internal-golden field.
    - Reject unknown keys/references/protocol values and any vendor leaderboard score field; expand only documented environment placeholders.
    - Add config tests for all six tiers, malformed references/URLs, unknown fields, and absence of vendor-score ordering.
    - _Design: §11.7–§11.7.1, §13.2; Deliverable: 0.9_

  - [x] 13.2 Implement `ModelEndpoint`, `EndpointRegistry`, and production `OpenAICompatibleEndpoint`
    - Add structured request/response/error types, runtime key resolution, explicit timeout, W3C headers, validated `/chat/completions` handling, and redaction of authorization/key/prompt content.
    - Construct a real `OpenAICompatibleEndpoint` only for `openai_compatible`. Retain `anthropic_native` and `google_native` descriptors as honestly unavailable with `unsupported_protocol_phase_0`; never create fake adapters.
    - Add deterministic local HTTP fixture tests for success, timeout, non-2xx, malformed JSON/content, trace headers, key omission for self-hosted endpoints, redaction, and unavailable-native reporting.
    - _Design: §5.6, §11.7.1a, §13.2, Appendix C.1; Deliverable: 0.9; Criterion: 17; Property: P-02, P-03_

  - [x] 13.3 Implement the per-endpoint circuit breaker registry
    - Implement closed/open/half-open state, a 30-second sliding failure window, 5-failure threshold, 60-second open cooldown, and one in-flight half-open probe using an injected monotonic clock.
    - Add deterministic examples for threshold trip, old-failure pruning, cooldown, one probe, probe success reset, and probe failure reopen.
    - _Design: §11.7.2, Appendix A.1; Deliverable: 0.9; Criterion: 18; Property: P-01_

  - [x] 13.4 Implement the tiered semantic cache and recoverable index setup
    - Implement strict L1 exact→L2 semantic→L3 prefix precedence, canonical hashed keys, Redis Vector Search HNSW `DIM 1536`, threshold acceptance, stale/degraded outage fallback, and one platform preamble prefix block.
    - Add idempotent semantic-index creation that retries with bounded backoff when Redis becomes reachable; initial Redis unavailability must not abort lifespan or liveness.
    - Add examples for precedence, threshold boundaries, stale flags, non-negative age, Redis miss degradation, index-already-exists, and recovery after startup outage.
    - _Design: §4.4, §11.8, Appendix A.3, Appendix C.1; Deliverable: 0.9; Property: P-04_

  - [x] 13.5 Implement BYO-key resolvers and only then add the vault profile
    - Add `KeyResolver`, `EnvKeyResolver`, and real `InfisicalKeyResolver` for `/{tenant_id}/llm/{provider}`, using `SecretStr`, tenant `default`, redacted errors, and no key logging.
    - Only in this task add the digest-pinned Infisical service under Compose profile `vault`; it must remain absent from unprofiled selection.
    - Add resolver fixture tests and static Compose tests for profile isolation, exact pinning, and no secret exposure.
    - _Design: §11.7.4, §13.3, §14.4; Deliverable: 0.9; Criterion: 4_

  - [x] 13.6 Implement the Redis/Lua atomic token bucket
    - Implement a single Lua operation keyed by verified OIDC `sub` plus route, using Redis `TIME` as production time for refill, consume, state write, bounded TTL, remaining count, and retry delay.
    - Support only fail-closed mode for `/api/v1/ai/complete`; Redis/script failure maps to RFC 9457 503 and exhaustion maps to RFC 9457 429 with integer `Retry-After = ceil(time to one token)`.
    - Add deterministic reference-model examples and script unit tests for refill, capacity clamp, cost, TTL, key partitioning, retry rounding, and failure mapping.
    - _Design: §3.3, §5.7, §11.7.5, Appendix C.1; Deliverable: 0.9; Property: P-09_

  - [x] 13.7 Implement the concrete fallback router
    - Build deduplicated primary→secondary→cross-vendor→self-hosted chains, consult semantic cache first, skip open breakers and unavailable protocols with explicit attempt reasons, invoke only registry-provided endpoints, and end with deterministic `EXHAUSTED` rather than a fake template.
    - Record each endpoint at most once, preserve order, update breakers, redact provider reasons, and set degraded/served-from/staleness fields.
    - Add deterministic local-fixture examples for primary success, timeout, malformed response, cross-vendor fallback, self-hosted success, open-breaker skip, unsupported-native skip, cache hit, and full exhaustion.
    - _Design: §3.3, §11.7.3, Appendix A.2; Deliverable: 0.9; Criterion: 17; Property: P-02, P-03, P-04_

  - [x] 13.8 Wire model routes with the fixed security/admission order
    - Add `GET /api/v1/ai/tiers` with protocol, availability reason, and breaker state. Add `POST /api/v1/ai/complete` with `OIDC verify → require claims.sub → Redis limiter → semantic cache → registry/router/provider`.
    - On invalid bearer, missing sub, Redis failure, or exhausted bucket, no semantic-cache or provider operation may run. Return RFC 9457 401/503/429 as appropriate; exhaustion after admission remains the ordinary 200 routing outcome.
    - Wire registry/cache/router/limiter/index retry through lifespan/app state without eager vendor calls. Add ordered-collaborator tests and separate cache/provider counters for every pre-admission denial/failure.
    - _Design: §3.3, §5.2, §5.7, §11.1, §11.7.5, §14.2, Appendix C.1; Deliverable: 0.9; Criterion: 17; Property: P-09_

  - [x] 13.9 Run deterministic endpoint/cascade integration tests
    - Use local HTTP servers only—no vendor network or real key—to prove primary timeout/failure, malformed response, cross-provider fallback, self-hosted success, trace injection, error redaction, unsupported-native skip, and full exhaustion.
    - Assert `OpenAICompatibleEndpoint` is the only production adapter constructed in Phase 0.
    - _Design: §11.7.1a–§11.7.3, Appendix E criterion 17; Deliverable: 0.9; Criterion: 17; Property: P-02, P-03_

  - [x] 13.10 Write stateful property test P-01 for the circuit breaker
    - Generate failure/success/tick sequences and prove only valid states/transitions, threshold-within-window opening, cooldown-only half-open, one probe, success reset, and no spontaneous changes.
    - _Design: §11.7.2, Appendix A.1, Appendix B P-01; Deliverable: 0.9; Criterion: 18; Property: P-01_

  - [x] 13.11 Write property test P-02 for registry-backed cascade termination and order
    - Generate finite chains, availability maps, duplicate ids, and endpoint outcomes; prove termination within the deduplicated chain, at-most-once ordered concrete invocation, unsupported skip, swallowed provider errors, and `OK`/`EXHAUSTED` only.
    - _Design: §11.7.1a, §11.7.3, Appendix A.2, Appendix B P-02; Deliverable: 0.9; Criterion: 17; Property: P-02_

  - [x] 13.12 Write property test P-03 for zero invocation of skipped endpoints
    - Generate breaker/availability states and prove open-breaker and unsupported/unconfigured endpoints produce the correct skip reason with adapter invocation count zero.
    - _Design: §11.7.3, Appendix B P-03; Deliverable: 0.9; Criterion: 17; Property: P-03_

  - [x] 13.13 Write property test P-04 for semantic-cache precedence and resilience
    - Generate L1/L2 entries, similarities, ages, and provider availability; prove exact-hit precedence, L2-on-L1-miss only, below-threshold service only during provider outage with `degraded=true`, and non-negative staleness.
    - _Design: §11.8, Appendix A.3, Appendix B P-04; Deliverable: 0.9; Property: P-04_

  - [x] 13.14 Test the limiter against a real Redis under concurrency and failures
    - Compare Lua decisions to the injected-clock reference model, then use multiple clients to prove successful consumes never exceed available tokens and keys isolate subject/route pairs.
    - At the HTTP route, prove Redis failure returns RFC 9457 503, exhaustion returns 429 plus integer `Retry-After`, and every cache/provider counter remains zero for invalid bearer, missing sub, limiter failure, and limiter denial.
    - _Design: §5.7, §11.7.5, Appendix C.1; Deliverable: 0.9; Property: P-09_

  - [x] 13.15 Test the optional vault profile separately
    - Start/validate only the `vault` profile after the resolver exists, exercise deterministic Infisical key lookup/redaction, and prove unprofiled startup still selects exactly the five default services.
    - _Design: §2.2, §11.7.4, §13.3; Deliverable: 0.9; Criterion: 4_

- [x] 14. Implement the validation pipeline and deterministic Semantic Plan Analyzer
  - [x] 14.1 Implement plan parsing, syntax/schema stages, and the stage-agnostic pipeline runner
    - Add `PlanDocument`, findings/severity/context/result types, `Stage`, and an ordered runner over its injected stage list with non-fatal accumulation and fatal short-circuit; register Syntax and Schema only in this task.
    - Reserve documented insertion points for the Semantic stage delivered by task 14.2 and the Phase 1 dry-run stage; do not reference an unavailable constructor or implement feature generation/apply behavior.
    - Add tests for real OpenTofu JSON parsing using only the committed `agent/testdata/plan-sample.json` produced by prior task 9.5, plus malformed pointers/RFC 9457 422, injected-stage order, accumulation, and fatal short-circuit.
    - _Design: §3.5, §11.9, Appendix C.1; Deliverable: 0.7_

  - [x] 14.2 Implement deterministic destructive-action and blast-radius analysis
    - Implement configured action weights/class multipliers, affected/destructive counts, stateful deletion detection, score, and allow/warn/block verdict without any LLM call.
    - Add examples for each action/class, unknown-class conservative handling, stateful forced block, score thresholds, and deterministic repeatability.
    - _Design: §3.5, §11.9, Appendix A.7; Deliverable: 0.7; Property: P-11_

  - [x] 14.3 Implement and wire the approval seam
    - Add `ApprovalGate` with real `ThresholdApprovalGate`: allow→AUTO_OK, warn→REQUIRES_APPROVAL, block→BLOCKED. Wire it as the final pipeline output.
    - Do not add persisted approvals, change sets, governance control plane, or user interaction.
    - Add exhaustive mapping tests and pipeline tests proving the analyzer verdict reaches the gate unchanged.
    - _Design: §3.5, §11.9, §14.3; Deliverable: 0.7_

  - [x] 14.4 Expose plan analysis and test the real shared sample
    - Add `POST /api/v1/analysis/plan` returning findings, blast radius, verdict, and approval decision.
    - Test using only `agent/testdata/plan-sample.json` produced by prior task 9.5 and assert the expected non-empty deterministic result; add malformed and stateful-delete examples.
    - _Design: §3.5, §11.9, Appendix E criterion 13; Deliverable: 0.7; Criterion: 13; Property: P-11_

  - [x] 14.5 Write property test P-11 for analyzer monotonicity
    - Generate normalized plans and appended destructive actions; prove score never decreases, verdict never softens, results are deterministic, destructive count never exceeds affected count, and any stateful deletion blocks.
    - _Design: §11.9, Appendix A.7, Appendix B P-11; Deliverable: 0.7; Criterion: 13; Property: P-11_

- [x] 15. Compose the final Go application, build/release automation, and evidence records
  - [x] 15.1 Implement final `internal/app` composition and graceful shutdown after all constructors exist
    - Construct config/logging/telemetry/fileops/watcher/connection/probes/OpenTofu/Git/MCP dependencies explicitly, register closers in construction order, run long-lived subsystems under one errgroup, and close once in exact reverse order under the configured timeout.
    - Use only constructors delivered in earlier tasks; add no temporary fake, service locator, package global, generated DI, or app task before a constructor exists.
    - Add deterministic lifecycle tests for disabled connection, subsystem failure cancellation, joined close errors, reverse order, timeout, and idempotence.
    - _Design: §5.1, §7.5, §10.1, §10.3–§10.4; Deliverable: 0.2; Property: P-07_

  - [x] 15.2 Implement final Cobra commands, doctor composition, and thin `main.go`
    - Add `run`, `doctor`, `version`, and `mcp serve`; wire Docker/Kubernetes/OpenTofu checks into doctor; use `signal.NotifyContext`, injected build variables, constructed App, and deferred close.
    - Keep `cmd/agent/main.go` thin and preserve Phase 0’s normal disabled-backend path; do not add pairing, auto-update, reconnect, command execution, or agent identity.
    - Add CLI tests for version fields, unknown commands, doctor status/remediation, clean SIGTERM, and shutdown timeout.
    - _Design: §5.1, §10.1–§10.4, §10.9, Appendix C.2; Deliverable: 0.2_

  - [x] 15.3 Write property test P-07 for shutdown ordering
    - Generate component lists and close-failure patterns; prove exact reverse order, exactly-once close, continued closing after errors, idempotence, and configured time bound.
    - _Design: §10.4, Appendix A.4, Appendix B P-07; Deliverable: 0.2; Property: P-07_

  - [x] 15.4 Complete the Go dependency-exercise matrix and boundary checks
    - Exercise coder/websocket, Docker/client-go probes, zap, Cobra, fsnotify, selfupdate verification, go-diff, mcp-go, go-git, go-github, and errgroup through the earlier tests/doctor paths.
    - Assert no dependency is merely declared, structural packages remain non-code, `CGO_ENABLED=0` remains viable, deprecated websocket and tree-sitter are absent, and all six target builds reference no unavailable constructor.
    - _Design: §10.1, §10.9, §16.1, §17.1 D-1, §17.1 D-5; Deliverable: 0.2; Criterion: 7_

  - [x] 15.5 Complete and audit `agent/NOTICE`
    - Audit direct and transitive linked dependencies for upstream NOTICE reproduction obligations, append only legally required notice text with identified sources, or record “no upstream NOTICE reproduction required” in release evidence.
    - Keep the complete base project notice and reject TODO, stub, prospective attribution, empty upstream headings, or an exhaustive dependency list that belongs in the SBOM.
    - Add a NOTICE audit check for mandatory base text, forbidden placeholders, and consistency with the dependency evidence.
    - _Design: §2.4, §10.9, §16.6, §17.1 D-19; Deliverable: 0.1, 0.2; Criterion: 8, 15_

  - [x] 15.6 Complete all root Makefile contracts
    - Wire `build`, `test`, `lint` and component targets; backend lock/freshness; tofu lock check; frontend public build args; `up/down/logs/migrate`; `e2e/load`; `sbom`; `release-snapshot`; and `verify-release` exactly as Design §13.4 specifies.
    - Use single-run test commands, preserve `.env`/volumes/locks on clean, and keep long-running logs/watchers out of automated validation targets.
    - Add Make contract tests proving target presence, working directories, `--require-hashes`, frozen/read-only lock flags, `vitest --run`, and no mutation of authoritative documents.
    - _Design: §13.4; Deliverable: 0.1–0.4, 0.8; Criterion: 1, 2, 3, 4, 15, 16_

  - [x] 15.7 Implement CI with lock, scope, and fresh-clone gates
    - Add SHA-pinned `ci.yml` with paths-filter, pre-commit, lock integrity, lint, test, build, e2e, audit, and snapshot supply-chain stages; use `agent/` as Go working directory and `requirements-dev.lock --require-hashes` for backend CI.
    - Regenerate/diff both Python locks, run `tofu init -lockfile=readonly` plus six-platform lock freshness, build frontend with a non-default public URL and test the generated client, and run default/profile-specific Compose smoke evidence separately.
    - Keep the four authoritative documents outside component path filters but inside Gitleaks; do not add Trivy, DeepEval, vendor model calls, or future-phase services.
    - _Design: §7.7, §8.3–§8.4, §13.3, §14.1; Deliverable: 0.1, 0.2, 0.3, 0.4, 0.8, 0.9; Criterion: 1, 2, 3, 4, 6, 7, 9_

  - [x] 15.8 Implement reproducible release, SBOM, signing, provenance, and verification configuration
    - Add `.goreleaser.yaml` for six `CGO_ENABLED=0` targets, archives/packages, deterministic metadata, and `main.version/commit/date` injection. Add SHA-pinned release workflow with keyless signing, CycloneDX SBOMs, provenance, and required permissions.
    - Add `verify-release` tooling for certificate identity/issuer, signature, SBOM schema/presence, and provenance; snapshot validation must not publish.
    - Add configuration/artifact-shape tests for six targets, `.sig`, `.pem`, `.sbom.json`, provenance, correct module path, and absence of tree-sitter/CGO requirements.
    - _Design: §8.1–§8.2, §13.4, §16.1, §17.1 D-1, §17.1 D-14; Deliverable: 0.2; Criterion: 7, 8, 15, 16_

  - [x] 15.9 Write property test P-09 across the final backend route set
    - Generate failures across health/readiness, MCP, Tasks, Apps, AI admission/routing, and plan analysis; prove every non-2xx is RFC 9457, status matches, required fields exist, and detail contains no secret pattern.
    - Include limiter 429/503, readiness 503, OIDC 401, MCP 400/403/404/504, validation 422, and generic 500 examples.
    - _Design: §4.2, §11.2, Appendix B P-09, Appendix C.1; Deliverable: 0.3, 0.5, 0.7, 0.9; Criterion: 5, 10, 12, 17; Property: P-09_

  - [x] 15.10 Verify all Phase 0 completion criteria using only earlier implementation
    - Execute the evidence paths for criteria 1–18: Make targets, exact default Compose set, liveness/readiness outage behavior, frontend/Home/build URL, six target artifacts, release/SBOM/signature/provenance, pre-commit, MCP list/call and zero-work denial, Tasks lifecycle, OIDC rejection, real sample plan, HNSW migration, cascade, and breaker.
    - Do not implement missing behavior in this task. If evidence fails, return to the owning leaf and then rerun only the affected checks.
    - _Design: Appendix E criteria 1–18; Deliverable: 0.1–0.9, Progress record; Criterion: 1–18; Property: P-01–P-15_

  - [x] 15.11 Finalize documentation and `PROGRESS.md` from captured evidence
    - Update project-owned README/docs and `PROGRESS.md` only: record every deliverable 0.1–0.9, criteria 1–18 evidence, P-01–P-15 coverage, decisions D-1/D-2/D-5/D-14/D-19, open questions, licence/NOTICE result, and exact commands/artifact paths.
    - Keep the four authoritative root documents byte-identical. Mark Phase 0 completed only when every criterion has evidence and no future-phase implementation/dependency is present.
    - Add a final traceability check for all deliverables, criteria, properties, PROGRESS sections, no tree-sitter string in implementation metadata, and no TODO/stub NOTICE text.
    - _Design: §0.3, §1, §17–§18, Appendix B, Appendix E; Deliverable: 0.1–0.9, Progress record; Criterion: 1–18; Property: P-01–P-15_

## Notes

- All 108 numbered executable leaves are mandatory and cannot be skipped for a faster MVP; implementation leaves still include focused example/unit checks for their own behavior.
- Property tasks map one-for-one to Design Appendix B P-01 through P-15. Properties shared across runtimes are tested in one cross-runtime leaf.
- Default Compose behavior is established before either optional profile. `tools` is added only in task 9.5; `vault` is added only in task 13.5; both are verified separately.
- Final Go composition is intentionally task 15.1, after primitives, probes/transports, OpenTofu, Git, and MCP constructors exist.
- No task creates `requirements.md`, implements Phase 1+ behavior, or modifies the four authoritative root documents.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["1.2", "1.3", "1.4", "1.5", "1.6", "1.7"] },
    { "id": 2, "tasks": ["1.8", "2.1", "3.1", "4.1", "6.1"] },
    { "id": 3, "tasks": ["2.2", "2.3", "2.4", "2.5", "2.6", "3.2", "3.3", "3.4", "3.5", "3.6", "3.7", "4.2", "6.2", "6.3"] },
    { "id": 4, "tasks": ["2.7", "3.8", "3.9", "3.10", "3.11", "4.3", "5.1", "6.4", "6.5"] },
    { "id": 5, "tasks": ["5.2", "5.5", "6.6", "6.7", "8.1", "8.2"] },
    { "id": 6, "tasks": ["5.3", "5.6", "5.7", "8.3"] },
    { "id": 7, "tasks": ["5.4", "6.8", "9.1", "10.1"] },
    { "id": 8, "tasks": ["6.9", "7.1", "9.2", "9.3", "10.2", "10.3"] },
    { "id": 9, "tasks": ["7.2", "7.3", "9.4", "10.4"] },
    { "id": 10, "tasks": ["9.5"] },
    { "id": 11, "tasks": ["9.6", "9.7", "11.1", "14.1"] },
    { "id": 12, "tasks": ["11.2", "14.2"] },
    { "id": 13, "tasks": ["11.3", "12.1", "14.3"] },
    { "id": 14, "tasks": ["12.2", "12.3", "12.4", "12.5", "12.6", "14.4"] },
    { "id": 15, "tasks": ["12.7", "13.1", "14.5"] },
    { "id": 16, "tasks": ["12.8", "13.2", "13.3", "13.4", "13.5", "13.6"] },
    { "id": 17, "tasks": ["12.9", "12.10", "12.11", "13.7", "13.15"] },
    { "id": 18, "tasks": ["12.12", "13.8"] },
    { "id": 19, "tasks": ["13.9", "13.10", "13.11", "13.12", "13.13", "13.14"] },
    { "id": 20, "tasks": ["15.1"] },
    { "id": 21, "tasks": ["15.2"] },
    { "id": 22, "tasks": ["15.3", "15.4", "15.5", "15.8"] },
    { "id": 23, "tasks": ["15.6", "15.9"] },
    { "id": 24, "tasks": ["15.7"] },
    { "id": 25, "tasks": ["15.10"] },
    { "id": 26, "tasks": ["15.11"] }
  ]
}
```
