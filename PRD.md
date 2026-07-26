# Project Requirements Document (PRD)
# AI-Powered DevOps Automation Platform

**Document Version:** 2.0  
**Date:** 24 July 2026  
**Status:** Updated — incorporates 27 deep research streams (MCP final spec, Tree-sitter cAST, multi-tenant scaling, error recovery, onboarding, Go/FastAPI patterns, OTel deployment, CI/CD, K8s deployment, API/WS protocol)  
**Project Model:** (GitHub), AI-assisted development

---

## 1. Executive Summary

The **AI-Powered DevOps Automation Platform** is a web-based system that acts as an **AI DevOps engineer**. It analyzes a developer's local codebase, scores how production-ready it is, generates and applies missing DevOps files (with user approval), deploys the application across multiple environments, manages Docker and Kubernetes, monitors production, explains failures in plain language, self-heals with guard-rails, learns from project history, and is operated through a web dashboard connected to a lightweight local agent on the user's machine.

**Target Users:** Developers and teams who lack deep DevOps expertise, and experienced teams wanting to automate repetitive infrastructure scaffolding and operations.

**Key Differentiators:**
- First tool uniting codebase analysis → readiness scoring → config generation → deploy → monitor → troubleshoot → learn
- Fully self-hostable with BYO-LLM keys and local model support
- Local agent with outbound-only connectivity for security

---

## 2. System Architecture

### 2.1 Three-Tier Architecture (With MCP Gateway + OTel)

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  TIER 1: Web Frontend (Next.js 16 + React 19)                               │
│  - Dashboard, Command Center, Approval UI, Pipeline Designer                │
│  - shadcn/ui + Radix UI + TanStack Query + Zustand                          │
│  - Apache ECharts, xterm.js, React Flow, D2/Mermaid                         │
│  - React Hook Form + Zod, Tailwind CSS v4, pnpm                             │
└────────────────────────────┬────────────────────────────────────────────────┘
                             │ WSS / REST / SSE (EventSource)
┌────────────────────────────▼────────────────────────────────────────────────┐
│  TIER 2: Cloud Backend (FastAPI + PostgreSQL 17 + pgvector 0.8.5)            │
│                                                                              │
│  ┌──────────────────────────┐  ┌─────────────────────┐  ┌─────────────────┐ │
│  │ API Gateway (REST v1)   │  │ WebSocket Hub       │  │ AI Engine        │ │
│  │ RFC 9457 errors          │  │ (JSON-RPC 2.0       │  │ (LangGraph +     │ │
│  │ URL versioning           │  │  over WSS)          │  │  LlamaIndex)     │ │
│  │ Rate limiting per tenant │  │  Redis Cluster P/S  │  │  6-tier routing  │ │
│  └──────────────────────────┘  └─────────────────────┘  └────────┬────────┘ │
│                                                                  │          │
│  ┌──────────────────────────┐  ┌─────────────────────┐           │          │
│  │ Auth (Authentik +       │  │ Policy Engine       │           ▼          │
│  │  Cerbos RBAC)           │  │ (OPA Sidecar +      │    ┌────────────────┐│
│  │ OAuth 2.1/OIDC + iss    │  │  Wasm Embedded)     │    │ MCP Gateway    ││
│  └──────────────────────────┘  │ + Kyverno (K8s)     │    │ (Stateless,    ││
│                                │ + Approval Engine   │    │  OAuth+OPA     ││
│  ┌──────────────────────────┐  └─────────────────────┘    │  TTL Cache,    ││
│  │ Secrets (Infisical       │                              │  W3C Trace)   ││
│  │  E2EE, BYOK LLM Keys)   │  ┌─────────────────────┐    └───────┬────────┘│
│  └──────────────────────────┘  │ Async Tasks       │            │         │
│                                │ (ARQ/Dramatiq P1  │            ▼         │
│  ┌──────────────────────────┐  │  Inngest P2 →     │    ┌────────────────┐│
│  │ PostgreSQL 17            │  │  Temporal P3+ opt)│    │ MCP Servers:   ││
│  │ + pgvector 0.8.5         │  └─────────────────────┘    │ Docker, K8s,   ││
│  │ HNSW indexes             │                              │ GitHub, Tofu,  ││
│  │ RLS multi-tenant iso.   │  ┌─────────────────────┐    │ Vault, OTel    ││
│  └──────────────────────────┘  │ Redis Cluster       │    └────────────────┘│
│                                │ L1 Exact-match      │                       │
│  ┌──────────────────────────┐  │ L2 Semantic (>.95)  │  ┌─────────────────┐ │
│  │ LLM Eval (DeepEval +    │  │ L3 Prefix Cache     │  │ Safe Templates  │ │
│  │  LangFuse tracing)      │  └─────────────────────┘  │ (8+ languages)  │ │
│  └──────────────────────────┘                          └─────────────────┘ │
│                                                                              │
│  ┌──────────────────────────────────────────────────────────────────────┐   │
│  │ OTel Collector (Two-Tier): Sidecar (per-pod) → Gateway (cluster)     │   │
│  │ → Prometheus/Mimir (metrics) + Loki (logs) + Tempo (traces)          │   │
│  │ → Grafana (dashboards) + Per-tenant cost tracking                    │   │
│  └──────────────────────────────────────────────────────────────────────┘   │
└────────────────────────────┬────────────────────────────────────────────────┘
                             │ WSS (outbound-only, mTLS — JSON-RPC 2.0)
