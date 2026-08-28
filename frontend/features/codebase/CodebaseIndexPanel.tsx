// SPDX-License-Identifier: FSL-1.1-ALv2
"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, queryKeys } from "@/lib/api";
import { AsyncState } from "@/components/ui/async-state";
import type { ChunkDetail, CodebaseStatus, SymbolResult } from "@/features/projects/types";

/**
 * Has this project ever been scanned, and what is in the index?
 *
 * THE MOST CONFUSING THING ABOUT THE PRODUCT, until now. Three routes served this and none had a
 * caller: `/codebase/{id}/status`, `/codebase/{id}/symbols` and `/codebase/{id}/chunks/{chunk_id}`.
 * With no screen reading them there was no way for a user to learn whether a project had been
 * indexed — so a readiness score of zero, an empty retrieval result and a generation with no context
 * all looked like bugs, when the answer in every case was "nobody has scanned it".
 *
 * All three of these endpoints previously returned LITERALS — `indexed_files=42`, a `NewParser`
 * symbol at a fixed line, a chunk body of `"func NewParser() ..."` for any chunk id. They are real
 * queries now, and this component is what makes the real answers visible.
 */

/**
 * What each index state means, and what to do about it.
 *
 * `indexed_without_vectors` is the one worth spelling out: it is the honest outcome when no embedding
 * provider is configured, and it means retrieval is sparse-only rather than broken. A UI that showed
 * it as "indexed" would leave someone puzzled by weak retrieval; one that showed it as "failed" would
 * be wrong, because the tree, the contents and the dependency graph are all there.
 */
const STATUS_MEANING: Record<CodebaseStatus["status"], { headline: string; detail: string }> = {
  empty: {
    headline: "Never scanned",
    detail:
      "No files, no chunks, no dependency edges. Readiness cannot be scored, retrieval has nothing to " +
      "search, and generation will run without context from your codebase. Run a scan.",
  },
  indexed_without_vectors: {
    headline: "Indexed, without vectors",
    detail:
      "The file tree, the redacted contents and the dependency graph are stored, but no embeddings " +
      "were written — which is what an unconfigured or unreachable embedding provider honestly looks " +
      "like. Readiness scores normally; retrieval is sparse-only (BM25) rather than hybrid, and symbol " +
      "search is empty because the symbol metadata lives on the embedding rows.",
  },
  indexed: {
    headline: "Indexed",
    detail:
      "Tree, contents, dependency graph and vectors are all present. Hybrid retrieval is available.",
  },
};

export function CodebaseIndexPanel({
  projectId,
  projectPath,
}: {
  projectId: string;
  /** Shown in the scan command, so the operator can copy the exact invocation. */
  projectPath: string;
}) {
  const status = useQuery({
    queryKey: queryKeys.codebase.status(projectId),
    queryFn: () => api.get<CodebaseStatus>(`/analysis/codebase/${projectId}/status`),
    enabled: projectId !== "",
    retry: false,
  });

  return (
    <div className="space-y-4">
      <AsyncState isPending={status.isPending} error={status.error} label="index status">
        {status.data ? (
          <div className="rounded-lg border border-border bg-background p-4 text-sm">
            <p className="font-semibold" data-testid="index-headline">
              {STATUS_MEANING[status.data.status].headline}
            </p>
            <p className="mt-1 text-muted-foreground">
              {STATUS_MEANING[status.data.status].detail}
            </p>

            <dl className="mt-3 grid grid-cols-2 gap-x-6 gap-y-1 text-xs sm:grid-cols-3">
              <Fact label="Files" value={String(status.data.indexed_files)} />
              <Fact label="Chunks" value={String(status.data.total_chunks)} />
              <Fact label="Bytes" value={status.data.total_bytes.toLocaleString()} />
              <Fact
                label="Dependencies resolved"
                value={`${status.data.resolved_dependencies} of ${
                  status.data.resolved_dependencies + status.data.unresolved_dependencies
                }`}
              />
              <Fact
                label="Languages"
                value={
                  status.data.languages.length === 0
                    ? "none detected"
                    : status.data.languages.join(", ")
                }
              />
              <Fact label="Last indexed" value={status.data.last_indexed_at ?? "never"} />
            </dl>

            {status.data.unresolved_dependencies > 0 ? (
              <p className="mt-3 text-xs text-muted-foreground">
                Unresolved dependencies are imports the graph builder could not point at a file in
                this project — third-party packages, generated code, or a language whose resolver is
                partial. They are recorded rather than dropped, so the count is a real measure of
                how complete cross-file retrieval can be.
              </p>
            ) : null}
          </div>
        ) : null}
      </AsyncState>

      <ScanInstructions projectId={projectId} projectPath={projectPath} />

      {status.data && status.data.total_chunks > 0 ? <SymbolSearch projectId={projectId} /> : null}
    </div>
  );
}

