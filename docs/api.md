# ForgeOps API â€” Phase 0

> **Stale as of 2026-08-21, and kept rather than deleted so the gap stays visible.** This
> document describes the **Phase 0** API surface. Phase 1 has since shipped and this file was not
> updated with it, so it is wrong in two ways that matter.
>
> It states there is **no login flow and no general user authentication**. There is: Phase 1 added
> the OIDC flow at `/api/v1/auth/login`, `/callback`, `/refresh` and `/logout`, and
> `require_principal` now guards every route not listed in the `PUBLIC_ROUTES` table.
>
> It also **documents none of the Phase 1 surfaces**. `projects`, `policies`, `audit` and
> `approvals` are not mentioned once, and twelve routers are registered in `create_app`. Until this
> file is rewritten, the accurate and self-updating source is the generated schema â€” Swagger UI at
> <http://localhost:8000/api/v1/docs> and `openapi.json` beside it â€” because it is produced from the
> routers themselves and cannot drift from them.

Authority: `.antigravity/specs/phase-0-foundation/design.md` Â§4.2, Â§4.3, Â§4.4, Â§11, Â§14.2, Â§15.2.
Only the surfaces listed here exist in Phase 0.

## Versioning

Public API routes are versioned in the URL under `/api/v1`. The OpenAPI document is served
at `/api/v1/openapi.json`. Probe endpoints are deliberately **unversioned**: they are an
infrastructure contract for container orchestrators, not part of the public API, so they do
not move when the API version bumps.

## Authentication

Phase 0 has **no general user authentication**. There is no login flow, no session or
refresh-token lifecycle, no user records, and no RBAC; those arrive in Phase 1 Â§1.11.

Two surfaces verify an OAuth 2.1/OIDC bearer token:

- `/api/v1/mcp*` â€” the MCP Gateway. Verification runs before routing.
- `POST /api/v1/ai/complete` â€” verification supplies the `sub` that keys the per-caller
  rate-limit bucket.

Verification enforces the JWT signature against JWKS fetched from the token issuer, an
`iss` value inside an explicit allowlist (required to be non-empty when
`APP_ENV=production`), the required `aud`, and `exp`/`nbf`/`iat`. Failures return `401`
problem documents. Every other route is unauthenticated in Phase 0, which is why the
Phase 0 topology is local-development-only â€” see `docs/deployment.md`.

## Error contract â€” RFC 9457

Every non-2xx response carries `Content-Type: application/problem+json` and this shape:

```jsonc
{
  "type": "https://errors.forgeops.dev/validation-failed",
  "title": "Request validation failed",
  "status": 422,
  "detail": "Field 'tier' is not a recognised model tier.",
  "instance": "/api/v1/ai/complete",
  "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
  "errors": [{ "pointer": "#/tier", "detail": "unknown tier 'ultra'" }],
}
```

Rules:

- `type` is a stable project-owned URI and is never resolved at runtime.
- `status` in the body always equals the HTTP status code.
- `detail` never contains secrets, bearer tokens, connection strings, keys, PEM material,
  or stack traces.
- Unhandled exceptions map to a generic `internal` problem plus the `trace_id` for
  correlation.

Frontend clients normalise every non-2xx into `ApiProblemError` (or its transport
subclass), never a raw parsing exception, and preserve the real HTTP status when a body is
not RFC 9457 conforming.

## Health and readiness

| Route                | Purpose                 | Dependency I/O                                         | Success                                                                                | Failure                                                           |
| :------------------- | :---------------------- | :----------------------------------------------------- | :------------------------------------------------------------------------------------- | :---------------------------------------------------------------- |
| `GET /health`        | Liveness                | none                                                   | `200 {"status":"ok","version":"â€¦","commit":"â€¦"}`, including during a dependency outage | only when the process is dead or wedged                           |
| `GET /health/ready`  | Readiness               | PostgreSQL `SELECT 1` + Redis `PING`, 2 s timeout each | `200 {"status":"ready","checks":{"postgres":"ok","redis":"ok"}}`                       | RFC 9457 `503`, one `errors[]` item per failed or timed-out check |
| `GET /api/v1/health` | Versioned liveness echo | none                                                   | `200`                                                                                  | process-level failure only                                        |

