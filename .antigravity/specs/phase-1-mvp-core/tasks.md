# Implementation Plan: Phase 1 — MVP Core: Analysis, Generation & Approval

**Spec:** `phase-1-mvp-core`
**Project:** ForgeOps (`github.com/parag8487/ForgeOps`)
**Workflow:** design-first
**Planning authority:** `design.md`; no `requirements.md` is created or referenced.

## Overview

This plan converts the Phase 1 design into incremental coding prompts. Its ordering is the plan's substance, not its packaging. The §0.4 test-integrity regime lands **first**, because a lint added after the code it governs finds nothing and Phase 0 shipped 419 green tests over a gateway that could not serve a request (D-23). The §0.5 inherited debt lands **second**, so nothing is built on unproven ground — in particular `load_tier_config` is wired into the app factory and proved by Q-27 before any generation leaf exists, because §1.5 sits entirely on six-tier routing. From there the plan follows Phase 0's staging: independent primitives, then transports and probes, then packages built on primitives, then final composition. The governance chokepoint and the `executor/internal/mutate` boundary land before the first mutating operation, so no mutating path is ever written outside the boundary even transiently; the redaction chokepoint lands before the first prompt is assembled from repository content. Optional Compose services arrive only with the implementation that uses them. The plan ends with fourteen criterion-verification leaves that may only execute behaviour implemented earlier, then one records leaf.

## Plan-wide constraints

- The four authoritative root documents—`AI-Powered-DevOps-Platform-Complete-Technical-Research.md`, `PRD.md`, `Tech-Stack-Analysis.md`, and `phases.md`—are immutable inputs. Exclude them only from mutating hooks/formatters; continue scanning them with Gitleaks. `PROGRESS.md` and `REVIEW-PHASE-0.md` are read-only inputs except where task 20.15 edits `PROGRESS.md` in place.
- Preserve the inherited decisions: D-1's **constraint** (a pure-static six-target build), D-2, D-5, D-14, D-19, and every Phase 1 decision D-28 … D-50. In particular `CGO_ENABLED=0` holds for all six release targets (D-29), the agent's policy engine is **in-process Rego, not Wasm** (D-30), `iac.Runner` still exposes no `apply`, and `MCP_AGENT_BLAST_RADIUS` is never a production source of authority (D-39).
- Use exact dependency versions, hash-pinned Python locks, digest-pinned images, SHA-pinned actions, and committed lockfiles. **No `@latest` anywhere**, including the inherited `govulncheck`, which is pinned at `v1.1.4` in `agent/tools/go.mod`.
- Coverage is a **per-component gate at ≥70 %** — backend, agent and frontend separately, never aggregated (D-31).
- Test doubles are signature-enforcing. Reassigning a `spec=`'d child is forbidden and mechanically detected by `scripts/check-test-doubles.py`; integration tests substitute transports, never collaborators.
- Every mandatory test must actually execute in CI. New capability gates go through `require_capability` and `FORGEOPS_REQUIRE_INTEGRATION=1` (D-26) and nothing else; `scripts/check-no-skips.py` fails the build on a skip in the mandatory selection.
- Every property test ships with its `mutations.toml` row and an executable negative control; the `mutation` job fails on a property that survives its own mutation.
- No task introduces Phase 2+ behaviour: no deployment automation, no Docker/K8s dashboards, no natural-language command surface, no monitoring or observability stack, no self-healing, no learning history, no `tofu apply`, no `environments`/`deployments`/`teams` tables.
- Test credentials are synthetic, self-labelling and assembled at runtime; JWTs are generated from throwaway key pairs inside the test session. No pre-baked signed token, no value shaped like a real provider token, and nothing resembling a credential in a fixture, doc, comment or commit message.
- All 166 numbered executable leaves are mandatory. Implementation leaves still land focused unit/example tests with their code; property leaves are separate.
- Completion-verification tasks may only execute or inspect behaviour implemented earlier. They must not hide new implementation work; failures return to the owning leaf and then rerun only the affected checks.

## Tasks

