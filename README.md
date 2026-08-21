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

## What this is, and how to run it

ForgeOps analyses a codebase, generates deployment artifacts for it — Dockerfiles,
Kubernetes manifests, OpenTofu HCL — with an LLM, and puts every generated change behind a
policy check and a human approval gate before a local agent applies it to disk. Three
components: a **Go agent** that runs next to your code and is the only thing that writes to
it, a **FastAPI backend** that does the analysis, generation and governance, and a
**Next.js** shell.

**Prerequisites.** GNU make and a POSIX shell — on Windows use Git Bash or WSL2, as
PowerShell and `cmd.exe` are not supported for these targets — plus Docker with Compose
2.24.7, Go 1.26, Python 3.13 and pnpm 10. `make bootstrap` verifies the pinned versions
rather than assuming them.

```sh
make bootstrap   # verify the pinned toolchain, install the git hooks
make up          # start the default Compose profile, polling until readiness answers
```

`make up` returns once the backend answers its readiness probe, so when it exits the stack
is genuinely serving:

| Surface                    | URL                                  |
| :------------------------- | :----------------------------------- |
| Readiness probe            | <http://localhost:8000/health/ready> |
| API documentation, Swagger | <http://localhost:8000/api/v1/docs>  |
| Frontend shell             | <http://localhost:3000>              |

`make down` stops the containers and preserves the volumes. Everything else — the Compose
profiles, the seeded development credentials, the test containers, the full target list —
is in [`docs/development.md`](docs/development.md) and is deliberately not duplicated here.

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
