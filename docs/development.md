# ForgeOps Development Guide

**This document is the project's build-rules home.** The research document instructs an AI
IDE to read a build-rules file (conventionally `rules.md`) before anything else; that file
does not exist in this workspace and nothing was invented to replace it (open question
OQ-18). Until such a file is supplied, the rules an agent or contributor must follow are the
ones written here, and the authoritative sources remain
`.kiro/specs/phase-0-foundation/design.md` plus the four read-only reference documents at
the repository root.

## Read-only reference documents

`AI-Powered-DevOps-Platform-Complete-Technical-Research.md`, `PRD.md`,
`Tech-Stack-Analysis.md`, and `phases.md` are immutable inputs. Never edit, move, rename,
reformat, or lint them. They are excluded from every mutating pre-commit hook and formatter
glob, and they are still scanned by Gitleaks.

## Prerequisites

The Makefile itself needs **GNU make** and a **POSIX shell**. All business logic lives in
`scripts/*.sh` so the same commands work on Linux, macOS, and Windows under Git Bash or
WSL2. On Windows, run `make` from Git Bash or WSL2; `cmd.exe` and PowerShell are not
supported shells for these targets.

| Tool                    | Version                | Notes                                                             |
| :---------------------- | :--------------------- | :---------------------------------------------------------------- |
| GNU make                | 4.x                    | invoked as `make`; BSD make is not supported                      |
| POSIX shell             | `sh` (dash/bash)       | Git Bash or WSL2 on Windows                                       |
| Go toolchain            | 1.26                   | agent; builds run with `CGO_ENABLED=0`                            |
| Python                  | `>=3.13,<3.14`         | backend                                                           |
| `pip-tools`             | 7.6.0 (exact)          | the only lock generator                                           |
| Node.js + pnpm          | pnpm 10+               | frontend                                                          |
| Docker + Docker Compose | Compose 2.24.7 (exact) | long-form `env_file.required` is required                         |
| OpenTofu                | 1.12.5 (exact)         | IaC runner tests; also available in the `tools` profile container |
| `pre-commit`            | pinned by `bootstrap`  | Gitleaks, Ruff, gofmt, Prettier, hygiene                          |

`make bootstrap` verifies the pinned toolchain, including Docker Compose 2.24.7 and
`pip-tools==7.6.0`, and installs git hooks. It never silently rewrites lockfiles.

## Getting started

```sh
make bootstrap      # verify pinned toolchains, install git hooks
make init-env       # creates .env from .env.example only when .env is absent
make up             # starts the default Compose profile and polls readiness
make test           # agent + backend + frontend, single-run
make down
```

`.env.example` is committed and is loaded by every Compose service as a required env file;
`.env` is an optional local override. `scripts/init-env.sh` is idempotent: it never
truncates, merges, or overwrites an existing `.env`, so repeated runs leave the file
byte-identical. Direct `docker compose up -d --wait` works on a fresh clone with no `.env`
present.

Local endpoints (loopback only): frontend `http://localhost:3000`, backend
`http://localhost:8000`, OpenAPI `http://localhost:8000/api/v1/openapi.json`. Before
exposing anything, read `docs/deployment.md`.

## Make targets

`make help` is the default goal and lists every target from its `##` comment. The three
completion gates are `make build`, `make test`, and `make lint`, each of which must succeed
for all three components. `make clean` removes build output and never touches `.env`,
Docker volumes, or lockfiles. Optional Compose profiles are separate commands:
`docker compose --profile tools up -d --wait` and
`docker compose --profile vault up -d --wait`.

## Repository layout rules

- One monorepo, root == workspace root. There is no nested project directory.
- Structural directories required by the authoritative layout but unused in Phase 0 carry
  only a non-code `README.md` or `.gitkeep`. Do not add `doc.go`, an importable
  `__init__.py`, package docstring modules, or exported placeholder types there.
