# ForgeOps

AI-powered DevOps automation platform.

- **Repository:** <https://github.com/parag8487/ForgeOps>
- **Owner:** `parag8487`
- **Go module path (agent):** `github.com/parag8487/ForgeOps/agent`
- **Master Technical Document:** For the complete, authoritative specification covering the full architecture, problem statement, objectives, feature catalogue, G1–G7 gate workflows, G8 recovery, port authority, testing evidence, and evaluation guide, see [`FORGEOPS_PROJECT_MASTER_DOCUMENT.md`](FORGEOPS_PROJECT_MASTER_DOCUMENT.md).
- **Canonical Architecture & System Diagrams:** For the complete, unified architectural flowchart and subsystem breakdown (UI, Security/Governance, FastAPI, ARQ/Redis queues, G1–G7 verification gates, Docker UDS/BuildKit v2, multi-target execution, and automated host setup), see [`docs/architecture/FORGEOPS_CANONICAL_ARCHITECTURE.md`](docs/architecture/FORGEOPS_CANONICAL_ARCHITECTURE.md) and [`ARCHITECTURE-DIAGRAMS.md`](ARCHITECTURE-DIAGRAMS.md).
- **Status:** **Phase 2 Autonomous Deployment Engine Implemented.**
  Phase 0 (foundation scaffolding) and Phase 1 (MVP core analysis, generation, and approval) are complete. Phase 2 (Autonomous Deployment Orchestrator, multi-target deployment strategies across Docker, GitHub, and Vercel, G1–G7 verification gates, deterministic port authority, and G8 bounded self-healing recovery) is active and implemented on the `phase-2-implementation` branch.

## Running on Linux / Ubuntu (1-Click Master Setup)

For Linux systems — especially **fresh installations such as college lab computers or cloud VMs** running
Ubuntu 22.04 LTS or 24.04 LTS (or Ubuntu 20.04 LTS with Python 3.10+ installed) that have **no Docker**,
missing build tools, or stopped services — use the master setup script. It installs everything the stack
needs, configures the environment, and then starts and verifies the entire platform.

### Step-by-Step: From Cloning to Running Platform

Open your Ubuntu terminal and execute the following commands:

```bash
# 1. Clone the repository
git clone https://github.com/parag2-8487/forgeops.git
cd forgeops

# 2. Switch to the active implementation branch
git checkout phase-2-implementation

# 3. Grant execute permissions to the setup script
chmod +x setup.sh

# 4. Run the 1-click master setup
./setup.sh
```

That is the whole install. No manual installation of Docker, PostgreSQL, Redis, or Node.js is required.
Once the script completes, all 10–14 microservices will be healthy, and you can open your browser at **<http://localhost:13000>** to access the platform.

### Before you start

| Requirement          | Why                                             | If it is missing                                                                      |
| :------------------- | :---------------------------------------------- | :------------------------------------------------------------------------------------ |
| **Supported OS**     | Ubuntu 22.04 / 24.04 LTS (or 20.04 w/ Py 3.10+) | Ubuntu 22.04+ includes Python >= 3.10 in base repos; 20.04 requires Python 3.10+ pkg  |
| **~10 GB free disk** | four images plus their base layers              | the script stops with the measured free space rather than failing deep inside a build |
| **Internet access**  | Docker's repository, PyPI, and base images      | —                                                                                     |
| **sudo rights**      | Docker and system packages                      | —                                                                                     |
| **~4 GB RAM**        | Authentik alone wants about 1 GB                | under 2.5 GB with no swap, a safe 2 GB swapfile is created if filesystem supports it  |

A first run takes roughly **10–20 minutes**, most of it pulling base images and building. Later
runs detect the existing images and finish in seconds.

### What `setup.sh` handles automatically

1. **Balances the package-manager lock.** A freshly booted Ubuntu is usually running
   `unattended-upgrades`, which holds the `dpkg` lock. The script configures `apt` to _wait_ for it
   (up to 5 minutes) instead of failing — so a normal first boot does not produce a spurious
   "could not install Docker".
