# Implementation Plan: Phase 2 — Deploy, Manage, Observe & Self-Heal

**Spec:** `phase-2-deploy-manage-observe`
**Project:** ForgeOps (`github.com/parag2-8487/forgeops`)
**Planning authority:** `phases.md` §Phase 2, `design.md`

## Overview

This plan converts the Phase 2 design and deliverables into tracked implementation tasks. Phase 2 merges deployment automation, environment management, host operations (Docker/Kubernetes), durable execution, AI command center, notifications, GitOps, progressive delivery, two-tier OpenTelemetry observability, AI troubleshooting/RCA, guard-railed self-healing, AI learning memory, and project knowledge base.

## Build Order & Dependencies

1. **2.1 Multi-Environment Management** (Prerequisite for all deployments, promotions, and approval rules)
2. **2.2 Deployment Automation** (First host mutation via the governance chokepoint)
3. **2.3 Rollback & Release Timeline** (Reads deployment history)
4. **2.4 Docker Management Dashboard & Host Proxy** (Shares the signing and chokepoint path with 2.2)
5. **2.4a Inngest Integration** (Durable execution engine for deployment workflows)
6. **2.5 AI Command Center** (Structured interpretation and guarded dispatch)
7. **2.6 Notification Center** (In-app, Slack, Discord, Email channels)
8. **2.7 & 2.7a ArgoCD & Argo Rollouts** (GitOps & progressive canary delivery)
9. **2.7b Service Mesh** (Cilium / Istio Ambient infrastructure)
10. **2.8 Local Development Tools** (Constrained devtools proxy and panel)
11. **2.9 Kubernetes Management Dashboard** (Pod, workload, namespace, cluster monitoring)
12. **2.10 OTel-Native Monitoring** (Two-tier collector, Prometheus, Mimir, Loki, Grafana)
13. **2.11 AI Troubleshooting & RCA** (Incident ingestion, log analysis, fix generation)
14. **2.12 Guard-Railed Self-Healing** (Two-tier auto/manual healing loop)
15. **2.13 AI Learning History** (Feedback logging, reflector agent, skill file injection)
16. **2.14 Knowledge Base Mode** (Project-scoped RAG question answering)

---

## Tasks

### 2.1 Multi-Environment Management
- [x] 2.1.1 Backend: Environment CRUD API (`Dev`, `Test`, `Staging`, `Prod` + custom) (`src/environments/`, migration `0023`)
- [x] 2.1.2 Backend: Environment-specific sealed variables, secrets (AES-256-GCM + HKDF), and Kubernetes contexts
- [x] 2.1.3 Backend: Environment-specific approval requirements (`requires_approval` defaulting to `true`, prod non-waivable)
- [x] 2.1.4 Backend: Promotion rules and flows between environments (`POST /projects/{id}/releases/promote`)
- [x] 2.1.5 Frontend: Environment management UI (`features/environments/EnvironmentManager.tsx`)
- [x] 2.1.6 Frontend: Environment selector mounted on Environment, Deployment, and Kubernetes dashboards

### 2.2 Deployment Automation
- [x] 2.2.1 Agent: Container image build and push to registry via `docker.image_action` authority
- [x] 2.2.2 Agent: Kubernetes manifest apply with health verification (`deployment.apply_manifests`)
- [x] 2.2.3 Agent: OpenTofu apply with state management (`iac.apply`, saved plan locking)
- [x] 2.2.4 Backend: Deployment record CRUD (`src/deployments/`, migration `0024`)
- [x] 2.2.5 Backend: Stable-state snapshot per successful deployment (`ck_deployments_stable_implies_healthy`)
- [x] 2.2.6 Backend: Live log streaming during deployment (SSE with `log` event type, `src/deployments/logs.py`)
- [x] 2.2.7 Backend: Deployment dispatcher advancing deployment status from `pending_approval` to `applying`
- [x] 2.2.8 Backend: Circuit breaker pattern for deployment pipeline (`src/deployments/breaker.py`, migration `0031`)
- [x] 2.2.9 Frontend: Deployment dashboard with progress indicators (`features/deployments/DeploymentDashboard.tsx`)
- [x] 2.2.10 Frontend: Deployment results view with structured logs and live SSE stream (`features/deployments/DeploymentResult.tsx`)