┌────────────────────────────▼────────────────────────────────────────────────┐
│  TIER 3: Local Agent (Go 1.26)                                               │
│                                                                              │
│  ┌──────────────────────────┐  ┌──────────────────────────────────────────┐ │
│  │ Connection Manager      │  │ Command Executor                          │ │
│  │ • Outbound WSS only      │  │ • JSON-RPC over WSS                      │ │
│  │ • coder/websocket (mTLS) │  │ • Operation whitelist                    │ │
│  │ • Auto-reconnect + jitter│  │ • OPA Wasm embedded policy eval          │ │
│  │ • Heartbeat (30s)        │  │ • Approval verification                  │ │
│  │ • Constructor DI pattern │  │ • Backup-before-mutate + atomic sets     │ │
│  └──────────────────────────┘  │ • Graceful shutdown (signal+errgroup)    │ │
│                                └──────────────────────────────────────────┘ │
│  ┌──────────────────────────┐  ┌──────────────────────────────────────────┐ │
│  │ Codebase Scanner         │  │ MCP Server (mark3labs/mcp-go)            │ │
│  │ • Tree-sitter official   │  │ • Stdio transport (CLI mode)            │ │
│  │   Go bindings            │  │ • HTTP/SSE transport (sidecar mode)     │ │
│  │ • cAST semantic chunking │  │ • Docker tool handlers                  │ │
│  │ • Dependency-graph aware │  │ • K8s tool handlers                     │ │
│  │ • Cold start discovery   │  │ • Git tool handlers                     │ │
│  │ • Fan-out/fan-in watch   │  │ • JWT auth middleware                   │ │
│  └──────────────────────────┘  └──────────────────────────────────────────┘ │
│                                                                              │
│  ┌──────────────────────────┐  ┌──────────────────────────────────────────┐ │
│  │ Validation Engine        │  │ IaC Runner (OpenTofu/Helm/Docker)        │ │
│  │ • kubectl --dry-run      │  │ • subprocess with Context timeout       │ │
│  │ • tofu validate/plan     │  │ • Stdout/Stderr streaming to zap         │ │
│  │ • docker compose config  │  │ • Signal propagation via Setpgid        │ │
│  │ • helm lint + template   │  │ • Environment isolation                  │ │
│  │ • YAML/JSON Schema       │  └──────────────────────────────────────────┘ │
│  └──────────────────────────┘                                               │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 2.1a MCP Gateway Layer (New — Core Architecture)
| Component | Technology | Role |
|:----------|:-----------|:-----|
| **Auth** | OAuth 2.1/OIDC with `iss` validation (RFC 9207) | Validates identity at gateway before any tool call |
| **Policy** | OPA sidecar | Filters tools by agent blast radius before forwarding to MCP servers |
| **Routing** | `Mcp-Method` + `Mcp-Name` headers | Routes to correct target MCP server without inspecting JSON-RPC body |
| **Caching** | `ttlMs` from MCP server tool lists | Client + gateway level TTL caching to reduce latency |
| **Tasks** | `tasks/get`, `tasks/update`, `tasks/cancel` | Standard lifecycle for long-running DevOps operations |
| **Tracing** | W3C Trace Context + OpenTelemetry | Spans correlated across host → client → MCP server → infrastructure |
| **MCP Apps** | Sandboxed iframe endpoints | Interactive UIs (approval forms, config editors) rendered in agent UI |

### 2.2 Security Invariants (Non-Negotiable)

1. **Agent connects outbound-only** — backend never connects into user's machine
2. **Named, whitelisted operations only** — never arbitrary shell commands
3. **Every mutation requires an approval_id** (or explicit policy auto-approval)
4. **Double policy evaluation** — both server-side and agent-side (zero-trust)
5. **Secrets encrypted at rest**, redacted before LLM context, injected only at deploy time
6. **Backup-before-mutate** — every file write preceded by timestamped backup
7. **Atomic change-sets** — all-or-nothing file operations
8. **Path blocklists** — agent refuses to read/write `~/.ssh`, `~/.aws`, `.env`, `*.pem`

---

## 3. Functional Requirements

### 3.1 Multi-Project Workspace (Phase 1)

| ID | Requirement | Priority |
|:---|:---|---:|
| FR-01 | Import projects from GitHub or local folders | P0 |
| FR-02 | Search and organize projects with tags (Personal, Production, Learning, custom) | P0 |
| FR-03 | Mark projects as favorites | P0 |
| FR-04 | Display recent activity feed per project | P0 |
| FR-05 | Archive and delete projects | P0 |
| FR-06 | Per-project settings (LLM budget, policies, environments, notification rules) | P1 |
| FR-07 | Multi-user project sharing with role-based access | P2 |

### 3.2 Codebase Analysis Engine (Phase 1)

| ID | Requirement | Priority |
|:---|:---|---:|
| FR-08 | Scan entire local project directory honoring `.gitignore` | P0 |
| FR-09 | Exclude secrets, binaries, and node_modules from analysis | P0 |
| FR-10 | Detect programming languages, frameworks, package managers | P0 |
| FR-11 | Detect entry points, build scripts, existing config files | P0 |
| FR-12 | Build a Codebase Index: file-tree metadata + key file contents + vector embeddings | P0 |
| FR-13 | Use RAG (Retrieval-Augmented Generation) for AI prompting from real project context | P0 |
| FR-14 | Re-scan on git push or manual trigger | P1 |
| FR-15 | Incremental scanning (only changed files) | P2 |

### 3.3 Deployment Readiness Analysis (Phase 1)