2. **Docker Engine and Compose V2.** Adds Docker's official GPG keyring and repository
   (`download.docker.com/linux/ubuntu`) and installs `docker-ce`, `docker-ce-cli`,
   `containerd.io`, `docker-buildx-plugin` and `docker-compose-plugin`. If that repository is
   unreachable it falls back to the distribution packages, and if the Compose V2 plugin is still
   absent it downloads the plugin binary directly.
3. **Secure Docker group permissions.** Adds your user to the `docker` group (`usermod -aG docker`).
   It never weakens system security with insecure world-writable socket permissions (`chmod 666`).
   It activates group membership immediately in the launcher subshell via `sg docker` when available,
   or provides clear diagnostic instructions to activate it (`newgrp docker`).
   _(Note: membership in the docker group grants root-equivalent administrative privileges on the host)._
4. **Daemon startup on any init system.** `systemctl` where systemd is present, `service` otherwise
   — covering WSL2 and container hosts as well as a normal desktop.
5. **Python Compatibility.** Detects existing host Python (≥ 3.10) for host-side provisioning and
   launcher tasks (such as internal CA generation, port probing, and Authentik initialization).
   Ubuntu 22.04 LTS (Python 3.10) and Ubuntu 24.04 LTS (Python 3.12) are supported out of the box
   using official distribution packages without requiring third-party PPAs. Ubuntu 20.04 LTS (which
   ships Python 3.8 by default) requires manual installation of a Python 3.10+ package. The full
   backend runtime runs inside Docker pinned to Python 3.13.
6. **Low-memory and safe swap protection.** Under 2.5 GB available RAM with no swap, it verifies
   filesystem compatibility (safely skipping swapfile creation on Btrfs or ZFS to prevent filesystem
   corruption) and checks for at least 4 GB free disk before creating a 2 GB swapfile, preventing
   container builds from being OOM-killed.
7. **Free-capacity check.** Refuses to start a build that cannot fit, printing the actual available
   bytes and how to reclaim more, rather than failing partway through.
8. **Port collision avoidance.** Finds free host ports for PostgreSQL, Redis, Cerbos, OPA, Authentik,
   backend and frontend, and records them in `.env`. A machine already using 3000 or 8080 is fine.
9. **Secrets and certificates.** Generates cryptographically random values for every secret
   (Authentik key, bootstrap token, admin password, envelope pepper) and a development internal CA
   into `.env`. It never overwrites a secret that already exists.
10. **Full boot and verification.** Builds the images, starts the services in dependency order,
    provisions the Authentik identity provider, applies the database migrations, and then **proves**
    the result — reading `/health/ready`, confirming the identity provider is reachable from both the
    backend and a browser, and checking that sign-in actually starts. It does not report success just
    because containers started.

### Options

```bash
./setup.sh --fresh                    # rebuild from scratch, deleting the data volumes (asks first)
./setup.sh --fresh --force            # the same, without the confirmation prompt
./setup.sh --skip-install             # never install anything; fail if something is missing
./setup.sh --rebuild                  # force an image rebuild
./setup.sh --no-browser               # do not open a browser at the end
./setup.sh --no-reset                 # reuse existing containers instead of replacing them
```

### If something goes wrong

The script stops at the first real problem and prints what to do next. The two most common:

| Symptom                                                    | Cause and fix                                                                                                                    |
| :--------------------------------------------------------- | :------------------------------------------------------------------------------------------------------------------------------- |
| `Docker daemon did not answer after starting.`             | Inside WSL2 or a container, systemd is absent. Start Docker Desktop, or run `sudo service docker start` and re-run `./setup.sh`. |
| `only N GB free on the filesystem holding /var/lib/docker` | Free space, or reclaim what Docker is holding: `docker system prune -a --volumes`, then re-run.                                  |

To see why the stack itself is unhappy: `docker compose logs -f backend`.

### Verifying the setup script itself

The launcher's behaviour is covered by a harness that runs on real Ubuntu and mutates nothing:

```bash
docker run --rm -v "$PWD:/mnt" -w /mnt ubuntu:24.04 bash scripts/test-start-forgeops.sh
```

### Services and Default Credentials

Once `setup.sh` finishes, the stack is fully operational:

| Surface                     | Local URL                             | Notes                                         |
| :-------------------------- | :------------------------------------ | :-------------------------------------------- |
| **Frontend Web Dashboard**  | <http://localhost:13000>              | Main UI for codebase analysis and deployments |
| **Backend REST API & Docs** | <http://localhost:18000/docs>         | Interactive Swagger documentation             |
| **Readiness Health Probe**  | <http://localhost:18000/health/ready> | Comprehensive microservice readiness status   |
| **Authentik Admin Portal**  | <http://localhost:19000/if/admin/>    | Identity provider administration              |

**Default Login Credentials:**

- **Admin**: Username `parag` | Password `parag1111`
- **Developer**: Username `parag-developer` | Password `parag1111`
- **Viewer**: Username `parag-viewer` | Password `parag1111`

If a port was already taken on your machine, the script will have moved that service and printed
the new value. The URLs above are also written into `.env`, and the final summary repeats the
addresses actually in use.

**Compose Services Topology (14 services declared, 10–14 active):**
The platform declares 14 containerized services in `docker-compose.yml`, with 10–14 active in the default profile without requiring manual flags:

1. `postgres`: PostgreSQL 17 with `pgvector` extension for relational data and semantic vector embeddings.
2. `redis`: Redis Stack Server 7.4 providing L1/L2 semantic caching, rate limiting, and task queues.
3. `opa`: Open Policy Agent 1.4.2 policy engine evaluating Rego rules for gateway and governance control.
4. `cerbos`: Cerbos 0.54.0 RBAC sidecar enforcing principal resource permissions for backend requests.
5. `authentik-server`: Authentik 2026.5.6 enterprise identity provider for OIDC/OAuth 2.1 authentication.
6. `authentik-worker`: Background asynchronous worker process for Authentik identity tasks.
7. `backend`: FastAPI Python 3.13 REST API control plane and SSE streaming engine.
8. `backend-agent`: Dedicated mTLS listener (ports 8443 / 18443) terminating client certificates from paired agents.
9. `worker`: Asynchronous ARQ task worker executing background jobs and autonomous deployment pipelines.
10. `frontend`: Next.js 16 (React 19) App Router web dashboard.
11. `ollama`: Primary local model server serving completion models (`qwen3-coder-next`) and embeddings (`bge-m3:567m`).
12. `ollama-secondary`: Secondary model server providing live cascade failover (`glm-5.2`) using shared weights.
13. `inngest`: Durable execution engine coordinating multi-step deployment pipelines and events.
14. Optional profile services: `infisical` (`--profile vault`), `agent-dev` (`--profile tools`), and the OTel observability tier (`--profile observability`: `otel-agent`, `otel-gateway`, `prometheus`, `mimir`, `loki`, `tempo`, `grafana`).

---

## Core Features & Capabilities

### 1. Codebase Analysis & Production Readiness Scoring

- Scans target codebases across five operational pillars: Docker, CI/CD, Kubernetes, Observability, and Cloud/IaC.
- Indexes project files into PostgreSQL with `pgvector` semantic vector embeddings powered by Ollama.
- Generates tailored Dockerfiles, Kubernetes manifests, and OpenTofu configurations with AI RAG context grounding and deterministic template fallbacks.

### 2. Push to GitHub (Cloud Source Sync)

- Synchronize your generated deployment artifacts and code directly to GitHub.
- **Push to Existing Repository**: Select from any repository accessible to your GitHub Personal Access Token (`repo` scope).
- **Create New Repository**: Create a brand new GitHub repository on the fly, with customizable repository name, description, and **Public** or **Private** visibility toggle.
- **Git Data API Pipeline**: Uses the low-level GitHub Git Data API (Blobs, Trees, Commits, Refs) to commit all workspace files directly without requiring a local `git` binary or local disk cloning.