`/health` is the container liveness check. `/health/ready` is the gate polled by
`scripts/dev-up.sh` after startup.

## MCP Gateway

| Route                     | Method | Notes                                                                                                                                                 |
| :------------------------ | :----- | :---------------------------------------------------------------------------------------------------------------------------------------------------- |
| `/api/v1/mcp`             | `POST` | Stateless gateway entry. Routing comes from the `Mcp-Method` and `Mcp-Name` headers only, never the body, and only after bearer verification succeeds |
| `/api/v1/mcp/servers`     | `GET`  | Registered MCP servers, OPA-filtered                                                                                                                  |
| `/api/v1/mcp/apps/{name}` | `GET`  | MCP Apps descriptor `{name, title, entry_url, capabilities, csp}`                                                                                     |

`tools/list` order: verify bearer/OIDC â†’ route from headers â†’ Redis TTL cache or upstream
list â†’ OPA filter on every response (cache hit or miss) â†’ return. Only the unfiltered
upstream list is cached. A Redis failure is treated as a cache miss; an OPA failure returns
an empty allowed set.

`tools/call` order: verify bearer/OIDC â†’ route from headers â†’ parse the called tool â†’
resolve tool metadata locally or from an already-valid cache entry with no upstream I/O â†’
OPA authorise â†’ invoke upstream only on allow. Invalid bearer, malformed call, unresolved
metadata, unknown tool, policy denial, and policy error all return before any upstream
operation.

Status codes: `400` for missing routing headers, `401` for token failures, `403` for policy
denial, `404` for an unknown server or task, `504` for an upstream timeout â€” all as RFC 9457
problems.

### Tasks Extension

Task states are `submitted`, `working`, `input_required`, `completed`, `failed`,
`cancelled`. Terminal states absorb further transitions, and `tasks/cancel` on a terminal
task returns that state with `200` â€” cancellation is idempotent. Records live in Redis so
any replica can serve `tasks/get`; concurrent updates use compare-and-set so only one
writer wins.

### MCP Apps hosting

The host page sets
`Content-Security-Policy: default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; frame-ancestors 'self'`
and the iframe carries `sandbox="allow-scripts allow-forms"` without `allow-same-origin`.
The parentâ†”app channel is `postMessage` with the envelope `{v: 1, type, requestId, payload}`
and the parent drops messages whose origin does not match the descriptor origin. Phase 0
ships one descriptor, for the agent's `agent.health` tool.

## Model routing

| Route                 | Method | Notes                                                                                                                    |
| :-------------------- | :----- | :----------------------------------------------------------------------------------------------------------------------- |
| `/api/v1/ai/tiers`    | `GET`  | The six tiers with protocol, availability reason, and circuit-breaker state                                              |
| `/api/v1/ai/complete` | `POST` | Fixed order: OIDC verify â†’ require `claims.sub` â†’ Redis token-bucket limiter â†’ semantic cache â†’ registry/router/provider |

Before admission completes, no semantic-cache or provider operation runs. Invalid bearer
returns `401`; a Redis or limiter script failure fails closed with `503`; an exhausted
bucket returns `429` with an integer `Retry-After` header. Cascade exhaustion after
admission is an ordinary `200` routing outcome reporting `EXHAUSTED`, not an error.

## Plan analysis

| Route                   | Method | Notes                                                                                                                                                                                                            |
| :---------------------- | :----- | :--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `/api/v1/analysis/plan` | `POST` | Deterministic analysis of an OpenTofu plan JSON document: findings, blast radius, verdict (`allow`/`warn`/`block`), and the approval decision (`AUTO_OK`/`REQUIRES_APPROVAL`/`BLOCKED`). No LLM call is involved |

Malformed plan documents return RFC 9457 `422` with JSON-pointer field detail.

## Streaming