| ID | Requirement | Priority |
|:---|:---|---:|
| FR-16 | Produce a 0-100 readiness score with weighted categories | P0 |
| FR-17 | Categories: Containerization, CI/CD, Orchestration, Env Config, Security, IaC | P0 |
| FR-18 | Extended categories: Scalability, Reliability, Performance, Maintainability, Observability, Testing, Documentation, Compliance | P1 |
| FR-19 | Plain-language report explaining every gap with "why it matters" | P0 |
| FR-20 | Example checks: Dockerfile exists, multi-stage, non-root user; pipeline stages; K8s resource limits; `.env.example` exists; no secrets in code | P0 |
| FR-21 | Detailed drill-down for each category with actionable recommendations | P1 |

### 3.4 AI File Generation & Codebase Modification (Phase 1 - Core)

| ID | Requirement | Priority |
|:---|:---|---:|
| FR-22 | AI generates artifacts using RAG context from Codebase Index | P0 |
| FR-23 | Artifacts: Dockerfiles, docker-compose, K8s manifests (Deployments, Services, Ingress, ConfigMaps, HPA) | P0 |
| FR-24 | Artifacts: GitHub Actions workflows, Helm charts, Terraform/OpenTofu configs | P0 |
| FR-25 | Artifacts: `.env.example`, README/deployment docs, health/metrics endpoint code | P1 |
| FR-26 | Artifacts: Jenkins pipelines, Ansible playbooks, Prometheus/Grafana/Loki configs | P2 |
| FR-27 | Local agent validates artifacts before user sees them (docker build, YAML schema, kubectl dry-run, tofu validate) | P0 |
| FR-28 | Failed validations loop back to AI for regeneration | P0 |
| FR-29 | Validated changes presented as diff previews in Change Approval Center | P0 |
| FR-30 | Explicit approval required before agent applies changes | P0 |
| FR-31 | Atomic application with automatic timestamped backups | P0 |

### 3.5 Policy Engine (Phase 1)

| ID | Requirement | Priority |
|:---|:---|---:|
| FR-32 | Users define rules constraining AI and automation | P0 |
| FR-33 | Example policies: "Never edit package.json", "Never delete files", "Never deploy on Fridays" | P0 |
| FR-34 | "Require approval for production", "Auto-approve README changes" | P0 |
| FR-35 | "Maximum Docker memory 4GB" | P1 |
| FR-36 | Policies apply per project, per environment, per action class | P1 |
| FR-37 | Policy violations blocked and surfaced with explanations | P0 |
| FR-38 | Double evaluation: agent-side AND backend-side | P0 |
| FR-39 | AI-powered natural language policy authoring (NL → Rego) | P2 |

### 3.6 Secret Management (Phase 1)

| ID | Requirement | Priority |
|:---|:---|---:|
| FR-40 | Dedicated encrypted secret storage per project/environment | P0 |
| FR-41 | Support for environment variables, API keys, certificates | P0 |
| FR-42 | Secret scanning of codebase for hardcoded secrets | P0 |
| FR-43 | Git-leak detection on current and historical commits | P1 |
| FR-44 | Rotation reminders | P2 |
| FR-45 | Secret injection at deployment time (never written into files or sent to LLM) | P0 |
| FR-46 | Integration with External Secrets Operator for K8s | P1 |

### 3.7 Multi-Environment Management (Phase 2)

| ID | Requirement | Priority |
|:---|:---|---:|
| FR-47 | First-class environments: Development, Testing, Staging, Production + custom | P1 |
| FR-48 | Each environment has own variables, secrets, K8s context, Docker registry | P1 |
| FR-49 | Each environment has own approval requirements and policies | P1 |
| FR-50 | Promotion flows between environments (staging → production requires approval) | P2 |

### 3.8 Deployment Automation (Phase 2)

| ID | Requirement | Priority |
|:---|:---|---:|
| FR-51 | Build images, push to registries, apply manifests | P1 |
| FR-52 | Stream live logs during deployment | P1 |
| FR-53 | Verify health checks after deployment | P1 |
| FR-54 | Record stable-state snapshot per successful deploy | P1 |
| FR-55 | Rollback to any previous deployment | P1 |
| FR-56 | Integration with GitHub Actions and CI/CD pipelines | P2 |

### 3.9 Docker Management Dashboard (Phase 2)

| ID | Requirement | Priority |
|:---|:---|---:|
| FR-57 | View containers (status, logs, stats) | P1 |
| FR-58 | Create, start, stop, restart, delete containers | P1 |
| FR-59 | Build, pull, push, remove images | P1 |
| FR-60 | Manage volumes and networks | P2 |
| FR-61 | Live resource stats (CPU, memory, network) | P1 |

### 3.10 Kubernetes Management Dashboard (Phase 3)

| ID | Requirement | Priority |
|:---|:---|---:|
| FR-62 | View pods, deployments, services, namespaces | P2 |
| FR-63 | View Ingress, ConfigMaps, Secrets, HPA | P2 |
| FR-64 | Scale deployments, view logs, view cluster info | P2 |
| FR-65 | No kubectl needed — all via API | P2 |

### 3.11 Monitoring & Observability (Phase 3)

| ID | Requirement | Priority |
|:---|:---|---:|
| FR-66 | Deploy OpenTelemetry Collector for unified metrics/logs/traces ingestion | P2 |
| FR-67 | Prometheus for metrics storage and PromQL queries | P2 |
| FR-68 | Loki for log aggregation | P2 |
| FR-69 | Grafana dashboards for visualization | P2 |
| FR-70 | Unified dashboard: infrastructure health, container performance, app metrics | P2 |
| FR-71 | AI-generated health/metrics endpoint code emits OTLP | P2 |

