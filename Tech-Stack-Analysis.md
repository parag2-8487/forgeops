# Technology Stack Analysis (July 2026)

## AI-Powered DevOps Automation Platform

**Prepared:** 18 July 2026  
**Status:** Comprehensive Review & Recommendations  
**Project:** AI DevOps engineer platform  

---

## Executive Summary

This document presents a thorough technology-by-technology audit of the existing technical research for the **AI-Powered DevOps Automation Platform**. Each technology has been researched against the July 2026 ecosystem landscape to determine whether it remains the best choice, should be replaced, or requires supplementation.

### Key Findings

| Finding | Severity | Action Required |
|:--------|:--------:|:----------------|
| `nhooyr.io/websocket` is **deprecated** | 🔴 Critical | Replace with `github.com/coder/websocket` |
| Celery is **legacy** — lacks native `asyncio` | 🟡 Warning | Use ARQ/Dramatiq (asyncio-native) for P1; one durable engine (Temporal/Inngest) at P2 |
| Renovate license is **AGPL v3** (not MIT as stated) | 🟡 Warning | Keep Renovate (AGPL used as a tool, not linked); Dependabot **rejected** — far less configurable |
| Missing **testing frameworks** (pytest, vitest, k6, Playwright) | 🟡 Warning | Add to tech stack |
| Missing **Mimir for long-term Prometheus storage** | 🟢 Minor | Add to Phase 3 observability stack |
| Missing **GitOps tooling** (ArgoCD/Flux) | 🟢 Minor | Add for Phase 2+ deployment automation |
| Most core choices (Go, FastAPI, PostgreSQL, Next.js, OpenTofu, OTel) | ✅ **Confirmed Best Fit** | Continue with these choices |

**Overall Assessment:** The existing tech stack research is **well-researched and largely correct** for July 2026. The core architectural decisions — Go for the local agent, FastAPI for the backend, PostgreSQL + pgvector for the database, Next.js 16 + React for the frontend, OpenTofu for IaC — are all validated as industry best practices. Only **6 out of 50+ technologies** require changes or additions.

---

## Project Understanding

### Vision
a web-based system that acts as an **AI DevOps engineer** — analyzing local codebases, scoring production readiness, generating missing DevOps files, deploying across environments, managing Docker/Kubernetes, monitoring production, troubleshooting, self-healing, and learning from history.

### Architecture
Three-tier design:
1. **Tier 1:** Web Frontend (Next.js 16 + React 19) — dashboard, command center, approval UI
2. **Tier 2:** Cloud Backend (FastAPI + PostgreSQL) — REST API, WebSocket hub, AI engine, policy engine
3. **Tier 3:** Local Agent (Go) — outbound-only WSS connection, command execution, codebase scanning

### Scale Expectations
- 10,000+ concurrent agents per backend
- Unlimited concurrent projects per agent
- pgvector up to ~50M vectors
- 1,000+ jobs/second queue throughput

---

## Technology-by-Technology Review

---

### 1. Local Agent Language: Go

| Attribute | Assessment |
|:----------|:-----------|
| **Current Choice** | Go 1.26 |
| **Free / ** | ✅ Free, BSD-style license |
| **Licensing** | BSD-style (Go license) |
| **Industry Adoption** | ✅ Dominant for cloud-native tooling |
| **Long-term Viability** | ✅ Excellent |

**Assessment:** The existing research correctly identifies Go as the best choice. However, **Go 1.26** was released in February 2026 and is now the recommended version. Key updates include:
- **"Green Tea" Garbage Collector** — reduces GC overhead by 10–40%
- **Self-referential generics** now supported
- **Heap base address randomization** enabled by default on 64-bit systems
- **New `crypto/hpke`** package (RFC 9180) for Hybrid Public Key Encryption

**Recommendation:** ✅ **Keep but update version to Go 1.26.** The original reasoning (mature Docker/K8s SDKs, fast cross-compilation, single binary) remains correct.

---

### 2. Go WebSocket Library: nhooyr.io/websocket ⚠️

| Attribute | Assessment |
|:----------|:-----------|
| **Current Choice** | `github.com/nhooyr.io/websocket` v1.8+ |
| **Free / ** | ✅ MIT |
| **Industry Adoption** | ⚠️ Package is **deprecated** |

**Critical Finding:** The `nhooyr.io/websocket` package is **officially deprecated**. The maintainers now direct all users to `github.com/coder/websocket`, which is the active successor with a modern API emphasizing `context.Context` and continued maintenance.

**Alternatives:**
| Option | License | Status |
|:-------|:--------|:-------|
| `github.com/coder/websocket` | ISC | ✅ **Recommended** — actively maintained successor |
| `github.com/gorilla/websocket` | MIT | ⚠️ Legacy standard, still functional but less modern API |

**Recommendation:** 🔴 **Replace `nhooyr.io/websocket` with `github.com/coder/websocket`.** This is the actively maintained successor with an ISC license (not MIT).

---

### 3. Backend Framework: FastAPI (Python)

| Attribute | Assessment |
|:----------|:-----------|
| **Current Choice** | FastAPI 0.139.2+ |
| **Free / ** | ✅ MIT |
| **Industry Adoption** | ✅ Industry standard for Python AI backends |
| **Long-term Viability** | ✅ Excellent |

**Assessment:** FastAPI remains the industry standard for Python AI backends in July 2026. Its async-native architecture, Pydantic v2 integration (Rust-based `pydantic-core` providing 4–50x faster validation), and unmatched AI/ML ecosystem make it the correct choice.

**Key 2026 updates:**
- Pydantic v2's **strict mode** critical for enforcing deterministic structured LLM outputs
- Native WebSocket support through Starlette remains excellent
- OpenAPI auto-generation continues to be best-in-class

**Recommendation:** ✅ **Keep.** No better alternative exists for a Python-based AI platform.

---

### 4. Job Queue: Celery + Redis / BullMQ

| Attribute | Assessment |
|:----------|:-----------|
| **Current Choice** | Celery + Redis (Python) or BullMQ (if Node) |
| **Free / ** | ✅ BSD (Celery), MIT (BullMQ) |
| **Industry Adoption** | ⚠️ Celery is **legacy** for modern AI workloads |

**Critical Finding:** Celery **lacks native `asyncio` support**, which is a major bottleneck for AI agents performing I/O-bound LLM API calls. Celery also suffers from poor observability and complex rate-limiting configuration.

**Modern Alternatives (2026):**

| Option | Best For | License | Self-Hostable |
|:-------|:---------|:--------|:-------------|
| **Celery + Redis** | Simple Python tasks | BSD | ✅ Yes |
| **Temporal** | Complex, durable workflow orchestration with state management | MIT | ✅ Yes |
| **Inngest** | Event-driven durable functions, excellent DX | MIT (self-host available) | ✅ Yes |
| **Trigger.dev v4+** | Long-running AI tasks, built-in observability | MIT (self-host) | ✅ Yes |
| **BullMQ** | High-throughput Redis-based queues (Node-only) | MIT | ✅ Yes |

**Recommendation:** 🟡 **Phase 1: Use ARQ or Dramatiq** (asyncio-native — natural fit for async FastAPI, unlike Celery). **Phase 2: introduce ONE durable engine** (Temporal for full replay/history, or Inngest for self-host DX) behind an orchestrator-agnostic interface. Avoid the two-migration Celery → Inngest → Temporal path — it re-platforms the safety-critical deploy/rollback subsystem twice.

