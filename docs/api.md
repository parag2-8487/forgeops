# ForgeOps API — Phase 0

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
> file is rewritten, the accurate and self-updating source is the generated schema — Swagger UI at
> <http://localhost:8000/api/v1/docs> and `openapi.json` beside it — because it is produced from the
> routers themselves and cannot drift from them.

Authority: `.antigravity/specs/phase-0-foundation/design.md` §4.2, §4.3, §4.4, §11, §14.2, §15.2.
Only the surfaces listed here exist in Phase 0.

## Versioning

Public API routes are versioned in the URL under `/api/v1`. The OpenAPI document is served
at `/api/v1/openapi.json`. Probe endpoints are deliberately **unversioned**: they are an
infrastructure contract for container orchestrators, not part of the public API, so they do
not move when the API version bumps.

## Authentication

Phase 0 has **no general user authentication**. There is no login flow, no session or
refresh-token lifecycle, no user records, and no RBAC; those arrive in Phase 1 §1.11.

Two surfaces verify an OAuth 2.1/OIDC bearer token:

- `/api/v1/mcp*` — the MCP Gateway. Verification runs before routing.
- `POST /api/v1/ai/complete` — verification supplies the `sub` that keys the per-caller
  rate-limit bucket.

Verification enforces the JWT signature against JWKS fetched from the token issuer, an
`iss` value inside an explicit allowlist (required to be non-empty when
`APP_ENV=production`), the required `aud`, and `exp`/`nbf`/`iat`. Failures return `401`
problem documents. Every other route is unauthenticated in Phase 0, which is why the
Phase 0 topology is local-development-only — see `docs/deployment.md`.

## Error contract — RFC 9457

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
| `GET /health`        | Liveness                | none                                                   | `200 {"status":"ok","version":"…","commit":"…"}`, including during a dependency outage | only when the process is dead or wedged                           |
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

`tools/list` order: verify bearer/OIDC → route from headers → Redis TTL cache or upstream
list → OPA filter on every response (cache hit or miss) → return. Only the unfiltered
upstream list is cached. A Redis failure is treated as a cache miss; an OPA failure returns
an empty allowed set.

`tools/call` order: verify bearer/OIDC → route from headers → parse the called tool →
resolve tool metadata locally or from an already-valid cache entry with no upstream I/O →
OPA authorise → invoke upstream only on allow. Invalid bearer, malformed call, unresolved
metadata, unknown tool, policy denial, and policy error all return before any upstream
operation.

Status codes: `400` for missing routing headers, `401` for token failures, `403` for policy
denial, `404` for an unknown server or task, `504` for an upstream timeout — all as RFC 9457
problems.

### Tasks Extension

Task states are `submitted`, `working`, `input_required`, `completed`, `failed`,
`cancelled`. Terminal states absorb further transitions, and `tasks/cancel` on a terminal
task returns that state with `200` — cancellation is idempotent. Records live in Redis so
any replica can serve `tasks/get`; concurrent updates use compare-and-set so only one
writer wins.

### MCP Apps hosting

The host page sets
`Content-Security-Policy: default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; frame-ancestors 'self'`
and the iframe carries `sandbox="allow-scripts allow-forms"` without `allow-same-origin`.
The parent↔app channel is `postMessage` with the envelope `{v: 1, type, requestId, payload}`
and the parent drops messages whose origin does not match the descriptor origin. Phase 0
ships one descriptor, for the agent's `agent.health` tool.

## Model routing

| Route                 | Method | Notes                                                                                                                    |
| :-------------------- | :----- | :----------------------------------------------------------------------------------------------------------------------- |
| `/api/v1/ai/tiers`    | `GET`  | The six tiers with protocol, availability reason, and circuit-breaker state                                              |
| `/api/v1/ai/complete` | `POST` | Fixed order: OIDC verify → require `claims.sub` → Redis token-bucket limiter → semantic cache → registry/router/provider |

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
six-value event vocabulary; `sse-starlette` is not a dependency. The agent↔backend protocol
is JSON-RPC 2.0 over WSS, outbound-only, and in Phase 0 only the transport mechanics exist.

