"use client";

/**
 * Learning history and preference display. Phase 2 §2.13.
 *
 * THIS SCREEN IS THE CORRECTION MECHANISM, not a read-only report. A preference store shapes every future
 * prompt, so one the user can see but not change is only marginally better than one they cannot see: they
 * learn that the system believes something wrong and can do nothing about it. So every preference carries an
 * edit, a switch and a delete, and the three do different things which the copy states:
 *
 *   edit      corrects the text AND promotes it to "stated", so the next reflection cannot undo the fix
 *   switch    stops it being injected while keeping the record that it was inferred
 *   delete    removes it, leaving the feedback it came from intact
 *
 * WHAT MAKES THE DISPLAY HONEST rather than flattering:
 *
 *   - the evidence count is shown, because "derived from one edit" and "derived from forty" deserve
 *     different confidence and a bare sentence cannot express that
 *   - INACTIVE preferences are listed, not hidden: a user who switched one off needs to see it is still
 *     there and still off, or the next reflection finding it again looks like new learning
 *   - EXCLUDED preferences are shown on the skill-file panel, because a preference that never reaches a
 *     prompt is indistinguishable from one that does not exist judging by output alone, and "why did it
 *     ignore my preference" is exactly the question this answers
 *   - an empty store says the system has been told nothing, which is different from having been told and
 *     ignoring it
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api } from "@/lib/api/client";

export type PreferenceSource = "reflected" | "stated";

export interface PreferenceRow {
  id: string;
  scope: string;
  statement: string;
  source: PreferenceSource;
  evidence_count: number;
  active: boolean;
  created_at: string;
  updated_at: string;
}

export interface FeedbackRow {
  id: string;
  generation_run_id: string | null;
  artifact_path: string;
  verdict: "accepted" | "rejected" | "edited";
  comment: string;
  created_at: string;
  final_content_bytes: number;
}

export interface InjectionRow {
  id: string;
  content: string;
  included_preference_ids: string[];
  excluded_preference_ids: string[];
  created_at: string;
}

/** Where a preference came from, in words. A badge would not survive a screen reader. */
function sourceSentence(row: PreferenceRow): string {
  if (row.source === "stated") {
    return row.evidence_count > 0
      ? `You stated this. ${row.evidence_count} feedback event(s) have since agreed with it.`
      : "You stated this. Reflection can strengthen it but will not rewrite it.";
  }
  return row.evidence_count === 1
    ? "Inferred from a single feedback event, so treat it as a guess."
    : `Inferred from ${row.evidence_count} feedback events.`;
}

export function PreferenceDisplay({ projectId }: { projectId: string }) {
  const client = useQueryClient();
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState("");

  const query = useQuery<{ preferences: PreferenceRow[]; scopes: string[]; explanation: string }>({
    queryKey: ["learning", "preferences", projectId],
    queryFn: async () => api.get(`/projects/${projectId}/learning/preferences`),
  });

  const correct = useMutation({
    mutationFn: async (variables: { id: string; statement?: string; active?: boolean }) =>
      api.patch(`/learning/preferences/${variables.id}`, {
        ...(variables.statement !== undefined ? { statement: variables.statement } : {}),
        ...(variables.active !== undefined ? { active: variables.active } : {}),
      }),
    onSuccess: async () => {
      setEditing(null);
      await client.invalidateQueries({ queryKey: ["learning"] });
    },
  });

  const forget = useMutation({
    mutationFn: async (id: string) => api.delete(`/learning/preferences/${id}`),
    onSuccess: async () => client.invalidateQueries({ queryKey: ["learning"] }),
  });

  return (
    <section aria-labelledby="preferences-heading" data-testid="preference-display">
      <h2 id="preferences-heading">What this system has learned about this project</h2>

      {query.isLoading ? (
        <p role="status">Reading preferences.</p>
      ) : query.error ? (
        <p role="alert" data-testid="preferences-error">
          The preference list could not be loaded, so it is not known what this system believes
          about this project. An empty list is not being shown in its place.
        </p>
      ) : (
        <>
          <p data-testid="preferences-explanation">{query.data?.explanation}</p>

          {query.data && query.data.preferences.length > 0 ? (
            <ul>
              {query.data.preferences.map((row) => (
                <li key={row.id} data-testid={`preference-${row.id}`}>
                  {editing === row.id ? (
                    <form
                      onSubmit={(event) => {
                        event.preventDefault();
                        correct.mutate({ id: row.id, statement: draft });
                      }}
                    >
                      <label>
                        Correct this preference
                        <textarea
                          value={draft}
                          onChange={(event) => setDraft(event.target.value)}
                          data-testid={`preference-draft-${row.id}`}
                        />
                      </label>
                      <button type="submit">Save</button>
                      <button type="button" onClick={() => setEditing(null)}>
                        Cancel
                      </button>
                      <p>
                        Saving marks this as stated by you, so the next reflection pass cannot
                        overwrite the correction.
                      </p>
                    </form>
                  ) : (
                    <>
                      <p data-testid={`preference-statement-${row.id}`}>
                        <strong>[{row.scope}]</strong> {row.statement}
                      </p>
                      <p data-testid={`preference-provenance-${row.id}`}>{sourceSentence(row)}</p>
                      <p data-testid={`preference-active-${row.id}`}>
                        {row.active
                          ? "Included in prompts for this project."
                          : "Switched off, so it is not included in prompts. Kept so you can see it was inferred."}
                      </p>
                      <button
                        type="button"
                        onClick={() => {
                          setEditing(row.id);
                          setDraft(row.statement);
                        }}
                      >
                        Correct
                      </button>
                      <button
                        type="button"
                        data-testid={`preference-toggle-${row.id}`}
                        onClick={() => correct.mutate({ id: row.id, active: !row.active })}
                      >
                        {row.active ? "Stop using this" : "Start using this again"}
                      </button>
                      <button
                        type="button"
                        data-testid={`preference-forget-${row.id}`}
                        onClick={() => forget.mutate(row.id)}
                      >
                        Forget entirely
                      </button>
                    </>
                  )}
                </li>
              ))}
            </ul>
          ) : null}
        </>
      )}
    </section>
  );
}