---

### 5. Primary Database: PostgreSQL + pgvector

| Attribute | Assessment |
|:----------|:-----------|
| **Current Choice** | PostgreSQL 17+ / pgvector 0.8.5 |
| **Free / ** | ✅ PostgreSQL License |
| **Industry Adoption** | ✅ Industry standard |
| **Long-term Viability** | ✅ Excellent |

**Assessment:** This is the 2026 consensus for RAG-enabled applications. PostgreSQL 17 brings improved `VACUUM` memory management, `JSON_TABLE()` support, and faster write throughput under high concurrency. pgvector 0.8.5 has matured significantly with parallel HNSW fixes and memory optimization for IVFFlat index builds.

**Scaling ceiling:** pgvector handles up to ~50M vectors well. Beyond that, migrate to a dedicated vector DB (Qdrant, Milvus, Weaviate).

**Recommendation:** ✅ **Keep.** No better alternative exists. Adding a dedicated vector DB would be premature optimization.

---

### 6. ORM (Object-Relational Mapping)

**Issue:** The existing research does not specify an ORM for the Python backend.

**Recommendations (2026):**

| ORM | Best For | License | pgvector Support | FastAPI Compat |
|:----|:---------|:--------|:-----------------|:---------------|
| **SQLModel** | ✅ **Recommended** — bridges Pydantic + SQLAlchemy | MIT | Via raw SQL | ✅ Native |
| **SQLAlchemy 2.0+** | Complex enterprise logic requiring deep control | MIT | Via custom expressions | ✅ Excellent |
| **Prisma (Python)** | Quick prototyping, JS-to-Python teams | Apache 2.0 | ⚠️ Limited | ✅ Good |
| **Drizzle ORM** | TypeScript-like DX (Python version emerging) | Apache 2.0 | Emerging | ⚠️ Evolving |

**Recommendation:** 🟡 **Add SQLModel** to the tech stack. It eliminates boilerplate by bridging Pydantic (API models) and SQLAlchemy (database models) — a natural fit for FastAPI. Supports pgvector through raw SQL queries.

---

### 7. Redis (Caching & Job Queues)

| Attribute | Assessment |
|:----------|:-----------|
| **Free / ** | ✅ Redis License (RSALv2 + SSPL) |
| **Industry Adoption** | ✅ Industry standard |
| **Long-term Viability** | ✅ Excellent |

**Assessment:** Redis remains the industry standard for high-performance caching and job queue backing. The existing research correctly includes it.

**Recommendation:** ✅ **Keep.** No changes needed.

---

### 8. Authentication: Authentik

| Attribute | Assessment |
|:----------|:-----------|
| **Current Choice** | Authentik |
| **Free / ** | ✅ MIT (core) + Enterprise features paid |
| **Industry Adoption** | ✅ Strong in self-hosted ecosystem |
| **Long-term Viability** | ✅ Excellent |

**Assessment:** The existing research correctly identifies Authentik as the 2026 sweet spot for self-hosted auth. Its Flow Engine provides custom authentication workflows without code. Recent 2026 releases (version 2026.5+) add fleet connector capabilities for device trust signals.

**Alternative validation:** Keycloak (Apache 2.0) is the mature industry standard but carries significantly higher complexity. ZITADEL (AGPL 3.0) is strong for multi-tenant architectures but the AGPL license may conflict with the project's goals.

**Recommendation:** ✅ **Keep Authentik.** Add note that enterprise features (FIPS compliance, Google Workspace integration) are gated behind a paid tier.

---

### 9. Fine-Grained RBAC: Cerbos

| Attribute | Assessment |
|:----------|:-----------|
| **Current Choice** | Cerbos v0.54.0 |
| **Free / ** | ✅ Apache 2.0 |
| **Industry Adoption** | ✅ Growing, well-regarded |
| **Long-term Viability** | ✅ Good |

**Assessment:** Cerbos is correctly selected as a policy-as-code sidecar. It keeps authorization decisions out of application code, supports PBAC (Policy-Based Access Control) and ABAC (Attribute-Based Access Control), and integrates with any IdP.

**Recommendation:** ✅ **Keep.** Correct choice for fine-grained authorization.

---

### 10. Frontend Framework: Next.js 16 + React 19

| Attribute | Assessment |
|:----------|:-----------|
| **Current Choice** | Next.js 16 / React 19 |
| **Free / ** | ✅ MIT |
| **Industry Adoption** | ✅ Industry standard for production SaaS |
| **Long-term Viability** | ✅ Excellent |

**Assessment:** The existing research correctly identifies Next.js 16 + React 19 as the best choice for a dashboard-heavy application. Key 2026 updates:
- **Cache Components:** `"use cache"` directive for explicit caching control
- **Turbopack:** Now fully production-ready for significantly faster builds
- **Proxy.ts:** Replaces `middleware.ts` for cleaner request handling
- **DevTools MCP:** Integrates with AI assistants via Model Context Protocol

**Alternative considerations:**
- **SvelteKit + Svelte 5:** Excellent performance but lacks React Flow equivalent for pipeline designer
- **Nuxt 4 + Vue 3:** Smaller hiring pool, fewer specialized dashboard component libraries

**Recommendation:** ✅ **Keep.**

---

### 11. UI Library: shadcn/ui + Radix UI

| Attribute | Assessment |
|:----------|:-----------|
| **Current Choice** | shadcn/ui + Radix UI |
| **Free / ** | ✅ MIT |
| **Industry Adoption** | ✅ Standard for 2026 dashboard apps |
| **Long-term Viability** | ✅ Excellent |

**Assessment:** shadcn/ui remains the default standard for dashboard components in 2026. The "copy-paste" architecture gives developers full control of source code, avoiding black-box dependency issues.

**Recommendation:** ✅ **Keep.**

---

### 12. State Management: TanStack Query + Zustand

| Attribute | Assessment |
|:----------|:-----------|
| **Current Choice** | TanStack Query v6 + Zustand v5 |
| **Free / ** | ✅ MIT |
| **Industry Adoption** | ✅ Industry standard |
| **Long-term Viability** | ✅ Excellent |

**Assessment:** TanStack Query v6 is non-negotiable for server/WebSocket state in 2026. Zustand v5 continues to lead client-side state management over Redux due to minimal boilerplate.

**Recommendation:** ✅ **Keep.**

---

### 13. Styling: Tailwind CSS v4

| Attribute | Assessment |
|:----------|:-----------|
| **Current Choice** | Tailwind CSS v4 |
| **Free / ** | ✅ MIT |
| **Industry Adoption** | ✅ Industry standard |
| **Long-term Viability** | ✅ Excellent |

**Assessment:** Tailwind CSS v4 offers superior build speed with its Rust-powered engine. Works perfectly with shadcn/ui.

**Recommendation:** ✅ **Keep.**

---

### 14. Charts: Apache ECharts v5

| Attribute | Assessment |
|:----------|:-----------|
| **Current Choice** | Apache ECharts 5.x |
| **Free / ** | ✅ Apache 2.0 |
| **Industry Adoption** | ✅ Strong |
| **Long-term Viability** | ✅ Good |