- Three kinds of artifact must never be conflated: a **structural artifact** (tracking file
  only), a **seam** (an interface plus at least one implementation genuinely useful in
  Phase 0), and a **stub** (placeholder code awaiting replacement — forbidden).
- Backend domains do not import each other; cross-domain access and queue-engine imports
  outside `src/core/tasks.py` are banned by lint configuration.

## Dependency and pinning rules

- Exact versions everywhere: `==` pins for Python direct dependencies, exact Go module
  versions, pinned frontend packages, digest-pinned container images, SHA-pinned GitHub
  Actions.
- `backend/pyproject.toml` is the single dependency source of truth. `make lock-backend`
  regenerates `requirements.lock` (runtime) and `requirements-dev.lock` (runtime + dev)
  with `pip-compile --generate-hashes`. Docker installs the runtime lock only, CI installs
  the dev lock, both with `--require-hashes`. CI regenerates both and requires a clean diff.
- Lockfiles are committed: `go.sum`, both Python locks, `pnpm-lock.yaml`, and the
  six-platform `.terraform.lock.hcl` for the null-provider fixture.
- `github.com/tree-sitter/go-tree-sitter` must not appear in the Phase 0 `agent/go.mod`,
  directly or transitively (decision D-1). The deprecated `nhooyr.io/websocket` is likewise
  forbidden; use `github.com/coder/websocket`.

## Testing

| Component | Command                           | Notes                                                                  |
| :-------- | :-------------------------------- | :--------------------------------------------------------------------- |
| Agent     | `go test -race -shuffle=on ./...` | run from `agent/`; needs `CGO_ENABLED=1` and a gcc — see below         |
| Backend   | `pytest`                          | async tests via `pytest-asyncio`; Compose-managed PostgreSQL and Redis |
| Frontend  | `vitest --run`                    | never watch mode                                                       |
| E2E       | Playwright                        | `make e2e` against a built frontend                                    |
| Load      | k6 `/health` smoke                | `make load`, non-gating                                                |

### The race detector on Windows

`-race` requires cgo, and cgo requires a **gcc-compatible** compiler. Visual Studio
Build Tools do _not_ work: they provide `cl.exe`, which rejects the gcc-style flags
cgo passes and fails with `invalid numeric argument '/Werror'`. Install MinGW-w64
instead:

```powershell
winget install --id BrechtSanders.WinLibs.POSIX.UCRT
```

Then make sure its `mingw64\bin` is on `PATH` and run with `CGO_ENABLED=1`. Release
builds are unaffected — they stay `CGO_ENABLED=0` for the six-target static matrix
(§8.2), which is exactly why `tree-sitter` is deferred to Phase 1 (D-1).

Property-based tests use `hypothesis` (Python), `pgregory.net/rapid` (Go), and `fast-check`
(TypeScript), and map one-for-one to the numbered correctness properties P-01 through P-15
in the design appendix. Tests must not use mocks or fake data to manufacture a pass; local
HTTP servers, real Redis, and real PostgreSQL are used instead of vendor networks or real
API keys. Backend coverage above 70 % is a reported goal in Phase 0, not a gate.

## Error and API conventions

Every non-2xx backend response is an RFC 9457 problem document with
`application/problem+json`; body `status` equals the HTTP status and `detail` never carries
secrets. Probe endpoints `/health` and `/health/ready` are unversioned; all product routes
live under `/api/v1`. Full detail is in `docs/api.md`.

Phase 0 has **no general user authentication**. Only `/api/v1/mcp*` and
`POST /api/v1/ai/complete` verify OIDC bearer tokens. Do not add login flows, sessions, or
RBAC in Phase 0 — that is Phase 1 §1.11.

## Licence rules for contributions

The licence a change lands under depends on the directory it touches:

