# Generic Deployment Engine Step-by-Step Implementation Plan

**Plan Version:** 1.0.0
**Date:** 2026-10-08
**Architecture Spec:** [`2026-10-08-generic-deployment-engine-design.md`](file:///C:/IMP/antigravity-cli/Major%20Project/Devops%20Automation/docs/superpowers/specs/2026-10-08-generic-deployment-engine-design.md)
**Status:** Pending Operator Approval (Step 9 of Brainstorming / Pre-Implementation)
**Target Systems:**

- Backend: [`prompt_compiler.py`](file:///C:/IMP/antigravity-cli/Major%20Project/Devops%20Automation/backend/src/generation/prompt_compiler.py), [`service.py`](file:///C:/IMP/antigravity-cli/Major%20Project/Devops%20Automation/backend/src/generation/service.py), [`routes.py`](file:///C:/IMP/antigravity-cli/Major%20Project/Devops%20Automation/backend/src/deployments/routes.py)
- Agent: [`deployment.go`](file:///C:/IMP/antigravity-cli/Major%20Project/Devops%20Automation/agent/internal/executor/deployment.go), [`dispatcher.go`](file:///C:/IMP/antigravity-cli/Major%20Project/Devops%20Automation/agent/internal/executor/dispatcher.go)

---

## 1. Plan Overview & Architectural Foundations

This implementation plan translates the approved **Unified Two-Tier Inspection & Dynamic Blueprint Engine** into concrete, ordered engineering phases.

### 1.1 Acceptance Criteria

ForgeOps must eliminate avoidable ForgeOps-induced deployment errors for valid supported applications. Genuine application-source, infrastructure, or external-service failures must be correctly classified, diagnosed, and surfaced rather than hidden or incorrectly blamed on ForgeOps. The nested Next.js application (`code-review/`) is strictly a regression test case; no repository-specific logic shall be introduced.

### 1.2 Canonical 7-Gate Lifecycle

```text
G1: Blueprint Gate
        ↓
G2: Existing Artifact Gate
        ↓
G3: Pre-Execution Consistency Gate
        ↓
G4: Build / Compile Gate
        ↓
G5: Apply / Startup Gate
        ↓
G6: Workload Verification / Health Gate
        ↓
G7: Final Deployment Gate
        ↓
     SUCCESS
```

### 1.3 Universal Failure Handling & Recovery Flow

```text
Any Gate Failure (G1 - G7)
       ↓
Error Classification
       ↓
 ┌───────────────┬────────────────┬──────────────────────────┐
 ↓               ↓                ↓                          ↓
Deterministic   Transient       Artifact / ForgeOps /      Application Source
Failure         Failure         Configuration Failure      Defect
 ↓               ↓                ↓                          ↓
Fast-Fail       ≤2 Retries      Bounded AI Resolution      Surface Actionable
(Attempt 1)     (3s, 6s)        (Max 1-3 loops)            Diagnosis (ZERO Source
                                  ↓                         Modification)
                            Validate Proposed Fix
                                  ↓
                              Re-Execute Target Gate
```

---

## 2. Inventory of Affected Files, New Modules & Schemas

### 2.1 Existing Files to Modify

- [`backend/src/generation/prompt_compiler.py`](file:///C:/IMP/antigravity-cli/Major%20Project/Devops%20Automation/backend/src/generation/prompt_compiler.py): Bind prompt synthesis exclusively to `ProjectBlueprint`; purge hardcoded paths and monorepo heuristics.
- [`backend/src/generation/service.py`](file:///C:/IMP/antigravity-cli/Major%20Project/Devops%20Automation/backend/src/generation/service.py): Incorporate blueprint ingestion and multi-level artifact validation before calling generation models.
- [`backend/src/deployments/routes.py`](file:///C:/IMP/antigravity-cli/Major%20Project/Devops%20Automation/backend/src/deployments/routes.py): Remove ad-hoc directory replacement hacks; integrate G1-G7 pipeline triggers and operator ambiguity response endpoints.
- [`agent/internal/executor/deployment.go`](file:///C:/IMP/antigravity-cli/Major%20Project/Devops%20Automation/agent/internal/executor/deployment.go): Eliminate blind 10-attempt loop; remove hardcoded directory arrays (`frontend`, `Frontent`, `client`, `ui`); implement contextual error classification and gate verification.
- [`agent/internal/executor/dispatcher.go`](file:///C:/IMP/antigravity-cli/Major%20Project/Devops%20Automation/agent/internal/executor/dispatcher.go): Integrate structured `DiagnosticBundle` telemetry and G1-G7 gate event publishing.

### 2.2 New Files, Classes & Schemas to Create

- `backend/src/blueprint/models.py`: Authoritative `ProjectBlueprint`, `WorkloadType`, `RuntimeContract`, `BuildConfig`, `NetworkContract`, `AmbiguityResolution` Pydantic models.
- `backend/src/inspection/scanner.py`: Recursive tree scanner discovering ecosystem manifests and lockfiles without path assumptions.
- `backend/src/inspection/workspace_resolver.py`: Monorepo DAG analyzer for pnpm, npm, yarn, cargo, gradle to resolve ambiguity deterministically.
- `backend/src/inspection/blueprint_gate.py`: G1 Blueprint Gate validator asserting complete, non-contradictory contracts.
- `backend/src/validation/syntax_validator.py`: Level 1 syntax and schema validator for Dockerfiles, Compose files, and Kubernetes manifests.
- `backend/src/validation/blueprint_compatibility.py`: Level 2 semantic blueprint compatibility checker.
- `backend/src/validation/artifact_gate.py`: G2 Existing Artifact Gate selector.
- `backend/src/validation/consistency_gate.py`: G3 Pre-Execution Consistency Gate verifying context, files, and port alignment before execution.
- `agent/internal/models/blueprint.go`: Go counterpart struct definition for `ProjectBlueprint` and `DiagnosticBundle`.
- `agent/internal/executor/error_classifier.go`: Contextual error classifier distinguishing deterministic, transient, application-source, and internal engine errors.
- `agent/internal/executor/gates.go`: Implementations for G4 (Build/Compile), G5 (Apply/Startup), G6 (Workload Verification), and G7 (Final Sign-Off).
- `agent/internal/executor/workload_verifier.go`: Target-aware health verification engine (HTTP, TCP, Background Worker, Batch Job).
- `agent/internal/executor/diagnostics.go`: Diagnostic bundle assembler aggregating container logs, inspection state, build output, and repo tree snippets.
- `backend/src/recovery/ai_resolver.py`: Bounded AI recovery coordinator (1-3 attempts) with source-code mutation guard.
- `backend/src/recovery/fix_validator.py`: Proposed fix validator running Level 1/2 checks on AI suggestions before re-execution.
- `tests/lint/test_anti_patterns.py`: Static CI linter preventing hardcoded application strings.
- `tests/unit/test_blueprint_detection.py`: Unit tests for scanner, workspace DAG, and ambiguity resolver.
- `tests/unit/test_artifact_validation.py`: Unit tests for Level 1 syntax and Level 2 semantic validation.
- `tests/unit/test_error_classification.go`: Unit tests for Go error classification and fast-fail logic.
- `tests/e2e/test_heterogeneous_archetypes.py`: End-to-end matrix of 12 heterogeneous project archetypes.
- `tests/e2e/test_failure_injection.py`: Deterministic fast-fail, transient retry, and source error injection tests.
- `tests/regression/test_nested_nextjs_regression.py`: Explicit regression test for nested Next.js build (`code-review/`).

---

## 3. Detailed Step-by-Step Implementation Phases

### Phase 1: Canonical Blueprint Foundation & Dynamic Project Detection (G1 Gate)

#### Step 1.1: Canonical `ProjectBlueprint` Schema Definition

- **Objective:** Establish the strongly typed contract across backend and agent as the single source of truth.
- **Files Affected:**
  - Create `backend/src/blueprint/models.py`
  - Create `agent/internal/models/blueprint.go`
- **Current Behavior:** Ad-hoc dictionary with untyped assumptions passed across components; no unified schema.
- **Required Behavior:** Pydantic models in Python and strongly typed structs in Go capturing `WorkloadType` (`web_service`, `tcp_service`, `background_worker`, `batch_job`, `static_spa`), `BuildConfig`, `RuntimeContract`, `NetworkContract`, and `AmbiguityResolution`.
- **Implementation Details:**
  - Define `WorkloadType`, `ProtocolType` (`http`, `tcp`, `none`).
  - Define `BuildConfig` with `source_dir`, `build_command`, `artifact_output_dir`, `install_command`, and `cache_dirs`.
  - Define `RuntimeContract` with `language`, `runtime_version`, `framework`, `package_manager`, `start_command`, and `environment_variables`.
  - Define `NetworkContract` with `listen_port`, `protocol`, `health_check_path`, and `exposed_endpoints`.
  - Define `AmbiguityResolution` tracking `is_ambiguous`, `resolution_strategy`, `confidence_score`, and `unresolved_reason`.
  - Ensure full JSON serialization parity between Python Pydantic and Go `encoding/json`.
- **Dependencies:** None.
- **Tests Required:** Schema validation tests in `tests/unit/test_blueprint_models.py` verifying serialization, deserialization, and field constraints.
- **Acceptance Criteria:** Valid blueprints parse correctly in both Python and Go; invalid/missing required fields trigger validation errors.
- **Potential Risks/Regressions:** Field type mismatches between Go and Python; mitigated by automated cross-language roundtrip serialization tests.

---

#### Step 1.2: Dynamic Tree Scanner Without Path Assumptions

- **Objective:** Recursively inspect repository trees for manifests, configuration files, and lockfiles across ecosystems without hardcoded directory names.
- **Files Affected:**
  - Create `backend/src/inspection/scanner.py`
- **Current Behavior:** Backend checks only root or assumes specific names like `frontend` or `backend`.
- **Required Behavior:** Scanner searches for `package.json`, `pnpm-workspace.yaml`, `pom.xml`, `build.gradle`, `build.gradle.kts`, `Cargo.toml`, `go.mod`, `pyproject.toml`, `setup.py`, `requirements.txt`, `Pipfile`, `composer.json`, `*.csproj`, `*.sln`, `mix.exs`, `Gemfile`, and corresponding lockfiles (`pnpm-lock.yaml`, `yarn.lock`, `bun.lockb`, `package-lock.json`, `poetry.lock`, `uv.lock`, `Cargo.lock`, `go.sum`).
- **Implementation Details:**
  - Implement `scan_repository_tree(root_path: Path) -> DiscoveredManifests`.
  - Ignore standard ignored directories (`.git`, `node_modules`, `.next`, `dist`, `target`, `vendor`, `__pycache__`, `.venv`).
  - Extract directory depth, manifest path, and raw content for downstream parsing.
- **Dependencies:** Step 1.1.
- **Tests Required:** Unit tests in `tests/unit/test_scanner.py` scanning fixture trees with root, nested, and deeply nested manifests.
- **Acceptance Criteria:** Discovers manifests at any depth without name assumptions; correctly ignores build caches.
- **Potential Risks/Regressions:** Deep traversal performance in very large repositories; mitigated by path depth limits (max depth 6) and pruning ignored directory names during traversal.

---

#### Step 1.3: Monorepo Workspace Graph & Deterministic Ambiguity Resolution

- **Objective:** Deterministically resolve deployable application targets in monorepos before prompting the operator.
- **Files Affected:**
  - Create `backend/src/inspection/workspace_resolver.py`
- **Current Behavior:** Unhandled or defaulted to guesswork, leading to build crashes on nested directories.
- **Required Behavior:** Build DAG of monorepo packages, evaluate internal dependency relationships, identify leaf packages, inspect production scripts (`start`, `serve`, `build`), and determine the actual application target.
- **Implementation Details:**
  - Parse `pnpm-workspace.yaml`, `package.json` `workspaces`, `settings.gradle`, or `Cargo.toml` `[workspace]`.
  - Classify packages as shared libraries (imported by others) or applications (runnable entrypoints).
  - Precedence order:
    1. Single runnable leaf service with web framework -> select automatically.
    2. Gateway/main web service with backing shared packages -> select gateway.
    3. Multi-service repo with explicit user deployment target -> select specified target.
    4. Multiple independent runnable entrypoints with no default and no operator target -> flag as unresolvable with candidates listed.
- **Dependencies:** Step 1.2.
- **Tests Required:** Unit tests in `tests/unit/test_workspace_resolver.py` testing single-app, nested subfolder, multi-package pnpm monorepo, and unresolvable multi-app setups.
- **Acceptance Criteria:** Correctly identifies `code-review/` in the nested test case as the single runnable target without any hardcoded rule; marks genuinely ambiguous repos for operator clarification.
- **Potential Risks/Regressions:** Misidentifying a shared library as a runnable application; mitigated by checking for runnable production scripts (`start`/`serve`) and framework dependencies.

---

#### Step 1.4: G1 Blueprint Gate Implementation

- **Objective:** Validate that the synthesized `ProjectBlueprint` satisfies all structural, runtime, and execution preconditions.
- **Files Affected:**
  - Create `backend/src/inspection/blueprint_gate.py`
  - Update [`backend/src/deployments/routes.py`](file:///C:/IMP/antigravity-cli/Major%20Project/Devops%20Automation/backend/src/deployments/routes.py)
- **Current Behavior:** No blueprint gate exists; execution proceeds directly to generation regardless of ambiguity.
- **Required Behavior:** Assert that `source_dir` exists on disk, `runtime.language` and `runtime.runtime_version` are non-empty, `start_command` is defined, and `ambiguity.is_ambiguous` is false. If unresolved ambiguity exists, pause deployment and return structured clarification request to operator.
- **Implementation Details:**
  - Implement `verify_blueprint_gate(blueprint: ProjectBlueprint) -> GateResult`.
  - If gate fails due to ambiguity: return HTTP 200 with deployment state `AWAITING_OPERATOR_INPUT` and candidate list.
  - Expose `POST /api/deployments/{id}/resolve-ambiguity` in [`routes.py`](file:///C:/IMP/antigravity-cli/Major%20Project/Devops%20Automation/backend/src/deployments/routes.py) to resume execution with operator-selected target.
- **Dependencies:** Step 1.1, Step 1.2, Step 1.3.
- **Tests Required:** Unit and integration tests in `tests/unit/test_blueprint_gate.py`.
- **Acceptance Criteria:** Valid blueprint passes G1 cleanly; unresolved monorepo pauses with structured prompt; operator selection resumes pipeline.
- **Potential Risks/Regressions:** Pipeline hanging if operator clarification callback is not integrated; mitigated by explicit state transition `AWAITING_OPERATOR_INPUT`.

---

### Phase 2: Multi-Level Existing Artifact Validation & Reusability (G2 Gate)

#### Step 2.1: Level 1 Syntax & Schema Validation

- **Objective:** Validate pre-existing `Dockerfile`, `docker-compose.yml`, or Kubernetes manifests for syntactic and schema correctness.
- **Files Affected:**
  - Create `backend/src/validation/syntax_validator.py`
- **Current Behavior:** Backend naively assumes any existing Dockerfile is valid and reuses it blindly, or overwrites it arbitrarily.
- **Required Behavior:** Run parser/AST validation on Dockerfiles (valid instructions, non-empty `FROM`), validate Compose files against Compose v2/v3 specification schema, and validate Kubernetes manifests against Kubernetes OpenAPI schemas.
- **Implementation Details:**
  - Implement `validate_dockerfile_syntax(path: Path) -> ValidationResult`.
  - Implement `validate_compose_schema(path: Path) -> ValidationResult`.
  - Implement `validate_k8s_schema(path: Path) -> ValidationResult`.
- **Dependencies:** Step 1.1.
- **Tests Required:** Unit tests in `tests/unit/test_syntax_validator.py` verifying valid syntax, malformed Dockerfiles, and invalid Compose YAML.
- **Acceptance Criteria:** Syntactically malformed artifacts fail Level 1 with clear line and error descriptions.
- **Potential Risks/Regressions:** Rejecting valid non-standard Compose extensions; mitigated by allowing standard `x-` custom fields.

---

#### Step 2.2: Level 2 Semantic Blueprint Compatibility Validation

- **Objective:** Verify that a syntactically valid artifact matches the actual `ProjectBlueprint`.
- **Files Affected:**
  - Create `backend/src/validation/blueprint_compatibility.py`
- **Current Behavior:** No semantic checks; outdated Dockerfiles with wrong ports, obsolete base images, or incorrect subdirectories cause runtime crashes.
- **Required Behavior:** Verify:
  1. Path alignment: `COPY`, `ADD`, `WORKDIR` correspond to `build_config.source_dir` and real files on disk.
  2. Base image compatibility: `FROM` base image language and major version matches `runtime.language` and `runtime.runtime_version`.
  3. Port alignment: `EXPOSE` or Compose `ports` match `network.listen_port`.
  4. Entrypoint alignment: `CMD`/`ENTRYPOINT` invokes `runtime.start_command` or designated executable.
  5. Build context alignment: Compose `build.context` resolves correctly to application root.
- **Implementation Details:**
  - Implement `validate_blueprint_compatibility(artifact_path: Path, blueprint: ProjectBlueprint) -> CompatibilityResult`.
  - Record detailed mismatch reasons if compatibility check fails.
- **Dependencies:** Step 1.1, Step 2.1.
- **Tests Required:** Unit tests in `tests/unit/test_blueprint_compatibility.py` covering port mismatches, path drift, obsolete base images, and valid matching Dockerfiles.
- **Acceptance Criteria:** Incompatible artifacts are rejected with specific diagnostic reasons; compatible artifacts are approved for reuse.
- **Potential Risks/Regressions:** Over-strict base image matching (e.g. rejecting custom internal base images); mitigated by checking language compatibility rather than exact tag string equality.

---

#### Step 2.3: G2 Existing Artifact Gate & Reuse Engine

- **Objective:** Coordinate Level 1 and Level 2 validation to decide whether to reuse existing artifacts or proceed to blueprint-grounded generation.
- **Files Affected:**
  - Create `backend/src/validation/artifact_gate.py`
  - Update [`backend/src/generation/service.py`](file:///C:/IMP/antigravity-cli/Major%20Project/Devops%20Automation/backend/src/generation/service.py)
- **Current Behavior:** Unconditional generation or naive reuse.
- **Required Behavior:** If existing artifacts pass both Level 1 and Level 2, mark G2 as PASSED and bypass generation; if artifacts are absent or fail either level, mark G2 as REGENERATE_REQUIRED and proceed to Stage 3 (Synthesis).
- **Implementation Details:**
  - Implement `evaluate_existing_artifact_gate(repo_path: Path, blueprint: ProjectBlueprint) -> ArtifactGateDecision`.
  - Log reason for artifact rejection to deployment timeline.
- **Dependencies:** Step 2.1, Step 2.2.
- **Tests Required:** Integration tests in `tests/unit/test_artifact_gate.py`.
- **Acceptance Criteria:** Valid Dockerfile is reused without invoking AI generation; invalid/outdated Dockerfile is rejected with diagnostics and triggers generation.
- **Potential Risks/Regressions:** Unnecessary regeneration for minor differences; mitigated by clear tolerance rules for non-conflicting environment variables and build arguments.

---

### Phase 3: Blueprint-Grounded Synthesis & Pre-Execution Consistency (G3 Gate)

#### Step 3.1: Blueprint-Grounded Prompt Compilation & Generation

- **Objective:** Derive all Dockerfile and Compose generation instructions directly from the authoritative `ProjectBlueprint`, removing all path heuristics.
- **Files Affected:**
  - Modify [`backend/src/generation/prompt_compiler.py`](file:///C:/IMP/antigravity-cli/Major%20Project/Devops%20Automation/backend/src/generation/prompt_compiler.py)
  - Modify [`backend/src/generation/service.py`](file:///C:/IMP/antigravity-cli/Major%20Project/Devops%20Automation/backend/src/generation/service.py)
- **Current Behavior:** Prompt compiler injects monorepo rules with hardcoded assumptions (`npm run build --prefix <subfolder>` while copying files flat into `/app`), causing Next.js to crash when looking for pages/app.
- **Required Behavior:** Prompt compiler constructs prompts strictly parameterized by:
  - Exact `build_config.source_dir` for `WORKDIR`.
  - Exact `build_config.install_command` and `build_config.build_command`.
  - Discovered `build_config.artifact_output_dir`.
  - Exact `runtime.package_manager` and `runtime.start_command`.
  - Explicit multi-stage build instructions: copy manifests, install dependencies, copy source preserving subfolder tree, build, and copy only production artifacts into runner stage.
- **Implementation Details:**
  - Refactor `compile_dockerfile_prompt(blueprint: ProjectBlueprint) -> str`.
  - Refactor `compile_compose_prompt(blueprint: ProjectBlueprint) -> str`.
  - Delete all legacy hardcoded directory replacement rules.
- **Dependencies:** Step 1.1, Step 2.3.
- **Tests Required:** Prompt compiler tests in `tests/unit/test_prompt_compiler.py` testing root apps, nested apps, and multi-package workspaces.
- **Acceptance Criteria:** Generated prompts reference exact blueprint paths; zero instances of directory flattening or hardcoded prefix guesses.
- **Potential Risks/Regressions:** LLM prompt drift; mitigated by strict system prompt formatting and few-shot structural templates grounded in blueprint fields.

---

#### Step 3.2: Removal of Ad-Hoc Directory Munging in Backend Routes

- **Objective:** Eliminate legacy regex replacements and ad-hoc directory manipulations in the backend API routes.
- **Files Affected:**
  - Modify [`backend/src/deployments/routes.py`](file:///C:/IMP/antigravity-cli/Major%20Project/Devops%20Automation/backend/src/deployments/routes.py)
- **Current Behavior:** Route handlers perform string replacements for `frontend`, `backend`, `client`, etc., on incoming manifests.
- **Required Behavior:** Manifests are emitted directly as generated and validated against the blueprint; zero ad-hoc string replacements.
- **Implementation Details:**
  - Remove all manual substring substitution routines.
  - Route handlers pass the validated `ProjectBlueprint` alongside manifests to the agent.
- **Dependencies:** Step 3.1.
- **Tests Required:** Route tests in `tests/unit/test_routes.py` verifying raw manifest transmission.
- **Acceptance Criteria:** Manifest payload delivered to agent exactly matches compiler output.
- **Potential Risks/Regressions:** Breaking older agent versions expecting manipulated manifests; mitigated by updating agent executor simultaneously in Phase 4.

---

#### Step 3.3: G3 Pre-Execution Consistency Gate Implementation

- **Objective:** Assert static consistency between generated/reused deployment manifests and the physical filesystem before executing container runtimes.
- **Files Affected:**
  - Create `backend/src/validation/consistency_gate.py`
  - Create `agent/internal/executor/consistency.go`
- **Current Behavior:** No consistency check; execution is attempted even if build context or copied files do not exist.
- **Required Behavior:** G3 asserts:
  1. Build context path exists on disk.
  2. Every source file/directory referenced in `COPY` and `ADD` exists within the build context.
  3. Exposed ports in Dockerfile match service ports in Compose and `network.listen_port` in blueprint.
  4. Compose service environment variables contain required blueprint variables.
- **Implementation Details:**
  - Implement `VerifyConsistencyGate(repoDir string, manifestPath string, bp *ProjectBlueprint) error` in Go and Python.
  - If G3 fails: abort execution before invoking Docker/Kubernetes and transition to the recovery router with specific inconsistency details.
- **Dependencies:** Step 1.1, Step 3.1.
- **Tests Required:** Unit tests in `tests/unit/test_consistency_gate.py` and `agent/internal/executor/consistency_test.go`.
- **Acceptance Criteria:** Missing COPY source or port mismatch fails G3 immediately without invoking container runtime.
- **Potential Risks/Regressions:** Valid dynamic build-arg-dependent COPY paths failing static check; mitigated by resolving build-arg defaults during AST parsing.

---

### Phase 4: Agent Execution Engine Overhaul & Error Classification (G4 & G5 Gates)

#### Step 4.1: Purge of Blind 10-Attempt Loop & Ad-Hoc Heuristics in `deployment.go`

- **Objective:** Remove the blind `maxAttempts := 10` retry loop and all hardcoded directory slice iterations (`frontend`, `Frontent`, `client`, `ui`, `code-review`).
- **Files Affected:**
  - Modify [`agent/internal/executor/deployment.go`](file:///C:/IMP/antigravity-cli/Major%20Project/Devops%20Automation/agent/internal/executor/deployment.go)
- **Current Behavior:** Retries `docker compose up --build` 10 times unconditionally regardless of error type; loops over slice `[]string{"Frontent", "frontend", "client", "ui", "web"}` trying ad-hoc directory replacements.
- **Required Behavior:** Remove the loop and directory slices completely. Replace with single execution per attempt governed by the contextual error classifier.
- **Implementation Details:**
  - Delete `for attempt := 1; attempt <= maxAttempts; attempt++` and replace with controlled execution loop.
  - Delete `for _, sub := range []string{"Frontent", "frontend", ...}` and `for _, bad := range []string{...}`.
  - All directory paths and build contexts are taken directly from the blueprint contract.
- **Dependencies:** Step 1.1, Step 3.3.
- **Tests Required:** Unit tests in `agent/internal/executor/deployment_test.go`.
- **Acceptance Criteria:** Zero hardcoded directory names remaining in `deployment.go`; zero blind retries of deterministic errors.
- **Potential Risks/Regressions:** Regressing on poorly structured repos that previously worked by accident due to string replacement; mitigated by proper blueprint synthesis in Phase 1 & 3.

---

#### Step 4.2: Contextual Error Classification Engine

- **Objective:** Implement an error classifier that categorizes failures into Deterministic, Transient, Application-Source, and Engine-Internal based on stage, exit code, and log signatures.
- **Files Affected:**
  - Create `agent/internal/executor/error_classifier.go`
  - Create `agent/internal/executor/error_classifier_test.go`
- **Current Behavior:** All non-zero exit codes are treated identically and blindly retried up to 10 times.
- **Required Behavior:**
  - **Deterministic (Fast-fail on Attempt 1):** TypeScript errors (`TS2307`), syntax errors, missing modules (`Cannot find module`), compilation errors, Dockerfile syntax errors, port binding collisions (`bind: address already in use`).
  - **Transient (Max 2 retries, 3s and 6s backoff):** Network timeouts (`dial tcp: i/o timeout`, `TLS handshake timeout`), package registry rate limits (`429 Too Many Requests`), temporary Docker daemon socket locks.
  - **Application-Source:** Code-level runtime exceptions, missing project files, unhandled exceptions inside user code. Surface clearly to operator with zero modifications to user source.
  - **Engine-Internal:** Manifest generation syntax bugs, internal command dispatch errors.
- **Implementation Details:**
  - Implement `ClassifyError(stage string, exitCode int, stdout string, stderr string) ExecutionError`.
  - Maintain signature patterns for known deterministic and transient failure modes.
- **Dependencies:** Step 4.1.
- **Tests Required:** Comprehensive unit tests in `agent/internal/executor/error_classifier_test.go` testing >25 error log outputs across languages (Node, Python, Go, Rust, Docker).
- **Acceptance Criteria:** Deterministic compilation error triggers fast-fail on Attempt 1; network timeout triggers transient retry with backoff.
- **Potential Risks/Regressions:** Misclassifying a transient error as deterministic; mitigated by defaulting unrecognized errors with network indicators to transient (max 1 retry) and all other unrecognized errors to deterministic fast-fail.

---

#### Step 4.3: G4 Build / Compile Gate Implementation

- **Objective:** Execute container/image build and enforce G4 validation before advancing to container startup.
- **Files Affected:**
  - Create `agent/internal/executor/build_gate.go`
  - Modify [`agent/internal/executor/deployment.go`](file:///C:/IMP/antigravity-cli/Major%20Project/Devops%20Automation/agent/internal/executor/deployment.go)
- **Current Behavior:** Build and apply are lumped together into `docker compose up --build -d` without separating build verification from runtime startup.
- **Required Behavior:** Separate build phase (`docker compose build` or `docker build`) from apply phase. G4 asserts:
  1. Build process exits with code 0.
  2. Designated image tags are successfully created in the local or remote daemon.
  3. If build fails: classify error immediately. If deterministic, fast-fail on Attempt 1 and transition to diagnostic recovery; if transient, retry up to 2 times with 3s/6s backoff.
- **Implementation Details:**
  - Implement `ExecuteBuildGate(ctx context.Context, req *DeploymentRequest) (*GateResult, error)`.
  - Stream build output line-by-line into deployment telemetry while monitoring for error signatures.
- **Dependencies:** Step 4.1, Step 4.2.
- **Tests Required:** Unit and integration tests in `agent/internal/executor/build_gate_test.go`.
- **Acceptance Criteria:** Deterministic compilation error halts on Attempt 1 within 15 seconds instead of 15 minutes; image created on success.
- **Potential Risks/Regressions:** Increased overall execution time if separating build and apply is not cached; mitigated by Docker BuildKit layer caching.

---

#### Step 4.4: G5 Apply / Startup Gate Implementation (Compose & Kubernetes)

- **Objective:** Deploy built images and verify that containers transition out of `Pending`/`Creating` without crashing immediately.
- **Files Affected:**
  - Create `agent/internal/executor/apply_gate.go`
  - Modify [`agent/internal/executor/deployment.go`](file:///C:/IMP/antigravity-cli/Major%20Project/Devops%20Automation/agent/internal/executor/deployment.go)
- **Current Behavior:** `docker compose up -d` runs, but immediate crashes (`Exit 1`) are not distinguished from build failures or health check failures.
- **Required Behavior:**
  - **Docker Compose:** Run `docker compose up -d --no-build`. Inspect container states via `docker compose ps --format json`. Assert state is `running` and exit code is 0.
  - **Kubernetes:** Apply manifests via `kubectl apply -f`. Monitor pod lifecycle (`kubectl rollout status`). Detect `CrashLoopBackOff`, `ImagePullBackOff`, `CreateContainerConfigError`.
  - If container crashes immediately on startup: capture container logs (`docker compose logs` or `kubectl logs`), classify failure, and transition to recovery router.
- **Implementation Details:**
  - Implement `ExecuteApplyGate(ctx context.Context, req *DeploymentRequest) (*GateResult, error)`.
  - Poll container status every 1 second for up to 15 seconds to ensure no immediate startup crashes.
- **Dependencies:** Step 4.3.
- **Tests Required:** Tests in `agent/internal/executor/apply_gate_test.go` verifying clean startup and immediate crash detection (`CrashLoopBackOff`).
- **Acceptance Criteria:** Container startup failures captured immediately with container logs attached; successful startups advance to G6.
- **Potential Risks/Regressions:** Slow container initialization mistaken for startup crash; mitigated by verifying container state is `running` or `created` before checking process stability.

---

### Stage 5: Workload-Aware Verification & Final Deployment Gate (G6 & G7 Gates)

#### Step 5.1: Target-Aware Workload Verification Engine

- **Objective:** Implement health verification tailored to the workload type (`web_service`, `tcp_service`, `background_worker`, `batch_job`, `static_spa`).
- **Files Affected:**
  - Create `agent/internal/executor/workload_verifier.go`
  - Create `agent/internal/executor/workload_verifier_test.go`
- **Current Behavior:** Blind HTTP GET request to `http://localhost:<port>` or no verification at all; non-web workers fail because they do not listen on HTTP ports.
- **Required Behavior:**
  - **Web Service / Static SPA:** Poll `http://<host>:<port><health_check_path>` over a 30-second window. Pass on HTTP status code in range 200–399.
  - **TCP Service:** Perform TCP socket handshake on designated port. Pass on successful connection.
  - **Background Worker:** Observe container status for 15 seconds. Pass if container remains in `running` state with restart count = 0. Zero HTTP polling.
  - **Batch Job:** Await container exit. Pass if exit code == 0.
- **Implementation Details:**
  - Implement `VerifyWorkloadHealth(ctx context.Context, bp *ProjectBlueprint, target TargetConfig) error`.
  - Dispatch verification strategy based on `bp.WorkloadType`.
- **Dependencies:** Step 1.1, Step 4.4.
- **Tests Required:** Unit tests in `agent/internal/executor/workload_verifier_test.go` covering HTTP 200, HTTP 500, TCP connect, background worker stability, and batch exit 0.
- **Acceptance Criteria:** Background workers pass verification without HTTP probes; web services verify on correct blueprint port and path.
- **Potential Risks/Regressions:** Application taking longer than 30s to initialize (e.g. heavy Java app); mitigated by configurable `readiness_timeout` in `BuildConfig` defaulting to 30s for Node/Go and 60s for Java.

---

#### Step 5.2: G6 Workload Verification / Health Gate Implementation

- **Objective:** Integrate workload verification into the execution pipeline as Gate G6.
- **Files Affected:**
  - Create `agent/internal/executor/workload_gate.go`
  - Modify [`agent/internal/executor/deployment.go`](file:///C:/IMP/antigravity-cli/Major%20Project/Devops%20Automation/agent/internal/executor/deployment.go)
- **Current Behavior:** Missing or rudimentary health verification.
- **Required Behavior:** G6 executes `VerifyWorkloadHealth`. If verification fails, fetch stdout/stderr container logs and capture them in the `DiagnosticBundle` before routing to the recovery router.
- **Implementation Details:**
  - Implement `ExecuteWorkloadGate(ctx context.Context, req *DeploymentRequest) (*GateResult, error)`.
  - Record response times and status codes in deployment telemetry.
- **Dependencies:** Step 5.1.
- **Tests Required:** Integration tests in `agent/internal/executor/workload_gate_test.go`.
- **Acceptance Criteria:** Passing health checks advance to G7; failing health checks attach container logs and trigger recovery router.
- **Potential Risks/Regressions:** Ephemeral network blips during HTTP polling; mitigated by polling with 2-second intervals across the verification window.

---

#### Step 5.3: G7 Final Deployment Gate & Traffic Readiness Sign-Off

- **Objective:** Provide the final operational sign-off confirming stability, port bindings, ingress routing, and transitioning the deployment to `SUCCESS`.
- **Files Affected:**
  - Create `agent/internal/executor/final_gate.go`
  - Modify [`agent/internal/executor/dispatcher.go`](file:///C:/IMP/antigravity-cli/Major%20Project/Devops%20Automation/agent/internal/executor/dispatcher.go)
  - Modify [`backend/src/deployments/routes.py`](file:///C:/IMP/antigravity-cli/Major%20Project/Devops%20Automation/backend/src/deployments/routes.py)
- **Current Behavior:** Deployment marked successful as soon as `docker compose up` command exits, even if containers crash a second later.
- **Required Behavior:** G7 verifies that all endpoints, ingress/port bindings, and container statuses are verified, stable, and ready for traffic. Updates deployment status in database to `SUCCESS` with full telemetry and access URLs.
- **Implementation Details:**
  - Implement `ExecuteFinalDeploymentGate(ctx context.Context, req *DeploymentRequest) (*GateResult, error)`.
  - Publish final deployment event with URL, exposed ports, and verification summary.
- **Dependencies:** Step 5.2.
- **Tests Required:** Integration tests in `tests/e2e/test_final_gate.py`.
- **Acceptance Criteria:** Deployment is marked `SUCCESS` only when G1 through G7 have all passed.
- **Potential Risks/Regressions:** UI timing out while waiting for G7; mitigated by continuous SSE progress events emitted at each gate transition.

---

### Stage 6: Bounded AI Recovery, Diagnostic Bundling & User Source Protection

#### Step 6.1: Diagnostic Bundle Aggregation Engine

- **Objective:** Construct a structured diagnostic bundle whenever any gate G1–G7 fails.
- **Files Affected:**
  - Create `agent/internal/executor/diagnostics.go`
  - Create `backend/src/diagnostics/bundle.py`
- **Current Behavior:** Only raw error string from shell command sent to frontend.
- **Required Behavior:** Bundle contains:
  1. Failed gate identifier (G1–G7) and execution stage.
  2. The authoritative `ProjectBlueprint`.
  3. Generated deployment manifests (`Dockerfile`, `docker-compose.yml`, K8s).
  4. Command line, exit code, stdout, stderr, and attempt count.
  5. Container inspect state (exit code, restart count, health status, OOM status).
  6. Recent container logs (last 100 lines).
  7. Directory tree snippet around application root.
- **Implementation Details:**
  - Implement `AssembleDiagnosticBundle(...) -> DiagnosticBundle` in Go and Python.
  - Serialize into standardized JSON structure.
- **Dependencies:** Step 1.1, Step 4.2.
- **Tests Required:** Unit tests in `tests/unit/test_diagnostic_bundle.py` and `agent/internal/executor/diagnostics_test.go`.
- **Acceptance Criteria:** Complete diagnostic bundle emitted on failure, containing all 7 components.
- **Potential Risks/Regressions:** Large bundle size if logs are voluminous; mitigated by truncating logs to last 100 lines (max 64KB).

---

#### Step 6.2: Root Cause Classification & Bounded AI Recovery Router

- **Objective:** Coordinate AI resolution across classified failure categories, strictly bounded to 1–3 iterations, with an absolute restriction against modifying user application source code.
- **Files Affected:**
  - Create `backend/src/recovery/ai_resolver.py`
- **Current Behavior:** Blind retry or generic prompt asking LLM to fix errors without guardrails.
- **Required Behavior:**
  - Classify root cause into:
    1. _Blueprint Mismatch:_ Update blueprint field and re-synthesize.
    2. _Artifact Configuration Defect:_ Modify Dockerfile/Compose instructions.
    3. _Application Source Defect:_ **STRICT GUARD:** Halt immediately, present actionable diagnostic report to operator. Do NOT attempt to modify application code.
    4. _Infrastructure Defect:_ Halt and report daemon/port conflict.
  - Enforce iteration limit: max 1 to 3 attempts. If limit exceeded, halt with diagnostic report.
- **Implementation Details:**
  - Implement `resolve_failure_with_ai(bundle: DiagnosticBundle, iteration: int) -> RecoveryPlan`.
  - Assert that proposed recovery plan contains only manifest or blueprint modifications, zero file modifications within the user's source tree.
- **Dependencies:** Step 6.1.
- **Tests Required:** Unit tests in `tests/unit/test_ai_resolver.py` verifying source-code mutation prohibition and iteration bounding.
- **Acceptance Criteria:** Source code defects halt immediately with clear diagnosis; artifact bugs trigger bounded fix; iteration 4 is rejected.
- **Potential Risks/Regressions:** LLM attempting to output code diffs for user files; mitigated by strict validator rejecting any patch targeting files outside deployment manifests.

---

#### Step 6.3: Proposed Fix Validation & Re-Execution

- **Objective:** Subject AI-proposed fixes to Level 1 syntax and Level 2 blueprint validation before re-executing the failed gate.
- **Files Affected:**
  - Create `backend/src/recovery/fix_validator.py`
  - Modify [`backend/src/generation/service.py`](file:///C:/IMP/antigravity-cli/Major%20Project/Devops%20Automation/backend/src/generation/service.py)
- **Current Behavior:** LLM output applied directly without validation.
- **Required Behavior:** Proposed fix must pass `validate_dockerfile_syntax` and `validate_blueprint_compatibility` before re-execution. If the proposed fix fails validation, reject it immediately without running the container engine.
- **Implementation Details:**
  - Implement `validate_and_apply_fix(fix: RecoveryPlan, blueprint: ProjectBlueprint) -> ValidationResult`.
  - Re-execute pipeline starting from the failed gate.
- **Dependencies:** Step 2.1, Step 2.2, Step 6.2.
- **Tests Required:** Unit tests in `tests/unit/test_fix_validator.py`.
- **Acceptance Criteria:** Invalid AI fixes rejected statically; valid fixes applied and re-executed from target gate.
- **Potential Risks/Regressions:** Infinite loop between failed execution and failed fix; mitigated by the hard iteration counter (max 3).

---

### Stage 7: Heterogeneous Archetype Validation, Failure Injection & Anti-Pattern Linter

#### Step 7.1: Anti-Pattern Regression Linter

- **Objective:** Prevent regression by establishing a static test that bans hardcoded application/directory names and ad-hoc string replacements across ForgeOps code.
- **Files Affected:**
  - Create `tests/lint/test_anti_patterns.py`
- **Current Behavior:** Hardcoded strings exist in `deployment.go` and `routes.py`.
- **Required Behavior:** Static test scans all `.py` and `.go` source files in ForgeOps codebase and asserts:
  - Zero occurrences of hardcoded application directory names: `"frontend"`, `"Frontent"`, `"backend"`, `"client"`, `"ui"`, `"code-review"`.
  - Zero occurrences of hardcoded prefix build flags (`--prefix <name>`).
  - Zero occurrences of arbitrary retry loops (`for attempt := 1; attempt <= 10`).
- **Implementation Details:**
  - Implement AST and regex scanners across `backend/` and `agent/`.
  - Fail CI build if any prohibited pattern is detected.
- **Dependencies:** Step 3.1, Step 3.2, Step 4.1.
- **Tests Required:** `pytest tests/lint/test_anti_patterns.py`.
- **Acceptance Criteria:** Anti-pattern test passes cleanly on refactored codebase.
- **Potential Risks/Regressions:** False positives on variable names like `frontend_url`; mitigated by checking string literals used in path joining and command execution.

---

#### Step 7.2: Heterogeneous Test Matrix for 12 Archetypes

- **Objective:** Validate end-to-end deployment across 12 diverse application archetypes without repository-specific logic.
- **Files Affected:**
  - Create `tests/e2e/test_heterogeneous_archetypes.py`
  - Create test fixture repositories under `tests/fixtures/archetypes/`:
    1. `single_app_nextjs/` (Root Next.js)
    2. `nested_nextjs/` (Nested subfolder Next.js, e.g. `code-review/`)
    3. `static_spa/` (Vite / React / Nginx)
    4. `node_backend/` (Express / Fastify)
    5. `python_web/` (FastAPI with `pyproject.toml` / Poetry)
    6. `python_worker/` (Background Celery queue consumer, no network port)
    7. `go_service/` (Go multi-stage binary)
    8. `rust_service/` (Rust Cargo release binary)
    9. `java_spring/` (Maven / OpenJDK multi-stage)
    10. `pnpm_monorepo/` (Multi-package workspace with shared libraries)
    11. `existing_valid_dockerfile/` (Pre-existing verified Dockerfile)
    12. `existing_invalid_dockerfile/` (Pre-existing invalid Dockerfile triggering regeneration)
- **Current Behavior:** Tested primarily on a single portfolio app; fails on nested Next.js and unhandled architectures.
- **Required Behavior:** All 12 archetypes pass G1 through G7 automatically using the generic blueprint pipeline.
- **Implementation Details:**
  - Execute end-to-end pipeline against each fixture repository.
  - Verify G1 detection, G2 artifact choice, G3 consistency, G4 build, G5 apply, G6 workload verification, and G7 sign-off.
- **Dependencies:** Phases 1 through 5.
- **Tests Required:** `pytest tests/e2e/test_heterogeneous_archetypes.py`.
- **Acceptance Criteria:** All 12 archetypes succeed through G7; worker archetype passes without HTTP probes; existing valid Dockerfile is reused without AI generation.
- **Potential Risks/Regressions:** Build time in CI for compiling Rust/Java; mitigated by lightweight minimal fixture projects.

---

#### Step 7.3: Failure-Injection Test Suite

- **Objective:** Verify error classification, fast-fail behavior, transient retry backoff, and source protection under injected failure conditions.
- **Files Affected:**
  - Create `tests/e2e/test_failure_injection.py`
- **Current Behavior:** Failures trigger 10 blind retries and indefinite delays.
- **Required Behavior:**
  1. _Deterministic Compile Failure:_ Injected TypeScript syntax error -> Verified fast-fail on Attempt 1; total time < 15s.
  2. _Transient Network Timeout:_ Injected simulated network blip -> Verified retry with 3s and 6s backoff, succeeding on recovery.
  3. _Application Source Defect:_ Injected unhandled runtime crash in user code -> Verified G6 catches crash, captures logs, halts without mutating source code.
  4. _Port Conflict:_ Injected port collision -> Verified fast-fail and diagnostic report.
- **Implementation Details:**
  - Run failure scenarios using mock and real execution harnesses.
  - Assert exact attempt counts, timing, and diagnostic outputs.
- **Dependencies:** Step 4.2, Step 6.1, Step 6.2.
- **Tests Required:** `pytest tests/e2e/test_failure_injection.py`.
- **Acceptance Criteria:** Deterministic error halts on Attempt 1; transient error retries <= 2 times; user source code remains unaltered.
- **Potential Risks/Regressions:** Flaky timing in CI for backoff tests; mitigated by measuring sleep call invocations in unit tests and using bounded assertions in integration tests.

---

#### Step 7.4: Explicit Nested Next.js Subdirectory Regression Test

- **Objective:** Prove that the original motivating failure case (`code-review/` Next.js subfolder) deploys cleanly without any application-specific logic.
- **Files Affected:**
  - Create `tests/regression/test_nested_nextjs_regression.py`
- **Current Behavior:** Flattened into `/app` with `COPY code-review ./.`, Next.js build failed searching for pages/app directories, followed by 10 blind retries.
- **Required Behavior:**
  - Scanner detects Next.js application inside subfolder.
  - Workspace resolver identifies subfolder as application root.
  - Blueprint sets `source_dir` to the subfolder.
  - Prompt compiler generates multi-stage Dockerfile maintaining exact directory structure.
  - G4 build succeeds on Attempt 1.
  - G6 verifies HTTP 200 on exposed port.
  - G7 marks deployment `SUCCESS`.
- **Implementation Details:**
  - Run end-to-end deployment on reproduction repository fixture.
  - Assert zero retry attempts, zero regex manipulations, and complete success.
- **Dependencies:** Phases 1 through 5, Step 7.1.
- **Tests Required:** `pytest tests/regression/test_nested_nextjs_regression.py`.
- **Acceptance Criteria:** The nested Next.js application deploys successfully on Attempt 1.
- **Potential Risks/Regressions:** Regressing on root-level Next.js apps; verified by running alongside Step 7.2.

---

### Stage 8: Logging, Observability, Migration & Rollout

#### Step 8.1: Logging, Diagnostics & Real-Time Observability

- **Objective:** Provide granular real-time visibility into each gate transition and error classification.
- **Files Affected:**
  - Modify [`backend/src/deployments/routes.py`](file:///C:/IMP/antigravity-cli/Major%20Project/Devops%20Automation/backend/src/deployments/routes.py)
  - Modify [`agent/internal/executor/dispatcher.go`](file:///C:/IMP/antigravity-cli/Major%20Project/Devops%20Automation/agent/internal/executor/dispatcher.go)
- **Current Behavior:** Vague progress messages (`"building and starting containers (attempt 1/10)..."`).
- **Required Behavior:** Emit structured JSON SSE events for every gate transition:
  - `{"gate": "G1", "status": "PASSED", "message": "Blueprint validated for Node.js 20 Next.js"}`
  - `{"gate": "G4", "status": "BUILDING", "attempt": 1, "class": "INITIAL"}`
  - `{"gate": "G4", "status": "FAILED", "attempt": 1, "class": "DETERMINISTIC", "reason": "TypeScript compilation error"}`
- **Implementation Details:**
  - Standardize event schema across Go agent and Python backend.
  - Stream events to frontend client via SSE.
- **Dependencies:** All previous phases.
- **Tests Required:** Unit and integration tests in `tests/unit/test_telemetry.py`.
- **Acceptance Criteria:** Real-time event log in UI displays exact gate progress and error classifications.
- **Potential Risks/Regressions:** Excessive event frequency flooding SSE connection; mitigated by debouncing rapid build output lines.

---

#### Step 8.2: Dual-Mode Feature Flag & Migration Strategy

- **Objective:** Enable safe phased migration from legacy heuristics to the generic blueprint engine.
- **Files Affected:**
  - Modify [`backend/src/generation/service.py`](file:///C:/IMP/antigravity-cli/Major%20Project/Devops%20Automation/backend/src/generation/service.py)
  - Modify [`agent/internal/executor/dispatcher.go`](file:///C:/IMP/antigravity-cli/Major%20Project/Devops%20Automation/agent/internal/executor/dispatcher.go)
- **Current Behavior:** Legacy heuristics hardcoded into execution path.
- **Required Behavior:** Introduce environment variable `FORGEOPS_GENERIC_ENGINE_V2=true` (default: `true`).
  - When enabled: full G1–G7 pipeline, blueprint synthesis, and intelligent error classifier.
  - Allows side-by-side verification and instant fallback if necessary.
- **Implementation Details:**
  - Guard new pipeline execution behind configuration check.
  - Once all 12 archetypes pass, remove legacy branches.
- **Dependencies:** All previous steps.
- **Tests Required:** Test suite runs with feature flag enabled.
- **Acceptance Criteria:** Engine defaults to V2 generic engine; all legacy heuristics bypassed.
- **Potential Risks/Regressions:** State inconsistency between legacy and V2 models; mitigated by strictly scoping V2 to new deployment instances.

---

## 4. Phase-by-Phase Acceptance Criteria & Review Checkpoints

| Phase       | Description                | Acceptance Gate Criteria                                                                                                                                                                    | Review Checkpoint                                                             |
| :---------- | :------------------------- | :------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | :---------------------------------------------------------------------------- |
| **Phase 1** | Blueprint & Detection      | Manifest discovery works recursively without directory assumptions. Blueprint Pydantic and Go structs serialize identically. Monorepo DAG resolves runnable targets. G1 halts on ambiguity. | Inspect blueprint detection tests and schema roundtrip.                       |
| **Phase 2** | Artifact Validation        | Dockerfiles, Compose, and K8s validate for syntax (Level 1) and blueprint compatibility (Level 2). Incompatible artifacts rejected with diagnostic reason.                                  | Inspect artifact validation test results on valid and broken Dockerfiles.     |
| **Phase 3** | Synthesis & Consistency    | Generated prompts strictly parameterized by blueprint. Ad-hoc directory replacements removed from backend. G3 catches missing files/ports before execution.                                 | Inspect generated Dockerfiles for nested apps and prompt compiler unit tests. |
| **Phase 4** | Execution & Classification | Blind 10-attempt loop removed from `deployment.go`. Deterministic errors fast-fail on Attempt 1. Transient errors retry <= 2 times with 3s/6s backoff. G4 and G5 gates separated.           | Run failure-injection tests verifying Attempt 1 fast-fail for compile errors. |
| **Stage 5** | Workload Verification      | G6 health checks tailored to workload type (HTTP, TCP, worker, batch). Background workers verify without HTTP probes. G7 gives final sign-off before `SUCCESS`.                             | Run worker fixture test and web service health verification.                  |
| **Stage 6** | Diagnostic Recovery        | Comprehensive diagnostic bundle emitted on failure. AI recovery strictly bounded to 1–3 iterations. Zero modifications to user application source code.                                     | Verify source code protection and bounded recovery test cases.                |
| **Stage 7** | Validation & Anti-Patterns | Anti-pattern linter passes with zero hardcoded directory names. All 12 heterogeneous archetypes deploy cleanly. Original nested Next.js regression test passes on Attempt 1.                | Review complete 12-archetype test matrix report and linter output.            |
| **Stage 8** | Migration & Observability  | Structured SSE events emitted for G1–G7 transitions. Feature flag operational. System ready for production deployment.                                                                      | End-to-end deployment verification on live test containers.                   |

---

## 5. Summary of Prohibitions & Invariants

Throughout all implementation steps, the following strict invariants must be maintained:

1. **Zero Hardcoded Directory Assumptions:** Never write `if name in ["frontend", "backend", "client", "code-review"]`.
2. **Zero Hardcoded Framework Commands:** Never inject `--prefix <subfolder>` or arbitrary build flags not derived from the blueprint.
3. **Zero Blind Retries:** Never retry an execution without checking if the error is classified as transient.
4. **Zero Source Code Mutation:** Never allow ForgeOps or the AI resolution loop to modify the user's application source files.
5. **Blueprint as Single Source of Truth:** Never execute generation or deployment decisions that contradict the authoritative `ProjectBlueprint`.
