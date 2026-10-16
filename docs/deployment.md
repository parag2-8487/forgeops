# ForgeOps Deployment — Phase 0

**Warning: the Phase 0 `docker-compose.yml` topology is for local development on a trusted
machine only. It must never be exposed to a network.** Phase 0 adds no general user
authentication to the API surface, so most non-MCP routes are unauthenticated; the single
narrow exception is `POST /api/v1/ai/complete`, which verifies
OIDC solely to key the required per-caller abuse-protection bucket. There is no supported
non-local deployment of Phase 0, and no production deployment target exists yet.

> **Stale as of 2026-08-21, and the notice moved on 2026-08-28.** This describes the **Phase 0**
> topology. The warning above still holds and is deliberately the first paragraph: `scripts/check-docs.sh`
> asserts that both of its clauses appear there, and this notice previously sat in front of it — so the
> check had been failing, invisibly, because no workflow and no hook invoked it. It runs in the `audit`
> job now, beside the other gates that had the same problem.
>
> One concrete fact in the text below has since become false: it says Phase 0 has "exactly one
> revision, `0001_initial`". There are now **thirteen**, `0001` through `0013`, adding identity and
> devices, codebase-index extensions, change-sets, policies, secrets, the append-only audit table,
> generation runs, project tags, the change-set status vocabulary, `served_from: pending`, the cAST
> metadata columns on `embeddings_local`, and project archiving with per-user favourites.
>
> **Phase 2 Autonomous Deployment Update (2026-10-13):**
> For complete technical documentation of the Phase 2 autonomous deployment pipeline (Docker daemon management, GitHub Git Data API publishing, Vercel REST API integration), G1–G7 verification gates, G8 bounded self-healing engine, port authority validation, and browser safety rules, refer to [FORGEOPS_PROJECT_MASTER_DOCUMENT.md](file:///C:/IMP/antigravity-cli/Major%20Project/Devops%20Automation/FORGEOPS_PROJECT_MASTER_DOCUMENT.md).

## What the local topology publishes

The Compose project is named `forgeops`. In the current implementation, **14 services are declared** in `docker-compose.yml`, with **10–14 active** in the default unprofiled set without requiring manual flags:

| Service            | Local binding     | Notes                                                              |
| :----------------- | :---------------- | :----------------------------------------------------------------- |
| `frontend`         | `127.0.0.1:3000`  | Next.js 16 (React 19) App Router web shell                         |
| `backend`          | `127.0.0.1:8000`  | FastAPI modular monolith; liveness health check only               |
| `backend-agent`    | `127.0.0.1:18443` | Agent mTLS listener terminating client certificates on port 8443   |
| `worker`           | — (no port)       | Asynchronous ARQ distributed task runner and deployment pipeline   |
| `postgres`         | `127.0.0.1:5432`  | PostgreSQL 17 + pgvector (relational data & semantic embeddings)   |
| `redis`            | `127.0.0.1:6379`  | Redis Stack 7.4, vector search enabled (caching, queues, limitter) |
| `opa`              | `127.0.0.1:8181`  | Gateway and governance Rego policy server                          |
| `cerbos`           | `127.0.0.1:3592`  | Principal and resource RBAC policy engine                          |
| `authentik-server` | `127.0.0.1:9000`  | Authentik 2026.5.6 OIDC identity provider server                   |
| `authentik-worker` | — (no port)       | Authentik background asynchronous task processor                   |
| `ollama`           | `127.0.0.1:11434` | Primary local model server (qwen3-coder-next, bge-m3:567m)         |
| `ollama-secondary` | `127.0.0.1:11435` | Secondary model server for cascade failover (glm-5.2)              |
| `inngest`          | `127.0.0.1:8288`  | Inngest durable execution workflow engine                          |

Every published port binds to `127.0.0.1` in the committed file rather than `0.0.0.0`, so a
laptop on an untrusted network is not serving a database. `CORS_ALLOW_ORIGINS` defaults to
exactly `http://localhost:3000` with no wildcard, so a hostile page cannot drive the API
from the developer's browser.

Optional profiles are never part of the default startup and are exercised separately:
- `docker compose --profile vault up -d --wait` for Infisical secret management.
- `docker compose --profile tools up -d --wait` for the `agent-dev` container carrying OpenTofu 1.12.5.
- `docker compose --profile observability up -d --wait` for the OTel monitoring tier (`otel-agent`, `otel-gateway`, `prometheus`, `mimir`, `loki`, `tempo`, `grafana`).

The host agent binary connects to `backend-agent` over mTLS via outbound-only WebSockets.

## 1-Click Automated Setup on Linux / Ubuntu (`./setup.sh`)

For fresh Linux installations—particularly Ubuntu 22.04 LTS or 24.04 LTS (or Ubuntu 20.04 LTS with Python 3.10+) on lab or development workstations lacking Docker or build tools:

```bash
chmod +x setup.sh
./setup.sh
```

### Prerequisites
- **Operating System**: Ubuntu 22.04 LTS or 24.04 LTS (Ubuntu 20.04 LTS requires Python 3.10+ package).
- **Disk Space**: At least ~10 GB free space for Docker base layers and models.
- **Memory**: ~4 GB RAM minimum (a 2 GB safe swapfile is created automatically on low-RAM machines if filesystem allows).
- **Privileges**: sudo rights for initial package and Docker group configuration (`usermod -aG docker`).
- **Network**: Internet access for initial Docker repository setup and base image pulls.

`setup.sh` handles package manager locks (`unattended-upgrades`), installs Docker CE and Compose V2 plugin, sets up non-root docker group permissions, detects free ports, generates cryptographic keys and internal CA certificates, runs migrations, boots the services, and validates the entire stack against `/health/ready`.

## Autonomous Deployment Verification Gates (G1–G7) & BuildKit Integration

Phase 2 adds the Autonomous Deployment Engine, executing deployments through 7 sequential gates:

1. **G1 (Blueprint Baseline)**: Strategy validation (`docker_github_vercel`, `docker_github`, `vercel_only`, `github_only`), target configuration presence, workspace path resolution, and R0 readiness assessment.
2. **G2 (Synthesis & Template Library)**: Existing manifest discovery or deterministic multi-stage Dockerfile synthesis for Vite, Node.js, Python, and Go. Consolidates `ENV` instructions (`ENV NODE_ENV=production PORT=...`) and mounts stable tenant/project-scoped npm cache mounts (`id=npm-{clean_tenant}-{clean_proj}`) to avoid redundant dependency downloads. Computes post-generation readiness (R1).
3. **G3 (Cross-artifact Consistency)**: Static validation ensuring port ranges are valid (1–65535), reserved environment variables are not overwritten, and browser-restricted host ports (such as `4045` with `ERR_UNSAFE_PORT`) are rejected across all 77 Chromium/WHATWG bad ports.
4. **G4 (Build & Compile)**: Direct Docker Engine daemon integration via Unix Domain Socket (`/var/run/docker.sock`). Integrates the native **BuildKit HTTP API** (`version=2`) with tar streaming, BuildKit trace streaming (`moby.buildkit.trace`), and protobuf decoding. Evaluates the build graph directly without intermediate container commits (saving 808s on Code Review Platform), with sensitive credential exclusion (`.env*`, `*.pem`, `*.key`), symlink boundary confinement, and automatic graceful fallback to the legacy Docker V1 builder if BuildKit negotiation fails.
5. **G5 (Apply)**: Container launch via Docker Engine API over Unix Domain Socket with strict runtime port authority, conflict detection with immutable commands, and environment variable injection (`PORT`, `HOST=0.0.0.0`).
6. **G6 (Workload Convergence)**: Real-time container inspect publication check and live HTTP readiness probing with fast-failure on connection refusal or non-200 responses.
7. **G7 (Final Verification & Cryptographic Receipt)**: Comprehensive multi-target sign-off verifying exact physical container port bindings (`HostConfig.PortBindings`), live remote GitHub commit presence via Git Data API, and Vercel assigned production alias accessibility with SSO redirect bypass and synthetic placeholder rejection.

## Starting and stopping (Make & Compose)

```sh
make up      # init-env, then docker compose up -d --wait, then poll /health/ready
make logs    # docker compose logs -f --tail=100
make down    # unprofiled docker compose down
```

`make up` depends on `make init-env`, which creates `.env` from the committed
`.env.example` only when `.env` is absent and never overwrites an existing file. Direct
`docker compose up -d --wait` also works on a fresh clone because every service loads
`.env.example` as a required env file and `.env` as an optional override.

Database schema: `make migrate` runs `alembic upgrade head` inside the backend container.
The schema spans migrations `0001_initial` through `0013`.

## Health versus readiness when operating the stack

These two probes answer different questions, and conflating them is the most common
operational mistake here.

| Probe                | Question                                 | Dependency I/O                                         | During a PostgreSQL or Redis outage                               |
| :------------------- | :--------------------------------------- | :----------------------------------------------------- | :---------------------------------------------------------------- |
| `GET /health`        | Is the process alive and accepting work? | none                                                   | still `200` — do not restart the container on a dependency outage |
| `GET /health/ready`  | Can the process serve dependent traffic? | PostgreSQL `SELECT 1` + Redis `PING`, 2 s timeout each | RFC 9457 `503` naming each failed or timed-out dependency         |
| `GET /api/v1/health` | Versioned informational liveness echo    | none                                                   | still `200`                                                       |

The Compose backend container health check uses `/health` only, deliberately: a transient
data-plane outage must not make Docker mark the backend unhealthy and restart it.
`scripts/dev-up.sh` polls `/health/ready` separately after startup, with a bounded timeout
and named failure output. The backend also starts successfully when PostgreSQL and Redis are
unreachable — startup fails fast only on invalid local configuration — and readiness
recovers on its own once the dependencies return, without restarting the process.

## Configuration

Configuration is 12-factor environment variables, validated at startup. `.env.example` is
the committed baseline inventory and contains placeholder secret values only; never commit a
real secret. Compose interpolation in port and build-argument expressions does not read
`env_file`, so every such expression carries an explicit safe local default.

Frontend public variables (`NEXT_PUBLIC_API_BASE_URL`, `NEXT_PUBLIC_APP_NAME`) are inlined
at build time. They are passed as Docker `ARG`/`ENV` values in the builder stage before
`pnpm build`; supplying them only at runtime has no effect. They are shipped to the browser
and must never hold a secret or a server-internal Compose hostname.

## Release artifacts

The agent is released through GoReleaser for six `CGO_ENABLED=0` targets, with Syft
CycloneDX SBOMs, Cosign keyless signatures, and SLSA provenance. `make sbom`,
`make release-snapshot`, and `make verify-release` cover generation, unpublished validation,
and verification of a published artifact. An artifact that cannot be verified is not a
release. Keyless signing needs network access to Sigstore at release time.

Each artifact is published alongside `.sig` and `.pem` (Cosign keyless signature and Fulcio
certificate), `.sbom.json` (CycloneDX), `.intoto.jsonl` (the signed DSSE envelope carrying
an in-toto SLSA v1 provenance statement), and `.att.sigstore.json` (a Sigstore bundle with
the same attestation plus its Rekor inclusion proof).

Provenance comes from `cosign attest-blob`, not from GitHub's artifact attestation API,
because that API is unavailable for private repositories outside GitHub Enterprise Cloud
(decision D-20 in `PROGRESS.md`). Two consequences are worth knowing:

- `gh attestation verify` will **not** find these attestations. Verify them with cosign.
- Verifying from the bundle needs no access to Rekor, because the inclusion proof is
  embedded, so it works behind a network that blocks or intercepts `rekor.sigstore.dev`:

```sh
cosign verify-blob-attestation \
  --bundle  <artifact>.att.sigstore.json \
  --new-bundle-format --type slsaprovenance1 --check-claims=true \
  --certificate-identity-regexp '^https://github.com/parag8487/ForgeOps/.github/workflows/release.yml@refs/tags/v.*$' \
  --certificate-oidc-issuer 'https://token.actions.githubusercontent.com' \
  <artifact>
```

The native GitHub attestation step is retained in `release.yml` and gated on
`github.event.repository.private == false`. It reports `skipped` while the repository is
private and starts producing attestations by itself if the repository is made public.

## Before any non-local deployment

Phase 1 §1.11 adds Authentik/OIDC login, JWT lifecycle, and RBAC across all routes, plus
agent pairing, mTLS workload identity, and the governance control plane. Until those land,
there is no supported way to run this stack anywhere but a trusted developer machine.
