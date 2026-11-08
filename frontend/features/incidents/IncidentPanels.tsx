"use client";

/**
 * Incidents, root-cause analysis and fix previews. Phase 2 §2.11.
 *
 * THE PANEL MUST NOT PRESENT A CONCLUSION MORE CONFIDENTLY THAN THE PIPELINE REACHED IT. An RCA is something
 * an operator acts on -- they restart a service, roll back a release, spend an outage reading one set of
 * logs -- so the five analysis states get five different renderings, and only one of them shows a cause.
 *
 * The specific lie this is written to prevent: an analysis with state `insufficient_evidence` or
 * `unavailable` has empty problem/location/fix fields. Rendering those fields unconditionally would draw
 * three empty rows under a heading reading "Root cause", which a human reads as "analysed, nothing found".
 * So the STATE is rendered, and the fields only appear when the state is `analysed`.
 *
 * THE EVIDENCE TABLE SHOWS UNREACHABLE SOURCES AS PROMINENTLY AS REACHABLE ONES, for the same reason the
 * backend records them: a conclusion drawn from two of five sources is not wrong but is differently
 * trustworthy, and hiding the three would make the two look like the whole picture.
 */

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { api } from "@/lib/api/client";

export type AnalysisState =
  "not_analysed" | "unavailable" | "insufficient_evidence" | "analysed" | "inconclusive";

export interface IncidentListRow {
  id: string;
  source: string;
  severity: "info" | "warning" | "critical";
  title: string;
  occurrences: number;
  detected_at: string;
  last_seen_at: string;
  resolved_at: string | null;
  origin_kind: string;
  origin_id: string;
  analysis_state: AnalysisState;
  evidence_reachable: number | null;
  evidence_consulted: number | null;
}

export interface EvidenceRow {
  kind: string;
  reachable: boolean;
  summary: string;
  detail: Record<string, unknown>;
  collected_at: string;
}

export interface AnalysisRow {
  id: string;
  state: AnalysisState;
  problem: string;
  location: string;
  fix: string;
  evidence_reachable: number;
  evidence_consulted: number;
  model: string;
  served_from: string;
  created_at: string;
}

export interface SuggestionRow {
  id: string;
  analysis_id: string;
  path: string;
  rationale: string;
  change_set_id: string | null;
  created_at: string;
  proposed_bytes: number;
}

/**
 * One sentence per state. THE WHOLE CONTRACT OF THIS FILE.
 *
 * Note that none of the four non-`analysed` sentences contains a cause, a component, or anything an operator
 * could mistake for a diagnosis. `not_analysed` is deliberately not phrased as a failure: an incident with no
 * analysis is the normal state on a deployment with no model configured.
 */
const STATE_SENTENCE: Record<AnalysisState, string> = {
  not_analysed:
    "This incident has not been analysed. Nothing has been concluded about it, which is different from nothing being wrong.",
  unavailable:
    "Analysis was attempted and no model could be reached, so the evidence below was collected but not interpreted. What is missing is the inference, not the data.",
  insufficient_evidence:
    "Too few evidence sources could be read to identify a cause. One source can show that something broke but not what broke it, so no cause has been inferred.",
  inconclusive:
    "The evidence was read and does not support identifying a cause. This is a real answer rather than a failure: read the evidence below directly.",
  analysed: "A cause was identified from the evidence below.",
};

const STATE_LABEL: Record<AnalysisState, string> = {
  not_analysed: "Not analysed",
  unavailable: "No model available",
  insufficient_evidence: "Not enough evidence",
  inconclusive: "Inconclusive",
  analysed: "Cause identified",
};