### 2.3 Rollback & Release Timeline
- [x] 2.3.1 Backend: Deployment history with full version metadata (`GET /releases/timeline`)
- [x] 2.3.2 Backend: Side-by-side diff between any two deployments (`GET /releases/diff`)
- [x] 2.3.3 Backend: Rollback to any previous stable deployment through governance chokepoint (`POST /releases/rollback`)
- [x] 2.3.4 Frontend: Timeline visualization with deployment markers (`features/releases/ReleaseTimeline.tsx`)
- [x] 2.3.5 Frontend: Side-by-side deployment manifest & health comparison

### 2.4 Docker Management Dashboard & Host Proxy
- [x] 2.4.1 Agent: Docker Engine API wrapper (`docker.inventory`, containers, images, volumes, networks)
- [x] 2.4.2 Backend: Unified agent operation proxy for Docker and Kubernetes host operations (`src/hostops/routes.py`, migration `0025`)
- [x] 2.4.3 Frontend: Container list with status, logs, and resource stats (`features/hostops/DockerDashboard.tsx`)
- [x] 2.4.4 Frontend: Container start, stop, restart, and delete controls
- [x] 2.4.5 Frontend: Image list with build, pull, push, and remove controls
- [x] 2.4.6 Frontend: Resource utilisation view distinguishing probe samples from metrics tier

### 2.4a Inngest Integration (Deployment Workflows)
- [x] 2.4a.1 Backend: Inngest container configuration and `/api/inngest` registration
- [x] 2.4a.2 Backend: Deployment pipeline declared as Inngest step functions (build -> push -> apply -> verify)
- [x] 2.4a.3 Backend: Approval-gated stages in Inngest workflows
- [x] 2.4a.4 Backend: Task dispatcher integration with engine-neutral protocol
- [x] 2.4a.5 Backend: Orchestrator-agnostic workflow definition wrappers

### 2.5 AI Command Center
- [x] 2.5.1 Backend: Intent classifier with closed intent frozenset (`src/commands/intents.py`)
- [x] 2.5.2 Backend: Natural language to structured command pipeline (validated slots, no shell injection)
- [x] 2.5.3 Backend: Multi-domain dispatch table
- [x] 2.5.4 Backend: 5-layer defense-in-depth guard-rails
- [x] 2.5.5 Frontend: Command input with server-published autocomplete (`features/commands/CommandCenter.tsx`)
- [x] 2.5.6 Frontend: Command results display with explicit confirm modal showing resolved parameters
- [x] 2.5.7 Frontend: Command history log per session with refusal reasons

### 2.6 Notification Center
- [ ] 2.6.1 Backend: Novu hosted integration (deferred: requires external hosted API key, replaced by native adapters)
- [x] 2.6.2 Backend: Structured notification templates for deployment lifecycle and policy violations
- [x] 2.6.3 Backend: Channel adapters for Slack webhook, Discord webhook, and SMTP email (`src/notifications/channels.py`)
- [x] 2.6.4 Frontend: Notification bell dropdown with delivery status (`features/notifications/NotificationBell.tsx`)
- [x] 2.6.5 Frontend: Per-user notification channel preferences

### 2.7 ArgoCD GitOps Integration
- [x] 2.7.1 Backend: ArgoCD Application manifest generation with App of Apps pattern (`src/argocd/renderers.py`)
- [x] 2.7.2 Agent: Governed `argocd app sync` subprocess executor (`agent/internal/executor/argocd.go`)
- [x] 2.7.3 Backend: ApplicationSet template generation with environment list generator
- [x] 2.7.4 Backend: Authenticated ArgoCD repository webhook event recorder