### 3. Deploy to Vercel (Edge Hosting)

- Deploy frontend applications directly to Vercel edge infrastructure.
- **Automatic Framework Detection**: Automatically inspects project dependencies (`package.json`) to detect frameworks including Next.js, Vite, Create React App, or static HTML.
- **Smart SPA Routing**: Automatically injects Single Page Application rewrite rules into `vercel.json` (`"source": "/(.*)", "destination": "/index.html"`) so that client-side routers don't throw 404 errors on deep paths.
- **REST Deployment Engine**: Deploys project files directly through Vercel's REST API (`POST /v13/deployments`) with live deployment status and URL reporting.

### 4. Autonomous Deployment Engine & Verification Gates (G1–G7)

- **Multi-Target Strategies**: Orchestrates unified deployments across Docker, GitHub, and Vercel (`docker_github_vercel`, `docker_github`, `vercel_only`, `github_only`).
- **Deterministic Port Authority**: Enforces user-declared container ports (e.g. `8080`) over framework defaults, injecting runtime `PORT` and `HOST=0.0.0.0` environment variables and configuring Dockerfile synthesis.
- **Browser Port Safety**: Protects operators by strictly rejecting browser-restricted host ports (such as `4045` with `ERR_UNSAFE_PORT`) during schema validation and G3.
- **Sequential Gate Verification (G1–G7)**:
  - **G1 (Blueprint Baseline)**: Strategy constraints, required configuration presence, workspace path resolution, and baseline readiness assessment (R0).
  - **G2 (Synthesis & Template Library)**: Existing manifest evaluation or deterministic multi-stage Dockerfile synthesis for Vite, Node.js, Python, and Go with consolidated `ENV` declarations, stable tenant/project-scoped npm cache mounts (`id=npm-{clean_tenant}-{clean_proj}`), and post-generation readiness assessment (R1).
  - **G3 (Cross-artifact Consistency)**: Static pre-execution audit verifying port range integrity (1–65535), immutability of reserved environment variables, and enforcement of browser-safe host ports (rejecting all 77 Chromium/WHATWG bad ports).
  - **G4 (Build & Compile)**: Direct Docker Engine daemon integration featuring the native BuildKit HTTP API (`version=2`) with tar streaming, BuildKit trace streaming (`moby.buildkit.trace`), and protobuf decoding. Evaluates the build graph directly without intermediate container commits (saving 808s on Code Review Platform), with sensitive credential exclusion, symlink boundary confinement, and automatic graceful fallback to legacy Docker V1 builder.
  - **G5 (Apply)**: Container launch via Docker Engine API over Unix Domain Socket (`/var/run/docker.sock`) with strict runtime port authority, conflict detection with immutable commands, and environment variable injection (`PORT`, `HOST=0.0.0.0`).
  - **G6 (Workload Convergence)**: Real-time container inspect publication check and live HTTP readiness probing with fast-failure on connection refusal or non-200 responses.
  - **G7 (Final Verification & Cryptographic Receipt)**: Comprehensive multi-target sign-off verifying exact physical container port bindings (`HostConfig.PortBindings`), live remote GitHub commit presence via Git Data API, and Vercel assigned production alias accessibility with SSO redirect bypass and synthetic placeholder rejection.
- **G8 Bounded Self-Healing**: Automated crash recovery sweeper with monotonic worker fencing tokens and AI incident remediation bounded to 3 iterations with zero application source code mutation.

### 5. Zero-Trust Local Agent & Governance Chokepoint

- Local Go agent connects via outbound-only WebSockets to the control plane.
- Every modification is modeled as an immutable `ChangeSet` validated against Cerbos/OPA policies.
- Four-Eyes human approval gate enforced whenever the calculated blast radius exceeds safe limits.

---

## Alternative Start Methods (Make & Compose)

For systems with GNU Make and pre-installed toolchains:

```sh
make bootstrap   # verify the pinned toolchain, install the git hooks
make up          # start the default Compose profile, polling until readiness answers
```

