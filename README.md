# ForgeOps

AI-powered DevOps automation platform.

- **Repository:** <https://github.com/parag8487/ForgeOps>
- **Owner:** `parag8487`
- **Go module path (agent):** `github.com/parag8487/ForgeOps/agent`
- **Status:** Phase 0 — foundation and project scaffolding, complete. All 18 completion
  criteria carry evidence in [`PROGRESS.md`](PROGRESS.md); the work is on the
  `phase-0-implementation` branch and is not yet merged into `main`.

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