| Path                                                                                      | SPDX identifier | Requirement for contributors                                                                                                         |
| :---------------------------------------------------------------------------------------- | :-------------- | :----------------------------------------------------------------------------------------------------------------------------------- |
| `agent/**`                                                                                | `Apache-2.0`    | Every Go file starts with `// SPDX-License-Identifier: Apache-2.0`; `agent/LICENSE` and a complete `agent/NOTICE` govern the subtree |
| everything else (`backend/`, `frontend/`, `policies/`, `scripts/`, `docs/`, root tooling) | `FSL-1.1-ALv2`  | Covered by the root `LICENSE`; declared in `backend/pyproject.toml` and `frontend/package.json`                                      |

`FSL-1.1-ALv2` is the registered SPDX short identifier and the only form allowed in package
metadata, SPDX headers, and SBOM-visible fields. In prose, call it the Functional Source
License 1.1 with an Apache 2.0 future licence, and describe the non-agent code as
source-available, converting to Apache 2.0 after two years — not as open source. The
descriptive alias that spells out the future licence in the identifier position is not a
registered SPDX identifier and must never appear in metadata.

`agent/NOTICE` is a release artifact: it carries the base project notice plus only upstream
notice texts whose licences require reproduction. No TODO, stub, or prospective attribution
text is permitted, and machine-readable dependency lists belong in the CycloneDX SBOM.

## Pre-commit and security hygiene

Hooks run Gitleaks (secret scanning, also enforced in CI), backend-scoped Ruff and Ruff
format, agent-scoped gofmt and `go vet`, Prettier, and general hygiene checks. The
four-document exclusion applies only to mutating hooks; Gitleaks still scans all four.
Never commit real secrets: `.env.example` contains placeholder values only, and `.env` is
git-ignored.

## Documentation checks

`scripts/check-docs.sh` validates the documentation set: the four `docs/` files exist, the
local-development-only warning appears in the first paragraph of `docs/deployment.md`, the
Phase 0 route names are documented, the health/readiness distinction and RFC 9457 contract
are stated, the licence identifiers are correct, and the non-registered FSL alias is absent.
Run it with `sh scripts/check-docs.sh`.

## Comprehension artifact

`docs/understand-anything/` holds an interactive knowledge graph of this repository — every
analysed file, function and class as a node, with tree-sitter-derived import edges, fourteen
architectural layers and a dependency-ordered guided tour. It is generated by
[Understand-Anything](https://github.com/Egonex-AI/Understand-Anything) pinned at **v2.9.0**
(commit `f08763d11d0202a8a8f52b5dedda6d1b2e2ebac8`), installed at user level outside this
repository. `docs/understand-anything/README.md` documents the layout, the exclusions, and
which parts of the graph are machine-derived versus authored.

Open it with Node.js ≥ 18. The viewer serves the graph read-only from local disk and makes no
LLM calls and no network requests once the package is cached:

```sh
npx https://github.com/Egonex-AI/Understand-Anything/releases/download/v2.9.0/understand-anything-viewer.tgz docs/understand-anything
```

Regenerate it in two steps from the repository root. Step 1 is the plugin's deterministic file
inventory; step 2 resolves imports, extracts structure, assembles the graph and validates it:

```sh
node "$HOME/.understand-anything/Understand-Anything-2.9.0/understand-anything-plugin/skills/understand/scan-project.mjs" \
     . docs/understand-anything/.ua/intermediate/scan-result.raw.json
node docs/understand-anything/build-graph.mjs
```

On Windows PowerShell the first path is `"$HOME\.understand-anything\…"`; everything else is
identical.

`build-graph.mjs` writes nothing when validation finds an issue, and exits non-zero when the
inventory is empty or when any analysed path has no rule in `semantic-overlay.json` — so a new
top-level directory forces a decision instead of landing in a default bucket. Update
`semantic-overlay.json` in the same commit as source changes that alter what a directory is
for.

`.kiro/steering/learning-journal.md` requires regenerating this artifact whenever a group of
task leaves completes, and recording the date in the log at the bottom of
`docs/understand-anything/README.md`. Step 1's scanner prefers `git ls-files -co
--exclude-standard` for its inventory, which is why the exclusion list is short — files git
already ignores never enter it.