**Assessment:** Canvas-based, handles dense data without performance issues, highly configurable. No better alternative for the project's dashboard needs.

**Recommendation:** ✅ **Keep.**

---

### 15. Log Viewer: xterm.js v5

| Attribute | Assessment |
|:----------|:-----------|
| **Current Choice** | xterm.js 5.x |
| **Free / ** | ✅ MIT |
| **Industry Adoption** | ✅ Standard for terminal emulation in browser |
| **Long-term Viability** | ✅ Excellent |

**Recommendation:** ✅ **Keep.**

---

### 16. Flow Editor: React Flow (xyflow) v12

| Attribute | Assessment |
|:----------|:-----------|
| **Current Choice** | React Flow (xyflow) 12.x |
| **Free / ** | ✅ MIT |
| **Industry Adoption** | ✅ Gold standard for node-based UIs |
| **Long-term Viability** | ✅ Excellent |

**Recommendation:** ✅ **Keep.**

---

### 17. Diagrams: D2 + Mermaid

| Attribute | Assessment |
|:----------|:-----------|
| **Current Choice** | D2 (primary) + Mermaid (fallback) |
| **Free / ** | ✅ Apache 2.0 (D2), MIT (Mermaid) |
| **Industry Adoption** | ✅ D2 growing fast, Mermaid mature |
| **Long-term Viability** | ✅ Good |

**Assessment:** D2 has reached major v2+ and is now mature. Its auto-layout (dagre, ELK, TALA) is superior to Mermaid's basic layout engine. D2's syntax is more readable and less "arcane" than Mermaid.

**Recommendation:** ✅ **Keep.** Correct choice of primary + fallback.

---

### 18. Forms: React Hook Form + Zod

**Issue:** Not explicitly listed in the existing tech stack table, but implied in the frontend section.

| Attribute | Assessment |
|:----------|:-----------|
| **Free / ** | ✅ MIT |
| **Industry Adoption** | ✅ Undisputed standard for 2026 |
| **Long-term Viability** | ✅ Excellent |

**Recommendation:** 🟡 **Add explicitly** to the tech stack table.

---

### 19. Data Tables: TanStack Table v8

| Attribute | Assessment |
|:----------|:-----------|
| **Free / ** | ✅ MIT |
| **Industry Adoption** | ✅ Industry standard |
| **Long-term Viability** | ✅ Excellent |

**Recommendation:** ✅ **Keep.**

---

### 20. Diff Viewer: react-diff-viewer

| Attribute | Assessment |
|:----------|:-----------|
| **Free / ** | ✅ MIT |
| **Industry Adoption** | ⚠️ Niche but appropriate |
| **Long-term Viability** | ⚠️ Stable but low activity |

**Assessment:** The existing research mentions react-diff-viewer or Monaco diff. Both are appropriate for the approval center's diff previews.

**Recommendation:** ✅ **Keep either option.**

---

### 21. Package Manager: npm/pnpm

**Assessment:** The existing research doesn't specify a package manager. For 2026, **Bun** is a highly viable alternative for install speed and CI/CD script execution.

**Recommendation:** 🟡 **Consider pnpm** (for deterministic installs) or **Bun** (for speed). Both are compatible with the Next.js ecosystem.

---

## AI Models Analysis

### Current AI Model Stack (from existing research)

| Task | Flagship | Best Value |
|:-----|:---------|:-----------|
| Code/Config Generation | Claude Fable 5 | Claude Sonnet 5 |
| Codebase Analysis | Claude Opus 4.8 | Claude Sonnet 5 |
| Log Root-Cause Analysis | Gemini 3.1 Pro | Gemini 3 Flash |
| Command Interpretation | GPT-5.6 Sol | Claude Sonnet 5 / GPT-5.4 Mini |

### July 2026 Updated Model Comparison

| Model | SWE-bench Verified | Context Window | Structured Output | Input Price /1M tok | Output Price /1M tok | Release Date |
|:-----|:------------------:|:--------------:|:-----------------:|:-------------------:|:--------------------:|:------------:|
| **GPT-5.6 Sol** | **97%** | ~1-2M | ✅ JSON/tools/functions | $15 | **$60** (↓20%) | July 9, 2026 |
| **Claude Fable 5** | 95% | ~2M+ | ✅ XML/JSON/tools | $15 | $75 | June 9, 2026 |
| **Grok 4.5** (est. — unverified) | ~90% (est.) | 256K+ | ✅ | $5 | $20 | **July 8, 2026** ★ New |
| **Claude Sonnet 5** | 89% | ~2M+ | ✅ | $3 | $15 | 2026 |
| **DeepSeek V4 (Stable)** | 88% | 256K-1M | ✅ | $0.50 | $2 | **July 24, 2026** ★ Stable |
| **Claude Opus 4.8** | 88.6% | ~2M+ | ✅ | $20 | $100 | 2026 |
| **GPT-5.4 Mini** | ~80% | ~1M | ✅ | $0.50 | $2 | 2026 |
| **Gemini 3.1 Pro** | ~80% | 2M+ | ✅ | $5 | $20 | 2026 |
| **Gemini 3 Flash** | ~78% | 1M+ | ✅ | $0.20 | $0.50 | 2026 |

### New Open-Weight Models (Late July 2026)

| Model | Type | SWE-bench Pro (est.) | Key Strength | License | Hardware Required |
|:------|:----:|:--------------------:|:-------------|:--------|:-----------------|
| **GLM-5.2** (Z.ai) | Open-weight | Frontier-adjacent | Best-in-class open-weight engineering/reasoning | Apache 2.0 | 48GB+ VRAM |
| **DeepSeek V4 Flash** | Open-weight | ~85% | Extremely cost-effective bulk long-context analysis | MIT | 24GB+ VRAM |
| **Kimi K2.7 Code** (Moonshot AI) | Open-weight | ~87% | Multimodal (text/image/video) optimized for autonomous agentic execution | Community | 32GB+ VRAM |
| **Qwen3-Coder-Next** | Open-weight | ~88% | Successor to Qwen3-Coder; improved code generation | Apache 2.0 | 24GB+ VRAM |

### Key Changes from Original Research

1. **GPT-5.6 Sol now leads SWE-bench** at 97% (original research claimed 93%). This makes it the top performer for high-complexity tasks, not Claude Fable 5.

2. **Claude Opus 4.8 SWE-bench score** corrected from 93% to **88.6%** based on updated July 2026 data.

3. **Grok 4.5 is a new entrant** (July 8, 2026) — high-performance agentic coding at 1/3 the cost of Frontier models. Strong value tier option.

4. **DeepSeek V4 graduated to stable** on July 24, 2026 — production-grade API with proven reliability.

5. **GPT-5.6 Sol output pricing dropped 20%** from $75 to $60/1M tokens — making it more cost-effective.

6. **All models now support structured outputs** as standard — no longer a differentiating factor.

### Updated Model Routing Strategy

**Validated as correct.** The tiered approach remains industry best practice. Updated with new entrants:

| Complexity Tier | Example Tasks | Primary Model | Secondary | Self-Hosted | Est. Cost/Task |
|:---------------|:--------------|:-------------|:----------|:------------|:--------------:|
| **High** | Architecture design, multi-file generation, complex deployment analysis | **GPT-5.6 Sol** | Claude Fable 5 | GLM-5.2 | $0.50-$2.00 |
| **Medium** | Dockerfile generation, CI/CD config, codebase analysis | **Claude Sonnet 5** or **Grok 4.5** | GPT-5.4 Mini | Qwen3-Coder-Next | $0.10-$0.50 |
| **Low** | Log analysis, simple explanations, formatting | **Gemini 3 Flash** or **DeepSeek V4** | GPT-5.4 Mini | DeepSeek V4 Flash | $0.01-$0.05 |
| **Self-hosted** | Air-gapped, sensitive codebases | **Qwen3-Coder-Next** | DeepSeek V4 Flash | GLM-5.2 | Free (hardware) |

### Free / AI Model Options

| Model | Type | Cost | Quality | Hardware Needed |
|:------|:-----|:----:|:-------:|:---------------|
| **DeepSeek V4** | API | $0.50/$2 per M tokens | ★★★★☆ | None (cloud API) |
| **Qwen3-Coder-32B** | Open-weight | Free | ★★★★★ | 24GB VRAM (Q4) |
| **DeepSeek V4-Pro** | Open-weight | Free | ★★★★★ | 48GB VRAM (Q4) |
| **Gemini 3 Flash** | API | $0.20/$0.50 per M tokens | ★★★☆☆ | None (cloud API) |

**Recommendation:** ✅ **Keep the existing model routing strategy.** GPT-5.6 Sol should be the primary flagship model. **⚠️ Caveat:** all "SWE-bench Verified" percentages here are self-reported and scaffolding-dependent — not independently reproducible or apples-to-apples across vendors. Rank on **SWE-bench Pro plus an internal golden dataset** of this project's own task archetypes for selection.

---

## Architecture Recommendations

### Three-Tier Architecture — **Validated as Sound**

The existing three-tier architecture (Web Frontend → Cloud Backend → Local Agent) is correct for July 2026. Key architectural patterns to emphasize:

1. **Modular Monolith First:** Start the backend as a modular monolith in FastAPI. Extract services only when they need independent scaling (antipattern avoided).

2. **MCP as Integration Surface:** MCP should be the **standard integration surface** for all tool connections. The AI agent communicates via MCP to Docker, Kubernetes, GitHub, OpenTofu, Prometheus, and more.

3. **Outbound-Only Agent:** The security invariants (outbound-only WSS, command whitelisting, double policy evaluation, approval IDs, atomic change-sets) are all current best practices.

### Recommended Architecture Diagram (Updated with MCP Gateway + Multi-Tenant Scale)

```
┌──────────────────────────────────────────────────────────────────────────────┐
│  TIER 1: Web Frontend (Next.js 16 + React 19)                                │
│  - shadcn/ui + Radix UI + TanStack Query + Zustand                           │
│  - React Flow (pipeline designer), xterm.js (logs), ECharts (charts)         │
│  - Tailwind CSS v4 styling, React Hook Form + Zod for forms                  │
│  - Onboarding: Show readiness score BEFORE auth ("Value First" pattern)     │
└────────────────────────────┬─────────────────────────────────────────────────┘
                             │ WSS / REST / OTLP
┌────────────────────────────▼─────────────────────────────────────────────────┐
│  TIER 2: Cloud Backend (FastAPI + PostgreSQL 17 + pgvector 0.8.5)             │
│                                                                              │
│  ┌────────────────────────┐  ┌─────────────────────────┐  ┌───────────────┐ │
│  │ Tenant Context         │  │ WebSocket Hub (Redis    │  │ AI Engine      │ │
│  │ Middleware (RLS via    │  │ Cluster Pub/Sub)        │  │ (LangGraph +   │ │
│  │ PgBouncer transaction) │  │ 10K+ concurrent agents  │  │  LlamaIndex)   │ │
│  └────────────────────────┘  └─────────────────────────┘  └───────┬───────┘ │
│                                                                    │         │
│  ┌────────────────────────┐  ┌─────────────────────────┐           ▼         │
│  │ Auth Service           │  │ Policy Engine (OPA      │    ┌────────────────┐│
│  │ (Authentik + Cerbos)   │  │ sidecar + Embedded Wasm │    │ MCP Gateway    ││
│  │ OAuth 2.1/OIDC, SSO    │  │ for Go agent)           │    │ (OAuth+OPA+    ││
│  └────────────────────────┘  └─────────────────────────┘    │  TTL Cache)    ││
│                                                              │  Mcp-Method &  ││
│  ┌────────────────────────┐  ┌─────────────────────────┐    │  Mcp-Name hdr  ││
│  │ Secret Vault           │  │ Async Tasks         │    └───────┬────────┘│
│  │ (Infisical E2EE)       │  │ (ARQ/Dramatiq P1    │            │         │
│  │ BYOK LLM Key Mgmt      │  │ → Inngest P2        │            ▼         │
│  └────────────────────────┘  │ → Temporal P3+ opt) │    ┌────────────────┐│
│                                                              │ MCP Servers:   ││
│  ┌────────────────────────┐  ┌─────────────────────────┐    │ Docker, K8s,   ││
│  │ PostgreSQL 17          │  │ Redis Cluster (Sharded)  │    │ GitHub, Tofu,  ││
│  │ (+ pgvector 0.8.5)     │  │ L1 Exact-match cache    │    │ Vault, OTel,   ││
│  │ HNSW indexes default   │  │ L2 Semantic cache       │    │ AI Gateway     ││
│  │ RLS multi-tenant iso.  │  │ L3 Prefix cache         │    └────────────────┘│
│  └────────────────────────┘  └─────────────────────────┘                       │
└────────────────────────────┬──────────────────────────────────────────────────┘
                             │ WSS (outbound-only, mTLS — MCP over WSS)
┌────────────────────────────▼──────────────────────────────────────────────────┐
│  TIER 3: Local Agent (Go 1.26)                                                │
│  - Connection Manager (coder/websocket, auto-reconnect with exponential backoff)│
│  - Command Executor (whitelist + double policy eval + approval verification)   │
│  - Codebase Scanner (official tree-sitter Go bindings, cAST semantic chunking) │
│  - Embedding Engine (Voyage Code 3 API / BGE-M3 local, HNSW index in pgvector)|
│  - Docker Module (Engine API) + K8s Module (client-go)                        │
│  - IaC Runner (OpenTofu subprocess) + Helm SDK                                │
│  - Validation Engine (dry-runs, schema, OPA Wasm embedded)                    │
│  - Safe Default Template Library (8+ languages, deterministic fallback)       │
│  - Telemetry Collector (OTel OTLP export)                                     │
│  - Cold Start Discovery Mode (progressive indexing, partial results)          │
└───────────────────────────────────────────────────────────────────────────────┘
```

---

## Missing Technologies

The following technologies are essential but **not explicitly included** in the existing tech stack:

### Critical Missing Technologies

| Technology | Purpose | Priority | Recommendation |
|:-----------|:--------|:--------:|:---------------|
| **SQLModel** | Python ORM bridging Pydantic + SQLAlchemy | High | Add — natural fit for FastAPI |
| **pytest** | Python unit/integration testing | High | Add — industry standard |
| **vitest** | Frontend testing | High | Add — Vite-native JS testing |
| **Playwright** | End-to-end testing | High | Add — best E2E for dashboard apps |
| **k6** | Load/performance testing | Medium | Add — cloud-native load testing |
| **Ruff** | Python linting + formatting | Medium | Add — replaces flake8, black, isort |
| **golangci-lint** | Go linting | Medium | Already in CI pipeline description |