export function IncidentList({
  projectId,
  onSelect,
}: {
  projectId: string;
  onSelect?: (incidentId: string) => void;
}) {
  const [includeResolved, setIncludeResolved] = useState(false);
  const query = useQuery<{ incidents: IncidentListRow[]; explanation: string }>({
    queryKey: ["incidents", projectId, includeResolved],
    queryFn: async () =>
      api.get(`/projects/${projectId}/incidents?include_resolved=${includeResolved}`),
    refetchInterval: 30_000,
  });

  return (
    <section aria-labelledby="incidents-heading" data-testid="incident-list">
      <h2 id="incidents-heading">Incidents</h2>

      <label>
        <input
          type="checkbox"
          checked={includeResolved}
          onChange={(event) => setIncludeResolved(event.target.checked)}
        />{" "}
        Include resolved
      </label>

      {query.isLoading ? (
        <p role="status">Reading incidents.</p>
      ) : query.error ? (
        <p role="alert" data-testid="incident-list-error">
          The incident list could not be loaded, so it is not known whether anything has failed. An
          empty list is not being shown in its place.
        </p>
      ) : query.data && query.data.incidents.length > 0 ? (
        <table>
          <caption>
            Most recently seen first. The analysis column says how far diagnosis got, so a row does
            not have to be opened to find out.
          </caption>
          <thead>
            <tr>
              <th scope="col">Severity</th>
              <th scope="col">What happened</th>
              <th scope="col">Seen</th>
              <th scope="col">Analysis</th>
            </tr>
          </thead>
          <tbody>
            {query.data.incidents.map((incident) => (
              <tr key={incident.id} data-testid={`incident-row-${incident.id}`}>
                <td>{incident.severity}</td>
                <td>
                  {onSelect ? (
                    <button type="button" onClick={() => onSelect(incident.id)}>
                      {incident.title}
                    </button>
                  ) : (
                    incident.title
                  )}
                  <span> ({incident.source.replace(/_/g, " ")})</span>
                </td>
                <td data-testid={`incident-occurrences-${incident.id}`}>
                  {/* "47 times" rather than 47 rows. A count of one is stated as once, so the column never
                      reads as a bare number whose unit a reader has to infer. */}
                  {incident.occurrences === 1 ? "once" : `${incident.occurrences} times`}
                </td>
                <td data-testid={`incident-analysis-${incident.id}`}>
                  {STATE_LABEL[incident.analysis_state]}
                  {incident.evidence_consulted ? (
                    <span>
                      {" "}
                      ({incident.evidence_reachable} of {incident.evidence_consulted} sources)
                    </span>
                  ) : null}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        // Not an empty table. The backend's own sentence is rendered, which says that no incident has been
        // INGESTED -- not that nothing has failed.
        <p data-testid="incident-list-empty">{query.data?.explanation}</p>
      )}
    </section>
  );
}

export function IncidentDetail({ incidentId }: { incidentId: string }) {
  const query = useQuery<{
    incident: IncidentListRow & { detail: Record<string, unknown>; fingerprint: string };
    evidence: EvidenceRow[];
    analyses: AnalysisRow[];
    suggestions: SuggestionRow[];
    evidence_caveat: string;
  }>({
    queryKey: ["incident", incidentId],
    queryFn: async () => api.get(`/incidents/${incidentId}`),
  });

  if (query.isLoading) return <p role="status">Reading the incident.</p>;
  if (query.error)
    return (
      <p role="alert" data-testid="incident-detail-error">
        This incident could not be loaded.
      </p>
    );
  if (!query.data) return null;

  const { incident, evidence, analyses, suggestions, evidence_caveat } = query.data;
  const latest = analyses[0];

  return (
    <article aria-labelledby="incident-detail-heading" data-testid="incident-detail">
      <h2 id="incident-detail-heading">{incident.title}</h2>
      <p>
        {incident.severity} &middot; observed by {incident.source.replace(/_/g, " ")} &middot;{" "}
        {incident.occurrences === 1 ? "seen once" : `seen ${incident.occurrences} times`}
      </p>

      {/* THE CAVEAT COMES FIRST, above any conclusion. An operator reading top to bottom cannot reach the
          cause without passing how much of the picture was available. */}
      <p
        data-testid="incident-evidence-caveat"
        role={evidence.some((record) => !record.reachable) ? "alert" : "status"}
      >
        {evidence_caveat}
      </p>

      <section aria-labelledby="rca-heading" data-testid="incident-rca">
        <h3 id="rca-heading">Root-cause analysis</h3>
        {latest === undefined ? (
          <p data-testid="incident-rca-none">{STATE_SENTENCE.not_analysed}</p>
        ) : (
          <>
            <p data-testid="incident-rca-state">
              <strong>{STATE_LABEL[latest.state]}.</strong> {STATE_SENTENCE[latest.state]}
            </p>
            {latest.state === "analysed" ? (
              // The three parts §2.11 asks for, rendered ONLY in this state. In any other state these
              // fields are empty, and three empty rows under "Root cause" read as "analysed, nothing found".
              <dl data-testid="incident-rca-content">
                <dt>Problem</dt>
                <dd>{latest.problem}</dd>
                <dt>Location</dt>
                <dd>{latest.location}</dd>
                <dt>Suggested direction</dt>
                <dd>
                  {latest.fix ? (
                    latest.fix
                  ) : (
                    // A cause can be identified without a remedy being obvious, and saying so is better
                    // than a blank that reads as "no fix needed".
                    <span data-testid="incident-rca-no-fix">
                      No remedy was proposed. The cause above was identified; what to do about it
                      was not.
                    </span>
                  )}
                </dd>
              </dl>
            ) : null}
            <p data-testid="incident-rca-provenance">
              {latest.evidence_reachable} of {latest.evidence_consulted} evidence sources were
              readable.{" "}
              {latest.model
                ? `Answered by ${latest.model}${latest.served_from ? ` (${latest.served_from})` : ""}.`
                : "No model answered, so nothing here was inferred by one."}
            </p>
          </>
        )}
      </section>

      <section aria-labelledby="evidence-heading" data-testid="incident-evidence">
        <h3 id="evidence-heading">Evidence consulted</h3>
        {evidence.length === 0 ? (
          <p data-testid="incident-evidence-none">
            No evidence has been collected. The incident itself was observed and recorded.
          </p>
        ) : (
          <table>
            <caption>
              Every source consulted, including the ones that could not be read. A source that did
              not answer is listed rather than omitted, because an omitted source is
              indistinguishable from one nobody thought to check.
            </caption>
            <thead>
              <tr>
                <th scope="col">Source</th>
                <th scope="col">Answered</th>
                <th scope="col">What it said</th>
              </tr>
            </thead>
            <tbody>
              {evidence.map((record, index) => (
                <tr
                  key={`${record.kind}-${index}`}
                  data-testid={`evidence-${record.kind}-${index}`}
                >
                  <td>{record.kind}</td>
                  <td>{record.reachable ? "yes" : "no"}</td>
                  <td>{record.summary}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <section aria-labelledby="suggestions-heading" data-testid="incident-suggestions">
        <h3 id="suggestions-heading">Suggested fixes</h3>
        {suggestions.length === 0 ? (
          <p data-testid="incident-suggestions-none">
            No fix has been suggested for this incident.
          </p>
        ) : (
          <ul>
            {suggestions.map((suggestion) => (
              <li key={suggestion.id} data-testid={`suggestion-${suggestion.id}`}>
                <code>{suggestion.path}</code>
                {suggestion.change_set_id ? (
                  <span data-testid={`suggestion-submitted-${suggestion.id}`}>
                    {" "}
                    &mdash; already a change set, awaiting the same approval as any other mutation
                  </span>
                ) : (
                  <span> &mdash; proposed, not applied</span>
                )}
                <SuggestionDiff incidentId={incidentId} suggestionId={suggestion.id} />
              </li>
            ))}
          </ul>
        )}
      </section>
    </article>
  );
}

export function SuggestionDiff({
  incidentId,
  suggestionId,
}: {
  incidentId: string;
  suggestionId: string;
}) {
  const query = useQuery<{
    path: string;
    rationale: string;
    diff: string[];
    already_submitted: boolean;
    change_set_id: string | null;
    caveat: string;
  }>({
    queryKey: ["incident", incidentId, "suggestion", suggestionId],
    queryFn: async () => api.get(`/incidents/${incidentId}/suggestions/${suggestionId}/preview`),
  });

  if (query.isLoading) return <p role="status">Rendering the diff.</p>;
  if (query.error)
    return (
      <p role="alert" data-testid={`diff-error-${suggestionId}`}>
        This diff could not be rendered, so what the suggestion would change is not being shown. It
        has not been applied.
      </p>
    );
  if (!query.data) return null;

  return (
    <div data-testid={`diff-${suggestionId}`}>
      {query.data.rationale ? <p>{query.data.rationale}</p> : null}

      {/* THE CAVEAT IS ABOVE THE DIFF, not below it. It says the comparison is against what the file held
          when the analysis ran, which is the one thing a reader would otherwise assume wrongly. */}
      <p data-testid={`diff-caveat-${suggestionId}`}>{query.data.caveat}</p>

      {query.data.diff.length === 0 ? (
        <p data-testid={`diff-empty-${suggestionId}`}>
          The proposal is identical to what the file held when the analysis ran, so there is nothing
          to change. This is not an empty diff to approve: there is no difference.
        </p>
      ) : (
        <pre>
          <code>{query.data.diff.join("\n")}</code>
        </pre>
      )}
    </div>
  );
}
