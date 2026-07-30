#!/usr/bin/env node
/**
 * build-graph.mjs — assemble the ForgeOps knowledge graph from the
 * Understand-Anything v2.9.0 deterministic extractors plus an authored
 * semantic overlay.
 *
 * Why this script exists
 * ----------------------
 * The upstream `/understand` skill orchestrates seven phases, five of which are
 * LLM subagents. Phases 1, 1.5 and 2's structural half are deterministic Node
 * scripts shipped with the plugin; this script drives exactly those, in the
 * order the skill prescribes, so the structural half of the graph is
 * reproducible byte-for-byte without a model. The semantic half — summaries,
 * tags, architectural layers, the guided tour — is what the plugin's
 * `file-analyzer`, `architecture-analyzer` and `tour-builder` agents produce;
 * here it comes from `semantic-overlay.json`, which is authored and reviewable
 * rather than regenerated on every run.
 *
 * Usage (from the repository root):
 *   node docs/understand-anything/build-graph.mjs
 *
 * Inputs
 *   docs/understand-anything/.ua/intermediate/scan-result.raw.json   (scan-project.mjs)
 *   docs/understand-anything/.ua/.understandignore                   (exclusions)
 *   docs/understand-anything/semantic-overlay.json                   (authored)
 *
 * Outputs
 *   docs/understand-anything/.ua/intermediate/scan-result.json
 *   docs/understand-anything/.ua/intermediate/import-map.json
 *   docs/understand-anything/.ua/intermediate/structure.json
 *   docs/understand-anything/.ua/knowledge-graph.json
 *   docs/understand-anything/.ua/meta.json
 *
 * Exit codes: 0 on success; 1 on any missing input, extractor failure, or
 * graph-validation failure. It never writes a partial knowledge-graph.json.
 */
import { execFileSync } from 'node:child_process';
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, join, posix, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { createRequire } from 'node:module';

const HERE = dirname(fileURLToPath(import.meta.url));
const PROJECT_ROOT = resolve(HERE, '..', '..');
const UA = join(HERE, '.ua');
const INTER = join(UA, 'intermediate');
const PLUGIN =
  process.env.UA_PLUGIN_ROOT ||
  join(process.env.USERPROFILE || process.env.HOME, '.understand-anything', 'Understand-Anything-2.9.0', 'understand-anything-plugin');
const SKILL = join(PLUGIN, 'skills', 'understand');

const die = (m) => {
  process.stderr.write(`build-graph: ${m}\n`);
  process.exit(1);
};

if (!existsSync(SKILL)) die(`plugin skill directory not found: ${SKILL}`);
mkdirSync(INTER, { recursive: true });

// --- 1. Load the deterministic inventory and apply the exclusions -----------
// The ignore matcher is the plugin's own `createIgnoreFilter`, pointed at this
// directory so it reads `docs/understand-anything/.ua/.understandignore`. That
// keeps the tool's exact matching semantics (gitignore-style, with negation)
// instead of reimplementing them, and keeps the exclusion list under docs/
// rather than adding an untracked file to the repository root.
const require_ = createRequire(join(PLUGIN, 'package.json'));
let core;
try {
  core = await import(pathToFileURL(require_.resolve('@understand-anything/core')).href);
} catch {
  core = await import(pathToFileURL(join(PLUGIN, 'packages/core/dist/index.js')).href);
}
const ignore = core.createIgnoreFilter(HERE);

const rawPath = join(INTER, 'scan-result.raw.json');
if (!existsSync(rawPath)) die(`missing ${rawPath} — run scan-project.mjs first (see README.md)`);
const raw = JSON.parse(readFileSync(rawPath, 'utf8'));

const kept = raw.files.filter((f) => !ignore.isIgnored(f.path));
const dropped = raw.files.length - kept.length;
process.stderr.write(`build-graph: inventory ${raw.files.length} files, ${dropped} excluded, ${kept.length} analysed\n`);
if (kept.length === 0) die('every file was excluded; an empty inventory proves nothing');

const overlayPath = join(HERE, 'semantic-overlay.json');
if (!existsSync(overlayPath)) die(`missing ${overlayPath}`);
const overlay = JSON.parse(readFileSync(overlayPath, 'utf8'));