### 3.12 AI Troubleshooting / Root-Cause Analysis (Phase 3)

| ID | Requirement | Priority |
|:---|:---|---:|
| FR-72 | Continuously analyze build failures, deployment errors, K8s/Docker issues | P2 |
| FR-73 | Output: plain-language root cause → exact problem location → recommended fix | P2 |
| FR-74 | Optionally generate corrected config/file that enters approval pipeline | P2 |

### 3.13 Self-Healing (Phase 3)

| ID | Requirement | Priority |
|:---|:---|---:|
| FR-75 | Detect unhealthy deployments, failed containers, crash-looping pods | P2 |
| FR-76 | Two-tier action: safe actions auto-execute, risky actions require approval | P2 |
| FR-77 | Every incident ends with AI post-incident summary and recommendations | P2 |

### 3.14 AI Command Center (Phase 2)

| ID | Requirement | Priority |
|:---|:---|---:|
| FR-78 | Natural-language commands: "Deploy my app", "Rollback production", "Show pods" | P1 |
| FR-79 | Commands resolve to whitelisted, policy-checked, approval-gated operations | P1 |
| FR-80 | Intent routing: Deploy, Diagnostic, Generate, Policy, General Chat | P1 |
| FR-81 | Defense-in-depth guard-rails: deterministic, model-based, policy, approval, sandbox | P1 |

### 3.15 AI Learning History (Phase 3)

| ID | Requirement | Priority |
|:---|:---|---:|
| FR-82 | Store previous fixes, incidents, generated files, accepted/rejected suggestions | P2 |
| FR-83 | AI stops re-suggesting rejected patterns | P2 |
| FR-84 | AI repeats what user accepted | P2 |
| FR-85 | Two-tier memory: short-term (session) + long-term (preference graph) | P2 |
| FR-86 | Memory is per-project, inspectable, and editable by the user | P2 |

### 3.16 Technology Recommendation Engine (Phase 3)

| ID | Requirement | Priority |
|:---|:---|---:|
| FR-87 | After analysis, AI proposes stack improvements with reasoning | P2 |
| FR-88 | Examples: "Use PostgreSQL instead of SQLite", "Add Redis for caching" | P2 |
| FR-89 | Each recommendation links to one-click generation flow (through approval pipeline) | P2 |

### 3.16a Knowledge Base Mode (Phase 3)

| ID | Requirement | Priority |
|:---|:---|---:|
| FR-89a | Question-answering pipeline with RAG from codebase, deployments, and incidents | P2 |
| FR-89b | Answer topics: "Explain this Dockerfile", "Explain this error", "Best practices for..." | P2 |
| FR-89c | Always uses the current project as the example (never generic) | P2 |

### 3.17 Features Deferred to Phase 4+

| ID | Feature | Phase |
|:---|:---|---:|
| FR-90 | Visual Pipeline Designer (drag-and-drop) | 4 |
| FR-91 | AI Architecture Diagram Generator | 4 |
| FR-92 | Dependency Health (full scanner) | 4 |
| FR-93 | Cost Analysis | 4 |
| FR-94 | Backup & Disaster Recovery | 4 |
| FR-95 | API Explorer | 4 |
| FR-96 | Team Collaboration (full RBAC with review requests) | 4 |
| FR-97 | Rollback Visualization & Release Timeline | 4 |
| FR-98 | Deployment Analytics (DORA metrics) | 4 |
| FR-99 | Notification Center (multi-channel) | 4 |
| FR-100 | Local Development Tools (run tests, lint, build) | 4 |

### 3.18 Advanced & Ecosystem (Phase 5)

| ID | Feature | Phase |
|:---|:---|---:|
| FR-101 | Multi-Agent Collaboration (Analyzer, Generator, Safety Reviewer, Deployment Manager + human orchestrator) | 5 |
| FR-102 | Air-Gapped Mode (fully offline, local models only, no cloud backend dependency) | 5 |
| FR-103 | Backstage Plugin (catalog + software templates integration) | 5 |
| FR-104 | Enterprise SSO & Compliance (SAML, LDAP, SOC2, audit export, data retention) | 5 |
| FR-105 | "Deploy to..." one-click templates for popular stacks (Next.js→Vercel, Django→Railway, Spring Boot→ECS) | 5 |
| FR-106 | Platform SDK (REST API, webhook system, plugin architecture for community extensions) | 5 |

---

## 4. Non-Functional Requirements

### 4.1 Performance

| ID | Requirement | Target |
|:---|:---|---:|
| NFR-01 | Codebase analysis for small project (<10K files) | <30 seconds |
| NFR-02 | Codebase analysis for medium project (<100K files) | <5 minutes |
| NFR-03 | AI artifact generation | <15 seconds first attempt |
| NFR-04 | Validation loop (generate → validate → regenerate) | <3 iterations average |
| NFR-05 | WebSocket connection latency (agent ↔ backend) | <100ms |
| NFR-06 | Dashboard page load (initial) | <2 seconds |
| NFR-07 | Dashboard page load (subsequent, cached) | <500ms |

### 4.2 Security

| ID | Requirement | Target |
|:---|:---|---:|
| NFR-08 | All agent-backend communication over WSS (TLS 1.3) | Mandatory |
| NFR-09 | All secrets encrypted at rest (AES-256-GCM) | Mandatory |
| NFR-10 | No secrets in LLM context (redacted before API call) | Mandatory |
| NFR-11 | Agent rejects paths outside registered project roots | Mandatory |
| NFR-12 | Agent has zero inbound ports | Mandatory |
| NFR-13 | Binary signing (Cosign) for all releases | Mandatory |
| NFR-14 | Audit log: every action logged with who, what, when, why | Mandatory |
| NFR-15 | Rate limiting: per-user, per-project, per-operation token budgets | Mandatory |
| NFR-16 | Approval ID verification: agent independently verifies all approvals | Mandatory |

