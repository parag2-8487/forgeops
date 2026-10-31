# `features/` — Per-Feature UI Modules

Required by PRD §8 layout. This directory contains the modularized domain feature components powering the ForgeOps web application.

## Overview & State of Feature Modules

Every module connects directly to authenticated or public backend endpoints via typed TanStack Query hooks, Zustand stores, or real-time Server-Sent Events (SSE) readers.

| Module Directory | Key Components                                             | Purpose & State                                                                                                                                                                                    |
| :--------------- | :--------------------------------------------------------- | :------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `projects/`      | `ProjectList.tsx`, `ProjectCreateForm.tsx`                 | Repository workspace creation, tag filtering, favorites, archive/deletion, and codebase indexing status. Fully mounted at `/projects`.                                                             |
| `deployments/`   | `AutonomousDeployForm.tsx`, `JenkinsPipelineDashboard.tsx` | Multi-target autonomous deployment launcher, browser port validator, live SSE log streamer, G1–G7 gate cards, and G8 sweeper panel. Fully mounted under `/projects/[projectId]/autonomous-deploy`. |
| `readiness/`     | `RadarChart.tsx`, `ReadinessDetail.tsx`                    | 6-pillar weighted readiness scoring (containerization, CI/CD, orchestration, env config, security, IaC). Fully mounted at `/readiness`.                                                            |
| `approvals/`     | `ApprovalCenter.tsx`                                       | Authenticated review center for pending change sets and diffs with two-phase commit verification. Fully mounted at `/approvals`.                                                                   |
| `policies/`      | `PolicyEditor.tsx`                                         | Open Policy Agent (OPA) and Cerbos policy inspection and verification. Fully mounted at `/policies`.                                                                                               |
| `generation/`    | `GeneratorWizard.tsx`                                      | Blueprint generation wizard with LLM context grounding and template fallback compilation. Fully mounted at `/generation`.                                                                          |
| `audit/`         | `AuditViewer.tsx`                                          | Tamper-evident cryptographic audit stream with SHA-256 hash chaining. Fully mounted at `/audit`.                                                                                                   |
| `vault/`         | `SecretVault.tsx`                                          | Scoped secret references with write-only masking and zero plaintext leakage. Fully mounted at `/vault`.                                                                                            |
| `models/`        | `ModelTiers.tsx`                                           | Model registry, failover tiers (`qwen3-coder-next`, `glm-5.2`), latency observation, and circuit breaker status. Fully mounted at `/models`.                                                       |
| `agent/`         | `AgentPairing.tsx`                                         | Outbound WebSocket agent pairing codes, certificates, and heartbeat verification. Fully mounted at `/pairing`.                                                                                     |
| `monitoring/`    | `MetricsDashboard.tsx`                                     | Real-time Prometheus/OpenTelemetry metrics and span collector telemetry. Fully mounted at `/monitoring`.                                                                                           |
| `incidents/`     | `IncidentResolver.tsx`                                     | Self-healing incident diagnosis and bounded AI remediation logs. Fully mounted at `/incidents`.                                                                                                    |
| `integrations/`  | `IntegrationSettings.tsx`                                  | User GitHub PAT and Vercel credentials configuration. Fully mounted at `/settings/integrations`.                                                                                                   |
| `environments/`  | `EnvironmentList.tsx`                                      | Environment variables, promotion pipelines, and deployment targets. Fully mounted at `/environments`.                                                                                              |
| `hostops/`       | `HostInventory.tsx`, `DockerContainerList.tsx`             | Host Docker containers and Kubernetes pod observability through the agent.                                                                                                                         |

All features adhere to the zero-fabrication principle: every screen consumes real API responses from the FastAPI control plane and explicitly renders unknowable facts rather than inventing plausible mock data.
