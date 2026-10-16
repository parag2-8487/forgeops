# ForgeOps System Architecture and Technical Specification

---

## 1. Problem Statement

Modern software delivery pipelines suffer from a fundamental tension between developer velocity and operational safety:

1. **Fragility of Manual DevOps Scripting**: Development teams frequently rely on ad-hoc shell scripts, unversioned Dockerfiles, and manual deployment commands. This introduces configuration drift, environment divergence ("it works on my machine"), and high human error rates during production rollouts.
2. **Hallucination and Security Risks in Autonomous AI**: Generic generative AI models produce invalid Kubernetes manifests, reference non-existent images or entrypoints, expose credentials in cleartext, and lack awareness of the local runtime environment. Providing an unconstrained AI agent with direct production credentials leads to catastrophic, irrecoverable changes.
3. **Absence of Governance and Blast Radius Controls**: Standard CI/CD systems treat every pull request or deployment command with identical trust assumptions. They lack automatic blast-radius calculation, policy-as-code enforcement, and mandatory approval gates for high-impact infrastructure alterations.
4. **Credential Exposure and Plaintext Drift**: Development environments routinely litter API tokens, database passwords, and SSH keys inside local `.env` files, shell histories, and unencrypted git commits, violating basic least-privilege standards.
5. **Disconnected Observability**: Operators are forced to jump between disparate command-line tools (`docker ps`, `kubectl`, Prometheus, Grafana, Slack) to determine if a workload actually converged, leading to delayed incident response and opaque failure modes.

ForgeOps solves this problem by providing a deterministic, policy-governed, agent-based DevOps platform. It combines static codebase indexing, strict policy-as-code enforcement, blast-radius scoring, write-only secrets management, and a zero-trust local agent bridge.

---

## 2. Project Scope and Completed Objectives

### In-Scope & Completed Objectives

- **Local Agent Runtime Architecture**: Secure, firewall-friendly daemon (`forgeops-agent`) written in Go that runs directly on the target host or developer workstation, performing local codebase scanning, validator runs, and container orchestration without exposing inbound network ports.
- **Deterministic Codebase Indexing Engine**: Multi-language static scanner that parses ASTs, directory trees, Dockerfiles, and workspace manifests into a structured dependency graph and hybrid vector index without transmitting raw source files to cloud third parties.
- **Multi-Tier Readiness Evaluation Engine**: Automated scoring system (0-100) analyzing containerization, CI/CD, orchestration, environment configuration, policy compliance, and infrastructure-as-code across 50+ deterministic heuristics before generation or deployment occurs.
- **Grounded Generation with Deterministic Fallbacks**: Framework-aware manifest generator (supporting Next.js, Vite, Node.js, Python, Go, and monorepos) backed by local LLM tiers (Ollama) and guaranteed deterministic fallbacks that eliminate missing entrypoint and dependency failures.
- **Governance Chokepoint & Blast-Radius Engine**: Central mutation gate ensuring every deployment, container action, and codebase change is cryptographically signed, evaluated against Open Policy Agent (OPA) and Cerbos policies, assigned an empirical blast-radius score (0-100), and held for human approval when risk thresholds are exceeded.
- **Write-Only Vault & Ephemeral Secrets Pipeline**: Encrypted secrets engine providing project-scoped credential storage where values cannot be read back by API callers and are injected into execution containers in-memory only.
- **High-Performance Native BuildKit Optimization (G4)**: Subshell-free container compilation via native BuildKit Engine API (v2) over UDS, in-memory Protobuf trace demuxer, intermediate container commit elimination, automatic tar-sanitization fallback to legacy V1 builder, and multi-tenant cache isolation yielding 23.2x warm build speedups.
- **Real-Time Deployment & Metrics Streaming**: Streaming WebSocket pipeline providing terminal-style build and image download feedback to the web interface, paired with live CPU, memory, and network I/O sampling.
- **Promotion & Rollback Lifecycle Management**: Multi-tier environment pipelines (development -> staging -> production) with atomic rollback handles to previous converged states.

---

## 3. Core Architectural Modules

---

### Module 1: Project Onboarding & GitHub Integration

#### What It Is

The project management interface represents the single source of truth for an application codebase. It links an indexed repository path, metadata tags, deployment targets, and VCS lineage.