### 4.3 Reliability

| ID | Requirement | Target |
|:---|:---|---:|
| NFR-17 | Agent auto-reconnects on connection loss with exponential backoff | Mandatory |
| NFR-18 | Agent queues operations when offline, executes when reconnected | P1 |
| NFR-19 | Backend has no single point of failure (stateless, horizontal scaling) | P2 |
| NFR-20 | Backup before every file mutation | Mandatory |
| NFR-21 | Atomic change-sets (all-or-nothing) | Mandatory |

### 4.4 Cross-Platform Compatibility

| ID | Platform | Requirement |
|:---|:---|---:|
| NFR-22 | Windows 10/11 | Agent fully supported |
| NFR-23 | macOS 13+ (Intel + Apple Silicon) | Agent fully supported |
| NFR-24 | Linux (Ubuntu 22.04+, Fedora 38+, Debian 12+) | Agent fully supported |
| NFR-25 | Docker Desktop (Windows/macOS/Linux) | Agent integration |
| NFR-26 | Minikube, k3s, kind, Docker Desktop K8s | Agent integration |

### 4.5 Scalability

| ID | Requirement | Target |
|:---|:---|---:|
| NFR-27 | Concurrent projects per agent | Unlimited |
| NFR-28 | Concurrent agents per backend | 10,000+ |
| NFR-29 | Vector store scaling | pgvector up to ~50M vectors |
| NFR-30 | Job queue throughput | 1,000+ jobs/second |

### 4.6 Licensing

| ID | Component | License |
|:---|:---|---:|
| NFR-31 | Local Agent + CLI | Apache 2.0 |
| NFR-32 | Backend Platform | FSL (Fair Source) or BSL 1.1 |
| NFR-33 | Open-Core Premium Features | Proprietary |

---

## 5. Technology Stack (Final Decisions)

