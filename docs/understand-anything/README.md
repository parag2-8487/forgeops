?# ForgeOps knowledge graph

An interactive map of this repository, generated with
[Understand-Anything](https://github.com/Egonex-AI/Understand-Anything) pinned at **v2.9.0**
(commit `f08763d11d0202a8a8f52b5dedda6d1b2e2ebac8`).

| Field          | Value                                                                                      |
| :------------- | :----------------------------------------------------------------------------------------- |
| Generated      | **2026-08-02**, regenerated at group 8's close-out with all twelve of its leaves landed    |
| Tool           | Understand-Anything v2.9.0, installed at user level (see below)                            |
| Files analysed | 463 of 467 in the inventory (4 excluded)                                                   |
| Graph          | 2,070 nodes · 2,118 edges · 14 layers · 15 tour steps                                      |
| Edges          | 1,607 `contains` (file to function/class) · 511 `imports` (resolved project-internal)      |
| Validation     | 0 issues, 168 orphan warnings (documents, configs and scripts that no source file imports) |

## Open it

Node.js �?� 18 is the only requirement. Nothing is sent anywhere; the viewer serves the graph
read-only from local disk and makes no LLM calls.

```sh
npx https://github.com/Egonex-AI/Understand-Anything/releases/download/v2.9.0/understand-anything-viewer.tgz docs/understand-anything
```

It prints a tokenised `http://127.0.0.1:5173/?token=�?�` URL. What you get:

- **The structural graph.** Every analysed file, function and class as a node you can click,
  search or filter, colour-coded by architectural layer.
- **Layers.** Fourteen of them, listed below. Selecting one isolates its members.
- **Search.** By name, and semantically over the node summaries �?" "which parts handle auth?"
  returns the auth package, the route-auth checker and the Cerbos policies.
- **A guided tour.** Fifteen steps ordered so each one only depends on earlier ones. Step 1 is
  the learning journal, step 2 is the Phase 0 review, and the rest walk the composition roots
  before the components they compose.

**Source snippets will not resolve in this layout.** The dashboard's file-content endpoint
resolves a node's `filePath` relative to the directory you pass it, and these paths are
relative to the repository root. To get clickable source, point the viewer at the repository
root with the data directory beside it �?" copy `docs/understand-anything/.ua` to `./.ua` first
and delete it afterwards. It is kept under `docs/` by default so that a generated artifact
never appears as untracked noise at the repository root.

## The fourteen layers

| Layer                 | Nodes | What belongs in it                                                        |
| :-------------------- | ----: | :------------------------------------------------------------------------ |
| Governance & Trust    |    22 | the mutation chokepoint, the audit chain, policy, identity, authorization |
| API Surface           |    26 | routers, the app factory, middleware, RFC 9457, the MCP gateway           |
| AI & Generation       |    19 | six-tier routing, breaker, cascade, cache, rate limiting                  |
| Analysis              |    12 | the codebase index, plan analyzer, blast radius, readiness                |
| Data                  |    14 | tables, the nine migrations, HNSW indexes, the two-role split             |
| Agent Core            |    35 | the agent's composition root, config, redacting logger, session transport |
| Agent Execution       |    39 | the mutation path, IaC runner, git client, scanner, MCP server            |
| Frontend              |    27 | the App Router shell, the API client, the env contract, UI state          |
| Policy as Code        |    11 | Rego for the gateway, Cerbos YAML for RBAC                                |
| Test Integrity        |   122 | the regime built after 419 green tests shipped over a broken gateway      |
| Verification & Checks |    50 | the scripts that turn a design claim into a build failure                 |
| Build & Supply Chain  |    38 | CI, Compose, pinned toolchains, signing and provenance                    |
| Documentation & Specs |    24 | the four read-only documents, the phase specs, steering rules             |
| Future-Phase Seams    |    12 | README-only markers for behaviour a later phase owns                      |

## How it was generated, and what is and is not machine-derived

Be clear about this when reading the graph.

**Machine-derived, deterministic, reproducible.** The file inventory, languages, categories and
line counts come from the plugin's `scan-project.mjs`. The import edges come from
`extract-import-map.mjs`, which parses each file with tree-sitter and applies per-language
resolution rules �?" 517 raw edges, 511 surviving after both endpoints were required to be in
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
because it respects `.gitignore` �?" which is why only 4 files needed excluding here. Files
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

`build-graph.mjs` refuses to write `knowledge-graph.json` if validation finds any issue, and
dies if the inventory is empty or if any path has no overlay rule �?" a new top-level directory
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

| Date       | Leaves    | Note                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| :--------- | :-------- | :-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 2026-07-31 | 38 of 166 | first generation                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| 2026-07-31 | 44 of 166 | regenerated after group 6 completed and 7.1 landed                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| 2026-07-31 | 54 of 166 | regenerated at group 7's close-out, all eleven of its leaves landed. 532 files scanned, 528 analysed, 2574 nodes, 2668 edges (622 imports), 14 layers, 15 tour steps, 182 orphan warnings. Two new `extract-import-map` warnings are expected and are evidence rather than noise: `backend/tests/mutation/overlays/q0{1,2}_*.go` have no ancestor `go.mod`, because Appendix B's Go negative controls are deliberately outside the module tree so `go build ./...` never compiles them and they are reachable only through the overlay JSON the mutation harness writes                                                                                                                                                     |
| 2026-08-02 | 66 of 166 | regenerated at group 8's close-out, all twelve of its leaves landed. 582 files scanned, 3071 nodes, 3315 edges (826 imports), 14 layers, 15 tour steps, 190 orphan warnings. **Three new `extract-import-map` warnings, all expected and all evidence rather than noise:** `backend/tests/mutation/overlays/q1{4,5}_*.go` and `q31_envelope_kind_admitted.go` have no ancestor `go.mod`, for exactly the reason the `q0{1,2}` overlays do — Appendix B's Go negative controls live outside the module tree so `go build ./...` never compiles them, and they are reachable only through the overlay JSON the mutation harness writes. A warning per Go overlay is therefore the correct count: five overlays, five warnings |
| 2026-08-06 | 89 of 166 | regenerated at group 11's close-out, all thirteen of its leaves landed. 666 files scanned, 662 analysed, 3333 nodes, 3589 edges (918 imports), 14 layers, 15 tour steps. |
| 2026-08-06 | 94 of 166 | regenerated at group 12's close-out, all five of its leaves landed. 673 files scanned, 669 analysed, 3364 nodes, 3621 edges (926 imports), 14 layers, 15 tour steps. |
| 2026-08-06 | 107 of 166 | regenerated at group 13's close-out, all thirteen of its leaves landed. 696 files scanned, 692 analysed, 3430 nodes, 3679 edges (941 imports), 14 layers, 15 tour steps. |