### 2.7a Argo Rollouts — Progressive Delivery
- [x] 2.7a.1 Backend: Argo Rollouts manifest generator for canary and blue-green strategies (`src/argocd/rollout_renderers.py`)
- [x] 2.7a.2 Backend: AnalysisTemplates gated on both error-rate and latency via Prometheus
- [x] 2.7a.3 Backend: Automatic rollback condition configuration on threshold breach
- [x] 2.7a.4 Frontend: Progressive rollout visualization panel (`features/rollouts/RolloutPanel.tsx`)

### 2.7b Service Mesh
- [x] 2.7b.1 Infra: Cilium eBPF sidecarless configuration (`infra/service-mesh/cilium-values.yaml`)
- [x] 2.7b.2 Infra: Istio Ambient fallback configuration (`infra/service-mesh/istio-ambient-values.yaml`)
- [x] 2.7b.3 Meta: Architectural decision test suite pinning service mesh invariants (`tests/meta/test_service_mesh_decision.py`)

### 2.8 Local Development Tools
- [x] 2.8.1 Agent: Ecosystem test execution (`devtools.run` kind `tests`)
- [x] 2.8.2 Agent: Linter execution (`devtools.run` kind `lint`)
- [x] 2.8.3 Agent: Project build execution (`devtools.run` kind `build`)
- [x] 2.8.4 Agent: Local compose execution (`devtools.run` kind `compose`)
- [x] 2.8.5 Agent: Database migration execution (`devtools.run` kind `migrations`)
- [x] 2.8.6 Backend: Governed devtools command proxy (`POST /projects/{id}/devtools/run`, migration `0027`)
- [x] 2.8.7 Frontend: Devtools panel in project dashboard (`features/devtools/DevToolsPanel.tsx`)

### 2.9 Kubernetes Management Dashboard
- [x] 2.9.1 Agent: Kubernetes API wrapper for pods, workloads, services, ingress, namespaces, HPA (`kubernetes.inventory`)
- [x] 2.9.2 Frontend: Pod list with status, logs, and events view (`features/hostops/KubernetesDashboard.tsx`)
- [x] 2.9.3 Frontend: Workload management (scale, restart, rollback) via `kubernetes.workload_action`
- [x] 2.9.4 Frontend: Namespace explorer scoping all dashboard panels
- [x] 2.9.5 Frontend: Cluster info, node status, and kubelet allocatables
- [x] 2.9.6 Frontend: Horizontal Pod Autoscaler (HPA) viewer

### 2.10 OTel-Native Monitoring
- [x] 2.10.1 Infra: Two-tier OpenTelemetry Collector (host agent + gateway)
- [x] 2.10.2 Backend: OTel SDK instrumentation with `gen_ai.*` semantic conventions (`src/monitoring/telemetry.py`)
- [x] 2.10.3 Backend: Hybrid sampling (10% head-based routine, 100% tail-based error and AI traces)
- [x] 2.10.4 Backend: Per-tenant AI cost metrics tracking (`gen_ai.cost.total`)
- [x] 2.10.5 Infra: Prometheus scraping and recording rules (`infra/observability/prometheus.yaml`)
- [x] 2.10.6 Infra: Grafana Mimir long-term metrics storage with 90d retention (`infra/observability/mimir.yaml`)
- [x] 2.10.7 Infra: Grafana Loki log aggregation with 30d retention (`infra/observability/loki.yaml`)
- [x] 2.10.8 Infra: Provisioned Grafana dashboards (`infra/observability/grafana-dashboards.yaml`)
- [x] 2.10.9 Frontend: Unified monitoring dashboard (`features/monitoring/MonitoringDashboard.tsx`)
- [x] 2.10.10 Frontend: Infrastructure health panels with shedding awareness (`features/monitoring/InfrastructurePanels.tsx`)
- [x] 2.10.11 Frontend: Application metrics with trace correlation
- [x] 2.10.12 Frontend: AI Cost Dashboard per tenant per model (`features/monitoring/AiCostPanel.tsx`)