| Layer | Technology | Version | Justification |
|:---|:---|---:|:---|
| Local Agent Language | **Go** | **1.26** | Mature Docker/K8s SDKs, fast cross-compilation, single binary; Green Tea GC |
| Agent DI Pattern | **Constructor injection** (no framework) | — | Lightweight, minimal startup, easy testing via mocks |
| Agent MCP SDK | **mark3labs/mcp-go** | v0.15+ | Production-grade MCP server for stdio + HTTP/SSE transport |
| Tree-sitter Go | **github.com/tree-sitter/go-tree-sitter** (official) | v0.10+ | Official Go bindings for AST parsing; cAST semantic chunking |
| Backend Framework | **Python (FastAPI)** | 0.139.2+ | Async-native, AI ecosystem supremacy, Pydantic v2 strict mode, native SSE |
| Database | **PostgreSQL + pgvector** | 17+ / 0.8.5 | ACID + vector store in single DB; HNSW indexes default (pin 0.8.5) |
| Multi-Tenant Isolation | **PostgreSQL RLS** | — | Row-Level Security per tenant; single-DB efficiency |
| ORM | **SQLModel** (on SQLAlchemy 2.0) | 0.0.39+ | Pydantic + SQLAlchemy bridge; `expire_on_commit=False` |
| Async Task Runner (P1) | **ARQ or Dramatiq** | Latest | asyncio-native fire-and-forget AI tasks (no Celery eventlet/gevent friction) |
| Job Queue (Phase 2) | **Inngest** (single durable engine) | Latest | Event-driven durable functions for deployment pipelines; introduced once |
| Workflow (Phase 3+, optional) | **Temporal** | Latest | Stateful workflow-as-code; adopt only if replay/history outgrows Inngest |
| Auth | **Authentik** | 2024+ | Self-hostable, MIT license, OIDC/OAuth/SAML |
| Auth (MCP Gateway) | **OAuth 2.1/OIDC with `iss` validation** | RFC 9207 | Prevents mix-up attacks in multi-tenant MCP deployments |
| Fine-Grained RBAC | **Cerbos** | v0.54.0 | Policy-as-code sidecar (app RBAC); OPA-Wasm embedded in agent for agent-side eval |
| Frontend | **Next.js 16 + React 19** | 16 / 19 | Industry standard, PPR, React Compiler, large ecosystem |
| UI Library | **shadcn/ui + Radix UI** | Latest | Accessible, fully owned code (MIT license) |
| State (Server) | **TanStack Query** | 6.x | Non-negotiable for WebSocket/SSE state |
| State (Client) | **Zustand** | 5.x | Lightweight, no Provider hell |
| Forms | **React Hook Form + Zod** | Latest | Industry standard; performs well with uncontrolled inputs |
| Charts | **Apache ECharts** | 5.x | Canvas-based, dense data performance |
| Log Viewer | **xterm.js** | 5.x | Full terminal emulator |
| Flow Editor | **React Flow (xyflow)** | 12.x | Gold standard for node-based UIs |
| Code/Diff Editor | **CodeMirror 6** (Monaco only if IDE-grade IntelliSense needed) | Latest | ~50 KB review surface for config editing, diffs, and generated artifact review |
| Diagrams | **D2** (primary) + **Mermaid** (fallback) | 0.7+ / 11.x | Declarative auto-layout (dagre/ELK/TALA) |
| Testing (Python) | **pytest + pytest-asyncio + httpx** | 8.x | Industry-standard async testing |
| Testing (Frontend) | **vitest** | 2.x | Vite-native, blazing-fast test runner |
| E2E Testing | **Playwright** | 1.50+ | Gold standard for cross-browser E2E |
| Load Testing | **k6** | Latest | Developer-friendly, JS-scriptable |
| Python Linting | **Ruff** | 0.6+ | Unified linter + formatter |
| Go Linting | **golangci-lint** | 1.62+ | Meta-linter aggregating 50+ linters |
| Package Manager | **pnpm** | 10+ | Content-addressable store; strict module resolution |
| Pre-commit | **pre-commit framework** | Latest | Gitleaks + Ruff + gofmt + trailing-whitespace hooks |
| Release Pipeline | **GoReleaser** | Latest | Cross-platform Go builds (linux/windows/darwin × amd64/arm64) |
| Supply-Chain Security | **Cosign** (keyless) + **Syft** (SBOM) + **SLSA** Level 3 | 2026 | Signed releases, CycloneDX SBOMs, non-falsifiable provenance |
| LLM (Code Gen — High) | **GPT-5.6 Sol** | July 2026 | Primary flagship (self-reported SWE-bench ~97%; rank on Pro + internal golden set) |
| LLM (Analysis — High) | **Claude Fable 5** | June 2026 | Backup flagship; complex analytical breadth (self-reported SWE-bench ~95%) |
| LLM (Medium) | **Grok 4.5** (~90% est.) | July 2026 | High-performance agentic coding at 1/3 frontier cost |
| LLM (Medium/Value) | **Claude Sonnet 5** (89%) / **DeepSeek V4** (88%) | 2026 | Balanced quality/cost for Dockerfile, CI/CD, analysis |
| LLM (Low/Logs) | **Gemini 3 Flash** (82%) | 2026 | Highest throughput, lowest latency for log analysis |
| LLM (Self-Hosted) | **GLM-5.2** / **DeepSeek V4-Pro** / **Qwen3-Coder-Next** | 2026 | Open-weight; frontier-adjacent performance; Apache 2.0 / MIT |
| Local Inference | **vLLM** (prod) + **Ollama** (dev) | 2026 | Full local capability for air-gapped mode |
| Agent Governance | **Governance Control Plane** (policy + approval + audit + change-set + rollback) | P1 | One enforced chokepoint in front of every mutating action; the trust moat |
| Agent Identity | **SPIFFE/SPIRE X.509-SVID + mTLS** with attestation | 2026 | No long-lived agent keys; attestation on namespace + service-account + image-digest; JWT-SVID only for L7 proxy crossing |
| RAG Framework | **LangGraph** (agents) + **LlamaIndex** (indexing) | 2026 | State of the art; LangGraph for agent loops, LlamaIndex for ingestion |
| LLM Evaluation | **DeepEval** (CI) + **LangFuse** (production tracing) | 2026 | Unit testing + production monitoring for AI outputs |
| Code Embeddings | **Voyage Code 3** (API) / **BGE-M3** (self-hosted) | 2026 | Best code retrieval quality |
| Tool Integration | **MCP Gateway** (Model Context Protocol) | 2026 (July 28 spec) | Stateless, OAuth 2.1, OPA enforcement, W3C trace, Tasks, MCP Apps |
| Policy Engine | **OPA/Rego** (platform-wide) + **Kyverno** (K8s) | 2026 | Defense in depth; OPA sidecar + Wasm embedded for Go agent |
| Secret Vault | **Infisical** | 2026 | Modern, embeddable, E2EE; BYO-LLM key management |
| Git-Leak Detection | **Gitleaks** (pre-commit) + **TruffleHog** (server) | 2026 | Two-gate approach |
| IaC Engine | **OpenTofu** | 1.12.5 | Community-governed Terraform fork (MPL 2.0); native state encryption |
| Helm | **Helm SDK (Go)** / OCI | 3.x | Programmatic chart management |
| Observability | **OTel Collector (two-tier)** → **Prometheus/Mimir** + **Loki** + **Tempo** | 2026 | Sidecar + Gateway collectors; hybrid sampling; gen_AI semantic conventions |
| Service Mesh | **Cilium** (eBPF, sidecarless, Hubble) → **Istio Ambient** (if rich L7 needed) | 2026 | Lowest-overhead mesh for 10k-agent self-host; Linkerd stable is behind a Buoyant subscription |
| Autoscaling | **KEDA** (event-driven) | 2026 | Scale ARQ/Dramatiq + durable-engine workers by queue depth |
| GitOps | **ArgoCD** (primary) / **Flux CD** (alternative) | 3.4+ / 2.8+ | Declarative continuous delivery |
| Progressive Delivery | **Argo Rollouts** (pairs with ArgoCD) | 1.9.1 | Canary/blue-green — not native to ArgoCD; gate on error-rate AND latency |
| K8s PostgreSQL | **CloudNativePG** | 2026 | K8s-native PostgreSQL operator with pgBackRest backup |
| K8s WebSocket | **NGINX Ingress** with WSS annotations | 2026 | proxy-read-timeout: 3600, proxy-buffering: off, sticky sessions |
| Backup | **Velero** (K8s) | 1.14+ | Industry standard for cluster state backup |
| Notifications | **Novu** | 2026 | Multi-channel, single API |
| WebSocket Protocol | **JSON-RPC 2.0** over WSS | — | Structured method invocation for agent ↔ backend communication |
| REST API Standard | **RFC 9457** (Problem Details) | IETF | Standardized error responses across all API endpoints |
| WebSocket Spec | **AsyncAPI** 3.0 | — | Documented event-driven architecture for agent protocol |
| API Client | **openapi-typescript** (frontend) / **httpx** (backend) | 2026 | Type-safe API client generation |
| Safe Templates | **Template Library** (8 languages × 5 artifacts) | Phase 1 | Hardcoded, verified fallback when AI fails after max retries |