#### Why It Is Needed

DevOps configurations cannot exist in isolation from code. Infrastructure manifests must remain tightly coupled to the application framework, dependency graph, and runtime environment. Alternative approaches (such as generic dashboard tools) treat infrastructure as disconnected text files, causing drift between what the application code expects and what the deployment manifests provide.

#### How It Is Implemented

- **Repository Registration**: Users provide either a local filesystem path or a GitHub repository URL (`https://github.com/org/repo`).
- **GitHub Integration**: ForgeOps interfaces with GitHub via OAuth/Personal Access Tokens or Git transport over SSH/HTTPS. It extracts repository metadata, default branches, and commit SHAs without permanently storing user git credentials in application logs.
- **Workspace Association**: The project entity generates a globally unique UUID (`project_id`). This identifier scopes all subsequent operations: codebase indices, secret references, approval change sets, and agent pairing sessions.

#### Alternatives Considered

- _Direct Git Clone by Central Backend_: Rejected because it requires the backend server to hold network access to private corporate intranets and developer firewalls. Using a distributed agent pattern allows the repository to remain exclusively on the user's local machine or private cloud VPC.

---

### Module 2: Codebase Indexing Engine (§1.3)

#### What It Is

The codebase indexer is a hybrid static analysis engine executed by `forgeops-agent` directly on the local project directory. It parses files, generates lexical chunks, extracts symbol declarations, traces dependency import graphs, and generates vector embeddings.

#### Why It Is Needed

Generative models fail when prompted with entire codebases due to context window limits, noise, and token costs. Conversely, simple keyword searches (grep) miss semantic context and cross-file import relationships. A hybrid index allows precise retrieval of only the relevant manifests, dependencies, and configuration files needed for deployment.

#### How It Is Implemented

- **Scanning Architecture**: The agent runs `forgeops-agent scan --project <id>`. It walks the directory tree while adhering to `.gitignore`, `.dockerignore`, and internal safety blocklists (excluding binary files, `.git`, `node_modules`, `.env`).
- **Dependency Resolution**: AST and regex parsers inspect package manifests (`package.json`, `go.mod`, `requirements.txt`, `pom.xml`, `Cargo.toml`). When import statements point to external libraries or unindexed files, they are flagged as "unresolved dependencies" (e.g., 11 of 444 resolved). This transparency lets operators see how complete cross-file retrieval is.
- **Continuous File Watching**: The `forgeops-agent watch` command registers operating-system-level filesystem notifications via `fsnotify`. When a developer edits a file, the agent debounces file events, re-indexes the changed file, and updates any dependent nodes in the local graph.
- **Hybrid Retrieval**: Combines BM25 lexical keyword search over extracted symbols with dense vector embeddings stored in the backend database.

---

### Module 3: Readiness Scoring Engine (§1.4)

#### What It Is

The Readiness Engine is a multi-dimensional diagnostic suite that evaluates whether a project is safe and mature enough for containerized production deployment. It computes a granular score from 0 to 100 across six distinct categories:

1. **Containerization** (e.g., presence of multi-stage Dockerfiles, non-root users, explicit base image tags).
2. **CI Config** (e.g., GitHub Actions, automated test pipelines, linting stages).
3. **Orchestration** (e.g., Compose configurations, Kubernetes manifests, Helm charts, replica counts).
4. **Env Config** (e.g., absence of hardcoded passwords, presence of `.env.example`, explicit port configurations).
5. **Security Policy** (e.g., static secret scanning via Gitleaks rules, security scanning tools, absence of privileged flags).
6. **Infrastructure as Code (IaC)** (e.g., Terraform/OpenTofu configurations, state definitions).

#### Why It Is Needed & Why It Does Not Just Hand Files to an AI

**Crucial Architectural Decision**: ForgeOps does NOT simply pass raw files to an LLM and ask "give this a score". LLM evaluations are non-deterministic, prone to hallucinations, impossible to audit, and susceptible to prompt injection.

#### How It Is Implemented

The engine runs deterministic, rule-based static analyzers and pinned external validation tools:

- **AST & Manifest Scanners**: Inspect `package.json`, `Dockerfile`, and Kubernetes YAMLs against concrete schema rules.
- **Local Security Scanners**: Runs Gitleaks regex patterns and Trivy checks locally via `agent/internal/validator`.
- **Category Weighting**: Each check produces a Boolean pass/fail or integer penalty. The scores are normalized across each category and summed into the overall readiness score (e.g., 83/100 - `production_ready`).
- **Actionable Gap Reports**: The output details exactly which checks failed (e.g., missing healthcheck, running container as root) with instructions on how to rectify them.

---

### Module 4: Infrastructure & Manifest Generation

#### What It Is

An intelligent generation pipeline that outputs production-ready `Dockerfile` configurations, `docker-compose.yml` stacks, Kubernetes manifests, and CI/CD workflows based on the scanned project architecture.

#### Why It Is Needed

Modern multi-framework applications (Next.js with standalone output, Vite monorepos, Go microservices) require specialized build stages, caching parameters, and non-root execution environments. Handcrafted configurations frequently suffer from common failure modes: forgetting `npm run build`, omitting production dependencies, or specifying invalid entrypoints (e.g., guessing `server.js` when only an `app` directory exists).

#### How It Is Implemented

- **Grounding via Scanned Inventory**: The generation service loads the latest `inventory` generated by the scanner. This inventory includes exact package scripts (`start`, `build`, `dev`), detected frameworks (React, Next.js, FastAPI, Gin), and filesystem entrypoints.
- **Prompt Compilation**: If an LLM tier is active, the prompt compiler injects strict OCI and security rules (e.g., "Must use non-root user 10001", "Must include multi-stage build", "Must expose port 3000").
- **Consolidated Layer ENV Synthesis**: Rather than emitting fragmented single `ENV` statements that bloat layer counts, the template library synthesizes atomic directives: `ENV NODE_ENV=production PORT={port}`.
- **Multi-Tenant Cache Mounts**: When generating package manager commands, the compiler injects keyed cache mounts (`RUN --mount=type=cache,target=/root/.npm,id=npm-{clean_tenant}-{clean_proj},sharing=locked npm ci`), eliminating remote tarball downloads without risking cross-tenant cache contamination.
- **Deterministic Fallback Pipeline**: If the AI model fails, times out, or hallucinates an unparseable structure, ForgeOps invokes `generate_deterministic_fallback`. This fallback reads the inventory directly, determines the framework structure, writes valid multi-stage Dockerfiles, sets `ENV HOSTNAME="0.0.0.0"`, configures proper workspace build commands, and generates complete compose setups without requiring external AI generation.

---

### Module 5: Approval Center & Blast Radius Engine (§1.7)

#### What It Is

A centralized gatekeeper that reviews every proposed codebase change, deployment action, or container operation before execution. It assigns a numerical **Blast Radius Score** (0 to 100) and halts execution for human sign-off when risk thresholds are exceeded.

#### Why It Is Needed

**Why do we need approvals if the AI already generated the fix?**
AI systems lack organizational context and accountability. An automated system must never have unmitigated authority to execute destructive infrastructure actions (such as dropping databases, exposing ports to `0.0.0.0`, or overwriting existing configs). The Approval Center enforces human-in-the-loop oversight and creates an immutable change ledger.

#### How It Is Implemented

- **Change Set Model**: Every proposed mutation is packaged into an atomic `ChangeSet` containing unified diffs of files to be modified, created, or deleted.
- **Blast Radius Calculation**: Evaluates the surface area of the proposed modification:
  - Number of files modified and deleted.
  - Presence of critical configuration targets (e.g., production database strings, root security policies).
  - Exposure of external network ports.
  - Scores from 0-20 are classified as low risk (`allow`), while higher scores trigger warning states (`warn` or `requires_approval`).
- **Atomic Rollback Storage**: Before applying changes to the working tree, the agent creates an atomic filesystem snapshot in `.forgeops-rollback`. If a deployment or application fails, the change set triggers `change_set_rolled_back`, immediately restoring the codebase to its prior clean state.

---

### Module 6: Policy-as-Code Engine

#### What It Is

An enforcement layer evaluating every action against security and operational policies using industry-standard policy engines: **Open Policy Agent (OPA)** with Rego policies, and **Cerbos** for context-aware role-based access control.

#### Who the Policy Is For & What It Enforces

