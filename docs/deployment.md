# ForgeOps Deployment — Phase 0

> **Stale as of 2026-08-21.** This describes the **Phase 0** topology. The security warning
> below still holds and should be read. One concrete fact has since become false: it says Phase 0
> has "exactly one revision, `0001_initial`" — there are now **ten**, `0001` through `0010`, adding
> identity and devices, codebase-index extensions, change-sets, policies, secrets, the append-only
> audit table, generation runs, project tags and the change-set status vocabulary.

**Warning: the Phase 0 `docker-compose.yml` topology is for local development on a trusted
machine only. It must never be exposed to a network.** Phase 0 adds no general user
authentication to the API surface, so most non-MCP routes are unauthenticated; the single
narrow exception is `POST /api/v1/ai/complete`, which verifies
OIDC solely to key the required per-caller abuse-protection bucket. There is no supported
non-local deployment of Phase 0, and no production deployment target exists yet.

## What the local topology publishes

The Compose project is named `forgeops`. The unprofiled default set is exactly five
services: `postgres`, `redis`, `opa`, `backend`, `frontend`.

| Service    | Local binding    | Notes                               |
| :--------- | :--------------- | :---------------------------------- |
| `frontend` | `127.0.0.1:3000` | Next.js shell                       |
| `backend`  | `127.0.0.1:8000` | FastAPI; liveness health check only |
| `postgres` | `127.0.0.1:5432` | PostgreSQL 17 + pgvector            |
| `redis`    | `127.0.0.1:6379` | Redis Stack, vector search enabled  |
| `opa`      | `127.0.0.1:8181` | Gateway policy server               |

Every published port binds to `127.0.0.1` in the committed file rather than `0.0.0.0`, so a
laptop on an untrusted network is not serving a database. `CORS_ALLOW_ORIGINS` defaults to
exactly `http://localhost:3000` with no wildcard, so a hostile page cannot drive the API
from the developer's browser.

Optional profiles are never part of the default startup and are exercised separately:
`docker compose --profile vault up -d --wait` for Infisical, and
`docker compose --profile tools up -d --wait` for the `agent-dev` container carrying
OpenTofu. The agent itself ships as a single binary, not as a Compose service.

## Starting and stopping

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
Phase 0 has exactly one revision, `0001_initial`.

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
