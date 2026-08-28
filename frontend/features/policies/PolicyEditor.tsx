// SPDX-License-Identifier: FSL-1.1-ALv2
"use client";

import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api, queryKeys, ApiProblemError } from "@/lib/api";
import { GovernanceRefusal } from "@/components/ui/governance-refusal";

/** Mirrors `PolicyRead` in `backend/src/policies/schemas.py`. */
export interface Policy {
  id: string;
  project_id: string | null;
  tenant_id: string | null;
  name: string;
  engine: string;
  rego_rules: string;
  enabled: boolean;
  template_id: string | null;
  created_at: string | null;
  updated_at: string | null;
}

/** Mirrors `DryRunResult`. There is no path that returns this without OPA having produced it. */
export interface DryRunResult {
  decision: string;
  rule: string;
  evaluated_with: string;
  undefined: boolean;
}

/** Mirrors `PolicyTemplateRead`. */
export interface PolicyTemplate {
  id: string;
  name: string;
  description: string;
  rego_rules: string;
  parameters: Record<string, unknown>;
}

const NEW_POLICY_REGO = `package forgeops.governance

# The chokepoint evaluates data.forgeops.governance.decision and expects
# "allow", "deny" or "require_approval". An undefined decision is a deny.
default decision = "deny"

decision = "allow" if {
	input.blast_radius == "workspace"
}
`;

/**
 * Author a Rego policy, see the validator's actual complaint, and test it against a real input.
 *
 * phases.md §1.7 "Frontend: Policy list and editor UI". What was here before was a textarea with a
 * `Validate & Save Policy` button wired to nothing — the page's own copy said shipping a button that
 * silently discards an edit would be worse than not shipping the screen, which was right, and is the
 * reason this is a full editor rather than a connected button.
 *
 * THREE THINGS THE EDITOR HAS TO GET RIGHT
 *
 * **The validator's message, not a generic one.** `validate_rego` runs `opa check` server-side on
 * create and update and answers 422 with `[rego_parse_error] <message> at line N` in the problem
 * document's `detail`. Rendering "Could not save" over that would throw away the line number, which is
 * the only part an author needs. So the 422's detail is surfaced verbatim, prominently, beside the
 * editor rather than in a toast.
 *
 * **Test is separate from save, and it is not a gate on it.** `POST /{id}/test` needs a stored policy
 * to evaluate, so it can only run after a save; making the save conditional on a passing dry-run would
 * mean you could not save a policy in order to test it. The two are sequential controls, and the panel
 * says which state you are in.
 *
 * **A dry-run answer is attributed.** The response carries the query that was evaluated and the
 * evaluator's version, and both are shown. This surface used to synthesise a decision when OPA was
 * absent; a decision with no statement of what produced it is indistinguishable from that.
 */