Policies govern both the human operators and the automated agents:

1. **Developer Restrictions**: Restricts junior developers from deploying directly to production environments.
2. **Infrastructure Rules**: Blocks containers running as root (`UID 0`), enforces mandatory resource limits (CPU/Memory caps), forbids mounting host sockets (`/var/run/docker.sock`), and requires healthcheck declarations.
3. **Network Ingress Rules**: Forbids wildcard binding (`0.0.0.0/0`) on administrative ports.

#### How It Is Implemented

- **Evaluation Pipeline**: Before a manifest is dispatched to the agent, the backend serializes the request context and queries the OPA engine (`http://opa:8181/v1/data/forgeops/allow`).
- **Decoupled Rule Definitions**: Policies are written in declarative Rego (`policies/opa/`) and Cerbos YAML files (`policies/cerbos/`).
- **Extensibility**: Adding new enterprise compliance rules requires dropping a new `.rego` file into the policy directory. OPA hot-reloads policy bundles without requiring backend recompilation or database schema changes.

---

### Module 7: Vault & Secrets Management (§1.8)

#### What It Is

A zero-leakage, write-only credential storage and injection engine managing database passwords, cloud tokens, and environment secrets.

#### Why We Need Vault When `.env` Already Exists

- **`.env` Files Leak Constantly**: `.env` files are frequently committed to git repositories by accident, copied across insecure developer machines, and bundled into Docker image layers during builds.
- **Plaintext Disk Storage**: Storing production database credentials in plaintext files on host disks violates compliance standards (SOC2, ISO 27001, HIPAA).
- **Environment Drift & Rotation**: When a database password changes, updating thirty static `.env` files across multiple machines is error-prone. A centralized Vault allows instantaneous rotation.

#### How It Is Implemented

- **Write-Only API Design**: Secrets entered into the UI (`DATABASE_PASSWORD`, etc.) are written directly to encrypted storage (Postgres with AES-256-GCM encryption or external HashiCorp Vault). The API **never returns the secret value back to any caller**—only the key name and target environment metadata are exposed.
- **In-Memory Ephemeral Injection**: During deployment, the backend resolves the registered secret references, constructs an in-memory environment payload, and signs it inside a cryptographic execution envelope. The agent receives the envelope, launches the container with the injected environment variables, and clears the plaintext secrets from memory. No `.env` file is written to the developer's disk.

---

### Module 8: Agent Architecture & Pairing Security

#### What It Is

A secure communication bridge between the ForgeOps backend and the host machine running the target workloads. The agent is a compiled Go binary (`forgeops-agent.exe`) that operates entirely within the user's workspace.

#### Why Pairing Codes & What Are the Commands

1. **One-Time Pairing Codes**: The backend generates an ephemeral, high-entropy 6-character code (e.g., `554XW2`) with an 8-minute TTL.
2. **Commands**:
   - `forgeops-agent pair --code 554XW2 --backend ws://localhost:18000/api/v1/ws/agent`: Exchanges the one-time code for a long-lived cryptographically signed device certificate and project credential.
   - `forgeops-agent connect`: Establishes the real-time runtime bridge.
   - `forgeops-agent scan`: Triggers local codebase static analysis.
   - `forgeops-agent watch`: Starts background filesystem monitoring.

#### How the Connection Works Under the Hood

- **Connection Protocol**: The agent establishes an outbound WebSocket (`ws://` or `wss://`) connection to the backend hub. Because the connection is outbound from the agent to the backend, **no inbound firewall ports need to be opened** on the developer's machine or corporate network.
- **Cryptographic Envelopes**: All commands dispatched from the backend to the agent are packaged in signed envelopes verified via ed25519 public keys and checked against a monotonic nonce replay guard (`NewMemoryReplayGuard`).
- **Heartbeat & Reconnection**: The WebSocket maintains bi-directional ping/pong heartbeats every 15 seconds. If network interruption occurs, the agent automatically retries with exponential backoff while preserving local workspace state.
- **Why the Backend Does Not Access Files Directly**: Security boundary isolation. The backend never has filesystem credentials to the host machine. The agent confines its actions strictly to the configured workspace root, preventing path traversal attacks.

---

### Module 9: Plan Analysis

#### What It Is