- [x] 1. Establish the test-integrity regime before the components it polices

  - [x] 1.1 Add the app-factory-derived production fixture

    - Create `backend/tests/integration/production_app.py` exposing a `production_app` fixture that builds the app through `create_app()` — the same callable uvicorn runs — under a lifespan manager.
    - Enforce the rule that makes it non-negotiable: the fixture may substitute a _transport_ (`httpx.MockTransport`, a local fixture HTTP server, a container URL) and may never substitute a collaborator object.
    - Add `backend/tests/integration/test_wiring_coverage.py`, which enumerates `vars(app.state)` and fails if any composed attribute is not named by a `@wires(...)` declaration in some wiring test, so a newly composed component cannot arrive untested.
    - _Design: §0.4.1, §7.8, §11.1; Deliverable: 1.11_

  - [x] 1.2 Add the self-maintaining call-site inventory and signature-conformance test

    - Implement `scripts/collect_call_sites.py`: walk `backend/src/**/*.py` with `ast`, find every `Call` whose function is an `Attribute` on a name bound from a constructor parameter or an `app.state` read, resolve the collaborator type from the annotation, and yield `(module, line, target_class, method, args, kwargs)`.
    - Add `backend/tests/unit/test_contract_conformance.py` parametrised over the collector, binding each site with `inspect.signature(...).bind()` against the real class.
    - Commit an `INVENTORY_FLOOR` integer that may only be raised, and assert the collector's output meets it, so a refactor cannot silently empty the inventory.
    - _Design: §0.4.2, §7.8; Deliverable: 1.11_

  - [x] 1.3 Implement the test-double AST lint with its own good/bad fixtures

    - Implement `scripts/check-test-doubles.py` with rules `FO-TD001` (assignment over a `spec=`'d child with a bare `Mock`/`AsyncMock`/`MagicMock`), `FO-TD002` (`spec=`/`create_autospec` without `spec_set=True`), `FO-TD003` (`patch`/`patch.object` without `autospec=True` on a project-owned target) and `FO-TD004` (any `Mock` under `tests/integration/**`).
    - Invocation `python scripts/check-test-doubles.py backend/tests`; input is every `.py` under `backend/tests/**` parsed with `ast` and never imported; failure is exit `1` with `path:line: FO-TD00N message`. Suppression requires `# noqa: FO-TD00N — <reason>`, and a reasonless suppression is itself `FO-TD001`.
    - Add `backend/tests/meta/fixtures/{bad_double.py,good_double.py}` and `backend/tests/meta/test_check_test_doubles.py` asserting the bad fixture is flagged and the good one is not; register the script as a `pre-commit` local hook scoped to `^backend/tests/.*\.py$`.
    - _Design: §0.4.3, §8.3; Deliverable: 1.11_

  - [x] 1.4 Add the Go interface-assertion completeness check

    - Implement `scripts/check-go-interface-assertions.sh`: enumerate every exported interface under `agent/internal/**` and every type that structurally satisfies it, and fail if an implementation has no `var _ Iface = (*Impl)(nil)` assertion in a `contract_test.go`.
    - Failure is exit `1` naming the interface and the unasserted implementation; the check also fails when the discovered interface set is empty.
    - Add the check to the `agent` job's lint step and add a negative fixture package proving it detects a missing assertion.
    - _Design: §0.4.2, §8.3, §9; Deliverable: 1.1_

  - [x] 1.5 Extend the capability gate and add skip detection

    - Extend `backend/tests/integration/capability.py::require_capability` with the Phase 1 capability keys `opa`, `cerbos`, `oidc`, `kubernetes`, `trivy`, `infisical`, `agent_binary`, keeping the D-26 semantics: skip locally, **fail** when `FORGEOPS_REQUIRE_INTEGRATION=1`.
    - Implement `scripts/check-no-skips.py` consuming `pytest --report-log` JSONL and `go test -json` events; failure is exit `1` listing every `mandatory`-marked node whose outcome was `skipped`, and also exit `1` when the mandatory selection is empty.
    - Add a `mandatory` pytest marker to `pyproject.toml` and meta tests proving the script detects both a skip and an empty selection.
    - _Design: §0.4.4, §7.13, §17.1 D-26 lineage; Deliverable: 1.11_

  - [x] 1.6 Implement the mutation harness and the negative-control manifest

    - Implement `scripts/mutation-harness.py --all`: read `backend/tests/mutation/mutations.toml`, write one pytest plugin per row into a `tempfile.mkdtemp()` directory asserted to lie **outside** the repository, run `pytest <property file> -p <plugin>`, and require the run to **fail**.
    - Create `backend/tests/mutation/mutations.toml` with the schema `property`/`target`/`mutation`/`description` and seed rows for the properties that exist at this point; the file grows one row per property leaf.
    - Failure is exit `1` on any `VACUOUS` row, on a missing row for any `Q-` id defined in Appendix B, or if `git status --porcelain` is non-empty after the run; add the Go variant using a `go build -overlay` temp module so no tracked file is ever edited.
    - Add meta tests: a deliberately vacuous property must be reported `VACUOUS`, and the harness must leave the working tree clean.
    - _Design: §0.4.5, §8.3.3, Appendix B; Deliverable: 1.11_

  - [x] 1.7 Add the CI-job existence check that keeps Appendix E honest

    - Implement `scripts/check-ci-jobs.py`: invocation `python scripts/check-ci-jobs.py .github/workflows/ci.yml .antigravity/specs/phase-1-mvp-core/design.md`; input is the workflow's `jobs:` keys and every backtick-quoted job name inside Appendix E.
    - Failure is exit `1` naming any job Appendix E cites that the workflow does not define, and exit `1` when the extracted set is empty.
    - Register it in `pre-commit` and add a negative fixture workflow proving it detects a missing job. Phase 0's Appendix E cited `build`, `test` and `lint` jobs that never existed; this makes that a build failure.
    - _Design: §8.3, §15.10, Appendix E; Deliverable: 1.11_

  - [x] 1.8 Exercise the regime's own safeguards end to end
    - Run tasks 1.1–1.7's checks together against the current tree and prove each fails on its negative fixture and passes on the real tree.
    - Assert the four authoritative root documents remain outside every mutating hook while staying inside Gitleaks, and that no new script writes to the repository during a check run.
    - _Design: §0.4, §8.3, §8.4; Deliverable: 1.11_

- [x] 2. Close the inherited debt that all later work sits on

  - [x] 2.1 Wire the model router from the shipped tier YAML in the app factory

    - In `backend/src/main.py`'s lifespan, call `load_tier_config(settings.model_tier_config_path, env=os.environ)`, build `EndpointRegistry.from_config(...)` and `ModelRouter(...)` from it, and expose the parsed config as `app.state.tier_config`.
    - Do not change `load_tier_config`'s signature; it stays `load_tier_config(path, env=None) -> TierConfig`.
    - Add `backend/tests/integration/test_wiring_tier_config.py`: copy `config/model-tiers.yaml` to a temp path, mutate a tier, point `MODEL_TIER_CONFIG_PATH` at the copy, rebuild via `create_app()`, and assert the running app's tier set changed. **No generation leaf may land before this test exists.**
    - _Design: §0.5 debt D1, §11.1, §11.5.4; Deliverable: 1.5; Criterion: 3; Property: Q-27_

  - [x] 2.2 Write property test Q-27 for tier-configuration provenance

    - Generate valid tier YAML documents into temporary paths and assert the tier set on the app built by `create_app()` equals the parsed document for every one, with no default fallback masking a load failure.
    - Add the `mutations.toml` row whose mutation hard-codes the tier map in the lifespan and ignores the configured path; the property must then fail.
    - _Design: §11.1, §11.5.4, Appendix B Q-27; Deliverable: 1.5; Criterion: 3; Property: Q-27_

  - [x] 2.3 Make `compose-smoke` actually start the stack

    - Extend the `compose-smoke` job to build the `backend` and `frontend` images and run `docker compose up -d --wait`, then assert the default service set is exactly the §2.3 set and all are healthy.
    - Add separate optional-profile evidence commands, each run only after its owning implementation task exists.
    - Keep the fresh-clone path intact: no committed `.env` required, `.env.example` supplies every value.
    - _Design: §0.5 debt D2, §2.3, §8.3, §13.3; Deliverable: 1.11; Criterion: 1_

  - [x] 2.4 Harden the supply chain and remove every floating tool version

    - Create `agent/tools/go.mod` + committed `go.sum` pinning `golangci-lint v1.62.2` and `golang.org/x/vuln/cmd/govulncheck v1.1.4`, and run both via `go run` so `go.sum` verifies the checksum.
    - Remove `|| true` from `pnpm audit` and gate it at `--audit-level high`; add a hash-pinned `requirements-tools.lock` installed with `--require-hashes` for `pre-commit`, `pip-audit` and `pip-tools==7.6.0`.
    - Implement `scripts/check-no-latest.sh`, which greps every workflow, script and Dockerfile for `@latest` and fails on a match; wire it into `pre-commit`.
    - _Design: §0.5 debt D4, §8.4, §16.1; Deliverable: 1.11_

  - [x] 2.5 Digest-pin every image and prove the OPA container is not root

    - Replace `infisical/infisical:v0.91.1` with a tag-plus-digest reference (`v0.162.15`, per D-52 — `v0.91.1` was never published) and keep the OPA reference digest-pinned, resolving both digests at implementation time.
    - Extend `scripts/check-compose-validate.py` to fail if **any** image reference lacks `@sha256:`, if any service overrides its image's runtime user back to root, or if a `<committed-digest>` placeholder survives.
    - Add fixture compose files proving each of the three failure modes is detected.
    - **Corrected by D-51.** The original second failure mode — "the OPA tag does not end in `-rootless`" — rested on a tag OPA 1.x does not publish, while the pinned `1.4.2` image already runs as `USER 1000:1000` on a Chainguard base. A tag substring was never evidence of a runtime user, so the runtime proof moved to `compose-smoke`, which asserts `id -u` inside the running `opa` container is not `0`.
    - _Design: §0.5 debt D5, §8.4, §13.3, §16.4, §17.1 D-51, D-52; Deliverable: 1.8; Criterion: 8_

  - [x] 2.6 Restore lockfile diff visibility

    - Drop `-diff` from all four lockfile entries in `.gitattributes` while keeping `linguist-generated`, so a dependency bump is reviewable.
    - Add a check asserting no lockfile carries `-diff` and that `grammars.lock.json` is treated the same way when it arrives.
    - _Design: §0.5, §8.5, §16.5; Deliverable: 1.11_

  - [x] 2.7 Tighten P-07's shutdown-timeout assertion with a slow closer
    - Replace the instantaneous closers in `agent/internal/app/app_test.go`'s timeout clause with a deliberately slow closer so the configured `ShutdownTimeout` bound is actually exercised.
    - Keep P-07's other clauses (reverse order, exactly-once, continue-past-error, idempotence) unchanged and still passing.
    - _Design: §0.5, §10.4; Deliverable: 1.1_

- [x] 3. Extend backend core primitives for Phase 1

  - [x] 3.1 Extend `Settings` with the Phase 1 configuration surface

    - Add the auth, pairing, envelope, scanner, retrieval, generation, governance, secrets, tasks and pooling fields from §13.1 to `backend/src/core/config.py`, keeping `extra="forbid"` and the accumulate-all-errors contract.
    - Type `generation_max_iterations` as `Literal[3]` so the safety bound cannot be raised by an environment variable, and add the validator rejecting `CHUNK_OVERLAP_TOKENS >= CHUNK_TARGET_TOKENS`.
    - Add `MCP_AGENT_BLAST_RADIUS` rejection when `APP_ENV=production`, and update `.env.example` with the full §13.1 additions using placeholder-only secret values.
    - Add examples proving every new key is validated together and that unrelated ambient OS variables are still ignored.
    - _Design: §7.1, §13.1, §17.1 D-39; Deliverable: 1.5, 1.11; Property: Q-30_

  - [x] 3.2 Extend the RFC 9457 registry with the Phase 1 problem types

    - Add every suffix in Appendix C.1 to `backend/src/core/errors.py` as stable `https://errors.forgeops.dev/...` types with their fixed statuses.
    - Implement the non-disclosing 403: a forbidden response body must be byte-identical whether or not the resource exists.
    - Keep D-27's traceback redaction and add tests that no new problem type's `detail` can carry a secret pattern, a connection string or exception text.
    - _Design: §4.2, §11.2, Appendix C.1; Deliverable: 1.11; Property: Q-20, Q-24_

  - [x] 3.3 Fill middleware row 6 with tenant context and transaction-scoped tenancy

    - Add `TenantContextMiddleware` at position 6 resolving the tenant from the verified principal into a `contextvar`, and issue `SET LOCAL app.tenant_id` inside the transaction from `get_session`.
    - Add `DATABASE_POOLER_MODE`; in `transaction` mode set asyncpg `statement_cache_size=0` and disable prepared-statement reuse. Create no RLS policies and set no column `NOT NULL`.
    - Add an integration test proving the variable is visible to `current_setting('app.tenant_id', true)` inside the transaction and **absent in the next transaction on the same pooled connection** — the assertion that proves `SET LOCAL` rather than `SET`.
    - _Design: §4.3, §6.7, §7.12, §17.1 D-35; Deliverable: 1.11_

  - [x] 3.4 Add `ArqDispatcher` behind the unchanged task seam

    - Implement `ArqDispatcher` in `backend/src/core/tasks.py` mapping `(name, payload, idempotency_key)` onto ARQ's job id, returning `TaskHandle(dispatcher="arq")`, and pin `arq==0.26.3`.
    - Leave `TaskDispatcher`, `TaskHandle`, `_TASK_HANDLERS` and `@register_task` untouched, and leak no engine concept upward — no job object, no workflow id, no signal, no query.
    - Extend the Ruff banned-api rule so `arq` cannot be imported outside `core/tasks.py`, and add a test running the whole registered handler set under both `InlineDispatcher` and `ArqDispatcher` with identical results.
    - Add an `arq` worker entry point and `make worker`, plus a duplicate-enqueue test proving idempotency-key deduplication.
    - _Design: §4.6, §7.10, §11.1, §17.1 D-32; Deliverable: 1.3, 1.5_

  - [x] 3.5 Add the shared JCS canonicalisation primitive
    - Pin `rfc8785==0.1.4` and add a thin `backend/src/core/canonical.py` wrapper used by both envelope signing and the audit hash chain, so two subsystems cannot diverge on canonical bytes.
    - Reject any input containing a float, matching §7.6's rule that no envelope or audit payload contains one.
    - Add tests over the RFC 8785 test vectors plus the project's own fixture corpus.
    - _Design: §7.6, §11.9, §16.2, Appendix A.2, A.8; Deliverable: 1.1, 1.9; Property: Q-14_

- [x] 4. Extend Go agent primitives before any session or executor work

  - [x] 4.1 Extend the typed Go configuration loader

    - Add the pairing, session, journal, identity, scanner and validator fields from §13.1 to `agent/internal/config`, preserving the single joined error containing every problem and ignoring unrelated ambient keys.
    - Add table examples for defaults, combined failures, and invalid durations/enums/paths.
    - _Design: §7.1, §13.1; Deliverable: 1.1_

  - [x] 4.2 Make the redacting logger the only agent logger

    - Change `app.New` to construct the logger via `logging.NewRedacted` only, and add a wiring assertion that no other constructor is reachable.
    - Add tests injecting synthetic self-labelling credentials into logged values and asserting they never appear in captured logs.
    - **Resequenced.** The "route scanner and validator diagnostics through `secretscan.Redact`" bullet moved to leaf 10.1, which is the leaf that creates `secretscan.Redact`. It was unbuildable here: this leaf sits in wave 2 and `agent/internal/secretscan` does not exist until wave 12.
    - _Design: §7.2, §14.5; Deliverable: 1.8; Property: Q-24_

  - [x] 4.3 Replace `taskkill` with Windows Job Objects

    - Implement `setProcessGroup`/`terminateGroup` in `agent/internal/iac/procattr_windows.go` using `golang.org/x/sys/windows` `CreateJobObject`, `SetInformationJobObject` with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`, and `AssignProcessToJobObject`; terminate gracefully then close the job handle.
    - Keep the build cgo-free so §8.2's six-target matrix is unaffected.
    - Add an integration test that spawns a process which spawns a **detached grandchild** and asserts both are gone after termination — the case `taskkill /T` misses.
    - _Design: §8.2, §10.11, §17.1 D-37; Deliverable: 1.1_

  - [x] 4.4 Add the identity seam and the paired-device provider

    - Create `agent/internal/identity` with the `Provider` interface (`ClientTLS`, `Identity`, `RenewBefore`) and the `PairedDevice` implementation: generate a P-256 key pair in memory, build a CSR, and never transmit the private key.
    - Add the compile-time assertions for both providers and reject any implementation path that would persist a non-expiring credential.
    - Add tests for CSR shape, TLS config assembly from an issued certificate, and `RenewBefore` behaviour near expiry.
    - _Design: §10.2, §14.3, §17.1 D-36; Deliverable: 1.1, 1.10_

  - [x] 4.5 Implement the credential store with a reported fallback

    - Implement `session.Store` over the OS keychain via `zalando/go-keyring`, with a `0600` file fallback when no keychain is available, and a `Backend()` accessor surfaced by `agent doctor`.
    - Persist only `Credentials` as defined in §10.3; assert the file mode on every load and refuse a world-readable file.
    - Add tests for save/load/wipe round trips on both backends and for the degraded-mode report.
    - _Design: §10.3, §10.10, §17.2 OQ-26; Deliverable: 1.1_

  - [x] 4.6 Implement the durable outbound journal

    - Implement `agent/internal/session/journal.go`: append-only file at `0600` under `AGENT_STATE_DIR`, length-prefixed records with CRC32C, `fsync` on append, bounds from `AGENT_JOURNAL_MAX_BYTES`/`AGENT_JOURNAL_MAX_AGE_HOURS`, truncation after a successful drain, and a corrupt tail record discarded with a warning rather than a startup failure.
    - Define `RecordKind` with exactly the six kinds in §10.3 and **no kind** for an envelope, `approval_id`, authority, device token, envelope key or secret value, so an authorisation cannot be represented let alone persisted.
    - Implement `Append`/`Drain`/`Wipe`/`Stats`, returning `ErrJournalFull` rather than evicting silently, and expose backlog in `agent.status` and `agent doctor`.
    - Add tests for bounded growth, corrupt-tail recovery, drain ordering (non-mutating records before intents), and wipe-on-revocation.
    - _Design: §10.3, §7.4, §17.1 D-41, Appendix C.2, Appendix D; Deliverable: 1.1; Criterion: 1; Property: Q-31_

  - [x] 4.7 Split the `fileops` path blocklist by intent
    - Implement `blockedForRead` (Phase 0 strictness unchanged) and `blockedForWrite` (identical plus exactly three permitted names: `.env.example`, `.env.sample`, `.env.template`) in `agent/internal/fileops/blocklist.go`.
    - Use a closed list of names, never a glob, so `.env.production.example.bak` stays blocked.
    - Add an enumerated matrix test over `.env`, `.env.local`, `.env.production`, `.env.example`, `.env.example.bak`, `.envrc` and `sub/.env` for both intents, and keep P-08's read clause passing.
    - _Design: §7.11(f), §17.1 D-46; Deliverable: 1.5, 1.8; Property: Q-01_

- [x] 5. Implement the Phase 1 schema as eight linear migrations, each with a gated proof

  - [x] 5.1 Add `0002_identity_and_devices`

    - Create the `citext` extension, the `user_role` and `device_status` enums, and the `users`, `sessions` and `agent_devices` tables exactly as §6.3 defines them, plus the `forgeops_app` and `forgeops_migrator` roles.
    - Store `refresh_token_hmac`, `pairing_token_hmac` and `device_token_hmac` as HMACs and `envelope_key_enc` as AES-256-GCM ciphertext; never a plaintext token column.
    - Add `backend/tests/integration/test_0002_identity.py` under `require_capability("postgres")`: enum values, unique `idp_subject`, `CITEXT` case collapsing, `ondelete=CASCADE` from user to sessions, and the app role's grants.
    - _Design: §6.1, §6.3, §6.5; Deliverable: 1.1, 1.11; Criterion: 1_

  - [x] 5.2 Add `0003_codebase_index_extensions`

    - Create `file_contents`, `file_dependencies`, `analysis_reports` and `embeddings_local` with its HNSW cosine index at `m=16, ef_construction=64`; add the nullable cAST columns to `embeddings`; create `pg_trgm` and the `file_tree.path` trigram index.
    - Keep `embeddings.embedding` at `vector(1536)` and `model_id` `NOT NULL` on both vector tables (D-2, D-48); add the reverse-dependency index the incremental closure needs.
    - Add `test_0003_index.py`: both column dimensions, both HNSW indexes with `vector_cosine_ops` and the exact build parameters, the reverse-dependency index, and `with_ef_search` applying per transaction only.
    - _Design: §6.3, §6.4, §6.5, §17.1 D-48; Deliverable: 1.3; Criterion: 12_

  - [x] 5.3 Add `0004_change_sets_and_approvals`

    - Create `change_sets`, `change_items`, `validations`, `approvals` and `rollback_handles` per §6.2/§6.3, including the `status` check constraint, the `version` optimistic-concurrency column and unique `(change_set_id, ordinal)`.
    - Store per-item `old_hash`/`new_hash` so a stale apply can be detected at the agent.
    - Add `test_0004_change_sets.py`: an unknown status is rejected, `version` defaults, ordinal uniqueness holds, and cascade behaviour matches the declared `ondelete`.
    - _Design: §6.2, §6.3, §6.5, §3.6; Deliverable: 1.6; Criterion: 5_

  - [x] 5.4 Add `0005_policies_and_bundles`

    - Create `policies`, `policy_evaluations` and `policy_bundles`, with a unique bundle `digest` and a partial unique index enforcing one active bundle per scope.
    - Record the evaluating `side` (`backend`/`agent`) on every evaluation row so double-evaluation disagreements are visible in data.
    - Add `test_0005_policies.py` proving two active global bundles violate the partial unique index.
    - _Design: §6.1, §6.3, §6.5, §11.7; Deliverable: 1.7; Criterion: 7_

  - [x] 5.5 Add `0006_secrets`

    - Create `secrets` with `environment TEXT NOT NULL` constrained to `dev|test|staging|prod`, unique `(project_id, environment, key)`, **no FK** to a Phase 2 table, and a check constraint making exactly one of `infisical_path` and `encrypted_value` non-null.
    - Add `test_0006_secrets.py`: uniqueness, the environment constraint, the exclusivity constraint, and that no plaintext column is writable when `SECRET_BACKEND=infisical`.
    - _Design: §6.3, §6.5, §6.6, §17.1 D-50; Deliverable: 1.8; Criterion: 8_

  - [x] 5.6 Add `0007_audit_append_only` with database-enforced immutability

    - Create `audit_events` per §6.3 with `seq BIGSERIAL`, `prev_hash`, `hash`, and no FK on `project_id`/`actor_user_id` so a record survives deletion of what it describes.
    - Add the `audit_events_immutable()` trigger function and the three `BEFORE UPDATE`/`DELETE`/`TRUNCATE` triggers, then `REVOKE UPDATE, DELETE, TRUNCATE ... FROM forgeops_app` and grant only `INSERT, SELECT`.
    - Implement `scripts/check-db-roles.py` asserting the running application role lacks UPDATE on `audit_events`, and add `test_0007_audit.py`: INSERT succeeds; UPDATE, DELETE and TRUNCATE each raise `42501`; the app role has no UPDATE privilege.
    - _Design: §6.3, §6.4, §6.5, §11.9; Deliverable: 1.9; Criterion: 9; Property: Q-05_

  - [x] 5.7 Add `0008_generation_runs`

    - Create `generation_runs` per §6.2 with the `iterations_used BETWEEN 0 AND 3` check constraint, so the 3-iteration bound is expressed in the schema as well as in the type and the property.
    - Record `served_from`, `tier`, `endpoint_id`, `rubric`, `retrieval` and token counts for NFR-04 evidence.
    - Add `test_0008_generation_runs.py` proving `iterations_used = 4` is rejected.
    - _Design: §6.1, §6.2, §6.5, §11.5; Deliverable: 1.5; Criterion: 3; Property: Q-08_

  - [x] 5.8 Add `0009_project_tags_and_settings`

    - Create `project_tags` with per-project uniqueness and add validation for the `projects.settings` keys `embedding_backend`, `llm_budget_usd_month`, `favourite`, `auto_approve_readme_only`, `max_file_size_bytes` and `ignore_globs`.
    - Add `test_0009_projects.py`: tag uniqueness, and the settings validator rejecting an unknown embedding backend.
    - _Design: §6.5, §11.3; Deliverable: 1.2_

  - [x] 5.9 Add the cross-cutting migration integrity tests
    - Add `test_alembic_linearity.py` asserting a single head and that every `down_revision` chain reaches `0001_initial` with no branches.
    - Add `test_alembic_autogenerate_clean.py` running `alembic upgrade head` then `--autogenerate` and asserting an empty diff, which catches model/migration divergence and naming-convention slips in one assertion; extend `render_item` coverage to the 1024-d vector column.
    - Gate both under `require_capability("postgres")` so they fail rather than skip in CI.
    - _Design: §6.4, §6.5; Deliverable: 1.11; Criterion: 12_

- [x] 6. Implement authentication, authorization and the identity provider service

  - [x] 6.1 Implement token verification, principals and deny-by-default routing

    - Implement `AppTokenVerifier` extending Phase 0's JWKS verifier with a **distinct** app audience, and the frozen `Principal` carrying role, tenant, kind and identity-derived `blast_radius`.
    - Implement `require_principal`, `require_role` and the committed `PUBLIC_ROUTES` set containing exactly the seven §4.4 entries; attach the dependency **per route**, never globally.
    - Implement `scripts/check-route-auth.py`, which enumerates `create_app().routes` and fails when a route lacks the dependency and is not public, plus a negative fixture proving detection.
    - _Design: §4.4, §11.2, §14.1; Deliverable: 1.11; Criterion: 1; Property: Q-19, Q-30_

  - [x] 6.2 Implement the OIDC authorization-code + PKCE flow and session lifecycle

    - Implement `/api/v1/auth/login`, `/callback`, `/refresh` and `/logout` over `httpx` + `pyjwt` with no new auth library; upsert `users` from the IdP subject and persist `sessions` with the refresh token stored as an HMAC.
    - Set an httpOnly `SameSite=Lax` session cookie and return the access token in the body; map IdP groups to exactly one of `admin`/`developer`/`viewer`.
    - Add integration tests against a fixture issuer whose signing key is generated per test run: successful exchange, replayed `state`, expired token, wrong audience, and refresh rotation.
    - _Design: §3.5, §11.2, §13.1; Deliverable: 1.11; Criterion: 1_

  - [x] 6.3 Add the Authentik services and the real-IdP CI job

    - Add `authentik-server` and `authentik-worker` to the **default** Compose profile with digest-pinned images sharing the existing Postgres and Redis, plus the §13.1 bootstrap variables as placeholders.
    - Add the `auth` CI job exercising the real code+PKCE flow and the RBAC matrix against the container, gated by `require_capability("oidc")`.
    - Keep Authentik out of `/health/ready`: an IdP outage must degrade login, not readiness of authenticated traffic.
    - _Design: §2.3, §8.3, §13.3, §16.4, §17.1 D-34; Deliverable: 1.11; Criterion: 1_

  - [x] 6.4 Add the Cerbos sidecar and resource-scoped authorization

    - Add the digest-pinned `cerbos` service with `policies/cerbos/` and `config/cerbos/`, pin the `cerbos==0.14.0` client, and implement `require_permission` calling it.
    - Author the §11.2 policy matrix for `project`, `change_set`, `policy`, `secret`, `agent_device` and `audit`, including that **no role** may read a secret value and that self-approval can be barred by policy.
    - Add `/health/ready` coverage for Cerbos and integration tests over the full role × resource × action matrix.
    - _Design: §2.3, §5.5, §11.2, §13.3, §16.4; Deliverable: 1.11; Criterion: 8; Property: Q-20_

  - [x] 6.5 Write property test Q-19 for deny-by-default route coverage

    - Generate requests without tokens against every route registered by `create_app()`; prove each route either depends on `require_principal` or is in `PUBLIC_ROUTES`, that unauthenticated calls return 401, and that no handler body executes.
    - Add the `mutations.toml` row dropping `require_principal` from `GET /api/v1/projects` without adding it to `PUBLIC_ROUTES`.
    - _Design: §4.4, §11.2, Appendix B Q-19; Deliverable: 1.11; Property: Q-19_

  - [x] 6.6 Write property test Q-20 for RBAC and secret-value confinement

    - Generate `(role, resource, action)` triples; prove a viewer is denied every mutating action, no role can read a secret **value** through any route, and a 403 body is byte-identical whether or not the resource exists.
    - Add the `mutations.toml` row adding `read_value` to the viewer's Cerbos policy.
    - _Design: §4.2, §11.2, §11.8, Appendix B Q-20; Deliverable: 1.8, 1.11; Criterion: 8; Property: Q-20_

  - [x] 6.7 Write property test Q-30 for identity-derived blast radius
    - Generate principals and environments; prove `blast_radius` is derived from the verified identity, that `MCP_AGENT_BLAST_RADIUS` cannot widen it for an authenticated caller, and that its presence with `APP_ENV=production` is a startup error.
    - Assert `policies/mcp/gateway.rego` is unchanged and its 27 tests still pass, as OQ-20 anticipated.
    - Add the `mutations.toml` row making the gateway read the env var when a principal is present.
    - _Design: §11.2, §13.1, §17.1 D-39, Appendix B Q-30; Deliverable: 1.10, 1.11; Property: Q-30_

- [x] 7. Build the governance chokepoint and the mutation boundary before any mutating operation

  - [x] 7.1 Implement the mint-only capability type and the primitive marker

    - Implement `backend/src/governance/authority.py` with the module-private `_MINT_SENTINEL` and the frozen `MutationAuthority` whose `__post_init__` raises `TypeError` for any other sentinel.
    - Implement `@mutation_primitive` in `governance/primitives.py` and add the §2.2.1 Ruff banned-api entries for `_MINT_SENTINEL`, `sign_envelope`, `_SIGNING_KEY`, `hub.send_command` and `devices.envelope_key`.
    - Add unit tests proving construction outside `governance/` raises, and that the banned-api rule rejects each forbidden import.
    - _Design: §2.2.1, §5.4, §11.6; Deliverable: 1.10; Property: Q-03_

  - [x] 7.2 Move the agent's only write path behind a compiler-enforced boundary

    - Create `agent/internal/executor/internal/mutate` and move the Phase 0 atomic-apply implementation there unchanged in algorithm, exposing `ApplyVerified(ctx, *envelope.Verified, root, entries)` and `Revert(ctx, *envelope.Verified, manifest)`.
    - **Signature fixed by D-59.** This leaf originally wrote `*session.Verified`, which does not compile: `session` imports `executor` (§10.1, §10.3), so `mutate` taking a `session` type closes a cycle. D-59 creates the leaf package `agent/internal/envelope` and it lands in this leaf's commit, because the boundary cannot compile without it.
    - Require an `ExpectedHash` per entry and abort the whole set with `ErrConflict` before any write on mismatch; return a `BackupManifest` as the rollback handle; apply `blockedForWrite`.
    - Keep `fileops.UnifiedDiff` and the path helpers exported and unchanged so P-08 still guards them; prove that a package outside `internal/executor/**` importing `mutate` does not compile.
    - _Design: §2.2.1, §10.1, §10.5, §17.1 D-45, Appendix A.9; Deliverable: 1.6; Criterion: 6; Property: Q-01, Q-02, Q-03_

  - [x] 7.3 Implement the chokepoint reachability check and test it

    - **Resequenced to run after 7.6, and it ran here.** This leaf's own non-vacuity rule made it unbuildable in its original position. §2.2.1 requires the check to `exit 1` when the discovered primitive set is **empty**, and the set is discovered by scanning `backend/src/**` for the `@mutation_primitive` decorator. After 7.1 that decorator had **zero** uses, so the check would correctly have refused to pass on a correct tree. With 7.5 and 7.6 landed the set is non-empty — `AuditWriter.append` is the one primitive, called from exactly one place, `governance/chokepoint.py` — so the check now passes for the right reason. Same disposition as leaves 2.5 and 4.2: the leaf's wording was what blocked it, not its work.
    - Implement `scripts/check-chokepoint.sh`: the Go half asserts no import of `executor/internal/mutate` outside `executor/**` using `go list -deps -json`; the Python half AST-walks `backend/src/**` and asserts every call to a `@mutation_primitive` function is inside `src/governance/` or receives a `MutationAuthority`.
    - Failure is exit `1` printing offenders, **and also** exit `1` when the discovered primitive set is empty, so a renamed decorator cannot make the check trivially pass.
    - Wire it into the `agent` and `backend` jobs and `pre-commit`; add negative fixtures for both halves.
    - **Two refinements, recorded as D-67.** Matching call sites on a bare method name is unusable — `AuditWriter.append` and `list.append` share one — so an attribute call counts only when its receiver **resolves to the owning class**, and a receiver the analysis cannot type is a third verdict, `unresolved-receiver`, which blocks. And the Go half's vacuity guard is on the **enumeration** §2.2.1 names as its input (the import graph must be non-empty and contain the boundary package), not on the importer set: only `executor` may import the boundary and its dispatcher arrives in leaf 8.7, so zero importers is today's correct answer.
    - The analysis lives in `scripts/chokepoint_graph.py` so leaf 7.7's Q-03 quantifies over the same implementation the gate runs, rather than a second one that could disagree.
    - _Design: §2.2.1, §8.3, §11.6, §17.1 D-67; Deliverable: 1.10; Property: Q-03_

  - [x] 7.4 Implement envelope canonicalisation, signing and the shared fixture corpus

    - Implement `governance/envelope.py`: JCS canonicalisation of the envelope without `signature`, the `"forgeops-envelope-v1" || 0x00` domain-separation prefix, and `HMAC-SHA256` under the per-device envelope key; add the `"forgeops-approval-v1"` variant for `approval.response`.
    - Create `agent/testdata/envelopes/*.json` holding envelopes with expected canonical bytes and signatures under a **synthetic self-labelling** test key, read by both the Python and Go tests so a divergence fails both suites.
    - Assert no envelope field is ever a float, and that the signing key is reachable only from `governance/`.
    - _Design: §7.6, §11.6, Appendix A.2; Deliverable: 1.1, 1.10; Property: Q-14_

  - [x] 7.5 Implement the six-stage chokepoint with authority mint

    - **Three prerequisites landed in this leaf's commit, each recorded as a numbered decision.** **D-62** decides envelope-key custody (HKDF-SHA256 from `ENVELOPE_PEPPER`, AES-256-GCM sealed into `agent_devices.envelope_key_enc` with the device id as AAD) and creates `backend/src/auth/devices.py` with the custody half of `DeviceService`; pairing-code issue and exchange stay with leaf 8.1, the CA with 8.2. **D-63** adds revision `0010`, because `0004`'s `CHANGE_SET_STATUSES` disagreed with §3.6 in nine places and three of A.3's six outcomes — `blocked`, `pending_approval`, `reverted` — were unstorable. The **`seq` and nonce allocator** (`governance/sequencing.py`, Redis Lua CAS per §7.6) lands here too: leaf 8.4 lists it among the hub's bullets, but an envelope cannot be *minted* without a `seq`, so the allocator precedes the hub. Same disposition as leaves 2.5, 4.2, 7.3 and 7.6.
    - Implement `GovernanceChokepoint.submit`, `.approve` and `.revert` executing admission → policy → approval gate → change-set compilation → blast radius → audit → rollback handle in exactly that order, with `mint_authority` reachable only after all six.
    - Fail closed on an OPA outage, raise `governance-policy-undefined` (503) for an undefined document, refuse a `policy_stale` or revoked device, and write exactly one audit record on every early return.
    - Reserve the rollback handle **before** any envelope exists, use optimistic concurrency on `change_sets.version`, and make `revert` mint its own authority rather than reusing the original.
    - Add integration tests over `production_app` for deny, block, pending, auto-approve, approve, apply and revert, asserting envelope presence or absence per path.
    - **`production_app` split, recorded.** That fixture points the app at unreachable Postgres and Redis by design (§0.4.1), so it can prove *composition* and cannot prove a *transaction*. `test_wiring_governance.py` drives the five new `app.state` names through the real factory and asserts both fail-closed defaults; `test_governance_chokepoint.py` drives the seven transits against `forgeops-test-pg` and `forgeops-test-redis`. The two collaborators substituted there — the policy source and the command sink — are the two whose production article does not exist yet (leaves 9.2 and 8.4), and both doubles are hand-written classes implementing the real Protocol, never a `Mock`, which `FO-TD004` forbids under `tests/integration/**`.
    - _Design: §2.2, §5.4, §11.6, Appendix A.3; Deliverable: 1.6, 1.10; Criterion: 5, 7; Property: Q-03, Q-04, Q-22_

  - [x] 7.6 Implement the append-only audit writer and chain verification

    - **One bullet resequenced.** "write agent-side records from the hub with `actor_kind="agent"`" needs `backend/src/websocket/hub.py`, which does not exist — `backend/src/websocket/` holds only its `README.md` until group 8 builds the session protocol. The writer half of that clause is implemented and asserted here: `actor_kind="agent"` is a member of the closed `ACTOR_KINDS` vocabulary and a draft carrying it is **required** to name `actor_device_id`, proved by `test_an_agent_actor_must_name_the_device`. Wiring the call site lands with the hub, in the leaf that creates it. Same disposition as leaves 2.5, 4.2 and 7.3: the leaf's wording reached one wave forward, not its work.
    - Implement `AuditWriter.append` joining the caller's transaction, taking a transaction-scoped advisory lock keyed by tenant, and computing `hash = sha256(JCS(semantic fields) || prev_hash)`.
    - Require all six NFR-14 fields including a non-empty `reason`; implement `verify_chain` reporting the first divergent `seq` and expose it as admin-only `GET /api/v1/audit/verify` plus `make verify-chain`.
    - Add the query API with filtering and cursor pagination, and write agent-side records from the hub with `actor_kind="agent"` so agent operations are covered without giving the agent database access.
    - _Design: §6.3, §6.4, §11.9, Appendix A.8; Deliverable: 1.9; Criterion: 9; Property: Q-04, Q-05_

  - [x] 7.7 Write property test Q-03 for chokepoint unbypassability

    - Generate call graphs over `backend/src/**` and package graphs over `agent/internal/**`; prove no `@mutation_primitive` is reachable without a `MutationAuthority`, that the type cannot be constructed outside `governance/`, and that `mutate` has no importer outside `executor/**`.
    - Add the `mutations.toml` row deleting the `_MINT_SENTINEL` check in `__post_init__`.
    - **The generated graphs found a defect in the gate leaf 7.3 had just landed.** `classify_importers` tested the executor prefix with a bare `startswith`, so `agent/internal/executorish` — a different package that merely shares the prefix as a string — was reported as **permitted**. Go itself refuses to compile that import, so the check was more lenient than the mechanism it polices. Fixed by making the path separator part of the test; the generator keeps `executorish` in its sample set so the property notices if it ever regresses.
    - The property imports the **same** analysis module `scripts/check-chokepoint.sh` runs, so the gate and the property cannot come to disagree about what "reachable without authority" means — the Q-06/Q-14 lesson applied to a lint.
    - _Design: §2.2.1, §11.6, §17.1 D-67, Appendix B Q-03; Deliverable: 1.10; Property: Q-03_

  - [x] 7.8 Write property test Q-04 for audit completeness per transit

    - Generate chokepoint transits across allow, deny, block, pending, apply and revert; prove exactly one `audit_events` row per transit written in the same transaction as the state change, and that a rolled-back transaction leaves neither.
    - Add the `mutations.toml` row moving the audit write outside the transaction.
    - **Transit kinds are generated as SEQUENCES, not one per test.** A per-kind example proves each kind writes one record and says nothing about ordering, where the interesting failures are: a refusal that writes two because a later stage re-audited, or one that writes none because the exception outran the commit. The property also checks the row *actions* in order, because "six rows for six transits" is satisfied by six copies of the wrong record.
    - **`apply` is not a separate kind.** A.3's apply path *is* the auto-approved transit, whose record is `change_set_auto_approved`; counting it twice would double-count one transit.
    - The transit fixtures moved to `tests/integration/chokepoint_support.py` so the property and the integration suite share one definition of what a transit is — two copies is how the property comes to quantify over a shape the integration tests never exercise.
    - _Design: §11.6, §11.9, Appendix A.3, Appendix B Q-04; Deliverable: 1.9, 1.10; Criterion: 9; Property: Q-04_

  - [x] 7.9 Write property test Q-05 for audit immutability and tamper evidence

    - Generate audit sequences and tamper attempts; prove UPDATE and DELETE raise, that recomputing the chain from any start point reproduces every stored hash, and that altering one row's semantic fields makes `verify_chain` report that row's `seq` as the first divergence.
    - Add the `mutations.toml` row dropping `prev_hash` from the hashed payload.
    - **The control forced a fifth clause into the property, and the reason is worth reading.** Dropping the concatenated `prev_hash` term does **not** break tamper detection for a rewritten `prev_hash` column: D-61 compares that column against its predecessor's `hash` directly, whatever the digest covers. The first version of Q-05 therefore passed under its own control. What the concatenation is the only defence against is a **splice** — delete a middle row and re-link the next one to the row before it, after which every `prev_hash` legitimately equals its new predecessor's `hash` and the sole remaining objection is that the surviving row's hash was computed over the predecessor that is now gone. `TestASplicedChainIsDetected` is that clause; it carries its own control asserting the re-link was done *correctly*, so the detection test cannot be passing because of a clumsy edit.
    - The tamper matrix is guarded against no-ops: a variant equal to the honest value tampers with nothing, and the first run hit exactly that with `outcome="allowed"`. `test_no_tamper_value_matches_the_honest_row` closes it.
    - _Design: §6.4, §11.9, Appendix A.8, Appendix B Q-05, §17.1 D-61; Deliverable: 1.9; Criterion: 9; Property: Q-05_

  - [x] 7.10 Write property test Q-01 for atomic all-or-nothing application

    - Generate change-sets and injected failure points; prove either every target holds its new content with a backup per pre-existing target, or every target byte-equals its pre-image; prove no path outside `root` is written, `blockedForWrite` paths are refused, and `.env.example` is writable while `.env` is not.
    - Add the `mutations.toml` row removing the rollback loop from the `CATCH` branch.
    - **The failure injection is cross-platform, deliberately.** The existing example test uses a `0555` directory and is skipped on Windows, which is the "gate that can never pass locally" shape D-51 rejects. Q-01 injects by **ordering** instead: one entry creates a plain file `collide`, a later entry targets `collide/child.txt`, and `MkdirAll` refuses because the parent is a regular file. Both survive pre-validation, the failure position is generated, and it is deterministic everywhere.
    - **`rollback` was extracted into `rollback.go`** so the `go build -overlay` control replaces four lines rather than a copy of nearly six hundred. The overlay keeps `backupInfo` and the signature identical, so a change to either stops the mutated build compiling instead of silently ceasing to mutate anything.
    - **Two defects found in the tooling, both recorded in chapter 9.** The harness's Go argv put `-rapid.nofailfile` **before** the package pattern, so `go test` consumed the pattern as a flag value, the run died with `no Go files in <module>`, and the harness read that non-zero exit as "failed as required" — Q-01 was the first Go row, so nothing had exercised that path. And `scripts/check-go-module.sh` still listed `internal/executor` as a structural directory, so it had been failing since leaf 7.2 populated it; it is wired into neither CI nor pre-commit nor `make lint`, which is why nobody noticed.
    - _Design: §10.5, §7.11(f), Appendix A.9, Appendix B Q-01, §17.1 D-46; Deliverable: 1.6; Criterion: 6; Property: Q-01_

  - [x] 7.11 Write property test Q-02 for byte-exact revert
    - Generate apply-then-revert sequences; prove `Revert(manifest)` restores every file byte-for-byte including deleting files that did not previously exist, that revert is idempotent, and that a consumed handle cannot be reused.
    - Add the `mutations.toml` row making `Revert` skip entries marked `NO_PREVIOUS`.
    - _Design: §10.5, §11.6, Appendix B Q-02; Deliverable: 1.6; Criterion: 6; Property: Q-02_

- [x] 8. Implement agent pairing, the session protocol and the named-operation executor

  - [x] 8.1 Implement pairing-code issue and single-use exchange

    - **`backend/src/auth/devices.py` already exists.** Leaf 7.5 created it for D-62's envelope-key custody — `derive_key_encryption_key`, `seal_envelope_key`, `unseal_envelope_key`, the banned module-level `envelope_key`, and `DeviceService.{envelope_key,provision_envelope_key,active_device_for}`. This leaf **extends** that class; `exchange` must call `provision_envelope_key` rather than sealing a key itself, so there is one sealing path and D-62's AAD binding cannot be bypassed by a second one.
    - Implement `DeviceService.issue_pairing_code` and `.exchange`: a 6-character Crockford base32 code from a CSPRNG, storage of only its HMAC under `ENVELOPE_PEPPER`, one live code per project, and a Redis Lua consume script performing fetch, attempt increment, burn-on-exceed and delete-on-success atomically.
    - Add per-IP and global exchange rate limits, constant-time comparison, and one indistinguishable `pairing-code-invalid` response for unknown, expired, burned and consumed codes; write an audit record on failure that never contains the code.
    - Expose `POST /api/v1/agents/pairing-codes` (authorized) and `POST /api/v1/agents/pair/exchange` (the only new public route), plus `DELETE /api/v1/agents/{id}` for revocation.
    - Add integration tests for expiry, burn, single use, and the §14.6 rate-limit caps.
    - **D-70 settled where a non-change-set audit write may live**, which had to be decided before any code: `AuditWriter.append` is a `@mutation_primitive`, so a call from `devices.py` classifies `no-authority`. The answer confines the write by the **shape** of its record — a `DeviceAuditEvent` whose action vocabulary is disjoint from `GovernanceAction`, whose `resource_kind` is a constant rather than a field, which has no before/after pair, and whose `details` keys are a closed set — with the write itself in `governance/device_audit.py`, reached through a Protocol declared in `audit/`. Location alone would have been a second entry point to the whole audit vocabulary, and Q-04 cannot see a transit-shaped row written by another writer, so it would have kept passing while its property stopped holding.
    - **D-71 registered three problem types Appendix C.1 had no row for** — `pairing-unavailable` (503), `csr-invalid` (400), `device-not-found` (404) — and **D-72** moved every request-shaped check in front of the Redis consume and gave §3.1's `fingerprint` field a checked definition (the CSR's SubjectPublicKeyInfo SHA-256).
    - **Finding 55, in pre-existing code.** A per-file `["TID251"]` ignore suppresses the whole Ruff rule, not one banned-api entry, so the four domain globs in `pyproject.toml` unbanned §2.2.1's private surface for `src/ai`, `src/mcp`, `src/analysis` and `src/projects` — and the comment above them asserted the opposite. Measured with `ruff check --stdin-filename`. Mechanism 2 is now re-asserted by a `CONFINED_NAMES` table parsed in `check-chokepoint.sh`'s Python half, which no lint ignore can switch off.
    - **Two things this leaf deliberately does not do,** both by the group's own decomposition: the exchange issues no certificate or `ca_bundle` (the internal CA is 8.2), and revocation writes no Redis `devtok:revoked` member and no pub/sub event (per-message enforcement is 8.4, Q-16).
    - _Design: §3.1, §4.4, §10.3, §11.2, §14.6, Appendix A.1, §17.1 D-70, D-71, D-72; Deliverable: 1.1; Criterion: 1; Property: Q-17_

  - [x] 8.2 Implement the internal CA and short-lived device certificates

    - Implement CSR signing with `cryptography==44.0.0` producing certificates with `notAfter = now + DEVICE_CERT_TTL_HOURS` (default 24), and `rotate_certificate` issuing a replacement over the live session before `renew_after`.
    - Add `scripts/init-ca.sh` and `make init-ca` writing the development CA into `.env` only when absent, never overwriting, and never committing key material.
    - Store `cert_serial` and `cert_fingerprint` on `agent_devices`; add tests for chain validation, expiry rejection, fingerprint mismatch and rotation without reconnection.
    - **`cryptography` is pinned at `49.0.0`, not `44.0.0`.** §15.9's resolution is that the committed lockfiles win and Phase 1 must not downgrade any of them; `pyjwt[crypto]==2.13.0` already resolves 49.0.0. The pin was made explicit in `backend/pyproject.toml` by leaf 7.5 with that reasoning recorded there, so this leaf consumes it rather than re-deciding it.
    - **D-73** settles what the certificate says: the CA **discards** the CSR's subject and issues `CN=<device_id>` (a CSR arrives on an unauthenticated route, and a CA that copies caller-supplied data into an identity field lets the caller choose who it is), issues no SAN, and carries `clientAuth` only. The chain check is a **precondition**; `agent_devices.cert_fingerprint` is the authorisation input, and the two are deliberately not collapsed.
    - **D-74** extends §11.2's `rotate_certificate` signature with `csr_pem`. Reissuing over the existing key would need a public-key store this design does not have, and a short-lived certificate whose key never changes gives up most of what short-lived buys. Rotation is refused for any device that is not `active` and **replaces** the serial and fingerprint rather than appending.
    - **Finding 56, in pre-existing code:** `DEVICE_CERT_TTL_HOURS`'s documented lower bound of 1 is unreachable, because `DEVICE_CERT_RENEW_BEFORE_HOURS` is `ge=1` and must be strictly smaller. It fails closed (the configuration refuses to load), so it is recorded rather than changed.
    - **What has no caller yet:** `rotate_certificate` is called by the hub in leaf 8.4, and `verify_chain` by the handshake in the same leaf. Both are exercised only by their tests until then, which is why `TestRotation` carries six cases and `TestChainValidation` eight.
    - _Design: §3.1, §11.2, §13.1, §14.2, §14.3, §17.1 D-73, D-74; Deliverable: 1.1, 1.10; Criterion: 1_

  - [x] 8.3 Implement the agent `pair` command and credential persistence

    - Implement `session.Manager.Pair` and the `forgeops-agent pair --code --backend` CLI path: generate the key pair, send the CSR, persist `Credentials` through `session.Store`, and report the result in `agent doctor`.
    - Add `session.ErrUnpaired` distinct from Phase 0's `connection.ErrDisabled` so `doctor` can distinguish "no URL" from "no token".
    - Add tests for a successful exchange, a rejected code, a retry after success failing by design, and credential wipe.
    - **The CSR carries a self-describing placeholder subject, not an identity.** §3.1 builds it before the device id exists, and D-73 already settles that the CA discards the CSR's subject and issues `CN=<device_id>` itself. `pairingCSRCommonName` is therefore `forgeops-agent-pairing-request`, so anyone reading a captured CSR can see the field is not meant to be trusted.
    - **`Pair` refuses locally before it sends anything (`ErrAlreadyPaired`).** The code is single-use server-side, so a second `pair` would fail regardless — but only after this agent had replaced the working credential it holds. Refusing first is what stops a mistyped retry from unpairing a healthy agent, and it spends no attempt against the five-attempt cap.
    - **Finding 62, in pre-existing code:** `agent/internal/app/commands.go` held double-encoded `✓`/`✗` glyphs, so `agent doctor` printed `âœ“ Docker: …` on every line. Fixed here with the glyphs as `\u` escapes behind named constants; the pattern is in the journal's chapter 9.
    - _Design: §10.3, §10.10, Appendix A.1; Deliverable: 1.1; Criterion: 1_

  - [x] 8.4 Implement the backend WebSocket hub

    - Implement `AgentHub.serve` at `/api/v1/ws/agent`: verify the client certificate against the internal CA and the device row, verify the bearer device token, run the handshake, and check the Redis revocation set **per message**.
    - Allocate `seq` via a Redis Lua compare-and-set, correlate `command.execute` → `command.result` by id, fan `command.progress` out to SSE, and deliver commands across replicas through a Redis stream keyed by device id.
    - **The `seq` allocator already exists.** `governance/sequencing.py::RedisEnvelopeSequencer` landed with leaf 7.5, because an envelope cannot be minted without a `seq` and a nonce (§7.6). This leaf **consumes** it rather than reimplementing it; a second allocator would hand out duplicate sequence numbers, which the agent cannot distinguish from a replay.
    - Keep `send_command` callable only from `governance` (banned-api) and implement `broadcast_revocation` over pub/sub for prompt socket closure.
    - Add integration tests for handshake rejection paths, heartbeat timeout at 90 s, per-message revocation, and cross-replica delivery.
    - **Authentication lives in the route, not in the hub, and that is a boundary rather than a layout choice.** §2.4's table bans `src.auth.devices` outside `governance/`, so a hub that authenticated peers itself would be a hub that could reach an envelope key. The route reads the composed `DeviceService` off `app.state` and hands `serve` an already-authenticated `AuthenticatedDevice`; the hub names its needs through a `DeviceDirectory` Protocol it declares.
    - **D-79** decides that the hub keeps `UnavailableCommandSink`'s refusal rather than inheriting the placeholder's absence: `send_command` refuses with `device-not-connected` (409) for a device with no live session key, and fails closed when Redis cannot answer. Delivery is always through the Redis stream even when this replica owns the socket, because a local fast path would be a second delivery order.
    - **D-80** decides that certificate rotation rides `session.heartbeat` — an optional `csr` param, a `certificate` member in the result — rather than a tenth JSON-RPC method. §7.3's catalogue is closed so a reader can enumerate every message that exists.
    - **What this leaf deliberately does not do:** `approval.request` is refused as retryable, because the chokepoint has no agent-originated entry (its three transits are `submit`, `approve`, `revert`); the stale-bundle handshake reports the current digest but carries no bundle body, which is leaf 9.3's; and `RedisProgressSink` publishes to a channel whose SSE consumer does not exist yet.
    - **Finding 63, in pre-existing code:** the `go-vet` pre-commit hook used `language: script`, so on Windows it failed with `Executable /bin/sh not found` and `go vet` never actually ran locally. Latent until this leaf, because the hook is filtered to `^agent/.*\.go$`. Fixed to `bash scripts/go-vet-changed.sh` under `language: system`, the form three sibling hooks in the same file already use.
    - _Design: §3.1, §7.3, §11.10, §14.1; Deliverable: 1.1; Criterion: 1; Property: Q-16_

  - [x] 8.5 Implement the agent session manager, reconnect and journal drain

    - Implement `Manager.Serve`: mTLS dial through `identity.Provider`, `session.connect`, heartbeat every 30 s with a 90 s timeout, and reconnect with base 1 s, cap 60 s, jitter 0.5×, resetting the attempt counter only after a successful `session.connect`.
    - Implement the nine `phases.md` message types on Phase 0's fixed envelope, adding no tenth method; replay journalled intents as `approval.request`.
    - Gate the journal drain behind a successful connect plus the revocation and bundle-digest checks, in the §10.3 order; wipe on `ErrRevoked` and abort in-flight work with rollback from the manifest.
    - Add tests for backoff bounds, hot-loop avoidance when the handshake is rejected, heartbeat timeout, and drain ordering under a stale bundle.
    - _Design: §3.1, §7.3, §7.4, §10.3; Deliverable: 1.1; Criterion: 1; Property: Q-31_

  - [x] 8.6 Implement envelope verification and replay rejection

    - Implement `Verifier.Verify` performing schema, freshness, signature, ordering, nonce-uniqueness, policy-digest and operation-catalogue checks in exactly that order, returning the unexported-field `Verified` value that only this function can construct.
    - Verify the signature **before** any state mutation so an unauthenticated caller cannot advance `last_seq` and lock out the backend; tolerate ±60 s clock skew and report measured skew in `agent.status`.
    - Export `CanonicalBytes` solely for the cross-runtime fixture test; map every failure to a typed error whose code matches an Appendix C suffix.
    - Add tests for each rejection path proving no mutation and no counter advance occurs.
    - _Design: §7.6, §10.4, Appendix A.2, Appendix C.2; Deliverable: 1.1, 1.10; Property: Q-14, Q-15_

  - [x] 8.7 Implement the named-operation dispatch table

    - Implement `executor.Dispatcher` with the closed §7.7 catalogue, a single `handlerTable` as the only dispatch surface, per-operation timeouts and progress emission.
    - Add no `exec`, no `shell`, no `run_command` and no operation taking a command string; route mutating operations only through `mutate` with a `*envelope.Verified`.
    - Add tests asserting every enum member has a handler, that no handler is referenced outside the table, and that mutating operations refuse to run without an `approval_id`.
    - _Design: §7.7, §10.5, §17.1 D-47; Deliverable: 1.1, 1.6; Criterion: 5, 6_

  - [x] 8.8 Write property test Q-14 for canonicalisation and signature verification

    - Generate envelopes and prove `CanonicalBytes` is byte-identical in Go and Python for the same logical envelope, that verification accepts exactly the correctly signed envelope, and that every single-byte mutation is rejected.
    - Add the `mutations.toml` row removing the domain-separation prefix on one side only.
    - _Design: §7.6, §10.4, Appendix A.2, Appendix B Q-14; Deliverable: 1.1; Property: Q-14_

  - [x] 8.9 Write property test Q-15 for replay, reordering and expiry rejection

    - Generate envelope streams containing replays, reorderings and expiries; prove each is rejected and that no rejected envelope performs a mutation or advances any counter.
    - Add the `mutations.toml` row updating `last_seq` before the signature check.
    - _Design: §7.6, §10.4, Appendix B Q-15; Deliverable: 1.1; Property: Q-15_

  - [x] 8.10 Write property test Q-16 for immediate revocation

    - Generate revocation timings relative to an in-flight message stream; prove the first message after revocation is rejected and the socket closed, and that a replica which missed the pub/sub event still rejects on the next frame.
    - Add the `mutations.toml` row checking revocation once per connection instead of per message.
    - _Design: §3.1, §11.2, §11.10, Appendix B Q-16; Deliverable: 1.1; Property: Q-16_

  - [x] 8.11 Write property test Q-17 for pairing-code safety

    - Generate concurrent exchange attempts on one code; prove at most one succeeds, that expired, burned and unknown codes are indistinguishable in the response, that attempts beyond the cap always fail, and that the code value appears in no log, audit row or column.
    - Add the `mutations.toml` row making the consume script non-atomic (read then delete).
    - _Design: §11.2, §14.6, Appendix A.1, Appendix B Q-17; Deliverable: 1.1; Criterion: 1; Property: Q-17_

  - [x] 8.12 Write property test Q-31 for queue-and-revalidate
    - Generate offline/reconnect sequences and journal contents; prove no persisted record carries an envelope, `approval_id`, authority, device token, envelope key or secret value; prove `Drain` applies nothing and that every intent produces a new chokepoint transit with a fresh `approval_id`, digest, nonce and `seq`.
    - Prove a revoked device wipes rather than drains, a stale bundle leaves intents queued, and redelivery after an acknowledged batch is a no-op.
    - Add the `mutations.toml` row adding a `KindEnvelope` case to `Drain` that hands the stored envelope straight to `executor.Execute`.
    - _Design: §10.3, §17.1 D-41, Appendix B Q-31; Deliverable: 1.1; Criterion: 1; Property: Q-31_

- [ ] 9. Implement the policy engine and prove double evaluation agrees

  - [x] 9.1 Author the governance Rego bundle and add the `policy` CI job

    - Create `policies/agent/{governance,schedule,paths,approval}.rego`, each with an explicit `default allow := false` at its entry document so a deny is a **defined** `false` (the D-25 lesson applied to the new bundle).
    - Implement the three named policies as data-driven rules: blocked-weekday windows in the project timezone, protected path globs, and approval required for non-`allow` verdicts, `prod`, or any delete.
    - Add `policies/agent/*_test.rego` covering Friday inside and outside the window across timezones, `package.json` protection, and prod approval; add the `policy` CI job running `opa test policies/ -v` and `opa check --strict policies/`, plus `make policy-test`.
    - _Design: §8.3, §11.7, §13.4; Deliverable: 1.7; Criterion: 7_

  - [x] 9.2 Implement the backend governance policy client

    - Implement `OpaGovernancePolicy` querying the governance bundle over the existing shared `httpx` client, failing closed on transport error and raising `governance-policy-undefined` (503) on an undefined document rather than reading it as a deny.
    - Persist a `policy_evaluations` row per decision with `side="backend"`, the rule id and the human-readable reason for FR-37.
    - Add the `opa` service to `/health/ready`, register the `opa` capability, and add integration tests for allow, deny, require-approval, undefined document and transport failure.
    - _Design: §5.5, §11.7, §17.1 D-25 lineage; Deliverable: 1.7; Criterion: 7_

  - [x] 9.3 Implement bundle build, digest and publication

    - Implement `PolicyBundleService.build`/`.publish`/`.active_digest` producing a **canonical** gzip tar (sorted paths, fixed mtimes, fixed permissions) so identical inputs always yield an identical `sha256` digest.
    - Deliver the bundle inside a signed command envelope so it inherits envelope integrity and needs no second signature scheme; record the digest in `policy_context` on every subsequent envelope.
    - Add `POST /api/v1/policies/publish`, the `policy.bundle.publish` task, drift detection from `agent.status`, and tests proving digest stability across rebuilds.
    - _Design: §11.7, §7.6, §3.7; Deliverable: 1.7, 1.10; Criterion: 7; Property: Q-07_

  - [x] 9.4 Implement the agent's in-process Rego evaluator

    - Implement `agent/internal/policy` over `github.com/open-policy-agent/opa/rego` at the same OPA version as the server, exposing `Evaluate`, `BundleDigest` and an atomic `Load` that leaves the previous bundle in place on failure.
    - Never contact the network during evaluation; treat `ErrNoBundle` as **deny**, and refuse every mutation while the loaded digest differs from the envelope's `policy_context` digest.
    - Add tests for allow/deny/require-approval, offline evaluation, failed load preserving the prior bundle, and no-bundle denial.
    - _Design: §5.5, §10.6, §10.6.1, §17.1 D-30; Deliverable: 1.7, 1.10; Criterion: 7; Property: Q-06, Q-07_

  - [x] 9.5 Implement policy CRUD, templates and the dry-run endpoint

    - Implement `GET|POST /api/v1/policies`, `GET|PATCH|DELETE /api/v1/policies/{id}`, `POST /api/v1/policies/{id}/test` and `GET /api/v1/policies/templates` with Cerbos-scoped authorization.
    - Ship the scheduling and file-restriction templates as data, validate submitted Rego with `opa check` server-side before persisting, and surface violations with rule id and reason.
    - Add integration tests for create/update/disable, a rejected malformed policy, and a dry-run returning the same decision the live path would.
    - _Design: §11.7, §12.1; Deliverable: 1.7; Criterion: 7_

  - [ ] 9.6 Write property test Q-06 for backend/agent policy agreement

    - Generate governance inputs across operations, change-item sets, weekdays, timezones, verdicts and environments; with equal bundle digests, prove the OPA-server decision equals the agent's embedded decision for every input, driven from one shared fixture corpus.
    - Add the `mutations.toml` row inverting `approval.rego`'s prod clause in the agent's copy of the bundle only.
    - _Design: §11.7, §10.6, Appendix A.11, Appendix B Q-06; Deliverable: 1.7, 1.10; Criterion: 7; Property: Q-06_

  - [ ] 9.7 Write property test Q-07 for fail-closed digest disagreement
    - Generate digest pairs; prove that when the agent's bundle digest differs from the envelope's `policy_context` digest the agent denies **and** the chokepoint refuses to mint, and that no mutation occurs on either path.
    - Add the `mutations.toml` row making the agent's digest comparison a warning.
    - _Design: §10.6, §11.6, §11.7, Appendix B Q-07; Deliverable: 1.7, 1.10; Property: Q-07_

- [x] 10. Implement secret handling and the redaction chokepoint before the first prompt is assembled

  - [x] 10.1 Implement agent-side secret scanning and redaction

    - Implement `agent/internal/secretscan` over `github.com/zricethezav/gitleaks/v8 v8.30.1` plus project-configured patterns, returning findings with `kind`, `path`, `line`, `entropy` and a keyed fingerprint — **never** the matched value.
    - Make `Redact` the only constructor of `RedactedChunk`, replacing findings with `FORGEOPS_REDACTED:<kind>:<hash8>` where `hash8` is `HMAC-SHA256(project_pepper, value)` truncated, so the same secret is recognisable across chunks without being recoverable.
    - Route scanner and validator diagnostics through `secretscan.Redact` before they are logged or transmitted, so a validator echoing file contents cannot leak a secret ahead of the log filter. **Moved here from leaf 4.2**, which sits ten waves earlier and could not call a function this leaf creates; making it a chokepoint here also binds group 14's validators, which land later still.
    - Add tests over synthetic self-labelling credentials in fixture files asserting no value is ever returned, logged or transmitted, including credentials injected into validator diagnostic output.
    - _Design: §7.2, §7.11, §10.7, §10.9, §14.5, §16.1; Deliverable: 1.8; Criterion: 8; Property: Q-24_

  - [x] 10.2 Implement the backend redactor and the single prompt-assembly chokepoint

    - Implement `secrets/redaction.py` as the only constructor of `RedactedChunk`, `RedactedPrompt` and `RedactedInstruction`, redacting both by pattern and by the project's known secret values.
    - Implement `generation/context.py::assemble_prompt(*, system, chunks, instruction) -> RedactedPrompt` with **no `str` overload**, so forgetting to redact is a call that neither type-checks nor binds under task 1.2's conformance test.
    - Add tests proving the retriever cannot bypass it — the store holds redacted text only — and that no prompt reaching a `ModelEndpoint` contains a synthetic secret.
    - _Design: §7.11, §11.5, §11.8, Appendix A.7; Deliverable: 1.8; Criterion: 8; Property: Q-12_

  - [x] 10.3 Constrain the semantic cache to redacted prompts

    - Change `TieredSemanticCache.lookup`/`.store` to accept `RedactedPrompt` rather than `str`, leaving L1→L2→L3 precedence, the 0.95 threshold and the staleness/resilience behaviour unchanged.
    - Assert no cache key can be computed from raw text and that no cached completion is reachable from an unredacted prompt.
    - Add integration tests against real Redis Stack: identical prompt served from L1 with zero provider calls, near-duplicate served from L2 above threshold.
    - _Design: §7.11, §11.5, §17.1 D-44; Deliverable: 1.5, 1.8; Criterion: 14; Property: Q-13_

  - [x] 10.4 Implement the secret store, its API and the Infisical service

    - Implement the `SecretStore` Protocol with `InfisicalStore` over the shared `httpx` client (no new SDK) and `LocalSealedStore` using AES-256-GCM for `SECRET_BACKEND=local`.
    - Implement `GET|POST|PATCH|DELETE /api/v1/secrets` exposing metadata only; confine `get_value` to `secrets.injection` with a banned-api rule so no route can reveal a value.
    - Add the digest-pinned `infisical` service under the `vault` profile **with this task**, register the `infisical` capability, and add the `secrets` CI job exercising CRUD round trips against the container.
    - _Design: §8.3, §11.8, §13.3, §16.4; Deliverable: 1.8; Criterion: 8_

  - [x] 10.5 Implement deploy-time secret injection as a governed operation

    - Implement the `secrets.inject` named operation: it travels only in a signed envelope, materialises values into a process environment for the duration of one command, zeroes the buffers afterwards, and never writes to a file, a change-item or a log.
    - Record an audit row naming the injected **keys** only.
    - Add tests proving no value reaches disk, a change-set, a log line or an audit row, and that injection without an `approval_id` is refused.
    - _Design: §7.7, §11.8, §14.1; Deliverable: 1.8; Criterion: 8; Property: Q-28_

  - [x] 10.6 Write property test Q-12 for redaction before prompt assembly

    - Generate chunk sets containing synthetic secrets; prove `assemble_prompt` accepts only redacted types and that no prompt reaching a `ModelEndpoint` contains a secret value.
    - Add the `mutations.toml` row adding a `str` overload to `assemble_prompt`.
    - _Design: §7.11, §11.5, Appendix A.7, Appendix B Q-12; Deliverable: 1.8; Criterion: 8; Property: Q-12_

  - [x] 10.7 Write property test Q-13 for the cache-key clause

    - Generate prompts; prove every cache key is computed over a `RedactedPrompt`, that no cached completion is retrievable using unredacted text, and that no stored key material contains a synthetic secret.
    - Add the `mutations.toml` row widening `lookup`/`store` back to `str`.
    - _Design: §11.5, §17.1 D-44, Appendix B Q-13; Deliverable: 1.5, 1.8; Criterion: 14; Property: Q-13_

  - [x] 10.8 Write property test Q-24 for secret absence across logs, audit and problems

    - Inject synthetic secrets into file content, validator output and exception paths; prove no secret value appears in any log line, any `audit_events` row, or any RFC 9457 `detail`.
    - Add the `mutations.toml` row emptying the redaction pattern list — the exact experiment `REVIEW-PHASE-0.md` Pass 8 ran against P-09.
    - _Design: §7.2, §11.2, §11.9, §14.5, Appendix B Q-24; Deliverable: 1.8, 1.9; Criterion: 8, 9; Property: Q-24_

  - [x] 10.9 Write property test Q-28 for injection confinement
    - Generate `secrets.inject` envelopes; prove no injected value is written to any file, change-item, log or audit row, and that the audit row names only the keys.
    - Add the `mutations.toml` row logging the injected environment map at debug level.
    - _Design: §11.8, §10.5, Appendix B Q-28; Deliverable: 1.8; Criterion: 8; Property: Q-28_

- [x] 11. Implement the codebase analysis engine and the incremental index

  - [x] 11.1 Vendor, pin and verify the tree-sitter Wasm grammars

    - Add `agent/internal/scanner/grammars/` with the twelve §10.8.2 grammar `.wasm` artifacts and `grammars.lock.json` carrying `name`, `version`, `sha256`, `licence`, `source_url` and `purl` per entry; embed them with `go:embed`.
    - Where no prebuilt artifact exists, build it with a digest-pinned container running the pinned tree-sitter CLI and commit the resulting digest; extend `lock-integrity` to reproduce and compare.
    - Implement `scripts/sbom-merge.py` injecting one CycloneDX component per grammar into the SBOM, and assert in `supply` that the merged document still validates against CycloneDX 1.6 and contains every entry.
    - Rewrite the D-1 guard in `agent/internal/app/deps_test.go` and `scripts/check-go-module.sh` in the same change: assert no cgo-requiring module is in the graph and that every grammar digest matches the embedded bytes.
    - _Design: §4.7, §8.1, §8.6, §16.1, §16.5, §17.1 D-29; Deliverable: 1.3; Criterion: 2; Property: Q-25_

  - [x] 11.2 Implement AST parsing over wazero

    - Implement `agent/internal/scanner/ast` with `NewParser` instantiating the wazero runtime once, compiling each embedded grammar once and pooling modules per language.
    - Verify each grammar's SHA-256 **at load time**, not only in CI, so a tampered binary fails closed at first parse rather than producing plausible wrong ASTs; return `ErrUnsupportedLanguage` when no grammar is embedded.
    - Add tests parsing real fixture files in every embedded language, a corrupted-blob fail-closed test, and a benchmark recording parse throughput for Appendix D.
    - _Design: §8.2, §10.8.2, §17.1 D-29; Deliverable: 1.3; Criterion: 2; Property: Q-25_

  - [x] 11.3 Implement tiered language detection

    - Implement `Detect` with the four `phases.md` tiers in order — package manager/manifest, extension, shebang, then content heuristics bounded to the first 8 KiB — stopping at the first confident answer and recording which tier decided.
    - Break ties toward the manifest-derived project language so the readiness report can explain itself.
    - Add table tests covering each tier, ambiguous extensions, and files with no signal.
    - _Design: §10.8.1; Deliverable: 1.3; Criterion: 2_

  - [x] 11.4 Implement the filtered recursive scanner

    - Implement traversal honouring `.gitignore` and `.dockerignore`, skipping binaries and files above `SCAN_MAX_FILE_SIZE_BYTES`, and excluding `node_modules` and `.git`.
    - Emit a cold-start heuristic inventory (languages, manifests, existing config files, entry points) in the first round trip so the UI has real content immediately, then continue to full indexing.
    - Add tests for ignore-rule precedence, size and binary filters, symlink handling, and inventory shape.
    - _Design: §10.8, §11.4.4; Deliverable: 1.3; Criterion: 2_

  - [x] 11.5 Implement cAST semantic chunking

    - Implement bottom-up grouping (statements → functions → classes) with constraint-based splitting at the highest syntactic boundary and density optimisation, honouring ~512 target tokens, 128-token overlap and ~1024-token module summaries.
    - Carry `Kind`, `Symbol`, `ParentSymbol`, `Signature`, line span, token count and the file's import block on every chunk so a retrieved chunk is self-contained.
    - Add tests asserting chunk sizes stay within target ± overlap, that a declaration larger than target splits into sibling parts, and that metadata is populated for every kind.
    - _Design: §10.8.3, §0.2; Deliverable: 1.3; Criterion: 2_

  - [x] 11.6 Implement the dependency graph and the dirty closure

    - Implement `Graph` with forward and reverse adjacency keyed by relative path, keeping unresolved edges with `resolved=false` so a later scan can resolve them without re-parsing the importer.
    - Implement `Dirty` exactly as §10.8.4 states: changed, plus dependants of files whose **exported surface** changed, plus files whose own imports changed, plus dependants of deleted files; invalidate summaries for the dirty set plus direct importers.
    - Compute the closure as a fixed point over a visited set so a cyclic import graph terminates; keep `exports` coarse (names and signatures, not bodies).
    - Add tests for cycles, export-only changes, implementation-only changes and deletions.
    - _Design: §10.8.4, Appendix A.5; Deliverable: 1.3; Property: Q-10, Q-25_

  - [x] 11.7 Implement watch mode with debouncing and bounded fan-out

    - Wire the Phase 0 fsnotify `Watcher` into a 250 ms debouncer that coalesces per path and drops ignored paths, then a bounded parser fan-out at `min(GOMAXPROCS, 8)` and a fan-in aggregator batching upserts and deletions.
    - Handle rename as delete+create on both paths, walk newly created directories, and degrade to periodic polling on inotify exhaustion while reporting the degraded mode in `agent.status`.
    - Add tests proving coalescing never shrinks the dirty set and that polling mode still produces the same batches.
    - _Design: §3.3, §10.8.5; Deliverable: 1.3; Property: Q-11_

  - [x] 11.8 Implement the backend Codebase Index API

    - Implement `IndexService.replace_full` and `.patch_incremental` with idempotent upserts keyed on `(file_id, chunk_index)` and `(from_file_id, raw_specifier)`, and optimistic concurrency on `base_version` returning `index-version-conflict`.
    - Delete embeddings for vanished chunk pairs, nullify edges to deleted files, and invalidate module summaries for the dirty set plus direct importers.
    - Add the `index.full` and `index.incremental` tasks behind `TaskDispatcher`, SSE `PROGRESS` emission, and integration tests for retry-after-drop idempotency and version conflict.
    - _Design: §3.3, §11.4.1, §11.4.4; Deliverable: 1.3; Criterion: 2; Property: Q-10_

  - [x] 11.9 Implement embedding orchestration for both backends

    - Implement the `embed.batch` task calling Voyage Code 3 over `httpx` for `EMBEDDING_BACKEND=voyage` and the local BGE-M3 endpoint for `bge_m3`, writing to `embeddings` (1536-d) or `embeddings_local` (1024-d) accordingly and never mixing.
    - Record `model_id` on every row (D-2) and set `hnsw.ef_search` per query transaction through the Phase 0 helper, never on the index.
    - Lock `projects.settings.embedding_backend` once embeddings exist, returning `project-embedding-backend-locked`; add batching, 429 backoff and budget checks before a run starts.
    - _Design: §6.3, §11.3, §11.4.2, §17.1 D-48; Deliverable: 1.3; Criterion: 12_

  - [x] 11.10 Implement the Redis BM25 sparse index

    - Create `idx:code:<project_id>` with the §11.4.3 schema and weighted `path`/`symbol`/`text` fields, searching with the BM25 scorer explicitly rather than the default.
    - Treat the index as derived: add the `index.reindex_sparse` task rebuilding it from `file_contents`, keep it out of `/health/ready`, and degrade retrieval to dense-only with a recorded `retrieval_degraded` flag when it is missing.
    - Add integration tests against real Redis Stack for index creation, BM25 ordering, rebuild, and the degraded path.
    - _Design: §11.4.3, §17.1 D-49; Deliverable: 1.3; Property: Q-29_

  - [x] 11.11 Write property test Q-10 for incremental-equals-full rescan

    - Generate edit sequences (create, modify, delete, rename, import changes, cycles) over a synthetic project and prove the incrementally maintained index equals `FullRescan(final_tree)` — same chunks, same edges, same summary invalidation, no orphans.
    - Add the `mutations.toml` row dropping the `Dependants(deleted)` term from `DirtySet`.
    - _Design: §10.8.4, §11.4.4, Appendix A.5, Appendix B Q-10; Deliverable: 1.3; Criterion: 2; Property: Q-10_

  - [x] 11.12 Write property test Q-11 for coalescing safety

    - Generate raw watcher event sequences and prove the debounced stream produces the same dirty set as the un-coalesced sequence, so an optimisation cannot lose a change.
    - Add the `mutations.toml` row coalescing a delete followed by a create into a no-op.
    - _Design: §10.8.5, Appendix B Q-11; Deliverable: 1.3; Property: Q-11_

  - [x] 11.13 Write property test Q-25 for grammar integrity and closure termination
    - Prove a grammar digest mismatch refuses to load and fails the scan closed with a typed error, and that closure computation terminates for every generated cyclic dependency graph.
    - Add the `mutations.toml` row skipping digest verification when the blob loads successfully.
    - _Design: §10.8.2, §10.8.4, §16.5, Appendix B Q-25; Deliverable: 1.3; Criterion: 2; Property: Q-25_

- [x] 12. Implement the multi-project workspace and deployment-readiness analysis

  - [x] 12.1 Implement project CRUD, settings, tags and the activity feed

    - Implement `POST|GET /api/v1/projects`, `GET|PATCH|DELETE /api/v1/projects/{id}`, `PUT /settings`, `POST|DELETE /tags/{tag}` and `GET /activity`, all behind `require_principal` and Cerbos.
    - Implement `ProjectSettings` as a strict Pydantic model with `extra="forbid"`, and make the activity feed a **projection over `audit_events`** filtered to the project rather than a second log.
    - Add search, tag filtering, favourites and cursor pagination; add integration tests for authorization, settings validation and activity ordering.
    - _Design: §11.3, §6.3; Deliverable: 1.2; Criterion: 1_

  - [x] 12.2 Implement GitHub import and the App installation token source

    - Implement local-path registration and GitHub import, driving `project.register` on the agent to start the watcher.
    - Add `AppInstallationTokenSource` using `bradleyfalzon/ghinstallation/v2` behind the unchanged Phase 0 `TokenSource` interface, selected by configuration, with `EnvTokenSource` retained for development.
    - Add tests against a recorded-response server for installation-token minting, expiry refresh, and that no call site changed — discharging D-5's promise.
    - _Design: §1.4, §11.3, §16.1, §17.1 D-38; Deliverable: 1.2; Criterion: 1_

  - [x] 12.3 Implement the deterministic readiness scoring engine

    - Implement `ReadinessEngine` with the six `phases.md` categories, weights loaded from `config/readiness-weights.yaml` summing to 100, integer-only arithmetic, and `applies_to` so an inapplicable check is excluded from its category denominator rather than scored zero.
    - Implement roughly thirty checks covering the `phases.md` and FR-20 examples: Dockerfile existence/multi-stage/non-root/pinned base/`HEALTHCHECK`, `.dockerignore`, CI existence/tests/SHA-pinned actions, K8s manifests/limits/probes/no-`latest`, `.env.example`, scan-clean, and IaC presence/state backend.
    - Involve no LLM anywhere in the score; add unit tests for weight redistribution, exclusion behaviour and integer stability between a partial and a full inventory.
    - _Design: §11.4.5, Appendix A.6; Deliverable: 1.4; Criterion: 2; Property: Q-18_

  - [x] 12.4 Implement the readiness API and the plain-language report

    - Implement `POST /api/v1/projects/{id}/readiness` persisting `analysis_reports` with `score`, `categories`, `inventory_hash` and `report_version`, and supporting a `partial: true` result from the cold-start inventory that is recomputed when full indexing completes.
    - Add `config/report_templates.yaml` keyed by check id with `title`, `why_it_matters`, `how_to_fix` and `severity`; no LLM writes the report, for the same reason none computes the score.
    - Add integration tests proving the same inventory always yields the same report bytes and that every check id has a template entry.
    - _Design: §11.4.4, §11.4.5; Deliverable: 1.4; Criterion: 2; Property: Q-18_

  - [x] 12.5 Write property test Q-18 for readiness determinism and monotonicity
    - Generate inventories and prove the score is deterministic, independent of file iteration order, and monotone — making an applicable failing check pass never lowers it — and that `inventory_hash` identifies the producing inventory.
    - Add the `mutations.toml` row replacing the integer division in `Score`'s per-category term with float division.
    - _Design: §11.4.5, Appendix A.6, Appendix B Q-18; Deliverable: 1.4; Criterion: 2; Property: Q-18_

- [x] 13. Implement the AI generation pipeline on top of proven routing and redaction

  - [x] 13.1 Implement hybrid retrieval with RRF fusion

    - Implement `HybridRetriever.retrieve`: dense pgvector HNSW cosine with a per-transaction `ef_search`, sparse Redis BM25, and Reciprocal Rank Fusion with the committed constant `k = 60` so no score normalisation between incomparable scales is needed.
    - Over-retrieve `RETRIEVAL_OVERFETCH_FACTOR × k` (default 3×) per Research §C10 and return `RedactedChunk` only, reading exactly one embedding table per project.
    - Add integration tests for fusion ordering, dense-only degradation, sparse-only degradation, and the `retrieval_degraded` flag.
    - _Design: §11.4.3, §11.5.2, Appendix A.10; Deliverable: 1.5; Criterion: 3; Property: Q-29_

  - [x] 13.2 Implement the reranker with explicit degradation

    - Implement `VoyageReranker` calling `voyage-rerank-2` over the shared `httpx` client with a BYO key, taking top-`k` after the 3× over-retrieve.
    - On timeout or unavailability, fall back to the fused order and record `retrieval_degraded=True` on the run; never raise, because retrieval is a read path.
    - Add tests for reranked ordering against a local fixture endpoint, timeout degradation, and per-project budget refusal before the call.
    - _Design: §11.5.2, §17.2 OQ-22; Deliverable: 1.5; Criterion: 3; Property: Q-29_

  - [x] 13.3 Implement the structured artifact schemas and renderers

    - Implement `DockerfileSpec`, `ComposeSpec`, `K8sManifestSet`, `GitHubActionsSpec`, `HelmChartSpec`, `TofuModuleSpec`, `EnvExampleSpec`, `DocsSpec` and `ArtifactSet` under Pydantic v2 `strict=True, extra="forbid", frozen=True`, with non-root user as a **type** requirement rather than a lint.
    - Render files from the validated structure in ForgeOps code, never from free-form model text; use JSON-Schema-constrained output where the endpoint supports it, tool-calling where it does not, and a single schema-repair attempt that counts against the 3-iteration budget.
    - Add tests for schema rejection of a root user, a missing entrypoint, an unpinned base image where required, and deterministic rendering from a fixed spec.
    - _Design: §11.5.3; Deliverable: 1.5; Criterion: 3, 4_

  - [x] 13.4 Wire tier selection and prove every tier is reachable

    - Map artifact work to tiers per §11.5.4: `high_coding` for multi-file architecture, `medium` for single artifacts, `low_logs` for prose, `medium_value` for the judge, `self_hosted` for air-gapped projects.
    - Add the two D-42 endpoint descriptors pointing at the vendor OpenAI-compatible surfaces, keeping the native descriptors present and marked unavailable as honest data.
    - Add a wiring test asserting `GET /api/v1/ai/tiers` reports **no tier whose primary is unavailable**, and cascade tests against local fixture endpoints with no vendor key or network.
    - _Design: §1.5, §11.5.4, §13.2, §17.1 D-42; Deliverable: 1.5; Criterion: 3_

  - [x] 13.5 Implement the blocking gate and the advisory rubric with no path between them

    - Implement `generation/gate.py::decide(findings) -> GateDecision` accepting **only** deterministic findings, so no rubric value can reach it — the separation is structural, not procedural.
    - Implement `generation/judge/rubric.py` with integer 0–5 anchors, temperature 0, a versioned prompt, and the judging model id recorded on `generation_runs.rubric`; add a CI stability probe that judges one fixture twice and **reports** variance without gating.
    - Add tests proving an all-zero rubric and an all-five rubric produce the identical `GateDecision`.
    - _Design: §11.5.5; Deliverable: 1.5; Criterion: 3, 4; Property: Q-09_

  - [x] 13.6 Implement the structurally bounded feedback loop

    - Implement `LoopState` as a frozen dataclass and `FeedbackLoop._next` as the only producer of a new state, always decrementing `attempts_remaining` and returning the closed union `Continue | Accepted | FallbackToTemplate`, with `Continue` unreachable at the bound.
    - Feed **all** blocking findings from a pass back into the next attempt, not the first, so a file with four problems does not exhaust the budget one fix at a time.
    - Add no `while True`, no resettable counter and no configuration that raises the bound; add tests asserting at most three model calls for every failure sequence and the `<= 1` boundary behaviour.
    - _Design: §3.8, §7.1, §11.5.6; Deliverable: 1.5; Criterion: 3, 4; Property: Q-08_

  - [x] 13.7 Insert the DryRun stage into the existing validation pipeline

    - Implement `DryRunStage` delegating to the agent hub and insert it **before** `SemanticStage`, leaving `ValidationPipeline.run` and the `Stage` Protocol untouched — the stage list is data.
    - Run every validator rather than short-circuiting on the first failure, and return a single fatal blocking `dryrun_unavailable` finding when no agent is connected, so an un-dry-run change-set can never be presented as validated.
    - Add integration tests for the stage order, the no-agent path, and aggregation of findings across validators.
    - _Design: §11.12, §1.4; Deliverable: 1.5; Criterion: 4_

  - [x] 13.8 Implement the generation service, SSE stream and run records

    - Implement `GenerationService.run` orchestrating retrieve → assemble → generate → validate (≤3) → judge → `chokepoint.submit`, and `POST /api/v1/generation/runs` returning `EventSourceResponse`.
    - Emit exactly the six `core/sse.py` event types with monotonic `PROGRESS` and exactly one terminal event, including on client disconnect; the run continues behind `TaskDispatcher` when the client drops.
    - Persist `generation_runs` with iterations used, `served_from`, tier, endpoint, rubric, retrieval provenance and token counts; the service must never write a file or contact the hub directly.
    - _Design: §4.5, §7.5, §11.5.1, §11.11; Deliverable: 1.5; Criterion: 3, 13; Property: Q-26_

  - [x] 13.9 Fill the terminal cascade slot with the template fallback

    - Implement `TemplateLibraryFallback.render` and install it at the `TerminalFallback` slot **without modifying the router**, marking results `served_from="template"` with the reason recorded.
    - Return `generation-unavailable` when no template exists for the detected language rather than substituting a wrong-language template.
    - Add tests for the exhausted-router path, the iteration-bound path, and the no-template path.
    - _Design: §11.5.7, §17.1 D-43; Deliverable: 1.5; Criterion: 3, 4; Property: Q-21_

  - [x] 13.10 Write property test Q-08 for iteration-bound termination

    - Generate sequences of validation outcomes and prove the loop performs at most three model calls and terminates in `Accepted`, `TemplateFallback` or `Unavailable`, with `attempts_remaining` strictly decreasing on every `Continue`.
    - Add the `mutations.toml` row making `_next` return `Continue` without decrementing.
    - _Design: §3.8, §11.5.6, Appendix A.4, Appendix B Q-08; Deliverable: 1.5; Criterion: 3; Property: Q-08_

  - [x] 13.11 Write property test Q-09 for rubric non-interference

    - Generate rubric values including all-zero and all-five and prove `GateDecision` is identical for every one, and that no rubric field appears among `decide`'s inputs.
    - Add the `mutations.toml` row adding a `rubric` parameter to `decide` and letting a low score block.
    - _Design: §11.5.5, Appendix B Q-09; Deliverable: 1.5; Criterion: 4; Property: Q-09_

  - [x] 13.12 Write property test Q-26 for SSE stream well-formedness

    - Generate generation and analysis streams and prove only the six `SSEEventType` names are emitted, `PROGRESS.percent` is non-decreasing, and exactly one of `COMPLETE`/`ERROR` terminates every stream including on disconnect.
    - Add the `mutations.toml` row emitting a second `COMPLETE` after an `ERROR`; add the `fast-check` counterpart asserting the client drops an unknown event name.
    - _Design: §4.5, §11.11, §12.4, Appendix B Q-26; Deliverable: 1.5; Criterion: 13; Property: Q-26_

  - [x] 13.13 Write property test Q-29 for retrieval degradation
    - Generate retrieval requests and prove that with the reranker unavailable the result is the RRF-fused order with `retrieval_degraded` recorded, that with the sparse index absent the result is dense-only with the same flag, and that neither raises.
    - Add the `mutations.toml` row letting a reranker timeout propagate as a 500.
    - _Design: §11.5.2, §11.4.3, Appendix B Q-29; Deliverable: 1.5; Criterion: 3; Property: Q-29_

- [x] 14. Implement the agent validators and the Kubernetes CI harness

  - [x] 14.1 Implement the Compose validator in process

    - Implement the `docker compose config` equivalent over `github.com/compose-spec/compose-go/v2`, loading and validating generated compose files without requiring the Docker CLI.
    - Return findings with the §10.7 shape, redacted before they leave the process, and expose `Available` reporting truthfully.
    - Add tests over the repository's own `docker-compose.yml` plus valid and invalid generated fixtures.
    - _Design: §10.7; Deliverable: 1.5; Criterion: 4_

  - [x] 14.2 Implement YAML and JSON Schema validation in process

    - Implement YAML syntax validation over `sigs.k8s.io/yaml` and schema validation over `santhosh-tekuri/jsonschema/v6` against bundled Kubernetes and GitHub Actions schemas.
    - Bundle the schemas as pinned assets with their own digests so validation does not depend on network access.
    - Add tests for malformed YAML, schema violations with correct path/line, and a clean pass.
    - _Design: §10.7; Deliverable: 1.5; Criterion: 4_

  - [x] 14.3 Implement Kubernetes server-side dry-run over client-go

    - Implement server-side apply with `DryRun: [All]` using the existing `k8s.io/client-go` dependency, exercising the same admission, defaulting and pruning path `kubectl --dry-run=server` uses.
    - Report `Available=false` with a reason when no cluster is reachable, and surface the distinction between shape validity and cluster acceptance in the finding codes.
    - Add `//go:build integration` tests run under `KUBECONFIG` from the `k8s` job, covering admission rejection, defaulting, pruning and an unavailable `apiVersion`.
    - _Design: §10.7, §8.3.1, §17.1 D-28; Deliverable: 1.5; Criterion: 4_

  - [x] 14.4 Implement Helm lint and template over the Helm SDK

    - Implement `helm lint` and `helm template` using `helm.sh/helm/v3`, and `--validate` behaviour against a cluster when one is available.
    - Report the embedded SDK version through `agent doctor`, and record the binary-size impact for OQ-27.
    - Add tests over the template library's charts for lint findings, rendered output stability, and the unavailable-cluster path.
    - _Design: §10.7, §8.2, §17.2 OQ-27; Deliverable: 1.5; Criterion: 4_

  - [x] 14.5 Implement the Trivy config validator and the availability policy

    - Invoke `trivy config` as a subprocess when the binary is present, parsing findings into the common shape.
    - Implement the §10.7 unavailable-validator rule: a `Fatal, Blocking` `validator_unavailable` finding at `infrastructure` blast radius, and a non-blocking warning otherwise.
    - Add `trivy` to the `agent-dev` devtools image with a pinned version, register the `trivy` capability, and add tests for both availability branches.
    - _Design: §10.7, §13.3, §16.4, §17.2 OQ-25; Deliverable: 1.5; Criterion: 4_

  - [x] 14.6 Adapt the Phase 0 OpenTofu runner as a validator

    - Wrap the existing `iac.Runner` `Validate` and `Plan` behind the `Validator` interface, feeding plan JSON to the Plan Analyzer as the semantic input.
    - Add **no `apply`** and no new subprocess surface; treat `-detailed-exitcode` 2 as success-with-changes as Phase 0 established.
    - Add integration tests over the pinned null-provider fixture for validate, plan-with-changes and plan-with-error.
    - _Design: §1.4, §10.7; Deliverable: 1.5; Criterion: 4_

  - [x] 14.7 Implement devtools discovery and extend `agent doctor`

    - Implement `internal/devtools` discovering and version-reporting optional external tools without installing anything.
    - Add `doctor` rows for session state and credential-store backend, certificate expiry, policy bundle digest and staleness, embedded grammar inventory with digests, embedded validator versions, external tool availability, clock skew, watcher mode and journal backlog.
    - Add tests asserting every degraded mode is reported rather than silent.
    - _Design: §10.10, §10.3, §10.7; Deliverable: 1.1, 1.5_

  - [x] 14.8 Implement the SPIFFE workload identity provider

    - Implement `identity.SpiffeWorkload` fetching an X.509-SVID over the SPIFFE Workload API with `github.com/spiffe/go-spiffe/v2`, selected by `AGENT_IDENTITY_PROVIDER=spiffe_workload`.
    - Use JWT-SVID only for crossing an L7 proxy, never as the primary credential; persist no credential.
    - Add `//go:build integration` tests exercised from the `k8s` job against a real SPIRE deployment; state in code comments that the laptop path is pairing-derived and is not platform attestation.
    - _Design: §10.2, §14.3, §16.1, §17.1 D-36; Deliverable: 1.10_

  - [x] 14.9 Add the kind-based `k8s` CI job with the SPIRE attestation harness
    - Add the `k8s` job using `kind v0.27.x` via a SHA-pinned action with a digest-pinned `kindest/node` image, running server-side dry-run validation, `helm template --validate`, and the backend tests marked `kubernetes`.
    - Add `scripts/k8s/spire-attest-test.sh` deploying pinned SPIRE manifests and performing a real attestation plus mTLS handshake on namespace + service-account + image-digest.
    - Pipe results through `check-no-skips.py`, gate the job on the `agent`/`backend` change filters plus `main`, and add `make k8s-up` / `make k8s-down`.
    - _Design: §8.3.1, §13.4, §14.3, §16.4, §17.1 D-28; Deliverable: 1.5, 1.10; Criterion: 4, 10_

- [x] 15. Implement the Safe Default Template Library and prove every template is verified

  - [x] 15.1 Implement the template loader and manifest contract

    - Create `backend/src/generation/templates/` with a per-language `manifest.yaml` schema and a substitution-only renderer (`string.Template`-level), deliberately not an expression-evaluating engine inside a security-relevant fallback.
    - Implement `TemplateLibrary.load` validating every manifest at import time and failing startup on a malformed or incomplete language set.
    - Add tests for manifest validation, missing artifact classes, and deterministic rendering from a fixed parameter set.
    - _Design: §11.5.7; Deliverable: 1.5; Criterion: 4; Property: Q-21_

  - [x] 15.2 Add the Node.js and Python template sets

    - Add Dockerfile, K8s Deployment + Service + Ingress, GitHub Actions CI, Helm chart and OpenTofu module for each, with pinned base images, non-root users, resource limits and probes.
    - Add a fixture project per language under `backend/tests/fixtures/templates/` for rendering and validation.
    - Add rendering tests asserting the parameter surface is complete and no placeholder survives.
    - _Design: §11.5.7; Deliverable: 1.5; Criterion: 4; Property: Q-21_

  - [x] 15.3 Add the Go and Rust template sets

    - Add the same five artifact classes for each language with static-binary-appropriate multi-stage builds and pinned toolchain images.
    - Add fixture projects and rendering tests as in task 15.2.
    - _Design: §11.5.7; Deliverable: 1.5; Criterion: 4; Property: Q-21_

  - [x] 15.4 Add the Java/Kotlin and Ruby template sets

    - Add the same five artifact classes for each, with JVM memory flags appropriate to container limits and Ruby bundler caching in the build stage.
    - Add fixture projects and rendering tests as in task 15.2.
    - _Design: §11.5.7; Deliverable: 1.5; Criterion: 4; Property: Q-21_

  - [x] 15.5 Add the PHP and .NET template sets

    - Add the same five artifact classes for each, completing the eight languages `phases.md` §1.5 names.
    - Add fixture projects and rendering tests as in task 15.2.
    - _Design: §11.5.7; Deliverable: 1.5; Criterion: 4; Property: Q-21_

  - [x] 15.6 Add the `templates` CI job that runs the real validation pipeline

    - Add the `templates` job rendering all 8 × 5 artifact sets against their fixture projects and driving them through the **same** `ValidationPipeline` the AI output traverses — `SyntaxStage`, `SchemaStage`, `DryRunStage` (including server-side dry-run from the `k8s` job) and `SemanticStage`.
    - Fail the build on any blocking finding; "verified" means this job is green and nothing else.
    - Add `make templates-verify` and pipe the job through `check-no-skips.py`.
    - _Design: §8.3, §11.5.7, §13.4; Deliverable: 1.5; Criterion: 4; Property: Q-21_

  - [x] 15.7 Write property test Q-21 for template-library validity
    - Parametrise over the 8 × 5 matrix and prove every rendered artifact set produces **zero blocking findings** from the same pipeline the AI output traverses.
    - Add the `mutations.toml` row corrupting one template's Dockerfile `FROM` line.
    - _Design: §11.5.7, Appendix B Q-21; Deliverable: 1.5; Criterion: 4; Property: Q-21_

- [x] 16. Implement the Change Approval Center API surface

  - [x] 16.1 Implement change-set retrieval, approval and the state machine

    - Implement `GET /api/v1/change-sets`, `GET /{id}`, `GET /{id}/diff`, `POST /{id}/approve`, `POST /{id}/reject` and `POST /{id}/apply`, all routed through `governance` and authorized by Cerbos.
    - Enforce the §3.6 state machine with terminal absorption and optimistic concurrency on `version`, returning `change-set-conflict` to the loser of two concurrent approvals and `approval-expired` past `APPROVAL_TTL_SECONDS`.
    - Add integration tests over `production_app` for every legal transition, every rejected illegal transition, and concurrent approval.
    - _Design: §3.6, §11.6, §12.1; Deliverable: 1.6; Criterion: 5; Property: Q-22_

  - [x] 16.2 Implement rollback handles and the revert path

    - Persist the agent's `BackupManifest` into `rollback_handles` on a successful apply, expose `POST /api/v1/change-sets/{id}/revert`, and mark the handle consumed exactly once.
    - Run revert through the full chokepoint with its own authority mint, and return `revert-unavailable` for a consumed or expired handle.
    - Add integration tests for a successful revert, a double revert, and a revert after handle expiry.
    - _Design: §3.6, §10.5, §11.6; Deliverable: 1.6; Criterion: 6; Property: Q-02, Q-22_

  - [x] 16.3 Write property test Q-22 for change-set state legality

    - Generate transition sequences and prove only §3.6 edges are accepted, terminal states are absorbing, `applied` leaves only via `reverted`, and two concurrent approvals yield exactly one winner and one 409.
    - Add the `mutations.toml` row removing the optimistic-concurrency `version` predicate.
    - _Design: §3.6, §6.5, §11.6, Appendix B Q-22; Deliverable: 1.6; Criterion: 5; Property: Q-22_

  - [x] 16.4 Write property test Q-23 for diff fidelity
    - Generate `(old, new)` content pairs and prove applying the compiled `change_items` reproduces `new_content` exactly, that the unified diff applied to old yields new, and that the frontend renders the hunk count the backend computed.
    - Add the `mutations.toml` row compiling change items with `old_content` from the wrong revision; add the `fast-check` counterpart on the client side.
    - _Design: §11.6, §12.2, Appendix B Q-23; Deliverable: 1.6; Criterion: 5; Property: Q-23_

- [x] 17. Implement the frontend feature surfaces

  - [x] 17.1 Add session handling, the login route and `proxy.ts`

    - Add `frontend/proxy.ts` (Next.js 16's `middleware.ts` successor) redirecting unauthenticated navigation to `/login` and refreshing an expiring session cookie; keep the existing "no `middleware.ts`" package-policy assertion passing.
    - Add `app/(shell)/login/page.tsx` initiating the OIDC redirect and handling the callback return path.
    - Add tests for redirect behaviour, return-path preservation and silent refresh.
    - _Design: §12.1, §12.5; Deliverable: 1.11; Criterion: 1_

  - [x] 17.2 Implement the typed SSE reader over fetch

    - Implement `lib/api/sse.ts` reading the stream with `fetch` + `ReadableStream` so an `Authorization` header can be sent, adding **no** SSE dependency and putting no token in a query string.
    - Accept only the six event names, dropping an unknown name with a console warning so a seventh type fails loudly in development.
    - Add unit tests for chunk-boundary splitting, abort handling, and unknown-event rejection.
    - _Design: §4.5, §12.4; Deliverable: 1.5; Criterion: 13; Property: Q-26_

  - [x] 17.3 Implement the project list and detail surfaces

    - Add `app/(shell)/projects/page.tsx` with search, tag filtering and favourites, and `[projectId]/page.tsx` with the recent-activity feed, both owned by TanStack Query.
    - Keep Zustand to client-ephemeral UI only and selection in `searchParams`, per the inherited state-ownership rule.
    - Add component tests for search, tag filtering, favourite toggling and activity ordering.
    - _Design: §12.1, §12.3; Deliverable: 1.2; Criterion: 1_

  - [x] 17.4 Implement the readiness surface with an accessible radar chart

    - Add `readiness/page.tsx` rendering the score, the category breakdown with expandable items and the recommendations list.
    - Import ECharts through `echarts/core` registering only `RadarChart` + `CanvasRenderer`; mark the canvas `aria-hidden` and pair it with a visually-hidden `<table>` of category scores as the accessible source of truth.
    - Add tests asserting the accessible table matches the chart data and that colour is never the only signal.
    - _Design: §12.2, §12.5, §16.3; Deliverable: 1.4; Criterion: 2_

  - [x] 17.5 Implement the generation surface with progressive UX

    - Add `generate/page.tsx` streaming tokens through the task 17.2 reader, accumulating deltas in a `useRef` flushed on an animation frame, and invalidating TanStack Query on every non-token event.
    - Show partial results as they arrive (cold-start inventory, then full index, then artifacts) and recover state from REST after a dropped stream.
    - Add tests for token accumulation, phase announcements via `role="status"`, and reconnection recovery.
    - _Design: §7.5, §12.3, §12.5; Deliverable: 1.5; Criterion: 3, 13_

  - [x] 17.6 Implement the Change Approval Center surface

    - Add `changes/page.tsx` (history timeline) and `changes/[changeSetId]/page.tsx` with `react-diff-viewer-continued` in both side-by-side and unified modes, an approve/reject control with a comment field, and a confirmation step before apply.
    - Render the diff in a `<table>` with row headers, an accessible change summary, per-hunk screen-reader descriptions, `+`/`−` glyphs, and a real radio group for the view toggle.
    - Add tests for both view modes, hunk-count parity with the backend, keyboard reachability of approve/reject, and the live-region outcome announcement.
    - _Design: §12.1, §12.2, §12.5, §16.3; Deliverable: 1.6; Criterion: 5; Property: Q-23_

  - [x] 17.7 Implement the policy list, editor and violation display

    - Add `policies/page.tsx` with a list, a CodeMirror 6 plain-text Rego editor (three packages, no language mode), and server-side `opa check` validation surfaced as inline problems.
    - Render violations with the rule id and the policy's own reason, satisfying FR-37's explanation requirement.
    - Add tests for editor mount, validation error placement and violation rendering.
    - _Design: §12.1, §12.2, §16.3; Deliverable: 1.7; Criterion: 7_

  - [x] 17.8 Implement the secret vault surface

    - Add `secrets/page.tsx` listing key, environment, rotation date and last-updated — **metadata only**, with no reveal affordance anywhere in the UI.
    - Support add, edit and delete with React Hook Form + Zod, mapping server field errors through the existing `ApiProblemError.fieldErrors` path.
    - Add tests asserting no code path requests or renders a secret value.
    - _Design: §11.8, §12.1; Deliverable: 1.8; Criterion: 8; Property: Q-20_

  - [x] 17.9 Implement the audit log viewer

    - Add `audit/page.tsx` using `@tanstack/react-table` with sorting, column filtering and cursor pagination over `GET /api/v1/audit`.
    - Show who, what, when, why and the before/after summary, and expose the chain-verification result for admins.
    - Add tests for pagination, filtering and the redaction expectation that no cell can contain a secret pattern.
    - _Design: §11.9, §12.1, §16.3; Deliverable: 1.9; Criterion: 9; Property: Q-24_

  - [x] 17.10 Implement the agent pairing and device surface

    - Add `agents/page.tsx` minting a pairing code with a visible 5-minute countdown, showing the copy-paste CLI command, and listing devices with status, last seen, certificate expiry and policy-bundle staleness.
    - Add a revoke control with confirmation, and surface journal backlog when a device is offline.
    - Add tests for countdown expiry, code single-use messaging, and the revoked-state rendering.
    - _Design: §3.1, §3.7, §12.1; Deliverable: 1.1; Criterion: 1_

  - [x] 17.11 Pin the new frontend dependencies and gate frontend coverage
    - Add `echarts`, `react-diff-viewer-continued`, `@tanstack/react-table` and the three CodeMirror 6 packages at exact versions with a regenerated `pnpm-lock.yaml`.
    - Extend `frontend/__tests__/package-policy.test.ts` to assert the four additions are exact-pinned and that no unsanctioned library (xterm.js, React Flow, Monaco, a date library, an SSE library) has appeared.
    - Configure `vitest --coverage` with the v8 provider at 70/70/70 thresholds and wire it into the `frontend` job.
    - _Design: §12.2, §16.3, §17.1 D-31; Deliverable: 1.11; Criterion: 11_

- [x] 18. Build the end-to-end journey and the `e2e` CI job

  - [x] 18.1 Add the e2e stack overlay, the fixture OIDC issuer and the agent container

    - Add a Compose overlay starting the built `backend` and `frontend` images plus a small signed-JWT fixture issuer, keeping the real Authentik flow in the `auth` job so the journey does not pay its cold start.
    - Add an `agent-e2e` service running the **real** `forgeops-agent` binary with a fixture Node.js project mounted, and a harness step that pairs it with a code minted through the API.
    - Generate the fixture issuer's signing key per run; commit no key material and register the `agent_binary` capability.
    - _Design: §8.3.2, §12.6, §17.2 OQ-28; Deliverable: 1.11; Criterion: 10_

  - [x] 18.2 Implement the criterion-10 journey specification

    - Implement `frontend/e2e/journey.spec.ts` executing the thirteen §12.6 steps: log in, create the project, mint and consume a pairing code, assert the device is active and heartbeating, wait for the readiness score and radar chart, generate, observe the SSE sequence, inspect the diff in both modes, approve with a comment, assert the applied state.
    - Assert **on the filesystem through the agent container** that the Dockerfile and the three K8s manifests exist with the expected content hashes, that a backup exists for every overwritten pre-existing file, that the audit viewer lists the full transit, and that a revert restores every pre-image byte-for-byte.
    - Keep the spec free of implementation: it may only drive behaviour built in earlier tasks.
    - _Design: §12.6; Deliverable: 1.1, 1.2, 1.4, 1.5, 1.6, 1.9; Criterion: 10_

  - [x] 18.3 Add the `e2e` CI job

    - Add the `e2e` job building both images, starting the overlay, building the agent binary, running Playwright, and uploading traces plus agent logs on failure — an e2e failure with no artifacts is a rerun, not a diagnosis.
    - Gate it on the change filters plus `main`, pipe it through `check-no-skips.py`, and extend `make e2e` to run the same journey locally.
    - This closes inherited debt D3: `ci.yml`'s header comment claimed an `e2e` stage that did not exist.
    - _Design: §0.5 debt D3, §8.3, §8.3.2, §13.4; Deliverable: 1.11; Criterion: 10_

  - [x] 18.4 Add the accessibility assertions to the journey
    - Assert the skip link, landmark structure, one `<h1>` per route, keyboard reachability and activation of approve/reject, the radar chart's accessible table, and focus management when the diff route loads.
    - Assert the SSE progress live region announces phase changes and completion but does not announce token deltas.
    - _Design: §12.5, §12.6; Deliverable: 1.4, 1.6; Criterion: 5, 10_

- [ ] 19. Gate coverage, run the negative controls, and assemble the workflow

  - [x] 19.1 Turn on the per-component coverage gates

    - Set `--cov=src --cov-branch --cov-fail-under=70` in the backend `addopts`, add `scripts/check-coverage.sh 70` over `agent/./internal/...`, and keep the frontend thresholds from task 17.11.
    - Commit the exclusion list for vendored `.wasm` artifacts and generated code as explicit paths rather than wildcards, and never aggregate the three numbers.
    - Add a check asserting no component's threshold can be lowered without editing the committed value.
    - _Design: §7.13, §17.1 D-31; Deliverable: 1.11; Criterion: 11_

  - [ ] 19.2 Complete `mutations.toml` and add the `mutation` CI job

    - Ensure `backend/tests/mutation/mutations.toml` carries one row for every property Q-01 … Q-31 with the exact negative control named in Appendix B.
    - Add the `mutation` job running `python scripts/mutation-harness.py --all`, printing the `property → mutation → expected FAIL → observed` table, and failing on any `VACUOUS` row, any missing row, or a dirty working tree afterwards.
    - Add `make mutation` and assert the harness's temp directory is outside the repository on every run.
    - _Design: §0.4.5, §8.3.3, §13.4, Appendix B; Deliverable: 1.11; Criterion: 11; Property: Q-01–Q-31_

  - [x] 19.3 Assemble the fifteen-job workflow and prove every cited job exists
    - Extend `.github/workflows/ci.yml` to the §8.3 job set — `changes`, `pre-commit`, `lock-integrity`, `agent`, `backend`, `frontend`, `compose-smoke`, `audit`, `supply`, `k8s`, `e2e`, `mutation`, `policy`, `templates`, `secrets`, `auth` — with all actions SHA-pinned and `changes` filters extended for `policies/**` and `agent/internal/scanner/grammars/**`.
    - Add the OPA, Cerbos and fixture-issuer services to the `backend` job, set `FORGEOPS_REQUIRE_INTEGRATION=1` everywhere tests run, and gate the three heavy jobs on filters plus `main` with a nightly full run.
    - Run `scripts/check-ci-jobs.py` in `pre-commit` so no Appendix E evidence string can name a job the workflow does not define.
    - _Design: §8.3, §8.4, §15.10; Deliverable: 1.11; Criterion: 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14_

- [ ] 20. Verify every Phase 1 completion criterion using only earlier implementation, then finalise records

  - [x] 20.1 Verify criterion 1 — install, pair, import

    - Execute the `e2e` journey's install/pair/import steps, the `agent` pairing and journal round-trip tests, and the `supply` step extracting the `linux_amd64` archive and running `forgeops-agent version`.
    - Implement nothing here; a failure returns to tasks 8.1–8.5, 4.6 or 12.1 and then reruns only the affected checks.
    - _Design: Appendix E criterion 1; Deliverable: 1.1, 1.2, 1.11; Criterion: 1; Property: Q-17, Q-31_

  - [x] 20.2 Verify criterion 2 — scan and readiness score

    - Execute the `agent` wazero parse and chunk-size assertions, the dependency-edge resolution tests, `test_readiness_determinism.py` with Q-18, and the `e2e` step rendering a non-zero score with a category breakdown.
    - _Design: Appendix E criterion 2; Deliverable: 1.3, 1.4; Criterion: 2; Property: Q-10, Q-18, Q-25_

  - [x] 20.3 Verify criterion 3 — AI generates a Dockerfile and K8s manifests

    - Execute `test_generation_integration.py` against local HTTP fixture endpoints with no vendor key or network, assert a schema-valid `ArtifactSet` with a Dockerfile and Deployment + Service + Ingress, and confirm Q-27 proves the tier chain came from the YAML.
    - _Design: Appendix E criterion 3; Deliverable: 1.5; Criterion: 3; Property: Q-08, Q-27, Q-29_

  - [x] 20.4 Verify criterion 4 — generated files pass the validation pipeline

    - Execute the `agent` validator suite, the `k8s` server-side dry-run and `helm template --validate` runs, and the `templates` job proving all 8 × 5 template artifacts traverse the identical pipeline with zero blocking findings.
    - _Design: Appendix E criterion 4; Deliverable: 1.5; Criterion: 4; Property: Q-09, Q-21_

  - [x] 20.5 Verify criterion 5 — view diff, approve, apply

    - Execute the `e2e` diff/approve/apply steps in both view modes, the frontend diff-fidelity tests with Q-23, and the backend Q-22 state-machine and concurrent-approval assertions.
    - _Design: Appendix E criterion 5; Deliverable: 1.6; Criterion: 5; Property: Q-22, Q-23_

  - [x] 20.6 Verify criterion 6 — atomic application with backup

    - Execute Q-01 and Q-02 in the `agent` job and the `e2e` on-disk hash, backup-existence and byte-exact revert steps.
    - _Design: Appendix E criterion 6; Deliverable: 1.6; Criterion: 6; Property: Q-01, Q-02_

  - [x] 20.7 Verify criterion 7 — policies are enforced

    - Execute `opa test policies/ -v` and `opa check --strict` in the `policy` job, the Friday-clock integration test returning `403 policy-denied` with an audit record and no minted envelope, and Q-06/Q-07.
    - _Design: Appendix E criterion 7; Deliverable: 1.7, 1.10; Criterion: 7; Property: Q-06, Q-07_

  - [x] 20.8 Verify criterion 8 — secrets stored encrypted and injected

    - Execute the `secrets` job's Infisical CRUD round trip, the no-value-read assertions with Q-20, the injection confinement checks with Q-28, and Q-12/Q-13/Q-24 in the `backend` job.
    - _Design: Appendix E criterion 8; Deliverable: 1.8; Criterion: 8; Property: Q-12, Q-13, Q-20, Q-24, Q-28_

  - [x] 20.9 Verify criterion 9 — immutable audit trail

    - Execute the `0007` migration test proving UPDATE, DELETE and TRUNCATE raise `42501` and the app role holds no UPDATE privilege, `check-db-roles.py`, Q-04, Q-05, and the `e2e` audit-viewer step showing the full transit.
    - _Design: Appendix E criterion 9; Deliverable: 1.9; Criterion: 9; Property: Q-04, Q-05_

  - [x] 20.10 Verify criterion 10 — the end-to-end journey

    - Execute the `e2e` job's full thirteen-step journey against built containers with a real paired agent, with the `k8s` job supplying the server-side dry-run, and confirm traces and agent logs upload on failure.
    - _Design: Appendix E criterion 10; Deliverable: 1.1–1.9; Criterion: 10_

  - [x] 20.11 Verify criterion 11 — coverage ≥ 70 % per component

    - Execute the three coverage gates independently, confirm none is aggregated, and confirm the `mutation` job reports no `VACUOUS` row and `check-no-skips.py` reports zero skips in the mandatory selection.
    - _Design: Appendix E criterion 11; Deliverable: 1.11; Criterion: 11; Property: Q-01–Q-31_

  - [x] 20.12 Verify criterion 12 — HNSW indexes on both vector columns

    - Execute the `0003` migration test asserting `vector(1536)` and `vector(1024)`, both HNSW indexes with `vector_cosine_ops` and `m='16', ef_construction='64'`, and the per-transaction `ef_search` behaviour, all under `FORGEOPS_REQUIRE_INTEGRATION=1`.
    - _Design: Appendix E criterion 12; Deliverable: 1.3; Criterion: 12_

  - [x] 20.13 Verify criterion 13 — SSE streaming

    - Execute `test_sse_generation.py`, assert `sse-starlette` is absent from `requirements.lock`, confirm only the six event names appear with monotonic `PROGRESS` and one terminal event (Q-26), run the frontend reader tests, and confirm the `e2e` step observes the live sequence.
    - _Design: Appendix E criterion 13; Deliverable: 1.5; Criterion: 13; Property: Q-26_

  - [x] 20.14 Verify criterion 14 — Redis semantic caching

    - Execute the real-Redis integration test proving a repeated prompt is served from L1 with zero provider calls and a near-duplicate from L2 above the 0.95 threshold, together with Q-13's cache-key clause.
    - _Design: Appendix E criterion 14; Deliverable: 1.5; Criterion: 14; Property: Q-13_

  - [ ] 20.15 Finalise documentation and `PROGRESS.md` from captured evidence
    - Update project-owned `README.md`, `docs/{architecture,api,development,deployment}.md` and `PROGRESS.md` **in place**: add the Phase 1 task list, the fourteen criteria with real evidence, the `Q-01 … Q-31` coverage table including each negative-control row, decisions D-28 … D-50, open questions OQ-22 … OQ-32 and the Phase 0 dispositions, and the inherited-debt closure table for D1–D5.
    - Preserve the entire Phase 0 record: no Phase 0 row, criterion, decision or deviation is deleted or reworded, closed items are marked closed and dated, and the four authoritative root documents plus `REVIEW-PHASE-0.md` stay byte-identical.
    - Extend `scripts/check-progress.sh` to require every Phase 1 deliverable §1.1–§1.11, all fourteen criteria with non-empty evidence, all thirty-one properties with locations and controls, and every decision row; mark Phase 1 `completed` only when all of that holds and no Phase 2+ implementation or dependency is present.
    - _Design: §0.5, §17, §18, Appendix B, Appendix E; Deliverable: 1.1–1.11, Progress record; Criterion: 1–14; Property: Q-01–Q-31_

## Notes

- All 166 numbered executable leaves are mandatory and cannot be skipped for a faster MVP; implementation leaves still land focused example/unit checks for their own behaviour.
- Group 1 exists before group 2 on purpose: the §0.4 regime is infrastructure, and a lint added after the code it governs finds nothing. Group 2 exists before every feature group because §0.5's debt is a prerequisite, not cleanup.
- Task 2.1 (wiring `load_tier_config` into `create_app`) and task 2.2 (Q-27) are the gate for all of group 13. **No generation leaf appears before them**, because §1.5 sits entirely on six-tier routing and Phase 0 proved the shipped YAML was never what a running backend loaded.
- Group 7 (`MutationAuthority`, the `executor/internal/mutate` boundary, `check-chokepoint.sh`) precedes group 8's mutating operations and group 16's apply surface, so no mutating path is ever written outside the boundary even transiently.
- Group 10 (the redaction chokepoint and `RedactedPrompt`) precedes group 13, so the first prompt assembled from repository content cannot predate the type that makes redaction mandatory.
- **Group 7's execution order is 7.1, 7.2, 7.4, 7.6, 7.5, 7.3, then 7.7–7.11**, and the Task Dependency Graph below already says so: wave 5 carries 7.1 and 7.2, wave 6 carries 7.3, 7.4 and 7.6, wave 7 carries 7.5 and 7.9, wave 8 carries 7.7, 7.8, 7.10 and 7.11. Two points are worth stating rather than leaving to be re-derived. **7.6 precedes 7.5** because stage 5 of the six-stage chokepoint *is* the audit write (Appendix A.3), so building 7.5 first would mean either calling a writer that does not exist or standing up a substitute collaborator — which §0.4.1 forbids by name. **7.3 runs last of the implementation leaves** for the reason written into the leaf itself: its own non-vacuity rule requires a non-empty `@mutation_primitive` set, and the first true primitives are created by 7.6 and 7.5.
- **Resequencing applied during implementation.** Leaf 4.2 originally carried a bullet requiring `secretscan.Redact`, which leaf 10.1 creates. The wave graph put 4.2 in wave 2 and 10.1 in wave 12, so the bullet was unbuildable where it stood. It moved to 10.1 rather than dragging 4.2 ten waves later, because 4.2's other half — making `logging.NewRedacted` the only reachable agent logger — is a wave-2 prerequisite for every later agent subsystem. No wave-graph edge changed: removing the bullet removed the only forward dependency 4.2 had. The other direction was rejected: pushing 4.2 to wave 12 would have let eight agent subsystems land against an unfiltered logger first.
- **Debt D5's OPA premise was false and is corrected by D-51.** Leaf 2.5 as written required `openpolicyagent/opa:1.4.2-rootless`, which OPA 1.x does not publish, while the already-pinned `1.4.2` image runs as `USER 1000:1000` on a Chainguard base. The leaf's second failure mode became "no service may override its image's runtime user back to root", and the non-root property is proved at runtime in `compose-smoke` instead of by a tag substring.
- Property tasks map one-for-one onto Design Appendix B Q-01 … Q-31 and each carries its `mutations.toml` row and negative control. Cross-runtime properties are tested in one leaf against a shared fixture corpus.
- Optional and new Compose services arrive only with their owning implementation: Authentik and the `auth` job in 6.3, Cerbos in 6.4, Infisical and the `secrets` job in 10.4, Trivy in the devtools image in 14.5, the kind cluster in 14.9, the e2e overlay in 18.1.
- Migrations `0002` … `0009` are separate leaves 5.1–5.8, each with the §6.5 proof gated by `require_capability("postgres")` so it fails rather than skips in CI; 5.9 adds the linearity and clean-autogenerate assertions.
- Tasks 20.1–20.14 may only execute or inspect behaviour built earlier. They contain no implementation; a failure returns to the owning leaf and reruns only the affected checks. Task 20.15 is the only leaf that edits `PROGRESS.md`, and it edits in place.
- No task creates `requirements.md` or `tasks.meta.json`, implements Phase 2+ behaviour, adds `tofu apply`, or modifies the four authoritative root documents.

## Open sequencing questions

- **Where the `auth` job's real-Authentik flow sits relative to `e2e`.** Task 6.3 adds the real-IdP job while 18.1 gives the journey a fixture issuer, per OQ-28. Recommendation: keep the split as planned — the journey should not pay Authentik's cold start on every run, and both paths are required checks so neither is unproven. If you would rather have one path, move 18.1's issuer to Authentik and accept the slower `e2e`.
- **Grammar build reproducibility (OQ-29) inside task 11.1.** If a grammar will not build reproducibly in the pinned container, the plan's fallback is to omit that language's AST support and fall back to line-based chunking for it. Recommendation: keep 11.1 as a single leaf and record any omitted language in `grammars.lock.json` plus `agent doctor`, rather than splitting the leaf per grammar before knowing which ones are affected.
- **Whether the Helm SDK stays unconditional (OQ-27).** Task 14.4 adds it unconditionally and 14.7 reports the size impact. Recommendation: measure the six real artifacts in the first release before deciding; if the size is unacceptable, a follow-up leaf adds the `noheml` build tag and degrades Helm validation to the external binary when present.
- **Granularity of the template sets.** Tasks 15.2–15.5 pair two languages per leaf so each leaf is one focused session with its own fixture project and rendering tests. Recommendation: keep the pairing; splitting to eight leaves would add ceremony without adding a test boundary, and merging to one would let a language land without its fixture.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["1.2", "1.3", "1.4", "1.5", "1.6", "1.7"] },
    {
      "id": 2,
      "tasks": [
        "1.8",
        "2.1",
        "2.4",
        "2.5",
        "2.6",
        "2.7",
        "3.1",
        "3.5",
        "4.1",
        "4.2",
        "4.3",
        "4.7"
      ]
    },
    {
      "id": 3,
      "tasks": ["2.2", "2.3", "3.2", "3.3", "3.4", "4.4", "4.5", "4.6"]
    },
    {
      "id": 4,
      "tasks": ["5.1", "5.2", "5.3", "5.4", "5.5", "5.6", "5.7", "5.8"]
    },
    { "id": 5, "tasks": ["5.9", "6.1", "6.2", "7.1", "7.2"] },
    { "id": 6, "tasks": ["6.3", "6.4", "7.3", "7.4", "7.6"] },
    { "id": 7, "tasks": ["6.5", "6.6", "6.7", "7.5", "7.9"] },
    { "id": 8, "tasks": ["7.7", "7.8", "7.10", "7.11", "8.1", "8.2"] },
    { "id": 9, "tasks": ["8.3", "8.4", "9.1", "9.2", "9.3"] },
    { "id": 10, "tasks": ["8.5", "8.6", "9.4", "9.5"] },
    {
      "id": 11,
      "tasks": ["8.7", "8.8", "8.9", "8.10", "8.11", "8.12", "9.6", "9.7"]
    },
    { "id": 12, "tasks": ["10.1", "10.2", "10.3", "10.4"] },
    { "id": 13, "tasks": ["10.5", "10.6", "10.7", "10.8", "10.9", "11.1"] },
    { "id": 14, "tasks": ["11.2", "11.3", "11.4"] },
    { "id": 15, "tasks": ["11.5", "11.6", "11.7", "11.8"] },
    { "id": 16, "tasks": ["11.9", "11.10", "12.1", "12.2"] },
    { "id": 17, "tasks": ["11.11", "11.12", "11.13", "12.3", "12.4"] },
    {
      "id": 18,
      "tasks": ["12.5", "14.1", "14.2", "14.4", "14.5", "14.6", "14.7", "14.8"]
    },
    { "id": 19, "tasks": ["14.3", "14.9"] },
    { "id": 20, "tasks": ["13.1", "13.2", "13.3", "13.4"] },
    { "id": 21, "tasks": ["13.5", "13.6", "13.7"] },
    { "id": 22, "tasks": ["13.8", "15.1"] },
    { "id": 23, "tasks": ["15.2", "15.3", "15.4", "15.5"] },
    { "id": 24, "tasks": ["13.9", "15.6", "15.7"] },
    { "id": 25, "tasks": ["13.10", "13.11", "13.12", "13.13"] },
    { "id": 26, "tasks": ["16.1", "16.2"] },
    { "id": 27, "tasks": ["16.3", "16.4", "17.1", "17.2"] },
    {
      "id": 28,
      "tasks": ["17.3", "17.4", "17.5", "17.6", "17.7", "17.8", "17.9", "17.10"]
    },
    { "id": 29, "tasks": ["17.11", "18.1"] },
    { "id": 30, "tasks": ["18.2", "18.4"] },
    { "id": 31, "tasks": ["18.3", "19.1", "19.2"] },
    { "id": 32, "tasks": ["19.3"] },
    {
      "id": 33,
      "tasks": [
        "20.1",
        "20.2",
        "20.3",
        "20.4",
        "20.5",
        "20.6",
        "20.7",
        "20.8",
        "20.9",
        "20.10",
        "20.11",
        "20.12",
        "20.13",
        "20.14"
      ]
    },
    { "id": 34, "tasks": ["20.15"] }
  ]
}
```
