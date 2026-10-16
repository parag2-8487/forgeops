"use client";

/**
 * Self-healing activity and post-incident records. Phase 2 §2.12.
 *
 * WHAT THIS PANEL EXISTS TO MAKE VISIBLE. Self-healing is the only feature that acts on production without a
 * human, so the operator-facing question is not "what did it do" but "what did it decline to do, and why, and
 * what will it no longer try". An activity log showing only successful actions would make a system that
 * declined three times look like a system that was never engaged.
 *
 * So REFUSALS ARE ROWS HERE, marked as decisions rather than as failures, and the remaining budget and the
 * disqualified remedies are stated in words. An operator reading this can tell:
 *   - nothing was attempted (no rows, and a sentence saying so is different from a declined attempt)
 *   - something was declined, and which bound declined it
 *   - a remedy has been disqualified and will never be retried for this incident
 *   - the budget is spent and the incident is now theirs
 *
 * AND `auto` IS RENDERED AS A SENTENCE, NOT A BADGE. "Ran without waiting for a human" and "required an
 * approval" are the two facts that matter most on this screen, and a coloured dot does not survive a
 * screenshot, a colour-blind reader, or a screen reader.
 */

import { useQuery } from "@tanstack/react-query";

import { api } from "@/lib/api/client";

export type HealingState = "proposed" | "executing" | "succeeded" | "failed" | "refused";

export interface HealingActionRow {
  id: string;
  remedy: string;
  operation: string;
  arguments: Record<string, unknown>;
  auto: boolean;
  state: HealingState;
  change_set_id: string | null;
  note: string;
  created_at: string;
  completed_at: string | null;
}

export interface PostmortemRow {
  id: string;
  state: "generated" | "unavailable" | "insufficient";
  summary: string;
  recommendations: string[];
  model: string;
  actions_considered: number;
  created_at: string;
}

/** One sentence per state. `refused` is a DECISION, not a failure, and the wording says so. */
const STATE_SENTENCE: Record<HealingState, string> = {
  proposed: "Proposed. Nothing has been sent; a human decides whether this runs.",
  executing: "Sent to the agent and awaiting its report.",
  succeeded: "Completed.",
  failed: "Failed. This remedy will not be tried again for this incident.",
  refused: "Declined by a guard rail before anything was sent.",
};

const POSTMORTEM_SENTENCE: Record<PostmortemRow["state"], string> = {
  generated: "",
  unavailable:
    "No summary was written because no model could be reached. The incident, its evidence and every healing action are recorded and readable directly; what is missing is the narrative, not the facts.",
  insufficient:
    "The model answered in a form that could not be parsed into a summary, so none has been recorded. Nothing has been invented in its place.",
};

/**
 * The notice shown when the log itself could not be read.
 *
 * A separate exported component rather than inline JSX, so it can be tested without driving React Query
 * into an error state -- two attempts at that (a rejected mock, then a seeded cache entry) fought the
 * library rather than the code: the first tripped vitest's unhandled-rejection reporter when the component
 * unmounted with a fetch in flight, and the second was overwritten by the query's own refetch on mount.
 * The branch being tested is a pure render decision, so testing it as one is both simpler and more direct.
 */
export function HealingLogUnavailable() {
  return (
    <p role="alert" data-testid="healing-log-error">
      The healing log could not be loaded, so it is not known what the system has done about this
      incident. An empty log is not being shown in its place.
    </p>
  );
}