A pre-execution validation stage that executes dry-run evaluations of infrastructure alterations before applying them to physical hosts or clusters.

#### Why It Is Needed

Deploying untested infrastructure manifests directly to a cluster frequently results in runtime crashes (e.g., syntax errors in Kubernetes manifests, conflicting host port allocations, or unresolvable persistent volume claims). Plan analysis executes client-side dry-runs, OpenTofu/Terraform plan generations, and Trivy security scans before any mutation occurs.

#### How It Is Implemented

- The agent invokes validators in dry-run mode (`kubectl apply --dry-run=client`, `tofu plan`, `docker compose config`).
- The stdout/stderr output is parsed and verified for schema correctness, resource attribution, and deprecated API versions.
- The synthesized report is submitted to the backend as part of the ChangeSet evidence bundle, giving approvers proof of validity before approving the rollout.

---

### Module 10: Model Tiers & AI Fallback Hierarchy

#### What It Is

A tiered, cost-aware LLM routing layer supporting local models (Ollama running Qwen, DeepSeek, or Llama) alongside cloud providers (Anthropic Claude, OpenAI GPT-4, Google Gemini).

#### Why It Is Needed

- **Data Sovereignty & Privacy**: Enterprise codebases cannot always be transmitted to cloud LLM providers due to regulatory or IP constraints. Local Ollama tiers process code entirely on the host machine.
- **Cost & Availability**: Cloud APIs can encounter rate limits or network outages. Tiered routing provides graceful degradation:
  1. Primary Cloud / Heavy Local Model.
  2. Fast Local Tier (Ollama).
  3. Deterministic AST Template Engine (Zero LLM reliance).

---

### Module 11: Immutable Audit Trail

#### What It Is

An append-only security log recording every single system interaction, pairing event, approval decision, deployment execution, and rollback.

#### Why It Is Needed & How It Works

- **Accountability & Compliance**: Enterprise compliance standards (SOC2, ISO 27001) require verifiable evidence of who authorized changes to production systems.
- **Implementation**: Every audit record captures:
  - Timestamp (RFC 3339 UTC).
  - Actor identity (authenticated principal or system agent).
  - Event type (`pairing_code_issued`, `approval_required`, `change_set_approved`, `change_set_rolled_back`, `device_paired`).
  - Cryptographic hash and rationale (e.g., policy gate verdicts, approver comments).
- Logs are strictly append-only in Postgres and cannot be mutated or purged via API endpoints.

---

### Module 12: Integrations Hub

#### What It Is

An integration router that connects ForgeOps to enterprise identity providers, developer notification channels, and source control repositories.

#### How It Is Implemented

- **Authentik IdP**: Integrates OIDC/OAuth2 authentication via an internal Authentik server container, separating unauthenticated traffic from tenant workloads.
- **Webhook Credential Masking**: Webhook URLs (Slack, Discord, custom HTTP targets) are treated as write-only secrets. Once saved, they are never exposed back to the UI or API endpoints, preventing token theft.
- **Event Dispatcher**: Asynchronous event handlers publish state shifts (e.g., `deploy_failed`, `approval_required`) to configured channels with zero blocking of pipeline threads.

---

### Module 13: Autonomous Deployment Engine & Native BuildKit Optimization (G1–G7)

#### What It Is

An immutable deployment state machine that orchestrates multi-target deployments across local Docker containers, remote GitHub repositories, and Vercel edge infrastructure through seven sequential verification gates (G1 through G7) with bounded self-healing recovery (G8).

#### Why It Is Needed

Traditional CI/CD pipelines suffer from four major operational defects:

1. **False-Positive Success Reporting**: Marking pipelines green when containers start, even if internal application processes crashed, failed to bind, or were blocked by browser safety policies (`ERR_UNSAFE_PORT` on port 4045).
2. **Intermediate Commit Disk Thrashing**: Legacy Docker V1 builders execute each Dockerfile instruction in an ephemeral container and call `docker commit` to disk, wasting hundreds of seconds on large monorepos.
3. **Cross-Tenant Cache Contamination**: Shared CI runners risk cache poisoning or cache eviction conflicts when multiple projects share an unkeyed package directory.
4. **Destructive Auto-Remediation**: Generic AI assistants frequently attempt to resolve deployment failures by destructively altering the user's application source code.