const scan = { ...raw, files: kept, totalFiles: kept.length, filteredByIgnore: dropped };
writeFileSync(join(INTER, 'scan-result.json'), JSON.stringify(scan, null, 2));

// --- 2. Deterministic import resolution (tree-sitter, no LLM) ---------------
const runExtractor = (script, input, output) => {
  writeFileSync(join(INTER, `${output}-input.json`), JSON.stringify(input, null, 2));
  execFileSync(process.execPath, [join(SKILL, script), join(INTER, `${output}-input.json`), join(INTER, `${output}.json`)], {
    stdio: ['ignore', 'inherit', 'inherit'],
  });
  return JSON.parse(readFileSync(join(INTER, `${output}.json`), 'utf8'));
};

const importOut = runExtractor(
  'extract-import-map.mjs',
  { projectRoot: PROJECT_ROOT, files: kept.map(({ path, language, fileCategory }) => ({ path, language, fileCategory })) },
  'import-map',
);
const importMap = importOut.importMap || {};

// --- 3. Deterministic structural extraction (tree-sitter, no LLM) -----------
const structureOut = runExtractor(
  'extract-structure.mjs',
  { projectRoot: PROJECT_ROOT, batchFiles: kept, batchImportData: importMap },
  'structure',
);
const structureByPath = new Map((structureOut.results || []).map((r) => [r.path, r]));

// --- 4. The semantic overlay: layer, summary and tags per node -------------
// Longest-prefix wins, so a specific file overrides its directory.
const rules = [...overlay.rules].sort((a, b) => b.prefix.length - a.prefix.length);
const describe = (p) => {
  const r = rules.find(
    (x) => x.prefix === '*' || p === x.prefix || p.startsWith(x.prefix.endsWith('/') ? x.prefix : `${x.prefix}/`),
  );
  if (!r) die(`no overlay rule matches ${p}; add one to semantic-overlay.json rather than defaulting silently`);
  return r;
};

const NODE_TYPE_BY_CATEGORY = { code: 'file', config: 'config', docs: 'document', infra: 'service', script: 'file', data: 'config', markup: 'document' };
const nodeType = (f) => {
  if (f.path.startsWith('.github/workflows/')) return 'pipeline';
  if (f.path.startsWith('backend/alembic/versions/')) return 'table';
  if (f.path.startsWith('policies/')) return 'resource';
  return NODE_TYPE_BY_CATEGORY[f.fileCategory] || 'file';
};

const nodes = [];
const edges = [];
const seenNode = new Set();
const addNode = (n) => {
  if (seenNode.has(n.id)) return;
  seenNode.add(n.id);
  nodes.push(n);
};
const addEdge = (source, target, type, weight) => {
  edges.push({ source, target, type, weight });
};

const layerMembers = new Map();

for (const f of kept) {
  const r = describe(f.path);
  const id = `${nodeType(f)}:${f.path}`;
  addNode({
    id,
    type: nodeType(f),
    name: posix.basename(f.path),
    filePath: f.path,
    summary: r.summary,
    tags: r.tags,
    language: f.language,
    sizeLines: f.sizeLines,
    complexity: f.sizeLines > 600 ? 'complex' : f.sizeLines > 200 ? 'moderate' : 'simple',
  });
  if (!layerMembers.has(r.layer)) layerMembers.set(r.layer, []);
  layerMembers.get(r.layer).push(id);

  // Structural children: functions and classes, straight from tree-sitter.
  const st = structureByPath.get(f.path);
  for (const fn of st?.functions ?? []) {
    const fid = `function:${f.path}:${fn.name}`;
    addNode({
      id: fid,
      type: 'function',
      name: fn.name,
      filePath: f.path,
      summary: `${fn.name} in ${posix.basename(f.path)} — ${r.summary}`,
      tags: [...r.tags, 'function'],
      signature: fn.signature ?? fn.name,
      startLine: fn.startLine,
      endLine: fn.endLine,
    });
    addEdge(id, fid, 'contains', 1.0);
  }
  for (const cl of st?.classes ?? []) {
    const cid = `class:${f.path}:${cl.name}`;
    addNode({
      id: cid,
      type: 'class',
      name: cl.name,
      filePath: f.path,
      summary: `${cl.name} in ${posix.basename(f.path)} — ${r.summary}`,
      tags: [...r.tags, 'class'],
      startLine: cl.startLine,
      endLine: cl.endLine,
    });
    addEdge(id, cid, 'contains', 1.0);
  }
}