export function HealingActivityLog({ incidentId }: { incidentId: string }) {
  const query = useQuery<{
    actions: HealingActionRow[];
    attempts_used: number;
    attempts_remaining: number;
    disqualified_remedies: string[];
    explanation: string;
  }>({
    queryKey: ["healing", incidentId],
    queryFn: async () => api.get(`/incidents/${incidentId}/healing`),
    refetchInterval: 30_000,
  });

  return (
    <section aria-labelledby="healing-heading" data-testid="healing-log">
      <h3 id="healing-heading">Automated response</h3>

      {query.isLoading ? (
        <p role="status">Reading the healing log.</p>
      ) : query.error ? (
        <HealingLogUnavailable />
      ) : query.data ? (
        <>
          {/* THE BUDGET SENTENCE COMES FIRST. Whether the system will act again is the thing an operator
              needs before reading what it already did. */}
          <p
            data-testid="healing-budget"
            role={query.data.attempts_remaining === 0 ? "alert" : "status"}
          >
            {query.data.explanation}
          </p>

          {query.data.actions.length === 0 ? null : (
            <table>
              <caption>
                Every action, including the ones a guard rail declined. A declined action is a
                decision with a reason, not an absence.
              </caption>
              <thead>
                <tr>
                  <th scope="col">Remedy</th>
                  <th scope="col">Who approved</th>
                  <th scope="col">Outcome</th>
                  <th scope="col">Why</th>
                </tr>
              </thead>
              <tbody>
                {query.data.actions.map((action) => (
                  <tr key={action.id} data-testid={`healing-action-${action.id}`}>
                    <td>
                      {action.remedy}
                      {/* The derived target, shown so an operator can confirm the action touched what the
                          incident was about rather than something else. */}
                      <span> ({Object.values(action.arguments).join(" ")})</span>
                    </td>
                    <td data-testid={`healing-auto-${action.id}`}>
                      {action.auto
                        ? "Ran without waiting for a human. Policy, blast radius and audit still applied."
                        : "Required an approval."}
                    </td>
                    <td data-testid={`healing-state-${action.id}`}>
                      {STATE_SENTENCE[action.state]}
                    </td>
                    <td>{action.note}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          {query.data.disqualified_remedies.length > 0 ? (
            <p data-testid="healing-disqualified" role="alert">
              {query.data.disqualified_remedies.join(", ")} failed and{" "}
              {query.data.disqualified_remedies.length === 1 ? "has" : "have"} been disqualified for
              this incident. A remedy that failed is evidence it is not the answer, so repeating it
              is not attempted.
            </p>
          ) : null}
        </>
      ) : null}
    </section>
  );
}

export function PostmortemDisplay({ incidentId }: { incidentId: string }) {
  const query = useQuery<{ postmortems: PostmortemRow[]; explanation: string }>({
    queryKey: ["postmortem", incidentId],
    queryFn: async () => api.get(`/incidents/${incidentId}/postmortem`),
  });

  if (query.isLoading) return <p role="status">Reading the post-incident record.</p>;
  if (query.error)
    return (
      <p role="alert" data-testid="postmortem-error">
        The post-incident record could not be loaded.
      </p>
    );

  const latest = query.data?.postmortems[0];

  return (
    <section aria-labelledby="postmortem-heading" data-testid="postmortem">
      <h3 id="postmortem-heading">Post-incident record</h3>

      {latest === undefined ? (
        <p data-testid="postmortem-none">{query.data?.explanation}</p>
      ) : latest.state !== "generated" ? (
        // NOT an empty summary block. A heading with nothing under it reads as "reviewed, nothing to say".
        <p data-testid="postmortem-unavailable" role="status">
          {POSTMORTEM_SENTENCE[latest.state]}
        </p>
      ) : (
        <>
          <p data-testid="postmortem-summary">{latest.summary}</p>

          <h4>Recommendations</h4>
          {latest.recommendations.length === 0 ? (
            // Distinct from "no postmortem": a record WAS written and it recommends nothing, which is a
            // finding. An empty list rendered as an empty <ul> would read as a rendering failure.
            <p data-testid="postmortem-no-recommendations">
              This record recommends no changes. That is a conclusion rather than a gap: the summary
              above was written and nothing in it supported a recommendation.
            </p>
          ) : (
            <ul data-testid="postmortem-recommendations">
              {latest.recommendations.map((recommendation) => (
                <li key={recommendation}>{recommendation}</li>
              ))}
            </ul>
          )}

          <p data-testid="postmortem-provenance">
            Written from {latest.actions_considered} recorded action(s)
            {latest.model ? ` by ${latest.model}` : ""}. Recommendations are prose for a human:
            nothing here can execute itself.
          </p>
        </>
      )}
    </section>
  );
}