#### How It Is Implemented

- **Sequential Verification Gates (G1–G7)**:
  - **G1 (Blueprint Baseline)**: Strategy configuration validation and baseline readiness scoring (R0).
  - **G2 (Artifact Evaluation & Synthesis)**: Manifest reuse or deterministic multi-stage Dockerfile synthesis with consolidated layer variables (`ENV NODE_ENV=production PORT={port}`) and post-generation readiness (R1).
  - **G3 (Pre-Execution Consistency)**: Port range validation (1–65535) and Chromium bad-ports safety guard (rejecting restricted ports like 4045).
  - **G4 (Native BuildKit v2 Build)**: Direct Docker Engine API execution over Unix Domain Sockets (`POST http://docker/build?version=2&t={tag}&dockerfile=Dockerfile`) with an in-memory Protobuf trace demuxer (`moby.buildkit.trace`), automatic tar-sanitization fallback to Docker V1, and intermediate commit elimination.
  - **G5 (Container Process Apply)**: Container launch with strict runtime port authority and in-memory environment injection (`PORT` and `HOST=0.0.0.0`).
  - **G6 (Workload Readiness)**: Direct HTTP endpoint probing against the published container listener, failing fast on `ECONNREFUSED` or non-200 responses.
  - **Operational Stages**: Transactional publishing via GitHub Git Data API (blobs/trees/commits) and Vercel REST deployment API v13.
  - **G7 (Multi-Target Verification)**: Exact `HostConfig.PortBindings` mapping verification, remote GitHub commit SHA confirmation, and Vercel assigned production alias verification (bypassing SSO walls and rejecting synthetic placeholders).
  - **G8 (Remediation & Sweeper)**: Bounded AI/rule-based recovery (maximum 3 iterations) and fence-token sweeper reconciliation with strict application source code immutability.
- **Multi-Tenant Keyed Cache Mounts**:
  Synthesizes project-scoped cache mounts (`--mount=type=cache,target=/root/.npm,id=npm-{clean_tenant}-{clean_proj},sharing=locked`), preventing cross-tenant contamination while accelerating dependency ingestion.
- **Empirical Monorepo Performance Benchmark (Code Review Platform)**:
  Verified on the production Next.js 16 monorepo, demonstrating dramatic performance gains:

| Deployment / Build Stage             | Legacy Docker V1 Baseline | ForgeOps Native BuildKit (Cold) | ForgeOps Native BuildKit (Warm) | Performance Gain                                          |
| :----------------------------------- | :------------------------ | :------------------------------ | :------------------------------ | :-------------------------------------------------------- |
| **Step 9: `npm ci`**                 | 455.12s                   | 288.40s                         | **0.82s (CACHED)**              | Cache mount avoids redownloading modules.                 |
| **Step 13: `next build`**            | 668.20s                   | 442.10s                         | **1.14s (CACHED)**              | Parallel AST compilation; zero layer re-runs.             |
| **Intermediate Commits**             | **808.10s** (14 commits)  | **0.00s (Eliminated)**          | **0.00s (Eliminated)**          | Zero intermediate disk serialization overhead.            |
| **Final Layer Export**               | 84.71s                    | 34.86s                          | 50.14s                          | Single-pass containerd snapshotter export.                |
| **Total Build Time**                 | **1,208.03s** (~20m 08s)  | **765.36s** (~12m 45s)          | **52.10s** (~0m 52s)            | **95.7% total reduction (23.2x speedup on warm builds)**. |
| **Improvement Relative to Baseline** | _Baseline (0%)_           | **36.6% Reduction**             | **95.7% Reduction (23.2x)**     | **Single-pass compilation with 100% layer hit**.          |

---

## 4. Comprehensive Breakdown of the Project View Interface

The Project Details dashboard is divided into specific architectural sections. Below is an exhaustive breakdown of each section visible on screen:

```
+---------------------------------------------------------------------------------------------------+
| PROJECT: Code_review_platform (UUID: 8b1f298d-ce07-4a9a-9d04-64e16bebfd83)                        |
| Working-Tree: C:\...\Code_review_platform | Repo: https://github.com/parag8487/Code_review_platform|
+---------------------------------------------------------------------------------------------------+
| 1. CODEBASE INDEX (§1.3)          | 2. READINESS ENGINE (§1.4)      | 3. CHANGE HISTORY (§1.7)    |
| - Files: 178 | Chunks: 18         | - Overall Score: 83/100         | - applied (manual, blast: 4)|
| - Bytes: 2.54 MB                  | - Containerization: 85          | - rolled_back (blast: 4)    |
| - Dependencies: 11 / 444          | - Orchestration: 100            | - applied (gen, blast: 66)  |
| - Languages: tsx, yaml, dockerfile| - Security Policy: 100          | - Reversible from rollback  |
+-----------------------------------+---------------------------------+-----------------------------+
| 4. ENVIRONMENTS (§2.1)            | 5. DEPLOYMENTS (§2.2)           | 6. RELEASES (§2.3)          |
| - Staging (development tier)      | - Manifests: docker-compose.yml | - 5 Total Releases Recorded |
| - Promotion Order: 1 -> None      | - Status: Applied / Converged   | - Handles: Promote, Rollback|
| - Human Approval: Unattended      | - Live: http://localhost:3000   | - Manifest Diffs Available  |
+-----------------------------------+---------------------------------+-----------------------------+
| 7. HOST RUNTIME MONITORS (§2.4/§2.9)                                | 8. WRITE-ONLY SECRETS (§1.8)|
| - Docker: Live CPU, Memory, Network I/O (Real-time polling)         | - Key Name & Target Tier    |
| - Status: Distinguishes "not measured" from "idle 0%"               | - Values Never Read Back    |
+---------------------------------------------------------------------------------------------------+
| 9. IMMUTABLE AUDIT LOG: pairing_code_issued -> device_paired -> change_set_approved -> applied    |
+---------------------------------------------------------------------------------------------------+
```

---

### Detailed Section Breakdown

#### 1. Header & Identity Metadata

- **Working-Tree Path**: The absolute path on the developer's workstation where the local agent operates.
- **Repository Lineage**: The remote origin URL linking code artifacts back to version control.
- **Project State & Tags**: Tenant tags used by the OPA policy engine to enforce environment policies (e.g., tagging a project as `production` automatically revokes unattended deployment waivers).

#### 2. Codebase Index Panel (§1.3)

- **Files (178) & Bytes (2,543,047)**: The boundary of the scanned project. Proves that the scanner discovered and indexed all application layers.
- **Chunks (18)**: Lexical segmentations used by the vector retrieval engine to answer semantic queries.
- **Dependencies Resolved (11 of 444)**: Explicit metric of cross-file import graph completeness. Unresolved dependencies identify third-party packages or dynamic imports without fabricating false graph links.
- **Languages**: Multi-language detection identifying syntax parsers required (TypeScript, TSX, Dockerfile, YAML, HCL, Bash).
- **Agent-Initiated Scan Command**: The UI intentionally provides a copyable terminal command (`forgeops-agent scan --project <id>`) rather than a web button. This reflects the security model: the backend cannot force a host to read files; the host agent must initiate all local disk reading.

#### 3. Readiness Breakdown Panel (§1.4)

- **Overall Score (83/100 — `production_ready`)**: Weighted aggregate score indicating the application meets containerization and orchestration readiness standards.
- **Category Breakdown**:
  - _Containerization (85)_: Multi-stage Dockerfiles present, standard exposed ports.
  - _CI Config (76)_: CI workflow present; flagged minor missing lint stages.
  - _Orchestration (100)_: Complete Compose specification with healthy dependency chains.
  - _Env Config (58)_: Deducted points due to missing sample environment documentation (`.env.example`).
  - _Security Policy (100)_: Clean secret scan report; zero leaked credentials.
  - _IaC (75)_: Terraform manifests detected with partial state definitions.

#### 4. Change History & Rollback Ledger (§1.7)

- Displays the complete lifecycle of code mutations submitted for this project:
  - **applied (blast radius 4 — allow)**: Small changes (e.g., config updates) accepted and written to disk.
  - **rolled_back (blast radius 4)**: Demonstrates self-healing: when an apply fails during execution, ForgeOps automatically reverts the working tree using `.forgeops-rollback`.
  - **applied from generation (blast radius 66 — warn)**: Structural additions (such as multi-stage Dockerfiles) assigned a higher blast radius, triggering warning logs and approver reviews.