export function PolicyEditor({
  policy,
  templates,
  onSaved,
  onCancel,
}: {
  /** `null` to author a new policy. */
  policy: Policy | null;
  templates: PolicyTemplate[];
  onSaved: (policy: Policy) => void;
  onCancel: () => void;
}) {
  const queryClient = useQueryClient();
  const [name, setName] = useState(policy?.name ?? "");
  const [rego, setRego] = useState(policy?.rego_rules ?? NEW_POLICY_REGO);
  const [enabled, setEnabled] = useState(policy?.enabled ?? true);
  const [inputDocument, setInputDocument] = useState(
    '{\n  "action": "write_file",\n  "blast_radius": "workspace"\n}\n',
  );
  const [inputError, setInputError] = useState<string | null>(null);

  /*
   * THE EDITOR IS REMOUNTED WHEN THE POLICY CHANGES, rather than resynchronised in an effect.
   *
   * The state above is initialised from props, so switching which policy is being edited has to reset
   * it — otherwise the first policy's Rego sits in the textarea under the second policy's name, and
   * saving writes one policy's rules over another's. The obvious implementation is a
   * `useEffect(() => { setName(...); setRego(...) }, [policy])`, and it is wrong twice: it renders once
   * with the previous policy's content before correcting itself, and it is exactly the cascading-render
   * pattern React's own guidance (and this repo's lint rule) rejects.
   *
   * The parent passes `key={policy?.id ?? "new"}`, so React discards this component and builds a fresh
   * one when the identity changes. That makes the reset structural: there is no window in which the
   * state and the props disagree, and no effect to forget to update when a fourth field is added.
   */

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: queryKeys.policies.all });
  };

  const save = useMutation({
    mutationFn: () =>
      policy === null
        ? api.post<Policy>("/policies", {
            name: name.trim(),
            engine: "rego",
            rego_rules: rego,
            enabled,
          })
        : api.patch<Policy>(`/policies/${policy.id}`, {
            name: name.trim(),
            rego_rules: rego,
            enabled,
          }),
    onSuccess: (saved) => {
      invalidate();
      onSaved(saved);
    },
  });

  const dryRun = useMutation({
    mutationFn: () => {
      // Parsed here so a malformed input document is reported as what it is rather than as a 422 from
      // the API about a body it could not read. The parse error names the position, which a server
      // round trip would not improve on.
      let parsed: unknown;
      try {
        parsed = JSON.parse(inputDocument);
      } catch (cause) {
        throw new Error(
          `The input document is not valid JSON: ${cause instanceof Error ? cause.message : "unparseable"}`,
        );
      }
      if (policy === null) throw new Error("Save the policy before testing it.");
      return api.post<DryRunResult>(`/policies/${policy.id}/test`, { input: parsed });
    },
  });

  /** The validator's own words, when the failure is a Rego syntax rejection. */
  const validationDetail =
    save.error instanceof ApiProblemError && save.error.problem.status === 422
      ? (save.error.problem.detail ??
        save.error.problem.errors?.map((e) => e.detail).join("; ") ??
        null)
      : null;

  return (
    <div className="space-y-4 rounded-lg border border-border bg-background p-4">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="text-sm font-semibold">
          {policy === null ? "New policy" : `Editing ${policy.name}`}
        </h2>
        <button
          type="button"
          onClick={onCancel}
          className="text-xs underline underline-offset-4 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          Close editor
        </button>
      </div>

      {policy === null && templates.length > 0 ? (
        <div>
          <label htmlFor="policy-template" className="block text-sm font-medium">
            Start from a template
          </label>
          <select
            id="policy-template"
            defaultValue=""
            onChange={(event) => {
              const template = templates.find((t) => t.id === event.target.value);
              if (template) {
                setRego(template.rego_rules);
                if (name.trim() === "") setName(template.name);
              }
            }}
            className="mt-1 w-full rounded-md border border-border bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            <option value="">Write from scratch…</option>
            {templates.map((template) => (
              <option key={template.id} value={template.id}>
                {template.name} — {template.description}
              </option>
            ))}
          </select>
        </div>
      ) : null}

      <div>
        <label htmlFor="policy-name" className="block text-sm font-medium">
          Name
        </label>
        <input
          id="policy-name"
          value={name}
          onChange={(event) => setName(event.target.value)}
          required
          maxLength={200}
          className="mt-1 w-full rounded-md border border-border bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        />
      </div>

      <div>
        <label htmlFor="policy-rego" className="block text-sm font-medium">
          Rego
        </label>
        <textarea
          id="policy-rego"
          value={rego}
          onChange={(event) => setRego(event.target.value)}
          rows={16}
          spellCheck={false}
          // `off` on all four: an editor that autocapitalises `package` or autocorrects an identifier
          // produces Rego that does not compile, and the author gets blamed for it.
          autoCapitalize="off"
          autoCorrect="off"
          autoComplete="off"
          aria-describedby="policy-rego-help"
          aria-invalid={validationDetail !== null}
          data-testid="policy-rego"
          className="mt-1 w-full rounded-md border border-border bg-muted px-3 py-2 font-mono text-xs focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        />
        <p id="policy-rego-help" className="mt-1 text-xs text-muted-foreground">
          Validated server-side by <code>opa check</code> when you save. The chokepoint evaluates{" "}
          <code>data.forgeops.governance.decision</code>, so a policy that defines something else
          will compile and never fire — the Test panel below reports that as <em>undefined</em>{" "}
          rather than as a deny.
        </p>
      </div>

      <label className="flex items-center gap-2 text-sm">
        <input
          type="checkbox"
          checked={enabled}
          onChange={(event) => setEnabled(event.target.checked)}
          className="focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        />
        Enabled — included in the next published bundle
      </label>

      {validationDetail ? (
        <div
          role="alert"
          data-testid="rego-validation-error"
          className="rounded-md border border-destructive/40 bg-destructive/5 p-3 text-sm"
        >
          <p className="font-semibold">The Rego was rejected.</p>
          {/* Verbatim. It carries the rule code and the line number, and paraphrasing it would throw
              away the only part that helps. */}
          <pre className="mt-2 overflow-x-auto whitespace-pre-wrap font-mono text-xs">
            {validationDetail}
          </pre>
          <p className="mt-2 text-xs text-muted-foreground">
            This is the validator&apos;s own message, from <code>opa check</code> running on the
            server. Nothing was saved.
          </p>
        </div>
      ) : null}

      {save.error && !validationDetail ? (
        <GovernanceRefusal error={save.error} action="save this policy" />
      ) : null}

      <div className="flex flex-wrap items-center gap-3">
        <button
          type="button"
          onClick={() => save.mutate()}
          disabled={name.trim() === "" || rego.trim() === "" || save.isPending}
          data-testid="save-policy"
          className="rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          {save.isPending ? "Validating and saving…" : "Validate and save"}
        </button>
        {save.isSuccess ? (
          <p role="status" className="text-sm text-muted-foreground">
            Saved. <code>opa check</code> accepted it.
          </p>
        ) : null}
      </div>

      <section aria-labelledby="dryrun-heading" className="space-y-3 border-t border-border pt-4">
        <h3 id="dryrun-heading" className="text-sm font-semibold">
          Test against an input document
        </h3>

        {policy === null ? (
          <p className="text-sm text-muted-foreground">
            Save the policy first. The dry-run evaluates the <em>stored</em> Rego, so testing an
            unsaved draft would report on a policy that does not exist — and a test result that does
            not correspond to what is stored is worse than no test.
          </p>
        ) : (
          <>
            <div>
              <label htmlFor="dryrun-input" className="block text-sm font-medium">
                Input document (JSON)
              </label>
              <textarea
                id="dryrun-input"
                value={inputDocument}
                onChange={(event) => {
                  setInputDocument(event.target.value);
                  setInputError(null);
                }}
                rows={6}
                spellCheck={false}
                autoCapitalize="off"
                autoCorrect="off"
                data-testid="dryrun-input"
                className="mt-1 w-full rounded-md border border-border bg-muted px-3 py-2 font-mono text-xs focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              />
            </div>

            <button
              type="button"
              onClick={() => {
                setInputError(null);
                dryRun.mutate();
              }}
              disabled={dryRun.isPending}
              data-testid="run-dryrun"
              className="rounded-md border border-border px-4 py-2 text-sm font-medium disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              {dryRun.isPending ? "Evaluating…" : "Test"}
            </button>

            {inputError ? (
              <p role="alert" className="text-sm text-destructive">
                {inputError}
              </p>
            ) : null}

            {/* A local error (bad JSON) is not an API problem, so it is rendered as itself rather than
                pushed through the problem renderer, which would report a status of nothing. */}
            {dryRun.error && !(dryRun.error instanceof ApiProblemError) ? (
              <p role="alert" data-testid="dryrun-local-error" className="text-sm text-destructive">
                {dryRun.error instanceof Error ? dryRun.error.message : "The test could not run."}
              </p>
            ) : null}

            {dryRun.error instanceof ApiProblemError ? (
              <GovernanceRefusal error={dryRun.error} action="evaluate this policy" />
            ) : null}

            {dryRun.data ? (
              <div
                data-testid="dryrun-result"
                className="rounded-md border border-border bg-muted/30 p-3 text-sm"
              >
                <p className="font-semibold">
                  Decision:{" "}
                  <span data-testid="dryrun-decision" className="font-mono">
                    {dryRun.data.decision}
                  </span>
                </p>
                {dryRun.data.undefined ? (
                  <p className="mt-2 text-muted-foreground">
                    <strong>Undefined, which is not a deny.</strong> OPA returned no value for{" "}
                    <code>{dryRun.data.rule}</code> against this input — the rule did not fire at
                    all, rather than firing and refusing. At the chokepoint an undefined decision IS
                    treated as a refusal, so the effect is the same; the cause is not, and it is
                    usually a package name or a rule name that does not match.
                  </p>
                ) : null}
                <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 text-xs text-muted-foreground">
                  <dt className="font-medium">Query</dt>
                  <dd>
                    <code>{dryRun.data.rule}</code>
                  </dd>
                  <dt className="font-medium">Evaluated by</dt>
                  <dd data-testid="dryrun-evaluator">{dryRun.data.evaluated_with}</dd>
                </dl>
                <p className="mt-2 text-xs text-muted-foreground">
                  A real OPA evaluation. This endpoint used to synthesise an allow or deny when the
                  evaluator was missing; it now refuses with a problem document instead, which is
                  why the version above is on screen — a decision you cannot attribute is not
                  evidence.
                </p>
              </div>
            ) : null}
          </>
        )}
      </section>
    </div>
  );
}
