# Design Document: Phase 1 — MVP Core: Analysis, Generation & Approval

**Spec:** `phase-1-mvp-core`
**Project:** **ForgeOps** — <https://github.com/parag8487/ForgeOps>
**Go module path:** `github.com/parag8487/ForgeOps/agent` (inherited, D-14 — unchanged)
**Licensing:** root repository = **`FSL-1.1-ALv2`**; `agent/` = **`Apache-2.0`** (inherited, D-19 — unchanged)
**Scope:** Phase 1 only, per `phases.md` § "Phase 1: MVP Core — Analysis, Generation, & Approval", deliverables §1.1 – §1.11
**Design artifacts:** High-Level Design (architecture, components, data model) + Low-Level Design (interfaces, algorithms, formal properties)
**Reference documents (read-only, never modified by this spec):** `AI-Powered-DevOps-Platform-Complete-Technical-Research.md`, `PRD.md`, `Tech-Stack-Analysis.md`, `phases.md`, `PROGRESS.md`, `REVIEW-PHASE-0.md`
**Inherited contract:** `.kiro/specs/phase-0-foundation/design.md` in full, plus decisions **D-1 … D-27** and open questions **OQ-3 … OQ-21** as recorded in `PROGRESS.md`
**Numbering continuity:** decisions continue at **D-28**; correctness properties use the fresh prefix **Q-01 …** (P-01 … P-15 belong to Phase 0 and are never reused); open questions continue at **OQ-22**
**Workflow:** design-first. This document is the planning authority; a later `tasks.md` traces to it. **No `requirements.md` exists or is referenced.**
**Last revised:** 2026-07-30 — initial Phase 1 design, incorporating owner decisions D-28 and D-29

---

## 0. Overview

Phase 1 builds the product. Phase 0 produced a monorepo whose three components build, test, lint, containerise and release reproducibly, with the MCP Gateway, model routing, validation pipeline, GitOps and OpenTofu *foundations* provably exercisable. Phase 1 turns those foundations into the core value proposition named by `phases.md`: **pair an agent, import a project, scan and score it, generate the missing DevOps artifacts with AI, validate them, and apply them atomically behind policy, approval and an immutable audit trail.**

Three things make this the highest-risk phase in the plan, and the design is shaped around them.

1. **It is the first phase that mutates a user's filesystem.** Every Phase 0 seam that touches a file (`fileops.ApplyAtomic`, the `ApprovalGate`, the blast-radius analyzer) exists precisely so that Phase 1 can put one enforced chokepoint in front of mutation. §1.10's Governance Control Plane is that chokepoint, and §2.2/§11.6 make bypassing it a **failing build**, not a review miss.
2. **It is the first phase that sends real source code to real models.** NFR-10 ("no secrets in LLM context, redacted before the API call") stops being theoretical. §7.11 and §11.8 put redaction at a single chokepoint the RAG retriever cannot route around, and make the type system — not discipline — the enforcement.
3. **Phase 0's own review is the design input that matters most.** `REVIEW-PHASE-0.md` recorded 419 passing tests over an MCP gateway that could not serve a single request. §0.4 turns that lesson into a standing, tool-enforced regime that applies to every component built in this phase.

As in Phase 0, the target architecture for later phases is sketched at a high level so Phase 1's seams are shaped correctly — but everything outside §1.1 is marked **Architectural Context Only** and must not be implemented, scaffolded or stubbed now.

### 0.1 Authority order (strict)

Every decision in this document cites its authority. Where authorities conflict, the following order governs:

1. `AI-Powered-DevOps-Platform-Complete-Technical-Research.md` **§0 Corrections & Updates (24 July 2026)** — supersedes everything, including the rest of that file.
2. The remainder of the research document.
3. `Tech-Stack-Analysis.md` — every technology choice and version is validated against this.
4. `phases.md` and `PRD.md`.
5. `.kiro/specs/phase-0-foundation/design.md` plus its decision log **D-1 … D-27**, as the **inherited contract**. Any Phase 1 change to a Phase 0 contract requires a **new numbered decision that names what it supersedes** (§17.1). Silent divergence from a Phase 0 contract is a defect.

Where all sources are silent or ambiguous, the gap is recorded in **§17.2** as an open question rather than guessed.

One consequence of level 5 worth stating plainly: the *repository* is also an authority about itself. Where the Phase 0 design's pin table and the committed `backend/pyproject.toml` / `agent/go.mod` disagree, the committed lockfiles win, because they are what CI actually installs. Those divergences are enumerated in §15.9 rather than silently inherited.

### 0.2 Non-negotiable corrections honoured by this design

| Correction | Authority | Where honoured |
|:---|:---|:---|
| **6-tier** model routing; GPT-5.6 Sol primary flagship, Claude Fable 5 analysis flagship | Research §0 | §11.5, §13.2, §15.6 |
| FastAPI **native `EventSourceResponse`**; never `sse-starlette` | Research §0, §A0b | §7.5, §11.11 |
| SSE event vocabulary is exactly `status`, `token`, `progress`, `validation`, `complete`, `error` | Research §0; Phase 0 `core/sse.py` | §7.5, Q-26 |
| **No Celery.** ARQ/Dramatiq at P1; exactly **one** durable engine at P2 behind an orchestrator-agnostic interface | Research §0, §B6 | §7.10, D-32 |
| **pgvector HNSW** by default; `hnsw.ef_search` tuned at query time | Research §0, §A0a | §6.4, §11.4 |
| Agent identity = **SPIFFE/SPIRE X.509-SVID + mTLS with attestation**; no long-lived agent keys | Research §0, §H31 | §14.3, D-36 |
| **Cerbos is not embeddable** in a single Go binary; agent-side eval = **OPA compiled to Wasm** | Research §B7 | §5.5, §10.6, D-29, D-30 |
| Multi-tenant isolation = **PostgreSQL RLS** with **PgBouncer transaction-mode** pooling in mind | Research §0 | §6.8, §7.12, D-35 |
| Semantic cache doubles as a **resilience layer** with a staleness flag | Research §A0c | §11.5.6 (reused as-is) |
| SWE-bench numbers are self-reported and scaffolding-dependent — rank on internal golden set | Research §0, §C8 | §11.5.4 (`rank_source` unchanged) |
| Reranking: **over-retrieve 3× then `voyage-rerank-2`**, from P1 | Research §C10 | §11.5.2, Q-29 |
| Chunks **~512 tokens with 128-token overlap**; module summaries **~1024 tokens** | Research §C10 | §10.8, §11.4 |
| cAST semantic chunking is **tree-sitter AST bottom-up grouping** | Research §0, §C10 | §10.8, D-29 |
| **Incremental scanning is pulled forward into Phase 1** (PRD marks FR-15 as P2) | Tech-Stack §"Performance Optimization Suggestions" item 1 | §11.4.4, §15.1, D-33 |
| Governance Control Plane is the project's **core trust moat**, one enforced chokepoint | Research §5.1 P0 #3b, §1 Executive Summary | §2.2, §11.6 |
| Defence in depth is **eight layers** ending in audit + rollback | Research §H29 | §14.4 |
| **Authentik** is the recommended self-hosted IdP; Keycloak heavier; ZITADEL's AGPL may conflict | Tech-Stack §8 | §11.2, D-34 |
| Gitleaks (pre-commit) + server-side scanning, two-gate | Research §F20 | §10.9, §14.5 |

### 0.3 Project identity and licensing (inherited, unchanged)

D-14 and D-19 are settled and Phase 1 changes neither. The project is **ForgeOps**, the module path is `github.com/parag8487/ForgeOps/agent`, the root licence is `FSL-1.1-ALv2` and `agent/` is `Apache-2.0`. The problem-type registry base URI remains `https://errors.forgeops.dev`. The four reference documents remain read-only inputs excluded from every mutating hook while staying inside the Gitleaks scan.

Two Phase 1 licensing consequences are recorded here so §16 does not have to relitigate them:

- **New Go dependencies must be permissive**, because they link into the Apache-2.0 `agent/` subtree. Every row in §16.1 is Apache-2.0, MIT, BSD or ISC. No GPL/AGPL library is linked; k6 and `golangci-lint` remain external tools, never linked (inherited §16.6).
- **The vendored tree-sitter grammar `.wasm` artifacts are non-Go, non-module components** introduced by D-29. They are licensed per grammar (MIT or Apache-2.0), and they must appear in the CycloneDX SBOM with their licence and SHA-256, because Syft cannot see them (§8.6, §16.5).

### 0.4 The standing test-integrity regime

This subsection is normative for **every component built in Phase 1**. It exists because of a specific, documented failure, and it is written as mechanisms rather than intentions.

**What happened.** Phase 0 shipped with 419 passing backend tests while its MCP gateway could not serve a single request. `backend/tests/unit/test_mcp_e2e.py` built `AsyncMock(spec=OpaGatewayPolicy)` and then **reassigned the spec'd child** (`policy.filter_tools = AsyncMock(...)`). Reassignment discards `spec`'s signature enforcement, so the doubles implemented the contract the *caller* wanted while the real collaborators implemented a different one. Neither type checking nor coverage could see it, because collaborators arrive by constructor injection and the call sites dispatch dynamically. `REVIEW-PHASE-0.md` Pass 4 / Pass 8 recorded it; **D-23** records it as the phase's main lesson.

The five clauses below make that class of failure structurally impossible. Each names its enforcing mechanism and the CI job that runs it.

#### 0.4.1 Clause 1 — Wiring tests over the real object graph

> Every component composed in production has at least one test that instantiates the **real** collaborators exactly as `create_app()` (backend) or `app.New()` (agent) does, and drives it through the real route or entry point.

**Mechanism: an app-factory-derived fixture, so the test cannot drift from production wiring.**

`backend/tests/integration/production_app.py` exposes a single fixture:

```python
# backend/tests/integration/production_app.py
@pytest.fixture
async def production_app(monkeypatch, capability) -> AsyncIterator[FastAPI]:
    """Build the app through the PRODUCTION factory, substituting only I/O edges.

    The rule that makes this non-negotiable: this fixture may substitute a
    *transport* (httpx.MockTransport, a local fixture HTTP server, a Redis or
    Postgres URL pointing at a container) but it may NEVER substitute a
    collaborator object. If a test needs a different OpaGatewayPolicy, the answer
    is a different OPA policy file, not a different Python object — because the
    Phase 0 defect was exactly a substituted collaborator whose signature had
    drifted from the real one (D-23).
    """
    app = create_app()                      # the same callable uvicorn runs
    async with LifespanManager(app):
        yield app
```

Two derived tests keep the coverage of this clause complete without hand maintenance:

```python
# backend/tests/integration/test_wiring_coverage.py
def test_every_composed_collaborator_has_a_wiring_test(production_app: FastAPI) -> None:
    """app.state is the production composition's public surface. Every attribute
    placed on it by the lifespan MUST be named by at least one wiring test, so a
    newly composed component cannot arrive untested."""
    composed = {k for k in vars(production_app.state) if not k.startswith("_")}
    covered = collect_wiring_declarations(Path("backend/tests/integration"))  # @wires("router")
    assert composed <= covered, f"composed but never wired-tested: {sorted(composed - covered)}"
```

The Go equivalent asserts the same invariant against the constructed graph:

```go
// agent/internal/app/wiring_test.go
//
// TestWiring_RealGraph builds the app through app.New — the same constructor
// cmd/agent/main.go calls — and drives each subsystem through its real entry
// point. No collaborator is replaced; only external edges (a httptest server, a
// temp directory, a fake tofu binary on PATH) are substituted.
func TestWiring_RealGraph(t *testing.T) { /* ... */ }

// TestWiring_CoversEveryClosers asserts every namedCloser registered by app.New
// is exercised by a wiring test, so a new subsystem cannot be composed silently.
func TestWiring_CoversEveryCloser(t *testing.T) { /* ... */ }
```

**CI job:** `backend` (integration selection), `agent`, and `k8s` for the collaborators that need a cluster.
**Phase 0 precedent to extend, not replace:** `backend/tests/integration/test_mcp_wiring.py` already composes the real gateway collaborators as `main.py` does. Phase 1 generalises that file's pattern into `production_app` and deletes nothing.

#### 0.4.2 Clause 2 — Signature conformance, with a self-maintaining inventory

> A fast test binds every cross-component call site against the real class: `inspect.signature().bind()` in Python, and a compile-time assertion in Go.

**Mechanism (Python): the inventory is derived by AST scan, never hand-written.**

`backend/tests/unit/test_contract_conformance.py` does not contain a list of call sites. It calls `scripts/collect_call_sites.py`, which walks `backend/src/**/*.py` with `ast`, finds every `Call` whose function is an `Attribute` on a name bound from a constructor parameter or an `app.state` read, resolves the declared collaborator type from the annotation, and yields `(module, line, target_class, method, args, kwargs)`. The test then binds each one:

```python
# backend/tests/unit/test_contract_conformance.py
@pytest.mark.parametrize("site", collect_call_sites(), ids=str)
def test_call_site_binds_against_the_real_class(site: CallSite) -> None:
    """Phase 0's D-23 defect in one assertion: the *caller's* argument shape must
    bind against the *callee's* real signature. Runs in milliseconds, needs no
    service, and fails the instant a collaborator's signature drifts."""
    sig = inspect.signature(getattr(site.target_class, site.method))
    sig.bind(*site.positional_placeholders, **site.keyword_placeholders)

def test_inventory_is_not_empty_and_grows_with_the_code() -> None:
    """A collector that silently returns [] would make the clause vacuous."""
    sites = collect_call_sites()
    assert len(sites) >= INVENTORY_FLOOR          # committed integer, raised deliberately
```

`INVENTORY_FLOOR` is committed and may only be raised. That is what stops a refactor from quietly emptying the inventory — the exact failure mode that made Phase 0's coverage number meaningless.

**Mechanism (Go): compile-time assertions, plus a check that they exist.**

Every package that declares an interface consumed elsewhere carries `var _ Iface = (*Impl)(nil)` in a `contract_test.go`. Completeness is enforced by `scripts/check-go-interface-assertions.sh`, which lists every exported interface under `agent/internal/**` and every type that structurally satisfies it, and fails if an implementation lacks an assertion. Compile-time assertions cannot rot, but they can be *absent*; the script closes that.

**CI job:** `backend` (unit selection) and `agent` (lint step). Both are sub-second.

#### 0.4.3 Clause 3 — Signature-enforcing doubles, enforced by tooling

> Reassigning a `spec=`'d child is **forbidden**. Doubles use `create_autospec(..., spec_set=True)`, or configure `m.method.side_effect` / `m.method.return_value` instead of assigning over the child.

**Mechanism: an AST lint over `backend/tests/**`, run in pre-commit and in CI.**

`scripts/check-test-doubles.py` (Ruff cannot express this rule) walks the test tree and reports:

| Rule | Detects | Why |
|:---|:---|:---|
| `FO-TD001` | `Assign` whose target is an `Attribute` on a name bound from `Mock(spec=…)`, `AsyncMock(spec=…)`, `MagicMock(spec=…)` or `create_autospec(…)`, where the assigned value is a bare `Mock()` / `AsyncMock()` / `MagicMock()` | The exact Phase 0 defect (D-23) |
| `FO-TD002` | `Mock(spec=X)` / `create_autospec(X)` without `spec_set=True` | `spec_set` also rejects *new* attribute names, closing the sibling hole |
| `FO-TD003` | `patch.object(..., autospec=False)` or `patch(...)` with no `autospec=True` on a project-owned target | A patch without autospec is a reassignment by another name |
| `FO-TD004` | `Mock` used at all in `tests/integration/**` | Integration tests substitute transports, not objects (§0.4.1) |

The rule set is proven non-vacuous by fixtures: `backend/tests/meta/fixtures/bad_double.py` must be flagged and `good_double.py` must not, asserted by `backend/tests/meta/test_check_test_doubles.py`. A lint whose own tests are missing is a lint nobody trusts.

**Invocation:** `python scripts/check-test-doubles.py backend/tests` — also wired as a `pre-commit` local hook with `files: ^backend/tests/.*\.py$`.
**Input:** every `.py` file under `backend/tests/**`, parsed with `ast`; no imports are executed, so the check is safe on untrusted trees and runs in well under a second.
**Failure condition:** exit `1` with one line per finding as `path:line: FO-TD00N message`. Zero findings exits `0`. Suppression requires an explicit `# noqa: FO-TD00N — <reason>` comment, and a suppression without a reason is itself `FO-TD001`.

**CI job:** `pre-commit` (repository-wide hook) and `backend`.

#### 0.4.4 Clause 4 — No silent skips

> Every mandatory test actually executes in CI. A test skipped behind an environment variable or capability probe that CI never sets is a **defect, not a gap**.

**Mechanism: extend D-26's existing gate; do not invent a second one.**

`backend/tests/integration/capability.py::require_capability` already skips locally and **fails** when `FORGEOPS_REQUIRE_INTEGRATION=1`, which the CI `backend` job sets. Phase 1 registers every new capability through that same function and no other:

| Capability | Gate key | Provided in CI by |
|:---|:---|:---|
| Postgres + pgvector | `postgres` | `backend` job service (existing) |
| Redis Stack (vector + BM25 search) | `redis` | `backend` job service (existing) |
| OPA server | `opa` | `backend` job service (new, §8.3) |
| Cerbos sidecar | `cerbos` | `backend` job service (new) |
| OIDC issuer | `oidc` | fixture issuer in the `backend` job; real Authentik in `auth` job |
| Kubernetes API server | `kubernetes` | `k8s` job (kind cluster, D-28) |
| OpenTofu binary | `tofu` | `agent` job (existing) |
| Trivy binary | `trivy` | `agent` job (new) |
| Infisical | `infisical` | `secrets` job (new) |
| Real agent binary | `agent_binary` | `e2e` job (new) |

Additionally, `scripts/check-no-skips.py` consumes `pytest --report-log` and `go test -json` output and **fails the build if the mandatory selection records any skip**, listing each one. The mandatory selection is defined by marker, not by path, so moving a file cannot drop it:

```python
# asserts zero skips in the mandatory selection
mandatory = [t for t in report if "mandatory" in t.keywords]
skipped = [t for t in mandatory if t.outcome == "skipped"]
assert not skipped, f"mandatory tests skipped in CI: {[t.nodeid for t in skipped]}"
```

**CI job:** `backend`, `agent`, `k8s`, `e2e`, `secrets` — every job that runs tests pipes through it.

**Invocation:** `pytest -m mandatory --report-log=mandatory.jsonl` then `python scripts/check-no-skips.py mandatory.jsonl`; on the Go side `go test -json -tags=integration ./... > agent.jsonl` then `python scripts/check-no-skips.py --go agent.jsonl`.
**Input:** the pytest `--report-log` JSONL, or `go test -json` events; nothing else, so the check cannot disagree with what actually ran.
**Failure condition:** exit `1` listing every `nodeid` (or Go `Test`/`Package` pair) whose outcome was `skipped` while carrying the `mandatory` marker, and also exit `1` if the mandatory selection is **empty** — a selector that matches nothing would otherwise pass silently, which is the same vacuity trap as §0.4.5.

**One exemption, on the Go side only, and it is declared by the test (D-68).** Some assertions cannot hold on some platforms: POSIX mode bits, symlinks, a read-only directory that refuses a write. "Provide the capability in CI" is not available for those, so on Windows this gate could never pass — the shape D-51 rejects. A Go test may therefore name the platform it needs in its own skip message, from a **closed** vocabulary:

```go
t.Skip("platform-only: posix - NTFS uses ACLs, so a mode-bit assertion is meaningless")
```

The gate judges the declaration against the platform the report came from (`--os`, else `go env GOOS`, printed either way). An **undeclared** skip fails; a declaration **outside the vocabulary** fails; and a declaration whose requirement the reporting platform **satisfies** fails — that last clause is what stops the tag being a blanket exemption. On Linux, where CI runs, `posix` is satisfied, so every such test must execute and the guarantee is exactly what it was. The permitted set is printed on every run, including when it is empty, so it growing is visible rather than assumed. No allowlist of test names exists anywhere: the declaration lives beside the guard that produces it, so it cannot outlive it.

#### 0.4.5 Clause 5 — Non-vacuity: every property ships with an executable negative control

> Every property test in Appendix B ships with a documented negative control: the specific mutation that must make it fail. A property that still passes under its negative control is a **failing build**.

**Mechanism: an in-memory mutation harness loaded from a temporary directory outside the repository, so no repository file is ever modified.**

This is precisely the technique `REVIEW-PHASE-0.md` Pass 8 used to prove P-09's secret clause was decorative (13 tests stayed green with both redaction pattern lists emptied). Phase 1 promotes it from a one-off review experiment to a CI job.

`backend/tests/mutation/mutations.toml` declares one row per property:

```toml
[Q-08]
property   = "backend/tests/property/test_q08_iteration_bound.py"
target     = "src.generation.loop.FeedbackLoop._next"
mutation   = "return replace(state, attempts_remaining=state.attempts_remaining)"  # never decrements
description = "removes the decrement that guarantees termination"

[Q-13]
property   = "backend/tests/property/test_q13_cache_key_redaction.py"
target     = "src.generation.context.assemble_prompt"
mutation   = "accept a raw str instead of RedactedPrompt"
description = "bypasses the redaction chokepoint"
```

`scripts/mutation-harness.py`:

1. creates a directory with `tempfile.mkdtemp()` — **outside the repository tree**, asserted by comparing `Path(tmp).resolve()` against `Path.cwd().resolve()`;
2. writes one pytest plugin per mutation into it, each applying the mutation with `monkeypatch`/`setattr` at session start;
3. runs `pytest <property file> -p <plugin>` with the temp dir on `PYTHONPATH`;
4. asserts the run **fails**, and reports `VACUOUS` if it passes;
5. removes the temp directory and asserts `git status --porcelain` is empty, so the harness provably left the working tree untouched.

Go properties use the same contract via build-tagged mutation variants compiled into a temp `GOFLAGS`-overlay module (`go build -overlay`), never by editing a tracked file.

**CI job:** `mutation` (new, §8.3). Invocation: `python scripts/mutation-harness.py --all` (also `make mutation`). Input: `backend/tests/mutation/mutations.toml` plus the property files it names. It runs the full Q-01 … Q-31 set and prints a table of `property → mutation → expected FAIL → observed`. Failure condition: exit non-zero if any row is `VACUOUS` (the property passed under its own mutation), if `mutations.toml` lacks a row for any `Q-` id defined in Appendix B, or if `git status --porcelain` is non-empty after the run.

#### 0.4.6 What this regime deliberately does not do

It does not add a coverage-percentage ritual on top of itself. Coverage is a gate in Phase 1 at ≥70 % per component (OQ-17 resolved, D-31), but the regime above exists because **Phase 0 proved coverage cannot see this class of defect at all** — the broken gateway code was covered. Coverage bounds the untested surface; §0.4 bounds the *falsely* tested surface. Both are required and neither substitutes for the other.

### 0.5 Inherited debt — prerequisites, not optional cleanup

`PROGRESS.md` records eleven outstanding items from Phase 0. Five are load-bearing for Phase 1 and are ordered below so the generation pipeline is never built on unproven ground. Each has an owning deliverable and an evidence bar naming a **real** CI job. `REVIEW-PHASE-0.md`'s P2 list supplies the rest.

The ordering is the point: **debt items D1 and D2 must land before any §1.5 generation code**, because §1.5 sits directly on six-tier routing and on containers that CI has never actually started.

| # | Debt | Why it blocks Phase 1 | Owning §  | Evidence bar (CI job) |
|:--|:---|:---|:---|:---|
| **D1** | **`load_tier_config` has no production caller.** `main.py` never builds the model router from `config/model-tiers.yaml`, so the shipped YAML is never what a running backend loads. `${VAR}` expansion works and rejects unset names, but the six-tier chain criterion 17 exercised is assembled from test fixtures only | §1.5's entire generation pipeline depends on six-tier routing. Building generation on a router whose configuration source is unproven repeats Phase 0's central mistake at a larger scale | §11.1, §11.5.4 | `backend`: `test_wiring_tier_config.py` asserts the tier set on `production_app.state.router` is byte-derived from `config/model-tiers.yaml` — it mutates a copy of the YAML in a temp dir, points `MODEL_TIER_CONFIG_PATH` at it, rebuilds via `create_app()`, and asserts the running app's tiers changed. **Q-27** |
| **D2** | **`compose-smoke` only runs `docker compose config`.** It never runs `docker compose up -d --wait` and never builds either image, so criterion 4's own wording and the container half of criterion 1 rest on local runs | Phase 1 adds four services (Authentik, Cerbos, Infisical, OPA promoted to a hard dependency) and an `e2e` journey that must start real containers. A smoke job that never starts anything cannot gate that | §8.3, §13.3 | `compose-smoke`: builds `backend` and `frontend` images, runs `docker compose up -d --wait`, asserts the exact default service set is healthy, then `docker compose --profile vault --profile auth up -d --wait` for the optional set |
| **D3** | **No Playwright `e2e` job exists**, though `ci.yml`'s header comment claims one | Criterion 10 is an end-to-end user journey. Asserting it from unit tests would be the same category error Phase 0 made | §8.3, §12.6 | new `e2e` job: Playwright against built containers, running the criterion-10 journey (§12.6). Also carries criterion 5's diff/approve/apply flow |
| **D4** | **`pnpm audit` is non-gating** (`|| true`) and **`govulncheck` is installed from `@latest`**; `golangci-lint@v1.62.2` is pinned by a mutable tag; `pip install pre-commit` / `pip install pip-audit` are unpinned | Phase 1 adds ~14 Go and ~6 frontend dependencies. An advisory-only frontend gate and an unpinned scanner mean the supply-chain posture degrades exactly as the surface grows | §8.4, §16 | `audit`: `|| true` removed; every tool installed from a checksum-verified pinned module (`agent/tools/go.mod` + `go.sum`) or a hash-pinned pip requirement. `scripts/check-no-latest.sh` greps every workflow and script for `@latest` and fails |
| **D5** | **`infisical/infisical:v0.91.1` is not digest-pinned** while every other Compose image is. The second half of this row — "OPA runs the non-rootless variant where Phase 0 §13.3 specified `1.4.2-rootless`" — **was factually wrong and is corrected by D-51**: OPA 1.x publishes no `-rootless` tag, and `openpolicyagent/opa:1.4.2` already runs as `USER 1000:1000` on a Chainguard base, so the security intent was already met | §1.8 uses Infisical for real, and §1.7/§1.10 make OPA a hard dependency rather than an optional one | §13.3, D-51 | `compose-smoke` + `pre-commit`: `scripts/check-compose-validate.py` extended to fail if **any** image reference lacks `@sha256:`, if a `<committed-digest>` placeholder survives, or if any service overrides its image's runtime user back to root; `compose-smoke` additionally asserts `id -u` inside the running `opa` container is not `0` — a runtime proof of non-root, which a tag-name substring never was |

Carried forward as smaller items, each with an owner in §8 or §13:

- **`.gitattributes` marks all four lockfiles `-diff`**, hiding supply-chain-relevant diffs from review. Phase 1 drops `-diff` and keeps `linguist-generated` (§8.5). This is a review-integrity fix, not cosmetics: a lockfile diff is the highest-signal artifact in a dependency bump.
- **GitHub secret scanning is disabled on the repository** — a repository setting only the owner can change (OQ-21, unchanged). Local gitleaks remains the only secret-scanning evidence; §14.5 records the consequence.
- **`.kiro/steering/agent-autonomy.md` is untracked**, so the file-preservation rules do not survive a fresh clone. Recorded, owner's call; this design does not track it unilaterally because it is a workflow rule rather than product code.
- **P-09's route-level redaction clause** was proven decorative by the review and repaired by D-27. Phase 1's new routes inherit the repaired assertion, and Q-24 extends it to audit records and agent-side logs.
- **P-07's shutdown-timeout clause is asserted against instantaneous closers**, so the timeout is untested. Phase 1's agent gains long-running subsystems (session manager, watcher, executor); §10.4 tightens the assertion with a deliberately slow closer.

### 0.6 Numbering continuity (binding)

| Series | Phase 0 range | Phase 1 starts at | Rule |
|:---|:---|:---|:---|
| Decisions | D-1 … D-27 | **D-28** | ADR-style. A decision that changes an inherited contract must name what it supersedes |
| Correctness properties | P-01 … P-15 | **Q-01** | `P-` is retired. Phase 1 never reuses or renumbers a `P-` id; Phase 0's fifteen properties continue to run unchanged |
| Open questions | OQ-3 … OQ-21 live | **OQ-22** | Phase 0 questions falling due in Phase 1 are resolved into decisions or explicitly re-deferred in §17.2 |

Phase 0 questions that fall due in this phase, with their Phase 1 disposition (detail in §17.2):

| Question | Phase 0 state | Phase 1 disposition |
|:---|:---|:---|
| **OQ-6** Windows process-tree termination | shipped `taskkill /T /F` | **Resolved** — Job Objects via `golang.org/x/sys/windows`, still cgo-free (D-37) |
| **OQ-7** GitHub App vs PAT | `EnvTokenSource` behind `TokenSource` | **Resolved** — `AppInstallationTokenSource` lands in Phase 1 (D-38) |
| **OQ-16** durable engine at P2 | `TaskDispatcher` kept neutral | **Re-deferred deliberately.** ARQ lands behind the seam (D-32); Temporal and Inngest both stay open |
| **OQ-17** coverage gate or goal | goal in Phase 0 | **Resolved** — a per-component gate at ≥70 % (D-31) |
| **OQ-20** source of agent blast radius | `MCP_AGENT_BLAST_RADIUS` env var | **Resolved** — derived from attested agent identity; env var demoted to a dev-only default (D-39, Q-30) |
| **OQ-15** tenant seam | nullable, no RLS | **Partially resolved** — `TenantContextMiddleware` fills middleware row 6 and `SET LOCAL app.tenant_id` is issued per transaction; RLS policies and `NOT NULL` stay deferred to Phase 2 (D-35) |
| **OQ-3, OQ-4, OQ-13** | recommendations implemented | **Confirmed unchanged** — stdlib logging; hypothesis / rapid / fast-check; `pyjwt[crypto]` |
| **OQ-10, OQ-11, OQ-18, OQ-21** | non-blocking | **Unchanged.** No Phase 1 deliverable depends on any of them |

---

## 1. Scope Boundary — In Scope Now vs Architectural Context Only

This is the controlling section. If any later section appears to describe work outside §1.1, §1.1 wins.

### 1.1 In scope for Phase 1

Every row maps to a `phases.md` Phase 1 deliverable. Nothing outside this table is built.

| Group | Deliverable | Authority |
|:---|:---|:---|
| **1.1** | Agent pairing and connection: JSON-RPC 2.0 over WSS on Phase 0's fixed envelope; the nine message types; auto-reconnect (1 s → 60 s, jitter 0.5×); mTLS + JWT handshake; 6-char pairing code → revocable device token, 5-min expiry; heartbeat 30 s / timeout 90 s; command envelope with `approval_id`, `policy_context`, HMAC-SHA256 `signature`; named-operation whitelist (never arbitrary shell); agent-side policy evaluation; replay protection | phases.md 1.1, PRD §2.2 1–4, NFR-05/08/12/16/17 |
| **1.2** | Multi-project workspace: project CRUD (GitHub import + local path), per-project settings (LLM budget, basic policies); frontend project list with search/tags/favourites, project detail, recent-activity feed; agent registers a project directory and watches it | phases.md 1.2, PRD FR-01…FR-06 |
| **1.3** | Codebase analysis engine: tiered language detection; dependency-graph builder; recursive scanner honouring `.gitignore` + `.dockerignore`; size/type filters; **tree-sitter AST parsing**; **cAST semantic chunking**; metadata enrichment; embeddings (Voyage Code 3 API / BGE-M3 local) with hybrid sparse-dense (BM25 + vector); pgvector HNSW storage with tuned `ef_search`; cold-start discovery mode; fsnotify watch mode with fan-out/fan-in; backend Codebase Index API; **dependency-graph-aware incremental scanning** | phases.md 1.3, Research §C10, Tech-Stack perf item 1, PRD FR-08…FR-15 |
| **1.4** | Deployment readiness analysis: weighted scoring engine over the six named categories; checklist checks; plain-language report with "why it matters"; frontend score display with radar chart, category breakdown, recommendations | phases.md 1.4, PRD FR-16…FR-21 |
| **1.5** | AI file generation and validation pipeline: RAG from the Codebase Index (hybrid retrieval, 3× over-retrieve + `voyage-rerank-2`); six-tier routing with the fallback cascade; circuit breaker; Pydantic v2 strict structured output; MCP Gateway tool access; SSE streaming on the six existing event types; tiered semantic cache L1/L2/L3; **Safe Default Template Library** (8 languages × 5 artifact classes); evaluation pipeline (deterministic checks blocking + LLM-as-judge rubric advisory); cold-start progressive UX; agent validators (compose, K8s server-side dry-run, tofu validate+plan, YAML+JSON Schema, Helm lint+template); validation-feedback loop bounded at 3 iterations then template fallback; Plan Analyzer on generated plans; the generated artifact set | phases.md 1.5, Research §0/§A0b/§A0c/§C10/§C11, PRD FR-22…FR-31, NFR-03/04 |
| **1.6** | Change Approval Center: change-set CRUD (create, validate, approve, reject, apply); automatic timestamped backup before apply; atomic all-or-nothing application; frontend side-by-side and unified diff, approve/reject with comment, per-project change history timeline; agent backup-before-mutate and atomic file operations | phases.md 1.6, PRD §2.2 6–7, FR-29…FR-31, NFR-20/21 |
| **1.7** | Policy engine (basic): OPA integration for evaluation; policy CRUD; pre-defined templates (scheduling, file restrictions); agent mirrors rules locally for zero-trust enforcement; frontend policy list/editor and violation display; the three named policies implemented | phases.md 1.7, PRD FR-32…FR-38 |
| **1.8** | Secret management (basic): Infisical for encrypted storage; secret CRUD per project and per environment; agent-side Gitleaks scanning during analysis; **secret redaction before LLM context**; deploy-time injection as environment variables; frontend secret vault UI | phases.md 1.8, PRD FR-40…FR-45, NFR-09/10 |
| **1.9** | Audit logging: immutable audit log for all actions with who / what / when / why / before-state / after-state, covering agent-side operations; frontend audit log viewer | phases.md 1.9, PRD NFR-14 |
| **1.10** | **Agent Governance Control Plane**: one enforced chokepoint routing every mutating action through policy evaluation → approval gate → change-set compilation → blast-radius check → audit record → rollback handle; no agent mutation bypasses it; OPA policy evaluation embedded in the Go agent for the agent half of double evaluation; SPIFFE/SPIRE X.509-SVID + mTLS with attestation for workload identity, no long-lived agent keys | phases.md 1.10, Research §5.1 P0 #3b, §B7, §H31 |
| **1.11** | Auth integration: Authentik container; OIDC/OAuth2 login flow; JWT lifecycle; device/agent token flow; basic RBAC (admin, developer, viewer) | phases.md 1.11, Tech-Stack §8, PRD FR-07 (roles only) |
| **Debt** | The five inherited-debt items of §0.5, each with its own evidence bar | `PROGRESS.md` outstanding items, `REVIEW-PHASE-0.md` P1/P2 |
| **Regime** | The §0.4 test-integrity mechanisms: `production_app` fixture, call-site conformance, `check-test-doubles.py`, `check-no-skips.py`, the mutation harness | D-23, `REVIEW-PHASE-0.md` Pass 8 |
| **Progress** | `PROGRESS.md` updated to Phase 1 in the same commits as the work | Phase 0 §18 (inherited obligation) |

### 1.2 Explicitly excluded from Phase 1

**Quoted verbatim from `phases.md` Phase 1 "Excluded (for this phase)":**

> - ❌ Multi-environment management
> - ❌ Docker/K8s management dashboards
> - ❌ Deployment automation
> - ❌ AI Command Center (NL commands)
> - ❌ Monitoring/observability
> - ❌ Self-healing
> - ❌ Learning history

Derived exclusions, each with its owning phase and the reason it is not pulled forward:

| Excluded | Owning phase | Reason |
|:---|:---|:---|
| **DeepEval** | Phase 2 | Tech-Stack places LLM evaluation in CI at Phase 2. Phase 1's §1.5 evaluation pipeline is deterministic checks + an advisory rubric, which needs no eval framework. The golden dataset DeepEval would score does not exist yet (Research §C8) |
| **LangFuse** | Phase 2 | Production AI observability; Research §0 lists it at Phase 2. Phase 1 records `attempts`, tier, cache tier and judge scores in its own tables, which is what the phase's decisions need |
| **OTel SDK and Collector** | Phase 3 | `phases.md` §3.2. Phase 1 continues Phase 0's W3C propagation + `Tracer` seam and adds no exporter (§7.9, inherited) |
| **ArgoCD, Argo Rollouts** | Phase 2 | GitOps deployment and progressive delivery are deployment automation, excluded verbatim above |
| **Cilium** | Phase 3 | Service mesh; Research §0 "New Technologies" places it at Phase 3. Phase 1 has no mesh-shaped problem |
| **Kyverno** | Phase 2+ | Kubernetes admission control. Phase 0 §5.4 already fixed the placement table; Phase 1 owns only the OPA rows |
| **Novu** | Phase 2 | Notification centre; `phases.md` §2.6. Phase 1 surfaces state through SSE and the audit viewer |
| **Temporal / Inngest** | Phase 2 | Exactly one durable engine at the P2 boundary, behind the existing seam. OQ-16 stays open; Phase 1 must not assume either (D-32) |
| **Celery** | never | Banned permanently by Research §0 |
| **KEDA, CloudNativePG, PgBouncer deployment, Redis Cluster** | Phase 2 | Scaling infrastructure. Phase 1 honours PgBouncer's *constraints* (`SET LOCAL`, `statement_cache_size=0`) without deploying it (§7.12) |
| **ECharts beyond one radar chart; xterm.js; React Flow; CodeMirror beyond the policy editor; TanStack Table beyond two read-only tables; D2** | Phases 2–4 | Every frontend library added in Phase 1 is justified against a specific §1.4 or §1.6 requirement in §16.3. Nothing is added "for later" |
| **Multi-user project sharing, teams** | Phase 2 | PRD FR-07 is P2. Phase 1 ships the three RBAC roles but no `teams` / `team_members` tables (D-40) |
| **Offline *execution* of a queued mutation** | Phase 2 | Queueing itself **is in scope** — NFR-18 is a P1 requirement and D-41 delivers it as a durable outbound journal with queue-and-revalidate. What is excluded is applying anything from that journal without a fresh chokepoint transit: nothing that authorises a mutation is ever written to disk (§10.3, Q-31) |
| **Native Anthropic / Google protocol codecs** | Phase 2 | Phase 1 reaches those models through their OpenAI-compatible surfaces instead (D-42, §15.6). Prompt caching and extended-thinking features that only the native APIs expose are deferred with them |
| **`tofu apply`** | Phase 2 | `iac.Runner` exposes `Validate` and `Plan` only. Phase 1 does not add `apply`; that is deployment automation |
| **RLS policies, `tenant_id NOT NULL`** | Phase 2 | D-35. Phase 1 fills the middleware row and issues the transaction-scoped variable; enabling policies needs a second tenant to be meaningful |

### 1.3 Structural artifacts, seams and stubs (inherited rule, Phase 1 application)

Phase 0 §1.3's three-way distinction is inherited verbatim and is not restated. Its Phase 1 application:

| Directory | Phase 0 state | Phase 1 state |
|:---|:---|:---|
| `backend/src/auth/` | structural `README.md` | **real package** (§11.2) |
| `backend/src/generation/` | structural `README.md` | **real package** (§11.5) |
| `backend/src/policies/` | structural `README.md` | **real package** (§11.7) |
| `backend/src/secrets/` | structural `README.md` | **real package** (§11.8) |
| `backend/src/websocket/` | structural `README.md` | **real package** (§11.10) |
| `backend/src/governance/` | *did not exist* | **new real package** (§11.6) — the chokepoint |
| `backend/src/audit/` | *did not exist* | **new real package** (§11.9) |
| `backend/src/deployment/`, `monitoring/`, `incidents/`, `notifications/` | structural `README.md` | **unchanged — still structural.** Phase 2/3 own them |
| `agent/internal/executor/` | structural `README.md` | **real package** (§10.5), with a nested-internal mutation boundary |
| `agent/internal/policy/` | structural `README.md` | **real package** (§10.6) |
| `agent/internal/validator/` | structural `README.md` | **real package** (§10.7) |
| `agent/internal/devtools/` | structural `README.md` | **real package** (§10.10) |
| `agent/internal/scanner/` | seam: `Watcher` + fsnotify | **extended** with AST parsing, cAST chunking, dependency graph, incremental closure (§10.8) |
| `frontend/features/` | structural `README.md` | **real feature directories** for the §1.2/§1.4/§1.6/§1.7/§1.8/§1.9 surfaces (§12) |

The rule that survives unchanged: a directory becomes a package **only when its owning deliverable is implemented**, and no Phase 2+ directory gains an importable placeholder. A Compose service is likewise declared only by the task that supplies its configuration and its integration evidence (inherited Phase 0 §2.2).

### 1.4 Seam disposition — every Phase 0 seam, dispositioned

One row per inherited seam. `as-is` means Phase 1 consumes it without modification; `extend` means Phase 1 adds to it without breaking existing callers; `replace` names the superseding construct and the decision that records it.

| Seam (Phase 0 §) | Disposition | Reason | Supersedes / recorded by |
|:---|:---:|:---|:---|
| **MCP Gateway** — OIDC verification (§11.4) | **extend** | Phase 1 adds a second accepted audience (the app API) and the `sub`→principal resolution that RBAC needs. The `iss` allowlist, JWKS cache and fail-closed behaviour are untouched | D-34 |
| **MCP Gateway** — header routing (§11.4) | **as-is** | Routing is a pure function of `Mcp-Method` + `Mcp-Name`; P-05 continues to hold. Phase 1 adds servers to the registry, not routing logic | — |
| **MCP Gateway** — Redis-authoritative TTL cache (§11.4) | **as-is** | Correct as built, including `SET PX min(ttlMs,max)` and the never-serve-after-expiry rule (P-06) | — |
| **MCP Gateway** — fail-closed OPA policy with `default allow := false` (D-25) | **extend** | Phase 1 adds `policies/agent/*.rego` for the governance policies and a bundle-versioning wrapper. `policies/mcp/gateway.rego` itself is unchanged, and its 27 tests keep passing | D-30 |
| **MCP Gateway** — blast-radius input | **replace** | `input.agent_blast_radius` no longer comes from `MCP_AGENT_BLAST_RADIUS`; it is derived from the attested agent identity. The Rego is unchanged because it was written against the input field, exactly as OQ-20 anticipated | **D-39** supersedes Phase 0 §13.1's env-var source; OQ-20 resolved |
| **MCP Gateway** — Tasks state machine with Lua CAS (§11.5, D-24) | **as-is** | Phase 1's long-running operations (scan, generate) use the `TaskDispatcher` seam, not MCP Tasks. MCP Tasks remains the gateway-level lifecycle for taskable tool calls | — |
| **MCP Gateway** — Apps sandbox hosting (§11.6) | **extend** | Phase 1 registers one real app: the approval form for §1.6, served under the existing strict CSP and `sandbox` attributes without `allow-same-origin` | — |
| **Model routing** — `EndpointRegistry`, `OpenAICompatibleEndpoint` (§11.7.1a) | **extend** | New endpoint descriptors for the Anthropic and Google OpenAI-compatible surfaces; the adapter is unchanged | **D-42** |
| **Model routing** — circuit breaker (§11.7.2) | **as-is** | 5 failures / 30 s → OPEN → 60 s → HALF-OPEN is correct and P-01-guarded | — |
| **Model routing** — fallback cascade (§11.7.3) | **extend** | The terminal `TerminalFallback` slot is filled by `TemplateLibraryFallback`. The router is not modified — that is what the slot was for | **D-43** |
| **Model routing** — tiered semantic cache (§11.8) | **extend** | Phase 1 constrains the cache's input type to `RedactedPrompt` so a cache key can never be computed over unredacted text (Q-13). Layer precedence and the staleness flag are unchanged | **D-44** |
| **Model routing** — BYO-key resolvers (§11.7.4) | **extend** | `InfisicalKeyResolver` gains a real Infisical backend and per-project scoping; `EnvKeyResolver` stays the dev default | — |
| **Model routing** — Redis/Lua token bucket on Redis `TIME` (§11.7.5) | **extend** | Same limiter, additional keyed routes (`/api/v1/generation/*`), plus a per-project LLM budget counter for FR-06. The Lua script is unchanged | — |
| **Model routing** — `load_tier_config` (§11.7) | **extend** | Given a production caller at last (debt D1). The signature `load_tier_config(path, env=None) -> TierConfig` is unchanged | Q-27 |
| **Validation pipeline** — stage-agnostic runner (§11.9) | **extend** | The `DryRun` stage is inserted **before** `Semantic`, exactly as Phase 0 §11.9 anticipated. `ValidationPipeline.run` and the `Stage` Protocol are unchanged | — |
| **Validation pipeline** — deterministic blast-radius analyzer (§11.9) | **as-is** | Deterministic and monotone (P-11). The governance chokepoint calls it; it does not change | — |
| **Validation pipeline** — `ApprovalGate` seam (§11.9) | **extend** | `ThresholdApprovalGate` is joined by `GovernanceApprovalGate`, which persists a change-set and awaits a human decision. The Protocol is unchanged, which is why Phase 0 shipped it | — |
| **Agent** — `connection.Transport` + `WSSTransport` (§10.5) | **as-is** | The transport contract is correct. Phase 1 layers the session protocol **above** it, which is what the seam was for | — |
| **Agent** — `connection.ErrDisabled` (§10.5) | **extend** | Still the correct unpaired path. Phase 1 adds `ErrUnpaired` for "configured but no device token" so `agent doctor` can distinguish the two | — |
| **Agent** — `fileops.ApplyAtomic` (§10.10) | **replace** | Mutation must be unreachable without a verified envelope. `ApplyAtomic` moves behind `internal/executor/internal/mutate` and the exported entry point becomes `ApplyVerified(ctx, *envelope.Verified, entries)`. `UnifiedDiff` stays exported and unchanged | **D-45** supersedes Phase 0 §10.10's exported `Ops.ApplyAtomic` |
| **Agent** — `fileops` path blocklist (§10.10) | **extend** | The rule is right in intent and one case too wide: it blocks `.env.example`. Phase 1 splits it into read-intent and write-intent rules | **D-46** |
| **Agent** — `scanner.Watcher` + fsnotify (§10.9) | **extend** | Real implementation kept; Phase 1 adds debounce/coalescing and the incremental-closure driver | — |
| **Agent** — `iac.Runner` with `Validate`/`Plan` only (§10.6) | **as-is** | **`apply` is not added.** Phase 1 consumes `Plan` output as Plan-Analyzer input | — |
| **Agent** — `git.Client` + `TokenSource` / `EnvTokenSource` (§10.7, D-5) | **extend** | `AppInstallationTokenSource` is added behind the same interface, resolving OQ-7. Call sites are untouched, as D-5 promised | **D-38** |
| **Agent** — `mcp.NewServer` with `agent.health`, `agent.tofu.validate`, `agent.tofu.plan` (§10.8) | **extend** | Phase 1 adds **non-mutating** tools only: `agent.scan.status`, `agent.validate.*`, `agent.readiness.inventory`. Mutation never becomes an MCP tool — it travels only as a signed envelope (§2.2) | **D-47** |
| **Backend** — `TaskDispatcher` + `InlineDispatcher` (§7.9) | **extend** | `ArqDispatcher` is added; `InlineDispatcher` stays for tests and dev. No engine concept enters the Protocol | **D-32** |
| **Backend** — SSE event vocabulary (`core/sse.py`) | **as-is** | Exactly six types. Phase 1 producers use only these; a seventh would need its own decision, and none is proposed | Q-26 |
| **Backend** — RFC 9457 primitives with traceback redaction (§11.2, D-27) | **extend** | ~26 new problem-type suffixes under the same registry base URI; `detail` still never carries secrets, tokens, connection strings or tracebacks | Appendix C |
| **Backend** — W3C trace context (§7.8) | **as-is** | Propagation only. No exporter is added (Phase 3) | — |
| **Backend** — middleware stack, row 6 reserved for `TenantContextMiddleware` (§4.3) | **extend** | Phase 1 fills row 6. RLS policies stay deferred | **D-35** |
| **Data** — `Project`, `FileTreeEntry`, `Embedding` with HNSW at DIM 1536 (§6.2, D-2) | **extend** | Additive columns and eight new tables. `embeddings.embedding` stays `vector(1536)` and `model_id` stays `NOT NULL`. Self-hosted 1024-d vectors get their own table rather than sharing the column — this is D-2's deferred multi-model decision, now made | **D-48** |
| **Data** — nullable `tenant_id`, no RLS (D-2, OQ-15) | **extend** | Every new table carries the same nullable `tenant_id` seam, for consistency and to avoid a Phase 2 backfill | D-35 |
| **Data** — `with_ef_search` using `SET LOCAL` (§6.3) | **as-is** | The pattern Phase 1 copies for `app.tenant_id`. Never a session-level `SET` | §7.12 |
| **Build** — GoReleaser six targets, `CGO_ENABLED=0`, `-trimpath`, `mod_timestamp` (§8.2) | **as-is** | **Preserved exactly.** This is the whole point of D-29 | D-29 |
| **Build** — Syft → Cosign keyless → Rekor → SLSA v1 via `cosign attest-blob` (§8.1, D-20/20a/20b) | **extend** | Unchanged chain, plus a merged SBOM component list covering the vendored `.wasm` grammars that Syft cannot see | §8.6 |
| **Guard** — `deps_test.go` asserting tree-sitter is absent from `go.mod` (D-1) | **replace** | D-1's *conclusion* (no CGO in the six-target build) is preserved; its *mechanism* changes because the parser now arrives as Wasm. The guard becomes: no cgo-requiring dependency in the module graph, and the grammar blob set is digest-pinned | **D-29** supersedes D-1's guard form while preserving D-1's constraint |

### 1.5 The one inconsistency this section must resolve

Phase 0 honestly marked `anthropic_native` and `google_native` endpoints `available=false, reason="unsupported_protocol_phase_0"`. Yet `phases.md` §1.5 names **Claude Fable 5** as the high-tier backup and **Gemini 3 Flash** as the low tier. Left alone, the Phase 1 cascade could not reach two of its own named models: `high_coding` would fall from GPT-5.6 Sol straight past its declared secondary, and `low_logs` would have no primary at all.

**Resolution (D-42):** Phase 1 reaches both vendors through their **OpenAI-compatible surfaces** rather than writing native codecs. Two new endpoint descriptors are added with `protocol: openai_compatible` and vendor-specific compatibility base URLs (`ANTHROPIC_OPENAI_BASE_URL`, `GOOGLE_OPENAI_BASE_URL`), so the existing `OpenAICompatibleEndpoint` executes them with no new adapter. The `anthropic_native` / `google_native` descriptors **remain in the config, still marked unavailable**, because they document a real protocol the project may adopt later; they are honest data, not stubs (Phase 0 §5.6's rule, unchanged).

What this costs, stated rather than hidden: the compatibility layers do not expose every native feature — notably Anthropic's `cache_control` prompt caching and extended thinking, and Google's context caching. Phase 1's generation calls need chat completion with JSON-Schema-constrained output, which both layers provide, so nothing in scope is lost; the cost is a cost-optimisation opportunity deferred with the native codecs to Phase 2 (**OQ-24**). Every tier in §13.2 therefore has at least one *available* endpoint at every cascade position, and `GET /api/v1/ai/tiers` shows no tier whose primary is unavailable — asserted by a wiring test, not by reading the YAML.


---

## 2. Architecture

### 2.1 Target system architecture — Phase 1 (scope-annotated)

Solid boxes are built in Phase 1. **Dotted** boxes are Phase 2+ and must not be implemented, scaffolded or stubbed. Boxes marked `P0` were built in Phase 0 and are consumed as-is or extended per §1.4.

```mermaid
graph TD
    subgraph T1["TIER 1 — Frontend: Next.js 16 + React 19"]
        SHELL["App shell + RFC 9457 client<br/>P0 — as-is"]
        FPROJ["Projects: list, detail, activity<br/>§1.2 IN SCOPE"]
        FREAD["Readiness: score, radar, breakdown<br/>§1.4 IN SCOPE"]
        FGEN["Generation: SSE stream, progressive UX<br/>§1.5 IN SCOPE"]
        FAPP["Approval Center: diff, approve, history<br/>§1.6 IN SCOPE"]
        FPOL["Policy list + editor + violations<br/>§1.7 IN SCOPE"]
        FSEC["Secret vault UI<br/>§1.8 IN SCOPE"]
        FAUD["Audit log viewer<br/>§1.9 IN SCOPE"]
        FDASH["Docker / K8s dashboards, Command Center<br/>PHASE 2"]
    end

    subgraph T2["TIER 2 — Backend: FastAPI modular monolith"]
        AUTHR["auth: OIDC login, JWT lifecycle,<br/>device tokens, RBAC<br/>§1.11 IN SCOPE"]
        PROJ["projects: CRUD, settings, import<br/>§1.2 IN SCOPE"]
        ANA["analysis: index ingest, dep graph,<br/>incremental closure, readiness score<br/>§1.3 §1.4 IN SCOPE"]
        GEN["generation: RAG, 6-tier routing,<br/>judge, bounded loop, templates<br/>§1.5 IN SCOPE"]
        GOV["GOVERNANCE CONTROL PLANE<br/>the single mutation chokepoint<br/>§1.10 IN SCOPE"]
        POLB["policies: CRUD, OPA client,<br/>bundle publication<br/>§1.7 IN SCOPE"]
        SECB["secrets: Infisical, redaction chokepoint<br/>§1.8 IN SCOPE"]
        AUD["audit: append-only hash chain<br/>§1.9 IN SCOPE"]
        WSH["websocket hub: JSON-RPC 2.0 / WSS<br/>§1.1 IN SCOPE"]
        MCPGW["MCP Gateway<br/>P0 — extend"]
        ROUTER["Model Router + breaker + cache<br/>P0 — extend"]
        VPIPE["ValidationPipeline + Plan Analyzer<br/>P0 — extend with DryRun"]
        TASKS["TaskDispatcher → ArqDispatcher<br/>P0 seam — extend"]
        DEPL["deployment, monitoring, incidents,<br/>notifications<br/>PHASE 2 and 3"]
    end

    subgraph DATA["Data plane"]
        PG["PostgreSQL 17 + pgvector<br/>HNSW 1536 and 1024<br/>P0 — extend"]
        RDS["Redis Stack: L1/L2/L3 cache,<br/>BM25 sparse index, ARQ queue,<br/>revocation list, seq counters<br/>P0 — extend"]
        OPA["OPA server: gateway + governance policy<br/>P0 — extend"]
        CER["Cerbos v0.54.0 sidecar: app RBAC<br/>§1.11 IN SCOPE"]
        IDP["Authentik: OIDC IdP<br/>§1.11 IN SCOPE"]
        INF["Infisical: secrets + BYO keys<br/>§1.8 IN SCOPE"]
        PROV["OpenAI-compatible model endpoints<br/>+ voyage embed and rerank<br/>P0 — extend"]
    end

    subgraph T3["TIER 3 — Local Agent: Go 1.26 single static binary"]
        SESS["session: pairing, mTLS+JWT, heartbeat,<br/>reconnect, envelope verify, replay guard<br/>§1.1 IN SCOPE"]
        EXEC["executor: named-operation whitelist<br/>nested-internal mutation boundary<br/>§1.1 §1.6 IN SCOPE"]
        APOL["policy: embedded Rego evaluation<br/>agent half of double eval<br/>§1.10 IN SCOPE"]
        AVAL["validator: compose, K8s dry-run,<br/>tofu, YAML+schema, Helm, Trivy<br/>§1.5 IN SCOPE"]
        ASCAN["scanner: tree-sitter via wazero Wasm,<br/>cAST chunking, dep graph, watch<br/>§1.3 IN SCOPE"]
        ASEC["secrets: Gitleaks scan + redaction<br/>§1.8 IN SCOPE"]
        AID["identity: paired device cert or SPIFFE SVID<br/>§1.10 IN SCOPE"]
        AFILE["fileops: ApplyVerified + backup + diff<br/>P0 — replace entry point"]
        AIAC["iac: tofu validate and plan only<br/>P0 — as-is"]
        AGIT["git: go-git + go-github + App token<br/>P0 — extend"]
        AMCP["mcp server: non-mutating tools only<br/>P0 — extend"]
    end

    SHELL --> FPROJ & FREAD & FGEN & FAPP & FPOL & FSEC & FAUD
    FPROJ --> PROJ
    FREAD --> ANA
    FGEN -->|"SSE"| GEN
    FAPP --> GOV
    FPOL --> POLB
    FSEC --> SECB
    FAUD --> AUD

    AUTHR --> IDP
    AUTHR --> CER
    PROJ --> PG
    ANA --> PG
    ANA --> RDS
    ANA --> TASKS
    GEN --> ROUTER --> PROV
    GEN --> ANA
    GEN --> SECB
    GEN --> VPIPE
    GEN --> MCPGW
    GOV --> POLB --> OPA
    GOV --> VPIPE
    GOV --> AUD --> PG
    GOV --> WSH
    SECB --> INF
    TASKS --> RDS
    ROUTER --> RDS

    WSH <-->|"JSON-RPC 2.0 over WSS<br/>outbound-only from agent"| SESS
    SESS --> AID
    SESS --> EXEC
    EXEC --> APOL
    EXEC --> AFILE
    EXEC --> AVAL
    SESS --> ASCAN --> ASEC
    ASCAN -->|"index batches"| ANA
    AVAL --> AIAC
    EXEC --> AGIT
    SESS --> AMCP

    style FDASH stroke-dasharray: 6 4
    style DEPL stroke-dasharray: 6 4
```

Two structural facts the diagram encodes and §11.6 enforces:

- **Every arrow that ends in a mutation passes through `GOV`.** `FAPP → GOV → WSH → SESS → EXEC` is the only path from a user's approval to a file write. `GEN` cannot reach `WSH`, and `EXEC` cannot be reached from `AMCP`.
- **`ASCAN → ASEC` is not optional.** Scanner output reaches the backend only after redaction, which is why the redaction chokepoint appears on the agent side as well as in `SECB` (§7.11 explains the two-sided arrangement).

### 2.2 The governance chokepoint (§1.10) — the trust moat

This is the phase's defining structure. One path, six ordered stages, and three independent enforcement mechanisms that make bypass a build failure rather than a review miss.

```mermaid
graph TD
    subgraph ANY["Any caller that wants to change something"]
        C1["Generation pipeline §1.5"]
        C2["Approval Center §1.6"]
        C3["Policy-driven remediation §1.7"]
        C4["A future Phase 2 deployer"]
    end

    C1 & C2 & C3 & C4 -->|"MutationRequest — the ONLY input type"| G0

    subgraph GOVP["governance/ — the chokepoint. Sole minter of MutationAuthority and sole holder of the envelope signing key"]
        G0["0. admit: resolve principal, project, tenant<br/>reject if no verified principal"]
        G1["1. policy evaluation<br/>OPA server, fail closed"]
        G2["2. approval gate<br/>ApprovalGate seam, P0"]
        G3["3. change-set compilation<br/>change_sets + change_items"]
        G4["4. blast-radius check<br/>SemanticPlanAnalyzer, P0, deterministic"]
        G5["5. audit record<br/>append-only hash chain, same transaction"]
        G6["6. rollback handle<br/>backup manifest + reverse change-set"]
        MINT["mint MutationAuthority<br/>sign command envelope<br/>HMAC-SHA256 per device key"]
    end

    G0 --> G1 --> G2 --> G3 --> G4 --> G5 --> G6 --> MINT

    MINT -->|"signed envelope only"| HUB["websocket hub"]
    HUB -->|"WSS"| AV["agent: envelope verifier"]
    AV -->|"verify signature, nonce, seq, expiry,<br/>approval_id, policy_context digest"| AP["agent: embedded Rego<br/>second, independent evaluation"]
    AP -->|"Verified envelope"| AX["executor: named-operation dispatch"]
    AX --> MUT["internal/executor/internal/mutate<br/>fileops.ApplyVerified"]
    MUT -->|"ApplyReport + backup manifest"| RES["command.result"]
    RES --> G5

    G1 -.->|"deny"| DEN["403 policy-denied<br/>audit record written<br/>no envelope minted"]
    G2 -.->|"pending"| PEND["202 approval-required<br/>change-set persisted, no envelope"]
    G4 -.->|"block"| BLK["409 blast-radius-blocked<br/>audit record written"]
    AP -.->|"agent denies"| ADEN["agent.error policy-denied<br/>nothing mutated, audit record written"]

    style DEN stroke-dasharray: 4 3
    style PEND stroke-dasharray: 4 3
    style BLK stroke-dasharray: 4 3
    style ADEN stroke-dasharray: 4 3
```

#### 2.2.1 The three enforcement mechanisms

Design intent is not enforcement. These three are.

**(1) A capability type only the control plane can mint — Python.**

Every mutation primitive requires a `MutationAuthority` argument. The type cannot be constructed outside `governance/`, because its `__init__` demands a module-private sentinel:

```python
# backend/src/governance/authority.py
_MINT_SENTINEL = object()          # module-private; never exported, never re-exported

@dataclass(frozen=True, slots=True)
class MutationAuthority:
    """Proof that a mutation traversed the full chokepoint.

    Constructing this outside governance/ raises TypeError, because _MINT_SENTINEL
    is module-private and Ruff's banned-api rule forbids importing it. Every
    mutation primitive takes one as a REQUIRED argument, so omitting it is a
    call-site error that §0.4.2's conformance test catches in milliseconds — not a
    review miss.
    """
    change_set_id: uuid.UUID
    approval_id: uuid.UUID
    policy_bundle_digest: str
    blast_radius: BlastRadius
    audit_seq: int
    envelope_digest: str
    _sentinel: InitVar[object]

    def __post_init__(self, _sentinel: object) -> None:
        if _sentinel is not _MINT_SENTINEL:
            raise TypeError(
                "MutationAuthority may only be minted by governance.chokepoint; "
                "see design §2.2.1 and Q-03"
            )
```

**(2) A banned-api rule confining mutation primitives — the same pattern already used for queue SDKs.**

Phase 0 confined `celery`/`arq`/`dramatiq`/`temporalio`/`inngest` imports to `core/tasks.py` with a Ruff `flake8-tidy-imports` `banned-api` rule. Phase 1 reuses the mechanism verbatim:

```toml
# backend/pyproject.toml — [tool.ruff.lint.flake8-tidy-imports.banned-api]
"src.governance.authority._MINT_SENTINEL".msg = "Only governance.chokepoint may mint authority (design §2.2.1)"
"src.governance.envelope.sign_envelope".msg   = "Envelope signing belongs to governance.chokepoint only"
"src.governance.envelope._SIGNING_KEY".msg    = "The envelope signing key is owned by governance/ (design §2.2.1)"
"src.websocket.hub.send_command".msg          = "Commands reach the hub only from governance.chokepoint"
```

**(3) A compiler-enforced boundary and a reachability check — Go.**

Go's nested-`internal` rule is a real compile-time boundary: a package under `agent/internal/executor/internal/…` is importable **only** by packages rooted at `agent/internal/executor/`. Phase 1 uses it to make the mutation primitive physically unreachable from anywhere else:

```
agent/internal/executor/
├── executor.go                  # named-operation dispatch; takes *envelope.Verified
├── contract_test.go             # var _ Dispatcher = (*dispatcher)(nil)
└── internal/
    └── mutate/
        ├── apply.go             # ApplyVerified — the ONLY write path
        └── apply_test.go
```

`agent/internal/fileops` keeps `UnifiedDiff` and the path-validation helpers exported, and its write implementation moves into `executor/internal/mutate` (D-45). Any package outside the executor subtree that tries to import it **does not compile** — no lint, no review, no discipline required.

Reachability is additionally checked, because a boundary can be widened by a well-meaning refactor:

```bash
# scripts/check-chokepoint.sh  (runs in the `agent` and `backend` CI jobs)
#  Go:     assert no import of executor/internal/mutate outside executor/**
#  Python: AST-walk src/**, assert every call to a @mutation_primitive-decorated
#          function is lexically inside src/governance/ OR receives a
#          MutationAuthority argument; fail listing offenders.
```

**Invocation:** `bash scripts/check-chokepoint.sh` (also `make lint`, and a `pre-commit` local hook on `^(agent|backend)/`).
**Input:** two enumerations, both derived rather than hand-listed — `go list -deps -json ./...` from `agent/` for the import graph, and an `ast` walk of `backend/src/**/*.py` for the Python half. The Python half discovers primitives by scanning for the `@mutation_primitive` decorator, so a newly marked function is covered without editing the script.
**Failure condition:** exit `1` on either half, printing `path:line: mutation primitive '<name>' called outside governance/ without MutationAuthority` or `package <p> imports executor/internal/mutate`. It also exits `1` if the discovered primitive set is **empty**, since a renamed decorator would otherwise make the check trivially pass. Exit `0` only when both enumerations are non-empty and clean.

**Q-03** is the property that quantifies this: for every generated call graph, no mutation primitive is reachable without a `MutationAuthority` / `*envelope.Verified`. Its negative control removes the sentinel check, and the property must then fail.

#### 2.2.2 Why the signing key placement matters

The control plane is the **sole holder of the per-device envelope signing key**. The hub does not have it; the generation pipeline does not have it; the policy module does not have it. Consequence, stated as a security property rather than a hope: **an unsigned or wrongly-signed envelope is rejected by the agent regardless of backend bugs.** A compromised or buggy code path elsewhere in the backend can, at worst, ask the hub to deliver bytes the agent will refuse. That is the difference between a defence and a convention.

### 2.3 What runs after `docker compose up` in Phase 1

The default profile grows from five services to seven. Authentik and Cerbos become default-profile because §1.11's deny-by-default posture means the API is unusable without them; Infisical stays behind the `vault` profile because `EnvKeyResolver` remains a valid dev path (Phase 0's arrangement, unchanged).

```mermaid
graph LR
    DEV["Developer"] -->|"localhost:3000"| FE["frontend"]
    DEV -->|"localhost:8000"| BE["backend"]
    DEV -->|"localhost:9000"| AK["authentik-server<br/>OIDC IdP"]
    FE -->|"/api/v1"| BE
    BE -->|"asyncpg"| PG["postgres<br/>pgvector/pgvector:pg17"]
    BE -->|"RESP + FT.SEARCH"| RD["redis<br/>redis-stack-server"]
    BE -->|"HTTP /v1/data"| OPA["opa 1.4.2-rootless"]
    BE -->|"HTTP :3592 /api/check/resources"| CB["cerbos 0.54.0"]
    BE -->|"JWKS + token"| AK
    AK --> PG
    AK --> RD
    BE -.->|"profile vault"| INF["infisical<br/>digest-pinned"]
    AGENT["forgeops-agent binary<br/>NOT a service"] -.->|"outbound WSS only"| BE
    AGENT2["agent-dev<br/>profile tools<br/>tofu + trivy + helm"] -.-> AGENT
```

`make up` starts exactly `postgres`, `redis`, `opa`, `cerbos`, `authentik-server`, `authentik-worker`, `backend`, `frontend`. The agent remains a **binary, not a default-profile service** (inherited Phase 0 §2.2) — it is what the user installs on their own machine, and §1.1 exists so it can find the backend from there.

Readiness semantics change in one respect and are stated explicitly because a health contract is expensive to alter later: `GET /health/ready` now checks Postgres, Redis, OPA and Cerbos. Authentik is **not** a readiness dependency — the backend is a resource server, so an IdP outage must degrade *login*, not liveness or readiness of already-authenticated traffic. `/health` remains dependency-free (Phase 0 §4.4, unchanged).

### 2.4 Monorepo additions

Only additions are shown; everything else is inherited from Phase 0 §2.3 unchanged. `[+]` marks a path not named by PRD §8, each justified.

```
agent/
├── internal/
│   ├── session/                     # §1.1 [+] pairing, handshake, heartbeat, reconnect, dispatch
│   │   ├── pairing.go  replay.go  reconnect.go  heartbeat.go
│   │   └── journal.go               # §1.1 D-41 durable outbound journal (NFR-18)
│   ├── envelope/                    # §7.6 §10.4 [+] LEAF: Envelope, Verified, CanonicalBytes, Verifier (D-59)
│   ├── identity/                    # §1.10 [+] device cert or SPIFFE SVID behind one Provider seam
│   ├── executor/                    # §1.1 §1.6 named-operation dispatch (was structural)
│   │   └── internal/mutate/         # [+] compiler-enforced mutation boundary (D-45)
│   ├── policy/                      # §1.10 embedded Rego evaluation (was structural)
│   │   └── bundle.go                # bundle digest, staleness, fail-closed
│   ├── validator/                   # §1.5 compose, k8s, tofu, yaml, helm, trivy (was structural)
│   ├── devtools/                    # §1.5 external-tool discovery + version report (was structural)
│   ├── scanner/                     # §1.3 extended: langdetect, ast, cast, depgraph, incremental
│   │   └── grammars/                # [+] vendored tree-sitter .wasm + grammars.lock.json (D-29)
│   └── secretscan/                  # §1.8 [+] gitleaks detection + redaction
└── tools/                           # [+] pinned dev tooling module (go.mod + go.sum) — fixes debt D4
backend/
├── src/
│   ├── auth/                        # §1.11 (was structural)
│   ├── generation/                  # §1.5 (was structural)
│   │   ├── rag/  prompts/  judge/  loop/  templates/
│   ├── governance/                  # §1.10 [+] the chokepoint — not in PRD §8, justified in §11.6
│   ├── audit/                       # §1.9 [+] append-only chain — not in PRD §8, justified in §11.9
│   ├── policies/                    # §1.7 (was structural)
│   ├── secrets/                     # §1.8 (was structural)
│   └── websocket/                   # §1.1 (was structural)
├── tests/
│   ├── integration/production_app.py  # §0.4.1 the app-factory fixture
│   ├── meta/                          # §0.4.3 tests for the test-integrity lints
│   └── mutation/mutations.toml        # §0.4.5 negative-control declarations
policies/
├── mcp/gateway.rego                 # P0 — unchanged, 27 tests keep passing
└── agent/                           # §1.7 §1.10 [+] governance policies, published as a bundle
    ├── governance.rego  schedule.rego  paths.rego  approval.rego
    └── *_test.rego
frontend/
├── app/(shell)/projects/…            # §1.2 §1.4 §1.5 §1.6 §1.7 §1.8 §1.9 route groups
├── features/{projects,readiness,generation,approval,policies,secrets,audit}/
└── e2e/{pairing,journey,approval}.spec.ts   # §12.6 criterion 10
scripts/
├── check-test-doubles.py  check-no-skips.py  collect_call_sites.py
├── check-chokepoint.sh    check-route-auth.py  check-no-latest.sh
├── check-ci-jobs.py       check-coverage.sh    check-db-roles.py
├── check-go-interface-assertions.sh  mutation-harness.py
└── sbom-merge.py                    # §8.6 injects wasm components Syft cannot see
```

Two `[+]` directories are not in PRD §8 and need a reason on the record. `backend/src/governance/` exists because §1.10 is a first-class deliverable and PRD §8 has no module for it — the same situation as `backend/src/mcp/` in Phase 0, resolved the same way. `backend/src/audit/` exists because PRD §8 has no module for §1.9 either, and folding audit into `core/` would make the append-only boundary a convention instead of a package.

### 2.5 Trust boundaries and identity topology

Four boundaries, stated once so §14 can reason about them and §10/§11 can implement to them.

| # | Boundary | Who holds what | Crossing mechanism |
|:-:|:---|:---|:---|
| 1 | Browser ↔ backend | Browser holds a short-lived access token from Authentik; backend verifies via JWKS | OIDC authorization code + PKCE; every route requires a principal unless in the public set (§4.4) |
| 2 | Backend ↔ agent | Backend holds the per-device envelope key and the internal CA; agent holds its device token, its client certificate and the current policy bundle digest | mTLS + JWT handshake, then JSON-RPC 2.0 over WSS; every mutating message is a signed envelope (§7.6) |
| 3 | Backend ↔ model providers | Backend resolves keys via `KeyResolver`; keys never reach the agent or the browser | `OpenAICompatibleEndpoint` over `httpx` with `SecretStr` keys and redacted errors (P0, unchanged) |
| 4 | Agent ↔ user filesystem | Agent holds no server credentials for the filesystem; the path blocklist and root confinement live below every caller | `ApplyVerified` inside the nested-internal mutation package; rejects any path outside the registered root (P-08, extended by Q-01/Q-02) |

The agent has **zero inbound ports** (NFR-12) in Phase 1 as in Phase 0. Pairing does not open one: the pairing code travels *out* from the agent to the backend, which is why a 6-character code is viable at all (§3.1, §10.3).


---

## 3. Sequence and State Diagrams — Phase 1 flows

### 3.1 Agent pairing and trust establishment (§1.1)

Phase 0 shipped none of this. The flow below is the whole path: code generation, exchange, device-token issuance, certificate issuance, and the first authenticated session.

```mermaid
sequenceDiagram
    autonumber
    participant U as User (browser)
    participant FE as Frontend
    participant BE as Backend /api/v1/agents
    participant R as Redis
    participant PG as Postgres
    participant CA as Internal CA (governance-owned)
    participant AG as Agent CLI (user's machine)

    U->>FE: "Pair an agent" on a project
    FE->>BE: POST /api/v1/agents/pairing-codes  (bearer, project_id)
    BE->>BE: authorise: principal must be admin or developer on the project
    BE->>BE: code = crockford_base32(6) from crypto/urandom<br/>store only HMAC-SHA256(pepper, code)
    BE->>R: SETEX pair:<hmac> 300s {project_id, tenant_id, issuer_sub, attempts:0}
    BE->>PG: INSERT agent_devices (status=pending)
    BE-->>FE: 201 {code, expires_at, device_id}
    FE-->>U: show 6-char code + 5:00 countdown

    U->>AG: forgeops-agent pair --code ABC234 --backend wss://...
    AG->>AG: generate P-256 keypair in memory, build CSR (CN=device_id-less)
    AG->>BE: POST /api/v1/agents/pair/exchange  (PUBLIC route)<br/>{code, csr, agent_version, platform, fingerprint}
    BE->>R: Lua: INCR attempts; reject if >5 or missing; DEL on success (single-use)
    alt code unknown, expired, or attempts exhausted
        BE->>PG: audit: pairing_failed (no code value recorded)
        BE-->>AG: 401 problem+json  pairing-code-invalid
    else code valid
        BE->>BE: device_token = 32 random bytes; store HMAC-SHA256(pepper, token)
        BE->>BE: envelope_key = 32 random bytes (per device)
        BE->>CA: sign CSR → client cert, notAfter = now + 24h
        BE->>PG: UPDATE agent_devices SET status=active, token_hmac, envelope_key_enc, cert_serial
        BE->>PG: audit: device_paired (who, project, fingerprint, cert serial)
        BE-->>AG: 201 {device_id, device_token, envelope_key, client_cert, ca_bundle,<br/>policy_bundle, policy_bundle_digest, renew_after}
    end
    AG->>AG: persist device_token + envelope_key in OS keychain<br/>(0600 file fallback, reported by `agent doctor`)

    AG->>BE: WSS CONNECT with client cert (mTLS) + Authorization: Bearer <device_token>
    BE->>BE: verify cert chain against internal CA, not expired,<br/>fingerprint matches agent_devices row
    BE->>R: SISMEMBER devtok:revoked <device_id>
    alt revoked or cert invalid
        BE-->>AG: 401 then close 4401
    else accepted
        AG->>BE: {"jsonrpc":"2.0","id":"1","method":"session.connect",<br/>"params":{device_id, agent_version, platform, policy_bundle_digest, capabilities}}
        BE->>BE: compare policy_bundle_digest with current
        alt digest stale
            BE-->>AG: result {policy_bundle, policy_bundle_digest} — agent reloads before any mutation
        else digest current
            BE-->>AG: result {session_id, heartbeat_interval:30, heartbeat_timeout:90, seq_base}
        end
        loop every 30s, timeout 90s
            AG->>BE: session.heartbeat {seq, uptime, queue_depth}
            BE->>R: refresh session key TTL 90s; check revocation per message
            BE-->>AG: result {server_time, policy_bundle_digest}
        end
    end
```

Revocation mid-session, which is the case that makes the design non-trivial:

```mermaid
sequenceDiagram
    autonumber
    participant U as Admin
    participant BE as Backend
    participant R as Redis
    participant HUB as WS hub (any replica)
    participant AG as Agent (mid-operation)

    U->>BE: DELETE /api/v1/agents/{device_id}  (revoke)
    BE->>R: SADD devtok:revoked <device_id>  (authoritative, checked per message)
    BE->>R: PUBLISH forgeops:revocations {device_id}
    par proactive close
        R-->>HUB: revocation event
        HUB->>AG: agent.error {code:"device-revoked"} then close 4403
    and next-message enforcement
        AG->>HUB: command.progress {seq:N}
        HUB->>R: SISMEMBER devtok:revoked  → 1
        HUB-->>AG: agent.error device-revoked, close 4403
    end
    AG->>AG: abort in-flight operation; roll back from backup manifest;<br/>wipe device_token + envelope_key from keychain; enter unpaired state
    AG->>AG: `agent doctor` now reports "unpaired: revoked at <ts>"
```

The property that matters here is **Q-16**: revocation takes effect on the *next message*, not the next connection. The pub/sub close is an optimisation; the per-message `SISMEMBER` is the guarantee. A replica that missed the pub/sub event still refuses the next frame.

### 3.2 Analysis → generation → validation → approval → apply (§1.3 – §1.6)

The main product flow, and the one criterion 10 walks end to end.

```mermaid
sequenceDiagram
    autonumber
    participant U as User
    participant FE as Frontend
    participant BE as Backend
    participant TD as TaskDispatcher (ARQ)
    participant AG as Agent
    participant GEN as generation
    participant RT as ModelRouter
    participant VP as ValidationPipeline
    participant GOV as Governance chokepoint
    participant PG as Postgres

    U->>FE: Import project (local path or GitHub)
    FE->>BE: POST /api/v1/projects
    BE->>PG: INSERT projects
    BE->>AG: command.execute {op:"project.register", root, ignore_rules}
    AG->>AG: cold-start discovery: heuristics only (langs, manifests, existing configs)
    AG-->>BE: command.result {inventory, partial:true}
    BE-->>FE: SSE status + progress (partial results visible immediately)
    BE->>TD: enqueue("index.full", {project_id})   # async full indexing

    AG->>AG: full scan: langdetect → dep graph → tree-sitter AST (wazero) → cAST chunks
    AG->>AG: gitleaks scan; redact secrets from every chunk BEFORE transmission
    AG-->>BE: index batches (file_tree, file_contents, chunks, dep edges)
    BE->>PG: upsert index; embed chunks (Voyage Code 3) → pgvector HNSW
    BE->>BE: build Redis BM25 sparse index over the same chunks
    BE-->>FE: SSE progress → complete

    U->>FE: "Analyse readiness"
    FE->>BE: POST /api/v1/projects/{id}/readiness
    BE->>BE: deterministic weighted scoring over the inventory (no LLM)
    BE->>PG: INSERT analysis_reports (score, categories, inventory_hash)
    BE-->>FE: 200 report → radar chart + breakdown + recommendations

    U->>FE: "Generate Dockerfile + K8s manifests"
    FE->>BE: POST /api/v1/generation/runs   (SSE response)
    BE->>GEN: run(project, artifact_kinds, budget)
    GEN->>GEN: hybrid retrieval: BM25 (Redis) + dense (pgvector), 3× over-retrieve
    GEN->>GEN: rerank via voyage-rerank-2 → top-k
    GEN->>GEN: assemble_prompt(RedactedChunk[]) — the ONLY prompt builder
    GEN->>RT: complete(tier=high_coding, RedactedPrompt, json_schema)
    RT-->>GEN: structured artifact set (Pydantic v2 strict)
    GEN-->>FE: SSE token stream while generating

    loop bounded: at most 3 iterations (Q-08)
        GEN->>VP: run(artifacts)  → Syntax → Schema → DryRun (agent) → Semantic
        VP->>AG: command.execute {op:"validate.compose"|"validate.k8s"|"validate.tofu"|"validate.helm"}
        AG->>AG: compose-go load; K8s server-side dry-run; tofu validate+plan; helm lint+template; trivy config
        AG-->>VP: findings (per validator, deterministic)
        VP-->>GEN: PipelineResult (blocking findings only)
        alt blocking findings and attempts remain
            GEN->>RT: regenerate with findings as feedback
        else clean
            GEN->>GEN: LLM-as-judge rubric — ADVISORY, cannot change the gate
        end
    end
    alt exhausted after 3 iterations
        GEN->>GEN: TemplateLibraryFallback — verified template for the detected language
        GEN-->>FE: SSE validation {source:"template", reason:"iteration-bound-exhausted"}
    end

    GEN->>GOV: MutationRequest {project, artifacts, origin:"generation"}
    GOV->>GOV: policy → approval gate → change-set → blast radius → audit → rollback handle
    GOV->>PG: INSERT change_sets + change_items + validations
    GOV-->>FE: 202 approval-required, change_set_id
    FE-->>U: diff preview (side-by-side and unified)

    U->>FE: Approve with comment
    FE->>BE: POST /api/v1/change-sets/{id}/approve
    BE->>GOV: approve(change_set_id, principal, comment)
    GOV->>PG: INSERT approvals; change_sets.status = approved
    GOV->>GOV: mint MutationAuthority; sign command envelope (per-device key)
    GOV->>AG: command.execute {op:"changeset.apply", approval_id, policy_context, signature, nonce, seq, not_after}
    AG->>AG: verify signature + replay guard + approval_id + bundle digest
    AG->>AG: embedded Rego: second, independent evaluation
    AG->>AG: ApplyVerified: backup every target, temp+fsync+rename, rollback on any error
    AG-->>GOV: command.result {applied, backup_manifest, per-file hashes}
    GOV->>PG: change_sets.status = applied; audit record; rollback handle retained
    GOV-->>FE: SSE complete
    FE-->>U: "Applied 4 files. Rollback available."
```

### 3.3 Dependency-graph-aware incremental rescan (§1.3)

```mermaid
sequenceDiagram
    autonumber
    participant FS as Filesystem
    participant W as scanner.Watcher (fsnotify)
    participant D as Debouncer (250 ms, coalescing)
    participant C as Closure computer
    participant P as Parser pool (fan-out)
    participant A as Aggregator (fan-in)
    participant BE as Backend index API

    FS-->>W: create / write / rename / remove events
    W->>D: raw events
    D->>D: coalesce per path; drop paths excluded by .gitignore/.dockerignore/size/type
    D->>C: changed = {added, modified, deleted}
    C->>C: reverse-dependency lookup over the persisted graph
    C->>C: dirty = changed<br/>  ∪ {f : f imports g, g ∈ changed, exports(g) changed}<br/>  ∪ {f : imports(f) changed}<br/>  ∪ dependants of deleted files
    par fan-out over dirty, bounded by GOMAXPROCS
        C->>P: parse + cAST chunk + enrich (file 1..n)
        P->>P: gitleaks scan + redact
    end
    P->>A: chunk sets + edge sets
    A->>A: compute deletions: chunks whose (file, index) vanished
    A->>BE: PATCH /api/v1/projects/{id}/index {upserts, deletions, edge_delta, base_version}
    BE->>BE: optimistic concurrency on base_version; re-embed only upserted chunks
    BE-->>A: new index_version
```

**Q-10** states the correctness condition: for every edit sequence, the incrementally maintained index equals what a full rescan of the final tree would have produced — including deletions, edge removals and stale-summary invalidation. **Q-11** covers the coalescing layer: dropping duplicate events must not shrink the dirty set.

### 3.4 Secret redaction before LLM context, and the cache-key clause (§1.8)

```mermaid
sequenceDiagram
    autonumber
    participant AG as Agent scanner
    participant SS as agent secretscan (gitleaks)
    participant BE as Backend index API
    participant RAG as generation.rag retriever
    participant RED as secrets.redaction (chokepoint)
    participant CTX as generation.context.assemble_prompt
    participant CA as TieredSemanticCache
    participant RT as ModelRouter

    AG->>SS: raw chunk text
    SS->>SS: detect: gitleaks ruleset + project-configured patterns
    SS-->>AG: RedactedChunk {text with findings replaced by FORGEOPS_REDACTED:<kind>:<hash8>}
    AG->>BE: only RedactedChunk ever crosses the wire
    BE->>BE: store redacted text; store finding metadata separately (kind, path, line — never the value)

    RAG->>BE: hybrid retrieval
    BE-->>RAG: RedactedChunk[]   (the store holds nothing else)
    RAG->>RED: second-pass redaction over the assembled candidate set
    RED-->>CTX: RedactedChunk[]  — assemble_prompt accepts NOTHING else
    CTX-->>CA: RedactedPrompt (a distinct type, constructible only by RED)
    CA->>CA: l1_key = sha256 over the RedactedPrompt — never over raw text
    alt cache hit
        CA-->>CTX: cached completion
    else miss
        CA->>RT: complete(RedactedPrompt)
        RT-->>CA: response; store keyed by the same RedactedPrompt
    end
```

Two clauses, both properties. **Q-12**: redaction precedes prompt assembly on every path, because `assemble_prompt` has no overload that accepts `str`. **Q-13**: a cache key is never computed over unredacted text, *and* a cached completion is unreachable from an unredacted prompt — the lookup signature requires `RedactedPrompt`, so there is no code path that could produce a colliding key from raw input.

### 3.5 OIDC login and RBAC (§1.11)

```mermaid
sequenceDiagram
    autonumber
    participant U as Browser
    participant FE as Frontend
    participant AK as Authentik
    participant BE as Backend
    participant CB as Cerbos
    participant PG as Postgres

    U->>FE: visit a protected route
    FE->>BE: GET /api/v1/auth/login (PUBLIC) → 302 to Authentik with PKCE challenge
    U->>AK: authenticate
    AK-->>FE: redirect to /api/v1/auth/callback?code=…&state=…
    FE->>BE: GET /api/v1/auth/callback (PUBLIC)
    BE->>AK: token exchange with PKCE verifier
    AK-->>BE: id_token + access_token + refresh_token
    BE->>BE: verify via JWKS: exact iss allowlist, aud, exp, nbf, sub required
    BE->>PG: upsert users; INSERT sessions (refresh token stored hashed)
    BE-->>FE: Set-Cookie httpOnly SameSite=Lax session id + access token in body
    FE->>BE: any protected route with Authorization: Bearer <access>
    BE->>BE: require_principal dependency (per-route, not global)
    BE->>CB: CheckResources {principal{roles}, resource{kind:project,id,attr}, actions}
    CB-->>BE: allow / deny per action
    alt deny
        BE-->>FE: 403 problem+json forbidden  (no resource existence disclosure)
    else allow
        BE-->>FE: 200
    end
```

### 3.6 State diagram — change-set lifecycle (§1.6)

```mermaid
stateDiagram-v2
    [*] --> draft : compiled by governance
    draft --> validating : validation pipeline runs
    validating --> rejected_by_policy : OPA deny
    validating --> blocked : blast radius = block
    validating --> pending_approval : verdict warn, or policy requires approval
    validating --> approved : verdict allow AND policy auto-approves
    pending_approval --> approved : human approves
    pending_approval --> rejected : human rejects
    pending_approval --> expired : approval TTL elapses
    approved --> applying : signed envelope delivered to the agent
    applying --> applied : ApplyReport success, all targets written
    applying --> rolled_back : any target failed, every write reverted
    applying --> conflicted : on-disk hash no longer matches change_items.old_content
    applied --> reverted : rollback handle used
    rejected_by_policy --> [*]
    blocked --> [*]
    rejected --> [*]
    expired --> [*]
    applied --> [*]
    reverted --> [*]
    rolled_back --> [*]
    conflicted --> [*]
```

Terminal states are absorbing; `applied → reverted` is the only transition out of a success state and it requires a fresh authority mint of its own, because a revert is itself a mutation. **Q-22** asserts legality, terminal absorption and optimistic-concurrency behaviour (two approvals of the same change-set: exactly one wins, mirroring the Phase 0 Lua-CAS pattern for MCP Tasks).

### 3.7 State diagram — pairing code and device token lifecycle (§1.1)

```mermaid
stateDiagram-v2
    state "pairing code" as PC {
        [*] --> issued : POST /pairing-codes
        issued --> consumed : exchange succeeds (single-use, code deleted)
        issued --> burned : 5 failed attempts
        issued --> expired : 5 minutes elapse
        burned --> [*]
        expired --> [*]
    }
    state "device" as DEV {
        [*] --> pending : row created with the code
        pending --> active : exchange issues token + cert
        pending --> abandoned : code expired or burned
        active --> cert_renewing : renew_after reached (< 24h cert)
        cert_renewing --> active : new cert issued over the live session
        active --> policy_stale : reported bundle digest ≠ current
        policy_stale --> active : agent reloads the bundle
        active --> revoked : admin revokes, or cert chain fails
        policy_stale --> revoked
        cert_renewing --> revoked
        revoked --> [*]
        abandoned --> [*]
    }
```

`policy_stale` is a **mutation-blocking** state, not a warning: while the agent's bundle digest disagrees with the backend's, the chokepoint refuses to mint authority (Q-07). That is the fail-closed side of double policy evaluation.

### 3.8 State diagram — the bounded validation-feedback loop (§1.5)

```mermaid
stateDiagram-v2
    [*] --> generating : attempts_remaining = 3
    generating --> validating : artifact set produced
    validating --> judging : zero blocking findings
    validating --> generating : blocking findings AND attempts_remaining > 0<br/>(decrement is the ONLY transition that writes attempts_remaining)
    validating --> template_fallback : blocking findings AND attempts_remaining == 0
    judging --> accepted : rubric recorded (advisory; cannot block)
    template_fallback --> accepted : verified template substituted
    generating --> unavailable : router returns EXHAUSTED (no endpoint reachable)
    unavailable --> template_fallback
    accepted --> [*]
```

Termination is structural, not conventional: `attempts_remaining` is a field of a frozen state object, the only function that produces a new state decrements it, and the type of that function's result is a closed union of `Continue | Accepted | TemplateFallback` with `Continue` unreachable at zero. **Q-08** proves at most three generation calls for every failure sequence, and its negative control is a `_next` that forgets to decrement.


---

## 4. Cross-Cutting Decisions

These are contracts later phases inherit. Where a row extends a Phase 0 contract, the Phase 0 reference is given.

### 4.1 Summary table

| Concern | Phase 1 decision | Authority | Phase 0 relation |
|:---|:---|:---|:---|
| API versioning | `/api/v1/` unchanged | Tech-Stack, PRD §5 | as-is |
| Error contract | RFC 9457 on every non-2xx; ~26 new type suffixes under `https://errors.forgeops.dev` | PRD §5, Appendix C | extend |
| Agent ↔ backend protocol | JSON-RPC 2.0 over WSS, outbound-only, Phase 0's fixed envelope, nine message types | phases.md 1.1, Research §0 | extend |
| Command integrity | Canonical JSON (RFC 8785 JCS) + domain separation + HMAC-SHA256 per device key; nonce + monotonic `seq` + `not_after` | phases.md 1.1 | new |
| Streaming to browser | SSE via FastAPI native `EventSourceResponse`, exactly the six existing event types | Research §0 | as-is |
| Auth posture | **Deny by default.** Every route requires a verified principal unless in the enumerated public set (§4.4) | phases.md 1.11, Phase 0 §14.2 warning | replaces Phase 0's "unauthenticated and local-only" posture |
| Authorization | Three roles (admin, developer, viewer) from Authentik groups → Cerbos v0.54.0 sidecar for resource-scoped decisions | Tech-Stack §9, PRD §5 | new |
| Policy placement | OPA server for gateway **and** governance; embedded Rego in the agent for the agent half; Cerbos for app RBAC only; Kyverno still Phase 2+ | Research §B7, Phase 0 §5.4 | extend |
| Task orchestration | **ARQ** behind the unchanged `TaskDispatcher` Protocol; SDK import confined to `core/tasks.py` by the existing banned-api rule | Research §0/§B6, Tech-Stack §4 | extend (D-32) |
| Mutation path | Exactly one: `governance.chokepoint` → signed envelope → agent verifier → `executor/internal/mutate` | phases.md 1.10 | new |
| Audit | Append-only `audit_events`, DB-enforced (revoked DML + trigger), hash-chained, one record per governance transit | phases.md 1.9, NFR-14 | new |
| Multi-tenancy | `TenantContextMiddleware` fills middleware row 6; `SET LOCAL app.tenant_id` per transaction; RLS and `NOT NULL` deferred to Phase 2 | Research §0, Phase 0 §6.5 | extend (D-35) |
| Vector storage | `embeddings` stays `vector(1536)` for Voyage Code 3; a separate `embeddings_local` table carries 1024-d BGE-M3; never mixed in one query | Research §C10, D-2's deferred follow-up | extend (D-48) |
| Sparse retrieval | Redis Stack `FT.SEARCH` with the BM25 scorer over the same chunks; fused with dense results by Reciprocal Rank Fusion | Research §C10 "BM25 keyword indexing" | new (D-49) |
| Coverage | **Gate** at ≥70 % per component (backend, agent, frontend) — not an aggregate | phases.md 1 criterion 11, OQ-17 | resolves OQ-17 (D-31) |
| Dependency pinning | Exact versions everywhere; Python locks hash-pinned; images digest-pinned; Actions SHA-pinned; **no `@latest` anywhere**, including `govulncheck` | phases.md risk row, §0.5 debt D4 | extend |
| Test integrity | The five §0.4 clauses apply to every component | D-23, `REVIEW-PHASE-0.md` Pass 8 | new |

### 4.2 RFC 9457 registry extension

Phase 0 fixed the shape; Phase 1 adds types. Rules that do not change: `type` is a stable registry URI never resolved at runtime; `status` always equals the HTTP status (P-09); `detail` never carries secrets, tokens, connection strings or tracebacks (D-27). The full table of new suffixes with statuses and `detail` guidance is Appendix C.1.

One new rule specific to this phase, because Phase 1 is the first with an authorization model: **a 403 never discloses resource existence.** A viewer asking about a project they cannot see gets the same problem body whether or not the project exists. Enumeration through error shape is a real leak and cheap to prevent now.

### 4.3 Middleware stack — row 6 is filled

| # | Middleware | Phase 1 status |
|:-:|:---|:---|
| 1 | Starlette `ServerErrorMiddleware` | unchanged |
| 2 | `RequestIdMiddleware` | unchanged |
| 3 | `TraceContextMiddleware` | unchanged |
| 4 | `AccessLogMiddleware` | unchanged |
| 5 | `CORSMiddleware` | unchanged |
| 6 | **`TenantContextMiddleware`** | **filled (D-35).** Resolves tenant from the verified principal into a `contextvar`; the session dependency issues `SET LOCAL app.tenant_id` inside the transaction. No RLS policies yet |
| 7 | Router / endpoint dependencies | **`require_principal` attaches per-route**, per Phase 0 §4.3 row 7 — never globally |

Why per-route rather than a global middleware, restated because it is easy to "improve" wrongly later: a global auth middleware has to carry a path allowlist, and path allowlists drift from the router. A per-route dependency cannot drift, because `scripts/check-route-auth.py` enumerates `create_app().routes` and fails if any route lacks the dependency and is not in the public set (§4.4). The check is derived from the app, not from a list.

`SET LOCAL` and never a session-level `SET`: PgBouncer transaction-mode pooling recycles connections between statements, so a session variable is not reliably the same connection's variable. asyncpg behind PgBouncer additionally requires `statement_cache_size=0`; Phase 0's engine factory already carries that comment and Phase 1 makes it a configurable that defaults on when `DATABASE_POOLER_MODE=transaction` (§13.1).

### 4.4 The public route set (exhaustive)

Everything not on this list requires a verified principal. The list is committed in code as `auth.PUBLIC_ROUTES` and asserted against the router.

| Route | Method | Why public |
|:---|:---|:---|
| `/health` | GET | Container liveness contract (Phase 0 §4.4) |
| `/health/ready` | GET | Orchestrator readiness contract |
| `/api/v1/health` | GET | Versioned informational echo |
| `/api/v1/openapi.json`, `/api/v1/docs` | GET | Schema/documentation; contains no data |
| `/api/v1/auth/login`, `/api/v1/auth/callback`, `/api/v1/auth/refresh`, `/api/v1/auth/logout` | GET/POST | The flow that *creates* a principal cannot require one |
| `/api/v1/agents/pair/exchange` | POST | The agent has no credential yet; this is the exchange that gives it one. Protected instead by single-use codes, a 5-attempt cap, per-IP and global rate limits, and 5-minute expiry (§10.3) |

`/api/v1/mcp*` and `/api/v1/ai/complete` keep their Phase 0 OIDC verification and additionally now resolve a principal, so the MCP surface gains RBAC without changing its token contract.

### 4.5 SSE producer discipline

Phase 1 is the first phase with real streams. Producers are `generation` (token + validation + progress), `analysis` (progress during indexing) and `governance` (status + complete on apply). All three use exactly `STATUS`, `TOKEN`, `PROGRESS`, `VALIDATION`, `COMPLETE`, `ERROR` from `core/sse.py`. **Q-26** asserts stream well-formedness: only those six names appear, and exactly one terminal event (`COMPLETE` or `ERROR`) is emitted per stream. Adding a seventh type would require its own numbered decision; none is proposed, and the two temptations — a `heartbeat` event and a `diff` event — are met by `PROGRESS` and by fetching the change-set over REST respectively.

SSE and authentication interact awkwardly and the resolution is worth recording: the browser's native `EventSource` cannot send an `Authorization` header. Rather than putting a token in a query string (which lands in access logs) or adding a dependency, the frontend uses a small typed reader over `fetch` + `ReadableStream` in `lib/api/sse.ts` (§12.4). No new package.

### 4.6 Job orchestration — ARQ, and why

`phases.md` and Research §0 allow ARQ or Dramatiq. Phase 1 chooses **ARQ** (D-32).

| Criterion | ARQ | Dramatiq |
|:---|:---|:---|
| asyncio fit with FastAPI | Native: tasks are coroutines, and the same `httpx`/`redis`/SQLAlchemy async clients are reusable inside a job | Worker model is thread/process-based; async support exists but the job body runs under a bridge, so async DB sessions need care |
| Broker | Redis only — already in the topology, no new service | Redis or RabbitMQ; RabbitMQ is the better-supported path and would add a service |
| Operational surface | One worker process, `arq.worker` cron for scheduled work | Broker + optional results backend + middleware stack |
| Honest cost | Smaller community; fewer built-in middlewares; no admin UI | Larger community; richer middleware; more moving parts |

The deciding argument is that Phase 1's queued work is *async I/O bound* — indexing batches, embedding calls, rerank calls, generation runs — so an asyncio-native runner lets the job body reuse the exact clients the request path uses. Dramatiq would mean a second concurrency model inside one codebase.

What does **not** change, and is what OQ-16 depends on: the `TaskDispatcher` Protocol is untouched.

```python
# backend/src/core/tasks.py — Phase 0 signature, unchanged
class TaskDispatcher(Protocol):
    async def enqueue(self, name: str, payload: dict[str, Any], *,
                      idempotency_key: str | None = None) -> TaskHandle: ...
```

No engine concept enters it: no workflow id, no signal, no query, no activity, no run id. `TaskHandle.dispatcher` becomes `"arq"`. The banned-api rule already forbids importing `arq` outside `core/tasks.py`, so business logic remains orchestrator-agnostic and the Phase 2 durable-engine move stays a one-module change. **Temporal and Inngest both remain open (OQ-16); nothing in Phase 1 assumes either**, and in particular no Phase 1 payload is shaped as a workflow-with-history.

### 4.7 Consequences of D-29 (wazero) that belong at the cross-cutting level

The Wasm decision is not just a scanner detail; it touches three cross-cutting contracts and they are stated here so §8, §10 and §16 stay consistent.

1. **`CGO_ENABLED=0` holds for all six targets, unchanged.** The GoReleaser configuration in `agent/.goreleaser.yaml` is not modified. Criterion 7's evidence (six real published binaries, already proven for `v0.0.1-rc3`) remains valid without re-litigation.
2. **The build gains a non-Go supply-chain input.** Grammar `.wasm` blobs are vendored under `agent/internal/scanner/grammars/`, digest-pinned in `grammars.lock.json`, embedded with `go:embed`, verified by SHA-256 **at load time** as well as in CI, and injected into the CycloneDX SBOM by `scripts/sbom-merge.py` because Syft cannot see them (§8.6).
3. **D-1's guard changes form while keeping its substance.** `agent/internal/app/deps_test.go` stops asserting "tree-sitter is absent from `go.mod`" — that assertion would now be actively misleading, since the *capability* is present. It becomes two assertions: (a) no dependency in the module graph requires cgo, checked by building with `CGO_ENABLED=0` and by a denylist of known-cgo modules; (b) every entry in `grammars.lock.json` matches the embedded bytes. `scripts/check-go-module.sh` is updated in the same commit so the two guards cannot disagree.

---

## 5. Component Decomposition and Responsibilities

### 5.1 Tier 3 — Local Agent additions

**Purpose in Phase 1:** become a trustworthy remote executor. The agent gains an identity, a session, a policy brain, a parser, validators and exactly one write path.

| Package | Responsibility | Explicitly **not** responsible for |
|:---|:---|:---|
| `internal/session` | Pairing exchange; mTLS + JWT handshake; the nine JSON-RPC message types; heartbeat 30 s / timeout 90 s; reconnect with exponential backoff 1 s → 60 s, jitter 0.5×; envelope verification; replay rejection; routing verified commands to the executor | Deciding *whether* an operation is allowed (that is `policy` + `executor`); performing mutations |
| `internal/identity` | One `Provider` seam with two implementations: `PairedDevice` (client cert issued at pairing, ≤24 h, auto-renewed over the live session) and `SpiffeWorkload` (X.509-SVID via the SPIFFE Workload API, cluster only) | Storing long-lived keys. Nothing persists a non-expiring credential |
| `internal/executor` | Named-operation whitelist and dispatch; per-operation timeout; progress emission. **Never** interprets a shell string | Signature verification (done in `session`); policy (done in `policy`) |
| `internal/executor/internal/mutate` | `ApplyVerified` — the only code in the binary that writes a project file. Backup-before-mutate, temp+fsync+rename, full rollback, backup manifest | Being importable from anywhere outside the executor subtree (compiler-enforced) |
| `internal/policy` | Embedded Rego evaluation of the governance bundle; bundle digest tracking; fail-closed on stale or missing bundle | Being the only evaluation (the backend evaluates first and independently) |
| `internal/validator` | `docker compose` config validation; K8s server-side dry-run; `tofu validate` + `plan`; YAML syntax + JSON Schema; Helm lint + template; Trivy config scan | Applying anything. Every validator is read-only by construction |
| `internal/scanner` | Tiered language detection; recursive scan honouring ignore files and size/type filters; tree-sitter parsing via wazero; cAST chunking; metadata enrichment; dependency-graph construction; watch mode with debounce; incremental closure computation | Embedding (backend), storing (backend), deciding readiness (backend) |
| `internal/secretscan` | Gitleaks-based detection over every chunk; redaction producing `RedactedChunk`; finding metadata without values | Storing secrets; injecting secrets |
| `internal/devtools` | Discover and version-report optional external tools (`tofu`, `trivy`); feed `agent doctor` | Installing anything |

The agent's Phase 0 surface (`iac`, `git`, `fileops.UnifiedDiff`, `mcp`, `docker`, `k8s`, `telemetry`, `logging`, `config`, `app`) is unchanged except where §1.4 says `extend`.

### 5.2 Tier 2 — Backend additions

| Module | Responsibility | Route prefix |
|:---|:---|:---|
| `auth` | OIDC authorization-code + PKCE login; JWKS verification (extends Phase 0's verifier); session and refresh lifecycle; device-token issuance and revocation; principal resolution; Cerbos client; `require_principal` dependency; `PUBLIC_ROUTES` | `/api/v1/auth/*`, `/api/v1/agents/*` |
| `projects` | Project CRUD, GitHub import, local-path registration, per-project settings including LLM budget and embedding backend, tags, favourites, activity feed | `/api/v1/projects/*` |
| `analysis` | Codebase Index API (file tree, contents, chunks, dependency edges, symbols); incremental index patching with optimistic concurrency; embedding orchestration; readiness scoring engine and report generation | `/api/v1/projects/{id}/index`, `/readiness` |
| `generation` | Hybrid retrieval + rerank; prompt assembly chokepoint; structured generation via the router; the bounded feedback loop; the LLM-as-judge rubric; the Safe Default Template Library; SSE streaming | `/api/v1/generation/*` |
| `governance` | **The chokepoint.** Admission, policy evaluation, approval gate, change-set compilation, blast-radius check, audit write, rollback handle, authority mint, envelope signing | `/api/v1/change-sets/*` |
| `policies` | Policy CRUD; policy templates; OPA evaluation client; bundle build, digest and publication to agents; evaluation records | `/api/v1/policies/*` |
| `secrets` | Infisical-backed CRUD per project and environment; the redaction chokepoint; deploy-time injection material | `/api/v1/secrets/*` |
| `audit` | Append-only writer with hash chaining; query API with filtering and cursor pagination | `/api/v1/audit/*` |
| `websocket` | The JSON-RPC hub: connection registry, per-message revocation check, `seq` high-water mark, correlation of `command.execute` → `command.result`, progress fan-out to SSE | `/api/v1/ws/agent` |

The modular-monolith rule is unchanged and still enforced by the Ruff banned-api cross-domain rule: a domain may import `core/` and itself, nothing else. Phase 1 adds one deliberate exception pattern rather than weakening the rule — `governance` is allowed to import `policies`, `audit` and `websocket`, because being the composition point of those three *is* its job. The exception is written explicitly in `pyproject.toml` rather than left to interpretation, and it is one-directional: none of the three may import `governance`.

### 5.3 Tier 1 — Frontend surfaces

Seven feature areas, each mapping to one deliverable: projects (§1.2), readiness (§1.4), generation (§1.5), approval (§1.6), policies (§1.7), secrets (§1.8), audit (§1.9). §12 gives the route structure and the library justifications. The Phase 0 shell, API client, error normalisation and store boundaries are consumed unchanged.

### 5.4 The Governance Control Plane in one paragraph

`governance` accepts exactly one input type, `MutationRequest`, from exactly one direction (in-process callers holding a verified principal). It emits either a problem response or a signed command envelope. It is the only module that can construct `MutationAuthority`, the only module that holds the envelope signing key, and the only module permitted to call `websocket.hub.send_command`. Every stage it runs is an existing, tested Phase 0 component wherever one exists — `OpaGatewayPolicy`'s sibling for governance policy, the `ApprovalGate` Protocol, `SemanticPlanAnalyzer` — so §1.10 is mostly *composition under enforcement* rather than new algorithms. That is deliberate: the trust moat should be made of parts that were already proven, arranged so they cannot be skipped.

### 5.5 Policy engine placement (Phase 0 §5.4, updated)

| Where | Engine | Phase | Phase 1 status |
|:---|:---|:---|:---|
| MCP Gateway tool filtering | OPA server | 0 | unchanged; blast-radius input now identity-derived (D-39) |
| Governance policy evaluation (backend half) | **OPA server**, same sidecar, `policies/agent/*.rego` | **1** | new |
| Agent-side double evaluation | **Embedded Rego in the Go binary** | **1** | new (D-30 explains the Wasm-vs-in-process wording) |
| Backend application RBAC | **Cerbos v0.54.0 sidecar** | **1** | new |
| Kubernetes admission | Kyverno | 2+ | still out of scope |

Cerbos remains a sidecar and is **not** embedded in the agent (Research §B7). The agent side is Rego, full stop.

---

## 6. Data Model

### 6.1 PRD §7 table groups — Phase 1 disposition

| Group | Tables | Phase 1 |
|:---|:---|:---|
| **D1** Users, Teams & Projects | `users`, `teams`, `team_members`, `projects`, `project_tags`, `sessions`, `agent_devices` | **`users`, `sessions`, `agent_devices`, `project_tags` added; `projects` extended.** `teams` / `team_members` **deferred to Phase 2** (D-40) — FR-07 multi-user sharing is P2 and no Phase 1 deliverable reads them |
| **D2** Codebase Index | `file_tree`, `file_contents`, `embeddings`, `analysis_reports` | **all four**, plus `[+] file_dependencies` and `[+] embeddings_local`, plus additive columns on `embeddings` |
| **D3** Change-sets & Approvals | `change_sets`, `change_items`, `validations`, `approvals` | **all four**, plus `[+] rollback_handles` |
| **D4** Deployments & Environments | all | **deferred to Phase 2** — deployment automation is excluded verbatim |
| **D5** Secret Vault | `secrets` | **added**, with the `environment_id` reference resolved (§6.6) |
| **D6** AI Learning History | `feedback_events`, `skill_files` | deferred to Phase 3 (learning history excluded verbatim) |
| **D7** Policies | `policies`, `policy_evaluations` | **both**, plus `[+] policy_bundles` |
| **D8** Incidents & Telemetry | all | deferred to Phase 3 |
| — | `[+] audit_events` | **added** — §1.9 has no PRD §7 table, so one is defined here |
| — | `[+] generation_runs` | **added** — §1.5's iteration/judge/cache provenance has no PRD table and is needed for NFR-04 evidence |

Every `[+]` table is justified by a `phases.md` Phase 1 deliverable that cannot be built without it, and each is named in §6.3.

### 6.2 Entity-relationship diagram

```mermaid
erDiagram
    users ||--o{ sessions : has
    users ||--o{ approvals : approves
    users ||--o{ audit_events : acts
    projects ||--o{ project_tags : tagged
    projects ||--o{ agent_devices : paired
    projects ||--o{ file_tree : contains
    projects ||--o{ analysis_reports : scored
    projects ||--o{ change_sets : proposes
    projects ||--o{ policies : governs
    projects ||--o{ secrets : stores
    projects ||--o{ generation_runs : runs
    file_tree ||--o| file_contents : body
    file_tree ||--o{ embeddings : chunked
    file_tree ||--o{ embeddings_local : chunked_local
    file_tree ||--o{ file_dependencies : imports
    change_sets ||--o{ change_items : items
    change_sets ||--o{ approvals : decisions
    change_sets ||--o| rollback_handles : reversible_by
    change_items ||--o{ validations : validated
    policies ||--o{ policy_evaluations : evaluated
    policy_bundles ||--o{ agent_devices : pinned_by
    generation_runs ||--o{ change_sets : produced

    users {
        uuid id PK
        uuid tenant_id "nullable seam"
        citext email UK
        text name
        text role "admin|developer|viewer"
        text idp_subject UK "Authentik sub"
        bool is_active
        timestamptz created_at
        timestamptz updated_at
    }
    sessions {
        uuid id PK
        uuid user_id FK
        bytea refresh_token_hmac "never the token"
        text idp_session_id "nullable"
        inet created_ip
        timestamptz expires_at
        timestamptz revoked_at "nullable"
        timestamptz created_at
    }
    agent_devices {
        uuid id PK
        uuid project_id FK
        uuid tenant_id "nullable seam"
        text status "pending|active|policy_stale|revoked|abandoned"
        bytea pairing_token_hmac "nullable, HMAC of the 6-char code"
        bytea device_token_hmac "nullable until exchange"
        bytea envelope_key_enc "AES-256-GCM, app-level key"
        text cert_serial "nullable"
        text cert_fingerprint "nullable"
        text agent_version
        text platform
        text policy_bundle_digest "nullable"
        bigint last_seq "replay high-water mark mirror"
        timestamptz pairing_expires_at "nullable"
        timestamptz cert_not_after "nullable"
        timestamptz last_seen "nullable"
        timestamptz revoked_at "nullable"
        timestamptz created_at
    }
    file_contents {
        uuid file_id PK_FK
        text content "redacted text only"
        text language "nullable"
        text summary "nullable, module-level ~1024 tokens"
        int redaction_count
        timestamptz updated_at
    }
    file_dependencies {
        uuid id PK
        uuid project_id FK
        uuid from_file_id FK
        uuid to_file_id FK "nullable when unresolved"
        text raw_specifier
        text kind "import|require|include|use"
        bool resolved
        timestamptz created_at
    }
    analysis_reports {
        uuid id PK
        uuid project_id FK
        int score "0..100"
        jsonb categories
        text inventory_hash "determinism evidence"
        int report_version
        timestamptz created_at
    }
    change_sets {
        uuid id PK
        uuid project_id FK
        uuid tenant_id "nullable seam"
        text status
        uuid created_by FK "nullable for system origin"
        text origin "generation|manual|policy"
        uuid generation_run_id FK "nullable"
        int blast_radius_score
        text blast_radius_verdict
        text policy_bundle_digest
        int version "optimistic concurrency"
        timestamptz applied_at "nullable"
        timestamptz created_at
    }
    change_items {
        uuid id PK
        uuid change_set_id FK
        text file_path
        text action "create|update|delete"
        text old_content "nullable"
        text new_content "nullable for delete"
        text old_hash "nullable, sha256"
        text new_hash "nullable, sha256"
        int ordinal
    }
    validations {
        uuid id PK
        uuid change_item_id FK
        text validator
        bool passed
        bool blocking
        text output "redacted"
        int iteration
        timestamptz created_at
    }
    approvals {
        uuid id PK
        uuid change_set_id FK
        uuid approver_id FK
        text status "approved|rejected"
        text comment "nullable"
        timestamptz created_at
    }
    rollback_handles {
        uuid id PK
        uuid change_set_id FK UK
        jsonb backup_manifest
        text agent_device_id
        bool consumed
        timestamptz expires_at
        timestamptz created_at
    }
    secrets {
        uuid id PK
        uuid project_id FK
        text environment "Phase 1 enum, see §6.6"
        text key
        text infisical_path
        bytea encrypted_value "nullable, see §6.6"
        timestamptz rotation_date "nullable"
        timestamptz created_at
        timestamptz updated_at
    }
    policies {
        uuid id PK
        uuid project_id FK "nullable = global"
        text name
        text engine "rego"
        text rego_rules
        bool enabled
        text template_id "nullable"
        timestamptz created_at
        timestamptz updated_at
    }
    policy_evaluations {
        uuid id PK
        uuid policy_id FK "nullable when bundle-level"
        uuid change_set_id FK "nullable"
        text operation
        text result "allow|deny|require_approval"
        text reason
        text side "backend|agent"
        timestamptz created_at
    }
    policy_bundles {
        uuid id PK
        text digest UK "sha256 of the canonical bundle"
        bytea bundle "gzip tar of rego + data"
        uuid project_id FK "nullable = global"
        bool active
        timestamptz created_at
    }
    generation_runs {
        uuid id PK
        uuid project_id FK
        uuid requested_by FK
        text status "running|accepted|template_fallback|unavailable|failed"
        int iterations_used
        text served_from "l1|l2|provider|template"
        text tier
        text endpoint_id "nullable"
        jsonb rubric "nullable, advisory"
        jsonb retrieval "chunk ids, rerank scores"
        int prompt_tokens
        int completion_tokens
        timestamptz created_at
        timestamptz finished_at "nullable"
    }
    audit_events {
        bigint seq PK "BIGSERIAL, chain order"
        uuid id UK
        uuid tenant_id "nullable seam"
        uuid project_id "nullable"
        uuid actor_user_id "nullable"
        uuid actor_device_id "nullable"
        text actor_kind "user|agent|system"
        text action
        text resource_kind
        text resource_id "nullable"
        text reason "the why"
        jsonb before_state "nullable"
        jsonb after_state "nullable"
        text outcome "allowed|denied|applied|rolled_back|failed"
        text trace_id "nullable"
        bytea prev_hash
        bytea hash
        timestamptz created_at
    }
```

`embeddings` and `embeddings_local` are omitted from the attribute listing above only to keep the diagram legible; both are defined precisely in §6.3.

### 6.3 SQLModel definitions

Only the load-bearing definitions are given in full. All of them follow Phase 0's conventions: UUID primary keys with `default_factory`, `ondelete` stated on every FK, `tenant_id` nullable as the D-2/D-35 seam, timezone-aware timestamps with server defaults, and the `MetaData(naming_convention=…)` from `core/db.py` so Alembic never emits a database-generated constraint name.

```python
# backend/src/auth/models.py
class UserRole(StrEnum):
    ADMIN = "admin"; DEVELOPER = "developer"; VIEWER = "viewer"

class User(SQLModel, table=True):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("idp_subject", name="uq_users_idp_subject"),)

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    tenant_id: uuid.UUID | None = Field(default=None, index=True)   # D-35 seam, still nullable
    # CITEXT so 'A@b.com' and 'a@b.com' cannot become two accounts. The extension is
    # created in 0002 before this column exists.
    email: str = Field(sa_column=Column("email", CITEXT, nullable=False, unique=True))
    name: str = Field(max_length=200)
    role: UserRole = Field(sa_column=Column("role", SAEnum(UserRole, name="user_role"), nullable=False))
    # The IdP subject is the join key to Authentik. Email is mutable there; sub is not.
    idp_subject: str = Field(max_length=255)
    is_active: bool = Field(default=True)
    created_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False, server_default=func.now()))
    updated_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False,
                                                  server_default=func.now(), onupdate=func.now()))

class Session(SQLModel, table=True):
    __tablename__ = "sessions"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(foreign_key="users.id", index=True, ondelete="CASCADE")
    # HMAC, never the token, and never a reversible encryption of it: a stolen
    # database must not yield usable refresh tokens (NFR-09 in spirit).
    refresh_token_hmac: bytes = Field(sa_column=Column("refresh_token_hmac", LargeBinary(32), nullable=False))
    idp_session_id: str | None = Field(default=None, max_length=255)
    expires_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False, index=True))
    revoked_at: datetime | None = Field(sa_column=Column(DateTime(timezone=True), nullable=True))
    created_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False, server_default=func.now()))
```

```python
# backend/src/auth/device_models.py
class DeviceStatus(StrEnum):
    PENDING = "pending"; ACTIVE = "active"; POLICY_STALE = "policy_stale"
    REVOKED = "revoked"; ABANDONED = "abandoned"

class AgentDevice(SQLModel, table=True):
    """PRD D1 `agent_devices`, with the columns the real pairing flow needs.

    PRD §7 lists (id, project_id, pairing_token, device_token, last_seen). Storing
    either token in plaintext would make a database read equivalent to a stolen
    credential, so both are stored as HMACs under a server pepper and the column
    names say so. The envelope key must be recoverable (the backend signs with it),
    so it is encrypted rather than hashed — AES-256-GCM under an app-level key from
    the secret store, never a column default.
    """
    __tablename__ = "agent_devices"
    __table_args__ = (
        UniqueConstraint("cert_serial", name="uq_agent_devices_cert_serial"),
        Index("ix_agent_devices_project_status", "project_id", "status"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    project_id: uuid.UUID = Field(foreign_key="projects.id", index=True, ondelete="CASCADE")
    tenant_id: uuid.UUID | None = Field(default=None, index=True)
    status: DeviceStatus = Field(sa_column=Column("status", SAEnum(DeviceStatus, name="device_status"),
                                                  nullable=False))
    pairing_token_hmac: bytes | None = Field(sa_column=Column("pairing_token_hmac", LargeBinary(32), nullable=True))
    device_token_hmac: bytes | None = Field(sa_column=Column("device_token_hmac", LargeBinary(32), nullable=True))
    envelope_key_enc: bytes | None = Field(sa_column=Column("envelope_key_enc", LargeBinary, nullable=True))
    cert_serial: str | None = Field(default=None, max_length=64)
    cert_fingerprint: str | None = Field(default=None, max_length=95)   # sha256 colon-hex
    agent_version: str = Field(max_length=64)
    platform: str = Field(max_length=64)
    policy_bundle_digest: str | None = Field(default=None, max_length=71)  # "sha256:" + 64
    # Mirror of the Redis high-water mark, for forensics after a Redis flush. Redis
    # remains authoritative for replay rejection (§7.6).
    last_seq: int = Field(default=0, sa_column=Column("last_seq", BigInteger, nullable=False, server_default=text("0")))
    pairing_expires_at: datetime | None = Field(sa_column=Column(DateTime(timezone=True), nullable=True))
    cert_not_after: datetime | None = Field(sa_column=Column(DateTime(timezone=True), nullable=True))
    last_seen: datetime | None = Field(sa_column=Column(DateTime(timezone=True), nullable=True))
    revoked_at: datetime | None = Field(sa_column=Column(DateTime(timezone=True), nullable=True))
    created_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False, server_default=func.now()))
```

```python
# backend/src/analysis/models.py  (additions; the Phase 0 classes are unchanged)
EMBEDDING_DIMS = 1536          # D-2, unchanged
EMBEDDING_DIMS_LOCAL = 1024    # D-48: BGE-M3 self-hosted, its own table

class FileContent(SQLModel, table=True):
    """PRD D2 `file_contents`. Holds REDACTED text only.

    The store never contains an unredacted secret, which is what makes Q-13's
    cache clause enforceable: there is no unredacted source for a cache key to be
    computed from. Finding metadata (kind, path, line) lives in the scan report,
    never the value.
    """
    __tablename__ = "file_contents"

    file_id: uuid.UUID = Field(foreign_key="file_tree.id", primary_key=True, ondelete="CASCADE")
    content: str
    language: str | None = Field(default=None, max_length=64)
    summary: str | None = None                       # module-level, ~1024 tokens (Research §C10)
    redaction_count: int = Field(default=0)
    updated_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False,
                                                  server_default=func.now(), onupdate=func.now()))

class FileDependency(SQLModel, table=True):
    """[+] Not in PRD §7, required by phases.md §1.3's dependency-graph builder and
    by the incremental-rescan closure (Q-10). Unresolved specifiers are KEPT with
    resolved=False so a later scan can resolve them without re-parsing the importer.
    """
    __tablename__ = "file_dependencies"
    __table_args__ = (
        UniqueConstraint("from_file_id", "raw_specifier", name="uq_file_deps_from_specifier"),
        Index("ix_file_deps_to_file", "to_file_id"),          # the reverse lookup the closure needs
        Index("ix_file_deps_project", "project_id"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    project_id: uuid.UUID = Field(foreign_key="projects.id", index=True, ondelete="CASCADE")
    from_file_id: uuid.UUID = Field(foreign_key="file_tree.id", index=True, ondelete="CASCADE")
    to_file_id: uuid.UUID | None = Field(default=None, foreign_key="file_tree.id", ondelete="SET NULL")
    raw_specifier: str = Field(max_length=1024)
    kind: str = Field(max_length=16)
    resolved: bool = Field(default=False)
    created_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False, server_default=func.now()))

class EmbeddingLocal(SQLModel, table=True):
    """D-48 — the multi-model strategy D-2 deferred to Phase 1, now decided.

    A pgvector column has one fixed dimension. Voyage Code 3 is 1536-d and BGE-M3 is
    1024-d, so a single column cannot hold both without padding or truncation, and
    BGE-M3 is not Matryoshka-trained, so truncation is not available. Phase 1 therefore
    uses a SECOND TABLE, and a project reads exactly one of them, chosen by
    projects.settings.embedding_backend. Cross-table mixing is impossible because no
    query references both.
    """
    __tablename__ = "embeddings_local"
    __table_args__ = (UniqueConstraint("file_id", "chunk_index", name="uq_embeddings_local_file_chunk"),)

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    file_id: uuid.UUID = Field(foreign_key="file_tree.id", index=True, ondelete="CASCADE")
    tenant_id: uuid.UUID | None = Field(default=None, index=True)
    chunk_index: int
    chunk_text: str
    model_id: str = Field(max_length=100)            # NOT NULL provenance, per D-2
    embedding: list[float] = Field(sa_column=Column("embedding", Vector(EMBEDDING_DIMS_LOCAL), nullable=False))
    created_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False, server_default=func.now()))
```

Additive columns on the existing `embeddings` table (revision `0003`), carrying the cAST metadata Research §C10 requires. All are nullable so the migration needs no backfill:

```python
    symbol: str | None = Field(default=None, max_length=512)        # function/class name
    parent_symbol: str | None = Field(default=None, max_length=512) # class hierarchy
    signature: str | None = None                                    # function signature
    kind: str | None = Field(default=None, max_length=32)           # function|class|module|block
    start_line: int | None = None
    end_line: int | None = None
    token_count: int | None = None
    chunk_metadata: dict | None = Field(default=None, sa_column=Column("chunk_metadata", JSONB, nullable=True))
```

```python
# backend/src/audit/models.py
class AuditEvent(SQLModel, table=True):
    """§1.9. Append-only and tamper-evident, enforced by the DATABASE, not the app.

    Three mechanisms, because any one alone is insufficient:
      1. `seq BIGSERIAL` gives a total order per database, so a deletion leaves a gap.
      2. `hash = sha256(canonical(payload) || prev_hash)` chains the records, so
         editing an old row invalidates every later hash — detectable without a
         second copy.
      3. UPDATE and DELETE are REVOKED from the application role and additionally
         raise in a trigger, so neither an ORM bug nor a stray SQL statement can
         rewrite history. Migrations run as a different role (§6.7).
    """
    __tablename__ = "audit_events"
    __table_args__ = (
        UniqueConstraint("id", name="uq_audit_events_id"),
        Index("ix_audit_project_created", "project_id", "created_at"),
        Index("ix_audit_actor_created", "actor_user_id", "created_at"),
        Index("ix_audit_resource", "resource_kind", "resource_id"),
    )

    seq: int | None = Field(default=None, sa_column=Column("seq", BigInteger, primary_key=True,
                                                           autoincrement=True))
    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    tenant_id: uuid.UUID | None = Field(default=None, index=True)
    project_id: uuid.UUID | None = Field(default=None, index=True)   # deliberately NOT a FK: an audit
    actor_user_id: uuid.UUID | None = Field(default=None)            # record must survive the deletion
    actor_device_id: uuid.UUID | None = Field(default=None)          # of what it describes
    actor_kind: str = Field(max_length=16)
    action: str = Field(max_length=64)
    resource_kind: str = Field(max_length=64)
    resource_id: str | None = Field(default=None, max_length=128)
    reason: str = Field(max_length=1024)                             # the "why" NFR-14 requires
    before_state: dict | None = Field(default=None, sa_column=Column("before_state", JSONB, nullable=True))
    after_state: dict | None = Field(default=None, sa_column=Column("after_state", JSONB, nullable=True))
    outcome: str = Field(max_length=32)
    trace_id: str | None = Field(default=None, max_length=32)
    prev_hash: bytes = Field(sa_column=Column("prev_hash", LargeBinary(32), nullable=False))
    hash: bytes = Field(sa_column=Column("hash", LargeBinary(32), nullable=False))
    created_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False,
                                                  server_default=func.now()))
```

The FK omission on `project_id` / `actor_user_id` is intentional and is the kind of thing a reviewer should be able to find a reason for: an immutable log that cascades away when a project is deleted is not an immutable log. Referential integrity is traded for durability, deliberately, and the columns are still indexed for the viewer.

### 6.4 Raw DDL that SQLModel cannot express

```sql
-- 0002: case-insensitive email
CREATE EXTENSION IF NOT EXISTS citext;

-- 0003: HNSW for the self-hosted 1024-d table. Same operator class and build
-- parameters as Phase 0's 1536-d index so recall/latency behaviour is comparable.
-- IVFFlat remains rejected for production vector search (Research §0, §A0a).
CREATE INDEX ix_embeddings_local_embedding_hnsw
    ON embeddings_local USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- 0003: trigram index for the file-path search the project detail page needs
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE INDEX ix_file_tree_path_trgm ON file_tree USING gin (path gin_trgm_ops);

-- 0007: audit immutability — mechanism 3
CREATE OR REPLACE FUNCTION audit_events_immutable() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'audit_events is append-only (design §6.3, Q-05): % attempted', TG_OP
        USING ERRCODE = '42501';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_audit_events_no_update
    BEFORE UPDATE ON audit_events FOR EACH ROW EXECUTE FUNCTION audit_events_immutable();
CREATE TRIGGER trg_audit_events_no_delete
    BEFORE DELETE ON audit_events FOR EACH ROW EXECUTE FUNCTION audit_events_immutable();
CREATE TRIGGER trg_audit_events_no_truncate
    BEFORE TRUNCATE ON audit_events EXECUTE FUNCTION audit_events_immutable();

-- 0007: and mechanism 3's belt-and-braces at the privilege level
REVOKE UPDATE, DELETE, TRUNCATE ON audit_events FROM forgeops_app;
GRANT  INSERT, SELECT               ON audit_events TO   forgeops_app;
GRANT  USAGE, SELECT ON SEQUENCE audit_events_seq_seq TO forgeops_app;

-- 0007: chain-head serialisation. The chain is only well-defined if writers
-- serialise, so the writer takes a transaction-scoped advisory lock keyed by
-- tenant (0 for the single-tenant Phase 1 case). Cost: audit writes for one tenant
-- are serial. That is acceptable because every write is a governance transit, and
-- those are already human-paced.
-- (Taken in application code as: SELECT pg_advisory_xact_lock(hashtext('audit'), $tenant_key))
```

The two-role arrangement is stated explicitly because it is easy to lose: `DATABASE_URL` uses `forgeops_app`, which cannot UPDATE or DELETE audit rows; `ALEMBIC_DATABASE_URL` uses `forgeops_migrator`, which owns the schema. Phase 1 creates both roles in `0002` and the Compose Postgres init grants them. A single-role deployment silently defeats mechanism 3, so `scripts/check-db-roles.py` asserts the running app's role lacks UPDATE on `audit_events` — a gated integration test, not a comment.

### 6.5 Alembic revision plan

Linear, no branches, `NNNN_snake_case_summary`, starting at `0002` because `0001_initial` is the only existing revision. Each revision is proven by a gated integration test in the D-26 pattern (`require_capability("postgres")`, which **fails** rather than skips when `FORGEOPS_REQUIRE_INTEGRATION=1`).

| Revision | Contents | How it is proven |
|:---|:---|:---|
| `0002_identity_and_devices` | `citext` extension; `user_role` / `device_status` enums; `users`, `sessions`, `agent_devices`; `forgeops_app` / `forgeops_migrator` roles | `test_0002_identity.py`: enum values, unique `idp_subject`, `CITEXT` collapses case, FK `ondelete=CASCADE` deletes sessions with the user, and the app role's grants |
| `0003_codebase_index_extensions` | `file_contents`, `file_dependencies`, `analysis_reports`, `embeddings_local` + its HNSW index, additive cAST columns on `embeddings`, `pg_trgm` + path trigram index | `test_0003_index.py`: `embeddings.embedding` is still `vector(1536)`, `embeddings_local.embedding` is `vector(1024)`, both HNSW indexes exist with `vector_cosine_ops` and `m=16, ef_construction=64`, reverse-dependency index present, `with_ef_search` still applies per transaction |
| `0004_change_sets_and_approvals` | `change_sets`, `change_items`, `validations`, `approvals`, `rollback_handles` | `test_0004_change_sets.py`: status check constraint rejects an unknown state, `version` default, unique `(change_set_id, ordinal)`, cascade behaviour |
| `0005_policies_and_bundles` | `policies`, `policy_evaluations`, `policy_bundles` with a unique digest and a partial unique index enforcing one active bundle per scope | `test_0005_policies.py`: two active global bundles violate the partial unique index |
| `0006_secrets` | `secrets` with the Phase 1 `environment` resolution (§6.6) | `test_0006_secrets.py`: unique `(project_id, environment, key)`; no plaintext column is writable when `SECRET_BACKEND=infisical` |
| `0007_audit_append_only` | `audit_events`, the three triggers, the REVOKE/GRANT pair | `test_0007_audit.py`: INSERT succeeds; UPDATE raises `42501`; DELETE raises; TRUNCATE raises; the app role has no UPDATE privilege; a tampered row breaks the chain verification helper |
| `0008_generation_runs` | `generation_runs` | `test_0008_generation_runs.py`: `iterations_used` check constraint `BETWEEN 0 AND 3` — the iteration bound expressed in the schema as well as the type |
| `0009_project_tags_and_settings` | `project_tags`, `projects.settings` key validation for `embedding_backend`, `llm_budget`, `favourite` | `test_0009_projects.py`: tag uniqueness per project; settings validator rejects an unknown embedding backend |
| `0010_change_set_status_vocabulary` | Reconciles `ck_change_sets_status_allowed` with §3.6's thirteen states. **Added by D-63**, beyond this plan's original eight: `0004` generated the constraint from a `CHANGE_SET_STATUSES` tuple that carried three names §3.6 does not define and lacked six it does, so `blocked`, `pending_approval` and `reverted` — three of Appendix A.3's six transit outcomes — were unstorable. Upgrade validates; downgrade restores the narrower list as `NOT VALID` so it can run against rows the wider vocabulary allowed | `test_0010_change_set_statuses.py`: the tuple equals the states parsed out of §3.6's mermaid block in `design.md`; each of the three removed names is rejected by the database; `pg_constraint.convalidated` is true at head; only `applied → reverted` leaves a terminal state |

Two cross-cutting migration tests, both gated the same way: `test_alembic_linearity.py` asserts a single head and that every `down_revision` chain reaches `0001_initial`; `test_alembic_autogenerate_clean.py` runs `alembic upgrade head` then `--autogenerate` and asserts an empty diff, which is what catches a model/migration divergence and a naming-convention slip in one assertion. The `render_item` hook that teaches autogenerate about `Vector` is already in `alembic/env.py` and is extended to the 1024-d column by construction (it renders `Vector(obj.dim)`).

`0008`'s check constraint deserves a sentence: `iterations_used BETWEEN 0 AND 3` means the 3-iteration bound is enforced in the type (§3.8), in the property (Q-08), **and** in the schema. Three independent expressions of one invariant is not redundancy here — it is what makes a regression impossible to ship quietly.

### 6.6 The dangling `environment_id` reference — resolved

PRD D5 defines `secrets (id, project_id, environment_id, key, encrypted_value, rotation_date)`. But `environment_id` would reference D4's `environments` table, and **D4 is Phase 2** (multi-environment management is excluded verbatim from Phase 1). A nullable FK to a non-existent table is not a seam; it is a broken reference.

**Resolution (D-50).** Phase 1's `secrets` table carries `environment TEXT NOT NULL` constrained to a fixed Phase 1 set — `dev`, `test`, `staging`, `prod` — with uniqueness on `(project_id, environment, key)`. No FK is created. When Phase 2 introduces `environments`, its migration adds `environment_id UUID REFERENCES environments(id)`, backfills it by matching the text value to the newly created rows (a deterministic, four-value mapping), then drops the text column. The backfill is trivially correct precisely *because* Phase 1 constrained the text to the same four names Phase 2 will create.

Rejected alternatives, recorded so Phase 2 does not re-open the question: creating a stub `environments` table now would violate §1.3's no-stub rule and put a Phase 2 table under Phase 1's migration numbering; making `environment_id` a free-form nullable UUID would allow unresolvable values into a security-relevant table.

`encrypted_value` is nullable for a related reason. With `SECRET_BACKEND=infisical` (the §1.8 default) the ciphertext lives in Infisical and the row holds only `infisical_path`, so ForgeOps' database is not a second copy of every secret. The column exists for the `SECRET_BACKEND=local` development mode, where values are sealed with AES-256-GCM under a key from the environment (NFR-09). A check constraint enforces the exclusivity: exactly one of `infisical_path` and `encrypted_value` is non-null.

### 6.7 Tenant seam and pooling constraints

Every new table carries `tenant_id UUID NULL` with an index, matching D-2 and OQ-15. Phase 1 does **not** set `NOT NULL` and does **not** create RLS policies (D-35), because a single-tenant deployment cannot exercise a policy and an unexercised security control is worse than an absent one — Phase 0's review is the evidence for that claim. What Phase 1 *does* add is the plumbing RLS will need, so Phase 2 turns policies on rather than retrofitting:

1. `TenantContextMiddleware` (row 6) resolves the tenant from the verified principal into a `contextvar`.
2. `get_session` issues `SET LOCAL app.tenant_id = :tid` inside the transaction when a tenant is present — the same `SET LOCAL` discipline `with_ef_search` established, never a session-level `SET`.
3. `DATABASE_POOLER_MODE=transaction` sets asyncpg's `statement_cache_size=0` and disables prepared-statement reuse, which is what PgBouncer transaction mode requires.

An integration test asserts the variable is visible to `current_setting('app.tenant_id', true)` within the transaction and **absent in the next transaction on the same pooled connection** — the assertion that actually proves `SET LOCAL` was used rather than `SET`.

### 6.8 What is deliberately not modelled

No `environments`, `deployments`, `deployment_logs`, `health_checks` (D4, Phase 2). No `feedback_events`, `skill_files` (D6, Phase 3). No `incidents`, `auto_actions`, `metrics` (D8, Phase 3). No `teams`, `team_members` (D-40, Phase 2). Nothing in Phase 1 reads any of them, and creating them empty would make the migration numbering claim ownership of work this phase does not do.


---

## 7. Cross-Cutting Foundations in Detail

### 7.1 Configuration additions

Both runtimes keep Phase 0's discipline: a flat env-var namespace, validated once at startup, reporting every project-configuration problem together (P-15), with unknown *project* keys rejected and unrelated ambient OS variables ignored. Phase 1 adds fields; it changes no mechanism. The full inventory is §13.1.

Two new validation rules that matter:

```python
# backend/src/core/config.py (additions)
    # §1.11 — the app API audience is distinct from the MCP gateway audience, so a
    # token minted for the gateway cannot be replayed against the product API.
    oidc_app_audience: str
    oidc_issuer: AnyHttpUrl                     # single issuer for login; the MCP allowlist is separate
    oidc_client_id: str
    oidc_client_secret: SecretStr
    cerbos_url: AnyHttpUrl

    # §1.1 — pairing and envelope integrity
    pairing_code_ttl_seconds: int = Field(default=300, ge=60, le=900)
    pairing_code_max_attempts: int = Field(default=5, ge=1, le=10)
    device_cert_ttl_hours: int = Field(default=24, ge=1, le=168)
    envelope_max_age_seconds: int = Field(default=300, ge=30, le=900)
    envelope_pepper: SecretStr                  # HMAC pepper for token/code storage
    internal_ca_cert_pem: SecretStr
    internal_ca_key_pem: SecretStr

    # §1.5 — generation bounds
    generation_max_iterations: Literal[3] = 3   # NOT an int: the bound is not tunable (Q-08)
    retrieval_overfetch_factor: int = Field(default=3, ge=1, le=10)   # Research §C10 "3×"
    rerank_model: str = Field(default="voyage-rerank-2")
    chunk_target_tokens: int = Field(default=512, ge=128, le=2048)
    chunk_overlap_tokens: int = Field(default=128, ge=0, le=512)
    summary_target_tokens: int = Field(default=1024, ge=256, le=4096)

    @field_validator("chunk_overlap_tokens")
    @classmethod
    def _overlap_below_target(cls, v: int, info) -> int:
        if v >= info.data.get("chunk_target_tokens", 512):
            raise ValueError("CHUNK_OVERLAP_TOKENS must be smaller than CHUNK_TARGET_TOKENS")
        return v
```

`generation_max_iterations: Literal[3]` is not pedantry. NFR-04 targets "under 3 iterations average" and `phases.md` fixes the maximum at 3. Typing it as `int` would invite an operator to set 10 and quietly change a safety-relevant bound; typing it as `Literal[3]` means the config refuses to load. If a later phase needs a different bound, that is a decision, not an environment variable.

The Go side extends `internal/config.Config` with the pairing, session, scanner and validator fields and keeps the accumulate-all-errors contract (P-15).

### 7.2 Logging and redaction

Phase 0's `SecretRedactingFilter` plus D-27's traceback coverage is inherited unchanged and extended in two ways Phase 1 makes necessary:

1. **The agent logger becomes the redacting logger everywhere.** `REVIEW-PHASE-0.md` recorded that the agent constructed a non-redacting logger; Phase 1's agent handles real project content and real tokens, so `logging.NewRedacted` is the only constructor `app.New` calls, asserted by a wiring test.
2. **Scanner and validator output is redacted at the source, not at the log boundary.** A validator that echoes a file's contents in a diagnostic would otherwise leak a secret into a log line before the filter sees a recognisable pattern. `secretscan.Redact` runs on validator output before it is logged or transmitted, and **Q-24** quantifies the result: no secret value appears in any log line, any audit record, or any RFC 9457 `detail`.

### 7.3 Protocol — the nine JSON-RPC message types

Phase 0 fixed the envelope (`jsonrpc`, `id`, `method`, `params`; `Result` and `Error` mutually exclusive). Phase 1 fills in the method catalogue named by `phases.md` §1.1. Direction is stated because it determines who may originate what.

| Method | Direction | Params (abridged) | Notes |
|:---|:---|:---|:---|
| `session.connect` | agent → backend | `device_id`, `agent_version`, `platform`, `policy_bundle_digest`, `capabilities[]` | First frame after the mTLS upgrade. Result carries `session_id`, `heartbeat_interval`, `heartbeat_timeout`, `seq_base`, and a bundle if the digest is stale |
| `session.heartbeat` | agent → backend | `seq`, `uptime_seconds`, `queue_depth` | Every 30 s. Missing for 90 s ⇒ the hub drops the session and marks the device offline |
| `command.execute` | **backend → agent** | the command envelope: `operation`, `args`, `approval_id`, `policy_context`, `nonce`, `seq`, `not_after`, `signature` | The only message that can cause a mutation. Signed with the per-device envelope key |
| `command.result` | agent → backend | `command_id`, `status`, `output`, `backup_manifest?`, `hashes?` | Terminal for one command; correlates by the request `id` |
| `command.progress` | agent → backend | `command_id`, `percent`, `message`, `stage` | Fanned out to the SSE `PROGRESS` event |
| `approval.request` | agent → backend | `command_id`, `reason`, `blast_radius`, `intent?` | The agent asking for a human decision it does not have; used when its local evaluation returns `require_approval`, and also to replay a journalled **intent** after an offline period (D-41) — which is why NFR-18 needs no tenth method |
| `approval.response` | backend → agent | `command_id`, `approval_id`, `decision`, `signature` | Signed exactly like `command.execute`; an unsigned approval is refused |
| `agent.error` | either | `code`, `message`, `retryable`, `command_id?` | Codes mirror the RFC 9457 suffixes (Appendix C.2) so one vocabulary covers both transports |
| `agent.status` | agent → backend | `state`, `policy_bundle_digest`, `scan`, `validators[]`, `versions` | Drives device health, policy-drift detection and `agent doctor` parity |

**No tenth method is added, and `command.execute` is the only mutation carrier.** In particular the MCP server hosted by the agent gains no mutating tool (D-47): an MCP `tools/call` cannot become a file write, because the write path requires a `*envelope.Verified` that only `session` can produce.

### 7.4 Reconnection and heartbeat

Exponential backoff with the numbers `phases.md` fixes: base 1 s, cap 60 s, jitter 0.5×, computed as `delay = min(60s, 1s · 2^(n−1))` then multiplied by a uniform factor in `[0.5, 1.5]`. Reconnect resets `n` only after a **successful `session.connect`**, not after a successful TCP connect — otherwise a backend that accepts sockets and immediately rejects the handshake produces a hot loop. The `seq` counter does **not** reset across reconnects (§7.6), and the device certificate is renewed over the live session before `renew_after`, so a reconnect never needs to re-pair.

The journal drain (D-41) is bound to the same gate: it begins only **after** a successful `session.connect` and after the revocation and bundle-digest checks pass, in the order §10.3's `Journal.Drain` fixes. A revoked device wipes its journal instead of draining it, and a stale bundle blocks the intent half of the drain while still allowing non-mutating records through.

### 7.5 SSE producers

Covered as a contract in §4.5; the producer-side detail is in §11.11. One rule stated here because it belongs to the cross-cutting layer: an SSE stream is a *view*, never a source of truth. The frontend reads change-set and generation state from REST and uses SSE only to know when to refetch and to render tokens as they arrive. That is what keeps a dropped stream from producing a wrong UI, and it is why TanStack Query remains the owner of server-derived state (Phase 0 §12.4, unchanged).

### 7.6 Command envelope: canonicalisation, signature, replay

This is the part where backend and agent cannot be allowed to disagree about a single byte.

**Canonical form.** The signed bytes are the **RFC 8785 JSON Canonicalization Scheme (JCS)** serialisation of the envelope with the `signature` member absent — not "the JSON we happened to send". Precisely:

1. Take the envelope object with members `v`, `command_id`, `device_id`, `operation`, `args`, `approval_id`, `policy_context`, `nonce`, `seq`, `not_after`.
2. Remove `signature` if present.
3. Serialise per JCS: UTF-8, no insignificant whitespace, object members sorted by UTF-16 code-unit order, strings with JSON minimal escaping, numbers in JCS's canonical numeric form. `seq` and `not_after` are integers; no floats appear anywhere in an envelope, which sidesteps JCS's hardest corner entirely.
4. Prefix a domain-separation string: `signing_input = "forgeops-envelope-v1" || 0x00 || jcs_bytes`.
5. `signature = base64url(HMAC-SHA256(envelope_key, signing_input))`.

The domain-separation prefix means an envelope signature can never be replayed as a signature over anything else the same key might one day sign (an `approval.response`, for instance, uses `"forgeops-approval-v1"`).

Both sides implement this from the same fixture set: `agent/testdata/envelopes/*.json` holds envelopes with their expected canonical bytes (hex) and expected signature under a **synthetic, self-labelling** test key (`test-only-not-a-real-secret-…`, per `.kiro/steering/secret-safety.md`). The Go test and the Python test read the *same files*, so a divergence fails both suites rather than producing a mystery in production. **Q-14** generates envelopes and asserts cross-runtime byte-identity.

**Replay protection — three independent conditions, all required.**

| Condition | Mechanism | Rejects |
|:---|:---|:---|
| Freshness | `not_after` ≤ now + `ENVELOPE_MAX_AGE_SECONDS` (default 300 s), and `now ≤ not_after` | A captured envelope used later |
| Uniqueness | 128-bit `nonce`; the agent keeps a bounded LRU of recent nonces and the backend keeps `SETNX nonce:<device>:<nonce>` with the max-age TTL | An immediate re-send |
| Ordering | Strictly monotonic per-device `seq`; the backend allocates via a Redis Lua compare-and-set, the agent refuses `seq ≤ last_seq` | Reordering and re-sending an older command after a newer one |

The `seq` high-water mark is Redis-authoritative (mirrored to `agent_devices.last_seq` for forensics), which mirrors exactly how Phase 0 made its TTL cache and Tasks state machine Redis-authoritative with Lua CAS. **Q-15** asserts that replayed, reordered and expired envelopes are all rejected and that a rejection performs no mutation — the same "counter stays at zero" shape as Phase 0's P-05.

**Clock skew** is handled honestly rather than assumed away: `not_after` is set by the backend, and the agent compares against its own clock with a tolerated skew of ±60 s, refusing envelopes outside that window and reporting skew in `agent.status` so `agent doctor` can say "your clock is 4 minutes fast" instead of "signature invalid".

### 7.7 Named-operation whitelist

`phases.md` §1.1 requires named operations and never arbitrary shell. The Phase 1 catalogue is closed and typed on both sides:

| Operation | Mutating | Requires `approval_id` | Notes |
|:---|:---:|:---:|:---|
| `project.register` | no | no | Registers a root and starts the watcher |
| `project.unregister` | no | no | Stops watching; touches no file |
| `scan.full` / `scan.incremental` | no | no | Read-only traversal |
| `validate.compose` / `validate.k8s` / `validate.tofu` / `validate.helm` / `validate.yaml` / `validate.trivy` | no | no | Read-only by construction |
| `readiness.inventory` | no | no | Read-only |
| `secretscan.run` | no | no | Read-only |
| `changeset.apply` | **yes** | **yes** | The only file-writing operation |
| `changeset.revert` | **yes** | **yes** | Consumes a rollback handle; itself a mutation with its own authority |
| `git.branch_commit_push` / `git.open_pr` | **yes** | **yes** | Mutates the repository, not the working tree; same envelope rules |
| `secrets.inject` | **yes** | **yes** | Writes environment material for a process; never writes it to a file |

The Go type is an enum with an exhaustive dispatch table, and a test asserts every enum member has a handler *and* that every handler is reachable only from the dispatch table. There is no `exec`, no `shell`, no `run_command`, and no operation that takes a command string as an argument — a reviewer can verify that claim by reading one file.

Two scope notes on this table, so it cannot be read as pulling Phase 2 forward. `git.branch_commit_push` and `git.open_pr` wrap the **Phase 0 §0.6 client** (`go-git` + `go-github`, D-5) unchanged and add no new capability; what Phase 1 adds is that they now require a signed envelope like every other mutation. `secrets.inject` materialises environment variables for the duration of one command, which is exactly `phases.md` §1.8's "deploy-time secret injection (environment variables)"; it does not deploy anything, and `iac.Runner` still exposes no `apply` (§1.4). Nothing in this catalogue performs a deployment, drives a dashboard, interprets natural language, collects metrics, remediates automatically, or records feedback for learning — the six capabilities `phases.md` excludes verbatim.

### 7.8 Dependency-injection conventions

Unchanged from Phase 0 §7.5. Go stays on constructor injection with consumer-declared interfaces and no framework; Python stays on lifespan-constructed resources on `app.state` with `Depends` providers. Phase 1's addition is that §0.4.1's `production_app` fixture makes the composition *testable as composed*, which is what closes D-23's gap. The graph gets larger, not different.

One convention worth adding explicitly because Phase 1 has many more collaborators: **a constructor takes at most one collaborator per responsibility, and no constructor takes `Settings`.** Passing `Settings` around makes every component's real dependencies invisible and defeats the conformance test's ability to derive them from annotations. Components take the specific values they need.

### 7.9 Telemetry seams

Unchanged. W3C trace-context propagation continues across the new surfaces — the WSS session carries `traceparent` in `command.execute` params so an agent-side operation joins the request's trace, and `audit_events.trace_id` records it. Still no OTel SDK, no collector, no exporter, no `gen_ai.*` semantic conventions (Phase 3). The `Tracer` interface still has exactly one implementation, `NoopTracer`.

### 7.10 Task orchestration

The decision and its rationale are in §4.6. The implementation shape:

```python
# backend/src/core/tasks.py (addition; the Protocol and InlineDispatcher are unchanged)
class ArqDispatcher:
    """The ONLY module in the codebase permitted to import arq (Ruff banned-api).

    Deliberately thin. It maps (name, payload, idempotency_key) onto arq's
    enqueue_job and returns TaskHandle(dispatcher="arq"). It exposes no arq
    concept upward — no job object, no defer_by, no queue name — because OQ-16
    keeps the Phase 2 durable engine open and any leak here becomes a rewrite
    there (Research §0, §B6).
    """
    def __init__(self, pool: "ArqRedis", *, queue: str) -> None: ...
    async def enqueue(self, name: str, payload: dict[str, Any], *,
                      idempotency_key: str | None = None) -> TaskHandle: ...
```

Idempotency is mapped onto arq's `_job_id`, so a duplicate enqueue with the same key is a no-op rather than a second run — which matters because `index.full` and `generation.run` are both expensive and both retried by the frontend. Handlers are registered through the existing module-level `_TASK_HANDLERS` registry and `@register_task(name)` decorator, so an ARQ worker and the `InlineDispatcher` execute *the same functions*; a test runs the full handler set under both and asserts identical results. That is what keeps the seam honest rather than decorative.

Phase 1's queued tasks: `index.full`, `index.incremental`, `embed.batch`, `generation.run`, `readiness.score`, `policy.bundle.publish`, `secretscan.project`. All are fire-and-forget with at-most-once user-visible effect; none carries a multi-step saga, because a saga is what the Phase 2 durable engine is for.

### 7.11 Secret handling and the redaction chokepoint (§1.8)

Phase 1 is the first phase to send real code to real models, so NFR-10 becomes live. The design has four parts, and the enforcement is in the type system rather than in review.

**(a) Detection at the source.** The agent scans every chunk with the Gitleaks ruleset (Research §F20 names Gitleaks) plus any project-configured patterns, producing findings with `kind`, `path`, `line`, `entropy` — **never the value**.

**(b) Redaction produces a distinct type.** The only constructor of `RedactedChunk` is `secretscan.Redact`. Findings are replaced by `FORGEOPS_REDACTED:<kind>:<hash8>` where `hash8` is the first 8 hex of `HMAC-SHA256(project_pepper, value)`. The keyed hash is deliberate: it lets the same secret be recognised across chunks (useful for "this key appears in 6 files") without being reversible by anyone holding only the index.

**(c) One prompt-assembly chokepoint the retriever cannot bypass.**

```python
# backend/src/generation/context.py
RedactedPrompt = NewType("RedactedPrompt", str)     # constructible only via secrets.redaction

def assemble_prompt(
    *,
    system: SystemBlock,
    chunks: Sequence[RedactedChunk],
    instruction: RedactedInstruction,
) -> RedactedPrompt:
    """The ONLY function in the codebase that builds an LLM prompt.

    Every parameter is a type that only secrets.redaction can produce. There is no
    overload taking `str`, so "forgetting to redact" is not a mistake a caller can
    make — it is a call that does not type-check and does not bind (§0.4.2). The RAG
    retriever cannot bypass this because it returns RedactedChunk and nothing else:
    the store contains only redacted text (§6.3 FileContent).
    """
```

**(d) The cache clause.** `TieredSemanticCache.lookup` and `.store` take `RedactedPrompt`, not `str` (D-44). Two consequences follow mechanically: a cache key is never computed over unredacted text, and a cached completion is unreachable from an unredacted prompt because there is no code path that can produce the key. **Q-13** asserts both halves, and its negative control widens the signature back to `str` — which must make the property fail.

**(e) Storage and injection.** Infisical is the encrypted store (§11.8), reached over its REST API with `httpx` — no new SDK dependency, and the Phase 0 `KeyResolver` seam already anticipated it. Secrets are injected at deploy time as environment variables via the `secrets.inject` operation, and are **never** written into a file and never included in a change-set. A change-set containing a value that matches a known secret is refused by the chokepoint with `secret-redaction-failed` (Appendix C.1) — a mutation is not a laundering channel.

**(f) The inherited `fileops` blocklist bug — fixed, carefully.** The Phase 0 rule blocks `.env` and everything with a `.env.` prefix, which wrongly blocks `.env.example`: a committed, placeholder-only file that `scripts/init-env.sh` copies from and that `phases.md` §1.5 explicitly lists as a **generated artifact**. The rule is right in intent and one case too wide. D-46 splits it by intent:

```go
// agent/internal/fileops/blocklist.go
//
// Two rules, because reading a real .env into an LLM prompt and writing a
// placeholder .env.example are opposite acts that the Phase 0 single rule
// conflated (design §7.11(f), D-46).

// blockedForRead reports whether a path must never be read into analysis, an
// index, or an LLM context. Unchanged in strictness: real env files, key
// material, SSH and cloud credential directories.
func blockedForRead(abs string) bool

// blockedForWrite reports whether a path must never be written by a mutation.
// Identical to blockedForRead EXCEPT that the documented placeholder-only env
// templates are permitted, because generating them is a Phase 1 deliverable:
//   .env.example, .env.sample, .env.template
// Everything else with a .env prefix stays blocked, and *.pem / ~/.ssh / ~/.aws
// stay blocked for both.
func blockedForWrite(abs string) bool
```

The allowance is an exact, closed list of three names — not a pattern like `.env.*example*`, which a file called `.env.production.example.bak` would slip through. A test enumerates the tricky cases: `.env`, `.env.local`, `.env.production`, `.env.example`, `.env.example.bak`, `.envrc`, `sub/.env`, and asserts the read/write matrix for each. P-08's blocklist clause continues to hold for reads; Q-01 covers the write side.

### 7.12 Multi-tenancy and pooling

Covered by §6.7. Restated as a one-line rule for implementers: **`SET LOCAL` inside the transaction, `statement_cache_size=0` when pooling in transaction mode, and no session-level state anywhere.** Phase 0's `with_ef_search` is the pattern; anything that needs per-request database state copies it.

### 7.13 Testing strategy

| Component | Unit | Integration | Property | E2E / Load |
|:---|:---|:---|:---|:---|
| Agent | `go test -race -shuffle=on ./...`; envelope canonicalisation against shared fixtures; operation-table exhaustiveness; `contract_test.go` interface assertions | `//go:build integration`: real tofu, real Trivy, real kind API server (via `KUBECONFIG`), real wazero grammar load | `pgregory.net/rapid`: Q-01, Q-02, Q-10, Q-11, Q-14, Q-15, Q-25, Q-31 | driven by the `e2e` job through a real paired agent |
| Backend | `pytest`; call-site conformance (§0.4.2); the test-double lint's own tests (§0.4.3) | `production_app` fixture against real Postgres/Redis/OPA/Cerbos/OIDC-fixture; every migration gated by `require_capability` | `hypothesis`: Q-03 … Q-09, Q-12, Q-13, Q-16 … Q-24, Q-26 … Q-31 | — |
| Frontend | `vitest` + Testing Library; SSE reader; diff rendering; form-error mapping | fetch interception on the API client | `fast-check`: Q-23's diff round-trip on the client side | Playwright (`e2e` job), k6 unchanged |
| Cross-cutting | — | — | — | `mutation` job asserts every Q-property fails under its negative control (§0.4.5) |

**Coverage gate (D-31, resolving OQ-17).** ≥70 % per component, gated, not aggregated:

- backend: `pytest --cov=src --cov-branch --cov-fail-under=70`;
- agent: `go test -coverprofile` over `./internal/...` with `scripts/check-coverage.sh 70`;
- frontend: `vitest --coverage` with v8 provider and `thresholds: { lines: 70, functions: 70, branches: 70 }`.

Aggregating the three would let a well-covered backend hide an untested agent, which is exactly the component that writes to a user's disk. Vendored `.wasm` blobs and generated code are excluded from the denominator; the exclusion list is committed and reviewed rather than expressed as a wildcard.

Coverage is a floor, and §0.4 is the ceiling: Phase 0's broken gateway was *covered*. Both gates run, and neither is described as sufficient.

---

## 8. Build, CI and Release

### 8.1 Supply-chain custody — unchanged, plus one addition

The Phase 0 chain (GoReleaser → Syft CycloneDX → Cosign keyless → Fulcio → Rekor → SLSA v1 via `cosign attest-blob --new-bundle-format`, per D-20/20a/20b) is **not modified**. Six targets, `CGO_ENABLED=0`, `-trimpath`, `mod_timestamp` all hold, which is the operative benefit of D-29.

The one addition is §8.6: the vendored grammar `.wasm` components are merged into the SBOM, because Syft cannot discover a binary blob embedded with `go:embed`.

### 8.2 Cross-compilation matrix

Unchanged: `linux`, `darwin`, `windows` × `amd64`, `arm64` — six targets, `CGO_ENABLED=0` for all of them. D-29 exists so this table needs no footnote. The Windows Job Object work (D-37, resolving OQ-6) uses `golang.org/x/sys/windows`, which is pure Go, so it does not disturb the matrix either.

Binary size grows materially and the number is worth stating rather than discovering: the embedded grammars (~8 languages × 0.5–2 MB), wazero, the Rego evaluator, the Helm SDK and `compose-go` together add roughly **25–45 MB** to a ~40 MB binary. `agent doctor` reports the embedded component inventory and versions so a user can see what they are carrying. If that proves unacceptable, the mitigation is a build tag that omits the Helm SDK — recorded as **OQ-27**, not silently assumed.

### 8.3 CI pipeline — the real job list, before and after

Phase 0's `.github/workflows/ci.yml` has exactly nine jobs. The workflow header comment claims an `e2e` stage that does not exist; Phase 1 either creates every job it names or does not name it. **Appendix E's evidence strings reference only jobs in the right-hand column.**

| Job | Phase 0 | Phase 1 | Change |
|:---|:---:|:---:|:---|
| `changes` | ✅ | ✅ | filters extended for `policies/**`, `agent/internal/scanner/grammars/**` |
| `pre-commit` | ✅ | ✅ | adds `check-test-doubles.py`, `check-no-latest.sh`, `check-ci-jobs.py`, compose digest/rootless assertions |
| `lock-integrity` | ✅ | ✅ | also verifies `agent/tools/go.sum` and `grammars.lock.json` |
| `agent` | ✅ | ✅ | adds the coverage gate, `check-chokepoint.sh`, `check-go-interface-assertions.sh`, Trivy + tofu capabilities, wasm load test |
| `backend` | ✅ | ✅ | adds OPA + Cerbos + OIDC-fixture services, the coverage gate, conformance and wiring selections, `check-route-auth.py`, `check-no-skips.py` |
| `frontend` | ✅ | ✅ | adds the coverage gate |
| `compose-smoke` | ✅ | ✅ | **now builds both images and runs `docker compose up -d --wait`** (debt D2) |
| `audit` | ✅ | ✅ | `pnpm audit` gating; every tool pinned (debt D4) |
| `supply` | ✅ | ✅ | adds SBOM merge verification (§8.6) |
| **`k8s`** | ❌ | ✅ **new** | kind cluster; server-side dry-run validation; SPIRE attestation harness; template manifests (**D-28**) |
| **`e2e`** | ❌ | ✅ **new** | Playwright against built containers with a real paired agent; the criterion-10 journey (debt D3) |
| **`mutation`** | ❌ | ✅ **new** | every Q-property must fail under its declared negative control (§0.4.5) |
| **`policy`** | ❌ | ✅ **new** | `opa test policies/ -v` for both the gateway and governance bundles; `opa check --strict` |
| **`templates`** | ❌ | ✅ **new** | all 8 languages × 5 artifact classes through the real validation pipeline (§11.5.7) |
| **`secrets`** | ❌ | ✅ **new** | Infisical container; secret CRUD and injection integration tests |
| **`auth`** | ❌ | ✅ **new** | real Authentik container; the full OIDC code+PKCE flow; RBAC matrix |

Fifteen jobs. `changes` keeps most of them off most pull requests, and the heavy three (`k8s`, `e2e`, `auth`) are additionally gated to run when their filters match or on `main`, with a nightly full run. That is a real cost of D-28 and D-3, stated rather than absorbed.

#### 8.3.1 The `k8s` job (D-28)

```yaml
  k8s:
    needs: changes
    if: needs.changes.outputs.agent == 'true' || needs.changes.outputs.backend == 'true'
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@<sha>
      - uses: helm/kind-action@<sha>              # kind pinned by action SHA
        with:
          version: v0.27.0                        # exact
          node_image: kindest/node:v1.32.2@sha256:<committed-digest>
      - name: Server-side dry-run validation of generated and template manifests
        run: go test -tags=integration ./internal/validator/... -run 'K8s'
        env: { FORGEOPS_REQUIRE_INTEGRATION: "1" }
      - name: SPIRE attestation harness
        run: bash scripts/k8s/spire-attest-test.sh   # pinned SPIRE manifests
      - name: Backend integration tests needing an API server
        run: pytest -m 'mandatory and kubernetes' --report-log=k8s.jsonl
      - run: python scripts/check-no-skips.py k8s.jsonl
```

What this buys, concretely: `kubectl --dry-run=server` semantics become real, so §1.5's K8s validator tests admission, defaulting, pruning and `apiVersion` availability rather than shape alone; and §1.10's SPIFFE attestation on namespace + service-account + image-digest is exercised in the shape it was designed for instead of being asserted in prose. What it costs: roughly 2–4 minutes per matching run, one more pinned image and binary, and a job class that can flake on cluster-ready timing — mitigated by `kind`'s own wait plus a bounded readiness poll, and by not making `k8s` a required check for documentation-only changes.

#### 8.3.2 The `e2e` job (debt D3, criterion 10)

Builds the backend and frontend images, starts the stack with a compose overlay, builds the real agent binary and runs it as a container with a fixture Node.js project mounted, pairs it with a code minted through the API, and drives the browser through the criterion-10 journey (§12.6). The OIDC issuer here is a **small signed-JWT fixture service**, not Authentik: Authentik's cold start is expensive and the flow it adds is covered by the dedicated `auth` job (OQ-28 records this split for confirmation). Playwright traces and the agent's logs are uploaded on failure, because an e2e failure with no artifacts is a rerun rather than a diagnosis.

#### 8.3.3 The `mutation` job

Runs `scripts/mutation-harness.py --all`, prints one row per property, and fails on any `VACUOUS`. It also fails if `mutations.toml` lacks a row for any `Q-` id defined in Appendix B — so adding a property without a negative control breaks the build, which is the only way the clause stays true over time.

### 8.4 Supply-chain hardening (debt D4)

| Item | Phase 0 | Phase 1 |
|:---|:---|:---|
| `pnpm audit` | `|| true` | gating at `--audit-level high`; a real advisory fails the build |
| `govulncheck` | `@latest` | pinned in `agent/tools/go.mod` and run via `go run`, so `go.sum` verifies the checksum |
| `golangci-lint` | `@v1.62.2` (mutable tag) | same version, installed from `agent/tools/go.mod` with `go.sum` integrity |
| `pre-commit`, `pip-audit`, `pip-tools` | `pip install` unpinned (except `pip-tools`) | all three hash-pinned in a `requirements-tools.lock` installed with `--require-hashes` |
| Any `@latest` | present | `scripts/check-no-latest.sh` greps every workflow, script and Dockerfile and fails on a match |
| Compose images | one un-pinned (`infisical`) | `scripts/check-compose-validate.py` fails unless **every** image has `@sha256:`; OPA tag must end `-rootless` |

`pip-tools` stays at **7.6.0** (D-21), not the Phase 0 design's 7.4.1 — the repository is right and the design table was stale (§15.9).

### 8.5 `.gitattributes`

Drop `-diff` from all four lockfiles; keep `linguist-generated`. A lockfile diff is the highest-signal artifact when reviewing a dependency bump, and Phase 1 adds around twenty dependencies across three ecosystems. `linguist-generated` still keeps them out of language statistics and collapses them by default in GitHub's UI, which was the legitimate half of the original intent.

### 8.6 SBOM coverage for the vendored Wasm grammars

Syft reads `go.mod`, `package.json` and `pyproject.toml`. It cannot see bytes embedded by `go:embed`, so without an explicit step the SBOM would silently omit the only non-Go artifacts in the binary — which for a project whose release story is "signed, SBOM'd and attested" is exactly the wrong omission.

```
grammars.lock.json  →  scripts/sbom-merge.py  →  merged CycloneDX
   { name: "tree-sitter-python", version: "0.23.6",
     sha256: "<64 hex>", licence: "MIT", source: "<release URL>",
     purl: "pkg:generic/tree-sitter-python@0.23.6?checksum=sha256:<hex>" }
```

`scripts/sbom-merge.py` injects one CycloneDX `component` per grammar with its `purl`, licence and hash, and the `supply` job asserts three things: the merged document still validates against the CycloneDX 1.6 schema; every entry in `grammars.lock.json` appears as a component; and every embedded blob's SHA-256 matches its lock entry. The agent additionally verifies each digest **at load time**, so a tampered binary fails closed at first parse rather than producing wrong ASTs (**Q-25**).

### 8.7 Release workflow

Unchanged except that `release.yml` gains the SBOM merge step in the same position Syft occupies today, before signing, so the signed SBOM is the merged one. D-20a's ordering rule — the self-verify step runs **before** every provenance step — is preserved verbatim, since that ordering is load-bearing evidence for criterion 16 and was itself the fix for a real failed release run.

---

## 9. Low-Level Design — Notation and Conventions

Unchanged from Phase 0 §9. Go 1.26 for §10, Python 3.13 / FastAPI for §11, TypeScript / React for §12, structured `pascal` pseudocode for Appendix A's cross-language algorithms, executable property statements for Appendix B.

Signatures below are **contracts, not implementations**: exported names, parameter and return types, error semantics, and a doc comment explaining why the shape is what it is. Bodies appear only where the body *is* the design — canonicalisation, the closure computation, the loop's transition function, the authority mint.

Two conventions added for this phase:

- **Every error is typed and carries a code that maps to an RFC 9457 suffix** (Appendix C), so one vocabulary spans HTTP, JSON-RPC and the agent's logs. A stringly-typed error is a defect.
- **Anything that can fail closed, does.** Where a design choice exists between "continue with degraded information" and "refuse", Phase 1 refuses for anything on the mutation path and degrades only for read paths (retrieval, cache, readiness detail). The one-line test for a reviewer: if the failure could cause a wrong file to be written, it must refuse.


---

## 10. Low-Level Design — Go Agent

### 10.1 Package layout and dependency direction

Additions to Phase 0 §10.1's graph. Dependencies still point inward only; `internal/app` and `cmd/agent` remain the only packages that know the whole graph.

```mermaid
graph TD
    APP["internal/app"] --> SESS["internal/session"]
    APP --> IDENT["internal/identity"]
    APP --> SCAN["internal/scanner"]
    APP --> EXEC["internal/executor"]
    APP --> POL["internal/policy"]
    APP --> VAL["internal/validator"]
    APP --> DEVT["internal/devtools"]
    SESS --> CONN["internal/connection<br/>P0 Transport"]
    SESS --> IDENT
    SESS --> EXEC
    SESS --> ENV["internal/envelope<br/>LEAF: Envelope, Verified,<br/>CanonicalBytes, Verifier"]
    EXEC --> ENV
    EXEC --> POL
    EXEC --> VAL
    EXEC --> MUT["internal/executor/internal/mutate<br/>COMPILER-ENFORCED BOUNDARY"]
    EXEC --> GIT["internal/git<br/>P0"]
    MUT --> ENV
    MUT --> FOPS["internal/fileops<br/>path validation + diff"]
    SCAN --> SEC["internal/secretscan"]
    SCAN --> AST["internal/scanner/ast<br/>wazero + embedded grammars"]
    VAL --> IAC["internal/iac<br/>P0, validate+plan only"]
    VAL --> K8SX["internal/k8s<br/>P0, extended with dry-run"]
    POL --> REGO["embedded Rego evaluator"]
    style MUT stroke-width:3px
```

Only `internal/executor` and its own subtree can import `internal/executor/internal/mutate`. That is Go's nested-`internal` rule, and it is the strongest available enforcement: a violation does not compile.

### 10.2 `internal/identity` — one seam, two providers

```go
// Package identity supplies the agent's cryptographic identity for mTLS.
//
// Two providers exist because Research §H31's model is cluster-shaped and Phase 1's
// primary reality is a developer laptop (§14.3 states that gap honestly). Both
// satisfy "no long-lived agent keys": the paired-device certificate lives ≤24h and
// renews over the live session; the SVID lives as long as SPIRE says and no longer.
type Provider interface {
    // ClientTLS returns a config for dialling the backend. The returned config's
    // certificate MUST be short-lived; implementations that would return a
    // non-expiring credential are a design violation, not an option.
    ClientTLS(ctx context.Context) (*tls.Config, error)

    // Identity describes who we are, for logging and for agent.status.
    Identity(ctx context.Context) (Info, error)

    // RenewBefore reports how long before expiry a renewal should start, so the
    // session manager can renew without dropping the connection.
    RenewBefore() time.Duration
}

type Info struct {
    Kind        string    // "paired_device" | "spiffe_workload"
    Subject     string    // device id, or the SPIFFE ID
    Fingerprint string    // sha256 of the leaf certificate
    NotAfter    time.Time
}

// PairedDevice uses the client certificate issued during pairing (§3.1). The
// private key is generated on this machine and never leaves it; only a CSR is sent.
type PairedDevice struct{ /* store, csr signer, logger */ }

// SpiffeWorkload fetches an X.509-SVID from the SPIFFE Workload API. Used when the
// agent runs inside a cluster, where namespace + service-account + image-digest
// attestation is real. Exercised by the k8s CI job (§8.3.1), not on laptops.
type SpiffeWorkload struct{ /* socket path, logger */ }

var (
    _ Provider = (*PairedDevice)(nil)
    _ Provider = (*SpiffeWorkload)(nil)
)
```

### 10.3 `internal/session` — pairing, handshake, heartbeat, reconnect

```go
// Package session implements the agent half of phases.md §1.1: JSON-RPC 2.0 over
// WSS on Phase 0's fixed envelope, layered ABOVE connection.Transport so the
// Phase 0 transport contract is consumed, not modified.
type Manager struct{ /* transport, identity, store, verifier, dispatcher, clock, logger */ }

// Pair exchanges a one-time pairing code for a device token, an envelope key and a
// short-lived client certificate. It is the only method that runs unauthenticated,
// and it runs exactly once per device: the code is single-use server-side, so a
// retry after a successful exchange fails by design.
//
// The 6-character code is small (32^6 ≈ 1.07e9 for Crockford base32). What makes it
// safe is not the code's entropy alone but the enclosing controls, all server-side:
// single use, 5-minute expiry, a 5-attempt cap per code after which the code is
// burned, a per-IP exchange limit, a global exchange bucket, at most one live code
// per project, constant-time comparison, and storage of only an HMAC of the code.
// With ≤10 live codes and the per-IP cap, an attacker's expected success over the
// full 5-minute window is below 1e-6 (§14.6 does the arithmetic).
func (m *Manager) Pair(ctx context.Context, code string, backendURL string) (*PairResult, error)

// Serve runs the session until ctx is cancelled or the device is revoked. It
// reconnects with exponential backoff (base 1s, cap 60s, jitter 0.5x) and resets
// the attempt counter only after a SUCCESSFUL session.connect — not after a
// successful TCP dial, which would hot-loop against a backend that accepts sockets
// and rejects handshakes.
func (m *Manager) Serve(ctx context.Context) error

// ErrUnpaired is returned when a backend URL is configured but no device token
// exists. Distinct from Phase 0's connection.ErrDisabled (no URL at all) so
// `agent doctor` can tell a user which of the two situations they are in.
var ErrUnpaired = errors.New("session: no device token; run `forgeops-agent pair`")

// ErrRevoked is returned when the backend reports this device revoked. The caller
// aborts in-flight work, rolls back from the backup manifest, and wipes credentials.
var ErrRevoked = errors.New("session: device revoked")
```

Credential storage at rest:

```go
// Store persists the device credential set. Primary implementation uses the OS
// keychain (macOS Keychain, Windows Credential Manager, Secret Service on Linux),
// which Research §2 names for local agent state. When no keychain is available —
// headless Linux without a Secret Service, a common CI and server case — it falls
// back to a 0600 file under the agent's state directory, and `agent doctor` reports
// the degraded mode explicitly rather than pretending. See OQ-26.
type Store interface {
    Save(ctx context.Context, c Credentials) error
    Load(ctx context.Context) (Credentials, error)
    Wipe(ctx context.Context) error
    Backend() string     // "keychain" | "file(0600)" — surfaced by agent doctor
}

type Credentials struct {
    DeviceID    string
    DeviceToken []byte    // opaque 32 bytes; the backend stores only its HMAC
    EnvelopeKey []byte    // 32 bytes, shared with the backend for HMAC-SHA256
    ClientCert  []byte    // PEM, ≤24h
    ClientKey   []byte    // PEM, generated locally, never transmitted
    CABundle    []byte    // PEM
}
```

Durable offline queue (D-41, delivering **NFR-18**):

```go
// Journal is the agent's durable OUTBOUND queue. It exists so an agent that loses
// its connection keeps working and catches up on reconnect (NFR-18, PRD §4.3 P1).
//
// The type that makes it safe is RecordKind: there is deliberately NO kind for a
// command envelope, an approval response, an approval_id, an authority, a device
// token or a secret value. Nothing that AUTHORISES a mutation can be represented,
// so nothing that authorises a mutation can be persisted — which is why envelope
// expiry, seq allocation, revocation and policy staleness (D-41's items 1–4) do not
// need mitigating here: they cannot arise.
type RecordKind string

const (
    KindScanBatch      RecordKind = "scan.batch"
    KindCommandResult  RecordKind = "command.result"
    KindCommandProgress RecordKind = "command.progress"
    KindAgentStatus    RecordKind = "agent.status"
    KindSecretFindings RecordKind = "secretscan.findings"   // metadata only, never values
    KindIntent         RecordKind = "intent"                // replayed as approval.request
)

type Record struct {
    RecordID  string          // agent-generated; the backend dedupes on it (at-least-once)
    Kind      RecordKind
    CreatedAt time.Time
    Payload   json.RawMessage
}

type Journal interface {
    // Append durably enqueues one record: length-prefixed write, CRC32C, fsync.
    // It returns ErrJournalFull rather than evicting silently, because a dropped
    // scan batch that nobody reports is an index that is quietly wrong.
    Append(ctx context.Context, r Record) error

    // Drain delivers the journal after a SUCCESSFUL session.connect and after the
    // revocation and bundle-digest checks, in this order, and truncates only what
    // the backend acknowledged:
    //   1. non-mutating records (scan, result, progress, status, findings);
    //   2. intents, each replayed as approval.request so the backend re-runs the
    //      full chokepoint and mints a FRESH envelope. Drain NEVER applies anything.
    // A stale policy bundle stops step 2 and leaves those records queued.
    Drain(ctx context.Context, send func(context.Context, Record) error, bundleCurrent bool) (DrainReport, error)

    // Wipe removes the journal without delivering it. Called when the device is
    // revoked: a revoked principal's queued intents must not reach the backend.
    Wipe(ctx context.Context) error

    // Stats feeds agent.status and `agent doctor` so a growing backlog is visible
    // rather than discovered.
    Stats(ctx context.Context) (JournalStats, error)
}

var (
    ErrJournalFull    = errors.New("journal: AGENT_JOURNAL_MAX_BYTES exceeded")
    ErrJournalCorrupt = errors.New("journal: CRC mismatch; trailing record discarded")
)
```

### 10.4 Envelope verification and replay rejection

```go
// Package envelope — envelope.go
//
// A LEAF package: it imports nothing from internal/**, which is what keeps it out of
// the session → executor → mutate cycle. See D-59, which supersedes this block's
// original `package session` placement and every `*session.Verified` below.
//
// Verified is a value that can only be produced by Verify. Every mutating code
// path in the agent takes one, so "we forgot to check the signature" is not a
// reachable state: there is no other constructor, and the field is unexported.
type Verified struct {
    env       Envelope
    verifiedAt time.Time
}

func (v Verified) Operation() Operation          { return v.env.Operation }
func (v Verified) ApprovalID() string            { return v.env.ApprovalID }
func (v Verified) PolicyContext() PolicyContext  { return v.env.PolicyContext }
func (v Verified) Args() json.RawMessage         { return v.env.Args }

// Verify performs, in this order and short-circuiting on the first failure:
//   1. schema: required members present, no unknown members, seq and not_after are
//      integers (no floats appear in an envelope — §7.6);
//   2. freshness: now <= not_after, and not_after - now <= maxAge, with ±60s
//      tolerated clock skew reported in agent.status;
//   3. signature: constant-time compare of HMAC-SHA256(key,
//      "forgeops-envelope-v1" || 0x00 || JCS(envelope без signature));
//   4. ordering: seq > lastSeq for this device, then lastSeq = seq atomically;
//   5. uniqueness: nonce unseen in a bounded LRU covering at least maxAge;
//   6. policy binding: policy_context.bundle_digest == the loaded bundle's digest.
//
// Every failure returns a typed error whose Code() maps to an RFC 9457 suffix and
// an agent.error code, and NO failure path reaches the executor. Property Q-14
// covers 1–3, Q-15 covers 4–5, Q-07 covers 6.
func (v *Verifier) Verify(ctx context.Context, raw []byte) (*Verified, error)

// CanonicalBytes is exported for one reason: the cross-runtime fixture test. The
// Python side must produce byte-identical output for the same envelope, and the
// only way to keep that true is to test both against the same committed vectors.
func CanonicalBytes(e Envelope) ([]byte, error)
```

Ordering note that is easy to get wrong: signature verification happens **before** the sequence and nonce updates. Verifying order first would let an unauthenticated attacker advance a device's `seq` counter and lock out the real backend — a denial of service through a check that was supposed to be a defence.

### 10.5 `internal/executor` — named-operation dispatch and the mutation boundary

```go
// Package executor dispatches verified commands to named operations. There is no
// shell, no exec of an arbitrary string, and no operation whose argument is a
// command line (phases.md §1.1: "named operations only").
type Dispatcher interface {
    // Execute runs one verified command. It emits progress through sink and returns
    // a Result that the session marshals into command.result.
    Execute(ctx context.Context, v *envelope.Verified, sink ProgressSink) (Result, error)

    // Operations returns the closed catalogue, for agent.status and agent doctor.
    Operations() []OperationInfo
}

// handlerTable is the ONLY dispatch surface. A test asserts every Operation enum
// member has an entry and that no handler is referenced from anywhere else, which
// is what makes the catalogue's closedness checkable rather than asserted.
var handlerTable = map[Operation]handler{ /* ... */ }
```

```go
// agent/internal/executor/internal/mutate/apply.go
//
// Package mutate holds the agent's ONLY file-writing code. It is importable solely
// from within internal/executor/** by Go's nested-internal rule — a compile-time
// boundary, not a convention (design §2.2.1, D-45).
package mutate

// ApplyVerified applies a change-set atomically, or applies nothing.
//
// It supersedes Phase 0's exported fileops.Ops.ApplyAtomic (D-45). The change is
// the ARGUMENT, not the algorithm: the Phase 0 algorithm (validate every path,
// back up before mutate, temp file + fsync + rename, roll back every write in
// reverse on any error) is preserved exactly and P-08 continues to guard it. What
// changes is that a caller must now present a *envelope.Verified, so a mutation
// without a governance-signed envelope is a compile error rather than a review miss.
//
// Additional Phase 1 obligations:
//   - each entry carries the expected pre-image hash; a mismatch aborts the whole
//     set with ErrConflict and writes nothing (change_sets → conflicted);
//   - the returned BackupManifest is the rollback handle the backend persists;
//   - write-intent path rules apply (blockedForWrite), so generating .env.example
//     is permitted while .env, *.pem, ~/.ssh and ~/.aws stay refused (D-46).
func ApplyVerified(
    ctx context.Context,
    v *envelope.Verified,
    root string,
    entries []Entry,
) (*ApplyReport, error)

type Entry struct {
    RelPath      string
    Action       Action        // Create | Update | Delete
    Content      []byte        // nil for Delete
    ExpectedHash string        // sha256 of the pre-image; "" for Create
    Mode         os.FileMode
}

type ApplyReport struct {
    Written  []WrittenFile     // path, new hash, backup path
    Backups  BackupManifest    // the rollback handle
    Duration time.Duration
}

// Revert restores every file named by a manifest to its pre-image. It is itself a
// mutation and therefore also requires a Verified envelope with its own approval —
// a rollback is a change to the user's disk and gets the same scrutiny.
func Revert(ctx context.Context, v *envelope.Verified, m BackupManifest) (*RevertReport, error)
```

### 10.6 `internal/policy` — the agent half of double evaluation

```go
// Package policy evaluates the governance bundle locally, giving PRD §2.2's
// invariant 4 ("double policy evaluation — both server-side and agent-side") a real
// second opinion rather than a restatement of the first.
type Evaluator interface {
    // Evaluate returns the agent's independent decision. It never contacts the
    // network: the bundle is local, so a partitioned agent still enforces policy.
    Evaluate(ctx context.Context, in Input) (Decision, error)

    // BundleDigest reports the loaded bundle's digest, sent in session.connect and
    // every agent.status so the backend can detect drift (Q-07).
    BundleDigest() string

    // Load replaces the bundle atomically. A failed load leaves the previous bundle
    // in place and returns an error; it never leaves the agent with no policy.
    Load(ctx context.Context, bundle []byte, expectedDigest string) error
}

type Decision struct {
    Result  Result   // Allow | Deny | RequireApproval
    Reason  string   // human-readable, surfaced in agent.error and the audit record
    Rule    string   // which rule decided, for explainability (FR-37)
}

// ErrNoBundle is returned when no bundle has been loaded. Callers MUST treat it as
// Deny: an agent with no policy is not an agent with permission. This is the
// fail-closed direction, and Q-06's negative control flips it to prove the test
// would notice.
var ErrNoBundle = errors.New("policy: no bundle loaded")
```

**Which side wins.** Both sides must allow. If either denies, the operation is denied. If the digests disagree, the agent denies and requests a bundle refresh (`policy-bundle-stale`), and the backend's chokepoint refuses to mint authority for a `policy_stale` device — so disagreement fails closed on *both* sides independently. That redundancy is the point: a bug in one side's staleness check does not open the gate.

**Drift detection** runs continuously rather than at connect time only: every `agent.status` carries the agent's digest, and the backend compares it to the active bundle. A device whose digest lags moves to `policy_stale` and is refused authority until it reloads.

#### 10.6.1 D-30 — how "OPA compiled to Wasm" is satisfied, stated honestly

`phases.md` §1.10 and Research §B7 both say "OPA compiled to **Wasm** embedded in the Go agent". Phase 1 embeds **OPA's own Go Rego evaluator** (`github.com/open-policy-agent/opa/rego`) and evaluates the bundle in-process, with no Wasm and no cgo.

Why: the literal Wasm route needs a Wasm host for the compiled policy. The mainstream Go host, `wasmtime-go`, requires cgo — which would break the six-target `CGO_ENABLED=0` build that D-1 protected and D-29 preserves. wazero could host it, but compiling Rego to Wasm loses parts of the language (notably `http.send` and some builtins) and adds a build step producing an artifact that must itself be signed, versioned and verified — for no gain over evaluating the same Rego directly.

What is preserved is the *requirement behind the wording*: policy is evaluated **inside the agent binary**, **offline**, **from a signed, versioned bundle**, by **the OPA project's own evaluator**, so the agent and the backend run the same Rego semantics. What is not satisfied is the literal compilation target. This is recorded as a **deliberate, numbered deviation (D-30)** rather than a claim that the wording is met, and it is reversible: if a pure-Go Wasm host for OPA policies becomes viable, the `Evaluator` interface above absorbs it without touching a call site — which is exactly why it is an interface.

### 10.7 `internal/validator` — the §1.5 validators

`phases.md` §1.5 names five validator commands. Phase 1 implements them as **in-process libraries** wherever one exists, and as a subprocess only where the tool has no library form. The reason is the property the whole agent design rests on: a single static binary that works on a user's machine without them installing five CLIs.

| §1.5 requirement | Phase 1 implementation | Needs a live API server? | Proven in |
|:---|:---|:---:|:---|
| `docker compose config` | `github.com/compose-spec/compose-go/v2` loader + validator, in-process | no | `agent` |
| `kubectl --dry-run=server` | `k8s.io/client-go` server-side apply with `DryRun: [All]` — the same admission, defaulting and pruning path `kubectl` uses | **yes** | **`k8s`** (D-28) |
| `tofu validate`, `tofu plan` | existing Phase 0 `iac.Runner` subprocess (no library exists) | no | `agent` |
| `yamllint` + JSON Schema | in-process: `sigs.k8s.io/yaml` for syntax + `santhosh-tekuri/jsonschema/v6` against bundled Kubernetes and workflow schemas | no | `agent` |
| `helm lint`, `helm template --validate` | `helm.sh/helm/v3` SDK (PRD §5 names "Helm SDK (Go)") | `--validate` **yes**; `lint` and plain `template` no | `agent` + **`k8s`** |
| Trivy config scan (§1.5 deterministic checks) | `trivy` subprocess when present; otherwise `validator_unavailable` | no | `agent` |

```go
// Validator is one deterministic, read-only check over generated artifacts.
// Every implementation is side-effect free: it may read the workspace and talk to a
// cluster's dry-run endpoint, and it may never write a file or mutate a resource.
type Validator interface {
    Name() string
    // Available reports whether this validator can run here, and why not if it
    // cannot. An unavailable validator is REPORTED, never silently skipped —
    // §0.4.4's rule applied to runtime rather than to tests.
    Available(ctx context.Context) (bool, string)
    Validate(ctx context.Context, in Artifacts) (Findings, error)
}

type Finding struct {
    Validator string
    Severity  Severity      // Info | Warning | Error | Fatal (Phase 0's vocabulary)
    Blocking  bool          // deterministic checks are blocking; the rubric never is
    Code      string
    Message   string        // redacted before it leaves the process (§7.2)
    Path      string
    Line      int
}
```

**The unavailable-validator rule**, because it decides whether an absent tool becomes a silent pass: a blocking validator that cannot run yields `Finding{Severity: Fatal, Blocking: true, Code: "validator_unavailable"}` when the change-set's blast radius is `infrastructure`, and a non-blocking warning otherwise. Fail closed where it matters, degrade where it does not. Recorded as **OQ-25** for confirmation, with that split as the recommendation.

### 10.8 `internal/scanner` — detection, parsing, chunking, graph, incremental

#### 10.8.1 Tiered language detection

```go
// Detect resolves a file's language using the tiers phases.md §1.3 specifies, in
// order, stopping at the first confident answer:
//   1. package manager / manifest evidence (package.json, go.mod, Cargo.toml,
//      pyproject.toml, pom.xml, build.gradle.kts, Gemfile, composer.json, *.csproj)
//      — strongest signal, and the only one that identifies a PROJECT rather than a file;
//   2. file extension;
//   3. shebang line;
//   4. content heuristics (bounded to the first 8 KiB, never the whole file).
// Ties are broken toward the manifest-derived project language, and the tier that
// decided is recorded so the readiness report can explain itself.
func (d *Detector) Detect(path string, head []byte) Detection

type Detection struct {
    Language   string
    Confidence float32
    Tier       int     // 1..4 — which tier decided; recorded for explainability
}
```

#### 10.8.2 AST parsing via wazero (D-29)

```go
// Package ast parses source into a concrete syntax tree using tree-sitter grammars
// compiled to WebAssembly and executed by github.com/tetratelabs/wazero — a pure-Go
// runtime. This is decision D-29: it preserves CGO_ENABLED=0 across all six release
// targets, which the official cgo bindings would have broken (D-1's constraint).
type Parser struct{ /* wazero runtime, compiled modules, pool */ }

// NewParser instantiates the wazero runtime once and compiles each embedded grammar
// once. Compilation is the expensive step, so it happens at construction and the
// resulting modules are pooled per language; parsing a file then costs one module
// instantiation from an already-compiled artifact.
//
// Every grammar's SHA-256 is verified against grammars.lock.json HERE, at load
// time, not only in CI — a tampered binary must fail closed at first parse rather
// than produce plausible-looking wrong ASTs (Q-25).
func NewParser(logger *zap.Logger) (*Parser, error)

// Parse returns a tree, or ErrUnsupportedLanguage when no grammar is embedded for
// the language. The caller degrades to line-based chunking in that case, which is
// worse retrieval but not an error — an unsupported language must not fail a scan.
func (p *Parser) Parse(ctx context.Context, lang string, src []byte) (*Tree, error)

var ErrUnsupportedLanguage = errors.New("ast: no embedded grammar for language")
```

Embedded grammar set for Phase 1, matching the eight template languages so every supported project can be chunked semantically: JavaScript/TypeScript (incl. TSX), Python, Go, Rust, Java, Kotlin, Ruby, PHP, C#, plus YAML, Dockerfile and HCL because the artifacts the phase *generates* are worth parsing too. Twelve grammars, each digest-pinned in `grammars.lock.json`.

**The honest risk (OQ-29).** Tree-sitter grammars do not all publish prebuilt `.wasm` artifacts on their releases. Where a prebuilt artifact exists it is vendored with its digest and its source URL. Where it does not, the grammar is built once by a **digest-pinned container** running the pinned tree-sitter CLI, the output digest is committed, and the `wasm-verify` step in `lock-integrity` reproduces the build and compares. If a grammar turns out not to build reproducibly, the fallback is to omit that language's AST support and fall back to line-based chunking for it — degraded retrieval, not a broken phase. This is the single largest execution risk D-29 carries and it is stated as such rather than assumed away.

#### 10.8.3 cAST semantic chunking

```go
// Chunk implements cAST: bottom-up grouping (statements → functions → classes) with
// constraint-based splitting and density optimisation (Research §0, §C10).
//
// The numbers are Research §C10's and are honoured, not reinvented: ~512 target
// tokens with 128-token overlap for function-level chunks, and ~1024 tokens for
// module-level summaries. Deviating from them would need a decision; none is taken.
//
// Algorithm, in one paragraph so the invariants are visible: walk the tree bottom-up
// accumulating sibling statements into a chunk while the running token count stays
// under target; when a named declaration (function, method, class) is complete and
// fits, emit it as its own chunk carrying its signature and enclosing class as
// metadata; when a single declaration exceeds the target, split it at the highest
// syntactic boundary that yields parts under the target and mark the parts as
// siblings of one logical unit; prepend the file's import block to every chunk (the
// "sliding window for imports" of §C10) so a retrieved chunk is self-contained.
func Chunk(t *Tree, src []byte, cfg ChunkConfig) []Chunk

type Chunk struct {
    Index        int
    Text         string
    Kind         string   // "function" | "class" | "module" | "block"
    Symbol       string   // function or class name
    ParentSymbol string   // enclosing class, for the hierarchy metadata §C10 wants
    Signature    string
    StartLine    int
    EndLine      int
    TokenCount   int
    Imports      []string
}
```

#### 10.8.4 Dependency graph and the incremental closure

```go
// Graph is the project's file-level import graph. Edges may be unresolved (an import
// of a package outside the project); unresolved edges are KEPT so a later scan can
// resolve them without re-parsing the importer.
type Graph struct{ /* forward and reverse adjacency, keyed by relative path */ }

// Dirty computes the closure that must be re-indexed after a change set. This is the
// heart of Q-10, so the rule is stated exactly rather than described:
//
//   dirty = changed
//         ∪ { f : f imports g, g ∈ changed, exports(g) differs from before }
//         ∪ { f : imports(f) differs from before }            // f's own edges moved
//         ∪ { f : f imports g, g ∈ deleted }                  // dangling edges
//
// and, separately, module-level SUMMARIES are invalidated for every file in dirty
// plus every file that directly imports a member of dirty, because a summary
// describes a file in the context of its imports.
//
// Cycles are handled by construction: the closure is a fixed-point over a visited
// set, so a cyclic import graph terminates (Q-25's termination clause). It does NOT
// take the transitive closure of every change: an implementation detail edit that
// leaves a file's exported surface identical does not dirty its dependants, which is
// what makes incremental scanning worth doing at all.
func (g *Graph) Dirty(changed, deleted []string, exportsBefore, exportsAfter map[string]ExportSet) []string
```

`exports(g)` is deliberately coarse — the set of exported symbol names and signatures, not bodies — because that is the part a dependant's chunk metadata embeds. Being coarse makes it cheap and makes Q-10 provable; being finer would be an optimisation with a correctness risk.

#### 10.8.5 Watch mode

fsnotify (Phase 0's real `Watcher`) → a 250 ms debouncer that coalesces per path and drops ignored paths → the closure computer → a bounded fan-out of parser workers (`min(GOMAXPROCS, 8)`) → a fan-in aggregator that batches upserts and deletions. Rename is handled as delete+create on both paths. Directory creation triggers a subtree walk. Watcher-limit exhaustion (`ENOSPC` on inotify) degrades to a periodic poll and is reported in `agent.status` — silently watching nothing is the failure mode to avoid.

**Q-11** covers the coalescing layer specifically: for every event sequence, the debouncer's output must produce the same dirty set as the un-coalesced sequence. Coalescing is an optimisation and must not be able to lose a change.

### 10.9 `internal/secretscan`

```go
// Package secretscan detects and redacts secrets before any chunk leaves the
// machine. Detection uses the Gitleaks rule set (Research §F20 names Gitleaks) plus
// project-configured patterns.
type Scanner interface {
    // Scan returns findings WITHOUT values. A finding carries kind, path, line and
    // a keyed fingerprint; the matched text is never returned, logged, or stored,
    // because a "findings report" that quotes the secret is a second copy of it.
    Scan(ctx context.Context, path string, content []byte) ([]Finding, error)

    // Redact returns text with every finding replaced by
    // FORGEOPS_REDACTED:<kind>:<hash8>. The returned type is RedactedChunk, which
    // has no other constructor — the agent-side half of §7.11's chokepoint.
    Redact(ctx context.Context, c Chunk, findings []Finding) RedactedChunk
}

type Finding struct {
    Kind        string   // "aws-access-key" | "github-pat" | "private-key" | ...
    Path        string
    Line        int
    Fingerprint string   // first 8 hex of HMAC-SHA256(project_pepper, value)
    Entropy     float32
}
```

The keyed fingerprint is what lets the readiness report say "this credential appears in 6 files" without anyone being able to recover it from the index. An unkeyed hash would be reversible for low-entropy secrets by dictionary attack.

### 10.10 `internal/devtools` and `agent doctor`

`devtools` discovers optional external tools and reports versions. `agent doctor` gains rows for: session state (`unpaired` / `paired` / `revoked`, with the credential store backend), certificate expiry, policy bundle digest and staleness, embedded grammar inventory with digests, embedded validator versions (compose-go, helm SDK, jsonschema), external tool availability (`tofu`, `trivy`), clock skew against the backend, and watcher mode (`fsnotify` / `polling`). A `doctor` that cannot explain a degraded mode is why users file "it doesn't work" issues.

### 10.11 Windows process-tree termination (D-37, resolving OQ-6)

Phase 0 shipped `taskkill /PID <pid> /T /F`, recorded as weaker than a Job Object. Phase 1 replaces it:

```go
//go:build windows
// internal/iac/procattr_windows.go
//
// A Job Object with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE guarantees the whole tree
// dies when the job handle closes — including a provider plugin that re-parented
// itself, which taskkill /T can miss. golang.org/x/sys/windows exposes
// CreateJobObject, SetInformationJobObject and AssignProcessToJobObject with no
// cgo, so the CGO_ENABLED=0 six-target build is unaffected (§8.2).
func setProcessGroup(cmd *exec.Cmd) { /* CREATE_NEW_PROCESS_GROUP + job assignment */ }
func terminateGroup(cmd *exec.Cmd, grace time.Duration) { /* graceful, then close the job handle */ }
```

The integration test starts a process that spawns a detached grandchild and asserts both are gone after termination — the case `taskkill /T` can miss and the reason OQ-6 existed.

### 10.12 Dependency-exercise policy (Phase 0 §10.9, extended)

Every new dependency is pinned **and** exercised by real behaviour, a test, or an `agent doctor` probe. New rows:

| Dependency | Phase 1 exercise |
|:---|:---|
| `tetratelabs/wazero` | parses real fixture files in twelve languages; digest verification at load; a corrupted-blob test asserts fail-closed |
| `open-policy-agent/opa` (rego) | evaluates the real governance bundle; Q-06 cross-checks agreement with the backend's OPA server over generated inputs |
| `compose-spec/compose-go/v2` | validates the repository's own `docker-compose.yml` plus generated compose files |
| `santhosh-tekuri/jsonschema/v6` | validates generated K8s manifests and GitHub Actions workflows against bundled schemas |
| `helm.sh/helm/v3` | lints and templates the template library's Helm charts; `--validate` path runs in `k8s` |
| `zricethezav/gitleaks/v8` | detects synthetic, self-labelling test credentials in fixture files; asserts no value is ever returned |
| `zalando/go-keyring` | round-trips a credential; the fallback path is exercised by forcing the keychain unavailable |
| `bradleyfalzon/ghinstallation/v2` | mints an installation token against a recorded-response server (D-38) |
| `spiffe/go-spiffe/v2` | fetches an X.509-SVID in the `k8s` job against a real SPIRE deployment |
| `golang.org/x/sys/windows` | Job Object termination test (Windows runner) |
| `golang.org/x/crypto` (HKDF) | derives the per-purpose keys used by the envelope and fingerprint HMACs |

The D-1 guard row changes as described in §4.7: `deps_test.go` no longer asserts tree-sitter's absence — it asserts no cgo-requiring module is in the graph and that every grammar digest matches.


---

## 11. Low-Level Design — Python Backend

### 11.1 Application factory and lifespan (including debt D1)

The Phase 0 lifespan is extended, not rewritten. The critical addition is the line that has been missing since Phase 0: the model router is built **from the shipped YAML**.

```python
# backend/src/main.py (lifespan additions; Phase 0 construction unchanged above)
    # ── debt D1: the shipped YAML is now what a running backend loads ───────────
    # Phase 0 defined load_tier_config(path, env) and never called it from
    # production, so config/model-tiers.yaml was only ever exercised by fixtures
    # (PROGRESS.md outstanding item). §1.5's entire generation pipeline sits on
    # six-tier routing, so this wiring lands BEFORE any generation code and is
    # proven by Q-27 against the running app, not by reading the file.
    tier_config = load_tier_config(settings.model_tier_config_path, env=os.environ)
    endpoint_registry = EndpointRegistry.from_config(
        tier_config, keys=keys, http=shared_http, tracer=NoopTracer()
    )
    router = ModelRouter(tier_config, endpoint_registry, breakers, cache)
    app.state.tier_config = tier_config        # exposed so Q-27 can assert provenance

    # ── §1.11 auth ─────────────────────────────────────────────────────────────
    app.state.oidc = AppTokenVerifier(settings, http=shared_http)      # extends Phase 0's verifier
    app.state.cerbos = CerbosClient(settings.cerbos_url, http=shared_http)
    app.state.device_service = DeviceService(sessionmaker, redis, settings)

    # ── §1.7 policy ────────────────────────────────────────────────────────────
    app.state.governance_policy = OpaGovernancePolicy(settings.opa_url, http=shared_http)
    app.state.bundle_service = PolicyBundleService(sessionmaker, settings)

    # ── §1.9 audit, §1.10 governance ───────────────────────────────────────────
    app.state.audit = AuditWriter(sessionmaker)
    app.state.hub = AgentHub(redis, settings)
    app.state.chokepoint = GovernanceChokepoint(
        policy=app.state.governance_policy,
        approvals=GovernanceApprovalGate(sessionmaker),
        analyzer=SemanticPlanAnalyzer(),                  # Phase 0, deterministic, unchanged
        audit=app.state.audit,
        hub=app.state.hub,
        devices=app.state.device_service,
        sessionmaker=sessionmaker,
        signer=EnvelopeSigner(app.state.device_service),  # sole holder of the signing path
    )

    # ── §1.3/§1.5 analysis and generation ──────────────────────────────────────
    app.state.dispatcher = ArqDispatcher(await create_arq_pool(settings), queue="forgeops")
    app.state.retriever = HybridRetriever(sessionmaker, redis, settings)
    app.state.reranker = VoyageReranker(http=shared_http, keys=keys, model=settings.rerank_model)
    app.state.generation = GenerationService(
        router=router, retriever=app.state.retriever, reranker=app.state.reranker,
        redactor=Redactor(sessionmaker, settings), judge=RubricJudge(router),
        templates=TemplateLibrary.load(), chokepoint=app.state.chokepoint,
        validation=ValidationPipeline(stages=[SyntaxStage(), SchemaStage(),
                                              DryRunStage(app.state.hub), SemanticStage()]),
    )
```

Two facts about that construction are load-bearing:

- **`InlineDispatcher` is replaced by `ArqDispatcher` in one line**, which is what the Phase 0 seam was for. Tests still build the app with `InlineDispatcher` through the `production_app` fixture's transport substitution, so handlers run in-process without a worker.
- **The `DryRun` stage sits before `Semantic`**, exactly as Phase 0 §11.9 anticipated. `ValidationPipeline.run` and the `Stage` Protocol are untouched — the stage list is data.

The non-destructive lifespan contract is preserved: construction validates local configuration and fails fast on invalid config, but no mandatory network handshake is performed, so Postgres/Redis/OPA/Cerbos being down changes **readiness**, not liveness (Phase 0 §4.4).

### 11.2 `auth` — OIDC, sessions, devices, RBAC (§1.11)

```python
# backend/src/auth/verifier.py
class AppTokenVerifier:
    """Verifies product-API bearer tokens.

    Extends Phase 0's OidcTokenVerifier rather than duplicating it: same JWKS cache,
    same exact-issuer allowlist, same required claims (exp, iat, iss, aud) and the
    same asymmetric-only algorithm list. The one difference is the audience: the app
    API's audience is DISTINCT from the MCP gateway's, so a token minted for the
    gateway cannot be replayed against the product API and vice versa. That is the
    enforceable half of RFC 9207's mix-up defence at a resource server.
    """
    async def verify(self, authorization: str | None) -> Principal: ...
```

```python
# backend/src/auth/principal.py
@dataclass(frozen=True, slots=True)
class Principal:
    """Who is acting. Constructed only by a verifier — never from request data.

    A Principal is required by every non-public route. Its presence is what the
    governance chokepoint's admission stage checks first, so an unauthenticated
    mutation is impossible before any policy is even consulted.
    """
    user_id: uuid.UUID
    subject: str                 # IdP sub
    email: str
    role: UserRole
    tenant_id: uuid.UUID | None
    session_id: uuid.UUID | None
    kind: Literal["user", "device", "service"]
    device_id: uuid.UUID | None = None
    # Blast radius comes from the ATTESTED identity, never from an env var (D-39,
    # resolving OQ-20). For a user principal it is derived from the role; for a
    # device principal it is derived from the device's project grant and its
    # attestation kind.
    blast_radius: Literal["read_only", "workspace", "infrastructure"] = "read_only"
```

```python
# backend/src/auth/dependencies.py
async def require_principal(request: Request) -> Principal:
    """The per-route auth dependency (Phase 0 §4.3 row 7 — never global).

    Attached to every route except auth.PUBLIC_ROUTES. Completeness is not a review
    obligation: scripts/check-route-auth.py enumerates create_app().routes and fails
    the build if any route lacks this dependency and is not in the public set, so the
    allowlist cannot drift from the router (Q-19).
    """

def require_role(*allowed: UserRole) -> Callable[..., Awaitable[Principal]]:
    """Coarse role gate for routes whose authorisation needs no resource attributes."""

async def require_permission(
    request: Request, principal: Principal, *, resource: CerbosResource, action: str
) -> None:
    """Resource-scoped authorisation via the Cerbos sidecar (Tech-Stack §9).

    Cerbos owns the policy; this function owns only the call. A deny raises 403 with
    a body identical to the body a non-existent resource would produce, so error
    shape cannot be used to enumerate projects (§4.2).
    """
```

**How Cerbos relates to the three roles.** Authentik carries group membership; the callback maps groups to exactly one of `admin`, `developer`, `viewer` and stores it on `users.role`. That role is the Cerbos *principal role*. Cerbos policies then express the resource-scoped part that a role alone cannot: a developer may create a change-set on a project they are a member of but may not approve their own change-set for a `prod`-scoped policy; a viewer may read everything they can see and mutate nothing; an admin may do both plus device revocation and policy edits. Roles are coarse and static; Cerbos adds resource, action and attribute. Keeping the split explicit is what stops role checks from creeping into handlers.

Policy set shipped in Phase 1 — three roles × the resource kinds that exist:

| Resource | admin | developer | viewer |
|:---|:---|:---|:---|
| `project` | create, read, update, delete, pair | create, read, update (own/member) | read |
| `change_set` | read, approve, reject, apply, revert | create, read, approve (unless policy forbids self-approval) | read |
| `policy` | create, read, update, delete | read | read |
| `secret` | create, read-metadata, update, delete | create, read-metadata, update | read-metadata |
| `agent_device` | pair, revoke, read | pair, read | read |
| `audit` | read | read (own project) | read (own project) |

No role can read a secret **value** through the API — not even admin. The value exists to be injected at deploy time, and a "reveal" endpoint would turn the vault into a distribution channel. `read-metadata` returns key, environment, rotation date and last-updated only. **Q-20** asserts the viewer-cannot-mutate half and the no-value-read half.

```python
# backend/src/auth/devices.py
class DeviceService:
    """Pairing codes, device tokens, certificates, revocation (§1.1, §3.1).

    Revocation is Redis-authoritative and checked PER MESSAGE, not per connection,
    so an already-open socket dies too (Q-16). Postgres holds the durable record;
    Redis holds the enforcement set. If Redis is unavailable, is_revoked raises and
    the hub closes the session — fail closed, because the alternative is honouring a
    revoked device during an outage.
    """
    async def issue_pairing_code(self, *, project_id: uuid.UUID, actor: Principal) -> PairingCode: ...
    async def exchange(self, *, code: str, csr_pem: bytes, meta: AgentMeta,
                       client_ip: str) -> DeviceCredentials: ...
    async def is_revoked(self, device_id: uuid.UUID) -> bool: ...
    async def revoke(self, *, device_id: uuid.UUID, actor: Principal, reason: str) -> None: ...
    async def rotate_certificate(self, *, device_id: uuid.UUID) -> CertificateBundle: ...
    async def envelope_key(self, device_id: uuid.UUID) -> SecretStr: ...   # governance-only caller
```

`envelope_key` is listed in the Ruff banned-api table (§2.2.1) so only `governance` can reach it. A service that can fetch a signing key is a service that can forge a command.

### 11.3 `projects` — CRUD, import, settings (§1.2)

Routes: `POST /api/v1/projects` (local path or GitHub URL), `GET /api/v1/projects` (search, tag filter, favourites, cursor pagination), `GET|PATCH|DELETE /api/v1/projects/{id}`, `GET /api/v1/projects/{id}/activity`, `PUT /api/v1/projects/{id}/settings`, `POST|DELETE /api/v1/projects/{id}/tags/{tag}`.

Settings are a validated model rather than free-form JSONB, because `settings` drives behaviour:

```python
class ProjectSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)      # Pydantic v2 strict

    llm_budget_usd_month: Decimal = Field(default=Decimal("10"), ge=0, le=10_000)  # FR-06
    embedding_backend: Literal["voyage", "bge_m3"] = "voyage"     # selects the table (D-48)
    favourite: bool = False
    auto_approve_readme_only: bool = False                       # FR-34's named policy
    max_file_size_bytes: int = Field(default=1_048_576, ge=1024, le=16_777_216)  # §1.3 "skip >1MB"
    ignore_globs: list[str] = Field(default_factory=list)
```

`embedding_backend` is immutable once any embedding exists for the project — changing it would mean two vector spaces for one project, and D-48's whole point is that a query reads exactly one table. The API returns `409 project-embedding-backend-locked` and points at re-indexing as the supported path.

**Activity feed (FR-04)** is a projection over `audit_events` filtered to the project, not a second log. One append-only source of truth is the whole point of §1.9; a parallel activity table would drift from it.

### 11.4 `analysis` — index API, incremental patching, readiness (§1.3, §1.4)

#### 11.4.1 Index ingestion

```python
# backend/src/analysis/index_service.py
class IndexService:
    """Owns the Codebase Index: file tree, contents, chunks, embeddings, dep edges.

    Ingestion is idempotent per (project, index_version): the agent may retry a batch
    after a dropped socket without duplicating chunks, because upserts key on
    (file_id, chunk_index) and edge upserts key on (from_file_id, raw_specifier).
    """
    async def replace_full(self, *, project_id: uuid.UUID, batch: FullIndexBatch) -> IndexVersion: ...

    async def patch_incremental(self, *, project_id: uuid.UUID, patch: IndexPatch) -> IndexVersion:
        """Apply an incremental patch under optimistic concurrency.

        patch.base_version must equal the current index version or the call returns
        409 index-version-conflict and the agent re-derives from the current version.
        Without this, two overlapping watch batches could interleave and leave the
        index in a state no full rescan would produce — which is exactly what Q-10
        forbids.
        """
```

#### 11.4.2 Embedding orchestration

Chunks arrive redacted. Embedding runs as a queued `embed.batch` task, batched to the provider's limit, retried with backoff on 429, and written to `embeddings` (1536-d, Voyage Code 3) or `embeddings_local` (1024-d, BGE-M3) according to `projects.settings.embedding_backend`. `model_id` is recorded on every row (D-2's provenance requirement, unchanged). `hnsw.ef_search` is set per query transaction through Phase 0's `with_ef_search`, never baked into the index.

#### 11.4.3 Sparse index for hybrid retrieval (D-49)

Research §C10 calls for "hybrid embedding + BM25 keyword indexing". pgvector has no BM25, and Postgres full-text `ts_rank_cd` is not BM25. Redis Stack — already in the topology for the L2 semantic cache — provides genuine BM25 scoring in `FT.SEARCH`, so the sparse half lives there:

```
FT.CREATE idx:code:<project_id> ON HASH PREFIX 1 code:<project_id>:
  SCHEMA  chunk_id TAG
          path     TEXT WEIGHT 2.0
          symbol   TEXT WEIGHT 3.0
          text     TEXT
          language TAG
```

Search uses the BM25 scorer explicitly rather than the default TF-IDF. Durability: the index is **derived**, never authoritative — it is rebuilt from `file_contents`/`embeddings` by the `index.reindex_sparse` task, and readiness does not depend on it. A missing sparse index degrades retrieval to dense-only with a recorded `retrieval_degraded` flag on the generation run; it never fails a generation. That is the read-path degradation §9's rule permits.

#### 11.4.4 Incremental rescan (D-33, promoting FR-15)

The closure rule is defined in §10.8.4 and proven by **Q-10**. The backend's half is: apply the patch under optimistic concurrency, delete embeddings for vanished `(file_id, chunk_index)` pairs, delete edges whose `from_file_id` was re-parsed and whose specifier no longer appears, re-embed only upserted chunks, and invalidate module summaries for the dirty set plus direct importers.

Cold-start discovery mode (`phases.md` §1.3) is the same machinery with a different entry point: the agent returns a heuristic inventory (languages, manifests, existing config files, obvious entry points) in the first round trip so the UI has something real within a second or two, and the full index arrives asynchronously behind `TaskDispatcher` with `PROGRESS` events. The readiness score is computable from the heuristic inventory alone, marked `partial: true`, and recomputed when full indexing completes — which is what makes the progressive UX honest rather than a spinner.

#### 11.4.5 Readiness scoring (§1.4)

```python
# backend/src/analysis/readiness.py
class Category(StrEnum):
    CONTAINERIZATION = "containerization"; CICD = "cicd"; ORCHESTRATION = "orchestration"
    ENV_CONFIG = "env_config"; SECURITY = "security"; IAC = "iac"

# Weights sum to 100 and are DATA, not literals scattered in code, so a change is a
# reviewable diff. phases.md §1.4 fixes the category list; the weights are a design
# choice recorded here and in config/readiness-weights.yaml.
CATEGORY_WEIGHTS: Mapping[Category, int] = {
    Category.CONTAINERIZATION: 25, Category.CICD: 20, Category.ORCHESTRATION: 20,
    Category.ENV_CONFIG: 15, Category.SECURITY: 15, Category.IAC: 5,
}

@dataclass(frozen=True, slots=True)
class Check:
    id: str                  # stable; the report template and the recommendation key off it
    category: Category
    weight: int              # within its category
    applies_to: Callable[[Inventory], bool]
    evaluate: Callable[[Inventory], CheckResult]

class ReadinessEngine:
    """Deterministic, integer, order-independent scoring. No LLM is involved.

    Three properties make the score trustworthy, all asserted by Q-18:
      - determinism: the same inventory always yields the same score, and
        analysis_reports.inventory_hash records which inventory produced it;
      - order independence: the score does not depend on file iteration order;
      - monotonicity: making a failing check pass never lowers the score.
    Integer arithmetic throughout (no floats) so there is no rounding drift between
    a partial and a full run of the same inventory.

    A check that does not apply is EXCLUDED from its category's denominator rather
    than scored zero — a project with no Terraform should not be punished for having
    no Terraform. That is why `applies_to` exists.
    """
    def score(self, inv: Inventory) -> ReadinessReport: ...
```

The Phase 1 check set covers `phases.md`'s named examples and PRD FR-20: Dockerfile exists / is multi-stage / declares a non-root user / pins a base image digest / has a `HEALTHCHECK`; `.dockerignore` exists; CI workflow exists / runs tests / pins actions by SHA; K8s manifests exist / declare resource requests and limits / declare probes / avoid `latest`; `.env.example` exists / no hardcoded secrets found by the scan; IaC present / state backend configured. Around 30 checks.

**Plain-language "why it matters" (FR-19)** comes from a committed template table keyed by check id — `report_templates.yaml` with `title`, `why_it_matters`, `how_to_fix`, `severity` per check. No LLM writes the report, for the same reason no LLM computes the score: the report is what the user acts on, and it must say the same thing twice for the same input. An LLM may later *rephrase* a report for tone; that is Phase 3's learning-history territory and it is not in this phase.

### 11.5 `generation` — RAG, routing, evaluation, bounded loop, templates (§1.5)

#### 11.5.1 Service shape

```python
class GenerationService:
    async def run(self, *, project_id: uuid.UUID, principal: Principal,
                  kinds: Sequence[ArtifactKind], stream: SSEStream) -> GenerationOutcome:
        """One generation run: retrieve → assemble → generate → validate (≤3) → judge
        → hand to the governance chokepoint. Emits only the six SSE event types.

        This method NEVER writes a file and NEVER contacts the agent hub directly. Its
        terminal act is chokepoint.submit(MutationRequest), which is the only way its
        output can become a change on disk (§2.2).
        """
```

#### 11.5.2 Hybrid retrieval and reranking

```python
class HybridRetriever:
    async def retrieve(self, *, project_id: uuid.UUID, query: str, k: int) -> list[RedactedChunk]:
        """Hybrid sparse-dense retrieval, then 3× over-retrieve and rerank.

        Research §C10 is explicit and is honoured verbatim:
          - sparse: Redis FT.SEARCH with the BM25 scorer over the same chunks;
          - dense: pgvector HNSW cosine with a per-transaction ef_search;
          - fuse: Reciprocal Rank Fusion, RRF(d) = Σ 1/(60 + rank_i(d)), which needs
            no score normalisation between two incomparable scales — the reason RRF
            is preferred here over weighted score fusion;
          - over-retrieve 3×k, then rerank with voyage-rerank-2 and take k.
        Reranking is "the single cheapest quality lever for code retrieval" (§C10) and
        is in the plan from P1, not deferred.

        Degradation is explicit: if the reranker is unavailable the fused order is
        used and the run records retrieval_degraded=True. If the sparse index is
        missing, dense-only, same flag. Retrieval is a READ path, so it degrades
        rather than failing (§9).
        """
```

The RRF constant 60 is the value from the original RRF work and is recorded as a committed constant with a comment, not a magic number; changing it is a reviewable diff.

#### 11.5.3 Structured output under Pydantic v2 strict

```python
class DockerfileSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    base_image: str
    base_image_digest: str | None
    stages: list[BuildStage] = Field(min_length=1)
    workdir: str
    user: str = Field(min_length=1)            # non-root is a schema requirement, not a hope
    exposed_ports: list[int] = Field(default_factory=list)
    healthcheck: HealthCheck | None
    entrypoint: list[str] = Field(min_length=1)

class ArtifactSet(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    dockerfile: DockerfileSpec | None = None
    compose: ComposeSpec | None = None
    k8s: K8sManifestSet | None = None
    workflow: GitHubActionsSpec | None = None
    helm: HelmChartSpec | None = None
    tofu: TofuModuleSpec | None = None
    env_example: EnvExampleSpec | None = None
    docs: DocsSpec | None = None
```

The model returns **structured data, and ForgeOps renders the file** (Research §C11's first technique). That is what makes a schema violation a caught error rather than a malformed Dockerfile, and it is what lets the non-root-user requirement be a type rather than a lint. Transport: JSON-Schema-constrained output where the endpoint supports it, tool-calling where it does not, and a single schema-repair prompt as the last resort — the repair counts against the 3-iteration budget, so it cannot become an unbounded retry loop.

#### 11.5.4 Tier selection

| Artifact work | Tier | Why (`phases.md` §1.5) |
|:---|:---|:---|
| Multi-file architecture, Helm + K8s + Tofu together | `high_coding` (GPT-5.6 Sol → Claude Fable 5) | "architecture, multi-file generation" |
| Single Dockerfile, CI workflow, `.env.example` | `medium` (Grok 4.5 → Claude Sonnet 5 → DeepSeek V4) | "Dockerfile, CI/CD, analysis" |
| Report prose, log summarisation, diff explanation | `low_logs` (Gemini 3 Flash) | "log analysis, formatting" |
| The LLM-as-judge rubric | `medium_value` | Advisory work should not consume flagship budget (**OQ-23**) |
| Air-gapped / sensitive projects | `self_hosted` (GLM-5.2, Qwen3-Coder-Next) | "air-gapped sensitive codebases" |

Every tier now has an available endpoint at every cascade position, because D-42 gave the Anthropic and Google models OpenAI-compatible descriptors. A wiring test asserts `GET /api/v1/ai/tiers` reports no tier whose primary is unavailable.

#### 11.5.5 The evaluation pipeline: deterministic blocking, rubric advisory

```python
# backend/src/generation/gate.py
@dataclass(frozen=True, slots=True)
class GateDecision:
    """The blocking decision. Note what this type does NOT contain: any rubric score.

    Separation is structural, not procedural. decide() accepts only deterministic
    findings, so a non-deterministic judge CANNOT become a safety gate — there is no
    parameter through which its opinion could arrive (Q-09). phases.md §1.5 lists both
    under "evaluation pipeline"; this design assigns them different powers and says so.
    """
    blocked: bool
    blocking_findings: tuple[Finding, ...]

def decide(findings: Sequence[Finding]) -> GateDecision: ...

# backend/src/generation/judge/rubric.py
@dataclass(frozen=True, slots=True)
class Rubric(BaseModel):
    """LLM-as-judge output. ADVISORY. Recorded on generation_runs.rubric, shown to
    the user, never consulted by decide().

    Comparability across runs is engineered rather than hoped for:
      - integer anchors 0..5 with written descriptors per level, not a free scale;
      - temperature 0 and a fixed, versioned prompt (judge_prompt_version);
      - the judging model id recorded, because a score is meaningless without it;
      - the same rubric object shape for every artifact kind, so runs are comparable;
      - a stability probe in CI judges one fixture twice and records the variance —
        reported, not gated, because gating on a stochastic value would be theatre.
    """
    model_config = ConfigDict(extra="forbid", strict=True)
    best_practice: int = Field(ge=0, le=5)
    security_posture: int = Field(ge=0, le=5)
    cost_efficiency: int = Field(ge=0, le=5)
    rationale: dict[str, str]
    judge_model_id: str
    judge_prompt_version: str
```

Deterministic checks (blocking): syntax, JSON-Schema validation, `compose-go` load, K8s server-side dry-run, `tofu validate`/`plan`, Helm lint/template, Trivy config scan, and the Semantic Plan Analyzer's blast-radius verdict. Rubric (advisory): best-practice compliance, security posture, cost efficiency — exactly the three `phases.md` names.

#### 11.5.6 The bounded feedback loop

```python
# backend/src/generation/loop.py
@dataclass(frozen=True, slots=True)
class LoopState:
    attempts_remaining: int          # invariant: 0 <= attempts_remaining <= 3
    findings: tuple[Finding, ...]
    artifacts: ArtifactSet | None

Step = Continue | Accepted | FallbackToTemplate       # a closed union; no other outcome exists

class FeedbackLoop:
    """Termination is structural, not conventional (Q-08).

    _next is the ONLY function that produces a new LoopState, it ALWAYS decrements
    attempts_remaining, and it cannot return Continue at zero because the branch that
    would is unreachable: the zero case returns FallbackToTemplate. There is no
    `while True`, no retry counter that a caller could reset, and no configuration
    that raises the bound (§7.1 types it as Literal[3]).

    NFR-04 targets under 3 iterations on average; 3 is the hard ceiling.
    """
    def _next(self, state: LoopState, decision: GateDecision) -> Step:
        if not decision.blocked:
            return Accepted(state.artifacts)
        if state.attempts_remaining <= 1:               # this attempt was the last
            return FallbackToTemplate(reason="iteration-bound-exhausted")
        return Continue(replace(state, attempts_remaining=state.attempts_remaining - 1,
                                findings=tuple(decision.blocking_findings)))
```

The `<= 1` rather than `<= 0` is deliberate and is the sort of off-by-one that a property catches and a reviewer does not: entering `_next` means an attempt has already been consumed, so one remaining means there is nothing left to spend.

#### 11.5.7 The Safe Default Template Library (§1.5, D-43)

Location: `backend/src/generation/templates/<language>/` with a `manifest.yaml` per language and Jinja-free, parameter-substituting templates (a template engine that can execute expressions inside a security-relevant fallback is an unnecessary risk; substitution is `string.Template`-level).

Eight languages, five artifact classes each, exactly as `phases.md` lists: **Node.js, Python, Go, Rust, Java/Kotlin, Ruby, PHP, .NET** × **Dockerfile, K8s (Deployment + Service + Ingress), GitHub Actions CI, Helm chart, OpenTofu module**.

**What "verified" means, and the only honest definition of it:** every template passes **the same validation pipeline the AI output passes** — the same `SyntaxStage`, `SchemaStage`, `DryRunStage` (including K8s server-side dry-run in the `k8s` job) and `SemanticStage`. The `templates` CI job renders all 40 artifact sets against fixture projects and runs them through the real pipeline; a template that fails is a failing build. "Verified" is not a review sign-off and not a comment in a manifest.

Filling Phase 0's terminal cascade slot without touching the router:

```python
class TemplateLibraryFallback:
    """Occupies the TerminalFallback slot Phase 0 §11.7.3 reserved for exactly this.

    The router is NOT modified: it already calls the terminal slot when every endpoint
    is exhausted, and it already returns RoutingOutcome.EXHAUSTED as data. This class
    turns that outcome into a rendered, verified artifact set for the detected
    language, marks the result served_from="template", and records the reason.

    If no template exists for the detected language, the outcome is
    generation-unavailable — an honest failure, not a wrong-language template.
    """
    async def render(self, *, language: str, kinds: Sequence[ArtifactKind],
                     inventory: Inventory) -> ArtifactSet | None: ...
```

**Q-21** asserts template validity across all 40 combinations; its negative control corrupts one template and requires the property to fail.

### 11.6 `governance` — the chokepoint (§1.10)

```python
# backend/src/governance/chokepoint.py
class GovernanceChokepoint:
    """The single enforced path from an intent to a change on a user's disk.

    Six ordered stages, then a mint. Every stage is an existing, tested component
    where one exists; the value this class adds is that they cannot be skipped and
    cannot be reordered — the order is a literal in one method, not a convention
    spread across callers.
    """

    async def submit(self, req: MutationRequest, *, principal: Principal) -> Submission:
        """Stages, in this exact order (Appendix A.3 gives the pseudocode):

          0. admit           — a verified Principal is required; resolve project and
                               tenant; reject a policy_stale or revoked target device
          1. policy          — OPA governance bundle, fail closed (an OPA outage denies)
          2. approval gate   — ApprovalGate seam; may return REQUIRES_APPROVAL
          3. change-set      — compile change_sets + change_items with pre-image hashes
          4. blast radius    — SemanticPlanAnalyzer, deterministic and monotone (P-11)
          5. audit           — exactly one append-only record, in the SAME transaction
                               as the change-set state transition (Q-04)
          6. rollback handle — reserve the handle row before any envelope is minted

        Only after all six does mint_authority run, and only the minted authority can
        produce a signed envelope. Every early return writes its own audit record, so
        a denial is as auditable as an approval — an audit trail with only successes
        in it is a marketing artifact.
        """

    async def approve(self, *, change_set_id: uuid.UUID, principal: Principal,
                      comment: str | None) -> Submission:
        """Human approval → authority mint → signed envelope → hub. Optimistic
        concurrency on change_sets.version means two concurrent approvals produce
        exactly one winner and one 409 change-set-conflict (Q-22)."""

    async def revert(self, *, change_set_id: uuid.UUID, principal: Principal) -> Submission:
        """A revert is a mutation: it runs the full chokepoint again and mints its own
        authority. Reusing the original authority would make rollback a privileged
        back door."""
```

```python
# backend/src/governance/primitives.py
def mutation_primitive(fn: F) -> F:
    """Marks a function as a mutation primitive.

    Two mechanisms consume this marker:
      - scripts/check-chokepoint.sh AST-walks src/** and fails if a marked function
        is called from outside src/governance/ without a MutationAuthority argument;
      - the §0.4.2 conformance test binds every call site, so a missing authority
        argument fails in milliseconds rather than at runtime.
    """

@mutation_primitive
async def dispatch_apply(authority: MutationAuthority, *, device_id: uuid.UUID,
                         change_set: ChangeSet) -> None: ...
```

**Why a capability argument rather than a check inside the primitive.** A check inside the primitive can be satisfied by a caller that fabricates a context; an argument of a type that cannot be constructed outside `governance/` cannot be satisfied at all. The failure mode moves from "someone forgot to call `assert_authorized()`" to "this does not compile / does not bind" — which is the only kind of enforcement that survives a year of edits by people who have not read this document.

### 11.7 `policies` — CRUD, evaluation, bundle publication (§1.7)

Routes: `GET|POST /api/v1/policies`, `GET|PATCH|DELETE /api/v1/policies/{id}`, `POST /api/v1/policies/{id}/test` (dry-run against a sample input), `GET /api/v1/policies/templates`, `POST /api/v1/policies/publish` (build and activate a bundle).

The three policies `phases.md` §1.7 names, implemented as templates and as real Rego under `policies/agent/`:

```rego
# policies/agent/schedule.rego — "Never deploy on Fridays"
package forgeops.governance.schedule
import rego.v1

default deny_reason := ""

# The window is DATA (project timezone + blocked weekdays), not a literal, so the
# policy is one rule and the project supplies the parameters.
deny_reason := sprintf("blocked window: %s in %s", [day, tz]) if {
    input.operation in {"changeset.apply", "deploy"}
    tz  := input.project.timezone
    day := time.weekday(time.parse_ns("2006-01-02T15:04:05Z07:00", input.now_rfc3339))
    day in input.project.blocked_weekdays
}
```

```rego
# policies/agent/paths.rego — "Never edit package.json"
package forgeops.governance.paths
import rego.v1

default deny_reason := ""

deny_reason := sprintf("protected path: %v", [violations]) if {
    violations := {p | some item in input.change_items
                       p := item.file_path
                       glob.match(input.project.protected_globs[_], ["/"], p)}
    count(violations) > 0
}
```

```rego
# policies/agent/approval.rego — "Require approval for production"
package forgeops.governance.approval
import rego.v1

default require_approval := false

require_approval if input.blast_radius.verdict != "allow"
require_approval if input.environment == "prod"
require_approval if some item in input.change_items; item.action == "delete"
```

Every bundle carries `default allow := false` at its entry document, following **D-25**'s hard-won lesson: an undefined OPA document answers HTTP 200 with no `result`, which is indistinguishable from a working fail-closed policy. The governance client raises `governance-policy-undefined` (503) on an undefined document rather than reading it as a deny — the same treatment D-25 gave the gateway, applied to the new bundle so the identical trap is not re-entered.

```python
class PolicyBundleService:
    """Builds, digests and publishes the bundle both sides evaluate.

    A bundle is a gzip tar of policies/agent/**.rego plus a data document derived
    from the project's policy rows. Its digest is sha256 over a CANONICAL archive
    (sorted paths, fixed mtimes, fixed permissions) so the same inputs always yield
    the same digest — otherwise every publish would look like a drift event.

    Delivery: the bundle travels inside a signed command envelope, so it inherits
    envelope integrity and needs no second signature scheme. The digest is recorded
    in policy_context on every subsequent envelope, and the agent refuses any envelope
    whose policy_context digest differs from the bundle it loaded (Q-07).
    """
    async def build(self, *, project_id: uuid.UUID | None) -> PolicyBundle: ...
    async def publish(self, bundle: PolicyBundle, *, actor: Principal) -> None: ...
    async def active_digest(self, *, project_id: uuid.UUID | None) -> str: ...
```

**Agreement between the two evaluators (Q-06).** Both sides evaluate the same Rego from the same bundle digest against the same input document. The property generates inputs — operations, change-item sets, timestamps across weekday boundaries and timezones, blast-radius verdicts, environments — and asserts the backend's OPA-server decision equals the agent's embedded decision for every one. Its negative control changes one rule in the agent's copy, which must make the property fail. Where they cannot agree (a digest mismatch), both refuse independently (§10.6).

### 11.8 `secrets` — Infisical, redaction, injection (§1.8)

```python
class SecretStore(Protocol):
    """Two implementations: InfisicalStore (default) and LocalSealedStore (dev).

    Neither exposes a read-value API to any route. get_value exists for the injection
    path only and is confined by a banned-api rule to secrets.injection — a vault
    with a reveal endpoint is a distribution channel, not a vault (§11.2).
    """
    async def list_metadata(self, *, project_id: uuid.UUID, environment: str) -> list[SecretMeta]: ...
    async def put(self, *, project_id: uuid.UUID, environment: str, key: str,
                  value: SecretStr, actor: Principal) -> SecretMeta: ...
    async def delete(self, *, project_id: uuid.UUID, environment: str, key: str,
                     actor: Principal) -> None: ...
    async def get_value(self, *, project_id: uuid.UUID, environment: str,
                        key: str) -> SecretStr: ...

class InfisicalStore:
    """Talks to Infisical over its REST API with the shared httpx client.

    No new SDK dependency: the Phase 0 KeyResolver seam already anticipated Infisical,
    the surface needed here is small (auth, list, get, upsert, delete), and adding an
    SDK would add a supply-chain component for four calls. Values are wrapped in
    SecretStr at the boundary and never logged (Phase 0's redaction filter plus D-27).
    """
```

```python
# backend/src/secrets/redaction.py
class Redactor:
    """The backend half of the §7.11 chokepoint, and the ONLY constructor of
    RedactedChunk / RedactedPrompt / RedactedInstruction on this side.

    It runs even though the agent already redacted, for two reasons: defence in depth
    (a project may be imported by a path that never went through the agent), and
    because the backend knows secrets the agent does not — every key in the project's
    vault is redacted by value from retrieved text, catching a credential that Gitleaks'
    entropy rules would have missed.
    """
    async def redact_chunks(self, *, project_id: uuid.UUID,
                            chunks: Sequence[Chunk]) -> list[RedactedChunk]: ...
    def redact_instruction(self, text: str) -> RedactedInstruction: ...
```

**Deploy-time injection (FR-45).** `secrets.inject` is a mutating operation carried by a signed envelope; it materialises values into a process environment for the duration of one command and never writes them to a file, into a change-set, or into a log. The agent holds them in memory only, zeroes the buffers after use, and the audit record names the keys injected — never the values.

### 11.9 `audit` — append-only, hash-chained, complete (§1.9)

```python
# backend/src/audit/writer.py
class AuditWriter:
    """Appends one immutable record. The write joins the CALLER'S transaction, which
    is what makes Q-04's "exactly one record per governance transit" provable: the
    change-set transition and its audit record commit or roll back together, so there
    is no window in which one exists without the other.

    Chain: hash = sha256(canonical(payload) || prev_hash), where prev_hash is the
    previous row's hash for this tenant and canonical() is RFC 8785 JCS over the
    record's semantic fields (seq and hash excluded).

    Writers serialise on a transaction-scoped advisory lock, because a chain is only
    well-defined under serial append. The cost — audit writes for one tenant are
    serial — is acceptable precisely because every write is a governance transit, and
    those are human-paced. It is recorded rather than discovered under load.
    """
    async def append(self, session: AsyncSession, ev: AuditDraft) -> AuditEvent: ...

    async def verify_chain(self, session: AsyncSession, *, tenant_id: uuid.UUID | None,
                           since_seq: int = 0) -> ChainVerification:
        """Recomputes every hash from since_seq and reports the first divergence.
        Exposed as GET /api/v1/audit/verify (admin only) so tamper-evidence is a
        product feature, not an internal helper."""
```

`AuditDraft` requires all six NFR-14 fields — `actor` (who), `action` + `resource` (what), `created_at` (when, server-side), `reason` (why), `before_state`, `after_state` — with `reason` a non-empty string. A required `reason` is what stops the log from becoming a list of verbs.

**Agent-side operations are covered** (`phases.md` §1.9: "Ensure agent-side operations are also logged"). The agent does not write to the audit table — it has no database access, which is a security property worth keeping. Instead every `command.result` and `agent.error` produces an audit record written by the hub with `actor_kind="agent"` and `actor_device_id` set, carrying the agent's reported before/after hashes. **Q-04** counts transits and records to assert the one-to-one relationship in both directions.

**Retention.** Append-only, no delete path, no TTL in Phase 1. Monthly partitioning and an export path are Phase 2 concerns and are recorded as **OQ-30** rather than half-built now. The `audit_events` table is the one place in the schema where unbounded growth is the correct behaviour.

### 11.10 `websocket` — the agent hub (§1.1)

```python
# backend/src/websocket/hub.py
class AgentHub:
    """The backend half of the JSON-RPC session. Deliberately thin: it is a
    transport-and-correlation layer, not a decision-maker.

    What it does: accept an mTLS connection, verify the client certificate against
    the internal CA and the device row, verify the bearer device token, run the
    handshake, keep heartbeats, correlate command.execute → command.result by id,
    fan progress out to SSE, allocate seq via Redis Lua CAS, and check revocation
    PER MESSAGE (Q-16).

    What it must never do: decide whether an operation is allowed, mint authority, or
    sign an envelope. send_command is in the banned-api table so only the chokepoint
    can call it (§2.2.1). A hub that could originate a command would be a second
    mutation path, and the phase's entire premise is that there is exactly one.
    """
    async def serve(self, ws: WebSocket, *, device: AgentDevice) -> None: ...
    async def send_command(self, *, device_id: uuid.UUID, envelope: SignedEnvelope) -> CommandFuture: ...
    async def broadcast_revocation(self, device_id: uuid.UUID) -> None: ...
```

Multi-replica behaviour: a device is connected to exactly one replica, and the chokepoint may run on another. Delivery therefore goes through a Redis stream keyed by device id, which the owning replica consumes — the same "Redis is the shared state, processes are stateless" arrangement Phase 0 used for the MCP gateway. Revocation broadcasts use pub/sub for promptness plus the per-message set membership check for correctness.

### 11.11 SSE streaming

```python
# backend/src/generation/routes.py
@router.post("/api/v1/generation/runs")
async def create_generation_run(...) -> EventSourceResponse:
    """FastAPI native EventSourceResponse (in-tree since 0.139.2). sse-starlette is
    NOT a dependency and must not become one (Research §0).

    Emits exactly the six core/sse.py event types. Contract, asserted by Q-26:
      - STATUS      once at start, and on each phase change
      - TOKEN       zero or more, only while a model streams
      - PROGRESS    zero or more, monotonically non-decreasing percent
      - VALIDATION  once per validation iteration, carrying blocking findings
      - COMPLETE    exactly once on success, carrying change_set_id
      - ERROR       exactly once on failure, carrying an RFC 9457 problem body
    Exactly one of COMPLETE / ERROR terminates every stream, including on client
    disconnect (the generator's finally block emits nothing but releases resources).
    """
```

A disconnected client must not abort the run: the work continues behind `TaskDispatcher` and the frontend recovers state from REST on reconnect. That is why SSE is a view and not the source of truth (§7.5).

### 11.12 Validation pipeline extension

```python
class DryRunStage:
    """Delegates to the agent. Inserted BEFORE SemanticStage, exactly where Phase 0
    §11.9 said it would go. ValidationPipeline.run and the Stage Protocol are
    unchanged — the stage list is data, which is what made this a seam rather than a
    rewrite.

    Every validator runs; the stage does not short-circuit on the first failure,
    because a generation regenerating against one finding at a time would burn its
    3-iteration budget on a file with four problems.
    """
    name = "dryrun"
    async def run(self, doc: PlanDocument, ctx: StageContext) -> list[Finding]: ...
```

If no agent is connected for the project, the stage returns one `Finding(severity=FATAL, blocking=True, code="dryrun_unavailable")`. It does not pass by default: a change-set that was never dry-run must not be presentable as validated. That is the §9 fail-closed rule applied to the mutation path.


---

## 12. Low-Level Design — Frontend

### 12.1 Route structure

Phase 0's shell, providers, RFC 9457 client, error normalisation and store boundaries are consumed unchanged. Phase 1 adds route groups under `(shell)`; the sidebar gains real links for each (Phase 0's "no disabled placeholder links" rule now has real destinations).

```
frontend/app/(shell)/
├── page.tsx                                   # P0 shell landing — unchanged
├── projects/
│   ├── page.tsx                               # §1.2 list: search, tags, favourites
│   └── [projectId]/
│       ├── page.tsx                           # §1.2 detail + activity feed
│       ├── readiness/page.tsx                 # §1.4 score, radar, breakdown
│       ├── generate/page.tsx                  # §1.5 SSE stream, progressive UX
│       ├── changes/
│       │   ├── page.tsx                       # §1.6 history timeline
│       │   └── [changeSetId]/page.tsx         # §1.6 diff, approve, reject
│       ├── policies/page.tsx                  # §1.7 list, editor, violations
│       ├── secrets/page.tsx                   # §1.8 vault UI
│       ├── audit/page.tsx                     # §1.9 audit viewer
│       └── agents/page.tsx                    # §1.1 pairing + device list
└── login/page.tsx                             # §1.11 initiates the OIDC redirect
frontend/features/{projects,readiness,generation,approval,policies,secrets,audit,agents}/
frontend/lib/api/sse.ts                        # §4.5 fetch-based SSE reader
frontend/proxy.ts                              # §1.11 — Next.js 16's middleware successor
```

`proxy.ts` is created now, not in Phase 0. Phase 0 deliberately created neither `middleware.ts` nor `proxy.ts` because it had no request-interception need; Phase 1 has one — redirecting unauthenticated navigation to `/login` and refreshing an expiring session cookie. Next.js 16 replaced `middleware.ts` with `proxy.ts` (Tech-Stack §10), so `proxy.ts` is the correct file and `frontend/__tests__/package-policy.test.ts`'s existing "no `middleware.ts`" assertion continues to hold.

### 12.2 Frontend libraries added — one justification each

Phase 0 deliberately shipped no charting, table, diff or editor library. Each addition below is tied to a specific `phases.md` §1.4 or §1.6 requirement, with the alternative that was rejected.

| Library | Version | Licence | Required by | Why this one, and what was rejected |
|:---|:---|:---|:---|:---|
| `echarts` | 5.x | Apache-2.0 | §1.4 "readiness score display with **radar chart**" | Tech-Stack §14 names Apache ECharts as the charting choice, so this is the authorised library rather than a new decision. Imported through `echarts/core` with only `RadarChart` + `CanvasRenderer` registered, which keeps the added bundle near ~120 KB gzipped instead of the full ~1 MB build. **Rejected:** Recharts and Chart.js — both unsanctioned by any authority, and choosing one would be a decision taken to save a few kilobytes |
| `react-diff-viewer-continued` | 4.x | MIT | §1.6 "diff preview (**side-by-side and unified**)" | Tech-Stack §20 names `react-diff-viewer`; the original is unmaintained and `-continued` is its maintained fork, so this honours the authority without adopting an abandoned package. Supports both view modes out of the box, which is literally the requirement. **Rejected:** hand-rolling a diff renderer (a week of work to reproduce word-level intra-line highlighting badly), and Monaco's diff editor (~2 MB for a read-only view) |
| `@tanstack/react-table` | 8.x | MIT | §1.6 change history, §1.9 audit viewer | Tech-Stack §19 names TanStack Table v8. Headless, so it adds no styling surface and reuses the existing shadcn primitives. Both tables need sorting, cursor pagination and column filtering over datasets that grow without bound. **Rejected:** plain `<table>` (would mean reimplementing sorting and virtualisation twice), and AG Grid (heavyweight, restrictive licence for the enterprise features) |
| `@codemirror/state`, `@codemirror/view`, `@codemirror/commands` | 6.x | MIT | §1.7 "policy list and **editor** UI" | PRD §5 names CodeMirror 6 for the code/diff editor and explicitly prefers it over Monaco unless IDE-grade IntelliSense is needed — editing Rego needs bracket matching, line numbers and undo, not IntelliSense. Three packages, no language mode: Rego has no official CodeMirror grammar, so the editor is plain text with server-side `opa check` validation surfaced as inline problems. Honest and small. **Rejected:** Monaco (~2 MB and a worker for a textarea-plus), and a bare `<textarea>` (no line numbers makes a Rego syntax error genuinely hard to locate) |

Nothing else is added. In particular **no** xterm.js (no log streaming surface in Phase 1), **no** React Flow (no pipeline designer), **no** D2 (OQ-10 unresolved and nothing needs it), **no** ECharts chart types beyond radar, **no** date library (`Intl.DateTimeFormat` covers every format Phase 1 renders), and **no** SSE library (§12.4).

### 12.3 State ownership

Phase 0 §12.4's rule is unchanged and the new surfaces obey it: TanStack Query owns everything server-derived (projects, readiness reports, change-sets, diffs, policies, secret metadata, audit pages, device list); Zustand owns only client-ephemeral UI (diff view mode, expanded categories, sidebar state, the generation panel's collapsed state); the URL owns selection (`projectId`, `changeSetId`, filters as `searchParams`); React Hook Form owns form state.

Streaming state is the one new case and it is resolved rather than left to taste: SSE events are **not** stored in Zustand. Token deltas accumulate in a `useRef` inside the generation feature and are flushed to React state on an animation frame; every other event type triggers a targeted TanStack Query invalidation. That keeps server truth in one place and makes a dropped stream a refetch rather than a divergence.

### 12.4 The SSE client

```typescript
// lib/api/sse.ts
/**
 * Reads a ForgeOps SSE stream over fetch + ReadableStream.
 *
 * Native EventSource cannot send an Authorization header, and the two usual
 * workarounds are both worse than 60 lines of code: a token in the query string
 * lands in access logs and browser history, and a cookie-only scheme would force
 * the whole API onto cookies for one endpoint's benefit. So this reads the stream
 * with fetch, which takes headers, and parses the wire format directly.
 *
 * Only the six event names from core/sse.py are accepted. An unknown event name is
 * dropped with a console warning rather than passed through, so a backend that
 * invents a seventh type fails loudly in development instead of silently in
 * production (Q-26's client-side counterpart).
 */
export type SseEvent =
  | { type: "status";     data: { phase: string } }
  | { type: "token";      data: { text: string } }
  | { type: "progress";   data: { percent: number; message?: string } }
  | { type: "validation"; data: { iteration: number; findings: Finding[] } }
  | { type: "complete";   data: { changeSetId: string } }
  | { type: "error";      data: ProblemDetails };

export async function readSse(
  path: string,
  init: RequestInit,
  onEvent: (e: SseEvent) => void,
  signal: AbortSignal,
): Promise<void>;
```

### 12.5 Accessibility

Phase 0's baseline is inherited (one `<h1>` per route, landmarks, skip link, focus-visible rings, keyboard-operable controls) and extended to the new interactive surfaces, because these are the ones where accessibility is usually lost:

- **Diff viewer:** the diff is rendered inside a `<table>` with row headers and an accessible summary ("4 files changed, 62 additions, 11 deletions"); a screen-reader-only per-hunk description precedes each hunk; the side-by-side/unified toggle is a real radio group; colour is never the only signal — added and removed lines carry `+`/`−` glyphs.
- **Radar chart:** ECharts renders to canvas, which is invisible to assistive technology, so the chart is accompanied by a visually-hidden `<table>` of category scores that is the accessible source of truth. The chart is decorative (`aria-hidden`), the table is real.
- **Approve/reject:** destructive and irreversible-feeling actions are `<button>`s with explicit labels, a confirmation step for apply, and a live region announcing the outcome.
- **SSE progress:** `role="status"` with `aria-live="polite"` for phase changes; token streaming is **not** announced (it would be unusable), and the completion is.

Playwright asserts the skip link, landmark structure, keyboard reachability of approve/reject, the radar chart's accessible table, and focus management when the diff route loads.

### 12.6 The criterion-10 Playwright journey

`frontend/e2e/journey.spec.ts`, run by the `e2e` job against built containers with a real paired agent:

```
 1. Log in through the fixture OIDC issuer (the real Authentik flow is the `auth` job)
 2. Create a project pointing at the fixture Node.js app mounted into the agent container
 3. Mint a pairing code in the UI; the test harness runs `forgeops-agent pair --code …`
 4. Assert the device appears as active and heartbeating
 5. Wait for the readiness score to render, with a radar chart and a category breakdown
 6. Click "Generate Dockerfile + K8s manifests"
 7. Assert SSE events arrive: status → token(s) → validation → complete
 8. Assert the change-set page shows a diff with both view modes working
 9. Approve with a comment
10. Assert the applied state, and assert on the filesystem (through the agent container)
    that the Dockerfile and the three K8s manifests exist with the expected content hashes
11. Assert a backup exists for every pre-existing file that was overwritten
12. Assert the audit viewer lists the full transit: policy, approval, apply — with actors
13. Revert, and assert every file returns to its pre-image byte-for-byte
```

Steps 10, 11 and 13 are what make this an end-to-end test rather than a UI test. A journey that stops at "the UI says applied" would have passed in Phase 0's broken-gateway situation.

---

## 13. Configuration Schemas

**Resolution note, as Phase 0 did:** the version stated in §16 is the authority-mandated floor; exact patch versions for Go/Node tooling, **every container image digest**, and **every GitHub Action commit SHA** are resolved once at implementation time and committed. This document does **not** invent a digest or a SHA — every `@sha256:` below is written as `<committed-digest>`, every action reference as `@<sha>`, and every unresolved patch as `<patch>`. A placeholder that survives into a committed file is a CI failure: `scripts/check-no-latest.sh` fails on `@latest`, and `scripts/check-compose-validate.py` fails on a surviving `<committed-digest>` (§8.4).

### 13.1 `.env.example` additions

Existing Phase 0 variables are unchanged. Additions, grouped:

```dotenv
# ─── Auth (§1.11) ────────────────────────────────────────────────────────────
OIDC_ISSUER="http://localhost:9000/application/o/forgeops/"   # Compose hostname: authentik-server, same port and path
OIDC_APP_AUDIENCE=forgeops-api            # DISTINCT from MCP_OIDC_AUDIENCE by design
OIDC_CLIENT_ID=forgeops-frontend
OIDC_CLIENT_SECRET=change-me-locally
OIDC_REDIRECT_URL=http://localhost:8000/api/v1/auth/callback
SESSION_COOKIE_NAME=forgeops_session
SESSION_TTL_SECONDS=3600
REFRESH_TTL_SECONDS=1209600
CERBOS_URL=http://cerbos:3592
AUTHENTIK_SECRET_KEY=change-me-locally
AUTHENTIK_BOOTSTRAP_PASSWORD=change-me-locally
AUTHENTIK_BOOTSTRAP_TOKEN=change-me-locally

# ─── Agent pairing and envelopes (§1.1) ─────────────────────────────────────
PAIRING_CODE_TTL_SECONDS=300
PAIRING_CODE_MAX_ATTEMPTS=5
PAIRING_CODE_ALPHABET=0123456789ABCDEFGHJKMNPQRSTVWXYZ   # Crockford base32, no I/L/O/U
PAIRING_RATE_LIMIT_PER_IP_PER_MINUTE=10
DEVICE_CERT_TTL_HOURS=24
DEVICE_CERT_RENEW_BEFORE_HOURS=6
ENVELOPE_MAX_AGE_SECONDS=300
ENVELOPE_CLOCK_SKEW_SECONDS=60
ENVELOPE_PEPPER=change-me-locally         # HMAC pepper for code/token storage
INTERNAL_CA_CERT_PEM=""                   # populated by scripts/init-ca.sh; never committed
INTERNAL_CA_KEY_PEM=""                    # explicit "" so a bare `KEY=` cannot match across the newline
HEARTBEAT_INTERVAL_SECONDS=30
HEARTBEAT_TIMEOUT_SECONDS=90

# ─── Analysis and indexing (§1.3) ───────────────────────────────────────────
SCAN_MAX_FILE_SIZE_BYTES=1048576          # phases.md §1.3 "skip binaries >1MB"
SCAN_WATCH_DEBOUNCE_MS=250
SCAN_PARSER_CONCURRENCY=0                 # 0 = min(GOMAXPROCS, 8)
CHUNK_TARGET_TOKENS=512                   # Research §C10
CHUNK_OVERLAP_TOKENS=128                  # Research §C10
SUMMARY_TARGET_TOKENS=1024                # Research §C10
EMBEDDING_BACKEND=voyage                  # voyage | bge_m3  (selects the table, D-48)
EMBEDDING_MODEL_ID_LOCAL=bge-m3
EMBEDDING_DIMS_LOCAL=1024                 # must equal embeddings_local's column
LLM_KEY_VOYAGE=placeholder
VOYAGE_BASE_URL=https://api.voyageai.com/v1
RERANK_MODEL=voyage-rerank-2              # Research §C10, from P1
RETRIEVAL_OVERFETCH_FACTOR=3              # Research §C10 "over-retrieve 3x"
RETRIEVAL_TOP_K=12

# ─── Generation (§1.5) ──────────────────────────────────────────────────────
GENERATION_MAX_ITERATIONS=3               # typed Literal[3]; the config refuses any other value
JUDGE_TIER=medium_value
JUDGE_PROMPT_VERSION=1
TEMPLATE_LIBRARY_PATH=src/generation/templates

# ─── Governance, policy, audit (§1.7 §1.9 §1.10) ────────────────────────────
GOVERNANCE_POLICY_PACKAGE=forgeops/governance
POLICY_BUNDLE_REFRESH_SECONDS=300
APPROVAL_TTL_SECONDS=604800               # a pending change-set expires after 7 days
AUDIT_ADVISORY_LOCK_KEY=forgeops-audit

# ─── Secrets (§1.8) ─────────────────────────────────────────────────────────
SECRET_BACKEND=infisical                  # infisical | local
INFISICAL_URL=http://infisical:8080       # digest-pinned image (debt D5)
INFISICAL_CLIENT_ID=placeholder
INFISICAL_CLIENT_SECRET=placeholder
INFISICAL_PROJECT_ID=placeholder
LOCAL_SECRET_SEAL_KEY=change-me-locally   # AES-256-GCM, SECRET_BACKEND=local only

# ─── Tasks (§7.10) ──────────────────────────────────────────────────────────
TASK_DISPATCHER=arq                       # arq | inline
ARQ_QUEUE_NAME=forgeops
ARQ_MAX_JOBS=10
ARQ_JOB_TIMEOUT_SECONDS=900

# ─── Database pooling (§6.7) ────────────────────────────────────────────────
DATABASE_POOLER_MODE=session              # session | transaction (transaction ⇒ stmt cache 0)
ALEMBIC_DATABASE_URL=postgresql+asyncpg://forgeops_migrator:change-me-locally@postgres:5432/forgeops

# ─── Agent-side (§1.1 §1.3 §1.5) ────────────────────────────────────────────
AGENT_BACKEND_WSS_URL=wss://localhost:8000/api/v1/ws/agent   # no longer empty by default
AGENT_STATE_DIR=
AGENT_CREDENTIAL_STORE=auto               # auto | keychain | file
AGENT_IDENTITY_PROVIDER=paired_device     # paired_device | spiffe_workload
SPIFFE_ENDPOINT_SOCKET=                   # only for spiffe_workload
AGENT_JOURNAL_MAX_BYTES=67108864          # D-41 offline queue bound (NFR-18); 0 disables
AGENT_JOURNAL_MAX_AGE_HOURS=168           # records older than this are dropped on load
AGENT_JOURNAL_DRAIN_BATCH=64              # records per drain round trip
AGENT_TRIVY_BINARY=trivy
AGENT_VALIDATOR_TIMEOUT_SECONDS=120

# ─── Frontend (browser-visible; never a secret) ─────────────────────────────
NEXT_PUBLIC_OIDC_LOGIN_PATH=/api/v1/auth/login
NEXT_PUBLIC_SSE_TIMEOUT_MS=600000
```

`MCP_AGENT_BLAST_RADIUS` **remains in the file but is demoted** (D-39): it is now a development default used only when no attested agent identity is present, and a validator rejects it when `APP_ENV=production`. The Rego is unchanged because it was always written against `input.agent_blast_radius`, which is exactly what OQ-20 anticipated.

### 13.2 `config/model-tiers.yaml` changes

Two endpoint descriptors are added and two existing ones are re-pointed at compatibility surfaces (D-42). The tier map itself does not change, because `phases.md` §1.5 fixes it:

```yaml
endpoints:
  # D-42: reached through the vendor's OpenAI-compatible surface so the cascade can
  # actually get to the models phases.md §1.5 names. The native descriptors below
  # stay, marked unavailable, because they document a real protocol (Phase 0 §5.6's
  # honest-data rule) — they are not stubs.
  claude-fable-5:
    provider: anthropic
    model: claude-fable-5
    protocol: openai_compatible
    base_url: ${ANTHROPIC_OPENAI_BASE_URL}
    key_ref: anthropic
    timeout_seconds: 60
    rank_source: internal_golden
    internal_golden_score: null
  gemini-3-flash:
    provider: google
    model: gemini-3-flash
    protocol: openai_compatible
    base_url: ${GOOGLE_OPENAI_BASE_URL}
    key_ref: google
    timeout_seconds: 60
    rank_source: internal_golden
    internal_golden_score: null
  claude-fable-5-native:            # retained, still unavailable: no codec in Phase 1
    provider: anthropic
    model: claude-fable-5
    protocol: anthropic_native
    base_url: ${ANTHROPIC_BASE_URL}
    key_ref: anthropic
    timeout_seconds: 60
    rank_source: internal_golden
    internal_golden_score: null
```

There is still **no vendor-leaderboard score field** (Phase 0 §11.7.1); `rank_source` and `internal_golden_score` are the only ranking inputs, and the golden dataset that would populate the latter is Phase 2.

### 13.3 `docker-compose.yml` additions

```yaml
services:
  # ── §1.11 — default profile: the API is unusable without an IdP under
  #    deny-by-default, so Authentik is not optional (§2.3).
  authentik-server:
    image: ghcr.io/goauthentik/server:2026.5.<patch>@sha256:<committed-digest>
    command: server
    env_file: *service-env
    environment:
      AUTHENTIK_POSTGRESQL__HOST: postgres
      AUTHENTIK_REDIS__HOST: redis
    ports: ["127.0.0.1:${AUTHENTIK_PORT:-9000}:9000"]
    depends_on:
      postgres: { condition: service_healthy }
      redis:    { condition: service_healthy }
    healthcheck:
      test: ["CMD", "ak", "healthcheck"]
      interval: 10s
      timeout: 5s
      retries: 30

  authentik-worker:
    image: ghcr.io/goauthentik/server:2026.5.<patch>@sha256:<committed-digest>
    command: worker
    env_file: *service-env
    depends_on:
      postgres: { condition: service_healthy }
      redis:    { condition: service_healthy }

  # ── §1.11 — app RBAC sidecar (Tech-Stack §9). NOT embeddable in the agent
  #    (Research §B7); the agent side is Rego (§10.6).
  cerbos:
    image: ghcr.io/cerbos/cerbos:0.54.0@sha256:<committed-digest>
    command: ["server", "--config=/config/cerbos.yaml"]
    volumes: ["./policies/cerbos:/policies:ro", "./config/cerbos:/config:ro"]
    ports: ["127.0.0.1:${CERBOS_HTTP_PORT:-3592}:3592"]
    healthcheck:
      test: ["CMD", "/cerbos", "healthcheck", "--config=/config/cerbos.yaml"]
      interval: 10s
      timeout: 3s
      retries: 10

  # ── debt D5, as corrected by D-51: OPA 1.x publishes no `-rootless` tag, and the
  #    pinned image already runs as USER 1000:1000 on a Chainguard base. The tag stays
  #    `1.4.2`; non-root is proved at runtime in `compose-smoke`, not by a tag substring.
  #    OPA now serves the governance bundle as well as the gateway policy.
  opa:
    image: openpolicyagent/opa:1.4.2@sha256:<committed-digest>
    command: ["run", "--server", "--addr=0.0.0.0:8181", "--log-level=info", "/policies"]

  # ── debt D5: digest-pinned like every other image. §1.8 uses it for real, so it
  #    stays a profile only because EnvKeyResolver remains a valid dev path. The
  #    version is `v0.162.15` per D-52; `v0.91.1` was never published.
  infisical:
    profiles: ["vault"]
    image: infisical/infisical:v0.162.15@sha256:<committed-digest>

  # ── tools profile: gains the external binaries §1.5's validators use when present
  agent-dev:
    profiles: ["tools"]
    build:
      context: ./agent
      target: devtools          # tofu 1.12.5 + trivy + helm, all pinned in the image
```

`scripts/check-compose-validate.py` is extended to fail if **any** image lacks `@sha256:`, if a `<committed-digest>` placeholder survives, or if any service overrides its image's runtime user back to root (`user: root`, `user: "0"`, or any `0:*` form) — turning debt D5 into a gate rather than a note. The *rootless* half of D5 is corrected by **D-51**: a tag-name substring was never evidence of a runtime user, OPA 1.x publishes no such tag, and the runtime proof lives in `compose-smoke`, which asserts `docker compose exec -T opa id -u` is not `0`.

### 13.4 `Makefile` additions

| Target | Contract | Idempotent |
|:---|:---|:---:|
| `init-ca` | Generates a local development CA into `.env` if absent; never overwrites (`init-env` semantics) | yes |
| `pair-dev` | Mints a pairing code through the API and runs the local agent's `pair` against it | no |
| `worker` | Runs the ARQ worker against the local Redis | yes |
| `policy-test` | `opa test policies/ -v` + `opa check --strict policies/` | yes |
| `templates-verify` | Renders all 8 × 5 templates and runs them through the real validation pipeline | yes |
| `mutation` | `scripts/mutation-harness.py --all`; fails on any `VACUOUS` row | yes |
| `k8s-up` / `k8s-down` | Creates/destroys the local kind cluster with the pinned node image | yes |
| `e2e` | Extended to run the criterion-10 journey against built containers | yes |
| `verify-chain` | Calls `GET /api/v1/audit/verify` and reports the first divergence, if any | yes |

`make lint`, `make test` and `make build` keep their Phase 0 contracts and now include the new checks and coverage gates.

---

## 14. Security Considerations

### 14.1 What Phase 1 is responsible for

Phase 0 created foundation attack surface. Phase 1 creates **real** attack surface: a remote-controlled process that writes to a developer's filesystem, an LLM that reads their source, and a secret store. Fourteen controls are in scope.

| Control | Implementation | Authority |
|:---|:---|:---|
| Deny-by-default API authorisation | `require_principal` per route + `check-route-auth.py` derived from the router (Q-19) | phases.md 1.11 |
| Resource-scoped authorisation | Cerbos sidecar; no role logic in handlers | Tech-Stack §9 |
| No secret value readable through the API | `get_value` confined to the injection path by banned-api | NFR-09, §11.2 |
| Outbound-only agent, zero inbound ports | WSS dialled by the agent; pairing is an outbound POST | NFR-12, PRD §2.2 1 |
| Named operations only, never shell | Closed enum + exhaustive dispatch table; no operation takes a command string | PRD §2.2 2, phases.md 1.1 |
| Every mutation carries an `approval_id` | Chokepoint mints it; the agent verifies it independently | PRD §2.2 3, NFR-16 |
| Double policy evaluation | Backend OPA server + agent embedded Rego, both must allow, digest-bound | PRD §2.2 4, phases.md 1.10 |
| Command integrity and replay resistance | JCS canonicalisation + domain separation + HMAC-SHA256; nonce + monotonic seq + expiry | phases.md 1.1, Q-14/Q-15 |
| Immediate revocation | Redis-authoritative set checked **per message**, plus a pub/sub proactive close | Q-16 |
| No long-lived agent keys | ≤24 h device certificate renewed over the live session, or a SPIRE SVID | Research §H31, NFR-08 |
| Secrets redacted before LLM context | Type-enforced chokepoint; store holds only redacted text; cache keyed on redacted prompts | NFR-10, Q-12/Q-13 |
| Backup-before-mutate, atomic change-sets, path confinement | `ApplyVerified` inside the nested-internal boundary; write-intent blocklist | PRD §2.2 6–8, NFR-20/21, Q-01/Q-02 |
| Immutable audit trail | Append-only enforced by revoked DML + triggers; hash-chained; one record per transit | NFR-14, Q-04/Q-05 |
| Per-caller and per-project rate/budget limits | Phase 0's Redis/Lua bucket extended to generation routes plus a project LLM budget | NFR-15, FR-06 |

### 14.2 Network exposure — the Phase 0 warning is now narrower

Phase 0 §14.2 warned that most of the API was unauthenticated and local-only. **That is no longer true, and the change is stated precisely rather than implied:** every route now requires a verified principal except the seven public routes in §4.4. The remaining exposure notes:

- Compose still binds published ports to `127.0.0.1` and `CORS_ALLOW_ORIGINS` still defaults to exactly `http://localhost:3000`.
- The WSS endpoint requires **both** a valid client certificate from the internal CA and a valid device token. Either alone is insufficient.
- **This topology is still development-shaped**, and `docs/deployment.md` continues to say so in its first paragraph. Phase 1 authenticates the API; it does not make the local Compose stack a production deployment. TLS termination, a real IdP configuration, secret management for the CA key, and network policy are Phase 2/3 concerns.
- The internal CA private key is a genuinely new high-value secret. In development it lives in `.env` (git-ignored). It must **never** be committed, and `.gitleaks` plus the mandatory pre-push scan in `.kiro/steering/secret-safety.md` are the gates. Production CA custody is **OQ-31**.

### 14.3 Identity: SPIFFE/SPIRE and the laptop gap, stated honestly

Research §H31 and PRD §5 specify SPIFFE/SPIRE X.509-SVID + mTLS with attestation on **namespace + service-account + image-digest**. Those three attestation inputs are Kubernetes concepts. Phase 1's primary agent runs on a developer's laptop, where none of them exists. Pretending otherwise would be the same category of dishonesty as Phase 0's "419 passing tests".

**The resolution (D-36):** two identity providers behind one seam (§10.2), with clearly different strength claims.

| Deployment | Provider | Attestation basis | Strength claim |
|:---|:---|:---|:---|
| Developer laptop (Phase 1 primary) | `PairedDevice` | Human-in-the-loop pairing: an authenticated user with a project grant issues a one-time code; the agent proves possession of a locally generated key via CSR; the resulting certificate lives ≤24 h and renews over the authenticated session | **No long-lived key**, and possession is bound to an operator action. It is **not** platform attestation, and this document does not claim it is |
| In-cluster agent (Phase 2 target, exercised now) | `SpiffeWorkload` | SPIRE node + workload attestation on namespace + service account + image digest | Full Research §H31 model |

What Phase 1 does about the gap rather than only describing it: the `k8s` CI job (D-28) deploys SPIRE into the kind cluster and runs a real attestation + mTLS handshake through `SpiffeWorkload`, so the cluster path is **implemented and tested** in Phase 1 even though the laptop path is what users get. That is the difference between a designed-for-later seam and an untested aspiration — and it is a concrete benefit of choosing option A for D-28.

`JWT-SVID` is used only where an L7 proxy must be crossed and never as the primary agent credential, per Research §H31's replay warning.

### 14.4 Defence in depth (Research §H29's eight layers) — where each lives

| Layer | Phase 1 location |
|:---|:---|
| L1 input sanitisation | `secretscan` redaction + prompt-injection scanning of retrieved content (§14.7) |
| L2 model-level guards | The generation system prompt constrains output to the artifact schema; a non-conforming response is a schema error, not free text |
| L3 policy engine | OPA governance bundle, backend side, fail closed |
| L4 approval check | `ApprovalGate` → `approvals` row → `approval_id` in the envelope |
| L5 sandbox validation | The `DryRun` stage: compose load, K8s server-side dry-run, tofu plan, helm template, Trivy |
| L6 agent-side enforcement | Envelope verification + embedded Rego + named-operation whitelist |
| L7 audit logging | `audit_events`, append-only and hash-chained |
| L8 rollback | `rollback_handles` + `mutate.Revert`, itself a governed mutation |

All eight exist in Phase 1. That is the first time the full stack is present, and it is why §1.10 is the phase's centre of gravity.

### 14.5 Secret-safety practice in this phase's own code and tests

`.kiro/steering/secret-safety.md` is binding and this phase touches credentials constantly, so the practice is restated as concrete obligations on the implementation:

- Test credentials are **synthetic and self-labelling**, assembled at runtime — e.g. `"test-only-not-a-real-secret-" + uuid4().hex`. No value resembling a real provider token format appears anywhere, including in fixtures, docs and comments.
- JWTs needed by tests are **generated at runtime from a throwaway key pair** created in the test session. No pre-baked signed token is committed. This is doubly important here because the OIDC fixture issuer signs tokens: its key is generated per test run.
- The envelope canonicalisation fixtures use a **synthetic, self-labelling** envelope key, and the fixture files carry a header comment saying so.
- The internal CA key, `ENVELOPE_PEPPER`, `LOCAL_SECRET_SEAL_KEY` and every Authentik bootstrap value exist **only** in the untracked local `.env`; `.env.example` carries placeholders.
- No secret value is echoed into terminal output, a log line, an audit record, an RFC 9457 `detail`, or a commit message. Secrets are referenced by key name.
- The mandatory pre-push gate applies: `gitleaks detect` over the working tree and `gitleaks protect --staged` before any push, plus the high-risk pattern grep. **GitHub secret scanning is disabled on this repository** (OQ-21), so the local gate is the only gate and there is no server-side safety net.

### 14.6 Pairing-code arithmetic

The claim "a 6-character code is safe" needs numbers, not assertion.

Alphabet: Crockford base32 without `I`, `L`, `O`, `U` — 32 symbols, chosen because a user retypes this code and `I`/`1` confusion causes support load. Search space: 32⁶ = 1,073,741,824.

Controls: one live code per project; 5-minute expiry; single use; 5 failed attempts burn the code; 10 exchange attempts per IP per minute; a global exchange bucket; constant-time comparison; only an HMAC of the code stored.

Worst case with 10 live codes across the deployment and one attacker IP: 10 attempts/minute × 5 minutes = 50 guesses against 10 valid codes ⇒ P(success) ≈ 50 × 10 / 1.07×10⁹ ≈ **4.7 × 10⁻⁷**. With a distributed attacker the global bucket, not the per-IP one, is the binding constraint, and it is sized so total attempts across the window cannot exceed 600 ⇒ P ≈ 5.6 × 10⁻⁶. Both are far below the risk of the user reading the code aloud in an open-plan office, which is the actual threat model for a 5-minute human-transcribed code.

What makes this acceptable rather than merely small: a successful guess yields a device token scoped to **one project**, whose every mutation still requires an approval the attacker cannot produce and whose activity is in the audit log. The code is a *bootstrap*, not an authorisation.

### 14.7 Threat notes specific to Phase 1

- **Prompt injection via repository content (Research §H29) is now live**, because retrieved chunks are attacker-influenced whenever a project has any external contributor. Mitigations: retrieved content is delimited and labelled as untrusted data in the system prompt; the model's output is **structured data validated against a schema**, so an injected instruction cannot become a file path or a shell string; the artifact renderer, not the model, decides where bytes land; and every generated change still traverses policy, approval and blast-radius checks. An injection's best case is a rejected change-set with an audit record.
- **Config poisoning** (a malicious `README`, `CLAUDE.md` or `.cursorrules`) is the same vector; the same three mitigations apply, plus those filenames carry no special weight in retrieval.
- **A compromised backend** can ask the agent to do anything the agent's policy bundle allows — which is why the agent evaluates independently and why the bundle is versioned and digest-bound. It cannot forge an envelope without the per-device key, and it cannot mint authority outside `governance/`.
- **A stolen device token alone is insufficient**: the WSS endpoint also requires the client certificate, and the certificate expires in ≤24 h.
- **A stolen database** yields no usable device token or refresh token (both stored as HMACs), no secret values (Infisical holds them), and a tamper-evident audit chain.
- **Rekor/Fulcio dependency at release time** is unchanged from Phase 0, including the recorded workstation limitation that Rekor is unreachable behind a TLS-intercepting network, which is why criterion-16 evidence is produced inside the runner.

---

## 15. Cross-Document Conflict Resolutions

### 15.1 FR-15 incremental scanning: PRD says P2, Tech-Stack says Phase 1

**Conflict.** PRD §3.2 marks FR-15 (incremental scanning) **P2**. `Tech-Stack-Analysis.md`'s performance recommendations say plainly: "Incremental scanning (FR-15) should be prioritized to Phase 1 (currently Phase 2). Full rescans are expensive for medium+ projects." `phases.md` §1.3 then lists "dependency-graph-aware incremental scanning" as a Phase 1 deliverable outright.

**Resolution.** Phase 1, per the authority order: `Tech-Stack-Analysis.md` (level 3) and `phases.md` (level 4, and here the more specific document) both place it in Phase 1, and PRD's P-numbering is a priority marker rather than a phase assignment. Recorded as **D-33**. NFR-01/NFR-02 (analysis under 30 s / 5 minutes) are effectively unreachable for medium projects on repeat scans without it, so the two documents agree on substance.

### 15.2 `phases.md` §1.3 says "Agent: Tree-sitter AST parsing using `github.com/tree-sitter/go-tree-sitter`"

**Conflict.** That module requires cgo. Phase 0's completion criterion — six targets under `CGO_ENABLED=0` — is proven and shipped (`v0.9.1-rc3`'s six real binaries). Both cannot hold.

**Resolution (D-29, owner-decided).** The **capability** is delivered on the agent, as `phases.md` attributes it; the **binding** is wazero-hosted Wasm grammars instead of the cgo module. Deliverable §1.3's requirement — the agent parses ASTs and does cAST chunking — is met in full. The named module is not used, and that is recorded as a deliberate deviation with its reasoning, exactly as D-1 recorded the original deferral. The compounding argument is in D-29: one pure-Go Wasm decision serves §1.3, and §10.6's pure-Go Rego evaluator serves §1.10, so the phase pays the cgo tax zero times instead of twice.

### 15.3 `phases.md` §1.10 says "OPA compiled to **Wasm** embedded in the Go agent"

Resolved in §10.6.1 and recorded as **D-30**: the requirement behind the wording (in-binary, offline, signed, versioned, OPA's own semantics) is met by embedding OPA's Go Rego evaluator; the literal compilation target is not. Recorded as a deviation, reversible behind the `Evaluator` interface.

### 15.4 Auth: Authentik or Keycloak

**Conflict.** `phases.md` §1.11 says "Authentik **or** Keycloak" without choosing.

**Resolution (D-34): Authentik.** Tech-Stack §8 recommends it explicitly — MIT core, the Flow Engine for custom authentication workflows without code, and 2026.5+ fleet-connector capabilities for device-trust signals that §1.1's device flow will want. It notes Keycloak is the mature industry standard but "carries significantly higher complexity", and that ZITADEL's AGPL "may conflict with the project's goals" — which for a project shipping `FSL-1.1-ALv2` and `Apache-2.0` is a real consideration, not a hypothetical. Recorded consequence: enterprise Authentik features (FIPS, Google Workspace) are paid, and Phase 1 uses none of them.

### 15.5 Job queue: PRD names Inngest for Phase 2, Research keeps both open

**Conflict.** PRD §5 lists "Job Queue (Phase 2): **Inngest** (single durable engine)". Research §0/§B6 and the Tech-Stack table say "Temporal, or Inngest if self-host DX wins". `phases.md` Appendix A names Inngest.

**Resolution.** Not a Phase 1 decision, and Phase 1 must not make it accidentally. OQ-16 stays open; ARQ lands behind the unchanged `TaskDispatcher` Protocol (D-32); no engine concept enters the Protocol or any payload. Deciding at P2 with real workloads is better than deciding now from a table.

### 15.6 The unreachable-model inconsistency

Resolved in §1.5 and recorded as **D-42**: Claude Fable 5 and Gemini 3 Flash are reached through OpenAI-compatible surfaces; the native descriptors remain as honest, unavailable data; native codecs and the prompt-caching features they unlock are Phase 2 (**OQ-24**).

### 15.7 `secrets.environment_id` references a Phase 2 table

Resolved in §6.6 and recorded as **D-50**: a constrained `environment TEXT` column in Phase 1, with a deterministic four-value backfill when Phase 2 creates `environments`. No stub table, no unresolvable FK.

### 15.8 Tech-Stack names SQLite + OS keychain for local agent state

**Conflict.** Research §2's table lists "SQLite + OS keychain/credential manager" as local agent state, and **NFR-18 (offline queueing) is marked P1** in PRD §4.3. `phases.md` §1.1 requires reconnect with backoff but says nothing about offline queueing.

**Resolution (D-41).** Two separable questions, resolved differently, because conflating them is what produced the wrong first answer.

*Does NFR-18 land in Phase 1?* **Yes.** A P1 requirement is owned by this phase, and `phases.md`'s silence is a gap in a deliverable checklist rather than a decision to drop it. Phase 1 ships a durable **outbound** journal with **queue-and-revalidate** semantics: the agent journals its own outbound records and *intents*, never an authorisation, and on reconnect every intent is re-submitted through the governance chokepoint, which mints a fresh envelope. Nothing queued is applied without a fresh policy evaluation, approval and blast-radius check (D-41, §10.3, Q-31).

*Does that require SQLite?* **No.** The access pattern is FIFO append-and-drain with no queries, so an append-only 0600 journal file with per-record CRC32C and `fsync` is sufficient. SQLite would add roughly 4 MB to a binary already growing under D-29, plus a parser on the attack surface, in exchange for indexing nothing reads. Research §2's intent — durable local state that survives a restart — is honoured; its named mechanism is not, and that is recorded as a deliberate deviation rather than left as an omission. Credentials remain in the OS keychain with the documented 0600-file fallback (OQ-26).

**Consequence.** No P1 requirement is deferred by this design, and §16.1 still contains no `modernc.org/sqlite`.

### 15.9 The Phase 0 design's pin table vs the repository's committed pins

**Conflict.** Phase 0 §16.2 lists `pydantic==2.10.6`, `pydantic-settings==2.7.1`, `orjson==3.10.15`, `pyjwt==2.10.1`, `pip-tools==7.4.1`. The committed `backend/pyproject.toml` has `2.11.0`, `2.9.1`, `3.11.6`, `2.13.0`, `7.6.0`. Similarly `agent/go.mod` carries `coder/websocket v1.8.12`, `docker v28.5.2+incompatible`, `go-git/v5 v5.19.1`, `client-go v0.31.4`, `cobra v1.8.1` — all at or above the design's floors.

**Resolution.** The committed lockfiles win, because they are what CI installs and what the hash-pinned locks verify (§0.1). `pip-tools` at 7.6.0 is already recorded as D-21 with its reason (incompatibility between 7.4.1 and current pip). The rest are patch/minor advances within the design's stated floors, which §16's preamble always allowed. **Phase 1 must not downgrade any of them**, and §16.2 lists the actual values so a later reader does not "fix" the code to match a stale table. Where this document repeats an anchor version, it repeats the committed one.

### 15.10 `ci.yml`'s header comment claims an `e2e` job that does not exist

**Resolution.** Phase 1 creates the job (§8.3.2) rather than editing the comment, and adds `scripts/check-ci-jobs.py`. Invocation: `python scripts/check-ci-jobs.py .github/workflows/ci.yml .kiro/specs/phase-1-mvp-core/design.md`. Input: the workflow's `jobs:` keys and every backtick-quoted job name inside this document's Appendix E. Failure condition: exit `1` naming any job Appendix E cites that the workflow does not define, and exit `1` if the extracted set is empty. Phase 0's Appendix E cited `build`, `test` and `lint` jobs that never existed; that class of error is now a build failure rather than a documentation drift.

---

## 16. Dependencies and Version Pinning

**Preamble, in Phase 0's terms:** the version stated is the authority-mandated floor or the exact pin where one is mandated. Exact patch versions for Go tooling and Node packages, and **every container image digest**, are resolved once at implementation time and committed to the appropriate lockfile. **No digest is invented in this document.** A `<committed-digest>` or `<patch>` placeholder that survives into a committed file fails `pre-commit` (§8.4). No floating range, and no `@latest`, anywhere.

### 16.1 Go agent — new dependencies

| Dependency | Version | Licence | Authority | Phase 1 role |
|:---|:---|:---|:---|:---|
| `github.com/tetratelabs/wazero` | `v1.9.x` | Apache-2.0 | **D-29** (no authority names a Wasm runtime) | Pure-Go Wasm runtime hosting tree-sitter grammars; preserves `CGO_ENABLED=0` |
| tree-sitter grammar `.wasm` artifacts (12) | per `grammars.lock.json` | MIT / Apache-2.0 per grammar | phases.md §1.3 (AST parsing), D-29 | The parsers themselves. Vendored, digest-pinned, SBOM-listed (§8.6), verified at load |
| `github.com/open-policy-agent/opa` | `v1.4.2` | Apache-2.0 | phases.md §1.10, Research §B7, **D-30** | In-process Rego evaluation for the agent half of double policy evaluation. Same version as the OPA server image, so both sides share semantics |
| `github.com/compose-spec/compose-go/v2` | `v2.4.x` | Apache-2.0 | phases.md §1.5 (`docker compose config`) | In-process compose validation; keeps the single-static-binary property |
| `github.com/santhosh-tekuri/jsonschema/v6` | `v6.0.x` | Apache-2.0 | phases.md §1.5 (YAML + JSON Schema) | Schema validation of manifests and workflows |
| `helm.sh/helm/v3` | `v3.16.x` | Apache-2.0 | PRD §5 "Helm SDK (Go)" | `helm lint` and `helm template --validate` in-process |
| `sigs.k8s.io/yaml` | `v1.4.x` | Apache-2.0 + BSD-3 | phases.md §1.5 | YAML↔JSON for schema validation and manifest parsing |
| `github.com/zricethezav/gitleaks/v8` | `v8.30.1` | MIT | phases.md §1.8, Research §F20 | Secret detection during analysis. Same version already used via the pinned Docker image in CI, so local and CI detection agree |
| `github.com/zalando/go-keyring` | `v0.2.x` | MIT | Research §2 "OS keychain/credential manager" | Device credential storage; 0600-file fallback when unavailable |
| `github.com/bradleyfalzon/ghinstallation/v2` | `v2.12.x` | Apache-2.0 | PRD §6 (GitHub App), **D-38** resolving OQ-7 | `AppInstallationTokenSource` behind Phase 0's `TokenSource` seam |
| `github.com/spiffe/go-spiffe/v2` | `v2.4.x` | Apache-2.0 | phases.md §1.10, Research §H31 | X.509-SVID fetch + mTLS for the cluster identity provider |
| `golang.org/x/sys` | exact tag resolved at implementation and committed to `go.sum` | BSD-3 | **D-37** resolving OQ-6 | Windows Job Objects; pure Go |
| `golang.org/x/crypto` | exact tag resolved at implementation and committed to `go.sum` | BSD-3 | §7.6, §7.11 | HKDF for per-purpose key derivation |

Existing Go pins are unchanged and are **not** downgraded to the Phase 0 design's floors (§15.9): `coder/websocket v1.8.12`, `docker v28.5.2+incompatible`, `k8s.io/client-go v0.31.4`, `go-git/v5 v5.19.1`, `google/go-github/v68 v68.0.0`, `mark3labs/mcp-go v0.15.0`, `zap v1.27.0`, `cobra v1.8.1`, `fsnotify v1.7.0`, `x/sync v0.22.0`, `rapid v1.1.0`, `minio/selfupdate v0.6.0`, `sergi/go-diff` at its pinned pseudo-version.

New tooling module `agent/tools/go.mod` (fixes debt D4), so every tool is checksum-verified through `go.sum` rather than resolved from a mutable tag:

| Tool | Version |
|:---|:---|
| `github.com/golangci/golangci-lint/cmd/golangci-lint` | `v1.62.2` |
| `golang.org/x/vuln/cmd/govulncheck` | `v1.1.4` |

### 16.2 Backend — new dependencies

Existing pins as **actually committed** (§15.9), unchanged by Phase 1: `fastapi==0.139.2`, `uvicorn[standard]==0.34.0`, `sqlmodel==0.0.39`, `sqlalchemy[asyncio]==2.0.38`, `asyncpg==0.30.0`, `pgvector==0.3.6`, `alembic==1.14.1`, `pydantic==2.11.0`, `pydantic-settings==2.9.1`, `redis==5.2.1`, `httpx==0.27.2`, `orjson==3.11.6`, `pyjwt[crypto]==2.13.0`; dev `pytest==8.3.4`, `pytest-asyncio==0.25.3`, `pytest-cov==6.0.0`, `hypothesis==6.125.3`, `ruff==0.9.7`, `pip-audit==2.7.3`, `pip-tools==7.6.0`.

| Dependency | Exact pin | Licence | Authority | Phase 1 role |
|:---|:---|:---|:---|:---|
| `arq` | `==0.26.3` | MIT | Research §0/§B6, Tech-Stack §4, **D-32** | The P1 async task runner behind `TaskDispatcher`. Import confined to `core/tasks.py` |
| ~~`cerbos`~~ | ~~`==0.14.0`~~ | — | **D-55** | **Dropped.** The sidecar is reached over its versioned HTTP API with the already-pinned `httpx`; the SDK's metadata makes `grpcio-tools`, `protobuf` and `grpcio-status` runtime requirements of the API image for one JSON POST, and §11.1 injects the shared client, which is not the SDK's constructor |
| `pyyaml` | `==6.0.2` | MIT | §11.5.7 templates, §13.2 tier config | Declared explicitly — `REVIEW-PHASE-0.md` recorded it as an undeclared transitive dependency, which is a supply-chain defect regardless of it working |
| `cryptography` | `==49.0.0` | Apache-2.0 / BSD | §3.1 internal CA, §11.8 local sealing, **D-62** | Already present transitively via `pyjwt[crypto]`; promoted to a direct pin because Phase 1 uses it directly for X.509 issuance and AES-256-GCM. **The pin is `49.0.0`, not the `44.0.0` this table first stated** — §15.9's resolution is that the committed lockfiles win and Phase 1 must not downgrade any of them, and `pyjwt[crypto]==2.13.0` already resolves 49.0.0, so `44.0.0` would be either unsatisfiable or a downgrade. Promoted in leaf 7.5, with the lock regenerated |
| `rfc8785` | `==0.1.4` | Apache-2.0 | §7.6, §11.9 | JCS canonicalisation for envelope signing and the audit hash chain. A hand-rolled canonicaliser is exactly the component that must not diverge between two runtimes |
| `pytest-cov` threshold config | — | — | **D-31** | `--cov-fail-under=70` in `addopts`; no new package |

Deliberately **not** added, with reasons: no `authlib` (the OIDC code+PKCE flow is ~80 lines over `httpx` + `pyjwt`, and an auth library is a large surface for a small need); no Infisical SDK (five REST calls over the shared `httpx` client, §11.8); no Voyage SDK (two HTTP endpoints); no `rank-bm25` (BM25 lives in Redis, D-49); no `tree-sitter` wheels (D-29 puts parsing on the agent); no `deepeval`, `langfuse`, `opentelemetry-*`, `celery`, `dramatiq`, `temporalio`, `inngest`, `sse-starlette`, `langchain`, `langgraph`, `llama-index`.

On LangGraph and LlamaIndex specifically, since Research §C10 recommends both: Phase 1's generation flow is a bounded three-iteration loop with one model call per iteration and a deterministic validator set. That is a `for` loop with a closed union return type, and expressing it as a graph framework would add a large dependency, a second control-flow model and a new failure surface to replace fifteen lines whose termination is currently provable (Q-08). Indexing likewise is chunk-and-embed with the agent doing the parsing, which LlamaIndex would wrap rather than simplify. Both are recorded as **OQ-32** for the phase where multi-agent orchestration genuinely arrives — this is a deliberate deviation from a Research recommendation, not an oversight.

### 16.3 Frontend — new dependencies

| Dependency | Version | Licence | Authority | Role |
|:---|:---|:---|:---|:---|
| `echarts` | `5.6.x` | Apache-2.0 | Tech-Stack §14 | §1.4 radar chart, tree-shaken to `RadarChart` + `CanvasRenderer` |
| `react-diff-viewer-continued` | `4.0.x` | MIT | Tech-Stack §20 | §1.6 side-by-side and unified diff |
| `@tanstack/react-table` | `8.21.x` | MIT | Tech-Stack §19 | §1.6 change history, §1.9 audit viewer |
| `@codemirror/state`, `@codemirror/view`, `@codemirror/commands` | `6.x` | MIT | PRD §5 | §1.7 policy editor |

Existing frontend pins (Next.js 16, React 19, Tailwind v4, TanStack Query 6.x, Zustand 5.x, React Hook Form + Zod, Playwright 1.50+, vitest 2.x, shadcn/Radix, `next-themes`, ESLint, Prettier, k6) are unchanged. `frontend/__tests__/package-policy.test.ts` already asserts every dependency is exact-pinned; the four additions inherit that assertion.

### 16.4 Container images and external tools

| Image / tool | Version | Notes |
|:---|:---|:---|
| `ghcr.io/goauthentik/server` | `2026.5.<patch>` + digest | §1.11 IdP (D-34); server + worker share the image |
| `ghcr.io/cerbos/cerbos` | `0.54.0` + digest | Exact version per Tech-Stack §9 / PRD §5 |
| `openpolicyagent/opa` | `1.4.2-rootless` + digest | **Rootless**, as Phase 0 §13.3 specified (debt D5) |
| `infisical/infisical` | `v0.91.1` + digest | **Now digest-pinned** (debt D5) |
| `kindest/node` | `v1.32.<patch>` + digest | `k8s` job only (D-28) |
| `kind` | `v0.27.x` | Installed via a SHA-pinned action |
| `kubectl` | `v1.32.x` | `k8s` job; the agent uses `client-go`, not the binary |
| `helm` | `3.16.x` | `agent-dev` image and CI; the agent uses the SDK |
| `aquasecurity/trivy` | `0.58.x` + digest | `agent-dev` image; optional at runtime |
| SPIRE server / agent | `1.11.x` + digest | `k8s` job attestation harness only |
| tree-sitter CLI (grammar build container) | pinned + digest | Only if a grammar has no prebuilt `.wasm` (OQ-29) |
| `pgvector/pgvector:pg17`, `redis/redis-stack-server:7.4.0-v3`, OpenTofu `1.12.5`, Docker Compose `2.24.7`, `hashicorp/null 3.2.3` | unchanged | Inherited from Phase 0 |

### 16.5 The Wasm grammar artifacts as supply-chain components

`grammars.lock.json` is a first-class lockfile and is treated as one: it is checked by `lock-integrity`, its `-diff` attribute is not set (§8.5), and each entry carries `name`, `version`, `sha256`, `licence`, `source_url` and a `purl`. `scripts/sbom-merge.py` turns each entry into a CycloneDX component so the signed SBOM covers 100 % of what ships in the binary. The agent verifies each digest at load time (Q-25), so a tampered binary fails closed rather than parsing incorrectly.

### 16.6 Licence posture

Every new Go dependency is Apache-2.0, MIT or BSD, so linking into the Apache-2.0 `agent/` subtree raises no obligation beyond attribution in `agent/NOTICE` — which the Phase 0 release process already audits and which gains rows for the new dependencies and for each grammar's licence. Every new Python and Node dependency is Apache-2.0 or MIT, compatible with the root `FSL-1.1-ALv2`. No GPL/AGPL library is linked: k6 and `golangci-lint` remain external tools, and Cerbos, OPA, Authentik and Infisical are separate services reached over the network, not linked code.


---

## 17. Decisions and Open Questions

### 17.1 Decision log (continuing at D-28)

Each entry is an architecture decision with its rationale preserved, because later phases will ask "why is it like this?". Decisions that change an inherited Phase 0 contract name what they supersede, per §0.1.

---

#### D-28 — Add a `kind`-based Kubernetes CI job during Phase 1

- **Status:** Accepted · **Date:** 2026-07-30 · **Decided by:** project owner (`parag8487`)
- **Context.** Phase 1 needs no cluster of its own, but §1.5 requires `kubectl --dry-run=server` validation, which needs an API server; §1.10's SPIFFE/SPIRE attestation is defined on namespace + service-account + image-digest, which are cluster concepts; and Phases 2–3 are gated on a cluster. The alternative was deferring entirely to Phase 2, which would degrade K8s validation to client-side plus JSON-Schema checking and leave SPIRE attestation as a laptop-shaped approximation.
- **Decision.** Add a `k8s` CI job in Phase 1 using `kind` `v0.27.x` with a digest-pinned `kindest/node:v1.32.x`. It runs server-side dry-run validation for generated and template manifests, `helm template --validate`, the SPIRE attestation harness, and the backend integration tests marked `kubernetes`.
- **Rationale.** Server-side dry-run is not a stricter version of client-side validation; it is a *different* check. It exercises admission webhooks, server-side defaulting and field pruning, `apiVersion` availability, CRD presence and quota conflicts — none of which schema validation can see. Criterion 4 ("generated files pass validation") would otherwise rest on shape-only checking, and Phase 0's review is the standing evidence for what happens when evidence measures something adjacent to the claim. Wiring the cluster now is also materially cheaper than mid-phase, and it makes §1.10's identity story testable rather than aspirational (§14.3).
- **Consequences.** §8.3.1 (job definition), §10.7 (validators needing a live server), §13.4 (`make k8s-up`/`k8s-down`), §14.3 (SPIRE exercised for real), §16.4 (`kind`, `kubectl`, `kindest/node`, SPIRE images pinned), Appendix E criteria 4 and 10 may name the `k8s` job. Cost accepted: ~2–4 minutes per matching run, two more pinned tools, one image set, and a job class that can flake on cluster readiness — mitigated by `kind`'s own wait plus a bounded poll, and by not gating documentation-only changes on it.
- **If overridden to Option B (defer):** `k8s` disappears from §8.3, `DryRunStage`'s K8s validator degrades to client-side + schema with `dryrun_degraded` recorded on every run, Appendix E criteria 4 and 10 must name only `agent`/`e2e`, §14.3's SPIRE row becomes untested design, and `helm template --validate` becomes plain `helm template`.

---

#### D-29 — tree-sitter runs as WebAssembly under `tetratelabs/wazero`; `CGO_ENABLED=0` is preserved

- **Status:** Accepted · **Date:** 2026-07-30 · **Decided by:** project owner (`parag8487`) · **Supersedes:** D-1's *guard mechanism* (not D-1's constraint)
- **Context.** `phases.md` §1.3 names `github.com/tree-sitter/go-tree-sitter`, which requires cgo. Phase 0's release criterion is six targets under `CGO_ENABLED=0`, already proven with six real published binaries for `v0.0.1-rc3`, and enforced executably by `agent/internal/app/deps_test.go` plus `scripts/check-go-module.sh`. §1.10 additionally requires OPA policy evaluation embedded in the agent, whose usual Wasm host (`wasmtime-go`) is also cgo. Four options were presented: (A) CGO on with a cross-toolchain, (B) Wasm grammars in a pure-Go runtime, (C) move parsing to the backend, (D) split the release into a static agent plus a cgo analyzer for Linux only.
- **Decision.** Option **B**. Tree-sitter grammars are compiled to WebAssembly, vendored under `agent/internal/scanner/grammars/`, digest-pinned in `grammars.lock.json`, embedded with `go:embed`, and executed by `tetratelabs/wazero` — a pure-Go runtime. `agent/.goreleaser.yaml` is not modified: six targets, `CGO_ENABLED=0`, `-trimpath`, `mod_timestamp` all hold.
- **Rationale.** One decision serves two deliverables. Options A and D pay the cgo tax twice — once for the parser and once for the policy engine — while B pays it zero times, since §1.10's evaluator is pure Go as well (D-30). B is also the only option that leaves the entire Phase 0 custody chain untouched: A would make `-trimpath`/`mod_timestamp` reproducibility depend on a C toolchain and would need an osxcross-style SDK for the darwin leg, the least reproducible part of any cross-build; D would produce two artifact classes to sign, SBOM and attest, and would cost Windows and macOS their AST fidelity — two of the three platforms NFR-22/23/24 call fully supported; C would deviate from `phases.md`'s explicit "Agent:" attribution and would ship more source content over the wire, weakening the air-gapped story that the `self_hosted` tier exists to serve.
- **Costs accepted, stated plainly.** Grammar `.wasm` files become vendored, pinned, checksummed supply-chain inputs that must appear in the SBOM (§8.6) — the first non-Go artifacts ForgeOps ships. Parse throughput is lower than native cgo bindings (acceptable: NFR-01/02 are minutes-scale and parsing is fan-out parallel). The binary grows ~25–45 MB (§8.2, OQ-27). Not every grammar publishes a prebuilt `.wasm`, so some must be built by a digest-pinned container with a reproducibility check — the phase's largest single execution risk, recorded as **OQ-29** with a degrade-to-line-chunking fallback per language.
- **What the D-1 guard becomes.** `deps_test.go` stops asserting "tree-sitter is absent from `go.mod`" — that assertion would now be misleading, since the capability is present. It becomes: (a) no dependency in the module graph requires cgo, checked by a `CGO_ENABLED=0` build plus a known-cgo-module denylist; (b) every `grammars.lock.json` entry matches the embedded bytes. `scripts/check-go-module.sh` is updated in the same commit so the two guards cannot disagree. **D-1's constraint — a pure-static six-target build — is preserved, not reversed.**
- **CI job that proves it.** `agent` (build for all six `GOOS/GOARCH` pairs with `CGO_ENABLED=0`; wazero load + parse tests; digest verification; corrupted-blob fail-closed test) and `supply` (SBOM contains every grammar component).
- **If overridden to A, C or D:** §8.2's matrix, §10.8.2, §16.1, the guard, and Appendix E criterion 7's evidence all change; D additionally requires `agent doctor` to report which analysis mode is active.

---

#### D-30 — The agent's policy engine is OPA's Go Rego evaluator, in-process, not Rego-compiled-to-Wasm

- **Status:** Accepted · **Date:** 2026-07-30 · **Decided by:** design (deliberate deviation, owner-reviewable) · **Deviates from:** `phases.md` §1.10 and Research §B7's literal wording
- **Context.** Both documents say "OPA compiled to **Wasm** embedded in the Go agent". Hosting compiled Rego needs a Wasm host; the mainstream Go host is cgo, which D-29 exists to avoid. wazero could host it, but compiling Rego to Wasm loses builtins and adds a build artifact that itself needs signing, versioning and verification.
- **Decision.** Embed `github.com/open-policy-agent/opa/rego` and evaluate the signed, versioned bundle in-process, with no Wasm and no cgo.
- **Rationale.** The requirement behind the wording is satisfied in full: policy is evaluated **inside the agent binary**, **offline**, **from a signed and versioned bundle**, by **the OPA project's own evaluator**, at the same OPA version as the server side — so both halves of the double evaluation share semantics, which is the property Q-06 asserts. The literal compilation target buys nothing here and costs builtins plus an artifact.
- **Consequences.** §5.5, §10.6, §10.6.1, §16.1. Recorded as a deviation rather than a claim of compliance. Reversible: the `policy.Evaluator` interface absorbs a Wasm implementation without touching a call site, which is why it is an interface.

---

#### D-31 — Test coverage is a per-component gate at ≥70 %

- **Status:** Accepted · **Date:** 2026-07-30 · **Retires:** OQ-17
- **Context.** `phases.md` 0.3 called >70 % a goal; Phase 1's completion criteria list "Test coverage ≥ 70%" as a criterion. Phase 0 treated it as a reported goal because a hard gate on scaffolding rewards test theatre.
- **Decision.** A **gate**, enforced per component: backend `--cov-fail-under=70` with branch coverage; agent `scripts/check-coverage.sh 70` over `./internal/...`; frontend vitest thresholds at 70 for lines, functions and branches. Not aggregated.
- **Rationale.** Aggregation would let a well-covered backend hide an untested agent — the component that writes to a user's disk. Per-component is the only reading of "≥70 %" that cannot be gamed by volume.
- **Consequences.** §7.13, §8.3, Appendix E criterion 11. Stated alongside it, because the number alone is misleading: Phase 0's broken gateway was *covered*. Coverage bounds the untested surface; §0.4 bounds the falsely tested surface. Both gates run and neither is described as sufficient.

---

#### D-32 — ARQ is the Phase 1 task runner, behind the unchanged `TaskDispatcher` seam

- **Status:** Accepted · **Date:** 2026-07-30 · **Does not retire:** OQ-16
- **Context.** Research §0 and `phases.md` allow ARQ or Dramatiq at P1, with exactly one durable engine at P2 behind an orchestrator-agnostic interface. PRD §5 names Inngest for Phase 2; Research and Tech-Stack keep Temporal and Inngest both open.
- **Decision.** **ARQ** `==0.26.3`, constructed in the lifespan, with the `TaskDispatcher` Protocol, `TaskHandle`, `_TASK_HANDLERS` registry and `@register_task` decorator all unchanged. The `arq` import stays confined to `core/tasks.py` by the existing Ruff banned-api rule. `InlineDispatcher` remains for tests and development.
- **Rationale.** Phase 1's queued work is async-I/O bound (index batches, embeddings, rerank, generation), so an asyncio-native runner lets a job body reuse the same `httpx`/`redis`/SQLAlchemy async clients the request path uses. Dramatiq would introduce a second concurrency model into one codebase and, in its best-supported configuration, a second broker service. Honest cost: ARQ has a smaller community, fewer middlewares and no admin UI.
- **Boundary preserved.** No engine concept enters the Protocol — no workflow id, signal, query, activity or run id — and no Phase 1 payload is shaped as a workflow with history. **Temporal and Inngest both stay open (OQ-16)**; the P2 move remains a one-module change. Celery stays banned permanently.
- **Consequences.** §4.6, §7.10, §11.1, §13.1, §16.2.

---

#### D-33 — Dependency-graph-aware incremental scanning lands in Phase 1

- **Status:** Accepted · **Date:** 2026-07-30 · **Resolves:** §15.1
- **Context.** PRD §3.2 marks FR-15 as P2; `Tech-Stack-Analysis.md` says explicitly it should be prioritised into Phase 1 because full rescans are expensive for medium projects; `phases.md` §1.3 lists it as a Phase 1 deliverable.
- **Decision.** Implemented in Phase 1, with correctness defined as index-equality with a full rescan (Q-10) and the closure rule stated exactly (§10.8.4).
- **Rationale.** Two of the three authorities place it here, and NFR-01/NFR-02 are unreachable on repeat scans without it. Defining correctness as equality with a full rescan is what makes it safe to ship: an incremental index that is merely "usually right" silently degrades every downstream generation.
- **Consequences.** §3.3, §10.8.4, §10.8.5, §11.4.4, Q-10, Q-11.

---

#### D-34 — Authentik is the identity provider

- **Status:** Accepted · **Date:** 2026-07-30 · **Resolves:** `phases.md` §1.11's "Authentik or Keycloak"
- **Decision.** Authentik `2026.5+` (server + worker), default Compose profile, sharing the existing Postgres and Redis.
- **Rationale.** Tech-Stack §8 recommends it: MIT core, the Flow Engine for custom authentication workflows without code, and 2026.5+ fleet connectors for device-trust signals that §1.1's device flow will want. Keycloak is called the mature standard but "significantly heavier"; ZITADEL's AGPL "may conflict with the project's goals", which matters for a project shipping FSL and Apache code.
- **Consequences.** §2.3 (default profile), §11.2, §13.1, §13.3, §16.4, the new `auth` CI job. Recorded cost: Authentik's enterprise features are paid and Phase 1 uses none; its cold start is slow enough that the `e2e` job uses a fixture issuer instead (OQ-28).

---

#### D-35 — `TenantContextMiddleware` fills middleware row 6; RLS and `NOT NULL` stay in Phase 2

- **Status:** Accepted · **Date:** 2026-07-30 · **Partially resolves:** OQ-15
- **Context.** Phase 0 reserved middleware row 6 and left `tenant_id` nullable with no RLS policies. Research §0 fixes RLS over schema-per-tenant with PgBouncer transaction-mode pooling in mind.
- **Decision.** Fill row 6: resolve the tenant from the verified principal into a `contextvar`, and issue `SET LOCAL app.tenant_id` inside the transaction from the session dependency. Add `DATABASE_POOLER_MODE`, which sets asyncpg `statement_cache_size=0` in transaction mode. **Do not** create RLS policies and **do not** set `tenant_id NOT NULL`. Every new table carries the same nullable seam.
- **Rationale.** Filling the row now means Phase 2 turns policies on rather than retrofitting a request-scoped variable through every route. Not enabling policies now is the honest half: a single-tenant deployment cannot exercise an RLS policy, and an unexercised security control is worse than an absent one — Phase 0's review is the evidence for that claim.
- **Consequences.** §4.3, §6.7, §7.12, §13.1. An integration test asserts the variable is visible inside the transaction and **absent in the next transaction on the same pooled connection**, which is what actually proves `SET LOCAL` rather than `SET`.

---

#### D-36 — Two identity providers behind one seam; the laptop path is not called attestation

- **Status:** Accepted · **Date:** 2026-07-30
- **Context.** Research §H31 specifies SPIFFE/SPIRE X.509-SVID with attestation on namespace + service-account + image-digest. Those are Kubernetes concepts; Phase 1's primary agent runs on a developer laptop where none exists.
- **Decision.** One `identity.Provider` interface with `PairedDevice` (default: locally generated key, CSR signed at pairing, certificate ≤24 h renewed over the live session) and `SpiffeWorkload` (cluster: X.509-SVID via the Workload API). The cluster path is implemented and exercised in the `k8s` job even though laptops use the paired path.
- **Rationale.** Both satisfy "no long-lived agent keys" (NFR-08, Research §H31). Only the cluster path is platform attestation, and this document says so rather than blurring the two. Implementing and testing the cluster path now — possible because of D-28 — means Phase 2's in-cluster agent inherits working code instead of a diagram.
- **Consequences.** §10.2, §14.3, §16.1, the `k8s` job's SPIRE harness. `JWT-SVID` is used only across L7 proxies, never as the primary credential.

---

#### D-37 — Windows process-tree termination uses Job Objects

- **Status:** Accepted · **Date:** 2026-07-30 · **Retires:** OQ-6
- **Context.** Phase 0 shipped `taskkill /PID <pid> /T /F` and recorded Job Objects as a Phase 1 hardening item. `taskkill /T` can miss a re-parented provider plugin, which then holds a state lock.
- **Decision.** Create a Job Object with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`, assign the child to it, and terminate by closing the handle after a graceful attempt. `golang.org/x/sys/windows` provides the syscalls with no cgo, so §8.2's matrix is unaffected.
- **Consequences.** §10.11, §16.1. Proven by an integration test that spawns a detached grandchild and asserts both processes are gone — the case `taskkill /T` misses.

---

#### D-38 — `AppInstallationTokenSource` lands in Phase 1

- **Status:** Accepted · **Date:** 2026-07-30 · **Retires:** OQ-7
- **Context.** PRD §6 mandates a GitHub App with short-lived installation tokens. Phase 0 shipped `EnvTokenSource` behind the `TokenSource` seam and D-5 promised the swap would not reach call sites.
- **Decision.** Add `AppInstallationTokenSource` using `bradleyfalzon/ghinstallation/v2`, selected by configuration, with `EnvTokenSource` retained for local development. The `TokenSource` interface is unchanged.
- **Rationale.** §1.2 imports repositories and §1.6 can open PRs, so the PAT's blast radius (a user's entire account) is no longer acceptable. An installation token is scoped to the installation and expires in an hour.
- **Consequences.** §1.4 seam table, §16.1. D-5's claim is discharged: no call site changes.

---

#### D-39 — Blast radius is derived from attested agent identity, not from an environment variable

- **Status:** Accepted · **Date:** 2026-07-30 · **Retires:** OQ-20 · **Supersedes:** Phase 0 §13.1's `MCP_AGENT_BLAST_RADIUS` as a production source
- **Context.** Phase 0's gateway filtered tools by `input.agent_blast_radius`, sourced from `MCP_AGENT_BLAST_RADIUS` because agents did not exist. OQ-20 anticipated exactly this replacement and had the Rego written against the input field so no policy change would be needed.
- **Decision.** `Principal.blast_radius` is derived from the verified identity: for a user principal from the role, for a device principal from the device's project grant and its attestation kind. The env var remains only as a development default when no attested identity is present, and a validator rejects it when `APP_ENV=production`.
- **Rationale.** A radius that an operator can widen with an environment variable is not a control. OQ-20's foresight means the Rego is untouched and the gateway's 27 policy tests keep passing.
- **Consequences.** §1.4 seam table, §11.2, §13.1, Q-30.

---

#### D-40 — `teams` and `team_members` are deferred to Phase 2

- **Status:** Accepted · **Date:** 2026-07-30
- **Context.** PRD D1 lists both. FR-07 (multi-user project sharing with role-based access) is P2, and `phases.md` §1.11 asks only for the three roles.
- **Decision.** Not created in Phase 1. `users.role` plus Cerbos resource policies cover the three roles; project membership is expressed by ownership until teams arrive.
- **Rationale.** Creating empty tables would make Phase 1's migration numbering claim ownership of Phase 2 work, and §1.3 forbids structural placeholders that carry no Phase 1 behaviour. No Phase 1 deliverable reads them.
- **Consequences.** §6.1, §6.9.

---

#### D-41 — NFR-18 is delivered in Phase 1 by a durable outbound journal with queue-and-revalidate; still no SQLite

- **Status:** Accepted · **Date:** 2026-07-30 · **Delivers:** **NFR-18 (PRD §4.3, priority P1)** · **Supersedes:** the deferral this design originally proposed
- **Context.** NFR-18 — "Agent queues operations when offline, executes when reconnected" — is marked **P1** in PRD §4.3, so Phase 1 is the phase that owns it. `phases.md` §1.1 requires reconnect with exponential backoff and is silent on queueing; that silence is a gap in a deliverable checklist, **not** a decision to drop a priority-1 requirement. Research §2 additionally lists "SQLite + OS keychain/credential manager" as local agent state. An earlier draft of this document deferred the whole requirement to Phase 2 to avoid adding SQLite. That was the wrong trade, and the analysis below is why.
- **What an offline queue must survive — analysed before deciding, because each item could have made queueing unsafe rather than merely awkward:**
  1. **Envelope expiry.** A command envelope carries `not_after` bounded by `ENVELOPE_MAX_AGE_SECONDS` (default 300 s, §7.6). Queueing an **inbound** mutation envelope is therefore architecturally dead: it expires long before any realistic reconnect, and extending its lifetime so it could survive an outage would widen exactly the replay window that bound exists to close.
  2. **Replay protection.** `seq` is a strictly monotonic per-device counter allocated by a Redis Lua compare-and-set on the backend (§7.6), and the nonce set is backend-side. An offline agent cannot allocate a `seq` and cannot manufacture a deliverable envelope — and must not be given a way to.
  3. **Revocation during the outage.** A device token, a certificate or an `approval_id` may be revoked while the agent is offline. Draining a queue before revalidating would execute the intent of a principal that no longer has authority.
  4. **Policy freshness.** The policy bundle may change during the outage. Applying a queued mutation against the bundle the agent held when it disconnected would defeat Q-07 and the double-evaluation invariant.
- **Decision — queue-and-revalidate.** Phase 1 ships a durable **outbound** journal in `internal/session`, and nothing queued is ever applied without a fresh chokepoint transit.
  - **What is journalled:** the agent's own outbound records only — completed scan batches, `command.result` and `command.progress` for work that finished before the disconnect, `agent.status`, secret-scan finding *metadata*, and **intents** (a record that the operator asked for a change while offline, to be re-submitted for evaluation on reconnect).
  - **What is never journalled:** a signed `command.execute` envelope, an `approval.response`, a `MutationAuthority`, an `approval_id`, a device token, an envelope key, or any secret value. **Nothing that authorises a mutation is written to disk**, which is what makes items 1–4 above moot rather than mitigated.
  - **Storage.** An append-only journal file at mode `0600` under `AGENT_STATE_DIR`: length-prefixed records, CRC32C per record, `fsync` on append, bounded by `AGENT_JOURNAL_MAX_BYTES` and `AGENT_JOURNAL_MAX_AGE_HOURS`, truncated after a successful drain, and a corrupt tail record discarded on load with a warning rather than failing startup. **No SQL engine, and therefore no new dependency:** the access pattern is FIFO append-and-drain with no queries, so SQLite would buy indexing nobody uses at the cost of roughly 4 MB of binary (already growing under D-29) and a new parser on the attack surface. Research §2's "SQLite" is honoured in substance — durable local state — and deviated from in letter; that deviation is recorded here rather than left implicit.
  - **Delivery semantics.** At-least-once with idempotent apply. Every record carries an agent-generated `record_id`; the backend deduplicates with `SETNX record:<device>:<record_id>` before acting, so a redelivered batch is a no-op rather than a duplicate index write.
  - **Revalidation on reconnect, in this exact order.** (1) mTLS + device token + per-message revocation check; **if the device is revoked the journal is discarded and wiped with the credentials, never drained.** (2) Bundle-digest check; a stale digest blocks the drain of intents until the bundle reloads (`policy-bundle-stale`). (3) Non-mutating records are delivered. (4) Every intent is replayed as an `approval.request` — an existing agent → backend method (§7.3), so **no tenth JSON-RPC method is added** — and the backend routes it into `GovernanceChokepoint.submit`, which re-runs policy → approval gate → change-set compilation → blast radius → audit → rollback handle and mints a **fresh** envelope with a fresh `approval_id`, `policy_context` digest, nonce, `seq` and `not_after`. Pre-image hashes are re-checked at apply time, so a file edited during the outage yields `change-set-conflict` rather than a stale overwrite.
- **Rationale.** This is the smallest scope that satisfies NFR-18 without weakening a single invariant, and it is strictly better for the project than deferring. The user-visible promise — work offline and the agent catches up — is delivered, while the security property that matters — nothing is applied without a fresh chokepoint transit — holds *by construction*, because the journal physically cannot hold an authorisation. The original deferral would have dropped a P1 requirement in order to avoid a dependency this design does not need anyway.
- **Consequences.** §1.2 (the excluded row narrows to offline *mutation execution*, not queueing), §2.4 (`internal/session/journal.go`), §7.3 (intents ride the existing `approval.request`), §7.4 (the drain runs only after a successful `session.connect`), §10.3 (`Journal` interface), §13.1 (three new variables), §15.8, §16.1 (still no `modernc.org/sqlite`), Appendix C.2 (`journal-*` codes), **Q-31**, Appendix E criterion 1.
- **Reversal cost if you prefer the deferral after all.** NFR-18 moves to Phase 2 as an explicit P1 deviation, §1.2 regains the broad excluded row, `Q-31` and `internal/session/journal.go` disappear, three environment variables and the `journal-*` error codes go with them, and Phase 2 inherits both the queue and the revalidation design above — which is the expensive half, so deferring saves less than it appears to.

---

#### D-42 — Claude Fable 5 and Gemini 3 Flash are reached through OpenAI-compatible surfaces

- **Status:** Accepted · **Date:** 2026-07-30 · **Resolves:** the §1.5 / Phase 0 §5.6 inconsistency
- **Context.** Phase 0 honestly marked `anthropic_native` and `google_native` endpoints unavailable, yet `phases.md` §1.5 names Claude Fable 5 as the high-tier backup and Gemini 3 Flash as the low tier. The cascade could not reach two of its own named models.
- **Decision.** Add `openai_compatible` descriptors pointing at each vendor's compatibility base URL, executed by the existing `OpenAICompatibleEndpoint` with no new adapter. Keep the native descriptors, still marked unavailable, as honest data (Phase 0 §5.6's rule).
- **Rationale.** Every tier gets an available endpoint at every cascade position without writing two codecs in the highest-risk phase. Cost, stated rather than hidden: the compatibility layers do not expose Anthropic `cache_control` prompt caching, extended thinking, or Google context caching — cost-optimisation features deferred with the native codecs to Phase 2 (**OQ-24**). Nothing in Phase 1's scope needs them: generation needs chat completion with JSON-Schema-constrained output, which both layers provide.
- **Consequences.** §1.5, §13.2, §15.6, §16.2 (no vendor SDKs). A wiring test asserts no tier has an unavailable primary.

---

#### D-43 — `TemplateLibraryFallback` fills Phase 0's terminal cascade slot

- **Status:** Accepted · **Date:** 2026-07-30
- **Context.** Phase 0 §11.7.3 reserved the terminal cascade slot with `TerminalFallback` returning `RoutingOutcome.EXHAUSTED`, explicitly for the Phase 1 Safe Default Template Library.
- **Decision.** Insert `TemplateLibraryFallback` at that slot. **The router is not modified.** When no template exists for the detected language the outcome is `generation-unavailable` — an honest failure rather than a wrong-language template.
- **Rationale.** This is what the seam was for, and it is the cheapest possible discharge of a Phase 0 promise. "Verified" is defined as passing the same validation pipeline the AI output passes, run in the `templates` CI job — the only honest definition.
- **Consequences.** §11.5.7, §8.3, Q-21, Appendix E criterion 4.

---

#### D-44 — The semantic cache accepts only `RedactedPrompt`

- **Status:** Accepted · **Date:** 2026-07-30 · **Extends:** Phase 0 §11.8
- **Decision.** `TieredSemanticCache.lookup` and `.store` take `RedactedPrompt`, not `str`. Layer precedence (L1 → L2 → L3), the 0.95 similarity threshold and the resilience/staleness behaviour are unchanged.
- **Rationale.** Two NFR-10 clauses become mechanical rather than procedural: a cache key can never be computed over unredacted text, and a cached completion is unreachable from an unredacted prompt, because no code path can produce the key. Q-13's negative control widens the signature back to `str` and must make the property fail.
- **Consequences.** §7.11, §11.5, Q-13.

---

#### D-45 — `fileops.ApplyAtomic` is superseded by `mutate.ApplyVerified` inside a nested-internal boundary

- **Status:** Accepted · **Date:** 2026-07-30 · **Supersedes:** Phase 0 §10.10's exported `fileops.Ops.ApplyAtomic`
- **Context.** §1.10 requires that no agent mutation bypass the governance chokepoint. An exported write function that any package can call is a bypass waiting to be written.
- **Decision.** Move the write implementation into `agent/internal/executor/internal/mutate`, importable only from within `internal/executor/**` by Go's nested-internal rule. The exported entry point becomes `ApplyVerified(ctx, *envelope.Verified, root, entries)` (spelling fixed by D-59). `fileops.UnifiedDiff` and the path-validation helpers stay exported and unchanged.
- **Rationale.** The **algorithm is preserved exactly** — validate paths, back up before mutate, temp + fsync + rename, roll back in reverse on any error — so P-08 continues to guard it. What changes is the argument: a mutation without a verified envelope is now a compile error. Phase 1 additionally requires an expected pre-image hash per entry, so a stale change-set aborts with `ErrConflict` instead of overwriting newer work.
- **Consequences.** §2.2.1, §10.1, §10.5, Q-01, Q-02, Q-03. Callers outside the executor subtree do not compile — the strongest available enforcement.

---

#### D-46 — The `fileops` path blocklist splits into read-intent and write-intent rules

- **Status:** Accepted · **Date:** 2026-07-30 · **Extends:** Phase 0 §10.10
- **Context.** The Phase 0 rule blocks `.env` and any path whose basename starts with `.env.`, which wrongly blocks `.env.example` — a committed placeholder-only file that `scripts/init-env.sh` copies from and that `phases.md` §1.5 lists as a **generated artifact**. `REVIEW-PHASE-0.md` recorded it as a P2 defect. The rule is right in intent and one case too wide.
- **Decision.** `blockedForRead` keeps the Phase 0 strictness exactly. `blockedForWrite` is identical except that three exact names are permitted: `.env.example`, `.env.sample`, `.env.template`.
- **Rationale.** Reading a real `.env` into an LLM prompt and writing a placeholder template are opposite acts that one rule conflated. The allowance is a closed list of three names, not a pattern — `.env.production.example.bak` must stay blocked, and a glob would let it through.
- **Consequences.** §7.11(f), Q-01. A test enumerates `.env`, `.env.local`, `.env.production`, `.env.example`, `.env.example.bak`, `.envrc` and `sub/.env` across both intents.

---

#### D-47 — The agent's MCP server gains only non-mutating tools

- **Status:** Accepted · **Date:** 2026-07-30 · **Extends:** Phase 0 §10.8
- **Decision.** Phase 1 adds `agent.scan.status`, `agent.validate.*` and `agent.readiness.inventory`. No mutating tool is added, in this phase or by this seam.
- **Rationale.** MCP is a tool-access surface reached through the gateway; making it a mutation surface would create a second write path and defeat §1.10 in a single commit. Structurally it already cannot: the write path requires a `*envelope.Verified`, which only `envelope.Verify` can produce from a signed envelope (D-59).
- **Consequences.** §1.4 seam table, §7.3, §7.7.

---

#### D-48 — Self-hosted 1024-d embeddings get their own table; `embeddings` stays 1536-d

- **Status:** Accepted · **Date:** 2026-07-30 · **Resolves:** D-2's deferred Phase 1 follow-up
- **Context.** D-2 fixed `embeddings.embedding` at `vector(1536)` for Voyage Code 3, kept `model_id` as provenance, and deferred the multi-model strategy to Phase 1 — either a second table per dimension or Matryoshka truncation to a common size.
- **Decision.** A second table, `embeddings_local` with `vector(1024)` and its own HNSW cosine index at the same `m=16, ef_construction=64`. A project reads exactly one table, selected by `projects.settings.embedding_backend`, which is immutable once embeddings exist.
- **Rationale.** Matryoshka truncation is unavailable: BGE-M3 is not Matryoshka-trained, so truncating 1024-d vectors would silently degrade recall, and padding to 1536 is meaningless. Two tables keep both vector spaces exact and make mixing impossible, because no query references both. `model_id` remains `NOT NULL` on both, so provenance survives.
- **Consequences.** §6.1, §6.3, §6.4, §6.5 (`0003`), §11.3, §11.4.2, §13.1. Changing a project's backend requires a re-index and returns `409 project-embedding-backend-locked` otherwise.

---

#### D-49 — BM25 sparse retrieval lives in Redis Stack; fusion is RRF

- **Status:** Accepted · **Date:** 2026-07-30
- **Context.** Research §C10 calls for "hybrid embedding + BM25 keyword indexing". pgvector has no BM25, and Postgres full-text `ts_rank_cd` is not BM25 — claiming otherwise would be a quiet substitution of a different algorithm.
- **Decision.** The sparse index is a Redis Stack `FT.SEARCH` index over the same chunks, using the BM25 scorer explicitly. Dense and sparse result lists are fused with Reciprocal Rank Fusion (`k=60`), then over-retrieved 3× and reranked with `voyage-rerank-2`.
- **Rationale.** Redis Stack is already in the topology for the L2 semantic cache, so genuine BM25 costs no new service. RRF fuses two incomparable score scales without normalisation, which is why it is preferred here over weighted score fusion. The index is derived and rebuildable from `file_contents`, so it is not a durability concern; its absence degrades retrieval to dense-only with a recorded flag rather than failing a generation.
- **Consequences.** §4.1, §11.4.3, §11.5.2, §13.1.

---

#### D-50 — `secrets.environment` is constrained text in Phase 1; the FK arrives with Phase 2's `environments`

- **Status:** Accepted · **Date:** 2026-07-30 · **Resolves:** the dangling PRD D5 reference
- **Context.** PRD D5 defines `secrets(..., environment_id, ...)`, but `environments` belongs to D4, which is Phase 2 (multi-environment management is excluded from Phase 1 verbatim).
- **Decision.** `environment TEXT NOT NULL` constrained to `dev|test|staging|prod`, unique on `(project_id, environment, key)`, no FK. Phase 2's migration adds `environment_id`, backfills by matching the four text values to the newly created rows, then drops the text column.
- **Rationale.** A nullable FK to a non-existent table is a broken reference, and a stub `environments` table would violate §1.3's no-stub rule while claiming Phase 2 work under Phase 1's migration numbering. Constraining the text to exactly the four names Phase 2 will create makes the backfill deterministic.
- **Consequences.** §6.1, §6.3, §6.6, §6.5 (`0006`), §11.8. Also: exactly one of `infisical_path` and `encrypted_value` is non-null, enforced by a check constraint, so the database is not a second copy of every secret when Infisical is the backend.

---

#### D-51 — Debt D5's "OPA is not rootless" premise was false; non-root is proved at runtime, not by a tag substring

- **Status:** Accepted · **Date:** 2026-07-30 · **Supersedes:** the OPA half of §0.5 debt row D5 and the §13.3 OPA image reference
- **Context.** §0.5 D5 and §13.3 both required `openpolicyagent/opa:1.4.2-rootless`, inheriting the string from Phase 0 §13.3. Implementing the row exposed two facts. First, **that tag does not exist**: OPA 1.x publishes `1.4.2`, `1.4.2-static`, `1.4.2-debug`, `1.4.2-envoy*` and `1.4.2-istio*`, and `docker manifest inspect openpolicyagent/opa:1.4.2-rootless` returns `no such manifest`. The `-rootless` variant belonged to OPA 0.x and was dropped when 1.0 made the default image non-root. Second, the image the repository already pins — `openpolicyagent/opa:1.4.2@sha256:35a093d9ae828373cf88f68ecaa8189ab26287468074a3b78f0601d9c8b7a4f5` — reports `Config.User == "1000:1000"` with `org.opencontainers.image.vendor == "Chainguard"` and a `glibc-dynamic` base. **The security intent D5 asserted was unmet was already met.**
- **Decision.** The OPA tag stays `1.4.2`, digest-pinned as it is. `scripts/check-compose-validate.py` drops the `-rootless` suffix assertion, which was never evidence of anything, and gains a rule that no service may override its image's runtime user back to root. `compose-smoke` gains the assertion that actually proves the property: `docker compose exec -T opa id -u` must not return `0` on the running container.
- **Rationale.** A gate that pattern-matches a tag name proves a naming convention, not a runtime user; had the tag existed, a `-rootless` image reconfigured with `user: root` in Compose would have passed. It is also the exact failure class this phase exists to eliminate — Phase 0 shipped 419 green tests over a gateway that could not serve a request (D-23), and a check asserting a substring of an unpullable reference is the same category error. Keeping the requirement as written would have been worse than dropping it: `docker compose up` would fail on an image that cannot be resolved, so the design would have been unimplementable rather than merely unproven.
- **Consequences.** §0.5 D5, §13.3, §16.4, task 2.5. Debt D5's Infisical half is unaffected and still stands: that image is now digest-pinned (see D-52). Reversal cost is nil — if OPA ever publishes a `-rootless` 1.x tag, the runtime assertion continues to hold and the tag can change without touching the gate.

---

#### D-52 — Infisical is pinned at `v0.162.15`; the integration assumptions were re-verified at that version before §11.8 was written

- **Status:** Accepted · **Date:** 2026-07-30 · **Supersedes:** the `v0.91.1` reference in §13.3
- **Context.** §13.3 named `infisical/infisical:v0.91.1`. That tag was never published, so the digest D5 demanded could not be resolved for it. The available line moves to `v0.162.15` — roughly seventy minor releases, not a patch bump — and §11.8 had not yet been implemented, so the integration assumptions could still be checked at pin time rather than discovered at integration time. A digest-pinned image whose API moved underneath the design is worse than an unpinned one, because it converts a review-time question into a runtime failure.
- **Decision.** Pin `infisical/infisical:v0.162.15@sha256:bcb31ccb2a3784315ad9d9e180a9c53c6423f0daf3087c3bb158093de99617f2` and keep §11.8's shape: no SDK, five REST calls over the shared `httpx` client, Universal Auth with a machine-identity `clientId`/`clientSecret`. §11.8's secret CRUD calls are written against the **v4** secrets API (`/api/v4/secrets`, `/api/v4/secrets/{secretName}`, keyed by `projectId`) rather than the v3 raw API (`/api/v3/secrets/raw`, keyed by `workspaceId`), which the vendor's own reference has moved under `endpoints/deprecated/`.
- **Rationale.** What was checked, and what it found: the **self-hosted deployment shape is unchanged** — one `infisical/infisical` container, external Postgres and Redis, no separate standalone image, and the server env contract (`ENCRYPTION_KEY`, `AUTH_SECRET`, `SITE_URL`, `DB_CONNECTION_URI`, `REDIS_URL`) keeps its names; **port 8080 is unchanged**; **schema migrations now run automatically at boot** (since v0.111.0, where v0.91.1 required them to be run by hand — a change in our favour); **the auth model is unchanged and is now the only supported one** — Universal Auth at `/api/v1/auth/universal-auth/login` with `clientId` + `clientSecret`, with legacy service tokens deprecated, so the design was already on the surviving path; and **the Python SDK question is unchanged** — `infisical-python` is deprecated, `infisicalsdk` replaces it, and §11.8 depends on neither. One thing did move: the **v3 raw-secrets endpoints are now labelled legacy** and superseded by v4. They still respond at v0.162.15, so writing v3 would have worked and then rotted; writing v4 costs nothing today because the code does not exist yet.
- **Consequences.** §11.8, §13.1, §13.3, tasks 2.5 and 10.4. §13.1 gains the Infisical **server-side** variables, which were absent because Phase 0 only ever needed the client half; the `vault` profile gains a `/api/status` healthcheck and `service_healthy` conditions on Postgres and Redis. Task 10.4's integration test must provision an organisation, project and machine identity on first boot, because a fresh Infisical has no machine identity to authenticate as. Reversal cost is one line in `docker-compose.yml` plus the v4→v3 path change, and both are behind the `SecretStore` seam.

---

#### D-53 — Appendix C.1 registered no problem type for an identity-provider outage, which §6.3 requires to be distinguishable

- **Status:** Accepted · **Date:** 2026-07-30 · **Extends:** Appendix C.1
- **Context.** §6.3 is explicit that Authentik is kept out of `/health/ready` because "an IdP outage must degrade login, not readiness of authenticated traffic". Implementing §11.2's `/login` and `/refresh` (task 6.2) made the gap concrete: degrading *login* requires login to answer something a client can act on, and Appendix C.1 registers no type for it. Every registered candidate is wrong. `unauthenticated` (401) asserts a fact about the caller's credential that is not in evidence — the caller may hold a perfectly good refresh token and the server simply cannot reach the issuer — and a frontend that treats 401 as "log in again" would send the user into a redirect loop through the very IdP that is down. `not-ready` is not in the registry at all; it is hand-built by `/health/ready`, and reusing it would say the service is unready, which §6.3 specifically forbids. `secret-store-unavailable` and `validator-unavailable` name different subsystems. `core.errors.ProblemException` accepts an unregistered suffix without objecting, so the alternative to registering one was to invent a type at the raise site — which is the exact practice the registry exists to prevent, and which `test_errors_phase1.py` would have caught only if the type were also added to the registry.
- **Decision.** Register `idp-unavailable` → 503. Raised at `/api/v1/auth/login`, `/callback` and `/refresh` when the discovery document cannot be read, is not an object, is missing an endpoint, or declares an issuer other than the configured one, and when the token endpoint is unreachable at transport level. A token-endpoint *rejection* (any 4xx) stays `unauthenticated`: that is a statement about the grant, not about availability. Readiness is untouched — Appendix C.1 gains one row and `/health/ready` still probes only Postgres and Redis.
- **Rationale.** The premise that proved wrong is that Appendix C.1 was complete for Phase 1's error surface. It was assembled per subsystem — pairing, envelopes, policy, approval, generation, secrets, indexing, audit, validation, tenancy — and login was the one subsystem whose *unavailability* case was never given a row, because §11.2 describes the happy path and the 401s while the availability requirement lives in §6.3. Conflating an outage with a credential failure is the same category error as D-25's undefined-document handling: two conditions with different remedies collapsed into one response, so the client cannot pick the right one. The distinction is load-bearing for a caller — 503 means retry with backoff and keep the session, 401 means discard the credential and re-authenticate — which is precisely what makes it worth a type rather than a detail string.
- **Consequences.** Appendix C.1, §11.2, §6.3, task 6.2. `PROBLEM_REGISTRY` gains one entry; `test_errors_phase1.py` parses Appendix C.1 from this document, so the row and the code cannot drift. Reversal cost is one registry line and three raise sites. No status of an existing type changes, so no client contract is broken.


#### D-54 — The `auth` job drives Authentik's flow-executor API headlessly; the real code+PKCE flow needs no browser

- **Status:** Accepted · **Date:** 2026-07-31 · **Implements:** §8.3's `auth` job and OQ-28's resolution, without amending either
- **Context.** Task 6.3 requires the `auth` job to exercise "the real code+PKCE flow", and OQ-28 resolves the `e2e`/`auth` split as "`auth` covers the real code+PKCE flow and the RBAC matrix; `e2e` covers the product journey". Everything before the browser leg was provable and is proved: provisioning through Authentik's own API, discovery satisfying `OidcMetadata.from_document`'s exact-issuer equality, a real JWKS yielding an RS256 key `IdTokenVerifier` fetches, and Authentik answering the authorization URL `OidcClient` builds with its login flow rather than `error=`. The leg that failed was reaching an **authenticated session**, from which `/authorize` mints a code. The working premise had become that this leg is browser-only, so the choices on the table were Playwright inside the `auth` job, deferral to `e2e`, or minting a code out of band.
- **Decision.** None of the three. `POST /api/v3/flows/executor/{flow_slug}/` is driven directly over `httpx` with a cookie jar: `GET` returns the `ak-stage-identification` challenge, `POST {"uid_field": ...}` advances to `ak-stage-password`, `POST {"password": ...}` returns `xak-flow-redirect`, and the resulting `authentik_session` cookie is an authenticated session. Re-issuing the **production** `OidcClient`'s authorization URL with that cookie yields a `302` to the registered redirect URI carrying a real `code` and the `state` the client generated, which the **production** `exchange_code` then redeems for a real RS256 id token. §8.3.2's fixture-issuer split is untouched, OQ-28 is untouched, and no browser enters the `auth` job.
- **Rationale.** The premise that proved wrong is that the flow executor cannot be driven headlessly. It can; the earlier attempt was defeated by two undocumented mechanics. First, **the executor answers a completed stage with `302 Location: <itself>`** rather than with the next challenge, so a client that does not follow redirects sees an empty `302` body and concludes the stage failed. Second, **at 2026.5.6 the identification stage reports `password_fields: false`**, so the password is a separate stage and a combined `{"uid_field", "password"}` post is answered with `password: This field is required` against the stage that has no password field. Both are visible only by inspecting the challenge payload. Every rejected alternative is worse on a durable axis. **Playwright in the `auth` job** couples an authorization gate to Authentik's UI markup, so a vendor restyle turns into a red required check with no product defect, and it imports a browser toolchain into a backend job for one navigation. **Deferral to `e2e`** contradicts OQ-28's resolution directly and puts the real code+PKCE flow behind the fixture issuer, leaving the exact integration that breaks in practice unproven. **Minting the code out of band** — a service account plus `client_credentials`, or writing an `AuthorizationCode` row through the API — proves the verifier but skips `/authorize` entirely, which is where redirect-URI registration, the PKCE method, `response_type` and the allowed grant type are actually adjudicated together.
- **Consequences.** §8.3's `auth` job, task 6.3. The dependency taken on is a stable, schema-published Authentik API surface (`/api/v3/flows/executor/`), which the vendor's own LDAP and RADIUS outposts consume, rather than on rendered HTML. Reversal cost is one fixture: the login helper is confined to `backend/tests/integration/authentik_login.py`, and swapping it for a browser leg changes no production code and no other test. A version bump that alters the stage sequence fails with the challenge `component` named in the assertion message, which is a diagnosis rather than a rerun.

---

#### D-55 — The Cerbos sidecar is reached over its versioned HTTP API with the shared `httpx` client; the `cerbos==0.14.0` SDK pin is dropped

- **Status:** Accepted · **Date:** 2026-07-31 · **Supersedes:** the `cerbos` row in §16.2 · **Resolves:** the §2.3 diagram's `gRPC :3593` edge
- **Context.** §16.2 pins `cerbos==0.14.0` and task 6.4 restates it. Implementing the row exposed that the design does not actually describe using that library. §11.1's composition line is `app.state.cerbos = CerbosClient(settings.cerbos_url, http=shared_http)` — a constructor taking a URL and an **injected `httpx` client**, which is not the SDK's `AsyncCerbosClient` signature; the SDK builds and owns its own transport. So §11.1 already specifies a project-owned wrapper, and the pin would sit underneath it unused. The design is also internally inconsistent about the transport: §2.3's diagram draws `BE -->|"gRPC :3593"| CB`, while `settings.cerbos_url` defaults to `http://cerbos:3592`, §13.3 publishes `3592` on loopback, and §13.1's `CERBOS_URL` is an `http://` URL. Three of the four say HTTP.
- **Decision.** `backend/src/auth/cerbos.py` implements `CerbosClient` over the injected `httpx.AsyncClient`, posting to Cerbos's versioned `POST /api/check/resources` with `requestId`, `principal` and `resources` exactly as `api.cerbos.dev/v1` documents, and reading `results[].actions[action]` for `EFFECT_ALLOW`. `cerbos==0.14.0` is **not** added to `pyproject.toml` or either lock. The §2.3 diagram's `gRPC :3593` edge is corrected to HTTP `:3592`; Cerbos still listens on `3593` for gRPC and nothing in Phase 1 dials it.
- **Rationale.** The premise that proved wrong is that pinning the vendor SDK is the low-risk choice. Its published metadata makes `grpcio-tools>=1.54.2`, `protobuf>=6.31.0`, `grpcio-status>=1.73.0`, `protoc-gen-openapiv2`, `types-protobuf` and `httpx[http2]` **runtime** requirements of the backend image — a protoc compiler plugin and a gRPC stack shipped into a production API container to make one JSON POST. That enlarges the SBOM and the CVE surface (protobuf and grpc are among the most frequently advised packages in this stack) and forces `--require-hashes` to resolve platform wheels for a C extension on every target, in exchange for no capability: Phase 1 asks Cerbos exactly one question. **Rejected: pin the SDK as written.** It honours §16.2's letter, but pays that transitive cost *and still needs the wrapper*, because §11.1 injects the shared client — so the pin buys a dependency, not an abstraction. **Rejected: gRPC on `:3593` per the §2.3 diagram.** It needs `grpcio` at runtime for the same reason, and contradicts `settings.cerbos_url`, §13.1's `CERBOS_URL` and §13.3's published port, so three places would have to change to make one diagram edge right. **Rejected: pin the SDK in the dev lock only, as a test oracle.** The real pinned Cerbos container is a strictly better oracle than a second client library, and `grpcio-tools` would still land in every CI install.
- **Consequences.** §16.2 loses one row; §2.3's diagram edge changes from gRPC to HTTP; §11.1, §11.2, §13.1 and §13.3 are unchanged, which is the point. Wire-format drift is caught by `test_cerbos_matrix.py`, which runs the whole §11.2 matrix against the digest-pinned `ghcr.io/cerbos/cerbos:0.54.0` under `require_capability("cerbos")` — a mandatory check, so drift fails the build rather than production. Reversal cost is one module and one pin: `backend/src/auth/cerbos.py` is the only caller of the wire format, and nothing above it knows the transport.

---

#### D-56 — An authorization-sidecar outage is `authorization-unavailable` (503), not `forbidden` (403)

- **Status:** Accepted · **Date:** 2026-07-31 · **Extends:** Appendix C.1 · **Follows:** D-53's reasoning for the identity provider
- **Context.** §11.2's `require_permission` "raises 403 with a body identical to the body a non-existent resource would produce", and Appendix C.1 registers `forbidden` → 403 for "RBAC or Cerbos deny". Neither says what happens when Cerbos cannot answer at all. §2.3 states the operational half — "an authorisation outage stops every non-public request, because deny-by-default means a request whose permission cannot be evaluated is refused. A replica that cannot reach Cerbos should be taken out of service" — so the request is definitely refused, but with which status is left open. The two registered candidates are both wrong. `forbidden` asserts a policy decision that was never made, and it is byte-identical to a real deny by design, so an outage would be indistinguishable from a working authorization layer refusing everyone — the D-23 shape again, and unfalsifiable from the client side. `governance-policy-undefined` (503) names the OPA governance bundle, a different subsystem with a different remedy.
- **Decision.** Register `authorization-unavailable` → 503. Raised by `require_permission` when the Cerbos client raises `CerbosUnavailable`: transport failure, a non-200 answer, a non-JSON body or a non-object body. A Cerbos answer of `EFFECT_DENY`, or an answer that simply omits the action asked about, remains `forbidden` (403) — those are decisions, and an omitted action is a deny by deny-by-default. Cerbos joins `/health/ready`, so the same outage also takes the replica out of service, which is §2.3's requirement.
- **Rationale.** The premise that proved wrong is the same one D-53 corrected for the identity provider, one layer along: Appendix C.1 was assembled per subsystem from the happy path and the denials, and the *unavailability* case of the authorization subsystem has no row. Availability and authority need different responses because they need different remedies — 503 with `Retry-After` semantics means the caller retries and the orchestrator drains the replica, 403 means the caller stops and a human changes a policy. Reusing `forbidden` would also defeat Q-20's own assertion: that property requires the 403 body to be byte-identical whether or not the resource exists, and a body that is *also* emitted for an outage makes "byte-identical" true for the wrong reason. Failing closed is preserved either way — nothing is allowed when Cerbos is silent.
- **Consequences.** Appendix C.1 gains one row; `PROBLEM_REGISTRY` gains one entry; `test_errors_phase1.py` parses the appendix, so the row and the code cannot drift. §11.2's `require_permission` docstring's "raises 403" is narrowed to denials. Reversal cost is one registry line and one `except` clause. No existing type changes status, so no client contract is broken.

---

#### D-57 — Each policy engine is given only its own subtree; OPA loads `policies/mcp`, not `policies/`

- **Status:** Accepted · **Date:** 2026-07-31 · **Supersedes:** the `opa` service's volume and command in §13.3 (inherited from Phase 0 §13.3)
- **Context.** Phase 0 mounted `./policies:/policies:ro` and ran `opa run --server ... /policies`, which was correct while `policies/` held nothing but `policies/mcp/*.rego`. Task 6.4 added `policies/cerbos/` — six YAML resource policies plus a test file — into the same tree. `opa run <dir>` loads **every** file under the path, and a `.yaml` file is loaded as a *data document*, so six documents each declaring `apiVersion` at the top level collide: OPA exits with `error: load error: 6 errors occurred during loading: /policies/cerbos/audit.yaml: merge error ...` **before binding a port**. The gateway's entire Rego surface was therefore unserveable, and `docker compose up` produced a stack with no policy engine. The design is silent on the interaction: §2.3 lists both engines and §13.3 gives both services, but nothing says the two policy sets share a directory that one of them recursively loads.
- **Decision.** OPA is mounted and pointed at `./policies/mcp:/policies/mcp:ro` — the subtree it owns — in `docker-compose.yml` and in `test_opa_policy_integration.py`. A governance bundle arriving in group 7 adds its own mount and its own path argument rather than widening this one. `scripts/check-compose-validate.py` gains a rule that resolves every path the `opa` service loads back through its volume list and fails when that directory contains a non-Rego file, **or** contains no Rego at all, **or** cannot be resolved — with a negative control proving the rule fails when the command is put back to `/policies`.
- **Rationale.** The premise that proved wrong is that `policies/` is a single-engine directory. It stopped being one the moment a second engine got a subdirectory, and the failure was total rather than partial — OPA did not degrade, it refused to start. It also went unnoticed, which is the more important half: the only test that starts OPA reports a dead container through `require_capability("opa", "the OPA container never became healthy")`, so a hard failure was being laundered into a *missing capability*, which skips locally. **Rejected: `opa run --ignore='*.yaml'`.** A denylist inverts the burden — it silently excludes any legitimate future YAML data document, and the next engine to arrive with `.json` policies breaks OPA again. **Rejected: move the Cerbos policies out of `policies/`.** Task 6.4 names `policies/cerbos/` explicitly and Cerbos policies *are* policy source; relocating them to hide from another engine's loader puts the workaround in the wrong file. **Rejected: leave the mount wide and fix only the command.** The mount is what makes the mistake possible; a container that cannot see another engine's files cannot load them by accident.
- **Consequences.** §13.3's `opa` service definition, `docker-compose.yml`, `test_opa_policy_integration.py`, `scripts/check-compose-validate.py`. The `compose-smoke` job's health assertion now means something for OPA, because a policy-set mistake makes the container exit rather than idle. Reversal cost is two lines of Compose. The `/opa version` healthcheck is left as it is: a load error exits the process, so Compose reports the container down rather than unhealthy, and the new validator rule is the check that actually reasons about the policy tree.

---

#### D-58 — JWKS location is discovered from the issuer's metadata, not guessed from its URL

- **Status:** Accepted · **Date:** 2026-07-31 · **Corrects:** Phase 0's `OidcTokenVerifier`, inherited unchanged into §11.2's `AppTokenVerifier` and `IdTokenVerifier`
- **Context.** `OidcTokenVerifier._get_jwks_client` built its key endpoint as `f"{issuer}/.well-known/jwks.json"`. That path is in no specification. OIDC Discovery standardises `/.well-known/openid-configuration` and requires that document to **name** `jwks_uri`; where the keys actually live is the provider's choice. Real Authentik serves `<issuer>jwks/`, so the fetch 404'd, `PyJWKClientError` was mapped to the `signature` failure mode, and **every token real Authentik minted was rejected as though its signature were bad** — `/api/v1/auth/callback` answered 401 after a successful code exchange, which reads like a credential problem and is an infrastructure one. This affected the product API bearer path and the MCP gateway equally, so with Authentik as the configured issuer nothing could authenticate at all.
- **Decision.** `_resolve_jwks_uri` fetches the issuer's discovery document over the injected `httpx` client with a 5-second timeout, requires the document's own `issuer` to equal the token issuer exactly, and uses its `jwks_uri` when that value is an absolute `http(s)` URL. The result is cached with the `PyJWKClient` under the existing `jwks_ttl_seconds`, so discovery costs one round trip per issuer per TTL rather than one per request. `_get_jwks_client` becomes a coroutine. The historical well-known path remains as a **fallback**, used only when the document is unreachable, is not an object, omits `jwks_uri`, gives a non-absolute value, or declares a different issuer.
- **Rationale.** The premise that proved wrong is that a JWKS lives at a conventional path. It does not, and the guess was never evidence of anything — but the more important finding is *why it survived*. Two independent test issuers were written to serve `/.well-known/jwks.json`, and `test_auth_oidc_flow.py`'s discovery document names that same path, so the guess and the metadata agreed **by construction**: the fixtures were shaped around the implementation rather than around the protocol. Worse, `test_authentik_real_idp.py` carried an assertion whose docstring said "fetched through `IdTokenVerifier`'s own JWKS client" while its body used a bare `httpx.get(metadata.jwks_uri)` — so the one test positioned to catch this examined nothing, the same class of defect as `with_ef_search` and the unreachable two-role arrangement. Both are repaired: that test now resolves through the verifier and additionally asserts Authentik has *not* moved its JWKS to the guessed path, which would silently disarm the regression check. `test_jwks_discovery.py` adds the decisive assertion the fix needs and the old code never had — the discovered URI **wins**, proved by an issuer that serves its keys where the guess would never look, plus a hit-log assertion that the guessed path is not requested at all. **Rejected: remove the fallback and require discovery.** Phase 0's gateway is a resource server for upstream issuers it does not control, and one of its own test issuers publishes a JWKS and no metadata; making that a hard failure would break a shipped contract to fix an unrelated bug. **Rejected: a `JWKS_URI` setting.** Two configured sources for one issuer's identity can disagree, and §13.1 registers no such variable. **Rejected: joining a relative `jwks_uri` against the issuer.** That is inventing a location again, which is the original mistake in a new spelling; a non-absolute value falls back instead.
- **Consequences.** `src/core/security.py`, `src/auth/oidc.py` (inherited), `src/auth/verifier.py` (inherited), `test_authentik_real_idp.py`, the three `test_mcp_gateway.py` sites that patch `_get_jwks_client` and now pass `autospec=True` so the coroutine is awaited. One pre-existing wart is left as it is and named rather than silently widened: `PyJWKClient.get_signing_key_from_jwt` fetches with `urllib` and therefore still blocks the event loop, which predates this change; discovery uses the shared async client, so this decision adds no new blocking call. Reversal cost is one method.

---

#### D-59 — The verified envelope is its own leaf package, `agent/internal/envelope`; `*envelope.Verified` is the parameter type everywhere

- **Status:** Accepted · **Date:** 2026-07-31 · **Adopts:** §2.2.1's `*envelope.Verified` spelling · **Supersedes:** the `Verified` type's placement in `internal/session` (§10.4's code block header) and the `*session.Verified` parameter in §10.5's `Dispatcher.Execute`, `mutate.ApplyVerified` and `mutate.Revert`, plus the same spelling in §10.3's `Manager` narrative, §11.6's note and `tasks.md` leaf 7.2 · **Blocks, until decided:** task group 7 in full
- **Context.** Three sections of this document name the type that proves an envelope was verified, and they do not agree. §10.4 defines it inside a block headed `// Package session — envelope.go`, so the type is `session.Verified`. §10.5 writes every consumer's signature as `*session.Verified` — `Dispatcher.Execute`, `ApplyVerified`, `Revert` — and `tasks.md` leaf 7.2 restates `ApplyVerified(ctx, *session.Verified, root, entries)`. §2.2.1's package tree says the opposite: `executor.go # named-operation dispatch; takes *envelope.Verified`. Neither type exists yet, so nothing was broken; but leaf 7.2 cannot be written until the plan says which one it is, and `internal/session` as built by leaf 4.6 contains a journal and a credential store and no `Verified` of any kind.
- **The decisive fact is an import cycle, not a preference.** §10.1's graph draws `SESS --> EXEC`, and §10.3's `Manager` holds a `dispatcher` among its collaborators, so **`session` imports `executor`**. §10.1 also draws `EXEC --> MUT`, where `MUT` is `executor/internal/mutate`. If `mutate.ApplyVerified` takes a `*session.Verified` then `mutate` imports `session`, and the graph closes: `session → executor → executor/internal/mutate → session`. Go rejects that at compile time. The `*session.Verified` spelling is therefore not merely inconsistent with §2.2.1 — **it does not compile**, and no amount of care at the call sites changes that.
- **Decision.** Create `agent/internal/envelope`, a **leaf** package that imports nothing from `internal/**`. It owns the envelope value type, the verified-envelope capability type, canonicalisation and verification:
  - `Envelope` — the §7.6 wire shape: `v`, `command_id`, `device_id`, `operation`, `args`, `approval_id`, `policy_context`, `nonce`, `seq`, `not_after`, `signature`.
  - `Verified` — unexported fields, no exported constructor, no settable field. The **only** way to obtain one is `(*Verifier).Verify`, exactly as §10.4 specifies; only the package it lives in changes.
  - `CanonicalBytes(Envelope) ([]byte, error)` — exported for the cross-runtime fixture corpus, which is the reason §10.4 gives for exporting it and the reason Q-14 exists.
  - `Verifier` and the typed errors whose `Code()` maps to an RFC 9457 suffix and an `agent.error` code.
  §10.1's graph gains one node, `ENV["internal/envelope"]`, with `SESS → ENV`, `EXEC → ENV` and `MUT → ENV`. `session` keeps the journal, the credential store and the `Manager`; it gains no envelope type and loses nothing it has. `§2.2.1`, `§10.4`, `§10.5`, `§10.3`, `§11.6` and `tasks.md` 7.2 are corrected to `*envelope.Verified` in the same commit as this entry, so no two places disagree afterwards.
- **Rationale.** The type's whole value is that it is unforgeable: a `*envelope.Verified` in a signature means "somebody checked the signature", and it means that because there is no other way to make one. That guarantee is a property of **package scope** — unexported fields plus a single constructor — so the question "which package owns it" is not cosmetic, it decides who can mint it. A leaf package is the smallest possible answer: `envelope` depends on nothing in `internal/**`, so nothing it might import can ever create a cycle with a consumer, and every consumer of the capability sits *above* it in the graph rather than beside it. It also puts canonicalisation, signing input and verification in one file next to the type they protect, which is what makes the Go half a peer of `backend/src/governance/envelope.py` rather than a fragment spread across two packages.
- **Alternatives rejected, and why each is worse.**
  - **(A) `Verified` in `session`, as §10.4, §10.5 and `tasks.md` 7.2 literally say.** Rejected because it does not compile: the cycle above. The only way to keep it would be to invert the dependency — `executor` declares an interface that `session` satisfies — and that is worse than the cycle it fixes. `Verified` would become an **interface**, and an interface can be implemented by any package that wants one, including by a test double with a `Verified()` method that checks nothing. The capability's unforgeability comes from having no other constructor; converting it to an interface deletes exactly that. This alternative trades a compile error for a silent hole, which is the trade §2.2.1 exists to refuse.
  - **(B) `Verified` in `executor`, with `session` importing it.** No cycle, because `session → executor` is the direction §10.1 already draws. Rejected on two counts. First, it inverts responsibility: the `Verifier` performs HMAC verification, freshness and replay checks, so putting the type in `executor` either drags the cryptography into the dispatch package or splits one invariant across two packages — and §10.4's ordering note ("signature verification happens **before** the sequence and nonce updates") is exactly the kind of ordering that must live in one file. Second, `CanonicalBytes` must be exported for Q-14's fixture corpus; exporting it from `executor` means the backend-parity test, and anything else that ever needs canonical bytes, imports the command dispatcher to get them.
  - **(C) A type alias: `package session; type Verified = envelope.Verified`, keeping both spellings compiling.** Rejected. It is the cheapest change and the most expensive to live with: two names for one type is precisely the ambiguity that produced this conflict, and a reader seeing `session.Verified` in one signature and `envelope.Verified` in another cannot tell whether they carry the same guarantee without checking. `scripts/check-chokepoint.sh` (leaf 7.3) would also have to know both spellings to find the boundary, and a checker that must know two names for one thing is one rename away from finding neither.
  - **(D) `Verified` in `fileops`, beside the path validation it travels with.** Rejected. `fileops` is the exported path-validation and diff package that P-08 guards and that many packages may read; the authorisation capability has no business there, and putting it there would mean every caller of `UnifiedDiff` imports the envelope machinery. It also reads as though path validation were the thing being authorised, when the thing being authorised is the write.
  - **(E) Leave the conflict open and write leaf 7.2 against whichever spelling compiles first.** Rejected explicitly, because that is how the conflict was created. A signature chosen by what compiled is a signature nobody decided, and group 7 is the trust boundary — the one place where "we will tidy the names later" produces a control that looks like a control.
- **Costs accepted, stated plainly.** One more package in the agent graph, so §10.1's diagram, the §2.4 monorepo listing and `scripts/check-structure.sh`'s structural lists each gain an entry. §10.4's heading now describes code that lives elsewhere, which is why this entry names the supersession rather than leaving a reader to reconcile it. And a real ordering cost: `envelope` must land **before** leaf 7.2 can compile, so D-59's implementation is part of 7.2's commit rather than a later leaf — the package arrives with the type and the canonicalisation and signature verification the type depends on, because a `Verified` that can be obtained without checking a signature is worse than no type at all. The stateful half of §10.4's six-step order (per-device `seq` high-water mark, the bounded nonce set, the policy-bundle digest) is supplied through **required** constructor collaborators, so `NewVerifier` refuses a nil replay guard or a nil bundle source; leaf 8.6 replaces group 7's in-memory implementations with the real ones and adds Q-15. That is a smaller and more honest scope than shipping a `Verifier` with three checks stubbed out.
- **Reversal cost.** Low, and lower than it looks. Reverting to alternative (B) is a package move plus one import line per consumer, because no call site mentions the package name in anything but its parameter type. Reverting to alternative (A) is not available at any price — it is the cycle. If the `envelope` package is ever felt to be too small, the cheap merge is `envelope` into `session` **only if** `session` first stops importing `executor`, which would mean re-drawing §10.1; recording that here means the next reader does not have to rediscover the cycle to find out why the package is separate.
- **CI job that proves it.** `agent`: the package builds under `CGO_ENABLED=0` for all six targets, `contract_test.go` carries the interface assertions, and `TestBoundary_MutateHasNoImporterOutsideExecutor` plus `bash scripts/check-chokepoint.sh` (leaf 7.3, run in both the `agent` and `backend` jobs) assert the boundary from the outside. The negative control for the placement specifically is a compile failure rather than a test: `agent/internal/envelope/boundary_test.go` builds a throwaway package outside `executor/**` that imports `executor/internal/mutate` and asserts `go build` **fails**, and a `testdata` fixture that imports `internal/envelope` from outside `executor/**` and asserts it **succeeds** — so the boundary is proven to be in the right place and not merely present. **Q-03** is the property that quantifies the whole clause.

#### D-60 — The envelope signing key reaches `sign_envelope` through a scoped `ContextVar`, and the setter is banned too

- **Status:** Accepted · **Date:** 2026-07-31 · **Adopts:** §2.2.1's literal `_SIGNING_KEY` spelling · **Extends:** §2.2.1's banned-api table, from five entries to six
- **Context.** §2.2.1 names `src.governance.envelope._SIGNING_KEY` in the banned-api table as a module-level constant of `governance/envelope.py`, and §2.2.2 states that the control plane is the **sole holder** of the per-device envelope signing key. Leaf 7.4 has to give that name a referent. Ruff's `banned-api` rule matches the written import path, so an entry naming a symbol that does not exist bans nothing while looking exactly like an entry that does — the same vacuity trap §0.4.5 closes for property tests.
- **Decision.** `_SIGNING_KEY` is a module-private `ContextVar[bytes | None]`, installed by `signing_key_scope(key)` — a context manager that resets to the **previous** token on exit rather than to `None`. `sign_envelope(envelope, *, prefix)` reads it and raises `SigningKeyUnavailableError`, naming the context manager, when no scope is active. A **sixth** banned-api entry confines `signing_key_scope` to `governance/` as well.
- **Rationale for the sixth entry.** `signing_key_scope` is the only writer of the ContextVar. Left unbanned, a module outside `governance/` could install a key of its choosing, and a governance path that forgot to open its own scope would then sign with that key instead of raising. Banning the setter restores the property that carries the weight: **a missing scope always raises.** §2.2.1's table is a floor, not a ceiling.
- **Alternatives rejected.**
  - **(A) A module-level `dict[uuid.UUID, bytes]` of device keys.** The obvious realisation of a "sole holder", and rejected because it is a process-wide cache that outlives a revocation. **Q-16** requires a revoked device's *next message* to be rejected, and a cache with no invalidation path is how that guarantee becomes eventual. A ContextVar has no state between requests to go stale.
  - **(B) `sign_envelope(envelope, *, key: bytes)`, mirroring the Go `Sign(prefix, e, key)`.** Works, and is easier to read at the call site. Rejected because it leaves `_SIGNING_KEY` with nothing to be: the table entry becomes decorative, and a decorative entry in the enforcement table is worse than a missing one, because a reader cannot distinguish them. The Go side is not a counter-example — it does not hold a key at all (see `sign.go`), it takes one so the fixture corpus can be produced from either side.
  - **(C) Read the key from `Settings`.** Rejected: the key is per device, not per process, and a process-wide envelope key would make every device's commands forgeable from any device's key.
- **Costs accepted.** The key is implicit at the call site, which is genuinely harder to follow than a parameter. Paid down by the named error, by the single setter, and by the previous-token reset so a nested scope restores its parent instead of clearing it. The second cost is the extra table entry: §2.2.1's five became six, so a reader comparing the design's table with `pyproject.toml` finds a difference and must read this entry to learn why.
- **CI job that proves it.** `backend`: `tests/unit/test_governance_authority.py` drives **real Ruff** over a temporary file outside the package for each of the six banned symbols and asserts `TID251` in the diagnostics; `tests/unit/test_governance_envelope.py::TestTheSigningKeyIsConfined` asserts signing without a scope raises, that the scope is restored when the body raises, and that a nested scope restores its parent.

#### D-61 — `verify_chain` also compares each row's stored `prev_hash` against its predecessor's `hash`

- **Status:** Accepted · **Date:** 2026-07-31 · **Extends:** Appendix A.8's `VerifyChain`, which compares only recomputed hashes
- **Context.** Appendix A.8 hashes `SHA256(JCS(SemanticFields(row)) ‖ prev)` where `prev` is the **previous row's stored hash**, and `SemanticFields` excludes `seq` and `hash`. It has to exclude `prev_hash` from the payload as well, or the value would be hashed twice and the chain's structure would depend on an accident of which list a field appeared in. But that leaves `prev_hash` covered by nothing except the recomputation using it.
- **The gap.** An actor with database write access can therefore rewrite one row's `prev_hash`, recompute that row's `hash` from the new value, and recompute every later hash. Appendix A.8's verifier as written walks forward carrying `prev = row.hash`, so it never reads the stored `prev_hash` column at all — the forged chain is internally consistent and verifies. The result is a chain that is arithmetically sound and no longer describes the history it came from, which is the one failure mode tamper evidence exists to exclude.
- **Decision.** `verify_chain` compares `row.prev_hash` against the predecessor's stored `hash` **before** recomputing the row's own hash, and reports the divergence with `kind="prev_hash"` rather than `kind="hash"`. Checking it first is what localises the tamper: the forged row is named, rather than the first row whose recomputation happens to fail.
- **Alternatives rejected.** **(A) Include `prev_hash` in the JCS payload.** It would be hashed twice — once in the payload, once in the concatenation — which is harmless arithmetically but means the negative control Appendix B fixes for Q-05 ("drop `prev_hash` from the hashed payload") no longer identifies one clause. **(B) Rely on `seq` gaps.** A gap catches deletion, not substitution, and this attack inserts nothing and deletes nothing. **(C) Store a signature over each row instead of a chain.** A larger change than the gap warrants, and it moves the trust to a key that has to live somewhere — which is a different problem, not a smaller one.
- **Costs accepted.** One extra 32-byte comparison per row, which is free next to the SHA-256 it precedes. And a third `kind` in the reported divergence (`hash`, `prev_hash`, `gap`), so a client rendering the result has one more case; `GET /api/v1/audit/verify` returns it as a string rather than an enum for exactly that reason.
- **CI job that proves it.** `backend`: `tests/integration/test_audit_writer.py::TestTamperEvidence::test_rewriting_prev_hash_alone_is_also_caught` disables `0007`'s UPDATE trigger as the table's owner, rewrites one row's `prev_hash` to the genesis value, re-enables the trigger, and asserts the reported divergence is that row's `seq` with `kind == "prev_hash"`. **Q-05** quantifies the whole clause in leaf 7.9.

#### D-62 — The envelope key-encryption key is derived from `ENVELOPE_PEPPER` with HKDF-SHA256, and the AEAD is bound to the device row

- **Status:** Accepted · **Date:** 2026-07-31 · **Implements:** §6.3's `agent_devices.envelope_key_enc`, §11.2's `envelope_key`
- **Context.** `envelope_key_enc` is the one secret in the schema that must be *recoverable* rather than hashed, because the backend signs command envelopes with it (§7.6, §2.2.2). §6.3 says only "encrypted (AES-256-GCM under an app-level key from the secret store)" — it names neither where that key comes from nor what the AEAD is bound to, and the chokepoint cannot mint an envelope without both answers.
- **Decision.** Derive the key-encryption key with **HKDF-SHA256 from the existing `ENVELOPE_PEPPER`** under the label `forgeops-envelope-key-v1`, and seal the per-device envelope key with **AES-256-GCM** into `envelope_key_enc`. No new configuration, and domain-separated from the pepper's HMAC use for pairing codes and device tokens. Two properties are part of the decision rather than of the implementation: **(1)** the **device id is the additional authenticated data**, so a ciphertext lifted from one `agent_devices` row cannot be unsealed under another; **(2)** every seal uses a **fresh 96-bit nonce from the OS CSPRNG**, stored in front of the ciphertext and never derived from anything reusable.
- **Why the AAD is not optional.** Without it, an attacker holding only `UPDATE` on `agent_devices` can transplant a ciphertext whose plaintext they know onto a victim device's row and then sign envelopes that device will accept — without ever learning the victim's key. The binding turns that from a key-substitution attack into an authentication failure.
- **Alternatives rejected.** **(A) A dedicated KEK environment variable.** It adds a secret to §13.1 whose rotation is already coupled to the pepper in practice, because rotating the pepper invalidates every stored device-token and pairing-code HMAC anyway. It buys independent rotation of something that cannot rotate independently. **(B) Leaving the column plaintext.** The column name asserts ciphertext, and a column that lies about its contents is worse than one that is honestly named. **(C) A per-device random HKDF salt.** The input keying material is a high-entropy configured secret rather than a password, so the salt's job here is domain separation, not stretching; a per-row salt would add a column and a migration for no adversary it excludes.
- **Costs accepted.** **(1) The derived KEK cannot rotate independently of the pepper.** Rotating it means re-sealing every `agent_devices` row, and **Phase 1 does not implement that re-seal** — it is a named gap, **OQ-33**. The operational consequence, stated plainly: a pepper rotation in Phase 1 invalidates every device's pairing and requires re-pairing rather than a re-seal. **(2) If the pepper leaks, the envelope keys fall with it.** The marginal loss is smaller than it first appears — a leaked pepper already forges device-token and pairing-code HMACs, which is enough to impersonate a device *to* the backend — so the coupling adds the ability to forge commands *to* a device. That is still a real widening, and saying so beats implying the coupling is free. **(3) `envelope_key` returns `SecretBytes`, not the `SecretStr` §11.2's sketch writes.** The key is 32 random bytes; a `SecretStr` would need an encoding step with two plausible spellings, and a wrapper whose contents must be decoded before use is a wrapper that gets unwrapped early. **(4) `envelope_key` exists as a module-level function as well as a `DeviceService` method,** because §2.2.1's confinement is a Ruff `banned-api` entry and that mechanism matches imports — an entry naming a method would ban nothing while looking exactly like one that does.
- **CI job that proves it.** `backend`: `tests/unit/test_auth_devices.py` — 32 tests, including `TestTheAeadIsBoundToTheRow::test_a_ciphertext_does_not_unseal_under_another_device` (seals under one device id, fails to unseal under another, and opens under the original as the control), `TestTheNonceIsFresh::test_every_seal_uses_a_fresh_nonce` (512 seals of one plaintext under one key, all nonces distinct), `test_it_is_domain_separated_from_the_peppers_hmac_use`, and `TestUnsealFailuresAreIndistinguishable`. `tests/integration/test_governance_chokepoint.py::TestTheAutoApprovedTransit::test_the_envelope_verifies_under_the_devices_sealed_key` drives the whole custody path — generate, seal, store, unseal, sign, verify.

#### D-63 — Revision `0010` reconciles `change_sets.status` with §3.6, beyond §6.5's eight-revision plan

- **Status:** Accepted · **Date:** 2026-07-31 · **Corrects:** revision `0004`, `src/governance/models.py::CHANGE_SET_STATUSES`
- **Context.** `0004` generated `ck_change_sets_status_allowed` from `CHANGE_SET_STATUSES`, which is the arrangement §6.5 asks for — but the tuple had been written from memory rather than from §3.6. It carried three names §3.6 does not define (`validated`, `awaiting_approval`, `failed`) and was missing six it does (`rejected_by_policy`, `blocked`, `pending_approval`, `expired`, `conflicted`, `reverted`).
- **The gap.** Three of the six outcomes Appendix A.3's transit produces were unstorable: a blast-radius block writes `blocked`, the approval gate writes `pending_approval`, and a completed revert writes `reverted`. Leaf 7.5 could not be implemented against that constraint, and Q-22 — "only edges in the §3.6 state machine are accepted" — could not be asserted against a schema that rejects three of its states. `test_0004_change_sets.py` stayed green throughout, because it parametrises over the same tuple that was wrong.
- **Decision.** Add `0010_change_set_status_vocabulary`, set `CHANGE_SET_STATUSES` to §3.6's thirteen states in lifecycle order, and add `CHANGE_SET_TRANSITIONS` and `TERMINAL_CHANGE_SET_STATUSES` as data so Q-22 has one source to quantify over. §6.5's revision table gains a ninth row. The upgrade **validates** against existing rows and refuses if any status falls outside §3.6; the downgrade restores `0004`'s narrower list as **`NOT VALID`**, so it can run against rows the wider vocabulary allowed.
- **Why the two directions differ.** A downgrade that refused whenever a row carried `blocked` would be a downgrade nobody can run, and the first thing it breaks is `alembic downgrade base`, which every §6.5 revision proof runs before it migrates up. That was observed directly the first time this revision was written with a symmetric guard. `NOT VALID` says the right thing: the narrower vocabulary constrains every future write, and rows already written stay readable rather than being deleted.
- **Alternatives rejected.** **(A) Map A.3's outcomes onto the nearest surviving names** — `blocked → rejected`, `pending_approval → awaiting_approval`, `reverted → rolled_back`. It would make the audit trail say a human rejected a change set the blast-radius analyser blocked. A schema that cannot express the design is a smaller problem than one that misrepresents it. **(B) Amend §3.6 to match the code.** The state machine is the authority and the tuple was the error; changing the authority to match an implementation slip is how a design stops being one. **(C) Widen the constraint inside `0004`.** `0004` is a published revision; editing it would leave any database already at `0009` with a constraint no revision describes.
- **Costs accepted.** A tenth revision beyond §6.5's stated plan, so §6.5's table and `test_alembic_linearity.py::EXPECTED_HEAD` both move — deliberately, because that constant moving is the reviewable signal that a revision beyond the plan was added on purpose. And `rejected_by_policy` is currently **unreachable**: A.3 evaluates policy before compiling a change set, so a policy deny produces no row to transition. The state is in the vocabulary because §3.6 defines it, not because wave 7 writes it.
- **CI job that proves it.** `backend`: `tests/integration/test_0010_change_set_statuses.py` — 25 tests, including `test_the_tuple_equals_the_states_named_by_the_diagram`, which **parses §3.6's mermaid block out of `design.md`** rather than restating it, so a future divergence fails and names the difference; plus `test_a_status_outside_the_vocabulary_is_rejected` over the three removed names and `test_the_constraint_is_valid_after_the_upgrade` reading `pg_constraint.convalidated`.

#### D-64 — On the auto-approved path, `approval_id` is the id of the audit record that recorded the auto-approval

- **Status:** Accepted · **Date:** 2026-07-31 · **Resolves:** an ambiguity in Appendix A.3's `MintAuthority(cs, gate.approval_id, …)`
- **Context.** §7.6 makes `approval_id` a required, signed envelope member, and §7.7 makes it required for `changeset.apply`. On the human path it is `approvals.id`. On the **auto-approved** path there is no `approvals` row, because nobody approved it — and `approvals.approver_id` is `NOT NULL` with `ON DELETE RESTRICT`, so there is no honest row to write. A.3 reads `gate.approval_id` from a gate whose Phase 0 contract returns only an `ApprovalDecision`.
- **Decision.** On the auto-approved path — and on the revert path, which mints the same way — `approval_id` is the **`audit_events.id` of the record that authorised the transit**. That row is immutable by construction (§6.4, migration `0007`), it exists before the authority does because `MutationAuthority.audit_seq` requires it, and it is the only artifact that actually authorised the mutation.
- **Alternatives rejected.** **(A) A fresh random UUID, persisted nowhere.** Joinable to nothing, so an operator holding an envelope could never find out what authorised it. An unjoinable identifier in a signed envelope is a lie with a UUID's credibility. **(B) Write an `approvals` row with the submitting principal as approver.** It records that a person approved their own change set when the gate decided; §11.2's self-approval rule exists precisely to make that distinction, and this would erase it. **(C) Make `approvals.approver_id` nullable.** A schema change to a published revision to model "nobody", when a better artifact for "what authorised this" already exists.
- **Costs accepted.** `approval_id` resolves in **two tables** depending on the path, so a reader needs to know which. Mitigated rather than hidden: the audit row's `action` distinguishes them (`change_set_auto_approved` and `change_set_revert_authorised` versus `change_set_approved`), and the auto-approved envelope's `approval_id` is always findable as an `audit_events.id` for the same `change_set_id`.
- **CI job that proves it.** `backend`: `tests/integration/test_governance_chokepoint.py::TestTheAutoApprovedTransit::test_the_envelope_carries_the_operation_the_bundle_and_the_approval` and `::TestTheApproveTransit::test_approval_mints_the_envelope_and_records_the_approver`, which assert the human path's `approval_id` equals `approvals.id`.

#### D-65 — `PlanFrom(change_set)` renders change items as one synthetic resource type, so blast radius is a function of action mix and cardinality

- **Status:** Accepted · **Date:** 2026-07-31 · **Implements:** Appendix A.3 stage 4's `SemanticPlanAnalyzer.Analyse(PlanFrom(cs))`
- **Context.** `SemanticPlanAnalyzer` was built in Phase 0 for OpenTofu plan JSON: it reads `resource_changes`, classifies each resource type as `stateful`/`network`/`iam`/`compute`/`unknown`, and multiplies an action weight by that class's multiplier. A Phase 1 change set is a set of **file** edits. A.3 names the analyser for stage 4 without saying what `PlanFrom` produces.
- **Decision.** `plan_from_change_items` emits one `resource_change` per item, with `address = "file.<path>"`, `actions` taken from the item's action, and `type = "forgeops_file"` — a type `classify_resource` does not know, so every item classifies as `unknown`. Blast radius for a file change set is therefore a function of how many files change and how destructively, which is exactly what a file change set has to offer.
- **Alternatives rejected.** **(A) Map file paths onto cloud resource classes.** It invents a class the change set does not have, and the multipliers were calibrated for infrastructure — a `.tf` file edit is not an `aws_db_instance` deletion. **(B) A second analyser for file change sets.** Two blast-radius implementations is how the two come to disagree, and P-11's monotonicity property would then guard only one of them. **(C) Reuse a type that *is* in `STATEFUL_TYPES`** (`null_resource` is present, for testing) to make protected-path deletions block. It would score every file deletion as a stateful loss and make the analyser's own comment untrue.
- **Costs accepted.** A file change set can never reach `stateful_deletions`, so the `block` verdict is reached only by cardinality: **a single deletion of a critical file is a `warn`, not a `block`**. Protecting a specific path is the policy layer's job — `policies/agent/paths.rego` ("never edit `package.json`", §11.7, leaf 9.1) — not the analyser's. Asserted rather than left implicit, so nobody later reads the absence as a bug.
- **CI job that proves it.** `backend`: `tests/unit/test_governance_chokepoint.py::TestThePlanTranslation` — 6 tests, including `test_the_verdict_is_independent_of_item_order`, `test_adding_a_deletion_never_lowers_the_score` (P-11's shape) and `test_a_file_change_set_never_claims_a_stateful_deletion`.

#### D-66 — A revert compiles a **reverse change set** and runs the full chokepoint over it

- **Status:** Accepted · **Date:** 2026-07-31 · **Implements:** §11.6's `revert`, §3.6's `applied → reverted`
- **Context.** §11.6 says a revert "runs the full chokepoint again and mints its own authority", and §3.6 gives exactly one edge out of a success state: `applied --> reverted : rollback handle used`. §3.6 has no in-flight state for a revert — no `applied → applying` edge and no `reverting`.
- **Decision.** `revert(X)` compiles a **new** change set `Y` whose items are `X`'s inverted (create↔delete, update with contents swapped), in reverse ordinal order, and runs stages 1, 2, 4, 5 and 6 over `Y`. `Y` has its own ordinary lifecycle and its own minted authority. `X` moves `applied → reverted` when `Y` has been applied and the handle consumed, which is precisely §3.6's label for that edge. `X`'s rollback handle is marked consumed in the same transaction that authorises `Y`.
- **Alternatives rejected.** **(A) Revert in place**, transitioning `X` from `applied` to `applying`. §3.6 has no such edge, so Q-22 would have to be weakened to accept one the design does not define. **(B) Mark `X` `reverted` at mint time.** It records a revert that has not happened; if delivery or the apply fails, the record says the change was undone while the disk still holds it. **(C) Reuse the original authority.** §11.6 rejects this outright: it "would make rollback a privileged back door."
- **Costs accepted.** **(1) Two change-set rows per revert**, and the link from `Y` to `X` lives in the audit record's `after_state` and in the envelope's `args.reverts_change_set_id` rather than in a column — adding a self-referential FK to a published revision is a larger change than the join it saves. **(2) `X`'s transition to `reverted` is written by the `command.result` handler, which arrives with the hub in group 8.** Until then a reverted original stays `applied` after its reverse set is minted. Named here rather than papered over. **(3) The handle is consumed at authorisation, not at completion,** so a revert whose delivery fails cannot be retried without an operator reserving a new handle. Single-use is the property Q-02 asserts and re-use is the more dangerous failure, so the asymmetry is deliberate — but a denied or approval-pending revert leaves the handle untouched, which is asserted.
- **CI job that proves it.** `backend`: `tests/integration/test_governance_chokepoint.py::TestTheRevertTransit` — 6 tests, including `test_a_revert_compiles_the_reverse_set_and_mints_its_own_authority` (fresh approval id, fresh digest, higher `seq` than the forward envelope), `test_the_reverse_items_invert_the_original`, `test_the_original_handle_is_consumed_and_a_second_revert_refuses`, and `test_a_denied_revert_writes_one_record_and_leaves_the_handle_alone`.

#### D-67 — `check-chokepoint` resolves receivers by type, adds a third verdict, and reads its Go vacuity guard as the *enumeration*

- **Status:** Accepted · **Date:** 2026-07-31 · **Implements:** §2.2.1's reachability check (leaf 7.3)
- **Context.** §2.2.1 specifies the check as "AST-walk `src/**`, assert every call to a `@mutation_primitive`-decorated function is lexically inside `src/governance/` OR receives a `MutationAuthority`". Implementing it exposed two gaps that a literal reading does not survive.
- **Gap 1 — matching by name is unusable.** The first primitive to exist is `AuditWriter.append`. Matching call sites on the bare name `append` flags every `list.append` in the backend. The first run reported five offenders, four of which were a list, a dict-of-clauses and an error accumulator. A check that cries wolf gets switched off, which is pattern O's failure by another route.
- **Gap 2 — the Go half's vacuity guard has two plausible readings.** "Exit 1 if the discovered set is empty" applied naively to the *importer* set fails on a correct tree: Go's nested-`internal` rule means only packages rooted at `internal/executor/` may import `mutate`, and the only such package is `executor` itself, whose dispatcher arrives in leaf 8.7. So today the correct answer is zero importers.
- **Decision.** **(1)** An attribute call is a primitive call only when its receiver **resolves to the owning class**, by a deliberately narrow syntactic analysis: annotated parameters, attributes assigned anywhere in a class body from an annotated parameter, locals assigned from a constructor call, annotated locals and class attributes, and literals. **(2)** A receiver the analysis cannot type is a **third verdict**, `unresolved-receiver`, and it **blocks** — a receiver that might be the primitive's owner is not something a mutation-path check may assume away, and the message tells the author to annotate it. **(3)** "Receives a `MutationAuthority`" is a **name-binding** question, not a keyword-name one: the call must pass a name the analysis knows holds an authority, so `authority=None` does not satisfy it. **(4)** The Go half's vacuity guard is on the **enumeration** §2.2.1 names as its input — the import graph must be non-empty and must contain the boundary package as a node — and an empty *importer* set is reported, never failed.
- **Alternatives rejected.** **(A) Flag every name match and allowlist the false positives.** The allowlist becomes the artifact, and it grows every time someone appends to a list. **(B) Require primitives to have globally unique names.** It puts a naming rule on production code to suit a checker, and `append` is the right name for what `AuditWriter.append` does. **(C) Resolve types properly with a type checker.** Correct and disproportionate: it would make the check depend on a full inference engine and on `mypy`'s cache being warm, for one question about one decorator. **(D) Treat an unresolved receiver as clean.** The failure would then be silent, which is the one direction a mutation-path check must not fail in.
- **Costs accepted.** The analysis is **shallow by design**, so a receiver reached through a factory function or a container is unresolvable and must be annotated. That is a real constraint on how `backend/src/**` may be written, and it is the constraint that makes the boundary decidable — stated here rather than discovered by whoever first hits it. The check also cannot see `agent/testdata/**`, because `go list ./...` skips it, so the Go half's classification is proved against a **synthetic** graph while the compile-time rule itself is proved by `mutate/boundary_test.go`'s two real `go build` fixtures.
- **CI job that proves it.** `agent`: `bash scripts/check-chokepoint.sh --go`. `backend`: `bash scripts/check-chokepoint.sh --python`. Also `make lint-chokepoint` and a `pre-commit` local hook on `^(agent|backend)/`. Its own tests are `backend/tests/meta/test_check_chokepoint.py` — 23 tests over `fixtures/chokepoint/**`, asserting that each of the four failing and two passing verdicts fires at least once, that a `list.append` is never reported, that an empty primitive set and a renamed decorator both exit 1, and that the check still prints a readable verdict under `PYTHONIOENCODING=cp1252`.

---

#### D-68 — a Go test may DECLARE a platform it cannot run on; nothing else is exempt from §0.4.4

- **Status:** Accepted · **Date:** 2026-07-31 · **Implements:** §0.4.4's skip gate, on the Go side
- **Context.** `scripts/check-no-skips.py --go` could not pass on a developer machine and its CI result did not exist. Two separate facts. **(1)** Nine Go tests skipped on Windows. Six assert POSIX file semantics the platform cannot express — symlink escape, a `0555` directory refusing a write, owner-only mode bits under NTFS ACLs — and §0.4.4's remedy, "provide the capability in CI", cannot be carried out: you cannot give Windows POSIX mode bits. **(2)** The other three were a defect, not a platform limit: `internal/iac/env_test.go` set process-global `PATH=/usr/bin` with no restore, so `exec.LookPath("powershell.exe")` failed for every later test in that binary and three `TestTerminateGroup_*` tests skipped. They passed under `-run` and skipped in the full suite. And **the gate was invoked nowhere for Go** — criterion 11 and §0.4.4 both say the `agent` job runs it; the only invocation in `ci.yml` was the backend `auth` job's. So the Go zero-skip claim rested on nothing, which is how all nine survived.
- **Decision.** **(1)** Fix the defect: `t.Setenv` throughout, which records the previous value and restores it, and which panics if the test also calls `t.Parallel`. **(2)** Wire `check-no-skips.py --go` into the `agent` job over a `go test -json -tags=integration ./...` report, with `actions/setup-python` added because the job had no interpreter. **(3)** A Go test may declare a platform requirement in **its own skip message** — `platform-only: posix` or `platform-only: windows`, a closed vocabulary — and the gate judges the declaration against the platform the report came from. An undeclared skip fails; a declaration outside the vocabulary fails; and **a declaration whose requirement the reporting platform SATISFIES fails**, which is the clause that stops the tag becoming a blanket exemption. On Linux `posix` is satisfied, so all six of those tests must run and CI's guarantee is unchanged. **(4)** `parse_go` keeps every output line rather than the ones containing "skip", because that filter matched the `--- SKIP:` banner and discarded the reason line above it — the gate whose purpose is reporting skips could not report why any of them skipped.
- **Alternatives rejected.** **(A) An allowlist of exempt test names, with an expiry date.** It restates data away from the `runtime.GOOS` guard that causes the skip, which is finding 49's rot in a new place: rename the test and the entry is dead weight, delete the guard and the entry keeps exempting a name that no longer skips. An expiry date makes you re-approve on a calendar rather than when reality changes. The declaration travels with the guard, so it cannot survive it. **(B) Leave the gate strict and tell developers not to run it.** That is precisely the pressure D-51 rejects, and a gate people are told to ignore is a gate that gets deleted. **(C) Make the gate refuse to render a verdict off the CI platform.** More honest than a false failure, but it leaves the developer with nothing, and it would also have hidden the three-test defect — which was found only because the local run failed and the reasons were read. **(D) Write it up as a named gap and change nothing.** It would have left a real defect in the tree and a design claim with no implementation behind it. **(E) Match skip reasons against a prose vocabulary the gate owns.** Fragile in both directions, and it makes the gate the authority on wording rather than the test the authority on its own reason.
- **Costs accepted.** **(1) The tag is self-declared,** so a determined author can write `platform-only: posix` into a capability skip and be believed on Windows. Three things contain it: the satisfied-requirement clause means the lie fails in CI, the permitted set is printed on every run including when empty, and the declaration is a source edit a reviewer sees. It is not airtight, and saying so is better than implying it is. **(2) A second `go test` run in the `agent` job**, roughly doubling that job's test time, because `-json` and `-race -shuffle=on` answer different questions and combining them would weaken one. **(3) The six POSIX assertions are still unproven on Windows** — that is a property of the platform, not of this decision, and the gate now says so out loud instead of failing without explanation.
- **CI job that proves it.** `agent`: `go test -json -tags=integration ./... > ../agent.jsonl` then `python scripts/check-no-skips.py --go agent.jsonl`. Its own tests are `backend/tests/meta/test_check_no_skips.py` — **23** tests, including the control that the identical report judged `--os linux` FAILS, that an undeclared skip fails on windows, linux and darwin alike, that a vocabulary typo fails rather than exempting, that no test name appears in the gate's source, that a clean Go report needs no Go toolchain, and two source scans asserting no `iac` test uses `os.Setenv` or restores a variable by deleting it.

---

#### D-69 — `verify-chain` keeps its meaning; a *gate* states its expectation with `--require-rows`

- **Status:** Accepted · **Date:** 2026-07-31 · **Implements:** §11.9's chain verification and §13.4's `make verify-chain`, for group 7's close-out
- **Context.** Group 7's close-out called for `make verify-chain` run end to end against the real Compose stack. Run for the first time it produced two outputs and neither was usable as evidence. Against a **fresh** stack it died with a forty-line SQLAlchemy traceback ending in `UndefinedTableError: relation "audit_events" does not exist`, because nothing in Compose applies migrations — deliberately, since §6.4 splits schema ownership from the application role. Against a **migrated but empty** stack it printed `verify-chain: OK - 0 row(s) of the untenanted chain reproduce their stored hashes` and exited **0**. Every word true; worth nothing. A CI step gating on that exit code is green over an empty table, which is §0.4.5's `VACUOUS` row and §0.4.4's empty selection arriving in a §13.4 operator command. `verify_cli.py` shipped with leaf 7.6 carrying **no tests at all**, which is why neither was noticed.
- **Decision.** **(1)** The command's meaning is unchanged: for an operator an empty chain has nothing that fails to reproduce, and `OK - 0 row(s)` is the honest answer. **(2)** The *caller* states its expectation — `--require-rows N` exits 1 when fewer than N rows were checked, default 0. `compose-smoke` passes 1 after writing records, so its green is a statement about a chain that exists. **(3)** A missing `audit_events` is a named diagnostic naming the remedy (`alembic upgrade head`) and exit **2**, distinct from 1 for a divergence, so a script can tell "not set up" from "tampered". **(4)** `compose-smoke` gains four steps: migrate to head, assert an empty chain does **not** satisfy `--require-rows 1`, run `scripts/audit-chain-smoke.py`, then `make verify-chain rows=1`. **(5)** That script is the end-to-end claim: it writes records through the real `AuditWriter`, verifies them, then **tampers one row and requires the verifier to object at exactly that seq**, and finally restores the row. The tamper runs as the table owner with `0007`'s trigger disabled, which is `0007`'s own threat model — the application role cannot do it, and `test_0007_audit.py` asserts the REVOKE.
- **Alternatives rejected.** **(A) Make a zero-row chain exit non-zero.** It would make the command lie to an operator on a fresh install, turning "I have nothing to audit yet" into an error. The vacuity is a property of the *caller's* expectation, not of the chain. **(B) Have the CI step grep the output for `0 row(s)`.** String matching in YAML against a message written for humans, breaking the moment the wording changes, with no test of its own. **(C) Seed the chain from a SQL fixture.** Faster, and it would make the verifier agree with the fixture by construction rather than with the writer — the pattern F defect the mutation harness exists to catch. **(D) Stop at "verify-chain ran and said OK".** That proves the command executes; a `verify_chain` that returned `ok` unconditionally would satisfy it, which is the definition of a decorative check. **(E) Have Compose migrate on backend start.** It would give the application role's container the schema-owning step, collapsing §6.4's two-role split that `check-db-roles.py` exists to keep apart.
- **Costs accepted.** **(1) `compose-smoke` now writes to the database it starts**, so the job is no longer read-only against its own stack. Contained by the script restoring the row it altered and re-verifying afterwards, and by the stack being torn down regardless. **(2) The tamper needs `ALEMBIC_DATABASE_URL`** in the container, so the smoke script holds the owner credential for the length of one UPDATE; it is the same credential migrations already use in that container. **(3) `--require-rows` is a flag a caller can forget,** which is weaker than the command refusing on its own. The compose step asserts the guard *fires* on an empty chain before relying on it, so a forgotten flag is visible in the job that depends on it. **(4) Roughly a minute added to `compose-smoke`** for the migration and the four runs.
- **CI job that proves it.** `compose-smoke`, four steps as above. Its own tests are `backend/tests/integration/test_audit_verify_cli.py` — **9** tests, the file the CLI never had: an empty chain OK by default and failing `--require-rows 1`; a populated chain passing at exactly its row count and failing at one more, so the guard is a comparison rather than a boolean; both negative-argument refusals; and the unmigrated case driven against the cluster's `postgres` maintenance database, which genuinely lacks the table, rather than by raising the exception the `except` clause matches.

#### D-70 — a non-transit audit write is confined by the SHAPE of its record, not only by its location

- **Status:** Accepted · **Date:** 2026-07-31 · **Implements:** Appendix A.1's audit calls, §11.9, §2.2.1, keeping Appendix B Q-04 falsifiable
- **Context.** `AuditWriter.append` carries `@mutation_primitive`, so `scripts/check-chokepoint.sh --python` requires every caller to be lexically inside `src/governance/` or to hold a `MutationAuthority`. Appendix A.1 requires `Audit(actor, "pairing_code_issued", …)` on issue and `Audit(system, "pairing_failed", …)` / `Audit(r.issuer, "device_paired", …)` on the two exchange branches, and `src/auth/devices.py` can satisfy neither condition: minting an authority needs a change set, an approval id, a blast radius and an audit sequence, and a pairing has none of them. It is not a mutation that traversed the chokepoint and no plumbing can make it into one. Verified against `scripts/chokepoint_graph.py:512-518`: a call from `devices.py` classifies `no-authority` and fails the build.
- **Decision.** `audit_events` carries two kinds of row and the distinction is a **type**. **(1)** A *transit* record keeps `AuditDraft` — any `action`, any `resource_kind`, a before/after pair — and is writable only through `AuditWriter.append`, whose callers remain governance-only. **(2)** A *device-lifecycle* record is a new `audit/device_log.py::DeviceAuditEvent` with a closed four-action vocabulary, `resource_kind` as a **constant rather than a field**, no `before_state` and no `after_state`, and a `details` mapping restricted to a closed **key** set. **(3)** `tests/meta/test_device_audit_shape.py` asserts the two vocabularies are disjoint, that both are non-empty, that no permitted detail key names a credential, and — parametrised over every `GovernanceAction` — that no transit action can be constructed as a device event. **(4)** The write itself happens in `governance/device_audit.py::GovernanceDeviceAuditRecorder`, which is where §2.2.1's positional authorisation lives, and `DeviceService` depends on a `DeviceAuditRecorder` **Protocol declared in `audit/`** so the import direction stays `governance → auth`.
- **Why the shape and not just the location.** Moving the write into `governance/` alone satisfies the checker and nothing else. A governance-positioned helper taking an unrestricted `AuditDraft` is a second entry point to the whole audit vocabulary, and **Q-04 quantifies only over transits it drives itself** — it counts rows for a project after running transits, so it cannot see a transit-shaped row written by another writer. It would keep passing while "exactly one audit row per transit" stopped holding. That is chapter 5's defect in a new location: a check that can no longer fail. With the shape closed, the transit-shaped fields are reachable only through the authority-bearing path, so Q-04 remains a claim about the only writer that can produce the row it counts.
- **Alternatives rejected.** **(A) Give `DeviceService` a `MutationAuthority`.** Unbuildable, per the context above. **(B) A second, non-primitive `append_operational(draft)` on `AuditWriter`.** Exactly the Q-04 vacuity above, plus it needs the chain logic shared through an undecorated private core that anything in `src/**` could import. **(C) A governance-positioned helper taking `AuditDraft`.** Same vacuity, differently located. **(D) Import `governance` from `auth/devices.py`.** No Ruff rule forbids it, but it inverts the layering — `governance/chokepoint.py` already imports `auth.devices` — and a package cycle in the dependency diagram is a cost paid for nothing the Protocol does not give. **(E) A `Mapping[str, str]` `details` with no key restriction.** Q-17 requires that the code appear in no audit row; a free mapping makes that a review obligation forever rather than a construction.
- **Costs accepted.** **(1) "Every row in `audit_events` came through the chokepoint" is no longer true;** what is true is "every *transit-shaped* row did". That is a genuine narrowing of a sentence people quote, and it is the price of Appendix A.1 requiring a pairing to be auditable. The compensation is that the narrowing is mechanical. **(2) `governance/` gains a module that mints nothing.** Its position is doing real work — it is the only positional authorisation §2.2.1 offers — but a reader now has to know that not everything in `governance/` is authority. Answered in that file's docstring rather than left to be discovered. **(3) A new detail key is an edit to a `frozenset` in `audit/device_log.py`,** which is friction by design: it is exactly the review the addition deserves. **(4) Two `append` call sites instead of one,** so Q-03's "governance only" assertion moved from a file list to a package prefix.
- **Tests that prove it.** `tests/meta/test_device_audit_shape.py` — 26 tests. `tests/property/test_q03_chokepoint_unbypassable.py` asserts both call sites are inside `src/governance/`, and `check-chokepoint.sh --python` reports them as `[governance]`.

#### D-71 — three problem types beyond Appendix C.1's table, because the exchange can reach three states C.1 registered nothing for

- **Status:** Accepted · **Date:** 2026-07-31 · **Implements:** Appendix C.1, §14.6, §3.1
- **Context.** The pairing exchange has states C.1's table does not name. Redis holds both §14.6 buckets **and** the single-use consume script, so a Redis outage refuses the exchange either way and arrives as an unhandled 500. A CSR can be unreadable, self-signature-invalid, or carry the wrong key type. And `DELETE /api/v1/agents/{id}` can name an id that does not exist. `core/errors.py`'s registry is asserted **equal** to C.1 by `tests/unit/test_errors_phase1.py`, so an unregistered type is a build failure — which is the mechanism working: adding one is a design edit, not a raise-site invention.
- **Decision.** Register `pairing-unavailable` (503), `csr-invalid` (400) and `device-not-found` (404), each with a row in C.1 carrying the decision marker, following D-53's and D-56's precedent exactly.
- **Why each, and why not the nearest existing type.** `pairing-unavailable` must not be `pairing-rate-limited`: a 429 tells a client a rate was measured and to slow down, when in fact no rate was measured at all — the D-56 mistake, one layer along. `csr-invalid` must not be folded into `pairing-code-invalid`: the CSR check runs **before** the code is consumed, so distinguishing it reveals nothing about whether the code exists, and folding it would leave a broken agent unable to tell a client bug from a wrong code. `device-not-found` is a plain 404 rather than the non-disclosing `forbidden` body because the route is admin-only and an admin may already read every device; §4.2's enumeration rule constrains the 403 body, not an admin-scoped 404.
- **Alternatives rejected.** **(A) Let the Redis outage surface as a 500.** It is a dependency outage, not an internal invariant violation, and 500 is the one status a client must not retry on. **(B) Report the outage as 429 with a `Retry-After`.** A lie a client cannot detect. **(C) Return `pairing-code-invalid` for a malformed CSR.** It hides a client bug behind a credential error and would send agent authors hunting for a code problem they do not have. **(D) Reuse `unauthenticated` for the missing device.** The caller is authenticated; the resource is absent.
- **Costs accepted.** **(1) Three more type URIs in a public vocabulary,** which is a compatibility surface: once published, `type` must never change meaning. **(2) `csr-invalid` is a distinguishable answer on an unauthenticated route,** so an attacker learns that its CSR was well-formed. That is information it already has — it built the CSR — and it learns nothing about any code. **(3) The registry-equals-appendix test now depends on three rows in `design.md`,** so a careless revert of the design breaks the backend build. That coupling is the point.

#### D-72 — the CSR is validated before the pairing code is consumed, and §3.1's `fingerprint` field is given a checked definition

- **Status:** Accepted · **Date:** 2026-07-31 · **Implements:** §3.1's exchange request, Appendix A.1, Appendix B Q-17
- **Context.** Appendix A.1 orders the exchange `RedisEval(CONSUME_SCRIPT, …)` then `SignCSR(internal_ca, csr, …)`. Taken literally, a caller holding a valid code and sending a malformed CSR spends the code's single use and gets nothing — the code is deleted by the consume script and cannot be retried. Separately, §3.1's request body lists a `fingerprint` member and defines neither what it fingerprints nor what the server does with it.
- **Decision.** **(1)** Every request-shaped check runs **before** the `EVAL`: the CSR must parse as PEM, its self-signature must verify (proof of possession of the private key), and its key must be EC P-256 as §3.1 fixes. **(2)** `fingerprint` is defined as the SHA-256 of the CSR's SubjectPublicKeyInfo DER in lowercase hex, and `exchange` **checks** it against the CSR with `hmac.compare_digest`, rejecting a mismatch. **(3)** The CA call stays where A.1 puts it, after the consume — it is the one step that cannot precede consumption, because it issues a credential.
- **Why.** Proof of possession is the check with real security content: without it, an attacker who intercepted a CSR could pair a device whose private key it does not hold, and every later mTLS handshake would be made by someone else. And a field the server accepts and ignores is worse than no field — it reads like a bound and is not one, which is the shape §0.4.5 exists to remove.
- **Alternatives rejected.** **(A) Follow A.1's order literally.** A broken agent burns a valid code on every retry, and the operator sees "the code stopped working" with no way to tell why. **(B) Accept `fingerprint` and ignore it.** Decorative. **(C) Drop `fingerprint` from the request.** The design lists it; dropping a documented field to avoid defining it is the wrong direction. **(D) Make it optional and check it when present.** An optional bound is not a bound, and the agent (leaf 8.3) computes it in three lines either way.
- **Costs accepted.** **(1) A distinguishable `csr-invalid` on the public route** (see D-71's cost 2). **(2) The reordering is a documented deviation from A.1's pseudocode,** so a reader comparing the two finds a difference; the docstring on `exchange` states it and why. **(3) `fingerprint` is now a required field the agent must compute correctly,** which is one more way a client can be wrong — and it fails loudly rather than silently, which is the trade being made. **(4) The 5-attempt burn branch becomes harder to reach,** because the failures that could plausibly increment a live code's counter now happen before the counter exists. It remains reachable by repeated presentation of one digest inside the window, and `test_the_burn_branch_deletes_the_code` drives that state directly with a control below the cap alongside it.

#### D-73 — the internal CA discards the CSR's subject, issues no SAN, and is not the authorisation input

- **Status:** Accepted · **Date:** 2026-07-31 · **Implements:** §3.1's certificate issuance, §11.2, §14.2, §14.3
- **Context.** §3.1 has the agent build its CSR **before** the exchange, annotating it `CN=device_id-less` — the device id does not exist until the exchange succeeds. So the CSR's subject is, by construction, not the identity anyone wants, and something has to decide what the certificate says instead. Three smaller questions come with it: whether to add a SAN, what the certificate's key usages should be, and what relationship the certificate bears to authorisation.
- **Decision.** **(1)** The CA **discards** the CSR's subject and issues `CN=<device_id>`. **(2)** No `subjectAltName`. **(3)** `basicConstraints CA:FALSE` and `keyUsage digitalSignature`, both critical, plus `extendedKeyUsage clientAuth` only. **(4)** The certificate is a **precondition**, not the authorisation input: §3.1's handshake checks the chain *and* compares `agent_devices.cert_fingerprint`, and this design keeps those two checks separate rather than collapsing them.
- **Why each.** Discarding the subject is the safer direction independent of the timing problem: a CSR reaches the backend on an unauthenticated route, and a CA that copies caller-supplied data into the identity field of a credential is a CA that lets the caller choose who it is. What the CSR contributes is the public key and the proof of possession. No SAN, because a SAN exists for **name** verification and nothing verifies a name here — the fingerprint comparison is a stronger binding than any name, and an unused extension is surface. `clientAuth` alone, because a certificate that could also serve as a *server* credential is one an attacker who steals it can use to impersonate the backend to another agent. And keeping the chain check separate from the fingerprint check matters because they answer different questions: a chain check alone accepts **any** certificate this CA ever issued, including one issued to a different device, so neither is sufficient alone and `test_the_chain_check_does_not_answer_which_device_a_certificate_belongs_to` states it.
- **Alternatives rejected.** **(A) Honour the CSR's subject.** Caller-chosen identity. **(B) Require the agent to re-submit a CSR carrying the device id after the exchange.** A second round trip, a second unauthenticated-ish surface, and the device id is not secret so it buys nothing. **(C) Add a URI SAN like `forgeops://device/<uuid>`.** Invents a naming scheme no verifier consults; §14.3 is explicit that this is not SPIFFE, and a SPIFFE-shaped URI would imply otherwise. **(D) Issue a CRL or an OCSP responder.** Revocation is Redis-authoritative and per message (Q-16), which is strictly faster than any CRL an agent would poll, for credentials that expire in a day.
- **Costs accepted.** **(1) Nothing verifies the certificate by name,** so a future component that wants name-based verification has to add a SAN and a decision. **(2) `path_length=0` on the development CA** means it can never mint an intermediate; a production PKI (**OQ-31**) will need its own hierarchy rather than an extension of this one. **(3) The subject rewrite means the CSR and the certificate disagree,** which is surprising the first time an operator diffs them; the module docstring is where that is answered. **(4) A single-level CA has no key-rotation story** beyond regenerating and re-pairing every device, which is acceptable while the CA is a development artifact and is part of what OQ-31 has to settle.

#### D-74 — `rotate_certificate` takes a new CSR; §11.2's signature is extended rather than satisfied literally

- **Status:** Accepted · **Date:** 2026-07-31 · **Implements:** §11.2's `rotate_certificate`, §3.1's `cert_renewing` state
- **Context.** §11.2 sketches `async def rotate_certificate(self, *, device_id: uuid.UUID) -> CertificateBundle` — no CSR. Implemented literally, the CA would have to reissue over the device's **existing** public key, which means either the backend stores every device's public key (a store this design does not have) or the certificate is issued over a key the CA cannot see.
- **Decision.** `rotate_certificate(session, *, device_id, csr_pem)`. The agent generates a fresh P-256 pair and submits a new CSR, exactly as it did at pairing. Rotation is refused for any device that is not `active`, and it **replaces** `cert_serial` and `cert_fingerprint` rather than appending, so the previous certificate stops being accepted the moment the transaction commits. There is deliberately no REST route: rotation travels over the live authenticated session (§3.1), because a device certificate handed out over a route authenticated by anything other than the device's current certificate would be a second, weaker enrolment path.
- **Why.** A short-lived certificate whose key never changes gives up most of what short-lived buys — a key stolen once stays useful for as long as the device does. Rotating the key is the point of rotating the certificate.
- **Alternatives rejected.** **(A) Store each device's public key so §11.2's signature can stand.** A new column and a new custody question, in exchange for a weaker credential. **(B) Re-run the whole pairing flow on renewal.** It needs a human to issue a code every 24 hours, which is not a renewal, it is re-enrolment. **(C) Allow rotation for a `policy_stale` device.** Tempting, since `policy_stale` is a live device — but it is a **mutation-blocking** state (§3.7) and extending its permissions during a blocked period is the wrong direction; the agent reloads its bundle, returns to `active`, and then rotates. **(D) Expose it as `POST /api/v1/agents/{id}/certificate`.** The second enrolment path above.
- **Costs accepted.** **(1) The design document's signature and the code's disagree,** which is exactly the kind of drift §0.1's authority order exists to prevent — so it is recorded here as a decision rather than left as a difference a reader has to reconcile. **(2) The method has no caller until leaf 8.4's hub,** so its correctness rests entirely on its tests until then; `TestRotation`'s six cases are written on that basis. **(3) Replacing rather than appending means there is a window of one transaction** in which the old certificate is still on disk in the agent and the new fingerprint is already in the row; the agent installs the new one before acknowledging, and a failure there costs a reconnect, not a lockout, because the CA is still reachable.



---

### 17.2 Open questions

**Status: no open question blocks the start of implementation.** Each carries a recommendation this design already implements, so work can begin and the choice stays visible and reversible. Phase 0 questions falling due in this phase are dispositioned in the second table.

#### New questions (OQ-22 onward)

| # | Question | Why it matters | Recommendation (implemented) | Blocking? |
|:---|:---|:---|:---|:---|
| **OQ-22** | Voyage Code 3 and `voyage-rerank-2` are API-only. Who supplies the key, what is the monthly budget, and what serves the reranker on the air-gapped `self_hosted` path — which by definition cannot call Voyage? | §1.3 embeddings and §1.5's "cheapest quality lever" both depend on a paid API, and the `self_hosted` tier exists precisely for codebases that must not leave the network | Voyage via BYO-key with a per-project budget (FR-06) for the hosted path; `EMBEDDING_BACKEND=bge_m3` (D-48) for air-gapped embedding; **no reranker on the air-gapped path in Phase 1** — retrieval is fused-order only, with `retrieval_degraded` recorded. A local `bge-reranker-v2-m3` is the Phase 2 answer | No |
| **OQ-23** | Which tier runs the LLM-as-judge rubric, and is per-generation judge cost acceptable? | The rubric runs on every accepted generation; on `high_coding` it could cost more than the generation it grades | `medium_value` (Claude Sonnet 5 / DeepSeek V4) at temperature 0. Confirm, or disable the rubric by default and make it opt-in per project | No |
| **OQ-24** | Confirm deferring native Anthropic/Google codecs — and with them `cache_control` prompt caching, extended thinking and Google context caching — to Phase 2 (D-42) | Prompt caching is a large cost lever for RAG prompts that share a long prefix | Defer. Phase 1's L3 prefix cache captures part of the benefit locally; native codecs are a Phase 2 cost-optimisation project with their own evidence bar | No |
| **OQ-25** | When a blocking validator cannot run (Trivy absent, no agent connected, no cluster), is the result fail-closed or advisory? | Decides whether a missing tool becomes a silent pass on the mutation path | Fail closed when the change-set's blast radius is `infrastructure`; advisory warning otherwise. `dryrun_unavailable` is always fatal, because a change-set that was never dry-run must not be presentable as validated | No |
| **OQ-26** | Is the 0600-file fallback acceptable where no OS keychain exists (headless Linux without a Secret Service, containers, CI)? | It is the storage of a credential that can drive file writes on the host | Accept, with `agent doctor` reporting the degraded mode explicitly and the file mode asserted at every load. Alternative — refusing to pair without a keychain — would make the agent unusable in exactly the environments Phase 2 targets | No |
| **OQ-27** | The agent binary grows ~25–45 MB under D-29 plus the Helm SDK and `compose-go`. Acceptable, or should the Helm SDK move behind a build tag? | Download size is a real adoption cost for a tool users install manually | Accept for Phase 1 and measure the six real artifacts in the first release. If unacceptable, a `noheml` build tag drops the largest single contributor and `helm` validation degrades to the external binary when present | No |
| **OQ-28** | The `e2e` job uses a fixture OIDC issuer for speed while a separate `auth` job exercises real Authentik. Acceptable split? | The criterion-10 journey would otherwise pay Authentik's cold start on every run | Accept the split: `auth` covers the real code+PKCE flow and the RBAC matrix; `e2e` covers the product journey. Both are required checks, so neither path is unproven | No |
| **OQ-29** | Not every tree-sitter grammar publishes a prebuilt `.wasm`. Is building the remainder in a digest-pinned container acceptable, and what if one does not build reproducibly? | This is D-29's largest execution risk and it lands in the supply chain, not just the build | Vendor prebuilt artifacts where they exist; build the rest in a digest-pinned container with a reproducibility check in `lock-integrity`. If a grammar will not build reproducibly, omit that language's AST support and fall back to line-based chunking for it — degraded retrieval, recorded, not a broken phase | No |
| **OQ-30** | Audit retention, partitioning and export. No authority names a retention period | `audit_events` grows without bound by design, and NFR-14 gives no duration | Indefinite retention in Phase 1 with no delete path; monthly partitioning and an export endpoint in Phase 2. Confirm that indefinite is the intended posture rather than an oversight | No |
| **OQ-31** | Production custody of the internal CA private key that signs device certificates | It is the highest-value new secret in the phase; possession allows minting an agent identity | Development: the local untracked `.env`. Production: an external KMS or Infisical with the key never on an application host — a Phase 2 deployment decision. Phase 1 must not ship a default that is acceptable in production | No |
| **OQ-32** | Research §C10 recommends LangGraph (agent loops) and LlamaIndex (indexing); Phase 1 uses neither | A deliberate deviation from a named recommendation, and it should be visible | Defer both. Phase 1's loop is three bounded iterations with one model call each and a provable termination property (Q-08); a graph framework would replace fifteen provable lines with a large dependency and a second control-flow model. Indexing is chunk-and-embed with the agent parsing. Revisit when multi-agent orchestration actually arrives | No |
| **OQ-33** | Rotating `ENVELOPE_PEPPER` invalidates every sealed `agent_devices.envelope_key_enc` (D-62), and Phase 1 ships no re-seal path | A pepper rotation is the response to a suspected pepper leak, and the leak is exactly when an operator cannot afford a surprise | **Named gap, deliberately deferred.** In Phase 1 a pepper rotation requires every device to re-pair, because the pairing-code and device-token HMACs are invalidated by the same rotation regardless — so a re-seal alone would not restore a working fleet. Phase 2 adds a two-key window: derive under both the old and the new label, unseal with either, re-seal on next write, then drop the old. Until then the runbook answer is "rotate and re-pair", stated rather than discovered | No |

#### Phase 0 questions dispositioned by this phase

| # | Phase 0 state | Phase 1 disposition |
|:---|:---|:---|
| **OQ-3** Python logging library | stdlib implemented | **Confirmed unchanged.** No new dependency; D-27's redaction extended (§7.2) |
| **OQ-4** PBT libraries | hypothesis / rapid / fast-check | **Confirmed unchanged.** All three used by Appendix B |
| **OQ-6** Windows process-tree termination | `taskkill /T /F` | **Resolved → D-37** (Job Objects, still cgo-free) |
| **OQ-7** GitHub App vs PAT | `EnvTokenSource` | **Resolved → D-38** (`AppInstallationTokenSource` behind the same seam) |
| **OQ-10** D2 version | deferred | **Unchanged.** No D2 dependency; Mermaid used throughout |
| **OQ-11** `DEEP_RESEARCH_SYNTHESIS.md` absent | nothing invented | **Unchanged.** Phase 1 cites only what exists; where a detail was only ever in that file, this design either uses PRD §2.1a / `phases.md` or records a question |
| **OQ-13** JWT/JWKS library | `pyjwt[crypto]` | **Confirmed unchanged**, now at the committed `2.13.0` (§15.9) |
| **OQ-15** tenant seam | nullable, no RLS | **Partially resolved → D-35.** Middleware row 6 filled and `SET LOCAL` issued; RLS and `NOT NULL` remain Phase 2 |
| **OQ-16** durable engine at P2 | seam kept neutral | **Deliberately re-deferred.** ARQ lands behind the unchanged Protocol (D-32); Temporal and Inngest both stay open and no Phase 1 payload assumes either |
| **OQ-17** coverage gate or goal | goal | **Resolved → D-31** (per-component gate at ≥70 %) |
| **OQ-18** missing build-rules document | `docs/development.md` designated | **Unchanged** |
| **OQ-20** blast-radius source | `MCP_AGENT_BLAST_RADIUS` | **Resolved → D-39** (attested identity; env var demoted to a dev default and rejected in production) |
| **OQ-21** repository visibility / secret scanning | owner's call | **Unchanged.** GitHub secret scanning is still disabled, so the local gitleaks gate is the only gate (§14.5) |

---

## 18. Progress Tracking

`PROGRESS.md` remains the durable progress record and is updated **in the same commit as the work it describes**. Phase 1 obligations:

1. **Phase status table:** Phase 0 stays `completed`; Phase 1 moves to `in-progress` with the first Phase 1 commit. Statuses remain exactly `completed` / `in-progress` / `not-started` / `blocked`, and task statuses exactly `done` / `in-progress` / `pending`.
2. **Task list:** replaced by the Phase 1 list, with every `phases.md` §1.1–§1.11 deliverable appearing as at least one row. The Phase 0 list is **retained** above it — the file is a record, not a dashboard, and deleting completed history would remove the evidence the Phase 0 criteria table points at.
3. **Completion criteria:** the 14 Phase 1 criteria, verbatim, each with an evidence column naming a real CI run, command output or artifact path. Appendix E is the source of the evidence bars.
4. **Inherited-debt table:** a new section tracking §0.5's five items to closure, because they are prerequisites rather than cleanup and their status must be visible.
5. **Property coverage:** a `Q-01 … Q-31` table mirroring Phase 0's `P-01 … P-15` table, with file locations **and** the negative-control row from `mutations.toml`, so a reader can see both that a property exists and that it is non-vacuous.
6. **Decision log:** D-28 … D-52 appended. The Phase 0 rows stay. Resolved open questions move into the decision log rather than being deleted.
7. **Open questions:** OQ-22 … OQ-32 added; the Phase 0 dispositions of §17.2's second table applied in place.
8. **Deviations and outstanding items:** carried forward, with each closed item marked closed and dated rather than removed — including the honest ones (Rekor unreachable from the workstation, GitHub attestations unavailable on a private repository, secret scanning disabled, `agent-autonomy.md` untracked).

The rule that made Phase 0's record trustworthy is unchanged and is worth restating: **a criterion is `done` only when its evidence is reproducible by someone else.** `REVIEW-PHASE-0.md` had to correct rows 10, 11 and 14 because their evidence was a code-reading claim or a one-off manual observation. Phase 1's evidence column names a CI job or a command, never a reading of the source.


---

## Appendix A — Algorithmic Pseudocode with Formal Specifications

Cross-language algorithms with preconditions, postconditions and loop invariants. These are the statements Appendix B turns into property tests.

### A.1 Pairing-code issue and exchange

```pascal
ALGORITHM IssuePairingCode(project, actor, now)
PRE:  actor is a verified Principal with pair permission on project
POST: exactly one live code exists for project; only its HMAC is stored
BEGIN
  RevokeLiveCodesFor(project)                    // at most one live code per project
  code   ← RandomSymbols(CROCKFORD32, 6)         // from a CSPRNG, never a PRNG
  digest ← HMAC_SHA256(pepper, code)
  device ← InsertDevice(project, status ← PENDING, pairing_token_hmac ← digest,
                        pairing_expires_at ← now + TTL)
  RedisSetEx("pair:" ‖ digest, TTL,
             {project, tenant, issuer ← actor.sub, device, attempts ← 0})
  Audit(actor, "pairing_code_issued", project, reason ← "operator initiated pairing")
  RETURN (code, device.id, now + TTL)            // the code is returned ONCE and never stored
END

ALGORITHM ExchangePairingCode(code, csr, meta, client_ip, now)
PRE:  none — this is the one unauthenticated route (§4.4)
POST: on success the code no longer exists, and a device credential set is issued
      exactly once; on failure nothing is issued and the attempt is counted
BEGIN
  IF NOT RateLimitAllow("pair_ip:" ‖ client_ip) THEN RETURN Problem(429, "pairing-rate-limited")
  IF NOT RateLimitAllow("pair_global")          THEN RETURN Problem(429, "pairing-rate-limited")

  digest ← HMAC_SHA256(pepper, code)             // constant-time by construction
  // One Lua script performs: fetch, increment attempts, burn on exceed, delete on
  // success. Atomicity is what makes single-use true under concurrency.
  r ← RedisEval(CONSUME_SCRIPT, "pair:" ‖ digest, MAX_ATTEMPTS)
  IF r = MISSING OR r = EXPIRED OR r = BURNED THEN
    Audit(system, "pairing_failed", r.project?, reason ← r.kind)   // never logs the code
    RETURN Problem(401, "pairing-code-invalid")
  END IF

  ASSERT r.attempts ≤ MAX_ATTEMPTS
  token   ← Random(32)
  envkey  ← Random(32)
  cert    ← SignCSR(internal_ca, csr, not_after ← now + CERT_TTL)
  UpdateDevice(r.device, status ← ACTIVE,
               device_token_hmac ← HMAC_SHA256(pepper, token),
               envelope_key_enc  ← AES_256_GCM(app_key, envkey),
               cert_serial ← cert.serial, cert_fingerprint ← SHA256(cert),
               pairing_token_hmac ← NULL)        // the code cannot be reused, even in the DB
  Audit(r.issuer, "device_paired", r.project, after ← {device, fingerprint, serial},
        reason ← "pairing code exchanged")
  RETURN {device, token, envkey, cert, ca_bundle, active_bundle, active_bundle_digest}
END
```

**Preconditions:** `MAX_ATTEMPTS ≥ 1`; `TTL > 0`; the CSPRNG is seeded from the OS.
**Postconditions:** a code is consumable **at most once** across all replicas (the Lua script is the serialisation point); a burned or expired code is indistinguishable in the response from an unknown one; no failure path reveals whether a code existed; the code value appears in no log, audit record or database column.
**Q-17** quantifies this over concurrent exchange attempts.

### A.2 Envelope canonicalisation, signing and verification

```pascal
ALGORITHM CanonicalBytes(env)
PRE:  env contains only objects, arrays, strings, integers and booleans — NO floats
POST: output is byte-identical for semantically identical envelopes, in both runtimes
BEGIN
  e ← env WITHOUT member "signature"
  RETURN JCS(e)             // RFC 8785: UTF-8, sorted members, minimal escaping
END

ALGORITHM SignEnvelope(env, key)
BEGIN
  input ← "forgeops-envelope-v1" ‖ 0x00 ‖ CanonicalBytes(env)   // domain separation
  RETURN Base64Url(HMAC_SHA256(key, input))
END

ALGORITHM VerifyEnvelope(raw, key, state, now)
PRE:  state holds last_seq and a nonce set covering at least MAX_AGE
POST: returns Verified only if ALL six checks pass; no check has a side effect
      before the signature check succeeds
BEGIN
  env ← ParseStrict(raw)                         // unknown members reject
  IF env.v ≠ 1 THEN RETURN Err("envelope-unsupported-version") END IF

  // 1. freshness
  IF now > env.not_after + SKEW              THEN RETURN Err("envelope-expired") END IF
  IF env.not_after - now > MAX_AGE + SKEW    THEN RETURN Err("envelope-expired") END IF

  // 2. signature — BEFORE any state mutation, so an unauthenticated caller cannot
  //    advance last_seq and lock out the real backend (a DoS through a defence)
  expect ← SignEnvelope(env, key)
  IF NOT ConstantTimeEqual(expect, env.signature) THEN RETURN Err("envelope-signature-invalid") END IF

  // 3. ordering, then 4. uniqueness — both mutate state, both after authentication
  IF env.seq ≤ state.last_seq                THEN RETURN Err("envelope-replayed") END IF
  IF env.nonce ∈ state.nonces                THEN RETURN Err("envelope-replayed") END IF

  // 5. policy binding
  IF env.policy_context.bundle_digest ≠ state.bundle_digest THEN
    RETURN Err("policy-bundle-stale")
  END IF

  // 6. operation is in the closed catalogue
  IF env.operation ∉ OPERATIONS              THEN RETURN Err("operation-unknown") END IF

  state.last_seq ← env.seq
  state.nonces   ← state.nonces ∪ {env.nonce}    // bounded LRU, ≥ MAX_AGE of history
  RETURN Verified(env, now)
END
```

**Postconditions:** verification is total (every input yields `Verified` or a typed error, never a panic); no mutation is performed on any failure path; a replayed, reordered or expired envelope is rejected with the state unchanged except for the authenticated-and-accepted case. **Q-14** covers canonicalisation and signature; **Q-15** covers replay; **Q-07** covers the digest binding.

### A.3 Governance chokepoint transit

```pascal
ALGORITHM Submit(request, principal, now)
PRE:  principal is verified (there is no unauthenticated entry to this algorithm)
POST: either a signed envelope was minted after ALL six stages, or no envelope
      exists and exactly one audit record explains why
BEGIN
  // 0. admit
  IF principal = ⊥ THEN RETURN Problem(401, "unauthenticated") END IF
  project ← Load(request.project_id)
  device  ← ActiveDeviceFor(project)
  IF device = ⊥                      THEN RETURN Problem(409, "agent-not-connected") END IF
  IF device.status = REVOKED         THEN RETURN Problem(409, "device-revoked") END IF
  IF device.bundle_digest ≠ ActiveDigest(project) THEN
    AuditDenied(principal, "policy-bundle-stale", project)
    RETURN Problem(409, "policy-bundle-stale")
  END IF

  // 1. policy — fail closed
  decision ← OPA.Evaluate(GovernanceInput(request, project, principal, now))
  IF decision = UNDEFINED THEN                    // D-25's lesson, applied here
    RETURN Problem(503, "governance-policy-undefined")
  END IF
  IF decision.result = DENY THEN
    AuditDenied(principal, "policy-denied", project, reason ← decision.reason)
    RETURN Problem(403, "policy-denied")
  END IF

  BEGIN TRANSACTION
    // 3. change-set compilation (2's outcome may need it persisted)
    cs ← InsertChangeSet(project, principal, request, status ← VALIDATING,
                         policy_bundle_digest ← device.bundle_digest)
    InsertChangeItems(cs, request.items)          // with pre-image hashes

    // 4. blast radius — deterministic, monotone (P-11), no LLM
    br ← SemanticPlanAnalyzer.Analyse(PlanFrom(cs))
    UpdateChangeSet(cs, blast_radius ← br)
    IF br.verdict = BLOCK THEN
      UpdateChangeSet(cs, status ← BLOCKED)
      audit_seq ← Audit(principal, "change_set_blocked", cs, before ← ⊥, after ← br,
                        reason ← "blast radius block")
      COMMIT
      RETURN Problem(409, "blast-radius-blocked")
    END IF

    // 2. approval gate
    gate ← ApprovalGate.Submit(br, Context(request, decision, project))
    IF gate = REQUIRES_APPROVAL OR decision.result = REQUIRE_APPROVAL THEN
      UpdateChangeSet(cs, status ← PENDING_APPROVAL)
      audit_seq ← Audit(principal, "approval_required", cs, reason ← gate.reason)
      COMMIT
      RETURN Accepted(202, cs, "approval-required")
    END IF

    UpdateChangeSet(cs, status ← APPROVED)
    // 5. audit — SAME transaction as the state transition (Q-04)
    audit_seq ← Audit(principal, "change_set_auto_approved", cs, after ← br,
                      reason ← decision.reason)
    // 6. rollback handle reserved BEFORE any envelope exists
    handle ← InsertRollbackHandle(cs, device)
  COMMIT

  // mint — only reachable after all six stages
  authority ← MintAuthority(cs, gate.approval_id, device.bundle_digest, br, audit_seq)
  envelope  ← SignCommand(authority, device, operation ← "changeset.apply")
  Hub.Send(device, envelope)
  RETURN Accepted(202, cs, "applying")
END
```

**Preconditions:** OPA reachable or the algorithm fails closed; the analyzer is deterministic; the audit writer joins this transaction.
**Postconditions:** `MintAuthority` is called on exactly one path, and that path is preceded by all six stages; every early return writes exactly one audit record; no envelope exists for a denied, blocked or pending change-set; the rollback handle exists before the envelope does, so a crash between mint and apply cannot leave an irreversible change.
**Loop invariant:** none — the algorithm is straight-line by design. A loop here would be a place to skip a stage.
**Q-03** (unbypassable), **Q-04** (exactly one record), **Q-22** (state legality).

### A.4 The bounded validation-feedback loop

```pascal
ALGORITHM GenerateWithFeedback(project, kinds, budget)
PRE:  MAX_ITERATIONS = 3, fixed at the type level (§7.1)
POST: terminates after at most 3 generation calls; outcome ∈ {ACCEPTED, TEMPLATE, UNAVAILABLE}
BEGIN
  state ← LoopState(attempts_remaining ← 3, findings ← ∅, artifacts ← ⊥)

  WHILE true DO
    INVARIANT 0 ≤ state.attempts_remaining ≤ 3
    INVARIANT generation_calls = 3 − state.attempts_remaining
    VARIANT   state.attempts_remaining strictly decreases on every Continue

    r ← Router.Complete(TierFor(kinds), AssemblePrompt(Retrieve(project), state.findings))
    IF r.outcome = EXHAUSTED THEN
      RETURN TemplateFallback("router-exhausted")   // or UNAVAILABLE if no template
    END IF

    state ← state WITH artifacts ← r.artifacts
    findings ← Pipeline.Run(r.artifacts)            // Syntax → Schema → DryRun → Semantic
    gate ← Decide(Deterministic(findings))          // rubric is NOT a parameter (Q-09)

    step ← Next(state, gate)
    CASE step OF
      Accepted:          rubric ← Judge(r.artifacts)     // advisory, recorded, not consulted
                         RETURN Accepted(r.artifacts, rubric)
      FallbackToTemplate: RETURN TemplateFallback(step.reason)
      Continue:          state ← step.state              // attempts_remaining decremented HERE
    END CASE
  END WHILE
END

ALGORITHM Next(state, gate)
BEGIN
  IF NOT gate.blocked            THEN RETURN Accepted END IF
  IF state.attempts_remaining ≤ 1 THEN RETURN FallbackToTemplate("iteration-bound-exhausted") END IF
  RETURN Continue(state WITH attempts_remaining ← state.attempts_remaining − 1)
END
```

**Postconditions:** `generation_calls ≤ 3` for every input; the loop cannot be re-entered at zero because `Next` returns `FallbackToTemplate` there; no configuration can raise the bound; the rubric is computed only on the accepting path and never reaches `Decide`.
**Variant:** `attempts_remaining` is a well-founded decreasing measure, which is the termination proof. **Q-08**, **Q-09**.

### A.5 Incremental rescan closure

```pascal
ALGORITHM DirtySet(graph, changed, deleted, exports_before, exports_after)
PRE:  graph is the persisted dependency graph before this edit batch
POST: result ⊇ every file whose index content or metadata would differ from a full rescan
BEGIN
  dirty ← changed
  FOR each g IN changed DO
    IF exports_before[g] ≠ exports_after[g] THEN
      dirty ← dirty ∪ Dependants(graph, g)       // only when the EXPORTED surface moved
    END IF
  END FOR
  FOR each f IN changed DO
    IF imports_before[f] ≠ imports_after[f] THEN
      dirty ← dirty ∪ {f}                        // f's own edges must be rewritten
    END IF
  END FOR
  FOR each d IN deleted DO
    dirty ← dirty ∪ Dependants(graph, d)         // dangling edges must be resolved to ⊥
  END FOR

  summaries ← dirty ∪ ⋃_{f ∈ dirty} DirectImporters(graph, f)
  RETURN (dirty, summaries)
END

ALGORITHM ApplyPatch(index, dirty, summaries, deleted, base_version)
PRE:  base_version = index.version, else 409 index-version-conflict
POST: index equals FullRescan(final_tree) — Q-10
BEGIN
  ASSERT base_version = index.version
  FOR each f IN deleted DO
    DeleteChunks(f); DeleteEdgesFrom(f); NullifyEdgesTo(f); DeleteContent(f)
  END FOR
  FOR each f IN dirty DO
    UpsertContent(f); ReplaceChunks(f); ReplaceEdgesFrom(f); ReEmbedChunks(f)
  END FOR
  FOR each f IN summaries DO InvalidateSummary(f) END FOR
  index.version ← index.version + 1
END
```

**Preconditions:** the graph is persisted and consistent with the previous index version; `changed` and `deleted` are disjoint.
**Postconditions:** the resulting index is byte-equal, per row, to a full rescan of the final tree, modulo row ids and timestamps; no chunk survives for a deleted `(file, index)` pair; no edge points at a deleted file.
**Termination:** `Dependants` is computed over a visited set, so a cyclic import graph terminates. **Q-10**, **Q-11**, **Q-25**.

### A.6 Readiness scoring

```pascal
ALGORITHM Score(inventory, checks, weights)
PRE:  Σ weights = 100; every check has weight ≥ 1; all arithmetic is integer
POST: score ∈ [0,100]; deterministic; order-independent; monotone in check outcomes
BEGIN
  total ← 0
  FOR each c IN Categories DO
    applicable ← { k ∈ checks : k.category = c ∧ k.applies_to(inventory) }
    IF applicable = ∅ THEN
      // A category with nothing applicable is EXCLUDED, and its weight is
      // redistributed proportionally — a project with no IaC is not punished for
      // having no IaC.
      MarkExcluded(c); CONTINUE
    END IF
    earned   ← Σ_{k ∈ applicable, k.evaluate(inventory) = PASS} k.weight
    possible ← Σ_{k ∈ applicable} k.weight
    total    ← total + (weights[c] × earned) DIV possible
  END FOR
  RETURN Redistribute(total, excluded_categories)
END
```

**Postconditions:** identical inventories yield identical scores (no map iteration order, no float, no clock, no LLM); making a failing applicable check pass never decreases the score; the score is accompanied by `inventory_hash` so a report can be tied to the inventory that produced it. **Q-18**.

### A.7 Redaction before prompt assembly, and the cache key

```pascal
ALGORITHM BuildPromptAndLookup(project, query, k, cache)
POST: no unredacted text is hashed, transmitted, or stored; no cached completion is
      reachable from an unredacted prompt
BEGIN
  raw       ← HybridRetrieve(project, query, 3 × k)     // store holds redacted text only
  reranked  ← Rerank(raw)[0 .. k−1]                     // degrades to raw order on failure
  redacted  ← Redactor.RedactChunks(project, reranked)  // ONLY producer of RedactedChunk
  ASSERT ∀ c ∈ redacted : TypeOf(c) = RedactedChunk

  prompt ← AssemblePrompt(system, redacted, Redactor.RedactInstruction(query))
  ASSERT TypeOf(prompt) = RedactedPrompt                // no str overload exists

  key ← SHA256(Canonical({model, Normalise(prompt), params}))
  hit ← cache.Lookup(prompt)                            // signature demands RedactedPrompt
  IF hit ≠ ⊥ THEN RETURN hit END IF
  resp ← Router.Complete(prompt)
  cache.Store(prompt, resp)
  RETURN resp
END
```

**Postconditions:** every value hashed into a cache key is a `RedactedPrompt`; the only constructor of that type is the redactor; therefore no key exists that an unredacted prompt could produce. **Q-12**, **Q-13**.

### A.8 Audit append with hash chaining

```pascal
ALGORITHM AppendAudit(session, draft, tenant_key)
PRE:  session is inside the caller's transaction
POST: exactly one row is appended, chained to the previous row for this tenant
BEGIN
  AdvisoryXactLock(session, "forgeops-audit", tenant_key)   // chains need serial append
  prev ← SELECT hash FROM audit_events
         WHERE tenant_id = draft.tenant ORDER BY seq DESC LIMIT 1
  prev_hash ← prev ?? ZERO32                                // genesis
  payload   ← JCS(SemanticFields(draft))                    // excludes seq and hash
  h         ← SHA256(payload ‖ prev_hash)
  INSERT audit_events(..., prev_hash ← prev_hash, hash ← h)
  RETURN seq
END

ALGORITHM VerifyChain(tenant, from_seq)
POST: returns OK, or the seq of the FIRST divergence
BEGIN
  prev ← (from_seq = 0) ? ZERO32 : HashAt(from_seq − 1)
  FOR each row IN Rows(tenant, from_seq ..) ORDER BY seq DO
    INVARIANT every row before this one verified
    IF SHA256(JCS(SemanticFields(row)) ‖ prev) ≠ row.hash THEN RETURN Divergence(row.seq) END IF
    prev ← row.hash
  END FOR
  RETURN OK
END
```

**Postconditions:** editing any row invalidates its own hash and every subsequent hash; deleting a row leaves a `seq` gap **and** breaks the chain; both are detectable without a second copy. UPDATE and DELETE are additionally impossible for the application role (§6.4). **Q-05**.

### A.9 Atomic apply with rollback handle (extends Phase 0 A.5)

```pascal
ALGORITHM ApplyVerified(verified, root, entries)
PRE:  verified came from VerifyEnvelope; entries have unique cleaned rel_paths
POST: all entries applied with a backup each, or NO target differs from its pre-image
BEGIN
  FOR each e IN entries DO                       // validate EVERYTHING before any I/O
    abs ← ResolveSymlinks(Join(root, Clean(e.rel_path)))
    ASSERT Within(abs, root)                     // no traversal escape
    ASSERT NOT BlockedForWrite(abs)              // D-46: .env.example permitted
    IF e.action ≠ CREATE THEN
      ASSERT SHA256(Read(abs)) = e.expected_hash // else ErrConflict, write nothing
    END IF
  END FOR

  backups ← [] ; written ← []
  TRY
    FOR each e IN entries DO
      INVARIANT |backups| = |written|
      IF Exists(abs(e)) THEN Append(backups, CopyToTimestampedBackup(abs(e)))
      ELSE                   Append(backups, NO_PREVIOUS) END IF
      tmp ← WriteTempSameDir(abs(e), e.content, e.mode)
      Fsync(tmp); Rename(tmp, abs(e)); FsyncDir(Dirname(abs(e)))
      Append(written, abs(e))
    END FOR
    RETURN ApplyReport(written, Manifest(backups))
  CATCH err
    FOR i FROM |written| − 1 DOWNTO 0 DO
      IF backups[i] = NO_PREVIOUS THEN Delete(written[i]) ELSE Restore(backups[i], written[i]) END IF
    END FOR
    RETURN Error(err)                            // change_sets → rolled_back
  END TRY
END
```

**Postconditions:** identical to Phase 0's P-08 (all-or-nothing, backup per pre-existing target, nothing outside `root` touched), plus: a pre-image mismatch aborts before any write; the returned manifest is sufficient to restore every target byte-for-byte. **Q-01**, **Q-02**.

### A.10 Hybrid retrieval with RRF and reranking

```pascal
ALGORITHM HybridRetrieve(project, query, n)
PRE:  n = overfetch × k; the dense index dimension matches the project's backend
POST: returns at most n distinct chunks, ordered; degrades rather than failing
BEGIN
  dense  ← TRY VectorKNN(TableFor(project), Embed(query), n) CATCH ∅
  sparse ← TRY RedisBM25(project, query, n)                  CATCH ∅
  IF dense = ∅ AND sparse = ∅ THEN RETURN ∅ END IF

  // RRF needs no score normalisation between two incomparable scales, which is
  // why it is used instead of weighted score fusion.
  FOR each d IN dense ∪ sparse DO
    rrf[d] ← Σ_{L ∈ {dense, sparse}, d ∈ L} 1 / (60 + Rank(L, d))
  END FOR
  fused ← SortDesc(rrf)[0 .. n−1]
  RETURN fused
END
```

**Postconditions:** the result is a deterministic function of the two ranked lists; a missing list halves the evidence but never fails; ranks are 1-based so the constant 60 behaves as intended.

### A.11 Policy-agreement check between backend and agent

```pascal
ALGORITHM AgreeOnDecision(input, backend_bundle, agent_bundle)
PRE:  both bundles are archives with computed digests
POST: if digests match, the decisions are equal; otherwise both sides deny
BEGIN
  IF Digest(backend_bundle) ≠ Digest(agent_bundle) THEN
    RETURN (DENY, DENY, "policy-bundle-stale")     // both fail closed, independently
  END IF
  b ← OPAServer.Evaluate(backend_bundle, input)
  a ← EmbeddedRego.Evaluate(agent_bundle, input)
  IF b = UNDEFINED THEN RETURN (ERROR_503, DENY, "governance-policy-undefined") END IF
  ASSERT a = b                                     // Q-06
  RETURN (b, a, "agreed")
END
```

**Postconditions:** with equal digests the two evaluators agree on every input; with unequal digests neither allows. Because both refuse independently, a bug in one side's staleness check does not open the gate. **Q-06**, **Q-07**.

---

## Appendix B — Correctness Properties for Property-Based Testing

Fresh prefix **`Q-`**, so no collision with Phase 0's `P-01 … P-15`, which continue to run unchanged. Properties marked ★ guard behaviour that is both easy to get subtly wrong and expensive to discover late.

**Every row carries a negative control** — the specific mutation that must make the property fail. `mutations.toml` declares them, the `mutation` CI job runs them, and a property that still passes under its own control **fails the build** (§0.4.5). This is the mechanism that stops Phase 0's P-09 situation, where a clause survived with both redaction pattern lists emptied.

| ID | Property (universally quantified) | Target module | Library | Negative control (must FAIL) |
|:---|:---|:---|:---|:---|
| **Q-01** ★ | ∀ change-sets and ∀ injected failure points: after `ApplyVerified` either every target holds its new content **and** a backup exists for every pre-existing target, or every target byte-equals its pre-image; no path outside `root` is written; `blockedForWrite` paths are always refused; `.env.example` is permitted while `.env` is not | `executor/internal/mutate` | rapid | Remove the rollback loop from the `CATCH` branch |
| **Q-02** ★ | ∀ apply-then-revert sequences: `Revert(manifest)` restores every file byte-for-byte to its pre-image, including deleting files that did not previously exist; revert is idempotent; a consumed handle cannot be reused | `executor/internal/mutate` | rapid | Make `Revert` skip entries marked `NO_PREVIOUS` |
| **Q-03** ★ | ∀ generated call graphs over `src/**`: no `@mutation_primitive` is reachable without a `MutationAuthority`, and `MutationAuthority` cannot be constructed outside `governance/`; ∀ Go packages: `executor/internal/mutate` has no importer outside `executor/**` | `governance/authority`, `scripts/check-chokepoint.sh` | hypothesis + rapid | Delete the `_MINT_SENTINEL` check in `__post_init__` |
| **Q-04** ★ | ∀ chokepoint transits (allow, deny, block, pending, apply, revert): exactly one `audit_events` row is written per transit, in the same transaction as the state change; a rolled-back transaction leaves neither | `governance/chokepoint`, `audit/writer` | hypothesis | Move the audit write outside the transaction |
| **Q-05** ★ | ∀ audit sequences and ∀ tamper attempts: UPDATE and DELETE raise; recomputing the chain from any start point reproduces every stored hash; altering one row's semantic fields makes `verify_chain` report that row's `seq` as the first divergence | `audit/*`, migration `0007` | hypothesis | Drop `prev_hash` from the hashed payload |
| **Q-06** ★ | ∀ governance inputs (operations × change-item sets × weekdays × timezones × verdicts × environments), when the bundle digests are equal: the backend OPA-server decision equals the agent's embedded decision | `policies/agent/*.rego`, `agent/internal/policy` | hypothesis (cross-runtime via a fixture corpus) | In the agent's copy of the bundle only, invert the comparison in `approval.rego`'s `require_approval if input.environment == "prod"` clause to `!=` |
| **Q-07** ★ | ∀ digest pairs: if the agent's bundle digest ≠ the envelope's `policy_context` digest, the agent denies **and** the chokepoint refuses to mint; no mutation occurs on either path | `agent/internal/policy`, `governance/chokepoint` | rapid + hypothesis | Make the agent's digest comparison a warning |
| **Q-08** ★ | ∀ sequences of validation outcomes: the generation loop performs at most 3 model calls and terminates with `Accepted`, `TemplateFallback` or `Unavailable`; `attempts_remaining` decreases strictly on every `Continue` | `generation/loop` | hypothesis (stateful) | Make `_next` return `Continue` without decrementing |
| **Q-09** ★ | ∀ rubric values (including all-zero and all-five): `GateDecision` is identical, and no rubric field appears in `decide`'s inputs | `generation/gate`, `generation/judge` | hypothesis | Add a `rubric` parameter to `decide` and let a low score block |
| **Q-10** ★ | ∀ edit sequences (create, modify, delete, rename, import changes, cycles) over a generated project: the incrementally maintained index equals `FullRescan(final_tree)` — same chunks, same edges, same summary invalidation, no orphans | `scanner` closure + `analysis/index_service` | rapid | Drop the `Dependants(deleted)` term from `DirtySet` |
| **Q-11** | ∀ raw watcher event sequences: the debounced/coalesced stream produces the same dirty set as the un-coalesced stream | `scanner` watch pipeline | rapid | Coalesce a delete followed by a create into a no-op |
| **Q-12** ★ | ∀ chunk sets containing generated synthetic secrets: `assemble_prompt` accepts only redacted types, and no prompt reaching a `ModelEndpoint` contains a synthetic secret value | `generation/context`, `secrets/redaction` | hypothesis | Add a `str` overload to `assemble_prompt` |
| **Q-13** ★ | ∀ prompts: every cache key is computed over a `RedactedPrompt`; no cached completion is retrievable using unredacted text; no cache entry's stored key material contains a synthetic secret | `ai/cache/tiered`, `generation/context` | hypothesis | Widen `lookup`/`store` back to `str` |
| **Q-14** ★ | ∀ envelopes: `CanonicalBytes` is byte-identical in Go and Python for the same logical envelope; signature verification accepts exactly the correctly signed envelope and rejects every single-byte mutation | `envelope` (Go, D-59), `governance/envelope` | rapid + hypothesis over a shared fixture corpus | Remove the domain-separation prefix on one side only |
| **Q-15** ★ | ∀ envelope streams with replays, reorderings and expiries: a replayed nonce, a non-increasing `seq`, or an expired `not_after` is rejected, and no rejected envelope performs any mutation or advances any counter | `envelope` (Go, D-59) | rapid | Update `last_seq` before the signature check |
| **Q-16** ★ | ∀ revocation timings relative to an in-flight message stream: the first message after revocation is rejected and the socket closed; a replica that missed the pub/sub event still rejects | `auth/devices`, `websocket/hub` | hypothesis | Check revocation once per connection instead of per message |
| **Q-17** ★ | ∀ concurrent exchange attempts on one pairing code: at most one succeeds; an expired, burned or unknown code is indistinguishable in the response; attempts beyond the cap always fail; the code value appears in no log, audit row or column | `auth/devices` | hypothesis (concurrent) | Make the consume script non-atomic (read then delete) |
| **Q-18** ★ | ∀ inventories: the readiness score is deterministic, independent of file iteration order, and monotone (making an applicable failing check pass never lowers the score); `inventory_hash` identifies the inventory that produced a report | `analysis/readiness` | hypothesis | Replace the integer division in `Score`'s per-category term with float division |
| **Q-19** ★ | ∀ routes registered by `create_app()`: each either depends on `require_principal` or is a member of `PUBLIC_ROUTES`; ∀ requests without a token to a non-public route: 401, and no handler body executes | `auth/dependencies`, `main` | hypothesis + httpx | Drop the `require_principal` dependency from `GET /api/v1/projects` without adding that path to `PUBLIC_ROUTES` |
| **Q-20** ★ | ∀ (role, resource, action) triples: a viewer is denied every mutating action; no role can read a secret **value** through any route; a 403 body is byte-identical whether or not the resource exists | `auth/*`, Cerbos policies | hypothesis | Add `read_value` to the viewer's Cerbos policy |
| **Q-21** | ∀ template ∈ 8 languages × 5 artifact classes: rendering against the fixture project for that language yields artifacts with **zero blocking findings** from the same pipeline the AI output traverses | `generation/templates` | hypothesis (parametrised over the matrix) | Corrupt one template's Dockerfile `FROM` line |
| **Q-22** ★ | ∀ change-set transition sequences: only edges in the §3.6 state machine are accepted; terminal states are absorbing; two concurrent approvals yield exactly one winner and one 409; an `applied` set can only leave via `reverted` | `governance/*`, migration `0004` | hypothesis (stateful) | Remove the optimistic-concurrency `version` predicate |
| **Q-23** | ∀ (old, new) content pairs: applying the compiled `change_items` reproduces `new_content` exactly; the unified diff of (old, new) applied to old yields new; the frontend renders the same hunk count the backend computed | `governance/chokepoint` (stage 3), `fileops.UnifiedDiff`, `features/approval` | hypothesis + fast-check | Compile change items with `old_content` from the wrong revision |
| **Q-24** ★ | ∀ synthetic secrets injected into file content, validator output and exception paths: no secret value appears in any log line, any `audit_events` row, or any RFC 9457 `detail` | `core/logging`, `audit/*`, `core/errors`, `validator/*` | hypothesis | Empty the redaction pattern list (the exact experiment `REVIEW-PHASE-0.md` Pass 8 ran) |
| **Q-25** | ∀ grammar blobs: a digest mismatch refuses to load and the scan fails closed with a typed error; ∀ cyclic dependency graphs: closure computation terminates | `scanner/ast`, `scanner` closure | rapid | Skip digest verification when the blob loads successfully |
| **Q-26** | ∀ generation and analysis streams: only the six `SSEEventType` names are emitted; `PROGRESS.percent` is non-decreasing; exactly one of `COMPLETE`/`ERROR` terminates every stream, including on client disconnect | `core/sse`, `generation/routes`, `lib/api/sse.ts` | hypothesis + fast-check | Emit a second `COMPLETE` after an `ERROR` |
| **Q-27** ★ | ∀ tier YAML documents written to a temp path: the tier set on the running app built by `create_app()` equals the parsed document — the app's routing configuration is derived from the file, not from a default | `main`, `ai/routing/tiers` | hypothesis | Hard-code the tier map in the lifespan and ignore the path |
| **Q-28** | ∀ envelopes carrying `secrets.inject`: no injected value is written to any file, change-item, log or audit row; the audit row names the keys only | `secrets/injection`, `executor` | hypothesis | Log the injected environment map at debug level |
| **Q-29** | ∀ retrieval requests: with the reranker unavailable the result is the RRF-fused order and `retrieval_degraded` is recorded; with the sparse index absent the result is dense-only and the flag is recorded; neither raises | `generation/rag` | hypothesis | Let a reranker timeout propagate as a 500 |
| **Q-30** ★ | ∀ principals: `blast_radius` is derived from the verified identity, and setting `MCP_AGENT_BLAST_RADIUS` cannot widen it for an authenticated caller; with `APP_ENV=production` the variable's presence is a startup error | `auth/principal`, `core/config`, `mcp/gateway` | hypothesis | Read the env var in the gateway when a principal is present |
| **Q-31** ★ | ∀ offline/reconnect sequences and ∀ journal contents: no `Record` persisted by `Journal.Append` carries an envelope, an `approval_id`, a `MutationAuthority`, a device token, an envelope key or a secret value; `Journal.Drain` applies nothing — every `KindIntent` record produces a **new** chokepoint transit with a fresh `approval_id`, `policy_context` digest, nonce and `seq`; a revoked device wipes rather than drains; a stale bundle leaves intents queued; redelivery after an acknowledged batch is a no-op | `session/journal` (Go), `governance/chokepoint`, `auth/devices` | rapid + hypothesis | Admit a `KindEnvelope` record kind in `validKinds`, so `Journal.Append` will persist an authorisation (**amended, D-87**: this cell previously read "add a `KindEnvelope` case to `Journal.Drain` that hands the stored envelope straight to `executor.Execute`". That branch has no input to consume — D-41's guarantee is that no record kind can carry an authorisation — so writing it would mean shipping a real, reachable path from a persisted envelope to the executor whose only purpose is to be broken. The control now breaks the fact the property rests on, the vocabulary, and therefore bites at `Append` rather than at `Drain`: earlier, not later) |

Thirty-one properties, twenty-one of them marked ★. Phase 0's fifteen `P-` properties continue to run unchanged, so the repository's total is forty-six.

Not assigned a `Q-` id but verified executably by focused integration tests: the OIDC code+PKCE round trip against real Authentik (`auth` job); Alembic linearity and a clean autogenerate; the two-role privilege split on `audit_events`; kind-based server-side dry-run acceptance of the template manifests (`k8s` job); SPIRE attestation and mTLS (`k8s` job); ARQ and `InlineDispatcher` producing identical handler results; the criterion-10 browser journey including on-disk assertions and revert (`e2e` job); and the merged SBOM containing every grammar component (`supply` job).


---

## Appendix C — Error Handling

### C.1 New RFC 9457 problem types

All under `https://errors.forgeops.dev/{suffix}`, extending Phase 0's registry. Rules unchanged: `type` is stable and never resolved at runtime; `status` always equals the HTTP status (P-09); `detail` never carries secrets, tokens, connection strings or tracebacks (D-27, Q-24).

| Suffix | Status | When | `detail` guidance |
|:---|:--:|:---|:---|
| `unauthenticated` | 401 | No token, or verification failed, on a non-public route | Never says which check failed |
| `idp-unavailable` | 503 | The identity provider cannot be reached, or its discovery document is unreadable or declares a different issuer, during login or refresh (**D-53**) | Names no URL, no upstream status and no provider error text |
| `forbidden` | 403 | RBAC or Cerbos deny | Identical body whether or not the resource exists (§4.2) |
| `authorization-unavailable` | 503 | The Cerbos sidecar cannot be reached, or answers a non-200 or an unparseable body, so the permission could not be evaluated (**D-56**) | Names no URL and no upstream status; must never be reported as `forbidden` |
| `pairing-code-invalid` | 401 | Unknown, expired, burned or already-consumed code | One message for all four cases; never echoes the code |
| `pairing-rate-limited` | 429 | Per-IP or global exchange bucket exhausted | Integer `Retry-After` |
| `pairing-unavailable` | 503 | Redis cannot be reached, so neither §14.6 bucket can be evaluated and the single-use consume script cannot run (**D-71**) | Names no host and no upstream error; must never be reported as `pairing-rate-limited`, which would tell a client a rate was measured when none was |
| `csr-invalid` | 400 | The submitted CSR does not parse, its self-signature does not verify, its key is not EC P-256, or the declared `fingerprint` does not match its SubjectPublicKeyInfo (**D-71**) | Names the failed check; never echoes the submitted bytes. Safe to distinguish from `pairing-code-invalid` because the check runs **before** the code is consumed |
| `device-not-found` | 404 | Revocation named a device id that does not exist (**D-71**) | Admin-only route, so a 404 discloses nothing a caller could not already read; §4.2's enumeration rule constrains the `forbidden` body |
| `device-revoked` | 401 | Device token or certificate revoked | Names the revocation time, not the actor |
| `device-not-connected` | 409 | No active agent session for the project | Suggests pairing or starting the agent |
| `envelope-signature-invalid` | 401 | HMAC mismatch (JSON-RPC `agent.error` counterpart) | Never includes the expected signature |
| `envelope-replayed` | 409 | Duplicate nonce or non-increasing `seq` | States which condition, not the counter value |
| `envelope-expired` | 401 | Outside `not_after` ± skew | Includes the tolerated skew so a clock problem is diagnosable |
| `envelope-unsupported-version` | 400 | Unknown envelope `v` | Names the supported version |
| `operation-unknown` | 400 | Operation outside the closed catalogue | Lists no internal names |
| `policy-denied` | 403 | OPA governance deny, either side | Carries the rule id and the policy's own reason (FR-37) |
| `policy-bundle-stale` | 409 | Agent's bundle digest ≠ active digest | Names both digests (they are public hashes) |
| `governance-policy-undefined` | 503 | OPA returned an undefined document | The D-25 lesson applied to the governance bundle |
| `approval-required` | 202 | Change-set pending human approval | Carries `change_set_id`; a 202 is not an error but uses the same body shape for uniformity |
| `approval-forbidden` | 403 | Approver may not approve this change-set (e.g. self-approval barred) | Names the policy, not the approver list |
| `approval-expired` | 409 | Pending change-set exceeded `APPROVAL_TTL_SECONDS` | Suggests regeneration |
| `blast-radius-blocked` | 409 | `SemanticPlanAnalyzer` verdict = block | Carries the score and the stateful deletions found |
| `change-set-conflict` | 409 | Optimistic-concurrency loss, or a pre-image hash mismatch on disk | Names the conflicting file paths, never their contents |
| `change-set-already-applied` | 409 | Apply attempted on a terminal change-set | States the current status |
| `apply-rolled-back` | 500 | Apply failed mid-way; every write reverted | Names the failing path and confirms the rollback completed |
| `revert-unavailable` | 409 | Rollback handle consumed or expired | States which |
| `iteration-bound-exhausted` | 200 | Three iterations failed; a template was substituted | Not an error status: the response carries artifacts. Recorded as `served_from: template` |
| `generation-unavailable` | 503 | Router exhausted **and** no template for the detected language | Names the language; never carries provider error text |
| `secret-redaction-failed` | 422 | A change-set or prompt contained a value matching a project secret | Names the file and the secret **key**, never the value |
| `secret-store-unavailable` | 503 | Infisical unreachable | No connection string |
| `project-embedding-backend-locked` | 409 | Attempt to change `embedding_backend` after embeddings exist | Points at re-indexing |
| `index-version-conflict` | 409 | Incremental patch `base_version` mismatch | Names the current version |
| `scan-in-progress` | 409 | Conflicting scan already running | Carries the task handle |
| `audit-write-failed` | 500 | Audit append failed; the enclosing transaction was rolled back | States that no partial change was committed — the important half |
| `dryrun-unavailable` | 503 | No agent or no cluster for a required dry-run validator | Names the validator |
| `validator-unavailable` | 503 | A blocking validator cannot run at `infrastructure` radius | Names the validator and how to provide it |
| `tenant-context-missing` | 500 | A tenant-scoped query ran without `app.tenant_id` | Internal invariant violation; generic detail plus `trace_id` |

`audit-write-failed` deserves a note. A failed audit write **aborts the mutation**, because §1.9's guarantee is that every action is logged — an action that happened without a record would break Q-04 and, worse, would be invisible. Availability is traded for auditability, deliberately.

### C.2 Agent error scenarios (JSON-RPC `agent.error` codes)

Codes mirror the suffixes above so one vocabulary spans HTTP and the socket.

| Scenario | Code | Behaviour | Recovery |
|:---|:---|:---|:---|
| No device token, backend URL configured | `session-unpaired` | `session.ErrUnpaired`; the agent keeps serving MCP and reports it in `doctor` | User runs `pair` |
| Certificate expired and renewal failed | `session-cert-expired` | Reconnect with backoff; refuse mutations meanwhile | Automatic on renewal |
| Device revoked mid-operation | `device-revoked` | Abort, roll back from the manifest, wipe credentials, enter unpaired | Re-pair |
| Signature, replay or expiry failure | `envelope-*` | Refuse; **no** mutation; report and continue serving | Backend resends |
| Bundle stale | `policy-bundle-stale` | Refuse every mutation; request a bundle | Automatic on reload |
| Agent-side policy deny | `policy-denied` | Refuse with the rule id | Not recoverable by retry |
| Path outside root, or blocked for write | `path-refused` | Refuse before any I/O; logged as a security event | Not recoverable by design |
| Pre-image hash mismatch | `change-set-conflict` | Write nothing; report per-file | Regenerate against current content |
| Partial apply failure | `apply-rolled-back` | Full rollback, original error wrapped | Retry the whole set |
| Grammar digest mismatch | `grammar-integrity` | Refuse to parse; scan fails closed | Reinstall the agent |
| Validator binary missing | `validator-unavailable` | Fatal at `infrastructure` radius, warning otherwise (OQ-25) | Install the tool |
| Watcher limit exhausted | `watch-degraded` | Fall back to periodic polling, report it | Raise the OS inotify limit |
| Keychain unavailable | `credential-store-degraded` | 0600-file fallback, reported by `doctor` | Provide a Secret Service |
| Clock skew beyond tolerance | `clock-skew` | Refuse envelopes, report the measured skew | Fix NTP |
| Journal full while offline | `journal-full` | Refuse to enqueue further records rather than evicting silently; report backlog in `agent.status` and `agent doctor` | Reconnect, or raise `AGENT_JOURNAL_MAX_BYTES` |
| Journal tail corrupt on load | `journal-corrupt` | Discard the trailing record, keep the rest, warn; never fail startup | Automatic |
| Revoked while offline | `device-revoked` | Journal is **wiped, not drained**; credentials wiped; agent enters unpaired | Re-pair; queued intents are deliberately lost |
| Bundle stale on reconnect | `policy-bundle-stale` | Non-mutating records drain; intents stay queued until the bundle reloads | Automatic on reload |

Phase 0's agent error rows (tofu missing, tofu timeout, non-zero exit, git push rejected, GitHub rate limit, shutdown during work) are unchanged.

### C.3 Frontend error scenarios

Phase 0's four rows are unchanged. New rows:

| Scenario | Behaviour |
|:---|:---|
| SSE stream drops mid-generation | The reader surfaces a non-blocking notice, the run continues server-side, and TanStack Query refetches the run's state on reconnect — SSE is a view, not the source of truth (§7.5) |
| Unknown SSE event name | Dropped with a console warning; never rendered. Fails loudly in development rather than silently in production (Q-26) |
| 202 `approval-required` | Rendered as a state, not an error: the diff view opens with approve/reject enabled |
| 409 `change-set-conflict` | Shows which files changed underneath and offers regeneration; never offers "force apply" |
| 403 on a project route | Renders "not found or not permitted" — the same copy for both, matching the server's non-disclosing body |
| Session expiry mid-navigation | `proxy.ts` refreshes silently when possible, otherwise redirects to `/login` preserving the return path |

---

## Appendix D — Performance Considerations

| NFR | Target | Phase 1 action |
|:---|:---|:---|
| **NFR-01** | Codebase analysis < 30 s for < 10 K files | Fan-out parsing bounded at `min(GOMAXPROCS, 8)`; wazero modules compiled once and pooled per language; embedding batched and queued so the scan is not blocked on the network; cold-start heuristics answer in well under a second so the *perceived* time is the heuristic pass. Honest note: wazero parses more slowly than native cgo bindings, so the parse stage has less headroom than option A would have given — mitigated by parallelism and by not re-parsing unchanged files at all (D-33) |
| **NFR-02** | < 5 minutes for < 100 K files | Incremental rescan makes this a **first-scan-only** cost; the watch path re-parses only the dirty closure. Size and type filters (>1 MB, binaries, `node_modules`, `.git`) exclude the bulk of a large tree before parsing |
| **NFR-03** | AI artifact generation < 15 s first attempt | Retrieval is two indexed queries plus one rerank call; the L3 prefix cache keeps the system preamble out of the per-request prompt; `high_coding` is used only for genuinely multi-file work, `medium` for single artifacts |
| **NFR-04** | Validation loop < 3 iterations average | Feedback carries **all** blocking findings from a pass, not the first — a regeneration that fixes one problem per iteration would exhaust the budget on a file with four (§11.12). The hard ceiling is 3 (Q-08) |
| **NFR-05** | Agent ↔ backend latency < 100 ms | JSON-RPC over a persistent WSS connection; heartbeats are small; progress messages are coalesced client-side before emission so a chatty operation does not saturate the socket |
| **NFR-06/07** | Dashboard < 2 s initial, < 500 ms cached | ECharts imported through `echarts/core` with only the radar chart registered; the diff viewer is code-split per route; TanStack Query `staleTime` unchanged at 30 s |
| **NFR-15** | Per-user, per-project, per-operation token budgets | Phase 0's Redis/Lua bucket extended to generation routes, plus a per-project monthly LLM budget counter (FR-06) checked before a run starts rather than after it overspends |
| **NFR-29** | pgvector to ~50 M vectors | HNSW from the first migration on both vector tables; `ef_search` per query transaction; the sparse index in Redis so keyword search does not compete with vector queries for the same buffers |
| **NFR-30** | Job throughput 1 000+/s | Not a Phase 1 target and not claimed: ARQ on a single Redis will not reach it. Phase 1's queue carries human-paced work (a scan, a generation), and the throughput target belongs with the Phase 2 durable engine and KEDA-based autoscaling |

Costs accepted and recorded so they are not rediscovered under load:

- **Audit writes serialise per tenant** (advisory lock, §11.9). Acceptable because every write is a governance transit; not acceptable if a future phase logs high-frequency telemetry to the same table, which is why §1.2's activity feed is a projection rather than a second writer.
- **The agent binary grows 25–45 MB** (§8.2, OQ-27).
- **`k8s` adds 2–4 minutes** to matching CI runs (D-28), and `e2e` adds more. `changes` filters keep both off unrelated pull requests.
- **HNSW build cost** is unchanged from Phase 0's accepted trade: slower builds for sub-10 ms queries at high recall.
- **The offline journal consumes disk while disconnected** (D-41), bounded by `AGENT_JOURNAL_MAX_BYTES` (default 64 MiB) and `AGENT_JOURNAL_MAX_AGE_HOURS`. Reaching the bound refuses further enqueues with `journal-full` rather than evicting silently, because a dropped scan batch nobody reports is an index that is quietly wrong. Drain cost is one `fsync` per append and a single truncation per successful drain.

---

## Appendix E — Phase 1 Completion Criteria Traceability

The 14 criteria are reproduced **verbatim** from `phases.md` Phase 1 "Completion Criteria". Every evidence bar names a CI job that exists in §8.3's right-hand column — `scripts/check-ci-jobs.py` asserts that mechanically, because Phase 0's Appendix E cited `build`, `test` and `lint` jobs that never existed (§15.10).

| # | Completion criterion (verbatim) | Design | CI-executable evidence bar |
|:--|:---|:---|:---|
| 1 | User can install agent, pair with dashboard, import a project | §3.1, §10.2, §10.3, §11.2, §11.3, §12.6 | **`e2e`**: the journey builds the real agent binary, mints a pairing code through the API, runs `forgeops-agent pair`, asserts the device reaches `active` and heartbeats, and imports the fixture project — all against built containers. **`agent`**: `Pair` unit + integration tests including expired, burned and concurrent-exchange cases (**Q-17**), plus the journal's offline round trip — disconnect, enqueue, reconnect, revalidate (**Q-31**, NFR-18). **`supply`**: `goreleaser release --snapshot` produces the six-target archives; the `linux_amd64` archive is extracted in-job and `forgeops-agent version` executed, so "install" is proven by a built artifact rather than asserted |
| 2 | Agent scans codebase and produces readiness score | §10.8, §11.4, §11.4.5, Appendix A.6 | **`agent`**: wazero parses fixtures in all embedded languages; cAST chunk sizes within `512 ± overlap`; dependency-graph edges resolved. **`backend`**: `test_readiness_determinism.py` + **Q-18** (determinism, order independence, monotonicity). **`e2e`**: the score and radar chart render for the fixture project with a non-zero score and a category breakdown |
| 3 | AI generates Dockerfile and K8s manifests from real project | §11.5, §13.2, Appendix A.7, A.10 | **`backend`**: `test_generation_integration.py` drives a full run against **local HTTP fixture endpoints** (the Phase 0 pattern that `REVIEW-PHASE-0.md` called the strongest test file in the PR) — no vendor key, no network — asserting a schema-valid `ArtifactSet` with a Dockerfile and Deployment + Service + Ingress. **Q-27** proves the six-tier chain came from `config/model-tiers.yaml`. **`e2e`**: the browser triggers a real run and the artifacts appear in the change-set |
| 4 | Generated files pass validation pipeline | §10.7, §11.12, §11.5.7 | **`agent`**: compose, YAML + JSON Schema, `tofu validate`/`plan`, `helm lint`, Trivy against generated and template artifacts. **`k8s`** (D-28): `kubectl`-equivalent **server-side** dry-run via `client-go` with `DryRun: All`, plus `helm template --validate`, against a real API server — so this criterion tests acceptance, not shape. **`templates`**: all 8 × 5 template artifacts traverse the identical pipeline with zero blocking findings (**Q-21**) |
| 5 | User can view diff, approve, and apply changes | §3.2, §3.6, §11.6, §12.2, §12.6 | **`e2e`**: the diff renders in both side-by-side and unified modes, approve-with-comment posts, and the change-set reaches `applied`. **`frontend`**: diff-fidelity unit tests + **Q-23**. **`backend`**: **Q-22** (state legality, terminal absorption, two concurrent approvals → one winner + one 409) |
| 6 | Files are applied atomically with backup | §10.5, Appendix A.9 | **`agent`**: **Q-01** (all-or-nothing with injected failure points, backup per pre-existing target, nothing outside root, `.env.example` write permitted while `.env` refused) and **Q-02** (byte-exact revert, idempotent, handle single-use). **`e2e`** steps 10–11 and 13: on-disk hash assertions, backup existence, and a revert that restores every pre-image byte-for-byte |
| 7 | Policies are enforced (block Friday deploys, require approvals) | §11.7, §10.6, Appendix A.11 | **`policy`**: `opa test policies/ -v` covers `schedule.rego` (Friday inside and outside the blocked window, across timezones), `paths.rego` (`package.json` protected) and `approval.rego` (prod requires approval), plus `opa check --strict`. **`backend`**: a Friday-clock integration test returns `403 policy-denied` with an audit record and **no** minted envelope. **`agent`** + **`backend`**: **Q-06** (both evaluators agree over generated inputs) and **Q-07** (digest mismatch ⇒ both deny) |
| 8 | Secrets are stored encrypted and injected at deploy time | §11.8, §7.11 | **`secrets`**: real Infisical container; CRUD round-trip; the API exposes no value-read path for any role (**Q-20**); `secrets.inject` materialises values into a process environment and **Q-28** asserts no value reaches a file, change-item, log or audit row. **`backend`**: **Q-12**/**Q-13** (redaction before prompt assembly; cache keys only over redacted prompts) and **Q-24** (no secret in any log, audit row or problem `detail`) |
| 9 | All actions are logged in immutable audit trail | §6.3, §6.4, §11.9, Appendix A.8 | **`backend`**: migration `0007` test — INSERT succeeds, UPDATE/DELETE/TRUNCATE raise `42501`, the app role holds no UPDATE privilege; **Q-05** (chain recomputation and first-divergence detection under tampering); **Q-04** (exactly one record per transit, in the same transaction, both directions). **`e2e`** step 12: the audit viewer shows the full transit with actors. All gated by `require_capability("postgres")`, which **fails** rather than skips in CI (D-26) |
| 10 | End-to-end test: import Node.js project → generate Dockerfile + K8s → approve → apply | §12.6 | **`e2e`**: `frontend/e2e/journey.spec.ts` runs the 13-step journey against built backend and frontend images with a **real paired agent container** and a fixture Node.js project, ending in on-disk assertions and a byte-exact revert. **`k8s`** supplies the server-side dry-run the generation traverses. Playwright traces and agent logs are uploaded on failure |
| 11 | Test coverage ≥ 70% | §7.13, D-31 | **`backend`**: `pytest --cov=src --cov-branch --cov-fail-under=70`. **`agent`**: `scripts/check-coverage.sh 70` over `./internal/...`. **`frontend`**: vitest v8 thresholds at 70/70/70. Per component, never aggregated. Additionally **`mutation`**: every Q-property fails under its declared negative control, and **`backend`**/**`agent`** run `check-no-skips.py` so the mandatory set records zero skips |
| 12 | HNSW indexes created on pgvector embedding columns for production performance | §6.3, §6.4, §6.5 | **`backend`**: migration `0003` test asserts `embeddings.embedding` is `vector(1536)` and `embeddings_local.embedding` is `vector(1024)`, that **both** carry `USING hnsw (… vector_cosine_ops) WITH (m='16', ef_construction='64')`, and that `with_ef_search` applies per transaction and does not leak to the next transaction on the same pooled connection. Gated by `require_capability("postgres")` with `FORGEOPS_REQUIRE_INTEGRATION=1` — the D-26 mechanism that turned criterion 14's silent skip into a failure |
| 13 | SSE streaming verified: LLM tokens stream to frontend without WebSocket overhead | §4.5, §7.5, §11.11, §12.4 | **`backend`**: `test_sse_generation.py` asserts `EventSourceResponse` (no `sse-starlette` in the dependency graph — asserted against `requirements.lock`), that only the six `SSEEventType` names appear, monotonic `PROGRESS`, and exactly one terminal event (**Q-26**). **`frontend`**: the fetch-based reader's unit tests. **`e2e`** step 7: the browser observes `status → token(s) → validation → complete` on a real run |
| 14 | Redis semantic caching operational: repeated LLM prompts return cached responses | §7.11, §11.5, D-44 | **`backend`**: an integration test against real Redis Stack issues the same generation prompt twice and asserts the second is served from L1 with zero provider calls, then a near-duplicate prompt is served from L2 above the 0.95 threshold; **Q-13** asserts every cache key is computed over a `RedactedPrompt` and that no cached completion is reachable from unredacted text |

### Deliverable coverage check

| `phases.md` deliverable | Covered in |
|:---|:---|
| **1.1** Agent Pairing & Connection | §2.5, §3.1, §3.7, §4.1, §7.3, §7.4, §7.6, §7.7, §10.2, §10.3, §10.4, §11.2, §11.10, Appendix A.1, A.2, Q-14 – Q-17, **Q-31 (NFR-18, D-41)** |
| **1.2** Multi-Project Workspace | §6.3, §11.3, §12.1, §12.3, Q-19, Q-20 |
| **1.3** Codebase Analysis Engine | §3.3, §6.3, §10.8, §11.4, §13.1, Appendix A.5, A.10, Q-10, Q-11, Q-25, Q-29, D-29, D-33, D-48, D-49 |
| **1.4** Deployment Readiness Analysis | §11.4.5, §12.2, §12.5, Appendix A.6, Q-18 |
| **1.5** AI File Generation & Validation Pipeline | §3.2, §3.8, §10.7, §11.5, §11.11, §11.12, §13.2, Appendix A.4, A.7, A.10, Q-08, Q-09, Q-12, Q-13, Q-21, Q-26, Q-27, Q-29, D-42, D-43, D-44 |
| **1.6** Change Approval Center | §3.2, §3.6, §6.3, §10.5, §11.6, §12.1, §12.2, Appendix A.9, Q-01, Q-02, Q-22, Q-23, D-45, D-46 |
| **1.7** Policy Engine (Basic) | §5.5, §10.6, §11.7, §12.2, Appendix A.11, Q-06, Q-07, D-30 |
| **1.8** Secret Management (Basic) | §3.4, §6.3, §6.6, §7.11, §10.9, §11.8, §14.5, Q-12, Q-13, Q-24, Q-28, D-46, D-50 |
| **1.9** Audit Logging | §6.3, §6.4, §11.9, §12.2, Appendix A.8, Q-04, Q-05 |
| **1.10** Agent Governance Control Plane | §2.2, §2.2.1, §2.2.2, §5.4, §10.2, §10.6, §11.6, §14.1, §14.3, §14.4, Appendix A.3, Q-03, Q-04, Q-06, Q-07, D-29, D-30, D-36, D-45 |
| **1.11** Auth Integration | §4.3, §4.4, §11.2, §13.1, §13.3, §14.1, §14.2, Q-19, Q-20, D-34 |
| Inherited debt (§0.5 D1–D5) | §0.5, §8.3, §8.4, §8.5, §11.1, §13.3, Q-27 |
| Test-integrity regime | §0.4, §7.13, §8.3.3, Appendix B (every negative control) |
| Progress record | §18 |

---

*End of Phase 1 design. Scope is bounded by §1; anything not listed in §1.1 is out of scope for this phase. Two owner decisions are recorded — **D-28** (kind-based Kubernetes CI job) and **D-29** (tree-sitter as WebAssembly under wazero, preserving `CGO_ENABLED=0`) — with the reversal cost of each stated in its entry. Thirty further decisions (D-30 … D-59) are design decisions open to owner override. Eleven open questions (OQ-22 … OQ-32) are recorded, none blocking, each with the recommendation this document already implements. Thirty-one correctness properties (Q-01 … Q-31) each carry an executable negative control, because a property that cannot fail is not a property.*
