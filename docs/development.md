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

## Local development on Windows

Everything in this section was learned the expensive way and is written down so it is not
learned again. The rule it all reduces to: **Python is launched from PowerShell, never from Git
Bash. Bash runs the `.sh` check scripts and nothing else.**

### Tracked entry points

There is one tracked script per job. None of them is optional scaffolding; each replaced an
untracked helper that was being rewritten from memory every session.

| Script                           | Job                                                                |
| :------------------------------- | :----------------------------------------------------------------- |
| `scripts\local-env.ps1`          | dot-source to get the host-side integration environment            |
| `scripts\pytest.ps1`             | the only entry point for pytest; loads `local-env.ps1` first       |
| `scripts\leaf-gate.ps1`          | every fast whole-repo static check, run per leaf before committing |
| `scripts\secret-gate.ps1`        | the mandatory pre-push secret gate, all stages                     |
| `scripts\pre-commit-run.ps1`     | the real hook set from `.pre-commit-config.yaml`                   |
| `scripts\install-pre-commit.ps1` | install `pre-commit` hash-verified from `requirements-tools.lock`  |

### Git Bash and native Windows executables

Git Bash rewrites values that look like absolute POSIX paths when it launches a **native**
Windows executable. `.env` carries `API_PREFIX=/api/v1`, so any shell that sources `.env` and
then runs `python.exe` delivers `API_PREFIX=C:/Program Files/Git/api/v1`. Because
`backend/src/core/config.py` sets `env_file=None`, settings come from the OS environment only;
`create_app()` then registers a route whose path does not start with `/` and Starlette asserts.
`scripts/check-route-auth.py` surfaced that as "could not build the app from
`src.main:create_app`", which reads like a repository defect and is not one — the same command
from PowerShell passes.

Two MSYS switches look interchangeable and are not:

| Variable              | Governs                | Effect here                                                                              |
| :-------------------- | :--------------------- | :--------------------------------------------------------------------------------------- |
| `MSYS2_ENV_CONV_EXCL` | **environment values** | `='*'` stops `API_PREFIX` being rewritten. This is the one that fixes the problem above. |
| `MSYS_NO_PATHCONV`    | **command arguments**  | `=1` stops argument rewriting — and breaks `check-chokepoint.sh`, which passes           |
|                       |                        | `scripts/chokepoint_graph.py` to a native `python.exe` that then gets an unconverted     |
|                       |                        | `/c/...` and reports `can't open file 'C:\c\IMP\...'`.                                   |

Adding the second one alongside the first on a guess cost a full extra lap. Neither is needed
now: the PowerShell entry points above never create an MSYS boundary for Python to cross.
`MSYS_NO_PATHCONV=1` is still needed for `docker run -v` and for `git show <ref>:<path>` when
those are invoked **from bash**.

### PowerShell 5.1 reads UTF-8 as ANSI

`Get-Content` without `-Encoding UTF8` decodes a UTF-8 file using the ANSI code page, and a
child process's stdout is decoded using the **console** code page. The same en dash therefore
becomes `â€"` one way and `Γ` the other, so a line compared across the two paths is unequal to
itself. This produced a false "NEW secret shape" verdict in `secret-gate.ps1` stage 2b, and it
truncates tool output when a `§` decodes to a control character. Two defences, both used by
every script here:

```powershell
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false
Get-Content -LiteralPath $f -Encoding UTF8
```

**Never edit a `.md` file through PowerShell string replacement.** The mangling is silent and
affects every en dash in the file.

### The `.env` and `ALEMBIC_DATABASE_URL` trap

`make init-env` copies `.env.example` to `.env`. `.env.example` is **Compose-targeted**: its
DSNs name the Compose services `postgres:5432` and `redis:6379`, which resolve only on the
Compose network. `.env` is also where `make init-ca` writes the development CA key, so a
host-side developer has a genuine reason to load it — and loading it wholesale puts
`ALEMBIC_DATABASE_URL=...@postgres:5432` into the OS environment.

`backend/alembic/env.py` prefers `ALEMBIC_DATABASE_URL` over `DATABASE_URL` **by design**
(§6.4: the migrator role owns the schema, the application role must not). `os.environ` outranks
anything a fixture configures. So every DB-backed test errors at setup, inside
`schema_at_head`'s `alembic downgrade base`, with `socket.gaierror: [Errno 11001] getaddrinfo
failed` — a failure that names neither the variable nor the file that set it. Nothing about this
is Windows-specific; a Linux developer who sources `.env` hits it identically.

Two mechanisms address it, and the precedence is deliberately **not** one of them:

1. `scripts/local-env.ps1` loads `.env` in full and then overrides the endpoint variables
   unconditionally. An allow-list of "safe" keys was rejected: a new key in `.env.example` would
   silently escape it. A guard afterwards reads the Compose file's own service names and fails if
   any exported value still points at one — which is how `OPA_URL=http://opa:8181` was found, on
   the guard's first run.
2. `alembic/env.py` catches `socket.gaierror` and re-raises naming the host, the variable it came
   from, and the remedy. Credentials are never printed, only which variable was chosen.

