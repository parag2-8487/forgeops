?# ForgeOps knowledge graph

An interactive map of this repository, generated with
[Understand-Anything](https://github.com/Egonex-AI/Understand-Anything) pinned at **v2.9.0**
(commit `f08763d11d0202a8a8f52b5dedda6d1b2e2ebac8`).

| Field          | Value                                                                                 |
| :------------- | :------------------------------------------------------------------------------------ |
| Generated      | **2026-08-19**, rebuilt after the seventeen remaining mutation controls landed        |
| Tool           | Understand-Anything v2.9.0, installed at user level (see below)                       |
| Files analysed | 757 of 761 in the inventory (4 excluded)                                              |
| Graph          | 3,649 nodes · 3,883 edges · 14 layers · 15 tour steps                                 |
| Edges          | 2,892 `contains` (file to function/class) · 991 `imports` (resolved project-internal) |
| Validation     | 0 issues, 227 orphan warnings (documents, configs and scripts that no source imports) |

## Open it

Node.js ≥ 18 is the only requirement. Nothing is sent anywhere; the viewer serves the graph
read-only from local disk and makes no LLM calls.

```sh
npx https://github.com/Egonex-AI/Understand-Anything/releases/download/v2.9.0/understand-anything-viewer.tgz docs/understand-anything
```

It prints a tokenised `http://127.0.0.1:5173/?token=≥` URL. What you get:

- **The structural graph.** Every analysed file, function and class as a node you can click,
  search or filter, colour-coded by architectural layer.
- **Layers.** Fourteen of them, listed below. Selecting one isolates its members.
- **Search.** By name, and semantically over the node summaries — "which parts handle auth?"
  returns the auth package, the route-auth checker and the Cerbos policies.
- **A guided tour.** Fifteen steps ordered so each one only depends on earlier ones. Step 1 is
  the learning journal, step 2 is the Phase 0 review, and the rest walk the composition roots
  before the components they compose.

**Source snippets will not resolve in this layout.** The dashboard's file-content endpoint
resolves a node's `filePath` relative to the directory you pass it, and these paths are
relative to the repository root. To get clickable source, point the viewer at the repository
root with the data directory beside it — copy `docs/understand-anything/.ua` to `./.ua` first
and delete it afterwards. It is kept under `docs/` by default so that a generated artifact
never appears as untracked noise at the repository root.

## The fourteen layers

| Layer                 | Nodes | What belongs in it                                                        |
| :-------------------- | ----: | :------------------------------------------------------------------------ |
| Governance & Trust    |    49 | the mutation chokepoint, the audit chain, policy, identity, authorization |
| API Surface           |    29 | routers, the app factory, middleware, RFC 9457, the MCP gateway           |
| AI & Generation       |    33 | six-tier routing, breaker, cascade, cache, rate limiting                  |
| Analysis              |    16 | the codebase index, plan analyzer, blast radius, readiness                |
| Data                  |    15 | tables, the nine migrations, HNSW indexes, the two-role split             |
| Agent Core            |    44 | the agent's composition root, config, redacting logger, session transport |
| Agent Execution       |    83 | the mutation path, IaC runner, git client, scanner, MCP server            |
| Frontend              |    30 | the App Router shell, the API client, the env contract, UI state          |
| Policy as Code        |    11 | Rego for the gateway, Cerbos YAML for RBAC                                |
| Test Integrity        |   220 | the regime built after 419 green tests shipped over a broken gateway      |
| Verification & Checks |    72 | the scripts that turn a design claim into a build failure                 |
| Build & Supply Chain  |    73 | CI, Compose, pinned toolchains, signing and provenance                    |
| Documentation & Specs |    30 | the four read-only documents, the phase specs, steering rules             |
| Future-Phase Seams    |    52 | README-only markers for behaviour a later phase owns                      |

## How it was generated, and what is and is not machine-derived

Be clear about this when reading the graph.

**Machine-derived, deterministic, reproducible.** The file inventory, languages, categories and
line counts come from the plugin's `scan-project.mjs`. The import edges come from
`extract-import-map.mjs`, which parses each file with tree-sitter and applies per-language
resolution rules — 998 raw edges, 991 surviving after both endpoints were required to be in
scope. The function and class nodes, with their line ranges, come from
`extract-structure.mjs`, also tree-sitter. Re-running these on unchanged source produces
identical output.

**Authored.** Node summaries, tags, the fourteen layer definitions and the fifteen tour steps
live in `semantic-overlay.json`. Upstream, these are produced by the plugin's `file-analyzer`,
`architecture-analyzer` and `tour-builder` LLM subagents on every run. Here they are written
down once and reviewed, so the graph's prose is stable across regenerations and can be
corrected by editing one file. Summaries are assigned by longest-matching path prefix, so a
file without an explicit entry inherits its directory's description; function and class nodes
derive their summary from their file's.

**Not run.** The upstream `--review` LLM graph-reviewer pass. The default deterministic
validation ran instead, and its output is `.ua/intermediate/review.json`.

## Exclusions

`.ua/.understandignore` layers project exclusions on top of the tool's defaults
(`node_modules/`, `.git/`, `.venv/`, `dist/`, `*.lock`, binaries). The categories and why each
exists are documented in that file. The one that matters: `.env` and every `.env.*` variant are
excluded, with `.env.example` explicitly re-included because it is placeholder-only by contract
and is part of the configuration surface a reader needs. No credential, token or real value
reaches this graph.

The inventory itself came from `git ls-files -co --exclude-standard`, which the scanner prefers
because it respects `.gitignore` — which is why only 4 files needed excluding here. Files
already ignored by git (`backend/.venv/`, `.evidence/`, `scripts/_*` scratch files) never
entered the inventory.

## Regenerate it

Two steps. From the repository root:

```sh
# 1. Deterministic inventory
node "$HOME/.understand-anything/Understand-Anything-2.9.0/understand-anything-plugin/skills/understand/scan-project.mjs" \
     . docs/understand-anything/.ua/intermediate/scan-result.raw.json

# 2. Import resolution, structural extraction, graph assembly and validation
node docs/understand-anything/build-graph.mjs
```

**Expected warnings on rebuild.** `extract-import-map` prints one
`has no ancestor go.mod — module-prefix imports skipped` warning per file in
`backend/tests/mutation/overlays/`. That is correct and deliberate: those files are negative-control
overlays substituted into a build by `scripts/mutation-harness.py` via `go build -overlay`, and they
live outside the `agent/` module precisely so they can never be compiled into the agent. Their
standard-library imports are unresolvable from where they sit, and nothing depends on resolving them.

`build-graph.mjs` refuses to write `knowledge-graph.json` if validation finds any issue, and
dies if the inventory is empty or if any path has no overlay rule — a new top-level directory
therefore forces a decision rather than silently landing in a default bucket.

When source changes materially, update `semantic-overlay.json` in the same commit. The steering
rule `.antigravity/steering/learning-journal.md` requires regenerating this artifact whenever a group
of task leaves completes, and recording the date in the table at the top of this file.

## Installation, for reference

The plugin is a Claude-Code-style skills bundle with first-class Kiro support. It is installed
at user level, outside this repository:

| Path                                                | What                                                                                                                        |
| :-------------------------------------------------- | :-------------------------------------------------------------------------------------------------------------------------- |
| `~/.understand-anything/Understand-Anything-2.9.0/` | the extracted `v2.9.0` release (SHA-256 of the archive: `2F4461C3DD14AFF3F87248FAA2573D0C74F0AFA78C0B3AEEE24ADF7ED588D85F`) |
| `~/.antigravity/skills/understand*`                 | nine directory junctions into the plugin's `skills/`                                                                        |
| `~/.antigravity/agents/understand.json`             | the Kiro agent definition, prompt-pointed at `skills/understand/SKILL.md`                                                   |
| `~/.understand-anything-plugin`                     | the universal plugin-root junction the skills resolve through                                                               |

Nothing was installed into this repository, and no credential was given to the tool. The
Figma analysis mode, which is the only feature that requires a token, is not configured. The
plugin's own `SECURITY.md` describes it as a local-only static-analysis tool that does not
phone home; the deterministic scripts used here make no network calls at all.

## Regeneration date log

| Date       | Leaves     | Note                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| :--------- | :--------- | :-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 2026-07-31 | 38 of 166  | first generation                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| 2026-07-31 | 44 of 166  | regenerated after group 6 completed and 7.1 landed                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| 2026-07-31 | 54 of 166  | regenerated at group 7's close-out, all eleven of its leaves landed. 532 files scanned, 528 analysed, 2574 nodes, 2668 edges (622 imports), 14 layers, 15 tour steps, 182 orphan warnings. Two new `extract-import-map` warnings are expected and are evidence rather than noise: `backend/tests/mutation/overlays/q0{1,2}_*.go` have no ancestor `go.mod`, because Appendix B's Go negative controls are deliberately outside the module tree so `go build ./...` never compiles them and they are reachable only through the overlay JSON the mutation harness writes                                                                                                                                                     |
| 2026-08-02 | 66 of 166  | regenerated at group 8's close-out, all twelve of its leaves landed. 582 files scanned, 3071 nodes, 3315 edges (826 imports), 14 layers, 15 tour steps, 190 orphan warnings. **Three new `extract-import-map` warnings, all expected and all evidence rather than noise:** `backend/tests/mutation/overlays/q1{4,5}_*.go` and `q31_envelope_kind_admitted.go` have no ancestor `go.mod`, for exactly the reason the `q0{1,2}` overlays do — Appendix B's Go negative controls live outside the module tree so `go build ./...` never compiles them, and they are reachable only through the overlay JSON the mutation harness writes. A warning per Go overlay is therefore the correct count: five overlays, five warnings |
| 2026-08-06 | 89 of 166  | regenerated at group 11's close-out, all thirteen of its leaves landed. 666 files scanned, 662 analysed, 3333 nodes, 3589 edges (918 imports), 14 layers, 15 tour steps.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| 2026-08-06 | 94 of 166  | regenerated at group 12's close-out, all five of its leaves landed. 673 files scanned, 669 analysed, 3364 nodes, 3621 edges (926 imports), 14 layers, 15 tour steps.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| 2026-08-06 | 107 of 166 | regenerated at group 13's close-out, all thirteen of its leaves landed. 696 files scanned, 692 analysed, 3430 nodes, 3679 edges (941 imports), 14 layers, 15 tour steps.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| 2026-08-06 | 116 of 166 | regenerated at group 14's close-out, all nine of its leaves landed. 716 files scanned, 712 analysed, 3487 nodes, 3726 edges (951 imports), 14 layers, 15 tour steps.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| 2026-08-06 | 123 of 166 | regenerated at group 15's close-out, all seven of its leaves landed. 722 files scanned, 718 analysed, 3501 nodes, 3738 edges (955 imports), 14 layers, 15 tour steps.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| 2026-08-06 | 127 of 166 | regenerated at group 16's close-out, all four of its leaves landed. 728 files scanned, 724 analysed, 3521 nodes, 3759 edges (962 imports), 14 layers, 15 tour steps.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| 2026-08-06 | 138 of 166 | regenerated at group 17's close-out, all eleven of its leaves landed. 742 files scanned, 738 analysed, 3547 nodes, 3781 edges (972 imports), 14 layers, 15 tour steps.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| 2026-08-07 | 166 of 166 | regenerated at Phase 1 final close-out, all 166 leaves complete. 749 files scanned, 745 analysed, 3557 nodes, 3787 edges (975 imports), 14 layers, 15 tour steps.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| 2026-08-19 | 166 of 166 | regenerated after the seventeen remaining mutation controls landed. 757 of 761 files analysed, 3,649 nodes, 3,883 edges (991 imports), 14 layers, 15 tour steps, 227 orphan warnings. **This row was missing until 2026-08-21.** That rebuild updated the header table at the top of this file and never appended here, so the header read 2026-08-19 while this log stopped at 2026-08-07 — the exact drift the rule above exists to prevent, in the file that states the rule.                                                                                                                                                                                                                                            |
| 2026-08-21 | 166 of 166 | **Not regenerated. Recorded as stale on purpose.** The graph describes the tree at `03d250f`. Since then `1ce7267` wired the L2 semantic cache into `create_app` and added a helper plus four tests, the frontend gained eleven files — nine routes and three shared components — and `fix_tests.py` was removed. So the header's node and edge counts are low and the frontend layer is missing its routes. Regenerating is the single command documented above; it was left undone rather than left unmentioned, because a graph quietly describing an older tree is worse than one labelled as doing so.                                                                                                                 |