Server-to-browser streaming uses SSE through FastAPI's native support with a fixed
six-value event vocabulary; `sse-starlette` is not a dependency. The agentâ†”backend protocol
is JSON-RPC 2.0 over WSS, outbound-only, and in Phase 0 only the transport mechanics exist.

<!-- BEGIN GENERATED ENDPOINTS -->

<!-- Generated by scripts/dump-openapi.py from the live app. Do not edit by hand:
     `python scripts/dump-openapi.py --check` fails the build when this drifts. -->

The application publishes **163 operations across 139 paths**.

### `agents`

| Method | Path | Summary | Auth |
| --- | --- | --- | --- |
| `GET` | `/api/v1/agents/connection-info` | What an agent needs in order to connect to this backend | principal |
| `GET` | `/api/v1/agents/devices` | List paired agent devices | principal |
| `GET` | `/api/v1/agents/devices/{device_id}` | Read one agent device | principal |
| `POST` | `/api/v1/agents/pair/exchange` | Exchange a pairing code for device credentials (public) | public |
| `POST` | `/api/v1/agents/pairing-codes` | Issue a single-use pairing code for a project | principal |
| `POST` | `/api/v1/agents/self/abandon` | Surrender the calling device (agent, authenticated by its own device token) | public |
| `DELETE` | `/api/v1/agents/{device_id}` | Revoke a device | principal |

### `ai`

| Method | Path | Summary | Auth |
| --- | --- | --- | --- |
| `POST` | `/api/v1/ai/complete` | Complete | principal |
| `GET` | `/api/v1/ai/credentials` | List Provider Credentials | principal |
| `PUT` | `/api/v1/ai/credentials/{key_ref}` | Set Provider Credential | principal |
| `DELETE` | `/api/v1/ai/credentials/{key_ref}` | Delete Provider Credential | principal |
| `GET` | `/api/v1/ai/endpoints/custom` | List Custom Endpoints | principal |
| `PUT` | `/api/v1/ai/endpoints/custom/{endpoint_id}` | Upsert Custom Endpoint | principal |
| `DELETE` | `/api/v1/ai/endpoints/custom/{endpoint_id}` | Delete Custom Endpoint | principal |
| `POST` | `/api/v1/ai/endpoints/probe` | Probe Arbitrary Endpoint | principal |
| `POST` | `/api/v1/ai/endpoints/{endpoint_id}/test` | Test Endpoint Connection | principal |
| `GET` | `/api/v1/ai/tiers` | List Tiers | principal |

### `analysis`

| Method | Path | Summary | Auth |
| --- | --- | --- | --- |
| `GET` | `/api/v1/analysis/codebase/{project_id}/chunks/{chunk_id}` | Get Chunk Details | principal |
| `POST` | `/api/v1/analysis/codebase/{project_id}/index` | Persist an agent scan report into the codebase index | principal |
| `GET` | `/api/v1/analysis/codebase/{project_id}/secrets` | Secret Scan Summary | principal |
| `GET` | `/api/v1/analysis/codebase/{project_id}/status` | Get Codebase Status | principal |
| `GET` | `/api/v1/analysis/codebase/{project_id}/symbols` | Query Symbols | principal |
| `POST` | `/api/v1/analysis/plan` | Analyse Plan | principal |

### `approvals`

| Method | Path | Summary | Auth |
| --- | --- | --- | --- |
| `GET` | `/api/v1/approvals` | List change sets awaiting or past decision | principal |
| `GET` | `/api/v1/approvals/{change_set_id}` | Read one change set and its diff | principal |
| `POST` | `/api/v1/approvals/{change_set_id}/approve` | Approve a pending change set | principal |
| `POST` | `/api/v1/approvals/{change_set_id}/deliver` | Deliver an approved change set to the agent | principal |
| `POST` | `/api/v1/approvals/{change_set_id}/reject` | Reject a pending change set | principal |
| `POST` | `/api/v1/approvals/{change_set_id}/revert` | Revert an applied change set | principal |

### `argocd`