// --- 5. Import edges, dropped when either endpoint was excluded -------------
const idFor = new Map(kept.map((f) => [f.path, `${nodeType(f)}:${f.path}`]));
let importEdges = 0;
for (const [from, targets] of Object.entries(importMap)) {
  const s = idFor.get(from);
  if (!s) continue;
  for (const to of targets) {
    const t = idFor.get(to);
    if (!t || t === s) continue;
    addEdge(s, t, 'imports', 0.7);
    importEdges++;
  }
}

// Deduplicate edges by (source, target, type) and drop dangling references.
const nodeIds = new Set(nodes.map((n) => n.id));
const seenEdge = new Set();
const finalEdges = edges.filter((e) => {
  const k = `${e.source}|${e.target}|${e.type}`;
  if (seenEdge.has(k) || !nodeIds.has(e.source) || !nodeIds.has(e.target)) return false;
  seenEdge.add(k);
  return true;
});

// --- 6. Layers and tour ----------------------------------------------------
const layers = overlay.layers
  .map((l) => ({ id: l.id, name: l.name, description: l.description, nodeIds: layerMembers.get(l.id) ?? [] }))
  .filter((l) => l.nodeIds.length > 0);

const tour = overlay.tour.map((step, i) => ({
  order: i + 1,
  title: step.title,
  description: step.description,
  nodeIds: step.paths.map((p) => idFor.get(p)).filter(Boolean),
}));

const graph = {
  version: '1.0.0',
  project: {
    name: 'ForgeOps',
    languages: Object.keys(scan.stats.byLanguage),
    frameworks: overlay.frameworks,
    description: overlay.description,
    analyzedAt: new Date().toISOString(),
    gitCommitHash: 'not-recorded',
  },
  nodes,
  edges: finalEdges,
  layers,
  tour,
};

// --- 7. The plugin's own inline deterministic validation -------------------
const issues = [];
const warnings = [];
const fileLevel = new Set(['file', 'config', 'document', 'service', 'pipeline', 'table', 'schema', 'resource', 'endpoint']);
const assigned = new Map();
for (const l of layers) {
  for (const id of l.nodeIds) {
    if (!nodeIds.has(id)) issues.push(`layer ${l.id} refs missing node ${id}`);
    if (assigned.has(id)) issues.push(`node ${id} appears in multiple layers`);
    assigned.set(id, l.id);
  }
}
for (const n of nodes) {
  if (!n.type || !n.name || !n.summary || !n.tags?.length) issues.push(`node ${n.id} missing a required field`);
  if (fileLevel.has(n.type) && !assigned.has(n.id)) issues.push(`file node ${n.id} not in any layer`);
}
for (const [i, s] of tour.entries()) {
  if (!s.nodeIds.length) issues.push(`tour step ${i + 1} resolves to no nodes`);
}
const withEdges = new Set(finalEdges.flatMap((e) => [e.source, e.target]));
for (const n of nodes) if (!withEdges.has(n.id)) warnings.push(`orphan node ${n.id}`);

writeFileSync(join(INTER, 'review.json'), JSON.stringify({ issues, warnings, stats: { totalNodes: nodes.length, totalEdges: finalEdges.length, totalLayers: layers.length, tourSteps: tour.length, importEdges } }, null, 2));
if (issues.length) {
  for (const i of issues.slice(0, 25)) process.stderr.write(`build-graph: ISSUE ${i}\n`);
  die(`${issues.length} validation issues; knowledge-graph.json not written`);
}

writeFileSync(join(UA, 'knowledge-graph.json'), JSON.stringify(graph, null, 2));
writeFileSync(
  join(UA, 'meta.json'),
  JSON.stringify({ lastAnalyzedAt: graph.project.analyzedAt, version: '1.0.0', analyzedFiles: kept.length, toolVersion: 'understand-anything v2.9.0' }, null, 2),
);
process.stderr.write(
  `build-graph: OK — ${nodes.length} nodes, ${finalEdges.length} edges (${importEdges} imports), ${layers.length} layers, ${tour.length} tour steps, ${warnings.length} orphan warnings\n`,
);
