# REVIEW-PHASE-0 — merge-gating review record

> Untracked working-tree file. Do not commit. Append-only: earlier sections are never
> rewritten or truncated.

## Header

| Field | Value |
| :--- | :--- |
| Repository | `parag8487/ForgeOps` |
| PR | [#1](https://github.com/parag8487/ForgeOps/pull/1) |
| Base branch | `main` |
| Base SHA | `d16eb0ed644f3a995698340c74f5e4b405598db9` |
| Head branch | `phase-0-implementation` |
| Head SHA (local) | `2a61dc6a151e5c0d4a330151ebd624fe7e1bb9dc` |
| Head SHA (`origin/phase-0-implementation`) | `2a61dc6a151e5c0d4a330151ebd624fe7e1bb9dc` — matches local |
| `origin/main` | `d16eb0ed644f3a995698340c74f5e4b405598db9` — matches local `main` |
| Working tree | 1 untracked file: `.kiro/steering/agent-autonomy.md`. No staged or unstaged tracked modifications at review start. |
| Docker availability | **Available.** `docker version --format '{{.Server.Version}}'` → `29.6.2`; `docker info --format '{{.ServerVersion}}'` → `29.6.2` |
| Reviewer model | `claude-opus-5` |
| Review date | 2026-07-30 (IST) |
| Review mode | READ-ONLY. No source edits, no staging, no commits, no push, no merge, no tags, no GitHub settings changes, no history rewrite, no file deletion. |

### Commands run to establish the header

```
git rev-parse HEAD                            -> 2a61dc6a151e5c0d4a330151ebd624fe7e1bb9dc
git rev-parse main                            -> d16eb0ed644f3a995698340c74f5e4b405598db9
git rev-parse origin/main                     -> d16eb0ed644f3a995698340c74f5e4b405598db9
git rev-parse origin/phase-0-implementation   -> 2a61dc6a151e5c0d4a330151ebd624fe7e1bb9dc
git status --porcelain=v1 --branch            -> ## phase-0-implementation...origin/phase-0-implementation
                                                 ?? .kiro/steering/agent-autonomy.md
docker version --format '{{.Server.Version}}' -> 29.6.2
```

### Steering files read first, as required

- `.kiro/steering/secret-safety.md` — read. Mandatory pre-push gate, synthetic-token
  rules, redaction rules. No push is performed in this review, so the gate is applied
  only as a review criterion, not as an action.
- `.kiro/steering/agent-autonomy.md` — read. File-preservation rules honoured: nothing
  deleted, moved, renamed or truncated. This file is currently **untracked** in the
  working tree (see Pass 1 findings).

---

## Passes not yet done

- [ ] Pass 1 — Establish exact review scope (diff inventory, classification, hygiene)
- [ ] Pass 2 — Requirements and traceability (design.md, tasks.md, 18 criteria, 0.1–0.9, P-01–P-15, decisions)
- [ ] Pass 3 — Security review
- [ ] Pass 4 — Backend review
- [ ] Pass 5 — Go agent review
- [ ] Pass 6 — Frontend review
- [ ] Pass 7 — Infrastructure, CI and supply-chain review
- [ ] Pass 8 — Testing quality review
- [ ] Pass 9 — Run validation
- [ ] Final — Consolidated findings, validation matrix, verdict

---


## Pass 1 — Exact review scope, inventory and repository hygiene

### Commands run

```
git merge-base main phase-0-implementation      -> d16eb0ed644f3a995698340c74f5e4b405598db9
git rev-list --count main..phase-0-implementation -> 13
git diff --stat main...phase-0-implementation   -> 271 files changed, 29182 insertions(+), 322 deletions(-)
git diff --name-status main...phase-0-implementation  (full list captured)
git log --oneline main..phase-0-implementation  (13 commits, listed below)
git ls-files | Measure-Object -Line            -> 287 tracked files at head
git ls-tree -r -l phase-0-implementation | sort -desc  (largest 15 blobs inspected)
git check-ignore -v frontend/.env.local frontend/tsconfig.tsbuildinfo "backend;W" .ruff_cache .pytest_cache backend/.venv
```

Base is a true ancestor (`merge-base == main`), so `main...head` and `main..head` describe
the same change set. No merge commits; 13 linear commits.

### Commit list (head → base)

```
2a61dc6 Fix the ci / supply GoReleaser step, relabel synthetic test tokens, track the secret-safety rule
8472d7e Record verification of the complete rc3 asset set, off-runner
2be59cb Record the documentation CI gate and drop the stale HEAD reference
8f2cee8 Close out Phase 0: criteria evidence, decisions D-20 to D-22, docs
2e35737 Discard the pipe Close error returns explicitly for errcheck
7217868 Fix the OpenTofu runner output race by owning its pipes
7f5213f Verify SLSA attestations from a Sigstore bundle, not a Rekor search
da661c8 Deliver SLSA provenance via cosign attest-blob and unblock criterion 16
a4581a6 Fix CI round four: the interleaved-streams race and a time-dependent lock gate
0cfaefc Fix CI round three: cross-platform lint, Python vulns, pip-tools compatibility
e80797f Fix CI round two: exec bits, shell dialect, toolchain versions, vulns, ESLint 10
7d0a5b8 Fix the first CI run: invalid action pins, missing scope, unformatted files
f5ad2b0 Phase 0: agent, backend, frontend, MCP gateway, model routing and supply chain
```

### Classification of the 271 changed paths

| Class | Count (approx.) | Notes |
| :--- | :--- | :--- |
| Production source — Go agent | 34 `.go` non-test files under `agent/` | `cmd/agent`, `internal/{app,config,connection,docker,fileops,git,iac,k8s,logging,mcp,scanner,selfupdate,telemetry}` |
| Production source — backend | 45 `.py` under `backend/src/` + `backend/alembic/` | `core`, `mcp`, `ai`, `analysis`, `projects` |
| Production source — frontend | 27 `.ts/.tsx/.css` under `frontend/{app,components,lib,stores}` | |
| Tests | 21 Go `_test.go`; 22 backend test modules; 8 frontend `__tests__`; 1 Playwright `e2e`; 1 k6 `load`; 5 `scripts/tests/*` | |
| Fixtures | `agent/testdata/plan-sample.json`, `agent/testfixtures/tofu-null/{main.tf,.terraform.lock.hcl}` | Legitimately committed |
| Scripts | 24 under `scripts/` incl. `scripts/lib/pip-compile.sh` | |
| Workflows / config | `.github/workflows/{ci,release}.yml`, `docker-compose.yml`, `Makefile`, `.gitattributes`, 3 `Dockerfile`, 3 `.dockerignore`, `agent/{.golangci.yml,.goreleaser.yaml}`, frontend tooling configs | |
| Migrations | `backend/alembic/{env.py,script.py.mako,versions/0001_initial.py}`, `alembic.ini` | Single revision, as designed |
| Policies | `policies/mcp/{gateway.rego,gateway_test.rego}` | |
| Documentation / specs | `README.md`, `PROGRESS.md`, `docs/*.md`, `.kiro/specs/phase-0-foundation/tasks.md`, `tasks.meta.json`, `.kiro/steering/secret-safety.md`, `frontend/public/README.md` | |
| Licences / notices | `agent/NOTICE` (modified) | Root `LICENSE`, `agent/LICENSE` already on `main` |
| Locks | `agent/go.sum`, `backend/requirements.lock`, `backend/requirements-dev.lock`, `frontend/pnpm-lock.yaml`, `agent/testfixtures/tofu-null/.terraform.lock.hcl`, `agent/go.mod`, `frontend/package.json` | |
| Generated output committed | **none found** | |
| Deletions | 24 `.gitkeep` placeholders replaced by real files | Correct: the deletion is the placeholder being superseded |

### Hygiene verdict (Pass 1)

Tracked-junk scan is **clean**. Verified by `git ls-files` filter for
`tsbuildinfo|.env.local|__pycache__|.venv|ruff_cache|pytest_cache|hypothesis|test-results|playwright-report|node_modules|*.log|dist/|*.exe|*.zip|*.tar.gz`
— zero matches other than `.gitkeep` files (which are structural, not junk).
`git check-ignore -v` confirms `frontend/.env.local`, `frontend/tsconfig.tsbuildinfo`,
`.ruff_cache`, `.pytest_cache`, `backend/.venv` are all ignored and untracked.
Largest tracked blobs are all legitimate (`design.md`, `pnpm-lock.yaml`, the four
read-only root documents, hash locks, `go.sum`, big test modules). No binaries, no
release output, no editor state.

### Pass 1 findings

**[P2] `.kiro/steering/agent-autonomy.md` is untracked, so the agent-behaviour rules are not part of the PR**
- Evidence: `git status --porcelain=v1` → `?? .kiro/steering/agent-autonomy.md`. The sibling
  rule file `.kiro/steering/secret-safety.md` *is* added by this PR
  (`git diff --name-status` → `A .kiro/steering/secret-safety.md`), and commit `2a61dc6`
  is titled "…track the secret-safety rule". Only one of the two steering files was tracked.
- Impact: the file-preservation and autonomy rules that the project relies on exist only in
  one developer's working tree. After merge, a fresh clone has `secret-safety.md` but not
  `agent-autonomy.md`, so the guardrails are silently asymmetric and unreviewable.
- Required fix: `git add .kiro/steering/agent-autonomy.md` and include it in the PR, or
  state explicitly in `PROGRESS.md` that it is intentionally local-only.
- Validation: `git ls-files .kiro/steering/` lists both files; `scripts/check-hygiene.sh`
  could assert that every file under `.kiro/steering/` is tracked.

**[P3] `.gitattributes` marks all four lockfiles `-diff`, which hides supply-chain-relevant changes from PR review**
- Evidence: `.gitattributes:41-44` — `pnpm-lock.yaml -diff linguist-generated`,
  `requirements.lock -diff linguist-generated`, `requirements-dev.lock -diff linguist-generated`,
  `go.sum -diff linguist-generated`. Confirmed effective: `git diff --stat` renders
  `agent/go.sum | Bin 0 -> 30503 bytes` instead of a textual diff.
- Impact: a dependency substitution, an added index URL, or a changed hash inside a lockfile
  is invisible in the GitHub diff and in `git diff`. For a repository whose stated posture is
  supply-chain custody (design §8.1, §14.1), suppressing the diff on exactly the files that
  encode dependency identity removes the human review layer. Gitleaks still scans content, so
  this is reviewability rather than secret exposure.
- Required fix: use `linguist-generated` alone (collapses the diff by default in the GitHub UI
  but keeps it expandable and keeps `git diff` textual). Drop `-diff`.
- Validation: `git diff main...HEAD -- agent/go.sum` produces a textual diff.

**[P3] Redundant `.gitkeep` files remain in directories that now contain real code**
- Evidence: `git ls-files` shows 16 remaining `.gitkeep` entries, including
  `.github/workflows/.gitkeep` (directory now holds `ci.yml`, `release.yml`),
  `agent/internal/iac/.gitkeep` (holds 10 Go files), `backend/src/core/.gitkeep`,
  `backend/src/mcp/.gitkeep`, `backend/src/projects/.gitkeep`,
  `backend/src/ai/{routing,rate_limit}/.gitkeep`, `backend/tests/{unit,property,integration}/.gitkeep`,
  `backend/alembic/versions/.gitkeep`, `docs/.gitkeep`. The PR *did* delete 24 other
  `.gitkeep` files in the same situation, so the cleanup is inconsistent rather than deliberate.
- Impact: cosmetic only. But two of them — `backend/src/ai/cache/.gitkeep` and
  `backend/src/ai/keys/.gitkeep` — mark directories that design §11.8 and §11.7 name as the
  homes of `ai/cache/tiered.py` and `ai/keys/resolver.py`, while the implementation put those
  modules at `ai/routing/cache.py` and `ai/routing/keys.py`. That is a real design/implementation
  path divergence worth confirming (carried into Pass 4).
- Required fix: none required for merge. Either delete the superseded placeholders or record
  the path divergence in `PROGRESS.md`.
- Validation: `scripts/check-structure.sh` behaviour on `.gitkeep` presence.

**[P3] Untracked empty directory `backend;W` in the working tree**
- Evidence: `Get-ChildItem -Recurse -Force "backend;W"` → 0 entries;
  `git check-ignore -v "backend;W"` → no match (not ignored); it does not appear in
  `git status` because Git does not report empty directories.
- Impact: none on the PR — nothing is tracked. It is local scratch from a mistyped shell
  redirection. Recorded because it is not ignored, so a future file created inside it would
  show up as untracked noise.
- Required fix: none in this PR. The reviewer did not delete it (read-only review).
- Validation: `git status --porcelain --ignored` after any file is added there.

### Checks NOT run in Pass 1

- `gh pr view 1` / PR metadata from the GitHub API — **not attempted yet**; deferred to Pass 9
  so a single authenticated-CLI availability result is recorded once.

---


## Pass 2 — Secret scanning, GitGuardian incident 35267706, PR check state

### Commands run

```
gh pr view 1 --json ...            -> OPEN, not draft, mergeable=MERGEABLE,
                                      mergeStateStatus=UNSTABLE, reviewDecision="" (none),
                                      271 files, +40523/-322 (GitHub counts locks textually)
gh pr checks 1                     -> GitGuardian Security Checks = FAIL; all 9 ci jobs pass;
                                      CodeRabbit = "Review skipped: 242 files exceed the limit of 150"
gh api .../check-runs              -> GitGuardian output.title = "1 secret uncovered!",
                                      summary = "1 secret were uncovered from the scan of 13 commits"
docker run --rm -v ${PWD}:/repo zricethezav/gitleaks:v8.30.1 detect --source=/repo \
    --no-banner --redact --log-opts="main..phase-0-implementation"
                                   -> 13 commits scanned, "no leaks found" (exit 1 is the
                                      PowerShell native-stderr artefact, not a finding)
docker run --rm -v ${PWD}:/repo zricethezav/gitleaks:v8.30.1 detect --source=/repo \
    --no-banner --redact           -> 14 commits scanned, "no leaks found"
git show 2a61dc6 -- backend/tests/property/test_p09_rfc9457.py backend/tests/unit/test_errors.py
                                   -> inspected with every alphanumeric masked to `x`; no token
                                      material was rendered at any point
```

### GitGuardian incident 35267706 — verified

The incident is real, is *not* a live credential, and is *not* fixed from GitGuardian's
point of view.

- Location, from the fixing commit's own message and confirmed by the masked diff:
  `backend/tests/property/test_p09_rfc9457.py:107` and three sites in
  `backend/tests/unit/test_errors.py`.
- Detector: generic bearer-token detector, triggered by a `Bearer <dot-separated>` clause.
- Redacted shape of the pre-fix literal, derived by masking every alphanumeric character:
  `[20 chars].[7 chars].[9 chars]` — three dot-separated all-alphanumeric segments, total
  38 characters. A real JWS header segment alone is ≈36 base64url characters and begins
  `eyJ`; a 20-character first segment cannot base64url-decode to a JSON header. So this was
  a **JWT-shaped placeholder, never a signed or usable token**. No rotation is required.
  It did, however, violate `.kiro/steering/secret-safety.md` ("Never use a value that
  resembles a real provider token format").
- Fix at HEAD is genuine and non-vacuous. `backend/src/core/logging.py:23` —
  `re.compile(r"Bearer\s+[A-Za-z0-9\-._~+/]+=*", re.IGNORECASE)` — the character class
  includes `-` and `.`, so it matches the whole replacement clause
  `Bearer test-only-not-a-real-secret.not-a-jwt` present at
  `backend/tests/property/test_p09_rfc9457.py:106-107`. The assertions were retargeted from
  the literal `"Bearer"` to the new self-labelling substring, so they still fail if
  redaction stops working. (Independently re-verified in Pass 8.)

### Findings

**[P1] The GitGuardian gate is red on the PR head and cannot be cleared by merging as-is**
- Evidence: `gh api repos/parag8487/ForgeOps/commits/2a61dc6.../check-runs` →
  `{"conclusion":"failure","name":"GitGuardian Security Checks","title":"1 secret uncovered!",`
  `"summary":"1 secret were uncovered from the scan of 13 commits in your pull request."`
  `gh pr view 1` → `mergeStateStatus: "UNSTABLE"`. GitGuardian scans the **commit range**,
  not the head tree, so commit `f5ad2b0` still carries the JWT-shaped literal even though
  `2a61dc6` removed it from the tip.
- Impact: a merge commit or rebase merge puts `f5ad2b0` on `main` permanently, so the
  incident stays open on the default branch forever and the repository's own
  "secret scanning gate" control (design §14.1) is left in a failing state at the exact
  moment Phase 0 declares itself complete. Whether this is a *credential* risk: no — the
  value is provably not a usable token (shape analysis above). It is a **gate-integrity and
  history-hygiene** blocker, not an exposure.
- Required fix (smallest correct remediation): **squash-merge PR #1.** A squash produces one
  commit whose tree is the clean HEAD tree, so the offending blob never reaches `main`.
  If the linear history must be preserved instead, the alternative is to resolve the
  GitGuardian incident as a false positive in the dashboard and record that decision in
  `PROGRESS.md`; do not rewrite the branch history for this, since the value is not a real
  credential and a force-push needs explicit owner approval.
- Validation: after squash-merge, `gh api repos/parag8487/ForgeOps/commits/<main-sha>/check-runs`
  shows GitGuardian success, and
  `docker run … gitleaks detect --log-opts="<base>..main"` still reports no leaks.

**[P2] No human or automated code review has actually looked at this PR**
- Evidence: `gh pr view 1 --json reviewDecision` → `""` (no review). CodeRabbit status
  description: `Review skipped: 242 files exceed the limit of 150`.
- Impact: a 271-file, 29 k-line foundation PR is merging on CI signal alone. Every finding in
  this document was invisible to the project's configured review automation.
- Required fix: none mechanical. Either accept this manual review as the record, or split
  future phase PRs so CodeRabbit's 150-file limit is not exceeded.
- Validation: `gh pr view 1 --json reviewDecision` is non-empty before merge.

### Secret-scan verdict (Pass 2)

- Working tree and full reachable history (14 commits): **gitleaks v8.30.1 reports no leaks.**
- PR commit range (13 commits): **gitleaks reports no leaks**; GitGuardian's single finding is
  the JWT-shaped test placeholder characterised above, which is not a usable credential.
- No `.env`, `*.pem`, `*.key`, `*.p12`, `*.pfx`, `id_rsa*` or `credentials.json` is tracked
  (verified in Pass 1 by `git ls-files` filter and `git check-ignore`).
- **No secret value was rendered at any point in this review.**

### Checks NOT run in Pass 2

- `gitleaks protect --staged` — not applicable: nothing is staged, and this review stages
  nothing.
- GitGuardian dashboard incident 35267706 itself — **not accessible**; no GitGuardian API
  token is available in this environment. The incident was characterised from the commit
  message plus the masked git diff instead.

---


## Pass 3 — CI, release and supply-chain review

Read in full: `.github/workflows/ci.yml` (300 lines), `.github/workflows/release.yml` (297
lines), `.pre-commit-config.yaml`, `.gitattributes`, `.gitignore`.

### What is correct and verified

- **Action pinning.** Every `uses:` in both workflows is pinned to a full 40-hex commit SHA
  with the version in a trailing comment: `actions/checkout@11bd719`, `setup-go@d35c59a`,
  `setup-python@a26af69`, `setup-node@49933ea`, `pnpm/action-setup@fe02b34`,
  `dorny/paths-filter@de90cc6`, `opentofu/setup-opentofu@9d84900`,
  `goreleaser/goreleaser-action@9c156ee`, `anchore/sbom-action/download-syft@e22c389`,
  `sigstore/cosign-installer@d7d6bc7`, `actions/attest-build-provenance@c074443`. No tag-only
  action reference exists.
- **Permissions least-privilege.** `ci.yml:28` workflow-level `permissions: contents: read`;
  only the `changes` job widens it, to `pull-requests: read`, with the reason documented
  (`ci.yml:41-46`). `release.yml:44` workflow-level `contents: read`; the single job elevates
  to `contents: write`, `id-token: write`, `attestations: write` with per-line justification.
  No `write-all`, no `GITHUB_TOKEN` exported to untrusted steps.
- **Four-document contract holds.** `ci.yml` path filters are `agent/**`, `backend/**`,
  `frontend/**`, and infra = `docker-compose.yml | policies/** | scripts/** | Makefile`. None
  of the four root documents matches any filter, so editing them triggers no component job.
  The `pre-commit` job carries no `if:`, so it always runs, and
  `.pre-commit-config.yaml:24-33` declares the gitleaks hook with `pass_filenames: false` and
  `always_run: true`, which structurally defeats the top-level `exclude:` block
  (`.pre-commit-config.yaml:15-22`). The design's stated requirement — mutating hooks skip the
  four documents, Gitleaks still scans them — is implemented exactly as described.
- **Criterion-16 step ordering is genuinely load-bearing and correct.** `release.yml:130-158`
  runs `cosign verify-blob` with `--certificate-identity-regexp` and
  `--certificate-oidc-issuer` **before** any provenance step, and fails if `verified == 0`
  (`release.yml:152`). The identity regexp is anchored at both ends and scoped to
  `refs/tags/v.*`, so a signature produced by a different workflow or a branch build does not
  satisfy it.
- **Provenance is bound to artifact bytes.** `release.yml:243-253` verifies with
  `--check-claims=true` against the Sigstore bundle, and the loop fails if `checked == 0`.
  `--new-bundle-format` + `--bundle` means the Rekor inclusion proof travels with the
  artifact, so third-party verification works offline. This matches README's documented
  command exactly.
- **Custody upload is failure-tolerant in the right direction.** `release.yml:275`
  `if: ${{ !cancelled() }}` ensures `.sig`, `.pem`, `.sbom.json`, `.intoto.jsonl`,
  `.att.sigstore.json` reach the release even when a later step failed.

### Findings

**[P1] Completion criterion 6's own assertion step is neutralised with `|| true`**
- Evidence: `.github/workflows/ci.yml:216-217`
  ```
  - name: Assert the generated client uses the supplied build URL
    run: bash ../scripts/check-frontend-container.sh || true
  ```
  The step immediately follows the deliberately non-default build
  (`NEXT_PUBLIC_API_BASE_URL: http://ci.example.test:9999/api/v1`, `ci.yml:209-214`), and its
  comment at `ci.yml:208` says "Criterion 6: a non-default browser URL must be inlined at
  build time."
- Impact: the check that proves `NEXT_PUBLIC_*` is inlined at build time — the one thing
  design §13.3 calls "explicitly insufficient" to do at runtime — **can never fail CI**. If a
  future change makes the browser bundle read the URL at runtime, or bakes in the Compose
  internal hostname `http://backend:8000`, CI stays green. The `frontend` job's green tick is
  therefore not evidence for criterion 6. This is a release/CI integrity failure: a named
  completion criterion has a gate that is structurally incapable of failing.
- Required fix: delete `|| true`. If the script is not runnable in the bare-runner context
  (no container), split it: run the container-independent bundle-grep assertion unconditionally
  and gate only the docker-dependent part behind a condition that is itself asserted.
- Validation: temporarily hardcode `http://backend:8000/api/v1` into the client, run
  `bash scripts/check-frontend-container.sh` after a build, and confirm non-zero exit.

**[P1] `compose-smoke` never starts the stack, so criterion 4 has no executable evidence in CI**
- Evidence: `.github/workflows/ci.yml:220-243`. The job's only commands are
  `docker compose config --services` (three times) and `docker compose --profile … config
  --services`. There is no `docker compose up`, no `--wait`, no `docker compose build`.
  Observed runtime confirms it: `gh pr checks 1` → `compose-smoke  pass  7s`.
  Design Appendix E criterion 4 requires "`docker compose up -d --wait` exits 0 for exactly
  default-profile `postgres`, `redis`, `opa`, `backend`, `frontend`".
- Impact: `docker compose config` validates YAML and profile selection only. It does not build
  `backend/Dockerfile` or `frontend/Dockerfile`, does not evaluate healthchecks, does not
  exercise `depends_on: condition: service_healthy`, and does not prove the five services can
  actually reach a healthy state from a fresh clone. Combined with the next finding, **no CI
  job builds either application image at any point**, so criterion 1 (`make build` for all
  three components) and criterion 4 are both unproven by the green checks.
- Required fix: add `docker compose up -d --wait` (with a bounded timeout) plus
  `docker compose down -v` in the same job, or add an explicit `docker compose build backend
  frontend` step. Keep the existing `config --services` assertions.
- Validation: the job's own log shows five containers reaching healthy, and it fails if a
  Dockerfile stage breaks.

**[P2] No CI job builds `backend/Dockerfile`, so the hash-enforced runtime install is never exercised**
- Evidence: grep of `ci.yml` for `docker build` / `compose build` → no matches. The `backend`
  job (`ci.yml:139-186`) installs `requirements-dev.lock` on the bare runner
  (`ci.yml:180-181`) and never touches the image. Design §13.4 defines `build-backend` as
  "Docker installs `requirements.lock` only with `--require-hashes`, then builds the
  multi-stage image", and criterion 1 is "`make build` succeeds for all three components".
- Impact: the production container's dependency set (`requirements.lock`, not `-dev`), its
  non-root user, and its `--require-hashes` install are untested. A broken runtime lock or a
  bad `COPY` in the final stage ships undetected. `make build` is asserted in `PROGRESS.md`
  only from a local run.
- Required fix: add a `docker build --target runtime ./backend` step to the `backend` job (or
  fold it into `compose-smoke`).
- Validation: the new step fails if `requirements.lock` hashes do not satisfy
  `--require-hashes`.

**[P2] OPA policy tests are never executed by CI**
- Evidence: `policies/mcp/gateway_test.rego` is added by this PR
  (`git diff --name-status` → `A policies/mcp/gateway_test.rego`). Grep of `ci.yml` for `opa`
  matches only the Compose service name in `compose-smoke`. There is no `opa test` step and no
  OPA entry in `.pre-commit-config.yaml`.
- Impact: the fail-closed gateway policy (design §14.1: "OPA unavailable ⇒ empty
  `tools/list`") is the one authorization boundary Phase 0 owns, and its Rego unit tests are
  dead code in CI. A policy regression that inverts a deny rule would be caught by nothing.
- Required fix: add a job or step running `opa test policies/ -v` with a SHA-pinned OPA
  setup action or the digest-pinned `openpolicyagent/opa` image already used by Compose.
- Validation: the step fails when a rule in `policies/mcp/gateway.rego` is inverted.

**[P2] `pnpm audit` is not a gate despite being listed as one**
- Evidence: `.github/workflows/ci.yml:270-272` — `run: pnpm audit --audit-level high || true`.
  Design §14.1 lists "`govulncheck` + `go mod verify`, `pip-audit`, `pnpm audit` in CI" under
  the row **"Dependency vulnerability gate"**, authority `phases.md 0.3, PRD §9`.
- Impact: a high or critical advisory in the frontend dependency tree does not fail CI. The
  Go and Python halves of the same control *are* enforced (`go mod verify && govulncheck` at
  `ci.yml:258-262`; `pip-audit … --strict || pip-audit …` at `ci.yml:263-268`, whose fallback
  still exits non-zero on a real vulnerability), so the frontend is the only unguarded leg.
- Required fix: drop `|| true`. If a currently-unfixable advisory forces the escape hatch, use
  an explicit allowlist (`pnpm audit --audit-level high --ignore <id>`) so the exception is
  named and expires.
- Validation: `pnpm audit --audit-level high` exit code observed non-zero when a known-vulnerable
  dev dependency is introduced.

**[P2] `govulncheck` is installed from `@latest`, breaking the stated no-floating-versions rule**
- Evidence: `.github/workflows/ci.yml:260` —
  `go install golang.org/x/vuln/cmd/govulncheck@latest`. Design §7.7 / §16 state "No floating
  ranges anywhere", and `ci.yml:15` states "Every action is pinned to a full commit SHA: tags
  are mutable, SHAs are not."
- Impact: the vulnerability gate itself is an unpinned network-resolved binary, so CI results
  are not reproducible and a compromised or simply changed upstream alters gate behaviour
  silently. Secondary: `ci.yml:118` installs `golangci-lint@v1.62.2` by mutable tag rather
  than by pseudo-version/SHA, and `ci.yml:79` / `ci.yml:265` `pip install pre-commit` /
  `pip install pip-audit` are entirely unpinned. `pip-tools` is correctly pinned
  (`ci.yml:34`, `ci.yml:101`), which shows the intent exists but was applied unevenly.
- Required fix: pin `govulncheck` to an explicit version and pin the three `pip install`
  invocations to exact versions.
- Validation: `scripts/check-makefile.sh`-style grep asserting no `@latest` and no unpinned
  `pip install` appears in `.github/workflows/`.

**[P2] The `e2e` stage named in the workflow's own contract does not exist**
- Evidence: `.github/workflows/ci.yml:4-5` — "Stage order mirrors the design:
  paths-filter → pre-commit → lock-integrity → lint → test → build → **e2e** → audit →
  supply." There is no `e2e` job in the file, and no job invokes Playwright. Yet
  `frontend/e2e/shell.spec.ts` and `frontend/playwright.config.ts` are added by this PR, and
  design Appendix E criterion 6 names "Playwright asserts keyboard-accessible active Home
  link" as the evidence.
- Impact: criterion 6's accessibility/navigation evidence is not produced by CI. Together with
  the `|| true` finding above, criterion 6 has **no** enforced CI evidence at all — only the
  Vitest shell-layout unit tests, which do not exercise a real browser.
- Required fix: either add the `e2e` job, or amend the header comment and `PROGRESS.md` to
  state that Playwright is a local-only gate in Phase 0 and record it as an accepted gap.
- Validation: `gh pr checks` lists an `e2e` context, or `PROGRESS.md` names the gap.

**[P3] Appendix E cites CI jobs named `build`, `test` and `lint` that do not exist**
- Evidence: design Appendix E rows 1–3 give evidence "CI `build` job", "CI `test` job",
  "CI `lint` job". `ci.yml` has jobs `changes`, `pre-commit`, `lock-integrity`, `agent`,
  `backend`, `frontend`, `compose-smoke`, `audit`, `supply`. The lint/test/build steps are
  folded into the three component jobs.
- Impact: traceability only — an auditor following Appendix E cannot find the named evidence.
  The underlying commands do run (except as noted in the two findings above).
- Required fix: update the Appendix E evidence column, or rename the steps.
- Validation: each criterion's evidence string resolves to a real job or step name.

**[P3] `release.yml` signs the SBOMs but never verifies those signatures**
- Evidence: the signing loop at `release.yml:118-127` includes `dist/*.sbom.json`, producing
  `*.sbom.json.sig`/`.pem`. The criterion-16 verification loop at `release.yml:139-151`
  iterates only `dist/*.tar.gz dist/*.zip dist/*.deb dist/*.rpm dist/checksums.txt` — SBOM
  signatures are never `verify-blob`-checked. The SBOM *presence* check (`release.yml:153-158`)
  confirms the file exists and contains `"bomFormat"`, but not that its signature validates.
- Impact: a corrupted or mis-signed SBOM signature ships without detection. Low severity
  because the SBOM itself is present, schema-sniffed, and its content is covered by the
  provenance over the archive.
- Required fix: add `dist/*.sbom.json` to the verification loop's glob list.
- Validation: the release log shows a verified count that includes the SBOM files.

### Checks NOT run in Pass 3

- `release.yml` end-to-end (tag push) — **not run and must not be run**: creating a tag is an
  explicitly confirm-first action and this is a read-only review. Its behaviour was assessed
  by reading the workflow plus the recorded evidence for run `30469955653` / `v0.0.1-rc3`
  referenced in the commit messages. **The reviewer did not independently verify that
  release run's artifacts.**
- `pre-commit run --all-files` locally — deferred to Pass 9 (needs `pre-commit` installed).

---


## Pass 4 — Backend review

A delegated deep pass over `backend/` produced candidate findings; every P0/P1 claim below
was then **independently re-verified by the reviewer** by reading the code and by executable
signature binding. Claims that did not survive verification are recorded as rejected.

### Executable verification performed

```
cd backend; .venv\Scripts\python.exe  (inspect.signature(...).bind on the real classes)

BIND FAIL policy.filter_tools(server=,tools=,claims=,blast_radius=) -> missing a required argument: 'subject'
BIND FAIL policy.authorise_call(server=,tool=,metadata=,claims=,blast_radius=) -> missing a required keyword-only argument: 'tool_name'
McpUpstream.list_tools return annotation: list[dict[str, Any]]
TtlToolCache.put signature: (self, key: str, value: str, server_ttl_ms: int) -> bool
TtlToolCache.get return annotation: str | None
RedisTaskStore.create signature: (self, *, tool_name: str, arguments: dict|None = None)
BIND FAIL RedisTaskStore.create(kind=..,owner=..) -> missing a required keyword-only argument: 'tool_name'
TaskState is str enum: True
can_transition(SUBMITTED, "working") -> True     (so tasks/update's raw-string state is fine)
```

### Findings

**[P1] The production MCP gateway composition cannot execute: four call sites do not match their collaborators' signatures**

This is the most serious defect in the PR. `McpGateway` is wired with the **real**
`OpaGatewayPolicy`, `TtlToolCache` and `McpUpstream` in production, but its call sites were
written against a different contract. Completion criteria 10 (`tools/list`/`tools/call`),
11 (tasks lifecycle) and 12 (OIDC blocks unauthorized requests — the *authorized* half)
are therefore not actually met on the deployed route.

- Evidence, production wiring: `backend/src/main.py:130-145` constructs
  `OpaGatewayPolicy(opa_url=…, http=shared_http)`, `TtlToolCache(redis_client, …)`,
  `McpUpstream(http=shared_http)`, `RedisTaskStore(redis_client)` and passes them into
  `McpGateway(...)` at `main.py:137-145`, storing it as `app.state.mcp_gateway`.
  `backend/src/mcp/routes.py:59` reads exactly that object
  (`gateway = _require(request, "mcp_gateway")`) and `routes.py:76` / `routes.py:80` call
  `handle_tools_list` / `handle_tools_call`. So the broken composition **is** the route.
- Break 1 — OPA filter. `backend/src/mcp/gateway.py:55-60` calls
  `self._policy.filter_tools(server=…, tools=…, claims=…, blast_radius=…)`.
  `backend/src/mcp/policy.py:39-44` defines
  `filter_tools(self, tools, *, subject, context=None)`. Binding fails: `subject` missing,
  and `server`/`claims`/`blast_radius` are unexpected. → `TypeError` on **every**
  `tools/list`.
- Break 2 — OPA authorize. `gateway.py:78-83` calls
  `authorise_call(server=…, tool=…, metadata=…, claims=…, blast_radius=…)`.
  `policy.py:67-73` defines `authorise_call(self, *, tool_name, subject, arguments=None,
  context=None)`. → `TypeError` on **every** `tools/call`, raised *before* the upstream
  invocation. (Note the perverse consequence: P-05's "zero upstream work on denied calls"
  holds trivially because no call ever reaches the upstream at all.)
- Break 3 — cache and upstream shapes. `gateway.py:49-52` does
  `upstream_result = await self._upstream.list_tools(route.server)` then
  `upstream_result.get("tools", [])`, but `backend/src/mcp/upstream.py:33-73` is
  `list_tools(self, server_url: str, …) -> list[dict]` and its body returns
  `body.get("result", {}).get("tools", [])` — a **list**. `list.get` is an `AttributeError`.
  It is also handed a `ServerDescriptor` where a URL string is expected, so
  `f"{server_url.rstrip('/')}/mcp"` would fail first. Then
  `await self._cache.put(route.server.name, tools, ttl_ms)` passes `ttl_ms=None` (the
  upstream never returns a `ttl_ms` key) into `TtlToolCache.put(key, value: str,
  server_ttl_ms: int)`, whose first statement is `min(server_ttl_ms, self._max_ttl_ms)`
  (`cache.py:44`) → `TypeError: '<' not supported between 'NoneType' and 'int'`. And
  `cache.get` returns `str | None` (`cache.py:55`) while the gateway treats the result as a
  tool list.
- Break 4 — tasks/create. `routes.py:135` calls
  `store.create(kind=params.get("kind", "generic"), owner="default")`;
  `backend/src/mcp/tasks.py:110-113` defines `create(self, *, tool_name, arguments=None)`.
  Binding fails. → `tasks/create` is `TypeError` on the production route, so criterion 11's
  "create → poll → cancel" cannot start. (`tasks/update`'s raw-string state *is* fine —
  `TaskState` subclasses `str`, verified above — and `tasks/get`/`tasks/cancel` bind
  correctly.)
- Impact: `POST /api/v1/mcp` with a valid token returns HTTP 500 for `tools/list`,
  `tools/call` and `tasks/create`. Phase 0's headline deliverable 0.5 is non-functional in
  the container that Compose starts. Criteria 10 and 11 are unmet. `PROGRESS.md` marks them
  complete on the strength of tests that never touch these classes together.
- Required fix (smallest correct remediation): make the collaborators match the gateway,
  since the gateway's contract is the one design §11.4 specifies. Change
  `OpaGatewayPolicy.filter_tools` to `(*, server, tools, claims, blast_radius)` and
  `authorise_call` to `(*, server, tool, metadata, claims, blast_radius)`, deriving
  `subject` from `claims["sub"]` and passing `agent_blast_radius` into the OPA input;
  give `TtlToolCache` internal JSON encode/decode so `put`/`get` exchange `list[dict]`
  and treat `None`/absent TTL as "do not cache"; change `McpUpstream.list_tools` to accept
  the descriptor (or have the gateway pass `route.server.url`) and to return
  `{"tools": [...], "ttl_ms": ...}`; align `store.create(...)` with `RedisTaskStore.create`.
- Validation: an integration test that builds the app through `create_app()` (not a
  hand-composed gateway) and drives `POST /api/v1/mcp` with `Mcp-Method: tools/list` and
  `tools/call` against a stub upstream and a real/stubbed OPA, asserting 200. A cheap
  regression guard: a test that asserts `inspect.signature(...).bind(...)` succeeds for
  each gateway→collaborator call site.

**[P1] The MCP tests replace `spec=`-constrained mocks with unconstrained ones, which is exactly why CI is green**
- Evidence: `backend/tests/unit/test_mcp_e2e.py:143-146` —
  `policy = AsyncMock(spec=OpaGatewayPolicy)` immediately followed by
  `policy.filter_tools = AsyncMock(side_effect=lambda **kwargs: kwargs.get("tools", []))`
  and `policy.authorise_call = AsyncMock(return_value=None)`. Assigning over a spec'd child
  **discards** the spec's signature validation, so `**kwargs` swallows the wrong keyword
  names. Same pattern at `test_mcp_e2e.py:152-155` for `cache.get`/`cache.put`
  (`AsyncMock(spec=TtlToolCache)` then reassigned) and at `test_mcp_e2e.py:130-137` where
  `upstream.list_tools` is made to return `{"tools": [...], "ttl_ms": 30000}` — a shape the
  real `McpUpstream.list_tools` never produces. The fakes encode the gateway's *intended*
  contract, so they certify a composition that does not exist.
  The same construction appears at `backend/tests/property/test_p05_gateway.py:121` and
  `backend/tests/unit/test_wave16.py:89`; no test anywhere composes `McpGateway` with the
  real `OpaGatewayPolicy`/`TtlToolCache`/`McpUpstream`
  (`grep -rn "McpGateway(" backend` → 4 sites: `main.py:137` plus those three tests).
- Impact: 419 green backend tests provide **no** evidence for criterion 10. P-05's
  "upstream invocation count is zero on denial" is proved against a fake whose interface
  differs from production, so the property is verified about code that never runs. This is
  the single largest test-quality defect in the PR.
- Required fix: keep `spec=` and configure behaviour via `return_value`/`side_effect` on the
  spec'd attribute rather than reassigning it (`policy.filter_tools.side_effect = …`), which
  preserves signature enforcement; and add at least one test that composes the real
  collaborator classes against a stub HTTP transport.
- Validation: after the change, the existing tests fail until the P1 above is fixed. That
  transition is the proof.

**[P1] `redact_secrets` is never applied to exception tracebacks, so a leaked DSN or bearer token is logged verbatim**
- Evidence: `backend/src/core/logging.py:44-54` — `SecretRedactingFilter.filter` rewrites
  only `record.msg` and `record.args`. `logging.py:70-71` — `JSONFormatter.format` writes
  `log_entry["exception"] = self.formatException(record.exc_info)`, the inherited
  `logging.Formatter.formatException`, with no redaction. `record.exc_text` is likewise
  untouched. The `console` formatter (`logging.py:88-90`) is a plain
  `logging.Formatter`, so it also emits raw tracebacks.
  This is directly contradicted by design §14.4: "the `SecretRedactingFilter` (§7.2) runs
  before any handler emits". The project's own test fixture demonstrates the exposure
  surface: `backend/tests/property/test_p09_rfc9457.py:104-107` raises a `RuntimeError`
  whose message contains a `postgresql+asyncpg://` DSN with credentials and a
  `Bearer …` clause — the RFC 9457 *response* is sanitised, but the same exception logged by
  Starlette's `ServerErrorMiddleware` reaches the log unredacted.
- Impact: any unhandled exception whose message embeds a DSN, bearer token or API key —
  `sqlalchemy`/`asyncpg` connection errors and `httpx` request errors routinely do — writes
  the secret in cleartext to stdout and thence to any log aggregator. This is the one
  security control Phase 0 explicitly owns for logs, and it has a hole on the most likely
  path.
- Required fix: override `formatException` in `JSONFormatter` to return
  `redact_secrets(super().formatException(ei))`, and extend `SecretRedactingFilter.filter`
  to scrub `record.exc_text` when set. Attach the filter to the `console` formatter path too.
- Validation: log an exception whose traceback contains `postgresql://u:p@h/db` and assert
  `[REDACTED]` appears and the credential does not — for both `LOG_FORMAT=json` and
  `console`.

**[P2] The AI rate limiter derives time from the client, not from Redis `TIME`**
- Evidence: `backend/src/ai/rate_limit/redis_bucket.py:22` documents
  `ARGV[3] = now (current timestamp in seconds…)`; the Lua script uses it for all refill
  arithmetic (`redis_bucket.py:29` `local now = tonumber(ARGV[3])`, `:41-44` elapsed/refill).
  The value is supplied by the caller from `self._clock()`, which defaults to `time.time`.
  `redis.call('TIME')` is never used.
- Impact: the bucket is not authoritative across replicas. Two backend containers with clock
  skew refill the same key inconsistently; a caller that can influence the app's clock (or
  simply a container with a fast clock) gets a higher effective rate. Design §14.1 places
  this limiter as the abuse control on the costly `/api/v1/ai/complete` seam, so the
  weakening is on the one route that spends money. The Lua script itself *is* atomic
  (single `EVAL`, verified), so this is a correctness/robustness defect rather than a race.
- Required fix: read `redis.call('TIME')` inside the script and drop `ARGV[3]`; keep the
  injected clock only for the pure reference model used by tests.
- Validation: a test with two clients whose injected clocks differ by 60 s observes the same
  allow/deny boundary.

**[P2] `RedisTaskStore.update` is read-modify-write with no compare-and-set, so P-10's concurrency clause is unproven**
- Evidence: `backend/src/mcp/tasks.py:127-150` — `record = await self.get(task_id)`
  (`:128`), `can_transition(...)` check (`:132`), in-memory mutation, then
  `await self._redis.set(...)`. No `WATCH`/`MULTI`, no Lua, no conditional `SET`. P-10
  requires "two concurrent updates cannot both succeed".
- Impact: two concurrent `tasks/update` calls both read `working` and both write, so a task
  can be completed and failed, or double-transitioned. Terminal absorption
  (`tasks.py:38-40`, `ALLOWED[COMPLETED|FAILED|CANCELLED] = frozenset()`) and idempotent
  cancel (`tasks.py:148-149`) *are* correct in the single-threaded case — verified — but the
  concurrency clause is not implemented.
- Required fix: move the read-check-write into one Lua script keyed on the expected current
  state, or use `WATCH`+`MULTI` with retry.
- Validation: `asyncio.gather` two conflicting transitions against a real Redis; exactly one
  must succeed. (The current `test_p10_tasks.py` should be checked — see Pass 8.)

**[P2] `${VAR}` placeholders in `config/model-tiers.yaml` are validated but never expanded**
- Evidence: `backend/config/model-tiers.yaml` sets `base_url: ${OPENAI_BASE_URL}` etc.
  `backend/src/ai/routing/tiers.py:75-77` reads `base_url` verbatim and its validator
  explicitly allows a `"${"` prefix through. No `os.environ` expansion exists in the loader.
  `backend/src/ai/routing/endpoints.py:113` then builds `f"{base_url}/chat/completions"`,
  yielding the literal `${OPENAI_BASE_URL}/chat/completions`. Design §13.2 states "The
  loader expands only the documented `${NAME}` variables".
- Impact: with the shipped config, no OpenAI-compatible endpoint has a usable URL, so the
  cascade always ends `EXHAUSTED` in a real deployment. CI does not catch it because the
  route tests substitute deterministic local fixture base URLs, exactly as §13.2 permits —
  which means the fixture substitution hides the missing feature. Criterion 17 ("fallback
  cascade functions end-to-end") is proven only against fixtures.
- Required fix: expand the documented `${NAME}` set from `os.environ` in `load_tier_config`,
  and make an unexpandable variable a load error rather than an accepted literal.
- Validation: a test asserting `load_tier_config` with `OPENAI_BASE_URL` set produces an
  absolute URL, and raises when the variable is absent.

**[P2] `/health/ready`'s 503 body bypasses the RFC 9457 renderer and its sanitiser**
- Evidence: `backend/src/main.py:178-192` builds a `JSONResponse(status_code=503,
  content={...}, media_type=PROBLEM_CONTENT_TYPE)` directly, embedding per-dependency error
  strings in `errors[]`. The central sanitiser `_sanitize_detail` in
  `backend/src/core/errors.py:31-40` is not invoked on that path.
- Impact: a Redis or Postgres failure string frequently contains the connection URL with
  embedded credentials (`redis://user:pass@host`, `postgresql+asyncpg://user:pass@host`).
  Those are exactly two of the patterns `_sanitize_detail` exists to suppress, and
  `/health/ready` is reachable without authentication. P-09 requires `detail` never to match
  a secret pattern on **every** non-2xx response.
- Required fix: pass each dependency error through `_sanitize_detail` (or raise
  `ProblemException` and let the handler render), keeping the required RFC 9457 fields.
- Validation: force a Redis outage with credentials in `REDIS_URL` and assert the
  `/health/ready` body contains no `redis://` substring.

**[P2] `pyyaml` is imported at runtime but is not a declared dependency**
- Evidence: `backend/src/main.py:57` `import yaml`; `backend/src/ai/routing/tiers.py:10`
  `import yaml`. `backend/pyproject.toml` `dependencies` does not list `pyyaml`;
  `backend/requirements.lock` contains `pyyaml==6.0.3` only transitively.
- Impact: startup depends on a transitive pin. Any upstream that drops PyYAML breaks
  `create_app()` at import time, and the failure surfaces only at container start.
- Required fix: add an exact `pyyaml==6.0.3` to `pyproject.toml` and regenerate both locks.
- Validation: `make lock-backend` diff is empty afterwards, and `pip install` of only the
  declared set satisfies `import yaml`.

**[P3] `null_resource` is listed in `STATEFUL_TYPES` with a comment saying it is not stateful**
- Evidence: `backend/src/analysis/plan_analyzer/semantic.py:64` —
  `"null_resource",  # NOT stateful, but useful for testing`. `semantic.py:156` forces
  `verdict = "block"` whenever `stateful_deletions` is non-empty.
- Impact: any real plan that destroys a `null_resource` is forced to BLOCK and mapped to
  `BLOCKED` by `approval.py:33-39`. A test convenience is embedded in production
  classification data. P-11's "any stateful deletion forces BLOCK" is satisfied, but the
  *membership* of the set is wrong.
- Required fix: remove `null_resource` from `STATEFUL_TYPES` and let the tests inject their
  own set, or add a separate `TEST_STATEFUL_TYPES` used only by fixtures.
- Validation: `analyse()` on a plan deleting only `null_resource` yields a non-BLOCK verdict.

**[P3] `nbf` is absent from the JWT required-claims list**
- Evidence: `backend/src/mcp/auth.py:78` —
  `options={"require": ["exp", "iat", "iss", "aud"]}`. Design §15.2 lists the enforced set as
  "required `aud`, `exp`/`nbf`/`iat`".
- Impact: minor. PyJWT *does* validate `nbf` when present, so a not-yet-valid token is still
  rejected; the gap is only that a token omitting `nbf` is accepted, which the design text
  implies should be required.
- Required fix: add `"nbf"` to the `require` list, or amend the design text.
- Validation: a token with no `nbf` claim is rejected with 401.

**[P3] Backend `Dockerfile` uses an editable install across a multi-stage boundary**
- Evidence: `backend/Dockerfile:13` `pip install --no-deps -e .`; the runtime stage copies
  `site-packages` (carrying the `.pth` file) and separately copies `/app/src`
  (`Dockerfile:22`). It works because both halves are copied, but it couples the image to an
  editable-install artefact.
- Impact: fragile rather than broken. A future change to either COPY breaks imports in a way
  that no CI job would catch (no CI job builds this image — see Pass 3).
- Required fix: `pip install --no-deps .` (non-editable) in the builder.
- Validation: `docker build --target runtime ./backend` then
  `python -c "import src.main"` inside the container.

### Rejected / downgraded delegated claims

- The delegated pass rated the four interface mismatches **P0**. Per the severity rubric in
  the review brief, P0 is reserved for active secret exposure, destructive corruption or a
  critical remote exploit. These are broken core behaviour with no exploit and no data loss,
  so they are recorded as **P1 merge blockers**, not P0.
- "JWKS fetch is fail-open" — **rejected**. `backend/src/mcp/auth.py:96` catches
  `jwt.PyJWKClientError`, which is what `PyJWKClient` raises on network failure, and raises
  401. Fail-closed confirmed.
- "`policy.py` has no blast-radius concept" — folded into the P1 above; it is the same defect.
- Frontend `res.json()` claim was rated P0 by the delegated pass; re-rated in Pass 6.

### VERIFIED CORRECT (backend, non-obvious)

- Algorithm confusion prevented: `mcp/auth.py:79` `algorithms=["RS256","ES256"]` — no `HS*`,
  no `none`, so an HMAC token cannot be validated against a JWKS RSA key.
- Issuer allowlist is exact set membership, not prefix matching: `mcp/auth.py:66`.
- Non-empty issuer list enforced in production: `core/config.py:146-149`
  `_require_issuer_in_production` raises when empty and `app_env == "production"`.
- JWKS clients cached per issuer with TTL and rebuilt on expiry: `mcp/auth.py:118-126`.
- Routing never parses the body: `mcp/routing.py:47-66` reads only the two headers;
  `mcp/routes.py:76` deliberately does not call `request.body()` on the `tools/list` path.
  P-05(a) holds structurally.
- OPA fail-closed: `mcp/policy.py:56-58` returns `[]` on any exception (empty `tools/list`);
  `mcp/policy.py:80-81` sets `allowed = False` on any exception and raises 403.
- Cache has no process-local expiry authority: `mcp/cache.py:38-67` uses `SET … px=` and
  gates reads on `pttl > 0`; no timestamps are stored in the object. P-06's runtime clause holds.
- `_sanitize_detail` suppresses the whole `detail` (sets it to `None`) on a pattern match
  rather than partially masking: `core/errors.py:31-40`.
- `/health` is dependency-free static JSON (`main.py:163-170`); `/health/ready` probes both
  dependencies under `asyncio.wait_for(..., timeout=2.0)` (`main.py:173-192`).
- Lifespan tolerates dependency outage: `main.py:120-132` probes best-effort inside
  `try/except` and only logs, so the app still constructs and serves `/health` with Postgres
  and Redis down.
- `hnsw.ef_search` is transaction-scoped: `core/db.py:90` uses `SET LOCAL`, not `SET`.
- Migration/model agreement: `alembic/versions/0001_initial.py:76` `Vector(1536)` vs
  `analysis/models.py:59` `EMBEDDING_DIMS = 1536`; `model_id` present in both; HNSW index
  with `vector_cosine_ops` in both. Matches D-2.
- Middleware registration order in `main.py:148-157` yields the design §4.3 execution order
  once Starlette's prepend semantics are applied.
- Config ignores ambient environment keys and rejects unknown ForgeOps keys:
  `core/config.py:175-180` and `:190-197`. P-15 holds.
- MCP Apps sandbox: `mcp/routes.py` host page sets `Content-Security-Policy` from
  `apps.CSP_POLICY` and an iframe `sandbox` from `apps.SANDBOX_ATTRS` that deliberately
  omits `allow-same-origin`.
- Endpoint honesty for native protocols: `ai/routing/endpoints.py:163-167` reports
  `available=False, reason="unsupported_protocol_phase_0"` rather than faking an adapter.
- Cascade de-duplicates endpoint ids: `ai/routing/tiers.py:43-49` uses a `seen` set.
- Approval mapping fails closed on an unknown verdict: `plan_analyzer/approval.py:33-39`.
- No Phase 1+ imports anywhere in `backend/src` (no cerbos, tree-sitter, opentelemetry, login).

### Checks NOT run in Pass 4

- End-to-end execution of `POST /api/v1/mcp` through `create_app()` against live Redis/OPA —
  **not run**. The defect was proven by signature binding instead, which is decisive and does
  not require the stack. A live run would additionally reveal which of the four breaks fires
  first.

---


## Pass 5 — Go agent review

A delegated deep pass produced candidates; every finding below was re-read and confirmed by
the reviewer at the cited lines.

### Findings

**[P2] `App.Close` builds a timeout context and then explicitly discards it, so P-07's shutdown bound is not implemented**
- Evidence: `agent/internal/app/app.go:130-132`
  ```go
  ctx, cancel := context.WithTimeout(context.Background(), a.cfg.ShutdownTimeout)
  defer cancel()
  _ = ctx // reserved for future bounded close operations
  ```
  The loop at `app.go:133-138` then calls `c.fn()` with no deadline and no `select` on
  `ctx.Done()`. P-07 (design Appendix B) requires "total shutdown ≤ configured timeout".
- Impact: any closer that blocks — `zap`'s `Sync()` on a stalled stdout, a `Close` on a
  half-open socket — hangs the agent process indefinitely on SIGTERM, and a container runtime
  then has to SIGKILL it. `AGENT_SHUTDOWN_TIMEOUT_SECONDS` is configuration that does nothing.
- Required fix: run the close loop in a goroutine and `select` on `ctx.Done()`, recording a
  timeout error for closers that did not finish; or give `namedCloser.fn` a `context.Context`
  parameter and bound each call.
- Validation: a test with a closer that blocks for `2 × ShutdownTimeout` must see `Close()`
  return within roughly `ShutdownTimeout`. (The existing test cannot: see the next finding.)

**[P2] `app_test.go`'s shutdown-timeout assertion is vacuous — it uses a hard-coded 5 s against instantaneous closers**
- Evidence: `agent/internal/app/app_test.go:63-66`
  ```go
  elapsed := time.Since(start)
  if elapsed > 5*time.Second { rt.Fatalf("Close took too long: %v", elapsed) }
  ```
  The generated closers return immediately, and `5*time.Second` is a literal unrelated to
  `cfg.ShutdownTimeout`. No case in the property test blocks.
- Impact: P-07 is reported as property-tested, but its timeout clause is asserted against a
  case that cannot fail. The reverse-order, exactly-once, continue-past-error and idempotence
  clauses *are* genuinely exercised by the `rapid` model (verified) — only the bound is
  hollow.
- Required fix: add a rapid case that injects a blocking closer and assert against
  `cfg.ShutdownTimeout`, not a literal.
- Validation: the new case fails until the finding above is fixed.

**[P2] The OpenTofu timeout path can never escalate to SIGKILL, and signals a process group after the leader has been reaped**
- Evidence: `agent/internal/iac/tofu_runner.go:271` runs `waitErr := cmd.Wait()`. Only at
  `tofu_runner.go:287-290`, *after* that reap, does the cancellation branch call
  `terminateGroup(cmd, r.cfg.KillGrace)`. Inside
  `agent/internal/iac/procattr_unix.go:19-40`, `terminateGroup` sends `SIGTERM` to
  `-cmd.Process.Pid`, then starts a goroutine whose only job is `_ = cmd.Wait()` and
  `close(done)`. Because `cmd.Wait()` has already been called, that second call returns
  `exec: Wait was already called` **immediately**, `done` closes at once, and the
  `select` at `procattr_unix.go:34-39` always takes the `<-done` branch. The
  `time.After(grace)` → `SIGKILL` escalation is unreachable code.
  On Windows, `procattr_windows.go:22-29` shells `taskkill /PID <pid> /T /F` after the same
  reap; `/T` walks the live process tree from that PID, which no longer exists, and the
  error is discarded.
  Compounding this: Go's `exec.CommandContext` default cancel calls `Process.Kill()` on the
  **leader only**, so provider plugins spawned into the group are not killed by the runtime
  either, even though `procattr_unix.go:15` correctly sets `Setpgid: true` to make group
  signalling possible.
- Impact: on a `TOFU_TIMEOUT_SECONDS` expiry, a `tofu` provider plugin that ignores SIGTERM
  is never force-killed and survives as an orphan holding the plugin cache and possibly a
  state lock. Design §3.4/§10.6 specify signal propagation to the whole tree with a grace
  period then a kill; the grace period is a no-op and the kill never fires. The narrower
  PID-recycling concern is real but low-probability: while any group member lives the PGID
  stays reserved, and once the group is empty `kill(-pgid)` normally fails with `ESRCH`.
- Required fix: call `terminateGroup` from the cancellation path **before** `cmd.Wait()` —
  e.g. watch `ctx.Done()` in a goroutine started right after `cmd.Start()` — and remove the
  inner `cmd.Wait()` from `terminateGroup`, replacing it with a bounded poll
  (`syscall.Kill(pgid, 0)`) so the SIGKILL escalation can actually run.
- Validation: an integration test that runs a fixture spawning a SIGTERM-ignoring child,
  cancels the context, and asserts no descendant survives after `KillGrace`. On Windows,
  assert `taskkill` ran while the PID was live.

**[P2] `PlanResult` output is unbounded in line count**
- Evidence: `agent/internal/iac/tofu_runner.go:314-329` (`scanPipe`) caps each line at
  `r.cfg.MaxLineBytes` (`:316-317`, `:321-323`) but appends to `lines` with no ceiling
  (`:324`). Design §10.6 describes the captured streams as bounded.
- Impact: a verbose or pathological provider can grow the slice without limit; the agent's
  memory tracks total tofu output rather than a bounded tail. Two of these run concurrently
  (stdout and stderr).
- Required fix: add `MaxOutputLines` to `TofuConfig` and keep a bounded tail (ring buffer),
  recording how many lines were dropped.
- Validation: a fixture emitting more than `MaxOutputLines` lines leaves
  `len(result.Stdout) == MaxOutputLines`.

**[P2] The agent runs the non-redacting logger, so the redaction path is dead code and tofu output is logged raw**
- Evidence: `agent/internal/app/app.go:57` — `logging.New(cfg.LogLevel, cfg.LogFormat)`.
  `agent/internal/logging/logging.go:43` defines `NewRedacted(level, format string, secrets
  []string)`; a repository-wide grep for `NewRedacted` matches only its own definition —
  no production caller and no test caller. The tofu sink at
  `agent/internal/iac/tofu_runner.go:52` logs every output line
  (`logger.Debug("tofu output", …)`) through the non-redacting logger.
- Impact: design §14.4 states "`tofu` output is streamed through the same logger, so a
  provider that echoes a credential is redacted at the boundary." That is not true of the
  shipped composition — the boundary exists but is not installed. A provider or module that
  prints a token to stdout writes it to the agent log verbatim.
- Required fix: construct the logger with `NewRedacted`, seeding it with the token values the
  agent holds (at minimum `GITHUB_TOKEN`), and keep the redaction pattern list in step with
  the backend's.
- Validation: a test asserting a tofu output line containing `Bearer <synthetic>` is emitted
  as `[REDACTED]`.

**[P2] The path blocklist rejects `.env.example`, which the project requires to be writable**
- Evidence: `agent/internal/fileops/fileops.go:248-250`
  ```go
  base := filepath.Base(norm)
  if base == ".env" || strings.HasPrefix(base, ".env.") { return true }
  ```
  `.env.example` matches the prefix. That file is a **committed, placeholder-only** artefact
  (design §13.1, `.gitignore` explicitly keeps it tracked) and `scripts/init-env.sh` copies
  *from* it.
- Impact: `ApplyAtomic` cannot create or update `.env.example`, so any future scaffolding
  change to the environment contract cannot go through the one sanctioned write path
  (`fileops.ApplyAtomic` is, per design §14.3, "already the only write path"). It also blocks
  `.env.local` and `.env.production`, which is correct, so the rule is right in intent and
  one case too wide.
- Required fix: exempt exactly `.env.example` before the prefix test.
- Validation: `ApplyAtomic` succeeds for `.env.example` and still rejects `.env`,
  `.env.local`, `.env.production`.

**[P3] Latent unsynchronised access to `FSNotifyWatcher.closed`**
- Evidence: `agent/internal/scanner/watcher.go:102` writes `fw.closed = true` from the
  caller's goroutine; `watcher.go:77` reads it inside the watcher goroutine. No mutex or
  atomic. `-race` does not flag it because `fsnotify.Close()` closing the Events channel
  usually wins the select.
- Impact: latent; no observed failure. Would become a real race if the select ordering
  changes.
- Required fix: make `closed` an `atomic.Bool`.
- Validation: `go test -race` with a tight concurrent `Watch`/`Close` loop.

**[P3] `~/.ssh` and `~/.aws` blocklist branches are untested**
- Evidence: the checks exist at `agent/internal/fileops/fileops.go:235-246`, but
  `agent/internal/fileops/fileops_test.go:120-146` (`TestApplyAtomic_BlockedPaths`) covers
  only `.env`, `key.pem` and `CERT.PEM`. The operation root is always `t.TempDir()`, which
  cannot exercise the home-relative logic.
- Impact: a regression in the home-directory containment logic would pass CI.
- Required fix: inject the home directory (or set `HOME`/`USERPROFILE` for the test) so the
  branch can be driven.
- Validation: new cases for `<home>/.ssh/id_rsa` and `<home>/.aws/credentials` return blocked.

**[P3] `checkFastForward` fetches without authentication, so the pre-check silently no-ops on private repositories**
- Evidence: `agent/internal/git/gitclient.go:245-251` builds `gogit.FetchOptions` with no
  `Auth`; a 401 is swallowed and the function returns `nil`.
- Impact: cosmetic — the real push carries auth and the server still rejects a
  non-fast-forward. Degradation is graceful.
- Required fix: pass the same `BasicAuth` used by push.
- Validation: integration test against an authenticated fixture remote.

**[P3] `App.closers` omits `docker` and `k8s`, which design §10.3 lists**
- Evidence: `agent/internal/app/app.go:105-108` registers only `connection`, `mcp`, `logger`.
- Impact: none in Phase 0 — neither probe holds a closable resource. Becomes a leak when they
  gain connection pools.
- Required fix: none now; add closers when the probes become stateful.
- Validation: n/a for Phase 0.

**[P3] The connection manager allocates a live `WSSTransport` even in the disabled configuration**
- Evidence: `agent/internal/connection/manager.go:29` always does
  `transport: NewWSSTransport(logger)`; `Serve()` returns `ErrDisabled` when the URL is empty.
  Design §10.5 and `.env.example` (`AGENT_BACKEND_WSS_URL=` empty) say the manager "stays
  disabled" in Phase 0.
- Impact: no functional harm — nothing dials, and `Close` on a nil conn is a no-op. Wasted
  allocation and a slightly misleading lifecycle.
- Required fix: skip the allocation, or substitute a no-op transport, when the URL is empty.
- Validation: assert `NewManager("", …)` holds no `WSSTransport`.

### VERIFIED CORRECT (Go agent, non-obvious)

- No `func init()` anywhere in `agent/`, no package-level mutable state; all wiring is in
  `app.New()` — constructor injection as design §10.3 requires.
- Reverse-order, continue-past-error, exactly-once close: `app.go:133-138` iterates
  `len(closers)-1 → 0` and accumulates with `errors.Join`; `closeOnce sync.Once` makes
  `Close` idempotent. The `rapid` model in `app_test.go` genuinely checks order and
  exactly-once.
- Pipe ownership is the runner's, not `exec`'s: `tofu_runner.go:199-220` uses `os.Pipe()`
  rather than `cmd.StdoutPipe()`, closes the write ends after `Start` (`:235-236`), drains
  with a mutex-protected assignment, and forces the read ends closed after a bounded
  `drainGrace()` so the scanners cannot block forever (`:274-281`). This is the fix from
  commit `7217868` and it is correct — no goroutine leak, no unsynchronised buffer access.
- Environment isolation (P-12): `agent/internal/iac/env.go:18-44` builds the child
  environment from the allowlist only and never calls `os.Environ()`; `TF_IN_AUTOMATION=1`
  and `TF_INPUT=0` are appended unconditionally (`:36-37`).
- No `apply`: `agent/internal/iac/runner.go:62` documents it, the `Runner` interface exposes
  only `Validate` and `Plan`, and `tofu_runner.go` never passes an `apply` argument.
- MCP server exposes exactly three non-mutating tools: `agent/internal/mcp/tools.go:26-28` —
  `agent.health`, `agent.tofu.validate`, `agent.tofu.plan`.
- `cmd/agent/main.go` is a 49-line composition root using `signal.NotifyContext` with
  `os.Interrupt` and `syscall.SIGTERM`, deferring `stop()` and `a.Close()`, exiting 1 on
  error. No business logic.
- Six `CGO_ENABLED=0` targets: `agent/.goreleaser.yaml:10` `env: [CGO_ENABLED=0]`,
  `:15-16` `goos: [linux, darwin, windows] × goarch: [amd64, arm64]`. `go.mod` contains no
  tree-sitter, and `agent/internal/app/deps_test.go:24` asserts its absence — D-1 is enforced
  by a test, not just by prose.
- Build constraints are correct and complementary: `procattr_unix.go:2` `//go:build !windows`,
  `procattr_windows.go:2` `//go:build windows`.
- Git token hygiene: no `zap` field ever carries the token; it is used only in
  `gogithttp.BasicAuth` and never written to `.git/config`; `gitclient.go:209` sets
  `Force: false` explicitly, so force-push is not reachable; `ErrTokenMissing` names the
  environment variable, never the value.
- `fileops` atomicity: temp file → `fsync` → `rename`, plus parent-directory `fsync`, with
  reverse-order rollback on partial failure and `EvalSymlinks` containment on the parent
  directory (`fileops.go:109-141`). Blocklist is applied before any write.
- P-13 traceparent: `agent/internal/telemetry/tracecontext.go` preserves trace-id, mints a
  new span-id, rejects malformed and version-`ff` headers, and passes `tracestate` through
  unmodified.
- No Phase 1+ leakage: no OTel SDK import in any `.go` file, no OPA/Cerbos/`Decision` types,
  `internal/policy/` is a README-only structural marker, and the only credential source is an
  on-demand `EnvTokenSource`.
- `go test -race -shuffle=on ./...` passes across all 14 packages with no race reports
  (run by the delegated pass; independently re-run in Pass 9).

---


## Pass 6 — Frontend review

Delegated candidates re-verified by the reviewer at the cited lines. Two delegated severities
were corrected downward after checking the actual contract.

### Findings

**[P2] A 2xx response with a non-JSON or empty body throws a raw `SyntaxError` out of the API client**
- Evidence: `frontend/lib/api/client.ts:58` — `return (await res.json()) as T;` with no
  `try`/`catch`. The non-2xx path is defensive (`client.ts:45` uses
  `await res.json().catch(() => null)`), and 204 is special-cased at `client.ts:38`, but a
  200/201 with an empty body, an HTML proxy page, or truncated JSON escapes as a native
  `SyntaxError`.
- Impact: consumers that branch on `instanceof ApiProblemError` — including the
  TanStack Query retry policy at `frontend/components/providers/query-provider.tsx:14-17`,
  which inspects `error.problem.status` to decide whether to retry — see an error object with
  no `problem` field. A `SyntaxError` therefore takes the retry path intended for 5xx and can
  also throw inside the retry predicate. Note this is **not** strictly a P-14 violation:
  P-14 is quantified over non-2xx responses, and every non-2xx path was verified to raise
  exactly one `ApiProblemError`. The delegated pass rated this P0; the rubric reserves P0 for
  secret exposure, destructive corruption or a remote exploit, so it is P2.
- Required fix: wrap the final parse and convert a failure into an `ApiProblemError` carrying
  `status: res.status`; also treat a `content-length: 0` / empty-body 200 as `undefined`.
- Validation: a Vitest case mocking `fetch` to return 200 with body `<html>ok</html>` must
  assert `ApiProblemError`, not `SyntaxError`; and a 200 with an empty body must resolve.

**[P2] The internal-hostname guard checks one hard-coded string, so most Compose hostnames pass**
- Evidence: `frontend/lib/env.ts:4-9` —
  `.refine((url) => !url.includes("backend:8000"), …)`. `http://backend:8080/api/v1`,
  `http://api:8000/api/v1`, `http://forgeops-backend/api/v1` all validate and would be
  inlined into the browser bundle at build time.
- Impact: design §12.6/§13.3 make "no server-internal hostname in browser code" a named
  property of criterion 6, and `docker-compose.yml`'s build arg is the exact place a wrong
  value gets injected. The guard is a string match against the one value someone happened to
  get wrong, not the invariant. Combined with the `|| true` on
  `scripts/check-frontend-container.sh` (Pass 3), nothing enforces this in CI either.
- Required fix: reject any URL whose hostname is a single DNS label (no dot) and is not
  `localhost`, plus RFC 1918 literals. That is the structural shape of a Compose service name.
- Validation: `NEXT_PUBLIC_API_BASE_URL=http://api:8000/v1` fails `envSchema.parse`;
  `http://localhost:8000/api/v1` and a real FQDN still pass.

**[P3] JSON-pointer mapping does not decode `~1`/`~0`, matching a backend that does not encode them either**
- Evidence: `frontend/lib/form-errors.ts:14` — `pointer.replace(/\//g, ".")`, with no RFC 6901
  unescaping. `frontend/lib/api/errors.ts:17` strips only a leading `#/`. The producing side,
  `backend/src/core/errors.py:114`, builds `"#/" + "/".join(loc_parts)` and likewise performs
  no `~1`/`~0` escaping.
- Impact: bounded. Because neither side escapes, the round trip is self-consistent for
  ordinary Pydantic field names, and array indices work (`#/items/0/name` → `items.0.name`,
  which react-hook-form accepts — verified against the mapping). Only a field name literally
  containing `/` or `~` misbehaves, and Pydantic model fields cannot contain `/`. The
  remaining rough edge is a whole-body error: `errors.py:114` emits `"#/"` when `loc_parts`
  is empty, which becomes the empty string and is passed to `setError("")`.
- Required fix: decode `~1` then `~0` on the client, encode them on the server, and map an
  empty pointer to react-hook-form's `root` error slot.
- Validation: a test for pointer `#/` producing a root-level form error rather than
  `setError("")`.

**[P3] `frontend/lib/env.ts` validates at module load, so a bad build arg fails at import rather than at build**
- Evidence: `frontend/lib/env.ts:23` — `export const env = getEnv();` runs `envSchema.parse`
  as a module side effect.
- Impact: acceptable and arguably desirable (fail fast), but it means the failure surfaces
  during `next build`'s module evaluation with a Zod stack rather than as a named build-time
  contract check. Worth knowing when diagnosing a broken image build.
- Required fix: none required. Optionally add an explicit build-time assertion script.
- Validation: n/a.

### VERIFIED CORRECT (frontend, non-obvious)

- **P-14 holds for every non-2xx path.** `client.ts:44-56`: a `problem+json` body that parses
  and type-guards becomes `ApiProblemError(body)`; a malformed or non-conforming body falls
  through to a synthesised `ApiProblemError` that carries `status: res.status` — the **real**
  HTTP status, not a placeholder. Transport/abort failures at `client.ts:25-33` raise
  `ApiTransportError`, which **extends** `ApiProblemError` (`lib/api/errors.ts:28`), so the
  "exactly one error type or its subclass" clause is satisfied. 204 short-circuits at
  `client.ts:38`. The `AbortController` timer is cleared in a `finally`
  (`client.ts:34-36`), so no timer leaks.
- **Build-time inlining is real.** `frontend/lib/env.ts:18-20` uses direct member access
  (`process.env.NEXT_PUBLIC_API_BASE_URL`, `process.env.NEXT_PUBLIC_APP_NAME`) — no bracket
  indexing, no destructuring of `process.env`. This is the form Next.js can statically
  replace, so §12.6 is satisfied at the code level. The browser-safe default is
  `http://localhost:8000/api/v1`, not a Compose hostname.
- Client/server boundary is clean: `app/layout.tsx`, `app/(shell)/layout.tsx` and
  `app/(shell)/page.tsx` are server components and import no store; every interactive file
  (`app-sidebar.tsx`, `theme-toggle.tsx`, `query-provider.tsx`, `theme-provider.tsx`,
  `error.tsx`) carries `'use client'` on line 1.
- `QueryClient` is created once: `components/providers/query-provider.tsx:25`
  `const [queryClient] = useState(makeQueryClient)` — the initialiser form, not
  `useState(makeQueryClient())`.
- Zustand holds only UI state (`stores/ui-store.ts`: `sidebarCollapsed`,
  `commandPaletteOpen`); no server-derived data. `__tests__/ui-store.test.ts:37` asserts it.
- Accessibility: exactly one real `href="/"` inside the shell
  (`components/layout/app-sidebar.tsx:18`) with `aria-current="page"` at `:19`; landmarks
  `<header>` (`app-header.tsx:5`), `<aside>` + `<nav aria-label="Primary">`
  (`app-sidebar.tsx:12,16`), `<main id="main">` (`app/(shell)/layout.tsx:16`); skip link at
  `app/(shell)/layout.tsx:8` targeting the real `#main`; exactly one `<h1>`
  (`app/(shell)/page.tsx:4`); no bare `outline: none` in `app/globals.css`.
- `app/error.tsx:1` is a client component taking `{ error, reset }`; `app/not-found.tsx`
  renders a 404 with a home link (outside the shell, so it does not create a second in-shell
  Home link).
- Theme strategy is internally consistent: `app/layout.tsx:19` `attribute="class"`,
  `globals.css:3` `@custom-variant dark (&:is(.dark *))`, `.dark {}` custom-property block at
  `globals.css:38`. `suppressHydrationWarning` is on `<html>`.
- `next.config.ts:4` sets `output: "standalone"`; `frontend/Dockerfile:20-26` converts the two
  `NEXT_PUBLIC_*` ARGs to ENV **before** `pnpm build` at `:33`; the runtime stage adds a
  non-root `nextjs` user and copies only `.next/standalone`, `.next/static` and `public` — no
  full `node_modules`.
- `frontend/package.json` pins every dependency exactly (no `^`/`~`), and
  `__tests__/package-policy.test.ts:21-30` enforces that as a test rather than a convention.
- No Phase 1+ leakage: no auth UI, no `middleware.ts` (asserted absent by
  `__tests__/package-policy.test.ts:43-49`), no real backend data fetching.

---


## Pass 7 — Infrastructure, Compose, scripts and OPA policy integration

### Findings

**[P1] The backend queries OPA at data paths and with input keys that the shipped Rego does not define, so the gateway denies everything even after the Pass 4 signature fix**

Three independent layers of the OPA integration disagree. The Rego policy itself is correct
and well tested; nothing connects it to the backend.

- Evidence — path mismatch. `policies/mcp/gateway.rego:8` declares `package mcp.gateway`,
  with rules `filter` (`:31`) and `allow` (`:38`). The corresponding OPA data paths are
  therefore `/v1/data/mcp/gateway/filter` and `/v1/data/mcp/gateway/allow`.
  `backend/src/mcp/policy.py:30-31` queries
  `/v1/data/forgeops/mcp/filter_tools` and `/v1/data/forgeops/mcp/allow_call`. Neither
  document exists. OPA answers an undefined document with **HTTP 200 and `{}`**, so
  `resp.raise_for_status()` (`policy.py:50`, `:76`) does not fire; instead
  `result.get("result", [])` yields `[]` and `result.get("result", False)` yields `False`.
- Evidence — input-key mismatch. `gateway.rego:31-35` reads `input.tools` and
  `input.agent_blast_radius`; `gateway.rego:38-41` additionally reads `input.tool`.
  `policy.py:46-49` sends `{tools, subject, context}` and `policy.py:73-79` sends
  `{tool_name, subject, arguments, context}`. `input.agent_blast_radius` and `input.tool` are
  never supplied, so even at the correct path `filter` evaluates to `[]` and `allow` is
  undefined.
- Impact: `tools/list` returns an empty tool set for every caller and `tools/call` returns 403
  for every caller — indistinguishable from the OPA-unreachable fail-closed path. Criterion 10
  ("MCP Gateway responds to `tools/list` and `tools/call`") cannot be met. Note the security
  direction is fail-**closed**, so this is a functional break, not a bypass; but it also means
  the fail-closed behaviour that the tests celebrate is being produced by a wiring bug rather
  than by policy evaluation, so a future path fix could silently change behaviour in the
  permissive direction with no test noticing.
- Required fix: point `filter_path`/`authz_path` at `/v1/data/mcp/gateway/filter` and
  `/v1/data/mcp/gateway/allow`, and build the OPA input as
  `{"tools": …, "tool": …, "agent_blast_radius": …, "subject": claims["sub"]}`. Fix together
  with the Pass 4 signature mismatch — they are the same integration.
- Validation: an integration test against the digest-pinned `openpolicyagent/opa` container
  that asserts a `read_only` agent sees only read-only tools (non-empty) and is denied a
  workspace tool. A cheap guard: assert the queried path's last two segments equal a rule name
  present in `policies/mcp/gateway.rego`.

**[P1] Criterion 14 has no executed evidence anywhere: the schema tests skip in CI even though CI provisions pgvector**
- Evidence: `backend/tests/integration/conftest.py:24` gates on
  `FORGEOPS_TEST_DATABASE_URL`, skipping when unset (`:28-33`). A repository-wide grep for
  `FORGEOPS_TEST_DATABASE_URL` matches only that conftest and
  `scripts/tests/health_outage_test.py:49` — **it appears nowhere in `.github/workflows/`**.
  The CI `backend` job sets only `DATABASE_URL` and `REDIS_URL` (`.github/workflows/ci.yml:174-176`)
  while running a real `pgvector/pgvector:pg17` service (`ci.yml:145-156`).
  Locally reproduced:
  ```
  cd backend; .venv\Scripts\python.exe -m pytest tests/integration -v
  -> 14 passed, 8 skipped
     SKIPPED test_initial_schema.py::test_vector_extension_installed
     SKIPPED test_initial_schema.py::test_exactly_the_three_phase_0_tables
     SKIPPED test_initial_schema.py::test_embedding_column_is_vector_1536
     SKIPPED test_initial_schema.py::test_hnsw_cosine_index_exists_with_tuned_parameters
     SKIPPED test_initial_schema.py::test_ef_search_is_transaction_scoped
     SKIPPED test_initial_schema.py::test_autogenerate_reports_no_pending_diff
     SKIPPED test_initial_schema.py::test_downgrade_removes_every_phase_0_table
     SKIPPED test_lifespan_health.py::test_readiness_recovers_without_restarting_the_process
  ```
- Impact: design Appendix E criterion 14's evidence is "migration test asserting the `hnsw`
  index exists with `vector_cosine_ops` and the column is `vector(1536)`". That test exists,
  is well written, and **never runs** — not locally by default, and not in CI. The same skip
  removes the clean-autogenerate check, the transaction-scoped `ef_search` check and the
  downgrade check. CI pays to start a pgvector database and then tests nothing against it.
  This is the clearest case in the PR of a mandatory test hidden behind environment detection.
- Required fix: set `FORGEOPS_TEST_DATABASE_URL` in the CI `backend` job to the same DSN as
  `DATABASE_URL`, and make the skip an error when `CI=true` so it can never silently return.
- Validation: the CI `backend` job log shows 0 skipped in `tests/integration/test_initial_schema.py`.

**[P2] The `infisical` image is not digest-pinned, unlike every other service**
- Evidence: `docker-compose.yml:98` — `image: infisical/infisical:v0.91.1`. The other three
  images are digest-pinned: `postgres` (`:25`), `redis` (`:38`), `opa` (`:51`) all carry
  `@sha256:…`. Design §13.3 specifies `infisical/infisical:<exact-version>@sha256:<committed-digest>`.
- Impact: `v0.91.1` is a mutable tag; the vault profile can silently pull different bytes.
  Blast radius is limited to the optional `vault` profile, which is not in the default set.
- Required fix: add the resolved digest.
- Validation: `docker compose --profile vault config` shows an `@sha256:` reference; a
  `scripts/check-compose*.sh` assertion that every `image:` contains `@sha256:`.

**[P2] OPA runs as the non-rootless image, deviating from the design with no recorded decision**
- Evidence: `docker-compose.yml:51` — `openpolicyagent/opa:1.4.2@sha256:35a093d9…`.
  Design §13.3 specifies `openpolicyagent/opa:1.4.2-rootless@sha256:<committed-digest>`.
  A grep of `PROGRESS.md` for `rootless` returns no match, so there is no decision record
  (D-1…D-22) covering the change.
- Impact: the OPA container's entrypoint runs as uid 0 rather than as the rootless image's
  unprivileged user. It mounts `./policies` read-only and publishes only to `127.0.0.1`, so
  exposure is limited, but it is an unannounced reduction of the designed posture in the one
  component that makes authorization decisions.
- Required fix: switch to the `-rootless` digest, or record a decision explaining why not
  (e.g. the `/opa version` healthcheck path differs) in `PROGRESS.md`.
- Validation: `docker compose exec opa id` reports a non-zero uid.

**[P2] `scripts/check-frontend-container.sh` is static-only, so criterion 6's bundle-level evidence does not exist even locally**
- Evidence: `scripts/check-frontend-container.sh:3` — "Static assertions for
  frontend/Dockerfile and docker-compose.yml frontend service." A grep of the script for
  `docker`, `.next`, `standalone`, `curl` or `node ` finds only the `DOCKERFILE`/`COMPOSEFILE`
  path variables; it never builds an image, never runs a container, and never greps a
  generated bundle. Local run output ends
  `OK: frontend/Dockerfile and docker-compose.yml pass all Phase 0 container assertions`.
  Design §13.3 requires "A container-build test **inspects/executes the generated client
  bundle** and proves requests use the supplied browser-reachable URL rather than a
  server-internal hostname or runtime-only value."
- Impact: combined with the `|| true` in `ci.yml:217` (Pass 3) and the narrow `env.ts` guard
  (Pass 6), there is **no** check at any level that the built browser bundle contains the
  supplied `NEXT_PUBLIC_API_BASE_URL`. The design's central claim about §12.6 is asserted only
  by reading the Dockerfile.
- Required fix: after `next build`, grep `.next/` (or the standalone output) for the
  build-time URL and fail if absent; keep the static assertions as a cheap pre-check.
- Validation: build with `NEXT_PUBLIC_API_BASE_URL=http://ci.example.test:9999/api/v1` and
  assert that string appears in the emitted client chunks.

**[P3] `check-tofu-lock.sh` exits 0 when `tofu` is absent, so a local "all green" is misleading**
- Evidence: local run log `/tmp/check-tofu-lock.log` →
  `check-tofu-lock: SKIP tofu not on PATH (install OpenTofu 1.12.5)`, exit 0. Same pattern in
  `scripts/check-govulncheck.sh:52-56`. In CI both tools are installed
  (`ci.yml:96-99`, `ci.yml:260`), so CI does exercise them; only local runs are affected.
- Impact: a developer running every `scripts/check-*.sh` sees 13/13 PASS while two gates did
  nothing. Six-platform lock integrity is one of them.
- Required fix: exit non-zero on a missing tool unless an explicit
  `ALLOW_SKIP=1` is set, or print the skip to stderr and summarise skips at the end.
- Validation: running with `tofu` absent and no override returns non-zero.

**[P3] `docker-compose.yml`'s header comment contradicts the file's own contents**
- Evidence: `docker-compose.yml:5-7` — "No optional service is declared here: `infisical`
  (profile vault) and `agent-dev` (profile tools) are added only by their owning
  implementation tasks". Both services *are* declared, at `:96-104` and `:106-118`.
- Impact: documentation drift only; the profile isolation itself is correct and verified.
- Required fix: reword the comment to say both are declared but profile-gated.
- Validation: n/a.

**[P3] The `govulncheck` allowlist is defensible but its matcher is broader than its own stated intent**
- Evidence: `scripts/check-govulncheck.sh:44-49` accepts four `docker/docker` advisories
  (GO-2026-5668, GO-2026-5617, GO-2026-4887, GO-2026-4883) with written rationale — all
  "Fixed in: N/A", all reached only through package `init` chains, and ForgeOps' entire Docker
  surface is `Ping()`/`ServerVersion()` in `agent/internal/docker/probe.go`. Explicit
  re-review triggers are recorded, including a Phase 1 note. This is a sound risk acceptance.
  The implementation detail: `:69` greps **every** `GO-\d{4}-\d+` from the full report rather
  than only the call-reachable symbol section, so a new *unreachable* advisory would also fail
  the gate — stricter than the preamble describes, not weaker.
- Impact: none negative; noted so the discrepancy is not mistaken for a suppression bug later.
- Required fix: none. Optionally add a review-by date.
- Validation: n/a.

### VERIFIED CORRECT (infrastructure)

- Unprofiled topology is exactly five services, confirmed locally:
  `docker compose config --services` → `backend frontend opa postgres redis`;
  `docker compose --profile vault config --services` adds only `infisical`.
- `.env.example` is a **required** env file and `.env` an **optional** one, via the
  `x-service-env` anchor (`docker-compose.yml:17-21`), so a fresh clone starts with no `.env`.
- Every published port binds to loopback: `127.0.0.1:${…}` at `docker-compose.yml:28`, `:41`,
  `:59`, `:71`, `:81`. No `0.0.0.0`.
- Every Compose interpolation in a port or build arg carries a safe default
  (`${POSTGRES_PORT:-5432}`, `${NEXT_PUBLIC_API_BASE_URL:-http://localhost:8000/api/v1}`, etc.),
  which is required because `env_file` is not read during interpolation.
- The backend healthcheck probes `/health` (liveness) and **not** `/health/ready`
  (`docker-compose.yml:88`), matching design §4.4; readiness is polled by `scripts/dev-up.sh`.
- `frontend` waits on `backend: condition: service_healthy` (`docker-compose.yml:75-77`).
- Policies are mounted read-only: `./policies:/policies:ro` (`docker-compose.yml:55`).
- Rego policy quality is high and genuinely tested: unknown tools default to
  `"infrastructure"`, the highest radius (`policies/mcp/gateway.rego:17-20`), exactly as
  design §11.4 requires. `opa test /policies -v` via the digest-pinned image →
  **PASS 27/27**, including `test_allow_unknown_tool_denied_for_read_only` and
  `test_deny_by_default_no_matching_tool`.
- Python hash locks are fresh: `scripts/check-lock-freshness.sh` →
  `requirements.lock is up to date`, `requirements-dev.lock is up to date`.

---


## Pass 8 — Testing quality

Test counts were deliberately not accepted as evidence. Where a test's value was in doubt it
was disproved or confirmed by neutering the behaviour **in memory** — via a pytest plugin
placed in the OS temp directory and loaded with `-p`, so **no repository file was ever
modified**. The plugin was deleted afterwards and the working tree verified clean (see
Pass 9's integrity check).

### Non-vacuity experiments run

```
# Plugin (in %TEMP%, outside the repo) cleared both redaction pattern lists at session start:
#   src.core.logging._SECRET_PATTERNS.clear()
#   src.core.errors._LEAK_PATTERNS.clear()

pytest tests/unit/test_errors.py -p fo_break_redaction -q
  -> 6 FAILED  (test_redacts_bearer_token, _postgresql_url, _redis_url,
                _openai_key, _anthropic_key, _pem_material)
     => these assertions are NON-VACUOUS. The GitGuardian fix is real.

pytest tests/property/test_p09_rfc9457.py -p fo_break_redaction -q
  -> 13 passed  (ALL still green with redaction fully disabled)
     => P-09's secret clause is NOT encoded by this file.
```

### Findings

**[P2] P-09's "detail never matches a secret pattern" clause is not encoded by the P-09 test file**
- Evidence: emptying both `src/core/logging._SECRET_PATTERNS` and
  `src/core/errors._LEAK_PATTERNS` leaves all 13 tests in
  `backend/tests/property/test_p09_rfc9457.py` passing (command and result above). The file
  builds an app with a `/api/v1/crash` route that raises a `RuntimeError` carrying a
  `postgresql+asyncpg://` DSN and a `Bearer …` clause
  (`test_p09_rfc9457.py:104-107`), but the 500 handler emits a fixed generic `detail`, so the
  sanitiser is never on the asserted path. The clause is covered only by
  `backend/tests/unit/test_errors.py::TestSecretRedaction`, which calls the helper
  `_sanitize_detail` directly — a unit test of a function, whereas P-09 is quantified over
  "every backend route".
- Impact: P-09 is reported as a property-based guarantee over all routes; in practice the
  route-level guarantee is untested. A future handler that interpolates an exception message
  into `detail` would ship green. This is exactly the gap the Pass 4 traceback-redaction
  finding sits in.
- Required fix: add a route to the P-09 fixture app that raises `ProblemException` with a
  secret-bearing `detail`, and assert the serialised response body matches no secret pattern.
  Keep the generated-request property loop over that route.
- Validation: with the patterns cleared, the P-09 file must fail.

**[P1 — cross-reference] The MCP test doubles nullify their own `spec=` constraint**

Recorded in full in Pass 4. Summarised here because it is the dominant testing-quality defect:
`backend/tests/unit/test_mcp_e2e.py:143-155` creates `AsyncMock(spec=OpaGatewayPolicy)` /
`AsyncMock(spec=TtlToolCache)` and then **reassigns** the spec'd child attributes with bare
`AsyncMock`s, discarding signature enforcement. The doubles implement the gateway's intended
contract rather than the shipped classes' actual contract, so 419 green tests coexist with a
production composition that raises `TypeError` on every request. The same pattern recurs in
`tests/property/test_p05_gateway.py:121` and `teststs/unit/test_wave16.py:89`, and **no** test
composes `McpGateway` with the real collaborators.

**[P1 — cross-reference] Criterion 14's schema tests are skipped in CI**

Recorded in full in Pass 7: `FORGEOPS_TEST_DATABASE_URL` is never set by any workflow, so all
7 tests in `backend/tests/integration/test_initial_schema.py` skip, together with
`test_readiness_recovers_without_restarting_the_process`.

**[P2 — cross-reference] P-07's shutdown-timeout clause is asserted against a hard-coded 5 s with instantaneous closers**

Recorded in full in Pass 5 (`agent/internal/app/app_test.go:63-66`).

**[P3] `scripts/tests/init-env.test.sh` is committed empty**
- Evidence: `git diff --stat main...phase-0-implementation` shows
  `scripts/tests/init-env.test.sh | 0` — a modification with zero changed lines, and the file
  is listed as `M` in `--name-status`. The `init-env` idempotence contract (design §13.3:
  "repeated calls leave that file byte-identical") is one of the few behaviours with a
  dedicated test file name and no content change in this PR.
- Impact: the non-destructive `.env` guarantee — which protects a developer's real local
  secrets from being overwritten — has no visible new coverage in this PR. The pre-existing
  file may already cover it; that was not confirmed.
- Required fix: confirm the existing test asserts byte-identity after a second `init-env`, or
  add it.
- Validation: run `scripts/tests/init-env.test.sh` with a pre-existing `.env` containing a
  sentinel and assert the sentinel survives.

### VERIFIED CORRECT (testing quality)

- `agent/internal/app/deps_test.go:24` asserts tree-sitter is **absent** from `go.mod`, so
  decision D-1 is enforced executably rather than by prose.
- `frontend/__tests__/package-policy.test.ts:21-30` asserts every dependency is exact-pinned,
  and `:43-49` asserts no `middleware.ts` exists — policy encoded as tests.
- `frontend/__tests__/ui-store.test.ts:37` asserts the Zustand store holds no server data.
- The `rapid` property model in `agent/internal/app/app_test.go` genuinely exercises reverse
  close order, exactly-once, continue-past-error and idempotence (only the timeout clause is
  hollow).
- `backend/tests/integration/test_cascade_integration.py` uses **real local HTTP fixture
  servers** rather than mocks for the model cascade — 12 tests covering timeout, malformed
  JSON, HTTP error, cross-provider fallback, self-hosted tail, unsupported-protocol skip and
  full exhaustion, all passing. This is the strongest test file in the PR.
- OPA policy tests are real and thorough: 27/27 including unknown-tool-denied and
  deny-by-default cases.
- No `time.sleep`-based flakiness was observed in the runs; total backend suite 51.8 s,
  Go suite ~7 s per package worst case, frontend 5.7 s.

---

## Pass 9 — Run validation

Docker daemon confirmed reachable at the start of the review (`docker version` → server
`29.6.2`), recorded in the header block.

### Validation matrix

| # | Command | Result |
| :-- | :--- | :--- |
| 1 | `git rev-parse HEAD` / `origin/phase-0-implementation` | both `2a61dc6…`; local matches remote |
| 2 | `git rev-parse main` / `origin/main` | both `d16eb0e…`; base is a true ancestor (`merge-base == main`) |
| 3 | `git diff --stat main...phase-0-implementation` | 271 files, +29182 / −322 |
| 4 | `gh pr view 1` | OPEN, not draft, `MERGEABLE`, **`mergeStateStatus: UNSTABLE`**, `reviewDecision: ""` |
| 5 | `gh pr checks 1` | 9 ci jobs **pass**; **GitGuardian Security Checks FAIL**; CodeRabbit skipped (242 > 150 files) |
| 6 | `gh api …/check-runs` | GitGuardian: "1 secret uncovered!" over 13 commits |
| 7 | `gitleaks v8.30.1 detect --log-opts="main..phase-0-implementation" --redact` (pinned Docker image) | 13 commits scanned, **no leaks found** |
| 8 | `gitleaks v8.30.1 detect --redact` (full reachable history) | 14 commits scanned, **no leaks found** |
| 9 | `scripts/check-structure.sh` | PASS |
| 10 | `scripts/check-hygiene.sh` | PASS |
| 11 | `scripts/check-licence.sh` | PASS |
| 12 | `scripts/check-docs.sh` | PASS |
| 13 | `scripts/check-progress.sh` | PASS |
| 14 | `scripts/check-makefile.sh` | PASS |
| 15 | `scripts/check-area1.sh` | PASS |
| 16 | `scripts/check-compose.sh` | PASS |
| 17 | `scripts/check-go-module.sh` | PASS |
| 18 | `scripts/check-lock-freshness.sh` | PASS — both hash locks up to date |
| 19 | `scripts/check-tofu-lock.sh` | **PASS but SKIPPED internally** — `tofu not on PATH`; exits 0 (see Pass 7 P3) |
| 20 | `scripts/check-govulncheck.sh` | PASS — 2 advisories reported ACCEPTED against the documented allowlist |
| 21 | `scripts/check-frontend-container.sh` | PASS — but static-only, no bundle inspection (Pass 7 P2) |
| 22 | `cd agent && go build ./...` | clean, no output |
| 23 | `cd agent && go vet ./...` | clean, no output |
| 24 | `cd agent && go test -race -shuffle=on -count=1 ./...` | **all 13 packages `ok`, no data races** |
| 25 | `cd backend && pytest tests/ -q` | **419 passed, 9 skipped** |
| 26 | `cd backend && pytest tests/integration -v` | 14 passed, **8 skipped** (7 × `test_initial_schema.py`, 1 × readiness recovery) |
| 27 | `cd frontend && pnpm exec eslint .` | exit 0 |
| 28 | `cd frontend && pnpm exec tsc --noEmit` | exit 0 |
| 29 | `cd frontend && pnpm exec vitest --run` | 8 files, **66 tests passed** |
| 30 | `docker compose config --services` | exactly `backend frontend opa postgres redis` |
| 31 | `docker compose --profile vault config --services` | the five plus `infisical` only |
| 32 | `docker run … openpolicyagent/opa:1.4.2 test /policies -v` | **PASS 27/27** |
| 33 | `inspect.signature(...).bind(...)` on the real MCP collaborators | **BIND FAIL** for `filter_tools`, `authorise_call`, `RedisTaskStore.create` (Pass 4) |
| 34 | pytest with redaction neutered in memory | `test_errors.py` → 6 FAILED (non-vacuous); `test_p09_rfc9457.py` → 13 passed (vacuous for that clause) |

### Working-tree integrity after validation

`git status --porcelain` reported `M` on `.github/workflows/ci.yml`,
`.github/workflows/release.yml` and `agent/internal/iac/runner_test.go`. These are **stale
stat-cache entries from CRLF/LF normalisation, not content changes** — proven:

```
git diff --exit-code --quiet      -> rc=0 (no content difference)
git hash-object <file> vs git rev-parse HEAD:<file>
  .github/workflows/ci.yml              5d46f0c9 == 5d46f0c9
  .github/workflows/release.yml         06dbc4de == 06dbc4de
  agent/internal/iac/runner_test.go     296139c9 == 296139c9
```

Nothing was staged, committed, pushed, deleted, renamed or truncated. The only new file is
this untracked `REVIEW-PHASE-0.md`. The temporary pytest plugin was created in `%TEMP%`,
outside the repository, and deleted.

### Checks NOT run, and why

| Check | Why not |
| :--- | :--- |
| `docker compose up -d --wait` (criterion 4 end-to-end) | Not run. It requires building the backend and frontend images; combined with the review's read-only remit and the time budget this was deferred. **Consequence: criterion 4 remains unverified by both CI and this review.** |
| `docker build ./backend` / `./frontend` | Not run, same reason. Criterion 1's container half is unverified anywhere. |
| `pytest tests/integration/test_initial_schema.py` against real pgvector | Not run — would have required starting Postgres and setting `FORGEOPS_TEST_DATABASE_URL`. The *skip* itself is the finding (Pass 7 P1). Criterion 14 unverified. |
| Playwright `frontend/e2e/shell.spec.ts` | Not run — needs a built frontend and browser download; there is no CI `e2e` job either. Criterion 6's accessibility evidence unverified. |
| `make build` / `make test` / `make lint` (criteria 1–3 as *make* targets) | Not run as `make`; the equivalent underlying commands were run individually (rows 22–29). GNU make + POSIX shell on this host was not exercised. |
| `tofu init -lockfile=readonly`, six-platform lock check | **Cannot run** — `tofu` is not installed on this host. `scripts/check-tofu-lock.sh` skipped. CI does run it. |
| `goreleaser release --snapshot`, `syft`, `cosign` | **Cannot run** — none of `goreleaser`, `syft`, `cosign` is installed. Criteria 7, 8, 15, 16 were assessed by reading `release.yml` and `.goreleaser.yaml` only. **The reviewer did not independently verify the `v0.0.1-rc3` release artifacts, signatures, SBOMs or attestations.** |
| `opa` CLI directly | Not installed; used the digest-pinned Docker image instead (row 32). |
| `pre-commit run --all-files` | Not run. `pre-commit` is present but a full hook install downloads five remote hook repositories; CI runs this job and it passes. |
| `pip-audit`, `pnpm audit` | Not run locally; both run in the CI `audit` job (with `pnpm audit` non-gating — Pass 3 P2). |
| GitGuardian dashboard incident 35267706 | **Not accessible** — no API token in this environment. Characterised from the commit message plus a fully masked diff. |

---


## Pass 10 — Requirements and traceability

Sources: `.kiro/specs/phase-0-foundation/design.md` (§1 scope, §13.4 Makefile contracts,
§15 conflict resolutions, §17.1 decisions, Appendix B properties, Appendix E criteria) and
`PROGRESS.md` lines 84–105 (criteria table) and its chain-of-custody section.

### Criteria coverage after verification

| # | Criterion | PROGRESS says | Verified verdict |
| :- | :--- | :--- | :--- |
| 1 | `make build` all three | done | **Partial.** `go build ./...` and `next build` verified. The backend/frontend **image** build is claimed from a local `docker compose build` only; no CI job builds either image. |
| 2 | `make test` passes | done | **Met, with caveats.** Reproduced: Go 13/13 `ok` under `-race -shuffle=on`; backend 419 passed / 9 skipped; frontend 66 passed. Caveat: the skips include all of criterion 14's tests. |
| 3 | `make lint` passes | done | **Met.** `eslint` exit 0, `tsc --noEmit` exit 0, `go vet` clean, ruff green in CI, `pre-commit` job green. |
| 4 | `docker-compose up` starts all services | done | **Unverified.** Only `docker compose config --services` was proven (exactly the five, profiles isolated). CI's `compose-smoke` never runs `up` (7 s runtime). The PROGRESS evidence conflates a local run with the CI job. |
| 5 | Health endpoint 200 | done | **Met at unit/integration level.** `test_lifespan_health.py` proves liveness 200 / readiness 503 during outage. The recovery test is skipped (needs a real `redis-server` binary). |
| 6 | Frontend loads at :3000 | done | **Partial.** Vitest shell-layout and accessibility unit tests pass; the bundle-level build-arg proof does not exist (static-only script + `\|\| true` in CI) and there is no `e2e` job. |
| 7 | Six-target compile | done | **Not independently verified.** `.goreleaser.yaml:10-16` is correct on inspection (`CGO_ENABLED=0`, 3 × 2 targets); the `v0.0.1-rc3` run was not re-checked (no `goreleaser` on this host). |
| 8 | Signed + SBOM-attested binaries | done | **Not independently verified.** `release.yml` logic reviewed and sound. Gap found: SBOM signatures are produced but never `verify-blob`-checked. |
| 9 | Pre-commit clean on all files | done | **Met in CI**; not re-run locally. |
| 10 | MCP Gateway `tools/list` + `tools/call` | done | **NOT MET.** The production composition raises `TypeError` on both paths (Pass 4), and even once fixed the OPA data paths and input keys do not match the shipped Rego (Pass 7), so both would return empty/403. Cited evidence is route registration in OpenAPI plus tests against non-conforming fakes. |
| 11 | Tasks lifecycle create → poll → cancel | done | **NOT MET.** `routes.py:135` calls `store.create(kind=…, owner=…)` against `create(*, tool_name, arguments)` → `TypeError`. `test_p10_tasks.py` exercises the state machine, not the route. |
| 12 | OIDC blocks unauthorized requests | done | **Met.** All four design §15.2 cases exist as real tests against a runtime-generated RSA keypair: `test_mcp_gateway.py:203` (no token), `:233` (untrusted `iss`), `:253` (expired), `:284` (wrong `aud`). Verification precedes routing (`gateway.py:43-44`). |
| 13 | Plan Analyzer on sample input | done | **Met.** `test_plan_pipeline.py` drives the real `agent/testdata/plan-sample.json`; P-11 monotonicity test present. |
| 14 | pgvector + HNSW models | done | **Not gated.** The manual `psql` evidence in PROGRESS is specific and plausible, but the automated test named by Appendix E skips in CI and locally (`FORGEOPS_TEST_DATABASE_URL` unset anywhere). |
| 15 | CycloneDX SBOM for the agent | done | **Not independently verified** (`syft` absent). Logic in `release.yml:106-116` is correct. |
| 16 | Cosign keyless verified | done | **Not independently verified** (`cosign` absent). `release.yml:130-158` ordering, identity regexp and issuer are correct on inspection, and the pre-provenance placement is a genuine improvement (D-20). |
| 17 | Fallback cascade end-to-end | done | **Met against fixtures.** 12 real-HTTP-fixture tests pass. Caveat: `${VAR}` expansion is missing (Pass 4 P2), so the shipped config would never reach a live endpoint. |
| 18 | Breaker trips on simulated failures | done | **Met.** `test_p01_breaker.py` stateful machine plus focused examples. |

Net: **13 of 18 criteria stand up; 2 are actively not met (10, 11); 3 are unverified or
ungated (4, 6, 14).** Criteria 7, 8, 15, 16 are plausible but outside this review's reach.

### Property coverage (P-01 … P-15)

Encoded and meaningful: P-01, P-02, P-03, P-04, P-06 (runtime clause), P-08, P-11, P-12,
P-13, P-14 (non-2xx clause), P-15.
Weakened or unproven:
- **P-05** — proven only against fakes whose interface differs from production (Pass 4/8).
- **P-07** — reverse order/exactly-once/idempotence proven; the "≤ configured timeout" clause
  is not implemented and its assertion is a hard-coded 5 s (Pass 5).
- **P-09** — RFC 9457 shape proven; the secret-pattern clause is not exercised at route level
  (Pass 8, demonstrated).
- **P-10** — allowed edges, terminal absorption and idempotent cancel proven; "two concurrent
  updates cannot both succeed" is not implemented (Pass 4).

### Scope discipline — clean

- **No Phase 1+ behaviour leaked.** No `cerbos`, `tree-sitter`, `opentelemetry` or login/RBAC
  import in `backend/src`, `agent/` or `frontend/`. `internal/policy/` is a README-only marker
  with no `Decision`/`Input` placeholder, exactly as design §14.3 requires.
- **D-1 (tree-sitter deferred)** is enforced executably by `agent/internal/app/deps_test.go:24`,
  not merely documented. `CGO_ENABLED=0` holds across six targets.
- **D-2 (1536-d + `model_id`)** is consistent across `analysis/models.py:59`,
  `alembic/versions/0001_initial.py:76`, `EMBEDDING_DIMS` in `.env.example`, and the L2 index.
- **D-5** (`go-git` v5 + `go-github` v68) and **D-14** (module path
  `github.com/parag8487/ForgeOps/agent`) are honoured throughout imports, `.goreleaser.yaml`
  and CI `working-directory`.
- **D-19/D-20/D-21/D-22** — D-20's rationale (criterion-16 verification before any provenance
  step) is implemented exactly as recorded.
- **No `requirements.md` dependency**: the spec directory contains only `design.md`,
  `tasks.md`, `tasks.meta.json`, `.config.kiro`.
- **No placeholder importable packages** in structural future directories; the surviving
  `.gitkeep` files are non-code.

### Traceability findings

**[P2] `PROGRESS.md` presents criteria 10 and 11 as `done` with evidence that does not establish them**
- Evidence: `PROGRESS.md:94` cites for criterion 10 "OpenAPI lists `/api/v1/mcp` …" (route
  registration, not handler behaviour) and "`test_mcp_e2e.py` drives both paths" (fakes whose
  signatures differ from the shipped collaborators). `PROGRESS.md:95` cites for criterion 11
  "`tasks/create → tasks/get → tasks/cancel` handled in `mcp/routes.py`" — a code-reading
  claim about a call that raises `TypeError`.
- Impact: the progress record is the project's audit trail and design §18 requires an evidence
  column that is "a command, a CI run, or an artifact path". Two rows rest on claims that
  cannot fail. This is the "unsubstantiated complete status" the review brief asks about.
- Required fix: revert rows 10 and 11 to `in-progress` until an integration test drives
  `POST /api/v1/mcp` through `create_app()`, then cite that test.
- Validation: `scripts/check-progress.sh` extended to require that each `done` row's evidence
  names a test node id, CI run id, or artifact path — not a source file.

**[P3] Criterion 4's evidence line credits the CI `compose-smoke` job for something it does not do**
- Evidence: `PROGRESS.md:89` ends "…; CI `compose-smoke` job green". That job runs only
  `docker compose config --services` (`.github/workflows/ci.yml:220-243`, 7 s).
- Impact: a reader concludes CI proves the stack starts. It does not.
- Required fix: split the row's evidence into the local `up --wait` run and the CI
  config-level assertion, or make the CI job actually start the stack.
- Validation: the row's CI claim matches the job's steps.

**[P3] Appendix E cites job names that do not exist (see Pass 3 P3), and `docs/` deployment guidance was not re-verified against the changed Compose file**
- Evidence: design Appendix E rows 1–3 name CI `build`/`test`/`lint` jobs; `ci.yml` has none.
  `docs/deployment.md` was modified in this PR but the OPA image variant change
  (rootless → non-rootless) is not reflected in any doc or decision.
- Impact: documentation drift.
- Required fix: align the evidence column and note the image choice.
- Validation: `scripts/check-docs.sh` extended to cross-check image references.

---

## Consolidated findings, ordered P0 → P3

**P0 — none.** No active secret exposure, no destructive corruption, no remote exploit was
found. The GitGuardian finding is a provably non-credential JWT-shaped placeholder.

**P1 — merge blockers (5)**
1. Production MCP gateway composition cannot execute — four signature mismatches (Pass 4).
2. OPA data paths and input keys do not match the shipped Rego (Pass 7).
3. MCP test doubles nullify their own `spec=`, which is why CI is green over a broken
   composition (Pass 4 / Pass 8).
4. `redact_secrets` never reaches exception tracebacks (Pass 4).
5. Criterion 14's schema tests skip in CI despite a provisioned pgvector service (Pass 7);
   plus criterion 6's `|| true` gate and GitGuardian red on the PR head (Pass 3, Pass 2).

**P2 — important defects and gaps (17)**
`compose-smoke` never starts the stack · no CI image build · OPA policy tests absent from CI ·
`pnpm audit` non-gating · `govulncheck@latest` unpinned · no `e2e` job · rate limiter uses the
client clock · task store has no CAS · `${VAR}` never expanded in `model-tiers.yaml` ·
`/health/ready` 503 bypasses the sanitiser · `pyyaml` undeclared · `App.Close` discards its
timeout context · `app_test.go` timeout assertion vacuous · tofu timeout cannot escalate to
SIGKILL · tofu output unbounded in line count · agent uses the non-redacting logger ·
`.env.example` wrongly blocklisted · frontend 2xx `SyntaxError` · narrow internal-hostname
guard · `infisical` not digest-pinned · OPA not rootless · static-only frontend container check ·
P-09 route-level redaction untested · `agent-autonomy.md` untracked · `PROGRESS.md` rows 10/11
overstated.

**P3 — maintainability (12)** — see individual passes.

## Open questions and assumptions

1. Was the `v0.0.1-rc3` release genuinely verified? I could not run `cosign`, `syft` or
   `goreleaser`. I accepted `release.yml`'s logic as correct by inspection and **did not**
   validate the published artifacts. If criteria 7/8/15/16 matter for the merge decision,
   someone should re-run `make verify-release` against a published artifact.
2. Is criterion 4's local `docker compose up -d --wait` claim accurate? Not reproduced here.
3. Was the OPA non-rootless image a deliberate choice? No decision record exists.
4. Is `.kiro/steering/agent-autonomy.md` intentionally local-only, or an oversight?
5. Assumption: `git diff --exit-code` returning 0 plus matching blob hashes is sufficient proof
   that this review left tracked content untouched. I did not run `git update-index --refresh`
   because that mutates the index.

## Tracked-junk and repository-hygiene verdict

**Clean.** 287 tracked files, no caches, logs, `.env`, build output, binaries, release
artifacts or editor state. `git check-ignore` confirms `frontend/.env.local`,
`frontend/tsconfig.tsbuildinfo`, `.ruff_cache`, `.pytest_cache` and `backend/.venv` are ignored
and untracked. Committed tests, fixtures, four lockfiles, the single migration, both licences,
`agent/NOTICE`, SBOM/release configuration and structural `README.md`/`.gitkeep` markers are
all legitimate and were **not** classified as junk. Two nits: `.gitattributes` marks the
lockfiles `-diff` (hiding supply-chain-relevant diffs from review), and 16 `.gitkeep` files
survive in directories that now hold real code. Licence split is correct — root `LICENSE`
`FSL-1.1-ALv2`, `agent/LICENSE` Apache-2.0, `agent/NOTICE` present, `scripts/check-licence.sh`
PASS.

## Security and secret-scan verdict

- **gitleaks v8.30.1 (pinned Docker image): no leaks** over the 13-commit PR range and over
  all 14 reachable commits.
- **GitGuardian: 1 finding, red on the PR head.** Characterised as a JWT-shaped placeholder
  of redacted shape `[20 chars].[7 chars].[9 chars]` at
  `backend/tests/property/test_p09_rfc9457.py:107` in commit `f5ad2b0`, removed at the tip by
  `2a61dc6`. Not a usable credential — **no rotation required**. It remains in PR history, so
  squash-merge is the clean resolution. No secret value was rendered anywhere in this review.
- Security boundaries reviewed: OIDC verification is genuinely fail-closed with an exact
  issuer allowlist, asymmetric-only algorithms, JWKS TTL caching, and verification strictly
  before routing. OPA is fail-closed in both directions. The rate limiter's Lua is atomic but
  clock-naive. Path confinement, blocklists, atomic writes with rollback, environment
  allowlisting and no-force-push are all implemented. CI permissions are least-privilege and
  every action is SHA-pinned. **Two real redaction holes exist** (backend tracebacks, agent
  logger not redacting) and are the most security-relevant defects found.

## Final recommendation

### DO NOT MERGE — in this state.

Not because tests fail, but because the green checks are not measuring the thing they claim to.
Deliverable 0.5 — the MCP Gateway, Phase 0's headline feature — cannot execute a single
`tools/list`, `tools/call` or `tasks/create` request in the composition that `docker compose up`
actually starts, and 419 passing tests do not notice because the test doubles were written
against the interface the gateway wanted rather than the one its collaborators expose. That is
two independent P1s (signature mismatch, OPA path/input mismatch) plus the test-quality defect
that concealed them. Merging would put criteria 10 and 11 on `main` marked `done`.

**Minimum set to convert this to MERGE AFTER FIXES:**
1. Align the gateway's four call sites with `OpaGatewayPolicy`, `TtlToolCache`, `McpUpstream`
   and `RedisTaskStore`; point OPA at `/v1/data/mcp/gateway/{filter,allow}` and send
   `input.tools`, `input.tool`, `input.agent_blast_radius`.
2. Add one integration test that drives `POST /api/v1/mcp` through `create_app()` against the
   digest-pinned OPA container and a stub upstream — and stop reassigning over `spec=` mocks.
3. Redact exception tracebacks in `JSONFormatter.formatException`/`record.exc_text`, and
   construct the agent logger with `logging.NewRedacted`.
4. Set `FORGEOPS_TEST_DATABASE_URL` in the CI `backend` job so criterion 14's tests run.
5. Remove `|| true` from the criterion-6 step and make
   `scripts/check-frontend-container.sh` inspect the built bundle.
6. Squash-merge so commit `f5ad2b0` does not carry the GitGuardian finding onto `main`.
7. Revert `PROGRESS.md` rows 10 and 11 to `in-progress` until (2) exists.

Everything else in this PR is of high quality and should not be disturbed: the OpenTofu pipe
ownership, environment allowlisting and process-group setup; `fileops` atomicity and rollback;
the Rego policy (27/27, unknown tools default to the highest radius); the cascade integration
tests against real local HTTP fixtures; the release workflow's ordering and identity checks;
the licence split; and the repository hygiene.

---

## Passes not yet done — FINAL STATE

- [x] Pass 1 — Establish exact review scope
- [x] Pass 2 — Secret scanning, GitGuardian incident, PR check state
- [x] Pass 3 — CI, release and supply-chain review
- [x] Pass 4 — Backend review
- [x] Pass 5 — Go agent review
- [x] Pass 6 — Frontend review
- [x] Pass 7 — Infrastructure, Compose, scripts, OPA integration
- [x] Pass 8 — Testing quality
- [x] Pass 9 — Run validation
- [x] Pass 10 — Requirements and traceability
- [x] Final — Consolidated findings, verdicts, recommendation

**Deferred / impossible in this environment** (each recorded as a finding, not a silence):
`docker compose up -d --wait`; `docker build` of the backend and frontend images; the pgvector
integration tests; Playwright `e2e`; `tofu` six-platform lock check; `goreleaser`/`syft`/`cosign`
verification of `v0.0.1-rc3`; `pre-commit run --all-files`; GitGuardian dashboard access.

*End of review record.*
