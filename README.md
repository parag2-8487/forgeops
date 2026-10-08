# ForgeOps

AI-powered DevOps automation platform.

- **Repository:** <https://github.com/parag8487/ForgeOps>
- **Owner:** `parag8487`
- **Go module path (agent):** `github.com/parag8487/ForgeOps/agent`
- **Status:** **Phase 1 substantially complete — 12 of its 14 completion criteria are met.**
  Phase 0, foundation and project scaffolding, closed with 108 task leaves and all **18** of
  its criteria. Phase 1, MVP core: analysis, generation and approval, has all 166 task leaves
  implemented, 13 of 14 criteria met, and **31** property tests each carrying a verified
  negative control. It is `in-progress`, not `completed`, because **two criteria are
  outstanding and were found on 2026-08-21 to have been recorded as met on evidence that does
  not exist:**

  - **C10 — end-to-end journey: not met.** The record described a 13-step journey run against
    built backend and frontend images with a paired agent container. What exists is a 20-line
    shell smoke test; the workflow builds only the frontend and starts no agent. Zero of the 13
    steps are implemented.
  - **C11 — test coverage ≥ 70 %: not met.** Of the four gates the record named, three do not
    exist — the backend `--cov-fail-under` setting, `scripts/check-coverage.sh`, and any
    frontend coverage thresholds. A unit test asserted the backend gate's _absence_.

  Both are corrected in [`PROGRESS.md`](PROGRESS.md) with the specifics, and the phase status
  reflects them. That is **2 of the 6 phases** in [`phases.md`](phases.md) substantially done;
  **Phases 2–5 are not started.** The work is on the `phase-1-implementation` branch and is not
  yet merged into `main`.

## Running on Linux / Ubuntu (1-Click Master Setup)

For Linux systems — especially **fresh installations such as college lab computers** running
Ubuntu 20.04, 22.04 or 24.04 that have **no Docker**, an **older Python**, or none of the build
tools — use the master setup script. It installs everything the stack needs and then starts and
verifies it.

```bash
./setup.sh
```

That is the whole install. No arguments are required, and none of the steps below need to be run
by hand.

### Before you start

| Requirement          | Why                                        | If it is missing                                                                      |
| :------------------- | :----------------------------------------- | :------------------------------------------------------------------------------------ |
| **~10 GB free disk** | four images plus their base layers         | the script stops with the measured free space rather than failing deep inside a build |
| **Internet access**  | Docker's repository, PyPI, and base images | —                                                                                     |
| **sudo rights**      | Docker and system packages                 | —                                                                                     |
| **~4 GB RAM**        | Authentik alone wants about 1 GB           | under 2.5 GB with no swap, a 2 GB swapfile is created automatically                   |

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
3. **Docker permissions without a logout.** Adds your user to the `docker` group and applies socket
   permissions in place, so the shell you are already in can talk to the daemon immediately. No
   reboot, no re-login.
4. **Daemon startup on any init system.** `systemctl` where systemd is present, `service` otherwise
   — covering WSL2 and container hosts as well as a normal desktop.
5. **Python.** Installs **Python 3.13** and `python3.13-venv` from the deadsnakes PPA. This is the
   interpreter the backend pins (`requires-python = ">=3.13,<3.14"`). If the PPA is blocked by a
   campus network, it falls back to the best host Python ≥ 3.10 for the launcher's own tooling and
   says so — the stack still runs, because the backend's real interpreter comes from its image.
6. **Low-memory protection.** Under 2.5 GB available RAM with no swap, it creates a 2 GB swapfile so
   a container build cannot be OOM-killed.
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

### 4. Zero-Trust Local Agent & Governance Chokepoint

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