export function SkillFilePanel({ projectId }: { projectId: string }) {
  const query = useQuery<{
    content: string;
    included_preference_ids: string[];
    excluded_preferences: Array<{
      id: string;
      scope: string;
      statement: string;
      evidence_count: number;
    }>;
    explanation: string;
  }>({
    queryKey: ["learning", "skill-file", projectId],
    queryFn: async () => api.get(`/projects/${projectId}/learning/skill-file`),
  });

  if (query.isLoading) return <p role="status">Compiling the skill file.</p>;
  if (query.error)
    return (
      <p role="alert" data-testid="skill-file-error">
        The skill file could not be compiled, so what would reach the model is unknown.
      </p>
    );

  return (
    <section aria-labelledby="skill-file-heading" data-testid="skill-file">
      <h2 id="skill-file-heading">What actually reaches the model</h2>

      <p data-testid="skill-file-explanation">{query.data?.explanation}</p>

      {query.data?.content ? (
        <pre data-testid="skill-file-content">
          <code>{query.data.content}</code>
        </pre>
      ) : (
        <p data-testid="skill-file-empty">
          Nothing is injected, so prompts for this project are identical to those for a project with
          no history. That is the honest state before any feedback exists -- not a failure to load.
        </p>
      )}

      {query.data && query.data.excluded_preferences.length > 0 ? (
        <div data-testid="skill-file-excluded" role="alert">
          <h3>Left out because the budget was full</h3>
          <p>
            These preferences exist and are switched on, but did not fit and so never reached the
            model. This is the answer to &ldquo;why did it ignore my preference&rdquo;.
          </p>
          <ul>
            {query.data.excluded_preferences.map((row) => (
              <li key={row.id} data-testid={`excluded-${row.id}`}>
                [{row.scope}] {row.statement.slice(0, 120)}
                {row.statement.length > 120 ? "\u2026" : ""}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </section>
  );
}

export function LearningHistoryViewer({ projectId }: { projectId: string }) {
  const query = useQuery<{
    feedback: FeedbackRow[];
    injections: InjectionRow[];
    explanation: string;
  }>({
    queryKey: ["learning", "history", projectId],
    queryFn: async () => api.get(`/projects/${projectId}/learning/history`),
  });

  return (
    <section aria-labelledby="history-heading" data-testid="learning-history">
      <h2 id="history-heading">Learning history</h2>

      {query.isLoading ? (
        <p role="status">Reading the history.</p>
      ) : query.error ? (
        <p role="alert" data-testid="history-error">
          The learning history could not be loaded.
        </p>
      ) : (
        <>
          <p data-testid="history-explanation">{query.data?.explanation}</p>

          {query.data && query.data.feedback.length > 0 ? (
            <table>
              <caption>
                What you told this system, newest first. `edited` carries the version you kept,
                which is the most informative of the three verdicts: the difference between what was
                produced and what you kept is a preference stated by demonstration.
              </caption>
              <thead>
                <tr>
                  <th scope="col">Artifact</th>
                  <th scope="col">What you did</th>
                  <th scope="col">Why</th>
                </tr>
              </thead>
              <tbody>
                {query.data.feedback.map((row) => (
                  <tr key={row.id} data-testid={`feedback-${row.id}`}>
                    <td>
                      <code>{row.artifact_path}</code>
                    </td>
                    <td data-testid={`feedback-verdict-${row.id}`}>
                      {row.verdict === "edited"
                        ? `kept it with changes (${row.final_content_bytes} bytes)`
                        : row.verdict}
                    </td>
                    <td>
                      {row.comment || (
                        // Not a blank: an absent reason is normal, and demanding one would bias the store
                        // towards whichever verdict needs no explanation.
                        <span data-testid={`feedback-no-comment-${row.id}`}>no reason given</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : null}
        </>
      )}
    </section>
  );
}