<!-- BEGIN GENERATED ENDPOINTS -->

<!-- Generated by scripts/dump-openapi.py from the live app. Do not edit by hand:
     `python scripts/dump-openapi.py --check` fails the build when this drifts. -->

The application publishes **61 operations across 50 paths**.

### `agents`

| Method | Path | Summary | Auth |
| --- | --- | --- | --- |
| `GET` | `/api/v1/agents/devices` | List paired agent devices | principal |
| `GET` | `/api/v1/agents/devices/{device_id}` | Read one agent device | principal |
| `POST` | `/api/v1/agents/pair/exchange` | Exchange a pairing code for device credentials (public) | public |
| `POST` | `/api/v1/agents/pairing-codes` | Issue a single-use pairing code for a project | principal |
| `DELETE` | `/api/v1/agents/{device_id}` | Revoke a device | principal |

### `ai`

| Method | Path | Summary | Auth |
| --- | --- | --- | --- |
| `POST` | `/api/v1/ai/complete` | Complete | principal |
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
| `POST` | `/api/v1/approvals/{change_set_id}/reject` | Reject a pending change set | principal |
| `POST` | `/api/v1/approvals/{change_set_id}/revert` | Revert an applied change set | principal |

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

### `generation`

| Method | Path | Summary | Auth |
| --- | --- | --- | --- |
| `POST` | `/api/v1/generation/runs` | Generate deployment artifacts, streaming progress as SSE | principal |

### `mcp`

| Method | Path | Summary | Auth |
| --- | --- | --- | --- |
| `POST` | `/api/v1/mcp` | Mcp Ingress | principal |
| `GET` | `/api/v1/mcp/apps/{name}` | Get App Descriptor | principal |
| `GET` | `/api/v1/mcp/apps/{name}/host` | Get App Host Page | principal |
| `GET` | `/api/v1/mcp/servers` | List Servers | principal |

### `policies`

| Method | Path | Summary | Auth |
| --- | --- | --- | --- |
| `GET` | `/api/v1/policies` | List the caller's stored policies | principal |
| `POST` | `/api/v1/policies` | Create Policy | principal |
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
| `POST` | `/api/v1/projects/import/github` | Import a GitHub repository as a project | principal |
| `GET` | `/api/v1/projects/tags` | Every tag in use in this tenant | principal |
| `GET` | `/api/v1/projects/{project_id}` | Read one project | principal |
| `DELETE` | `/api/v1/projects/{project_id}` | Delete a project and its dependent rows | principal |
| `GET` | `/api/v1/projects/{project_id}/activity` | Project activity | principal |
| `POST` | `/api/v1/projects/{project_id}/archive` | Archive a project (soft) | principal |
| `PUT` | `/api/v1/projects/{project_id}/favourite` | Mark as this caller's favourite | principal |
| `DELETE` | `/api/v1/projects/{project_id}/favourite` | Unstar | principal |
| `GET` | `/api/v1/projects/{project_id}/readiness` | Readiness score | principal |
| `PUT` | `/api/v1/projects/{project_id}/tags` | Add a tag to a project | principal |
| `DELETE` | `/api/v1/projects/{project_id}/tags/{tag}` | Remove a tag | principal |
| `POST` | `/api/v1/projects/{project_id}/unarchive` | Restore an archived project | principal |

### `secrets`

| Method | Path | Summary | Auth |
| --- | --- | --- | --- |
| `GET` | `/api/v1/secrets` | List Secrets | principal |
| `POST` | `/api/v1/secrets` | Create Secret | principal |
| `PATCH` | `/api/v1/secrets/{secret_id}` | Update Secret | principal |
| `DELETE` | `/api/v1/secrets/{secret_id}` | Delete Secret | principal |

### `untagged`

| Method | Path | Summary | Auth |
| --- | --- | --- | --- |
| `GET` | `/api/v1/health` | Api V1 Health | public |
| `GET` | `/health` | Health | public |
| `GET` | `/health/ready` | Health Ready | public |

<!-- END GENERATED ENDPOINTS -->