`make down` stops the containers and preserves the volumes. Everything else — the Compose
profiles, the seeded development credentials, the test containers, the full target list —
is in [`docs/development.md`](docs/development.md).

**What is reachable without signing in.** Only the readiness probe, the versioned health
echo and the API documentation are public (§4.4). Every project, policy, secret and audit
route requires an authenticated principal, so the UI shows an explicit sign-in-required
state on those panels rather than inventing data to fill them. `docs/development.md`
covers bringing up the identity provider if you want the authenticated surfaces.

## Verifying a release

Every release artifact is signed keyless with Cosign (OIDC → Fulcio → Rekor) and carries a
CycloneDX SBOM and an in-toto SLSA v1 provenance attestation. Nothing needs a shared secret
to check.

```sh
# Signature and SBOM presence (design §13.4 `make verify-release`, criterion 16)
make verify-release ARTIFACT=forgeops-agent_<version>_<os>_<arch>.tar.gz

# SLSA provenance, verified from the Sigstore bundle. This works offline, because the
# Rekor inclusion proof travels inside the bundle.
cosign verify-blob-attestation \
  --bundle  forgeops-agent_<version>_<os>_<arch>.tar.gz.att.sigstore.json \
  --new-bundle-format --type slsaprovenance1 --check-claims=true \
  --certificate-identity-regexp '^https://github.com/parag8487/ForgeOps/.github/workflows/release.yml@refs/tags/v.*$' \
  --certificate-oidc-issuer 'https://token.actions.githubusercontent.com' \
  forgeops-agent_<version>_<os>_<arch>.tar.gz
```

Provenance is produced by `cosign attest-blob` rather than GitHub's artifact attestation
API, so it is **not** discoverable via `gh attestation verify`. See decision D-20 in
[`PROGRESS.md`](PROGRESS.md) for why.

The monorepo lives directly in the repository root: `agent/` (Go local agent and
CLI), `backend/` (FastAPI platform), `frontend/` (Next.js shell), `policies/`
(OPA policy), `scripts/`, `docs/`, and `.github/workflows/`. The four reference
documents at the root — `AI-Powered-DevOps-Platform-Complete-Technical-Research.md`,
`PRD.md`, `Tech-Stack-Analysis.md` and `phases.md` — are read-only inputs.

## Licence

ForgeOps is one repository under **two licences**, split by path. The nearest
`LICENSE` file governs, and the split is stated here explicitly so nothing has
to be inferred.

| Path                                                                                                                                             | Licence                                                  | SPDX identifier |
| :----------------------------------------------------------------------------------------------------------------------------------------------- | :------------------------------------------------------- | :-------------- |
| Repository root — everything except paths carrying their own `LICENSE` (`backend/`, `frontend/`, `policies/`, `scripts/`, `docs/`, root tooling) | Functional Source License 1.1, Apache 2.0 future licence | `FSL-1.1-ALv2`  |
| `agent/` — the local agent and the CLI                                                                                                           | Apache License 2.0                                       | `Apache-2.0`    |

**Two-year Apache conversion.** Under the FSL, each version of the software
covered by the root `LICENSE` also carries an irrevocable additional grant under
the Apache License, Version 2.0, which becomes effective on the **second
anniversary of the date that version is made available**. Before that date, use
is limited to a Permitted Purpose — any purpose other than a Competing Use, as
defined in the root `LICENSE`.

**Wording that matters.** `FSL-1.1-ALv2` is the registered SPDX identifier for
this licence and is the only form used in package metadata and SBOMs; no
unregistered alias is used anywhere. The FSL is **not** an OSI-approved
open-source licence, so the platform under the root `LICENSE` is
**source-available, converting to Apache 2.0 after two years** — it is not
described as open source. Only the `agent/` subtree, under Apache-2.0, is open
source.

Full texts: root [`LICENSE`](LICENSE) and [`agent/LICENSE`](agent/LICENSE), with
the agent's attribution notice in [`agent/NOTICE`](agent/NOTICE). A contribution
lands under the licence of the directory it touches.