function Fact({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-col">
      <dt className="font-medium">{label}</dt>
      <dd className="text-muted-foreground">{value}</dd>
    </div>
  );
}

/**
 * How to scan — the exact command, and why there is no button.
 *
 * §2.2.1 confines `send_command` to `src/governance/`, so the only way the backend can tell an agent
 * to do anything is by minting a governed command envelope through the chokepoint. A "Scan now"
 * button would therefore need either an architecture violation or a new governed operation whose
 * approval semantics nobody has designed — and a scan is not a mutation of the user's tree, so
 * routing it through an approval gate would be governance theatre.
 *
 * The agent already owns its workspace and self-triggers: `forgeops-agent watch` runs fsnotify with a
 * real debounce and submits an incremental re-index on every edit. So the honest UI is not a button
 * that fakes a trigger; it is the command, exactly, and the observation that `watch` makes scanning
 * continuous. The one thing this screen CAN show — whether the index is populated and when it last
 * changed — is above, which is what the user actually wanted when they went looking for the button.
 */
function ScanInstructions({ projectId, projectPath }: { projectId: string; projectPath: string }) {
  const [copied, setCopied] = useState(false);
  /*
   * The agent's REAL flags. `scan` takes `--project` and nothing else.
   *
   * Worth a note because the first version of this panel rendered
   * `scan --project <id> --path <path>`, which reads perfectly reasonably and is not a command that
   * exists: `fatal: unknown flag: --path`. It was caught by `e2e/onboarding.spec.ts` executing the
   * string THIS COMPONENT DISPLAYS, verbatim, rather than an invocation the test knew independently.
   * A UI that hands somebody a command which does not parse is worse than one that hands them
   * nothing, and the only way to keep such a string honest is for something to actually run it.
   */
  const command = `forgeops-agent scan --project ${projectId}`;
  const watchCommand = `forgeops-agent watch --project ${projectId}`;

  return (
    <div className="rounded-lg border border-border bg-muted/30 p-4 text-sm">
      <p className="font-semibold">Scanning is agent-initiated, and there is no button here.</p>
      <p className="mt-2 text-xs text-muted-foreground">
        The backend cannot tell an agent to scan. §2.2.1 confines command dispatch to the governance
        chokepoint, so anything the backend sends an agent is a signed, policy-evaluated,
        audit-recorded envelope — and a scan reads your tree rather than mutating it, so putting it
        through an approval gate would add ceremony without adding a control. The agent owns its own
        workspace and triggers its own re-indexing.
      </p>
      <p className="mt-3 text-xs font-medium">
        Run this on the machine whose agent is paired to this project:
      </p>
      <pre className="mt-1 overflow-x-auto rounded bg-background p-2 text-xs">
        <code data-testid="scan-command">{command}</code>
      </pre>
      <p className="mt-2 text-xs text-muted-foreground">
        There is no path argument, and that is not an omission. The agent indexes{" "}
        <strong>the workspace it was configured with</strong> — it is the only party that can read
        those files — so the directory is a property of the agent rather than of this command. This
        project records <code className="break-all">{projectPath}</code>; if the agent&apos;s
        workspace is somewhere else the scan will succeed and index the wrong tree, and the file count
        above is how you would notice.
      </p>
      <p className="mt-2 text-xs text-muted-foreground">
        Or leave <code>{watchCommand}</code> running: it watches with fsnotify, debounces, and
        re-indexes a changed file together with the files that import it, so the index stays current
        without anyone running anything.
      </p>
      <button
        type="button"
        onClick={() => {
          // `navigator.clipboard` is absent in a non-secure context and in the jsdom used by the unit
          // tests, so its absence is handled rather than assumed. A copy button that throws is worse
          // than one that says it could not copy.
          void navigator.clipboard
            ?.writeText(command)
            .then(() => setCopied(true))
            .catch(() => setCopied(false));
        }}
        className="mt-3 rounded-md border border-border px-3 py-1.5 text-xs focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        Copy scan command
      </button>
      {copied ? (
        <span role="status" className="ml-2 text-xs text-muted-foreground">
          Copied.
        </span>
      ) : null}
    </div>
  );
}

/**
 * Substring symbol search, and one stored chunk.
 *
 * Rendered only when the index has chunks, because the symbol metadata lives on the embedding rows:
 * a project indexed without vectors would return an empty list, which is the correct answer and is
 * indistinguishable from "this project has no functions". Not offering the box is more honest than
 * offering one that can only ever come back empty.
 */
