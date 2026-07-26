# ForgeOps

AI-powered DevOps automation platform.

- **Repository:** <https://github.com/parag8487/ForgeOps>
- **Owner:** `parag8487`
- **Go module path (agent):** `github.com/parag8487/ForgeOps/agent`
- **Status:** Phase 0 — foundation and project scaffolding, in progress.

The monorepo lives directly in the repository root: `agent/` (Go local agent and
CLI), `backend/` (FastAPI platform), `frontend/` (Next.js shell), `policies/`
(OPA policy), `scripts/`, `docs/`, and `.github/workflows/`. The four reference
documents at the root — `AI-Powered-DevOps-Platform-Complete-Technical-Research.md`,
`PRD.md`, `Tech-Stack-Analysis.md` and `phases.md` — are read-only inputs.

## Licence

ForgeOps is one repository under **two licences**, split by path. The nearest
`LICENSE` file governs, and the split is stated here explicitly so nothing has
to be inferred.

| Path | Licence | SPDX identifier |
|:---|:---|:---|
| Repository root — everything except paths carrying their own `LICENSE` (`backend/`, `frontend/`, `policies/`, `scripts/`, `docs/`, root tooling) | Functional Source License 1.1, Apache 2.0 future licence | `FSL-1.1-ALv2` |
| `agent/` — the local agent and the CLI | Apache License 2.0 | `Apache-2.0` |

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