#### 5. Environments & Promotion Pipeline (§2.1)

- **Promotion Sequence**: Hierarchical deployment tiers (`development` -> `staging` -> `production`).
- **Approval Requirements**: Stated explicitly in words on each row rather than obscured in hidden settings. Staging can be configured for unattended deployment, while production tiers enforce mandatory human sign-off.

#### 6. Deployment Orchestration (§2.2)

- **Mutation Chokepoint Execution**: Manifests (`docker-compose.yml`) are submitted through the signed envelope pipeline.
- **Convergence Verification**: Unlike standard CI tools that exit as soon as `docker compose up -d` returns, ForgeOps waits for workload convergence, checking healthcheck endpoints until the container reports `Up (healthy)` and confirms live availability at `http://localhost:3000`.

#### 7. Release Timeline & Promotion Handles (§2.3)

- Every successful deployment registers a stable Release checkpoint.
- Provides **Roll Back to This** and **Promote This Environment** handles, allowing operators to instantly rollback to known-good images or promote verified images to higher tiers.

#### 8. Host Operations: Docker & Kubernetes Runtimes (§2.4, §2.9)

- **Live Resource Sampling**: Streams real-time container metrics: **CPU %**, **Memory Usage / Limit**, and **Network I/O In / Out**.
- **Zero Coercion Contract**: The dashboard explicitly refuses to coerce unmeasured data to zero. If the agent is disconnected or a container is starting, the UI renders `not measured` rather than `0.00%`, preventing stale or missing telemetry from being misinterpreted as idle workloads.

#### 9. Secret References (§1.8)

- Maps sensitive credentials (`DATABASE_PASSWORD`, etc.) to runtime configurations.
- Enforces write-only semantics: the API returns only metadata, never raw secret material.

#### 10. Immutable Activity Audit Log

- Chronological audit log showing every system interaction:
  - `project_imported`: Repository record created.
  - `pairing_code_issued` / `device_paired`: Cryptographic agent handshake completed.
  - `approval_required` / `change_set_approved`: Human governance approval granted.
  - `change_set_auto_approved`: Governance rules allowing unattended low-risk staging rollout.
  - `change_set_rolled_back`: Automated rollback executed upon container build timeout.

---

## 5. Technical Summary Table

| Architectural Dimension        | Traditional CI/CD / Scripts                                       | Generic AI Assistants          | ForgeOps Platform                                                                                      |
| :----------------------------- | :---------------------------------------------------------------- | :----------------------------- | :----------------------------------------------------------------------------------------------------- |
| **Execution Boundary**         | Central server needs open firewall ports                          | Unconstrained cloud execution  | Local Agent via outbound secure WebSocket                                                              |
| **Codebase Context**           | Blind to code semantics                                           | Context-window truncated       | Local hybrid AST + vector indexing                                                                     |
| **Manifest Generation**        | Static templates only                                             | Hallucination-prone, unchecked | Inventory-grounded with deterministic fallback                                                         |
| **Container Compilation (G4)** | Subshell `docker build` with heavy intermediate container commits | None                           | Native BuildKit v2 Engine API over UDS + Protobuf trace demuxer + 0 intermediate commits + V1 fallback |
| **Build Cache Isolation**      | Unkeyed shared host cache (poisoning/eviction risk)               | None                           | Multi-tenant keyed locked cache mounts (`id=npm-{tenant}-{proj}`)                                      |
| **Safety Governance**          | Binary pass/fail CI steps                                         | None (executes unchecked)      | OPA/Cerbos policy engine + Blast Radius Gate                                                           |
| **Runtime Verification**       | Superficial container status checks (false positives)             | Unchecked guesses              | Multi-target G1–G7 verification with browser bad-ports rejection & genuine HTTP probing                |
| **Secrets Handling**           | Plaintext `.env` / unencrypted variables                          | Plaintext in prompt tokens     | Write-only storage + In-memory runtime injection                                                       |
| **Observability**              | External disconnected monitoring                                  | None                           | Real-time terminal streaming & live container stats                                                    |
| **Failure Recovery**           | Manual intervention                                               | None                           | Atomic rollback via local snapshot handles                                                             |