function SymbolSearch({ projectId }: { projectId: string }) {
  const [draft, setDraft] = useState("");
  const [query, setQuery] = useState("");
  const [openChunk, setOpenChunk] = useState<string | null>(null);

  const symbols = useQuery({
    queryKey: queryKeys.codebase.symbols(projectId, query),
    queryFn: () =>
      api.get<SymbolResult[]>(
        `/analysis/codebase/${projectId}/symbols?query=${encodeURIComponent(query)}&limit=50`,
      ),
    enabled: query !== "",
    retry: false,
  });

  const chunk = useQuery({
    queryKey: queryKeys.codebase.chunk(projectId, openChunk ?? ""),
    queryFn: () => api.get<ChunkDetail>(`/analysis/codebase/${projectId}/chunks/${openChunk}`),
    enabled: openChunk !== null,
    retry: false,
  });

  return (
    <section aria-labelledby="symbols-heading" className="space-y-3">
      <h3 id="symbols-heading" className="text-sm font-semibold">
        Search indexed symbols
      </h3>
      <form
        className="flex flex-wrap items-end gap-3"
        onSubmit={(event) => {
          event.preventDefault();
          setQuery(draft.trim());
          setOpenChunk(null);
        }}
      >
        <div className="min-w-[14rem] flex-1">
          <label htmlFor="symbol-query" className="block text-sm font-medium">
            Symbol name contains
          </label>
          <input
            id="symbol-query"
            type="search"
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            className="mt-1 w-full rounded-md border border-border bg-background px-3 py-2 font-mono text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          />
        </div>
        <button
          type="submit"
          className="rounded-md border border-border px-3 py-2 text-sm font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          Search
        </button>
      </form>

      {query === "" ? (
        <p className="text-xs text-muted-foreground">
          Matches are substrings, case-insensitively, over the declarations the scanner recorded. A{" "}
          <code>%</code> is a literal percent sign here rather than a wildcard.
        </p>
      ) : (
        <AsyncState
          isPending={symbols.isPending}
          error={symbols.error}
          isEmpty={symbols.data?.length === 0}
          emptyMessage="No indexed symbol contains that. The index holds only what the last scan recorded."
          label="symbols"
        >
          <ul className="divide-y divide-border rounded-lg border border-border bg-background">
            {symbols.data?.map((symbol) => (
              <li key={symbol.chunk_id} className="p-3 text-sm">
                <div className="flex flex-wrap items-baseline justify-between gap-2">
                  <p className="font-mono font-medium">{symbol.name}</p>
                  <span className="text-xs text-muted-foreground">{symbol.kind}</span>
                </div>
                <p className="mt-0.5 font-mono text-xs text-muted-foreground">
                  {symbol.file_path}:{symbol.line_number}
                  {symbol.parent_symbol ? ` · in ${symbol.parent_symbol}` : ""}
                </p>
                {symbol.signature ? (
                  <p className="mt-1 font-mono text-xs">{symbol.signature}</p>
                ) : null}
                <button
                  type="button"
                  aria-expanded={openChunk === symbol.chunk_id}
                  onClick={() =>
                    setOpenChunk((current) =>
                      current === symbol.chunk_id ? null : symbol.chunk_id,
                    )
                  }
                  data-testid={`chunk-toggle-${symbol.chunk_id}`}
                  className="mt-2 rounded-md border border-border px-2 py-1 text-xs focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                >
                  {openChunk === symbol.chunk_id ? "Hide stored chunk" : "Show stored chunk"}
                </button>

                {openChunk === symbol.chunk_id ? (
                  <AsyncState isPending={chunk.isPending} error={chunk.error} label="chunk">
                    {chunk.data ? (
                      <div className="mt-2">
                        <p className="text-xs text-muted-foreground">
                          Lines {chunk.data.start_line}–{chunk.data.end_line} of{" "}
                          {chunk.data.file_path}
                          {chunk.data.language ? ` · ${chunk.data.language}` : ""}
                          {chunk.data.token_count === null
                            ? ""
                            : ` · ${chunk.data.token_count} tokens`}{" "}
                          · embedded by <code>{chunk.data.model_id}</code>
                        </p>
                        <pre className="mt-1 max-h-64 overflow-auto rounded bg-muted p-2 text-xs">
                          <code>{chunk.data.content}</code>
                        </pre>
                        <p className="mt-1 text-xs text-muted-foreground">
                          This is the <strong>redacted</strong> text that was stored. The agent
                          redacts before transmitting, so there is no unredacted copy here to show.
                        </p>
                      </div>
                    ) : null}
                  </AsyncState>
                ) : null}
              </li>
            ))}
          </ul>
        </AsyncState>
      )}
    </section>
  );
}