### Test container ports

Five containers, all published on loopback only, all named `forgeops-test-*`. Ports are
deliberately non-default so a local development stack and the test stack can coexist. The
PostgreSQL container runs with `POSTGRES_HOST_AUTH_METHOD=trust`, so the local DSNs carry no
password — there is nothing to leak.

| Container                 | Image                                 | Host port | Used as                     |
| :------------------------ | :------------------------------------ | :-------- | :-------------------------- |
| `forgeops-test-pg`        | `pgvector/pgvector:pg17`              | `55432`   | `forgeops_test` database    |
| `forgeops-test-redis`     | `redis/redis-stack-server:7.4.0-v3`   | `56379`   | db 0 runtime, db 1 tests    |
| `forgeops-test-cerbos`    | `ghcr.io/cerbos/cerbos:0.54.0`        | `53592`   | `CERBOS_URL`                |
| `forgeops-test-ak-server` | `ghcr.io/goauthentik/server:2026.5.6` | `9000`    | OIDC issuer                 |
| `forgeops-test-ak-worker` | `ghcr.io/goauthentik/server:2026.5.6` | —         | Authentik background worker |

Docker Desktop stopping between sessions leaves all five `Exited (255)`. They restart with
state intact:

```powershell
docker start forgeops-test-pg forgeops-test-redis forgeops-test-cerbos `
             forgeops-test-ak-server forgeops-test-ak-worker
```

### Running the suite: the two-chunk split

The whole backend suite passes 1,476 tests and takes over an hour, which exceeds the shell
timeout available to an agent session. It is therefore run in two chunks, and only **once per
task group** — never per leaf:

```powershell
scripts\pytest.ps1 -q tests/unit tests/meta tests/property
scripts\pytest.ps1 -q tests/integration
```

Per leaf, run the leaf's own tests plus the mandatory selection, plus `leaf-gate.ps1`:

```powershell
scripts\pytest.ps1 -q tests/unit/test_<leaf>.py
scripts\pytest.ps1 -q -m mandatory --report-log=../.evidence/mand.jsonl
backend\.venv\Scripts\python.exe scripts\check-no-skips.py .evidence\mand.jsonl
powershell -File scripts\leaf-gate.ps1
```

`check-no-skips.py` consumes a `--report-log` JSONL or `go test -json` events, so it is a
property of a test **run** and cannot be part of the static gate.

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

### The pre-push gate — three stages, not two

`.kiro/steering/secret-safety.md` mandates a scan before any push. Run **all three** stages;
each catches something the others cannot, and stage 3 is the one that has actually blocked a
push in this repository.

1. **`gitleaks`** over the working tree and the staged change:

   ```sh
   gitleaks detect  --no-banner --redact
   gitleaks protect --staged --no-banner --redact
   ```

   Use the pinned Docker image if the binary is absent; never skip the scan for want of a
   binary. `gitleaks` scores **likelihood**. The rules below are about **shape**, which is a
   different question, so a clean `gitleaks` is not a clearance.

2. **High-risk pattern grep, applied to two units.** The pattern list is in
   `secret-safety.md`. Grep (a) the **added** lines of the diff, and (b) the **full content**
   of every file in the push. (b) matters because a shape sitting in a file's unchanged region
   never appears in a diff and is still being published. Classify (b)'s hits against the file
   as it exists on the remote: a line already on the remote is pre-existing, and a line that
   is not is new and blocks.

3. **Every commit in the push range, separately.** For each commit in
   `origin/<branch>..HEAD`, grep that commit's own added lines.

   ```sh
   for c in $(git rev-list origin/<branch>..HEAD); do
     git show "$c" --format= --unified=0 | grep -E '^\+' | grep -vE '^\+\+\+ ' | grep -niE "<patterns>"
   done
   ```

   **Why this stage exists.** GitHub secret scanning and GitGuardian read each pushed commit
   individually, not the range's net diff. A range whose cumulative diff is clean can still
   contain an intermediate commit that introduced a shape and a later one that removed it —
   and pushing the range publishes the intermediate blob. `REVIEW-PHASE-0.md` Pass 2 recorded
   exactly that: GitGuardian went red on a pull-request head because an earlier commit still
   carried a literal the tip had already removed.

   If stage 3 blocks and the commits are **unpushed**, rebuild them so no commit ever
   contained the shape, and prove it with `git diff <old-tip> <new-tip>` returning **empty** —
   history changed, content did not. That repair is trivial before a push and becomes a
   force-push over published history after one.

Two implementation notes if you script this on Windows: export `MSYS_NO_PATHCONV=1`, because
MSYS rewrites `git show <ref>:<path>` and the baseline lookup then silently returns nothing;
and normalise line endings before comparing, because `git show` emits the blob at LF while the
working copy is CRLF.

If any stage reports a hit you did not introduce and cannot attribute, **stop and ask**. Do not
grant yourself a "it is only prose" exemption — that puts a human in the loop for every future
hit, which is a convention rather than a mechanism. See `docs/LEARNING-JOURNAL.md` chapter 9,
pattern L.

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