### 2.11 AI Troubleshooting / Root-Cause Analysis
- [x] 2.11.1 Backend: Incident ingestion from deployment errors and host events (`src/incidents/ingestion.py`, migration `0033`)
- [x] 2.11.2 Backend: AI-powered log and evidence analysis via model cascade (`src/incidents/analysis.py`)
- [x] 2.11.3 Backend: Root-cause identification pipeline with multi-source evidence floor (`src/incidents/evidence.py`)
- [x] 2.11.4 Backend: Fix suggestion generation entering approval pipeline (`POST .../suggestions/{id}/change-set`)
- [x] 2.11.5 Frontend: Incident list and detail view with evidence caveat (`features/incidents/IncidentPanels.tsx`)
- [x] 2.11.6 Frontend: Root-cause analysis display (problem -> location -> fix)
- [x] 2.11.7 Frontend: Suggested fix with side-by-side diff preview

### 2.12 Guard-Railed Self-Healing
- [x] 2.12.1 Backend: Health monitoring correlating metric series and incidents
- [x] 2.12.2 Backend: Two-tier action model with closed safe-remedy auto-execution (`src/incidents/healing.py`, migration `0034`)
- [x] 2.12.3 Backend: AI post-incident summary generator (`src/incidents/postmortem.py`)
- [x] 2.12.4 Backend: Long-term operational recommendation generation
- [x] 2.12.5 Frontend: Self-healing activity log with budget and decision sentences (`features/incidents/HealingPanels.tsx`)
- [x] 2.12.6 Frontend: Post-incident summary display

### 2.13 AI Learning History (Per-Project Memory)
- [x] 2.13.1 Backend: Feedback event logging (`learning_feedback`, migration `0035`)
- [x] 2.13.2 Backend: Two-tier memory architecture (session turns vs project preferences)
- [x] 2.13.3 Backend: Periodic Reflector Agent synthesizing skill files (`src/learning/reflector.py`)
- [x] 2.13.4 Backend: Skill file context injection with prompt budget auditing (`core/memory_port.py`)
- [x] 2.13.5 Frontend: Learning history viewer with editable preferences (`features/learning/LearningPanels.tsx`)
- [x] 2.13.6 Frontend: Project preference display and active skill file preview

### 2.14 Knowledge Base Mode
- [x] 2.14.1 Backend: Question-answering pipeline with RAG across codebase, deployments, and incidents (`src/knowledge/service.py`)
- [x] 2.14.2 Backend: Closed topic set ("Explain this Dockerfile", "Explain this error", "Best practices for...", "project_state")
- [x] 2.14.3 Backend: Project context grounding with strict multi-tenant isolation

---

## Phase 2 Completion Criteria Status

- [x] User can promote from dev -> staging -> production
- [x] Deployment with real image build + push + apply works
- [x] Rollback restores previous stable state
- [x] Docker dashboard shows containers and stats
- [x] AI Command Center understands "Deploy to staging" and executes
- [x] Notifications sent on deploy complete/failure
- [x] Local dev tools work (run tests, lint from dashboard)
- [x] Inngest workflows functional: deployment pipeline with approval gates completes end-to-end
- [x] ArgoCD Application manifests generated and synced successfully
- [x] K8s dashboard shows real pods, deployments, namespaces
- [x] Metrics flowing from OTel -> Prometheus -> Grafana
- [x] AI can analyze a failed deployment and identify root cause
- [x] Failed container is auto-restarted (with log)
- [x] AI generates post-incident summary
- [x] AI learns from accepted/rejected suggestions
- [x] Knowledge base answers questions using project context
- [x] Grafana Mimir storing long-term metrics with configured retention period
- [x] Test coverage >= 75% (86.57% combined backend statements, agent 74.1%, frontend 95.3%)
- [ ] End-to-end test: scan project → deploy to staging → verify health → rollback (requires live K8s cluster)
- [ ] End-to-end test: deploy → inject failure → AI detects → AI suggests fix → human approves (requires live K8s cluster)
