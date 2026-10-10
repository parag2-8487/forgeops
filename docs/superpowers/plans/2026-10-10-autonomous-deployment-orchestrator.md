# Autonomous Deployment Orchestrator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan one task at a time. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement a production-ready, fully integrated Autonomous Deployment feature in ForgeOps with strategy-aware orchestration (Docker, GitHub, Vercel), canonical G1–G7 verification gates, leased worker fencing, transactional outbox streaming, and a Jenkins Blue Ocean-style live pipeline UI.

**Architecture:** Unified backend orchestration state machine in PostgreSQL with leased worker fencing, durable task dispatch (`TaskDispatcher`), transactional outbox, and WebSocket/SSE streaming. Frontend features a dedicated full-page pipeline route (`/projects/[projectId]/autonomous-deploy/[runId]`) with interactive horizontal node graph and virtualized console.

**Tech Stack:** FastAPI, SQLAlchemy 2.0 (asyncio), PostgreSQL (Alembic), Redis (pub/sub), Next.js 14 (App Router), React Query, Tailwind CSS, WebSockets / SSE.

## Global Constraints

- Python backend strictly typed with mypy and linted with ruff.
- Zero emojis across all user interfaces, logs, and API payloads.
- All file references in documentation and code comments formatted as clickable markdown links with `file:///` and forward slashes.
- No terminal mutation on retry: all retries create new immutable attempts chained via `parent_run_id` and `attempt_number`.
- Pre-commit secret scanning: avoid literal credential shapes in source and test strings; assemble synthetic tokens from fragments (see `backend/tests/synthetic_secrets.py`).

---

## Existing Verified Capabilities vs. Proposed Changes

### Existing Verified Capabilities (Do Not Re-implement)