### Recommended Missing Technologies

| Technology | Purpose | Priority | Recommendation |
|:-----------|:--------|:--------:|:---------------|
| **ArgoCD** | GitOps deployment (Phase 2+) | Medium | Add — pull-based deployment automation |
| **Grafana Mimir** | Long-term Prometheus metrics storage | Medium | Add to Phase 3 observability |
| **React Hook Form + Zod** | Form validation | Medium | Add explicitly to tech stack |
| **pnpm** or **Bun** | Package management | Low | Add — faster installs, deterministic |

### Optional Enhancements

| Technology | Purpose | When | Recommendation |
|:-----------|:--------|:----:|:---------------|
| **Dagger.io** | Portable CI/CD pipelines | Phase 5 (deferred) | Deferred — standard GitHub Actions + goreleaser is sufficient; programmable pipelines add complexity for current needs |
| **Checkly** | Synthetic monitoring as code | — (rejected) | Rejected — synthetic monitoring is a small subset of the full observability stack; not comprehensive enough |
| **Better Stack** | Incident management + status pages | Phase 3 | Aligns with notification system |
| **SigNoz** | APM alternative | Phase 3 | OTel-native, simpler than Grafana stack |
| **SOPS** | GitOps-native secret encryption | — (rejected) | Rejected — Infisical already provides web dashboard, RBAC, audit logs, and rotation that SOPS lacks |
| **Dependabot** | GitHub-native dependency updates | — (rejected) | Rejected — Renovate already chosen and is significantly more configurable (grouping, scheduling, monorepo, merge confidence) |

### New Technologies (Late July 2026 Update)

| Technology | Category | Purpose | Phase | License |
|:-----------|:---------|:--------|:-----:|:--------|
| **GLM-5.2 (Z.ai)** | AI Model | Self-hosted open-weight model; frontier-adjacent SWE-bench Pro performance | Phase 1 | Apache 2.0 |
| **Grok 4.5** | AI Model | High-performance agentic coding at 1/3 frontier cost; medium complexity tier | Phase 1 | Proprietary |
| **Kimi K2.7 Code** | AI Model | Multimodal open-weight model for autonomous agentic execution | Phase 3 | Community |
| **Official Tree-sitter Go bindings** | Code Analysis | Production-grade AST parsing for Go agent | Phase 1 | MIT |
| **PostgreSQL RLS** | Multi-Tenant | Row-Level Security for tenant isolation; single-DB multi-tenancy | Phase 1 | PostgreSQL |
| **Redis Cluster** | Scaling | Sharded Redis for WebSocket Pub/Sub at 10K+ concurrent agents | Phase 2 | Redis |
| **Safe Default Template Library** | Error Recovery | Hardcoded, verified fallback templates for 8+ languages (Node.js, Python, Go, Rust, Java, Ruby, PHP, .NET) | Phase 1 | Proprietary (Part of Platform) |
| **PgBouncer** | Scaling | Connection pooling for PostgreSQL at high concurrency | Phase 1 | PostgreSQL |
| **Temporal (Durable Execution)** | Workflow | Stateful workflow-as-code for multi-step agentic workflows | Phase 3 | MIT |

---

## Security Recommendations

### Validated Security Architecture

The existing security invariants are **all current best practices:**

| Invariant | Assessment |
|:----------|:-----------|
| Outbound-only agent connections | ✅ Mandatory for this architecture |
| Named whitelisted operations | ✅ Prevents arbitrary command execution |
| Approval ID for every mutation | ✅ Defense in depth |
| Double policy evaluation (server + agent) | ✅ Zero-trust pattern |
| Secrets encrypted at rest (AES-256-GCM) | ✅ Correct |
| Backup-before-mutate + atomic change-sets | ✅ Production-grade |
| Path blocklists (~/.ssh, ~/.aws, .env, *.pem) | ✅ Correct |

### Additional Security Recommendations

1. **Binary signing:** Cosign for all releases ✅ Already included
2. **SBOM generation:** Add CycloneDX SBOM generation to CI pipeline 🔵 New
3. **Supply-chain security:** Add `go vet` and dependency verification to agent build 🔵 New
4. **Rate limiting:** Per-user, per-project, per-operation token budgets ✅ Already included
5. **Audit logging:** Every action logged with who, what, when, why ✅ Already included

---

## Performance Recommendations

### Validated Performance Targets

| Target | Assessment | Notes |
|:-------|:-----------|:------|
| Codebase analysis <30s (<10K files) | ✅ Achievable | With Tree-sitter + incremental scanning |
| Codebase analysis <5min (<100K files) | ✅ Achievable | May require optimization for monorepos |
| AI artifact generation <15s first attempt | ⚠️ Ambitious | Depends on model latency; use streaming |
| WebSocket latency <100ms | ✅ Achievable | With coder/websocket and WSS |
| Dashboard page load <2s initial | ✅ Achievable | Next.js PPR + Turbopack |
| Dashboard page load <500ms cached | ✅ Achievable | TanStack Query caching |

### Performance Optimization Suggestions

1. **Incremental scanning** (FR-15) should be prioritized to Phase 1 (currently Phase 2). Full rescans are expensive for medium+ projects.

2. **Model streaming:** All LLM calls should use streaming responses to give users immediate feedback.

3. **pgvector indexing:** Use HNSW indexes (not IVFFlat) for production workloads. HNSW provides faster search at the cost of slower index builds.

4. **Redis caching layer:** Cache codebase analysis results and readiness scores with TTL-based invalidation.

---

## Scalability Recommendations

### Validated Scalability Targets

| Target | Assessment | Path |
|:-------|:-----------|:-----|
| 10,000+ concurrent agents per backend | ✅ Achievable | Stateless FastAPI behind load balancer |
| Unlimited projects per agent | ✅ Go handles this | File-system bound, not CPU/memory bound |
| pgvector up to ~50M vectors | ✅ Achievable | Add indexes, consider partitioning at 10M+ |
| 1,000+ jobs/second throughput | ✅ Achievable | Redis can handle this; ARQ/Dramatiq (asyncio-native) at P1, durable engine at P2 |

### Scalability Concerns

1. **PostgreSQL connection pooling:** At 10,000+ concurrent agents, connection pooling (PgBouncer) is mandatory. Add to infrastructure.

2. **WebSocket scalability:** The WebSocket hub will need horizontal scaling with a Redis Pub/Sub backend for multi-instance broadcasting.

3. **Codebase embedding costs:** For 50M+ vectors, consider migrating to a dedicated vector DB (Qdrant/Milvus). Monitor pgvector performance metrics proactively.

---

## Cost Analysis

### Per-Technology Cost Breakdown

