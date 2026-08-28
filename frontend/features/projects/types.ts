// SPDX-License-Identifier: FSL-1.1-ALv2

/**
 * The project wire shapes, in one place.
 *
 * One module rather than a copy per screen. The list, the detail page, the picker and the onboarding
 * path all read the same `ProjectResponse`, and four hand-maintained copies of an interface is four
 * places for a field to be missed — which is how `readinessScore` ended up being invented by the
 * list screen instead of read from anywhere.
 */

/** Mirrors `ProjectResponse` in `backend/src/projects/routes.py`. */
export interface ProjectResponse {
  id: string;
  name: string;
  path: string;
  repo_url: string | null;
  settings: Record<string, unknown>;
  created_at: string | null;
  updated_at: string | null;
  /** NULL while active. A soft, reversible archive (PRD FR-05, revision `0013`). */
  archived_at: string | null;
  /** PRD FR-02. From `project_tags`. */
  tags: string[];
  /** PRD FR-03, and it is THIS caller's favourite — see `models.ProjectFavourite`. */
  favourite: boolean;
  /**
   * Files this project has in the codebase index.
   *
   * THE FIELD THAT REPLACES A LIE. The projects screen mapped every project to
   * `readinessScore: 0`, a hardcoded literal, so every project displayed a zero regardless of its
   * real score. Zero here means "never scanned", which is a fact the server actually knows, and the
   * UI renders it as words rather than as a number that looks like a measurement.
   */
  indexed_file_count: number;
}

/** Mirrors `ProjectPage`: a keyset page plus the cursor that fetches the next one. */
export interface ProjectPage {
  projects: ProjectResponse[];
  next_cursor: string | null;
}

/** Mirrors `ActivityFeedItem`. Read from the append-only `audit_events` table. */
export interface ActivityFeedItem {
  id: string;
  action: string;
  timestamp: string;
  details: string;
}

/** Mirrors `DeletionReport` — what a delete actually removed, counted before the statement ran. */
export interface DeletionReport {
  project_id: string;
  cascaded: Record<string, number>;
  audit_events_retained: number;
}

/** Mirrors `ReadinessCheckResponse`. `why_it_matters` is PRD FR-19's requirement, on the wire. */
export interface ReadinessCheck {
  id: string;
  category: string;
  passed: boolean;
  points: number;
  max_points: number;
  evidence: string;
  why_it_matters: string;
}

/** Mirrors `ReadinessReportResponse`. */
export interface ReadinessReport {
  project_id: string;
  score: number;
  level: string;
  summary_report: string;
  recommendations: string[];
  /** §1.4's six weighted categories, each 0-100, serialised from the engine's own model. */
  categories: Record<string, number>;
  /** False when the project has no indexed files. "Scored zero" and "never scanned" are different. */
  indexed: boolean;
  evaluated_paths: number;
  checks: ReadinessCheck[];
}

/** Mirrors `CodebaseStatusResponse` in `backend/src/analysis/routes.py`. */
export interface CodebaseStatus {
  indexed_files: number;
  total_chunks: number;
  languages: string[];
  /**
   * `empty` — nothing indexed. `indexed_without_vectors` — tree and contents stored but no
   * embeddings, which is what an unavailable embedding provider honestly looks like and means
   * retrieval is sparse-only. `indexed` — both.
   */
  status: "empty" | "indexed_without_vectors" | "indexed";
  total_bytes: number;
  resolved_dependencies: number;
  unresolved_dependencies: number;
  last_indexed_at: string | null;
}

/** Mirrors `SymbolQueryResponse`. */
export interface SymbolResult {
  name: string;
  kind: string;
  file_path: string;
  line_number: number;
  parent_symbol: string | null;
  signature: string | null;
  chunk_id: string;
}

/** Mirrors `ChunkDetailResponse`. The content is the REDACTED text that was stored. */
export interface ChunkDetail {
  chunk_id: string;
  file_path: string;
  content: string;
  start_line: number;
  end_line: number;
  language: string;
  symbol: string | null;
  parent_symbol: string | null;
  kind: string | null;
  token_count: number | null;
  model_id: string;
}

/** Turn `containerization_score` into `Containerization`, without inventing categories. */
export function categoryLabel(key: string): string {
  return key
    .replace(/_score$/, "")
    .split("_")
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(" ");
}

/**
 * How a project's index state should be described to a person.
 *
 * Centralised because three screens say it — the list, the detail page and the onboarding path — and
 * three copies would drift into three different claims about the same number.
 */
export function indexSummary(indexedFileCount: number): string {
  return indexedFileCount === 0
    ? "Not scanned — no readiness score can be computed yet"
    : `${indexedFileCount} file${indexedFileCount === 1 ? "" : "s"} indexed`;
}