- **Token Authentication & Tenancy:** `require_principal` from [`backend/src/auth/dependencies.py`](file:///C:/IMP/antigravity-cli/Major%20Project/Devops%20Automation/backend/src/auth/dependencies.py) enforces caller identity, tenant boundaries, and project access.
- **Agent Pairing & Heartbeat:** `DeviceService.active_device_for` from [`backend/src/auth/devices.py`](file:///C:/IMP/antigravity-cli/Major%20Project/Devops%20Automation/backend/src/auth/devices.py) locates active devices and inspects `last_seen`.
- **Durable Task Seam:** `TaskDispatcher` protocol in [`backend/src/core/tasks.py`](file:///C:/IMP/antigravity-cli/Major%20Project/Devops%20Automation/backend/src/core/tasks.py) provides engine-neutral task queue dispatch.
- **SSE Frame Formatting:** Canonical vocabulary `SSEEventType` and `format_event` in [`backend/src/core/sse.py`](file:///C:/IMP/antigravity-cli/Major%20Project/Devops%20Automation/backend/src/core/sse.py).
- **Secret Redaction Filter:** `redact_secrets` in [`backend/src/core/logging.py`](file:///C:/IMP/antigravity-cli/Major%20Project/Devops%20Automation/backend/src/core/logging.py) scrubs credentials from strings and log records.
- **Cloud Deployment Integrations:** `GitHubClient` and `VercelClient` integration logic in `backend/src/integrations/`.

### Proposed Changes

- **Database Schema:** Migration `0038_autonomous_deployments.py` creating `autonomous_deployments`, `autonomous_deployment_stages`, `autonomous_deployment_logs`, and `autonomous_deployment_outbox`.
- **Orchestration Worker:** Background worker with atomic lease claim, epoch fencing tokens, and heartbeat renewals.
- **Canonical Gate Pipeline (G1–G7):** Strategy-aware execution mapping with distinct, inspectable gate verdicts.
- **Transactional Outbox & Streaming Relay:** Guaranteed monotonic event delivery with subscribe-before-replay protocol.
- **Frontend Dashboard:** Dedicated route `/projects/[projectId]/autonomous-deploy/[runId]` featuring Jenkins Blue Ocean-style graph, virtualized console with 5,000-line cap notice, and `useAutonomousDeployStream` hook.

---

## Implementation Tasks

### Phase 1: Database Migration & Schema Models

#### Task 1.1: Database Migration Script

**Files:**

- Create: `backend/alembic/versions/0038_autonomous_deployments.py`
- Test: `backend/tests/test_migration_0038.py`

**Interfaces:**

- Produces: PostgreSQL tables `autonomous_deployments`, `autonomous_deployment_stages`, `autonomous_deployment_logs`, `autonomous_deployment_outbox`.

- [ ] **Step 1: Write the failing migration test**

```python
# backend/tests/test_migration_0038.py
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

@pytest.mark.asyncio
async def test_migration_0038_tables_exist(async_session: AsyncSession) -> None:
    for table_name in [
        "autonomous_deployments",
        "autonomous_deployment_stages",
        "autonomous_deployment_logs",
        "autonomous_deployment_outbox",
    ]:
        result = await async_session.execute(
            text(f"SELECT to_regclass('public.{table_name}')")
        )
        assert result.scalar() is not None, f"Table {table_name} missing"
```

- [ ] **Step 2: Run test to verify it fails**
      Run: `pytest backend/tests/test_migration_0038.py -v`
      Expected: FAIL with missing table assertion.

- [ ] **Step 3: Implement Alembic migration**
      Create `backend/alembic/versions/0038_autonomous_deployments.py` declaring the four tables, indexes, check constraints (`status`, `progress_pct`, `log_level`), and foreign keys per the technical specification.

- [ ] **Step 4: Run test to verify it passes**
      Run: `pytest backend/tests/test_migration_0038.py -v`
      Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/alembic/versions/0038_autonomous_deployments.py backend/tests/test_migration_0038.py
git commit -m "feat(deployments): add 0038 migration for autonomous deployments"
```

---

#### Task 1.2: SQLAlchemy ORM Models

**Files:**

- Create: `backend/src/deployments/autonomous_models.py`
- Modify: `backend/src/deployments/models.py`
- Test: `backend/tests/test_autonomous_models.py`

**Interfaces:**

- Produces: ORM classes `AutonomousDeployment`, `AutonomousDeploymentStage`, `AutonomousDeploymentLog`, `AutonomousDeploymentOutbox`.

- [ ] **Step 1: Write model unit test**
      Test model instantiation, relationship cascades from run to stages/logs/outbox, and property getters.

- [ ] **Step 2: Run test to verify failure**
      Run: `pytest backend/tests/test_autonomous_models.py -v`

- [ ] **Step 3: Implement ORM models**
      Define models using SQLAlchemy declarative mapping with typed columns matching schema constraints.

- [ ] **Step 4: Run test to verify success**
      Run: `pytest backend/tests/test_autonomous_models.py -v`

- [ ] **Step 5: Commit**

```bash
git add backend/src/deployments/autonomous_models.py backend/src/deployments/models.py backend/tests/test_autonomous_models.py
git commit -m "feat(deployments): add autonomous deployment orm models"
```

---

### Phase 2: Public Schemas, Strategy Validation & Secret Redaction

#### Task 2.1: Schemas and Secret Redaction Extension

**Files:**

- Create: `backend/src/deployments/autonomous_schemas.py`
- Modify: `backend/src/core/logging.py`
- Test: `backend/tests/test_autonomous_schemas.py`

**Interfaces:**

- Produces: Pydantic models `CreateAutonomousRunRequest`, `AutonomousRunPublicResponse`, `StagePublicResponse`, `LogEntryPublicResponse`, `PaginatedLogsResponse`.
- Produces: `redact_all_secrets(text: str) -> str` supporting GitHub PATs and cloud tokens.

- [ ] **Step 1: Write failing schema and redaction tests**

```python
# backend/tests/test_autonomous_schemas.py
from src.deployments.autonomous_schemas import CreateAutonomousRunRequest, DeploymentStrategy
from src.core.logging import redact_secrets

def test_strategy_validation_requires_config():
    # vercel_only without vercel_config must fail validation
    with pytest.raises(ValueError):
        CreateAutonomousRunRequest(
            strategy=DeploymentStrategy.VERCEL_ONLY,
            idempotency_key="test-key-12345678",
        )

def test_secret_redaction_scrubs_synthetic_tokens():
    raw = "Header: " + "gh" + "p_testtoken1234567890abcdef"
    assert "testtoken" not in redact_secrets(raw)
    assert "[REDACTED]" in redact_secrets(raw)
```

- [ ] **Step 2: Run test to verify failure**
      Run: `pytest backend/tests/test_autonomous_schemas.py -v`

- [ ] **Step 3: Implement schemas and extend regex filters in `logging.py`**
      Implement Pydantic models with cross-field validators ensuring configuration matches strategy and reserved env vars cannot be overridden.

- [ ] **Step 4: Run test to verify pass**
      Run: `pytest backend/tests/test_autonomous_schemas.py -v`

- [ ] **Step 5: Commit**

```bash
git add backend/src/deployments/autonomous_schemas.py backend/src/core/logging.py backend/tests/test_autonomous_schemas.py
git commit -m "feat(deployments): add autonomous schemas and secret redaction rules"
```

---

### Phase 3: Autonomous Deployment Service & Idempotent Run Creation

#### Task 3.1: Service Layer for Run Creation & Snapshot

**Files:**

- Create: `backend/src/deployments/autonomous_service.py`
- Test: `backend/tests/test_autonomous_service.py`

**Interfaces:**

- Produces: `AutonomousDeploymentService.create_run(...)`, `AutonomousDeploymentService.get_run_snapshot(...)`, `AutonomousDeploymentService.retry_run(...)`.

- [ ] **Step 1: Write failing service unit tests**
      Test run creation, stage graph generation for each of the 4 strategies, idempotency key matching with same vs different payload hash, and immutable retry creation with `parent_run_id` and `attempt_number=2`.

- [ ] **Step 2: Run test to verify failure**
      Run: `pytest backend/tests/test_autonomous_service.py -v`

- [ ] **Step 3: Implement service methods**
      Implement atomic run creation with SHA-256 payload fingerprinting and deterministic stage initialization. Implement `retry_run` ensuring target run is failed/rolled_back and prior history remains unchanged.

- [ ] **Step 4: Run test to verify pass**
      Run: `pytest backend/tests/test_autonomous_service.py -v`

- [ ] **Step 5: Commit**

```bash
git add backend/src/deployments/autonomous_service.py backend/tests/test_autonomous_service.py
git commit -m "feat(deployments): implement autonomous deployment service and immutable retry"
```

---

### Phase 4: Durable Worker, Fencing Token Claim & Execution State Machine

#### Task 4.1: Worker Claim Protocol & Heartbeat Fencing

**Files:**

- Create: `backend/src/deployments/autonomous_worker.py`
- Test: `backend/tests/test_autonomous_worker_fencing.py`

**Interfaces:**

- Produces: `AutonomousWorker.claim_run(run_id, worker_id) -> int | None`, `AutonomousWorker.heartbeat(run_id, fence_token)`, `AutonomousWorker.execute_run(run_id)`.

- [ ] **Step 1: Write failing worker fencing tests**

```python
# backend/tests/test_autonomous_worker_fencing.py
@pytest.mark.asyncio
async def test_worker_claim_increments_fence_token(service, session):
    run = await service.create_run(...)
    token1 = await worker.claim_run(run.id, "worker-1")
    assert token1 == 1
    # Second worker cannot claim active lease
    token2 = await worker.claim_run(run.id, "worker-2")
    assert token2 is None
```

- [ ] **Step 2: Run test to verify failure**
      Run: `pytest backend/tests/test_autonomous_worker_fencing.py -v`

- [ ] **Step 3: Implement atomic claim and heartbeat logic**
      Implement SQL queries with atomic checks `WHERE id = :run_id AND (lease_expires_at IS NULL OR lease_expires_at < now())` and guarded updates checking `fence_token`.

- [ ] **Step 4: Run test to verify pass**
      Run: `pytest backend/tests/test_autonomous_worker_fencing.py -v`

- [ ] **Step 5: Commit**

```bash
git add backend/src/deployments/autonomous_worker.py backend/tests/test_autonomous_worker_fencing.py
git commit -m "feat(deployments): implement atomic worker lease claim and fencing"
```

---

### Stage 5: Canonical Gate Evaluators & Strategy Execution Graph

#### Task 5.1: G1–G7 Gate Execution Pipeline

**Files:**

- Create: `backend/src/deployments/autonomous_gates.py`
- Modify: `backend/src/deployments/autonomous_worker.py`
- Test: `backend/tests/test_autonomous_gates.py`

**Interfaces:**

- Produces: Gate evaluators for G1 (blueprint), G2 (existing artifacts), G3 (consistency), G4 (build), G5 (apply), G6 (workload), G7 (final strategy-aware verification).

- [ ] **Step 1: Write failing gate pipeline tests**
      Test sequential execution of G1–G7, failure handling at each gate, strategy-aware omission of G4–G6 when Docker is not in strategy, and strategy-aware G7 evaluation checking only active targets.

- [ ] **Step 2: Run test to verify failure**
      Run: `pytest backend/tests/test_autonomous_gates.py -v`

- [ ] **Step 3: Implement gate verification logic**
      Integrate with ForgeOps Docker client, GitHub release helper, and Vercel client. Record gate status and metadata in `autonomous_deployment_stages`.

- [ ] **Step 4: Run test to verify pass**
      Run: `pytest backend/tests/test_autonomous_gates.py -v`

- [ ] **Step 5: Commit**

```bash
git add backend/src/deployments/autonomous_gates.py backend/src/deployments/autonomous_worker.py backend/tests/test_autonomous_gates.py
git commit -m "feat(deployments): implement canonical G1-G7 gate pipeline"
```

---

### Stage 6: Cancellation Settlement, Compensation Rollback & Recovery Sweeper

#### Task 6.1: Cancellation, Rollback and Recovery

**Files:**

- Modify: `backend/src/deployments/autonomous_worker.py`
- Create: `backend/src/deployments/autonomous_recovery.py`
- Test: `backend/tests/test_autonomous_recovery.py`

**Interfaces:**

- Produces: Cooperative cancellation with process kill settlement, compensation rollback handler, and recovery sweeper for expired leases.

- [ ] **Step 1: Write failing cancellation and recovery tests**
      Test cancelling active Docker build process, verifying container cleanup, and recovering orphaned runs after lease expiration.

- [ ] **Step 2: Run test to verify failure**
      Run: `pytest backend/tests/test_autonomous_recovery.py -v`

- [ ] **Step 3: Implement cancellation settlement and recovery reconciler**
      Handle process signals, update stage and run status to `cancelled`, and reconcile external resources (commit SHAs, container IDs) during crash recovery.

- [ ] **Step 4: Run test to verify pass**
      Run: `pytest backend/tests/test_autonomous_recovery.py -v`

- [ ] **Step 5: Commit**

```bash
git add backend/src/deployments/autonomous_worker.py backend/src/deployments/autonomous_recovery.py backend/tests/test_autonomous_recovery.py
git commit -m "feat(deployments): implement cancellation settlement and crash recovery sweeper"
```

---

### Stage 7: Transactional Outbox, Streaming Bridge & Log Pagination

#### Task 7.1: Outbox Dispatcher, Redis Relay & REST/WebSocket Routes

**Files:**

- Create: `backend/src/deployments/autonomous_outbox.py`
- Create: `backend/src/deployments/autonomous_routes.py`
- Modify: `backend/src/main.py`
- Test: `backend/tests/test_autonomous_api_and_streaming.py`

**Interfaces:**

- Produces: REST endpoints (`POST /autonomous-deploy`, `GET /{run_id}`, `POST /{run_id}/start`, `POST /{run_id}/cancel`, `POST /{run_id}/retry`, `GET /{run_id}/logs`).
- Produces: WebSocket `/ws` and SSE `/events` endpoints with cursor replay protocol.

- [ ] **Step 1: Write failing API and streaming tests**
      Test all REST status codes, idempotency 200 vs 201 vs 409, log cursor pagination, outbox drainer to Redis Pub/Sub, and replay with client high-water mark.

- [ ] **Step 2: Run test to verify failure**
      Run: `pytest backend/tests/test_autonomous_api_and_streaming.py -v`

- [ ] **Step 3: Implement routes and outbox streamer**
      Mount router in `main.py`. Implement outbox drainer claiming pending events and publishing to Redis. Implement WebSocket/SSE handlers subscribing to Redis and replaying missed events from PostgreSQL.

- [ ] **Step 4: Run test to verify pass**
      Run: `pytest backend/tests/test_autonomous_api_and_streaming.py -v`

- [ ] **Step 5: Commit**

```bash
git add backend/src/deployments/autonomous_outbox.py backend/src/deployments/autonomous_routes.py backend/src/main.py backend/tests/test_autonomous_api_and_streaming.py
git commit -m "feat(deployments): add autonomous deployment api and outbox streaming"
```

---

### Stage 8: Frontend Launch Modal & Project Page Integration

#### Task 8.1: Autonomous Deploy Modal & Action Button

**Files:**

- Create: `frontend/features/deployments/AutonomousDeployModal.tsx`
- Modify: `frontend/app/(shell)/projects/[projectId]/page.tsx`
- Test: `frontend/__tests__/AutonomousDeployModal.test.tsx`

**Interfaces:**

- Produces: Modal with 4 strategy cards, context-aware config sections, live agent pairing check, and `Create Run & Open Pipeline` action.

- [ ] **Step 1: Write failing frontend modal test**
      Test strategy selection, dynamic field rendering, agent pairing indicator, submit disabling during loading, and navigation to pipeline route on `201` or `200`.

- [ ] **Step 2: Run test to verify failure**
      Run: `npm test frontend/__tests__/AutonomousDeployModal.test.tsx`

- [ ] **Step 3: Implement modal component**
      Build component with React Hook Form or controlled inputs, integrate with `api.post`, and embed in project detail header.

- [ ] **Step 4: Run test to verify pass**
      Run: `npm test frontend/__tests__/AutonomousDeployModal.test.tsx`

- [ ] **Step 5: Commit**

```bash
git add frontend/features/deployments/AutonomousDeployModal.tsx frontend/app/(shell)/projects/[projectId]/page.tsx frontend/__tests__/AutonomousDeployModal.test.tsx
git commit -m "feat(frontend): add autonomous deploy launch modal and project page trigger"
```

---

### Stage 9: Dedicated Pipeline Route & Blue Ocean Graph Component

#### Task 9.1: Dedicated Route & Pipeline Graph

**Files:**

- Create: `frontend/app/(shell)/projects/[projectId]/autonomous-deploy/[runId]/page.tsx`
- Create: `frontend/features/deployments/JenkinsPipelineDashboard.tsx`
- Test: `frontend/__tests__/JenkinsPipelineDashboard.test.tsx`

**Interfaces:**

- Produces: Full-page layout, summary header bar, Start/Cancel/Retry buttons, and horizontal Blue Ocean node graph rendering distinct G1–G7 gates and operational stages.

- [ ] **Step 1: Write failing dashboard component tests**
      Test graph rendering from backend stages, omission of inapplicable stages, status color-coding, Start button disabled without agent, and Start action calling `POST .../start`.

- [ ] **Step 2: Run test to verify failure**
      Run: `npm test frontend/__tests__/JenkinsPipelineDashboard.test.tsx`

- [ ] **Step 3: Implement dashboard and route**
      Construct accessible SVG/HTML node graph with responsive horizontal scrolling, breadcrumbs, status badges, and action controls.

- [ ] **Step 4: Run test to verify pass**
      Run: `npm test frontend/__tests__/JenkinsPipelineDashboard.test.tsx`

- [ ] **Step 5: Commit**

```bash
git add frontend/app/(shell)/projects/[projectId]/autonomous-deploy/[runId]/page.tsx frontend/features/deployments/JenkinsPipelineDashboard.tsx frontend/__tests__/JenkinsPipelineDashboard.test.tsx
git commit -m "feat(frontend): add dedicated pipeline route and jenkins blue ocean graph"
```

---

### Stage 10: Real-time Terminal & Streaming Hook

#### Task 10.1: Virtualized Console & Replay Streaming Hook

**Files:**

- Create: `frontend/features/deployments/useAutonomousDeployStream.ts`
- Create: `frontend/features/deployments/AutonomousLogConsole.tsx`
- Test: `frontend/__tests__/useAutonomousDeployStream.test.ts`

**Interfaces:**

- Produces: `useAutonomousDeployStream` hook with WebSocket/SSE fallback, event deduplication, and REST reconciliation.
- Produces: `AutonomousLogConsole` virtualized console with auto-scroll lock, search filter, and 5,000-line truncation notice.

- [ ] **Step 1: Write failing hook and console tests**
      Test gap-free event ingestion, duplicate event rejection, 5,000-line truncation banner display, and copy logs action.

- [ ] **Step 2: Run test to verify failure**
      Run: `npm test frontend/__tests__/useAutonomousDeployStream.test.ts`

- [ ] **Step 3: Implement streaming hook and virtualized console**
      Implement WebSocket client with high-water mark replay, link with React Query cache, and wire into the pipeline page.

- [ ] **Step 4: Run test to verify pass**
      Run: `npm test frontend/__tests__/useAutonomousDeployStream.test.ts`

- [ ] **Step 5: Commit**

```bash
git add frontend/features/deployments/useAutonomousDeployStream.ts frontend/features/deployments/AutonomousLogConsole.tsx frontend/__tests__/useAutonomousDeployStream.test.ts
git commit -m "feat(frontend): implement virtualized log terminal and gap-free streaming hook"
```

---

### Stage 11: End-to-End Integration Test Suite & Verification Matrix

#### Task 11.1: E2E Integration Test Suite

**Files:**

- Create: `backend/tests/test_autonomous_e2e.py`
- Test: `backend/tests/test_autonomous_e2e.py`

**Interfaces:**

- Produces: Comprehensive automated verification across all 4 strategies, crash recovery, cancellation settlement, and stream reconnection.

- [ ] **Step 1: Write comprehensive end-to-end test scenarios**

```python
# backend/tests/test_autonomous_e2e.py
@pytest.mark.asyncio
async def test_e2e_docker_github_vercel_run(...) -> None:
    # Full lifecycle: create -> start -> gates G1-G7 -> succeeded -> authoritative snapshot

@pytest.mark.asyncio
async def test_e2e_cancellation_settlement(...) -> None:
    # Cancel running pipeline, assert processes terminated, status settled to cancelled

@pytest.mark.asyncio
async def test_e2e_stale_worker_rejection(...) -> None:
    # Worker loses lease; second worker claims; first worker write fails
```

- [ ] **Step 2: Run test to verify failure**
      Run: `pytest backend/tests/test_autonomous_e2e.py -v`

- [ ] **Step 3: Refine orchestrator coordination to ensure all E2E tests pass cleanly**

- [ ] **Step 4: Run complete backend and frontend test suite to verify full green pass**
      Run: `pytest backend/tests/test_autonomous*.py -v` and `npm test`

- [ ] **Step 5: Commit**

```bash
git add backend/tests/test_autonomous_e2e.py
git commit -m "test(deployments): add comprehensive e2e test suite for autonomous orchestrator"
```

---

## Acceptance Criteria

1. All four deployment strategies execute deterministically with appropriate stage graph omission.
2. Canonical gates G1–G7 are individually inspectable with distinct verdicts in both backend and frontend.
3. Creation (`POST /`) and execution (`POST /start`) remain strictly separated; duplicate submissions return idempotent responses without double execution.
4. Worker fencing prevents split-brain and stale worker writes; heartbeat renewals do not increment fencing tokens.
5. Outbox streaming guarantees monotonic event sequencing with zero lost frames during reconnects.
6. Secrets and authorization headers are scrubbed before persistence and streaming.
7. Retries generate new immutable attempts, leaving historical logs and gate outcomes intact.
8. Pipeline page restores authoritative state upon refresh, direct navigation, or tab reconnect.