---

## 6. Integration Requirements

| System | Integration Type | Details |
|:---|:---|---:|
| GitHub | GitHub App | Short-lived tokens, webhooks, CI/CD triggering |
| Docker | Docker Engine API (via agent) | Containers, images, volumes, networks |
| Kubernetes | client-go (via agent) / API proxy | Full resource management |
| OpenTofu/Terraform | Subprocess + JSON plan | `tofu validate`, `tofu plan`, `tofu apply` |
| Ansible | Subprocess | Playbook execution |
| Helm | Helm SDK (Go) / OCI | Chart generation, lint, install |
| Prometheus | HTTP API | Query metrics |
| Grafana | HTTP API + embedded dashboards | Visualization |
| Slack / Discord / Telegram | Novu / Webhooks | Notifications |
| Email | SMTP via Novu | Notifications |

---

## 7. Data Model (Overview)

### D1: Users, Teams & Projects
- `users` (id, email, name, role, created_at)
- `teams` (id, name, owner_id)
- `team_members` (team_id, user_id, role)
- `projects` (id, name, path, repo_url, owner_id, team_id, settings JSON)
- `project_tags` (project_id, tag)
- `sessions` (id, user_id, token, expires_at)
- `agent_devices` (id, project_id, pairing_token, device_token, last_seen)

### D2: Codebase Index
- `file_tree` (project_id, path, hash, size, last_modified)
- `file_contents` (file_id, content, language, summary)
- `embeddings` (file_id, chunk_index, chunk_text, embedding vector)
- `analysis_reports` (project_id, score, categories JSON, created_at)

### D3: Change-sets & Approvals
- `change_sets` (id, project_id, status, created_by, applied_at)
- `change_items` (change_set_id, file_path, old_content, new_content, action)
- `validations` (change_item_id, validator, passed, output)
- `approvals` (change_set_id, approver_id, status, comment)

### D4: Deployments & Environments
- `environments` (id, project_id, name, type, config JSON)
- `deployments` (id, environment_id, status, image_tag, manifest_hash, stable_snapshot JSON)
- `deployment_logs` (deployment_id, timestamp, level, message)
- `health_checks` (deployment_id, endpoint, status, latency)

### D5: Secret Vault (encrypted)
- `secrets` (id, project_id, environment_id, key, encrypted_value, rotation_date)

### D6: AI Learning History
- `feedback_events` (id, project_id, event_type, artifact_type, content_snapshot JSON)
- `skill_files` (project_id, synthesized_preferences TEXT)

### D7: Policies
- `policies` (id, project_id, name, engine, rego_rules, enabled)
- `policy_evaluations` (policy_id, operation, result, reason)

### D8: Incidents & Telemetry
- `incidents` (id, project_id, type, severity, status, resolution)
- `auto_actions` (incident_id, action, result, approval_required)
- `metrics` (project_id, timestamp, name, value, labels JSON)

---

## 8. Project Structure

```
ai-devops-platform/
├── agent/                          # Local Agent (Go)
│   ├── cmd/
│   │   └── agent/
│   │       └── main.go             # Entry point
│   ├── internal/
│   │   ├── connection/             # WSS connection manager
│   │   ├── docker/                 # Docker Engine API wrapper
│   │   ├── k8s/                    # Kubernetes client wrapper
│   │   ├── scanner/               # Codebase scanner/indexer
│   │   ├── executor/              # Command executor + whitelist
│   │   ├── validator/             # Validation engine
│   │   ├── policy/                # Local policy evaluator
│   │   ├── fileops/               # File operations + backups
│   │   ├── iac/                   # IaC runner (OpenTofu, Ansible)
│   │   ├── devtools/             # Local dev tools runner
│   │   └── telemetry/            # Telemetry collector
│   ├── pkg/                       # Shared utilities
│   ├── go.mod / go.sum
│   └── .goreleaser.yaml
│
├── backend/                        # Cloud Backend (Python/FastAPI)
│   ├── src/
│   │   ├── core/                  # Config, security, deps
│   │   ├── auth/                  # Authentication domain
│   │   ├── projects/             # Project workspace domain
│   │   ├── analysis/             # Codebase analysis domain
│   │   ├── generation/           # AI artifact generation domain
│   │   ├── deployment/           # Deployment automation domain
│   │   ├── monitoring/           # Observability domain
│   │   ├── incidents/            # Incident management domain
│   │   ├── policies/             # Policy engine domain
│   │   ├── secrets/              # Secret management domain
│   │   ├── notifications/        # Notification domain
│   │   ├── ai/                   # AI engine (LLM calls, RAG, agents)
│   │   ├── websocket/            # WebSocket hub
│   │   └── main.py               # App entry point
│   ├── alembic/                   # DB migrations
│   ├── tests/
│   ├── requirements.txt
│   ├── Dockerfile
│   └── docker-compose.yml
│
├── frontend/                       # Web Frontend (Next.js 16)
│   ├── app/                       # App Router pages
│   ├── components/
│   │   └── ui/                    # shadcn/ui components
│   ├── features/
│   │   ├── projects/             # Project workspace
│   │   ├── analysis/             # Readiness reports
│   │   ├── generation/           # AI generation + approval
│   │   ├── deployment/           # Deployment dashboard
│   │   ├── docker/               # Docker management
│   │   ├── kubernetes/           # K8s management
│   │   ├── monitoring/           # Observability dashboards
│   │   ├── incidents/            # Incidents view
│   │   ├── policies/             # Policy editor
│   │   ├── secrets/              # Secret vault UI
│   │   ├── pipeline-designer/    # React Flow pipeline editor
│   │   └── command-center/       # AI command center
│   ├── lib/                       # Utilities, API client
│   ├── hooks/                     # Custom React hooks
│   ├── package.json
│   └── next.config.js
│
├── docs/                          # Documentation
│   ├── architecture.md
│   ├── api.md
│   ├── development.md
│   └── deployment.md
│
├── .github/
│   └── workflows/
│       ├── ci.yml                # CI pipeline
│       └── release.yml           # Release pipeline
│
├── docker-compose.yml            # Full stack orchestration
├── Makefile                      # Common commands
└── README.md                     # Project overview
```