| Technology | Free? | ? | Paid Tier? | Usage Limits | Free Tier |
|:-----------|:-----:|:------------:|:----------:|:------------:|:---------:|
| **Go 1.26** | ✅ Free | ✅ BSD | N/A | Unlimited | ✅ |
| **FastAPI** | ✅ Free | ✅ MIT | N/A | Unlimited | ✅ |
| **PostgreSQL** | ✅ Free | ✅ PostgreSQL | N/A | Unlimited | ✅ |
| **pgvector** | ✅ Free | ✅ PostgreSQL | N/A | Unlimited | ✅ |
| **Redis** | ✅ Free | ✅ Redis | N/A | Unlimited | ✅ |
| **SQLModel** | ✅ Free | ✅ MIT | N/A | Unlimited | ✅ |
| **Next.js** | ✅ Free | ✅ MIT | N/A | Unlimited | ✅ |
| **React** | ✅ Free | ✅ MIT | N/A | Unlimited | ✅ |
| **shadcn/ui** | ✅ Free | ✅ MIT | N/A | Unlimited | ✅ |
| **Tailwind CSS** | ✅ Free | ✅ MIT | N/A | Unlimited | ✅ |
| **TanStack Query** | ✅ Free | ✅ MIT | N/A | Unlimited | ✅ |
| **Zustand** | ✅ Free | ✅ MIT | N/A | Unlimited | ✅ |
| **Apache ECharts** | ✅ Free | ✅ Apache 2.0 | N/A | Unlimited | ✅ |
| **xterm.js** | ✅ Free | ✅ MIT | N/A | Unlimited | ✅ |
| **React Flow** | ✅ Free | ✅ MIT | N/A | Unlimited | ✅ |
| **D2** | ✅ Free | ✅ Apache 2.0 | N/A | Unlimited | ✅ |
| **Mermaid** | ✅ Free | ✅ MIT | N/A | Unlimited | ✅ |
| **OpenTofu** | ✅ Free | ✅ MPL 2.0 | N/A | Unlimited | ✅ |
| **OPA** | ✅ Free | ✅ Apache 2.0 | N/A | Unlimited | ✅ |
| **Kyverno** | ✅ Free | ✅ Apache 2.0 | N/A | Unlimited | ✅ |
| **OpenTelemetry** | ✅ Free | ✅ Apache 2.0 | N/A | Unlimited | ✅ |
| **Prometheus** | ✅ Free | ✅ Apache 2.0 | N/A | Unlimited | ✅ |
| **Grafana** | ✅ Free | ✅ AGPL v3 | ✅ Grafana Cloud | Yes | ✅ Generous free tier |
| **Loki** | ✅ Free | ✅ AGPL v3 | ✅ Grafana Cloud | Yes | ✅ Generous free tier |
| **Tempo** | ✅ Free | ✅ AGPL v3 | ✅ Grafana Cloud | Yes | ✅ Generous free tier |
| **Helm** | ✅ Free | ✅ Apache 2.0 | N/A | Unlimited | ✅ |
| **Velero** | ✅ Free | ✅ Apache 2.0 | N/A | Unlimited | ✅ |
| **Cosign** | ✅ Free | ✅ Apache 2.0 | N/A | Unlimited | ✅ |
| **Trivy** | ✅ Free | ✅ Apache 2.0 | N/A | Unlimited | ✅ |
| **goreleaser** | ✅ Free | ✅ MIT | N/A | Unlimited | ✅ |
| **Novu** | ✅ Free | ✅ MIT | ✅ Novu Cloud | Yes | ✅ 30K events/month |
| **Infracost** | ✅ Free | ✅ Apache 2.0 | ✅ Infracost Cloud | Yes | ✅ Free tier |
| **Kubecost** | ✅ Free | ✅ Apache 2.0 | ✅ Enterprise | Yes | ✅ Free tier |
| **Gitleaks** | ✅ Free | ✅ MIT | N/A | Unlimited | ✅ |
| **TruffleHog** | ✅ Free | ✅ Apache 2.0 | ✅ TruffleHog Cloud | Yes | ✅ Free tier |
| **Renovate** | ✅ Free | ✅ AGPL v3 | N/A | Unlimited | ✅ |
| **OSV.dev** | ✅ Free | ✅ Apache 2.0 | N/A | Unlimited (rate-limited API) | ✅ |

### AI Model Costs (Estimated Monthly)

| Usage Scenario | Est. Monthly Cost | Notes |
|:---------------|:-----------------:|:------|
| Individual developer (light use) | $20-$50/month | Mostly free tier + some Sonnet 5 |
| Small team (5 devs, moderate use) | $200-$500/month | Mix of Sonnet 5 and Gemini 3 Flash |
| Startup (20 devs, heavy use) | $1,000-$3,000/month | Includes GPT-5.6 Sol for complex tasks |
| Enterprise (100+ devs) | $5,000-$20,000/month | BYO-Key, bulk pricing, local models for sensitive work |

### Can a Student Build This Completely Free?

**Yes, with caveats:**

- **All infrastructure software is free and ** (PostgreSQL, Redis, FastAPI, Go, Next.js, etc.)
- **AI models:** Free tier options exist but are limited
  - **DeepSeek V4 API:** $0.50/M input tokens — cheapest paid option
  - **Gemini 3 Flash:** $0.20/M input tokens — Google's lowest cost
  - **Local models** (Qwen3-Coder, DeepSeek V4-Pro) via Ollama — free but require hardware (24GB+ VRAM)
  - **GitHub Copilot free tier** (limited) for development assistance
- **Cloud hosting:** Free tiers available on Railway, Render, Fly.io, or self-host on a $10/month VPS
- **CI/CD:** GitHub Actions is free for public repositories

**Estimated minimum monthly cost for a functioning prototype:** **$0-$10/month** (if using free tiers + a cheap VPS).

### Hidden Costs to Monitor

1. **pgvector at scale:** Large vector indexes consume significant RAM. 50M vectors at 1536 dimensions ≈ 300GB RAM.
2. **Observability storage:** Logs and metrics storage costs grow with usage. Plan retention policies early.
3. **CI/CD minutes:** Free GitHub Actions minutes may be insufficient for larger teams.
4. **Container registry storage:** GHCR provides 500MB free; larger images may incur costs.

---

## Final Recommended Tech Stack

### Confirmed (Keep All)