| Method | Path | Summary | Auth |
| --- | --- | --- | --- |
| `POST` | `/api/v1/argocd/webhook` | Record a repository change (does NOT sync) | public |
| `POST` | `/api/v1/projects/{project_id}/argocd/manifests` | Render an ArgoCD or Rollouts manifest | principal |
| `POST` | `/api/v1/projects/{project_id}/argocd/sync` | Sync one ArgoCD Application | principal |

### `audit`

| Method | Path | Summary | Auth |
| --- | --- | --- | --- |
| `GET` | `/api/v1/audit/events` | Query audit records | principal |
| `GET` | `/api/v1/audit/verify` | Verify the audit hash chain | principal |

### `auth`

| Method | Path | Summary | Auth |
| --- | --- | --- | --- |
| `GET` | `/api/v1/auth/callback` | Callback | public |
| `POST` | `/api/v1/auth/callback` | Callback | public |
| `GET` | `/api/v1/auth/login` | Login | public |
| `POST` | `/api/v1/auth/login` | Login | public |
| `GET` | `/api/v1/auth/logout` | Logout | public |
| `POST` | `/api/v1/auth/logout` | Logout | public |
| `POST` | `/api/v1/auth/refresh` | Refresh | public |

### `autonomous-deploy`

| Method | Path | Summary | Auth |
| --- | --- | --- | --- |
| `POST` | `/api/v1/projects/{project_id}/autonomous-deploy` | Create or retrieve an autonomous deployment run (idempotent) | principal |
| `GET` | `/api/v1/projects/{project_id}/autonomous-deploy/{run_id}` | Get authoritative run snapshot and stages | principal |
| `POST` | `/api/v1/projects/{project_id}/autonomous-deploy/{run_id}/cancel` | Request cooperative cancellation of an autonomous deployment run | principal |
| `GET` | `/api/v1/projects/{project_id}/autonomous-deploy/{run_id}/events` | Real-time Server-Sent Events (SSE) streaming with outbox replay and live Redis relay | principal |
| `GET` | `/api/v1/projects/{project_id}/autonomous-deploy/{run_id}/logs` | Cursor-paginated log retrieval strictly ordered by monotonic log_seq ASC | principal |
| `POST` | `/api/v1/projects/{project_id}/autonomous-deploy/{run_id}/retry` | Create a new immutable attempt for a failed or rolled-back run | principal |
| `POST` | `/api/v1/projects/{project_id}/autonomous-deploy/{run_id}/start` | Initiate execution of a pending autonomous deployment run | principal |

### `commands`

| Method | Path | Summary | Auth |
| --- | --- | --- | --- |
| `GET` | `/api/v1/commands/catalogue` | Read Catalogue | principal |
| `POST` | `/api/v1/commands/execute` | Execute | principal |
| `GET` | `/api/v1/commands/history` | History | principal |
| `POST` | `/api/v1/commands/interpret` | Interpret | principal |

### `deployments`