---

## 9. Build & Release Pipeline

### CI Pipeline (GitHub Actions)
```
0. Pre-commit: gitleaks (secrets), ruff (Python lint/format), gofmt (Go fmt), prettier (JS)
1. Detect: dorny/paths-filter — only run relevant jobs per component
2. Lint: golangci-lint (Go), ruff (Python), ESLint (JS)
3. Test: go test -race -shuffle=on (agent), pytest-asyncio (backend), vitest (frontend), Playwright (E2E)
4. LLM Eval: DeepEval — run golden dataset against model outputs, compare against baseline
5. Build: go build (agent, all platforms), docker build (backend), npm build (frontend)
6. SCA: Trivy vulnerability scan + CycloneDX SBOM generation (Syft)
7. Size: k6 smoke test (basic load check on API)
8. Sign: Cosign keyless signing via Sigstore (Fulcio/Rekor)
9. Attest: Attach SBOM + SLSA provenance to release artifacts (SLSA Level 3)
10. Verify: go mod verify (Go), pip-audit (Python), pnpm audit (JS)
```

### Release Pipeline (goreleaser)
```
1. Build cross-platform binaries (Windows, macOS, Linux, amd64, arm64)
2. Package installers (MSI, .pkg, .deb, .rpm)
3. Generate CycloneDX SBOM via Syft
4. Sign binaries (Cosign keyless + Apple notarization)
5. Attach SLSA provenance attestation
6. Create GitHub Release with changelog
7. Push Docker images to GHCR
8. Push SBOM + attestations to Rekor transparency log
```

---

## 10. Phase Summary

| Phase | Focus | Key Deliverables | Timeline (est.) |
|:---|:---|---:|---:|
| **Phase 0** | Foundation | Go agent scaffold, FastAPI scaffold, Next.js scaffold, Docker Compose dev env, MCP integration, GitOps workflow, Plan Analyzer, OpenTofu switch | 2-3 weeks |
| **Phase 1** | MVP Core | Agent pairing, project workspace, codebase scan + readiness report, AI file generation (Dockerfile, K8s, CI/CD), validation loop, approval center, policy engine, secret vault, audit logging | 8-12 weeks |
| **Phase 2** | Deploy & Manage | Multi-environment, deployment automation, Docker dashboard, AI Command Center, rollback timeline, notification center | 8-12 weeks |
| **Phase 3** | Observe & Heal | OTel monitoring, K8s dashboard, AI troubleshooting, self-healing, AI learning history, knowledge base | 10-14 weeks |
| **Phase 4** | Scale & Polish | Visual pipeline designer, architecture diagrams, dependency health, cost analysis, team collaboration, backup/DR, API explorer, analytics | 12-16 weeks |
| **Phase 5** | Advanced & Ecosystem | Multi-agent collaboration, air-gapped mode, Backstage plugin, enterprise SSO & compliance (SAML/LDAP/SOC2), "Deploy to..." one-click templates, platform SDK (REST API/webhooks/plugins) | 16+ weeks |

---

## 11. Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|:---|:---:|:---:|:---|
| LLM generates invalid/insecure configs | High | High | Validation-feedback loop, Plan Analyzer, human approval |
| Agent security vulnerability | Low | Critical | Outbound-only, whitelist, double policy eval, backups |
| PostgreSQL+pgvector hits scaling limit | Low | Medium | Monitoring + migration plan to dedicated vector DB |
| LLM API costs exceed budget | Medium | Medium | BYO-Key, model routing (cheap for simple tasks), local models |
| Competition from Harness/StackGen | Medium | High | differentiation, self-hosting, BYO-LLM, unique readiness scoring |
| Low community adoption | Medium | High | Apache 2.0 license, clear docs, quick-start demo, active community engagement |
| Browser/WebSocket performance issues | Low | Medium | Efficient WebSocket state management, zustand + TanStack Query |
| Cross-platform agent compatibility | Medium | Medium | CI testing on all platforms, extensive QA matrix |

---

## 12. Success Metrics

| Metric | Target (Phase 1) | Target (Phase 4) |
|:---|---:|---:|
| Readiness score accuracy | ±10 points | ±5 points |
| AI generation success rate (first attempt) | 60% | 85% |
| Validation loop iterations (average) | <3 | <2 |
| User approval rate for AI suggestions | 70% | 90% |
| Deployment success rate | — | 95%+ |
| Time from scan to first deployment | — | <1 hour for simple projects |
| Agent uptime (connected) | 99% | 99.9% |
| Community GitHub stars | 500 (launch) | 10,000+ (Year 1) |
| contributors | 10+ | 100+ |

---

*End of Project Requirements Document*