| Layer | Technology | Version | License |
|:------|:-----------|:--------|:--------|
| Local Agent Language | **Go** | **1.26** (update from 1.23+) | BSD |
| Backend Framework | **FastAPI** | 0.139.2+ | MIT |
| Primary Database | **PostgreSQL** | 17+ | PostgreSQL |
| Vector Extension | **pgvector** | 0.8.5 | PostgreSQL |
| Auth | **Authentik** | 2024+ | MIT |
| Fine-Grained RBAC | **Cerbos** | v0.54.0 | Apache 2.0 |
| Frontend Framework | **Next.js 16** + **React 19** | 16 / 19 | MIT |
| UI Library | **shadcn/ui** + **Radix UI** | Latest | MIT |
| State (Server) | **TanStack Query** | 6.x | MIT |
| State (Client) | **Zustand** | 5.x | MIT |
| Charts | **Apache ECharts** | 5.x | Apache 2.0 |
| Log Viewer | **xterm.js** | 5.x | MIT |
| Flow Editor | **React Flow (xyflow)** | 12.x | MIT |
| Diagrams | **D2** (primary) + **Mermaid** (fallback) | 2.x / 11.x | Apache 2.0 / MIT |
| Policy Engine | **OPA/Rego** + **Kyverno** | 2024+ | Apache 2.0 |
| Secret Vault | **Infisical** | 2026 | MIT |
| Git-Leak Detection | **Gitleaks** + **TruffleHog** | Latest | MIT / Apache 2.0 |
| Cost Analysis | **Infracost** + **Kubecost** | Latest | Apache 2.0 |
| Dependency Scanner | **Trivy** + **Renovate** | Latest | Apache 2.0 / AGPL v3 |
| Vulnerability DB | **OSV (Google)** | — | Apache 2.0 |
| K8s Backup | **Velero** | 1.14+ | Apache 2.0 |
| Notification Hub | **Novu** | Latest | MIT |
| IaC Engine | **OpenTofu** | 1.12.5 | MPL 2.0 |
| Helm | **Helm SDK** (Go) / OCI | 3.x | Apache 2.0 |
| Observability | **OTel Collector → Prometheus + Loki + Tempo** | Latest | Apache 2.0 / AGPL v3 |
| GitHub Integration | **GitHub App** | — | — |
| Container Registry | **GHCR** (cloud) / **Harbor** (self-hosted) | — | Apache 2.0 |
| Agent Auto-Update | **Cosign** + **goreleaser** | Latest | Apache 2.0 / MIT |
| License (Agent/CLI) | **Apache 2.0** | — | Apache 2.0 |
| License (Backend) | **FSL** or **BSL 1.1** | — | Fair Source |
| RAG Framework | **LangGraph** + **LlamaIndex** | 2026 | MIT |
| Code Embeddings | **Voyage Code 3** (API) / **BGE-M3** (self-hosted) | 2026 | API / MIT |
| Tool Integration | **MCP (Model Context Protocol)** | 2026 | MIT |
| Service Mesh | **Cilium** (eBPF, sidecarless, Hubble) → **Istio Ambient** (fallback) | 2026 | Apache 2.0 |
| Progressive Delivery | **Argo Rollouts** (pairs with ArgoCD) | 1.9.1 | Apache 2.0 |
| Code/Diff Editor | **CodeMirror 6** (Monaco only for IDE-grade IntelliSense) | Latest | MIT |
| Agent Governance | **Governance Control Plane** (policy+approval+audit+change-set+rollback) | P1 | Custom |
| Agent Identity | **SPIFFE/SPIRE X.509-SVID + mTLS** with attestation | 2026 | Apache 2.0 |
| SSE Streaming | **FastAPI native `EventSourceResponse`** (0.139.2+) | 0.139.2 | MIT |
| Local Inference | **vLLM** (prod) + **Ollama** (dev) | 2026 | Apache 2.0 / MIT |
| Open-Weight Models | **Qwen3-Coder** / **DeepSeek V4-Pro** | 2026 | Apache 2.0 / MIT |

### Updated / Replaced

| Layer | Old Choice | New Choice | Reason |
|:------|:-----------|:-----------|:-------|
| **Go Version** | 1.23+ | **1.26** | Go 1.26 released Feb 2026 with "Green Tea" GC, security improvements |
| **WebSocket Library** | `nhooyr.io/websocket` | **`github.com/coder/websocket`** | `nhooyr.io/websocket` is deprecated; maintainers recommend `coder/websocket` |
| **Job Queue** | Celery (Phase 1) | **ARQ/Dramatiq** (P1 fire-and-forget), **one durable engine — Temporal or Inngest** (P2) | Celery lacks native asyncio; introduce the durable engine once at P2 behind an orchestrator-agnostic interface (no two-migration path) |
| **ORM** | Not specified | **SQLModel** | Bridges Pydantic + SQLAlchemy, natural fit for FastAPI |
| **LLM Flagship** | Claude Fable 5 | **GPT-5.6 Sol** (revised ranking) | GPT-5.6 Sol now leads SWE-bench at 97% vs Fable 5's 95% |
| **Package Manager** | Not specified | **pnpm** or **Bun** | Deterministic installs (pnpm) or speed (Bun) |
| **Prometheus Long-term** | Not specified | **Grafana Mimir** | Needed for long-term metrics storage at scale |
| **Service Mesh** | Linkerd/Istio (generic) | **Cilium** (eBPF, sidecarless, Hubble); Istio Ambient fallback | Lowest overhead for 10k-agent self-host; Linkerd stable behind Buoyant subscription |
| **Progressive Delivery** | Not specified | **Argo Rollouts** (pairs with ArgoCD) | Progressive delivery not native to ArgoCD; gate on error-rate AND latency |
| **Editor** | Monaco / react-diff-viewer | **CodeMirror 6** (~50 KB review surface) | Monaco 2-5 MB unjustified for a review surface; CodeMirror 6 is ~50 KB |
| **Agent Governance** | Scattered checks | **Unified Governance Control Plane** (P1) | Policy+approval+audit+change-set+rollback as one enforced chokepoint — the trust moat |
| **Agent Identity** | JWT tokens | **SPIFFE/SPIRE X.509-SVID + mTLS** with attestation | No long-lived keys; JWT-SVID only for L7 proxy crossing |
| **SSE** | `sse-starlette` | **FastAPI native `EventSourceResponse`** (0.139.2+) | In-tree; sse-starlette now redundant |

### New Additions to Tech Stack

| Layer | Technology | Version | License | Priority |
|:------|:-----------|:--------|:--------|:--------|
| **ORM** | SQLModel | Latest | MIT | Phase 1 |
| **Python Testing** | pytest | 8.x | MIT | Phase 1 |
| **Frontend Testing** | vitest | 2.x | MIT | Phase 1 |
| **E2E Testing** | Playwright | Latest | Apache 2.0 | Phase 1 |
| **Load Testing** | k6 | Latest | AGPL v3 | Phase 2 |
| **Python Linting** | Ruff | Latest | MIT | Phase 1 |
| **Forms** | React Hook Form + Zod | Latest | MIT | Phase 1 |
| **GitOps** | ArgoCD | Latest | Apache 2.0 | Phase 2 |
| **Metrics Long-term** | Grafana Mimir | Latest | AGPL v3 | Phase 3 |
| **CI/CD Portability** | Dagger.io | Latest | Apache 2.0 | Phase 2 |

---

## Migration Suggestions

The following migration steps should be planned:

### Immediate (Phase 0 / Pre-Development)

1. **Replace `nhooyr.io/websocket` with `github.com/coder/websocket`**
   - Straightforward API migration
   - Same MIT license
   - Actively maintained successor

2. **Update Go version target to 1.26**
   - Green Tea GC reduces overhead 10-40%
   - Security improvements (heap randomization)
   - New standard library packages

3. **Add SQLModel to backend dependencies**
   - Install alongside SQLAlchemy
   - Define models using SQLModel's combined syntax
   - Gains Pydantic v2 validation automatically

### Phase 1

4. **Add testing framework infrastructure:**
   - pytest + pytest-asyncio for backend
   - vitest for frontend
   - Playwright for E2E
   - Ruff for Python linting (replaces flake8/black/isort)

5. **Add form management:**
   - React Hook Form + Zod for type-safe forms
   - Already implied in the UI layer, make explicit

### Phase 2

6. **Evaluate Temporal for complex workflows:**
   - Deployment pipelines with approval gates
   - Multi-step rollouts with rollback
   - Long-running AI tasks (codebase analysis, batch operations)