| Method | Path | Summary | Auth |
| --- | --- | --- | --- |
| `GET` | `/api/v1/projects/{project_id}/deployments` | List Deployments | principal |
| `POST` | `/api/v1/projects/{project_id}/deployments` | Request Deployment | principal |
| `GET` | `/api/v1/projects/{project_id}/deployments/detected-manifests` | Get Detected Manifests | principal |
| `GET` | `/api/v1/projects/{project_id}/deployments/rollback-target` | Rollback Target | principal |
| `GET` | `/api/v1/projects/{project_id}/deployments/{deployment_id}` | Read Deployment | principal |
| `GET` | `/api/v1/projects/{project_id}/deployments/{deployment_id}/logs` | Live deployment output as SSE `log` events | principal |
| `POST` | `/projects/{project_id}/pipelines/deployment` | Start the durable deployment pipeline | principal |
| `POST` | `/projects/{project_id}/pipelines/deployment/release` | Release a gated pipeline step (does not replace the environment's own approval) | principal |

### `environments`

| Method | Path | Summary | Auth |
| --- | --- | --- | --- |
| `GET` | `/api/v1/projects/{project_id}/environments` | List Environments | principal |
| `POST` | `/api/v1/projects/{project_id}/environments` | Create Environment | principal |
| `PATCH` | `/api/v1/projects/{project_id}/environments/{environment_id}` | Update Environment | principal |
| `DELETE` | `/api/v1/projects/{project_id}/environments/{environment_id}` | Delete Environment | principal |
| `GET` | `/api/v1/projects/{project_id}/environments/{environment_id}/promotion` | Promotion | principal |
| `GET` | `/api/v1/projects/{project_id}/environments/{environment_id}/variables` | List Variables | principal |
| `PUT` | `/api/v1/projects/{project_id}/environments/{environment_id}/variables` | Set Variable | principal |

### `generation`

| Method | Path | Summary | Auth |
| --- | --- | --- | --- |
| `POST` | `/api/v1/generation/runs` | Generate deployment artifacts, streaming progress as SSE | principal |
| `GET` | `/api/v1/generation/runs/{run_id}` | Read one generation run and the prompt it was given | principal |

### `host-operations`

| Method | Path | Summary | Auth |
| --- | --- | --- | --- |
| `POST` | `/api/v1/projects/{project_id}/devtools/run` | Run the project's own tests, linters, build, compose stack or migrations | principal |
| `POST` | `/api/v1/projects/{project_id}/docker/containers/actions` | Start, stop, restart or remove one container (through the chokepoint) | principal |
| `GET` | `/api/v1/projects/{project_id}/docker/containers/{container}/logs` | One container's recent output | principal |
| `POST` | `/api/v1/projects/{project_id}/docker/images/actions` | Build, push, pull or remove one image (through the chokepoint) | principal |
| `GET` | `/api/v1/projects/{project_id}/docker/inventory` | Containers, images, volumes and networks on the agent's host | principal |
| `GET` | `/api/v1/projects/{project_id}/kubernetes/inventory` | Namespaces, nodes, pods, workloads, services and config | principal |
| `GET` | `/api/v1/projects/{project_id}/kubernetes/namespaces/{namespace}/pods/{pod}` | One pod's logs AND its events | principal |
| `GET` | `/api/v1/projects/{project_id}/kubernetes/namespaces/{namespace}/rollouts/{rollout}` | One Argo Rollout's progress: weight, step, replicas and analysis verdicts | principal |
| `POST` | `/api/v1/projects/{project_id}/kubernetes/workloads/actions` | Scale, restart or roll back one workload (through the chokepoint) | principal |

### `incidents`

| Method | Path | Summary | Auth |
| --- | --- | --- | --- |
| `GET` | `/api/v1/incidents/{incident_id}` | Read Incident | principal |
| `POST` | `/api/v1/incidents/{incident_id}/analysis` | Analyse Incident | principal |
| `GET` | `/api/v1/incidents/{incident_id}/suggestions/{suggestion_id}/preview` | Preview Suggestion | principal |
| `POST` | `/api/v1/incidents/{incident_id}/suggestions/{suggestion_id}/submit` | Submit Suggestion | principal |
| `GET` | `/api/v1/projects/{project_id}/incidents` | List Incidents | principal |

### `integrations`

| Method | Path | Summary | Auth |
| --- | --- | --- | --- |
| `GET` | `/api/v1/integrations/github` | Whether this user has linked GitHub | principal |
| `DELETE` | `/api/v1/integrations/github` | Disconnect and revoke | principal |
| `GET` | `/api/v1/integrations/github/callback` | Finish linking a GitHub account (browser redirect target) | public |
| `POST` | `/api/v1/integrations/github/connect` | Begin linking a GitHub account | principal |
| `GET` | `/api/v1/integrations/github/repositories` | The repositories this user's linked account can reach | principal |
| `POST` | `/api/v1/integrations/github/repositories` | Create a new GitHub repository for this user | principal |
| `GET` | `/api/v1/integrations/github/repositories/{owner}/{repo}/branches` | The branches and permissions for a specific GitHub repository | principal |
| `PUT` | `/api/v1/integrations/github/token` | Link a GitHub account with a token, without leaving ForgeOps | principal |
| `GET` | `/api/v1/integrations/vercel` | Get current user's Vercel integration status | principal |
| `DELETE` | `/api/v1/integrations/vercel` | Disconnect Vercel account | principal |
| `POST` | `/api/v1/integrations/vercel/test` | Test current Vercel connection | principal |
| `PUT` | `/api/v1/integrations/vercel/token` | Link a Vercel account with a personal access token | principal |

### `knowledge`

| Method | Path | Summary | Auth |
| --- | --- | --- | --- |
| `GET` | `/api/v1/knowledge/topics` | List Topics | principal |
| `POST` | `/api/v1/projects/{project_id}/knowledge/ask` | Ask | principal |

### `learning`

| Method | Path | Summary | Auth |
| --- | --- | --- | --- |
| `PATCH` | `/api/v1/learning/preferences/{preference_id}` | Correct Preference | principal |
| `DELETE` | `/api/v1/learning/preferences/{preference_id}` | Forget Preference | principal |
| `POST` | `/api/v1/projects/{project_id}/learning/feedback` | Record Feedback | principal |
| `GET` | `/api/v1/projects/{project_id}/learning/history` | Read History | principal |
| `GET` | `/api/v1/projects/{project_id}/learning/preferences` | List Preferences | principal |
| `POST` | `/api/v1/projects/{project_id}/learning/preferences` | State Preference | principal |
| `POST` | `/api/v1/projects/{project_id}/learning/reflect` | Run Reflection | principal |
| `GET` | `/api/v1/projects/{project_id}/learning/skill-file` | Read Skill File | principal |
| `POST` | `/api/v1/projects/{project_id}/learning/turns` | Record Turn | principal |

### `mcp`

| Method | Path | Summary | Auth |
| --- | --- | --- | --- |
| `POST` | `/api/v1/mcp` | Mcp Ingress | principal |
| `GET` | `/api/v1/mcp/apps/{name}` | Get App Descriptor | principal |
| `GET` | `/api/v1/mcp/apps/{name}/host` | Get App Host Page | principal |
| `GET` | `/api/v1/mcp/servers` | List Servers | principal |

### `monitoring`

| Method | Path | Summary | Auth |
| --- | --- | --- | --- |
| `GET` | `/api/v1/monitoring/queries` | List Queries | principal |
| `POST` | `/api/v1/monitoring/query` | Run Query | principal |
| `GET` | `/api/v1/monitoring/readiness` | Monitoring Readiness | principal |

### `notifications`

| Method | Path | Summary | Auth |
| --- | --- | --- | --- |
| `GET` | `/api/v1/projects/{project_id}/notifications` | This project's notifications, newest first | principal |
| `GET` | `/api/v1/projects/{project_id}/notifications/preferences` | This user's channel preferences for this project | principal |
| `PUT` | `/api/v1/projects/{project_id}/notifications/preferences` | Set one channel-and-kind preference | principal |
| `POST` | `/api/v1/projects/{project_id}/notifications/{notification_id}/read` | Mark one notification read | principal |

### `policies`

| Method | Path | Summary | Auth |
| --- | --- | --- | --- |
| `GET` | `/api/v1/policies` | List the caller's stored policies | principal |
| `POST` | `/api/v1/policies` | Create Policy | principal |
| `GET` | `/api/v1/policies/active-bundle` | Which policy bundle is active for a project | principal |
| `POST` | `/api/v1/policies/publish` | Publish Bundle | principal |
| `GET` | `/api/v1/policies/templates` | List Templates | principal |
| `GET` | `/api/v1/policies/{policy_id}` | Get Policy | principal |
| `PATCH` | `/api/v1/policies/{policy_id}` | Update Policy | principal |
| `DELETE` | `/api/v1/policies/{policy_id}` | Delete Policy | principal |
| `POST` | `/api/v1/policies/{policy_id}/test` | Test Policy Dry Run | principal |

### `projects`

| Method | Path | Summary | Auth |
| --- | --- | --- | --- |
| `GET` | `/api/v1/projects` | List the caller's projects | principal |
| `POST` | `/api/v1/projects` | Create a project | principal |
| `POST` | `/api/v1/projects/from-github` | Create a project from a repository on the caller's linked GitHub account | principal |
| `POST` | `/api/v1/projects/import/github` | Import a GitHub repository as a project | principal |
| `GET` | `/api/v1/projects/tags` | Every tag in use in this tenant | principal |
| `GET` | `/api/v1/projects/{project_id}` | Read one project | principal |
| `DELETE` | `/api/v1/projects/{project_id}` | Delete a project and its dependent rows | principal |
| `GET` | `/api/v1/projects/{project_id}/activity` | Project activity | principal |
| `POST` | `/api/v1/projects/{project_id}/archive` | Archive a project (soft) | principal |
| `POST` | `/api/v1/projects/{project_id}/clone` | Clone the project's GitHub repository onto the paired agent's machine | principal |
| `PUT` | `/api/v1/projects/{project_id}/favourite` | Mark as this caller's favourite | principal |
| `DELETE` | `/api/v1/projects/{project_id}/favourite` | Unstar | principal |
| `POST` | `/api/v1/projects/{project_id}/github/push` | Push project codebase to GitHub | principal |
| `GET` | `/api/v1/projects/{project_id}/readiness` | Readiness score | principal |
| `POST` | `/api/v1/projects/{project_id}/scan` | Trigger an agent codebase scan for this project | principal |
| `PUT` | `/api/v1/projects/{project_id}/tags` | Add a tag to a project | principal |
| `DELETE` | `/api/v1/projects/{project_id}/tags/{tag}` | Remove a tag | principal |
| `POST` | `/api/v1/projects/{project_id}/unarchive` | Restore an archived project | principal |
| `GET` | `/api/v1/projects/{project_id}/vercel/config-check` | Check project Vercel deployment configuration | principal |
| `POST` | `/api/v1/projects/{project_id}/vercel/deploy` | Deploy project to Vercel | principal |

### `releases`

| Method | Path | Summary | Auth |
| --- | --- | --- | --- |
| `GET` | `/api/v1/projects/{project_id}/releases/diff` | Diff between two deployments | principal |
| `POST` | `/api/v1/projects/{project_id}/releases/promote` | Promote an environment's stable state to the next | principal |
| `POST` | `/api/v1/projects/{project_id}/releases/rollback` | Roll an environment back to a stable deployment | principal |
| `GET` | `/api/v1/projects/{project_id}/releases/timeline` | Deployment history with version metadata | principal |

### `secrets`

| Method | Path | Summary | Auth |
| --- | --- | --- | --- |
| `GET` | `/api/v1/secrets` | List Secrets | principal |
| `POST` | `/api/v1/secrets` | Create Secret | principal |
| `PATCH` | `/api/v1/secrets/{secret_id}` | Update Secret | principal |
| `DELETE` | `/api/v1/secrets/{secret_id}` | Delete Secret | principal |

### `self-healing`

| Method | Path | Summary | Auth |
| --- | --- | --- | --- |
| `GET` | `/api/v1/healing/remedies` | List Remedies | principal |
| `POST` | `/api/v1/incidents/{incident_id}/heal` | Heal | principal |
| `POST` | `/api/v1/incidents/{incident_id}/heal/propose` | Propose | principal |
| `GET` | `/api/v1/incidents/{incident_id}/healing` | Healing Log | principal |
| `GET` | `/api/v1/incidents/{incident_id}/postmortem` | Read Postmortems | principal |
| `POST` | `/api/v1/incidents/{incident_id}/postmortem` | Write Postmortem | principal |

### `untagged`

| Method | Path | Summary | Auth |
| --- | --- | --- | --- |
| `GET` | `/api/v1/health` | Api V1 Health | public |
| `GET` | `/health` | Health | public |
| `GET` | `/health/ready` | Health Ready | public |

<!-- END GENERATED ENDPOINTS -->