7. **Add GitOps tooling:**
   - ArgoCD for pull-based deployment
   - Store deployment configs in git
   - Drift detection and automated sync

### Phase 3

8. **Add Grafana Mimir for long-term metrics:**
   - PromQL-compatible
   - Handles multi-month retention
   - Horizontal scaling

9. **Consider dedicated vector DB if pgvector exceeds 50M vectors:**
   - Qdrant or Milvus as first consideration
   - Monitor pgvector performance proactively

---

## References

- Anthropic, "Introducing Claude Sonnet 5" (June 2026): [anthropic.com/news/claude-sonnet-5](https://www.anthropic.com/news/claude-sonnet-5)
- Vals.ai, "SWE-bench Verified Leaderboard" (July 2026): [vals.ai/benchmarks/swebench](https://www.vals.ai/benchmarks/swebench)
- Go 1.26 Release Notes: [go.dev/doc/go1.26](https://go.dev/doc/go1.26)
- coder/websocket (Successor to nhooyr): [github.com/coder/websocket](https://github.com/coder/websocket)
- OpenTelemetry Collector Survey Analysis (Jan 2026): [opentelemetry.io/blog/2026/otel-collector-follow-up-survey-analysis/](https://opentelemetry.io/blog/2026/otel-collector-follow-up-survey-analysis/)
- Grafana Labs 2026 Observability Trends: [grafana.com/blog/2026-observability-trends-predictions/](https://grafana.com/blog/2026-observability-trends-predictions-from-grafana-labs-unified-intelligent-and-open/)
- Datadog Gartner Report (July 2026): [datadoghq.com/blog/datadog-observability-platforms-gartner-magic-quadrant-2026/](https://www.datadoghq.com/blog/datadog-observability-platforms-gartner-magic-quadrant-2026/)
- D2 FAQ & Specs: [d2lang.com/tour/faq/](https://d2lang.com/tour/faq/)
- Stoplight Elements: [stoplight.io//elements](https://stoplight.io//elements)
- OSV Documentation: [google.github.io/osv.dev/](https://google.github.io/osv.dev/)

---

### New Implementation Pattern Additions (Late July 2026 — Sections 19-26)

| Pattern | Technology | Purpose | Phase | License |
|:--------|:-----------|:--------|:-----:|:--------|
| **Go DI** | Constructor injection (no framework) | Lightweight agent architecture | 0 | — |
| **SSE Streaming** | FastAPI native `EventSourceResponse` (in-tree since 0.139.2) | LLM token streaming to frontend | 1 | MIT |
| **LLM Eval** | DeepEval | Unit testing for LLM outputs | 2 | MIT |
| **AI Tracing** | LangFuse | Production AI observability + evaluation | 2 | MIT |
| **OTel Collector** | OpenTelemetry Collector | Unified telemetry pipeline (two-tier) | 3 | Apache 2.0 |
| **Build Pipeline** | GoReleaser + Cosign + Syft | Cross-platform binary release pipeline | 0 | MIT / Apache 2.0 |
| **Code Quality** | pre-commit framework | Code quality gates (gitleaks, ruff, gofmt) | 0 | MIT |
| **WebSocket Proxy** | kubernetes/ingress-nginx | WebSocket + SSE support at ingress | 2 | Apache 2.0 |
| **Event Scaler** | KEDA | Event-driven autoscaling (ARQ/Dramatiq + durable-engine workers by queue depth) | 2 | Apache 2.0 |
| **PostgreSQL on K8s** | CloudNativePG | PostgreSQL operator for K8s | 2 | Apache 2.0 |
| **Service Mesh** | Cilium (eBPF, sidecarless, Hubble) → Istio Ambient (fallback) | mTLS, golden signals, traffic mgmt | 3 | Apache 2.0 |
| **MCP Go SDK** | mark3labs/mcp-go | MCP server implementation in Go | 0 | MIT |
| **API Spec** | AsyncAPI | WebSocket protocol documentation | 1 | Apache 2.0 |
| **Error Format** | RFC 9457 (Problem Details) | Standardized API error responses | 1 | IETF |
| **Protocol** | JSON-RPC 2.0 over WebSocket | Structured agent protocol | 1 | — |

### Updated Immediate Action Items (Sections 19-26)

| Phase | New Action Items |
|:------|:----------------|
| **Phase 0** | Use constructor injection (not wire/uber-fx), set up GoReleaser with Cosign signing + Syft SBOM, use mark3labs/mcp-go for MCP server, set up pre-commit framework with Gitleaks + Ruff + gofmt |
| **Phase 1** | Implement SSE streaming with FastAPI native `EventSourceResponse` (in-tree since 0.139.2; no `sse-starlette` dependency) for all LLM operations, use JSON-RPC 2.0 over WebSocket for agent protocol, use URL-based API versioning (/api/v1/), implement RFC 9457 error responses, create golden dataset of 20+ project archetypes for regression testing |
| **Phase 2** | Set up DeepEval for LLM evaluation in CI, set up LangFuse for production AI tracing, create AsyncAPI spec for WebSocket protocol, set up KEDA autoscaling for ARQ/Dramatiq + durable-engine workers, configure NGINX Ingress with WebSocket/SSE tuning, deploy CloudNativePG for PostgreSQL on K8s |
| **Phase 3** | Deploy OTel Collector two-tier architecture (sidecar + gateway), implement hybrid sampling strategy (head-based 10% + tail-based errors), add gen_ai.* semantic conventions to all AI spans, add per-tenant cost tracking via OTel custom metrics, deploy Cilium service mesh for mTLS (Istio Ambient fallback) |

### References Added (Late July 2026 Update)

- FastAPI Production Architecture: https://dev.to/mrchike/fastapi-in-production-build-scale-deploy-series-a-codebase-design-ao3
- SQLAlchemy 2.0 Best Practices (Async): https://chaoticengineer.hashnode.dev/fastapi-sqlalchemy
- DeepEval (LLM Eval Framework): https://github.com/confident-ai/deepeval
- LangFuse (AI Observability): https://langfuse.com
- Future AGI LLM Eval: https://futureagi.com/blog/llm-evaluation-frameworks-metrics-best-practices/
- OTel Collector Scaling: https://opentelemetry.io/docs/collector/scaling/
- OTel GenAI Semantics: https://opentelemetry.io/docs/specs/semconv/gen-ai/
- GoReleaser Supply Chain: https://goreleaser.com/blog/supply-chain-security/
- Testkube AI Code Testing: https://testkube.io/blog/system-level-testing-ai-generated-code
- MCP Go SDK (mark3labs): https://github.com/mark3labs/mcp-go
- Official MCP Go SDK: https://github.com/modelcontextprotocol/go-sdk
- CloudNativePG: https://cloudnative-pg.io
- KEDA: https://keda.sh
- Linkerd: https://linkerd.io
- AsyncAPI: https://www.asyncapi.com
- RFC 9457 Problem Details: https://www.rfc-editor.org/rfc/rfc9457
- Arnica AI Security: https://www.arnica.io/blog/ai-code-security-complete-guide

---

*End of Technology Stack Analysis (v2.0 — Updated July 24, 2026 with 9 additional research streams covering Go agent patterns, FastAPI patterns, LLM evaluation, OTel deployment, CI/CD design, K8s architecture, MCP implementation, and API/WebSocket protocol design)*
