// SPDX-License-Identifier: FSL-1.1-ALv2
"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, queryKeys } from "@/lib/api";
import { AsyncState } from "@/components/ui/async-state";
import { GovernanceRefusal } from "@/components/ui/governance-refusal";
import { PolicyEditor, type Policy, type PolicyTemplate } from "@/features/policies/PolicyEditor";

/** Mirrors `PolicyPage` — the list route that did not exist until this screen needed it. */
interface PolicyPage {
  policies: Policy[];
  next_cursor: string | null;
}

const PAGE_LIMIT = 100;

/**
 * Policy management — phases.md §1.7 "Frontend: Policy list and editor UI".
 *
 * The screen was a read-only wall of templates, and its own copy explained why: the write path was not
 * wired, and shipping a button that silently discarded an edit would be worse than not shipping the
 * screen. That was the right call. What made it unfixable was the missing list route — publish,
 * templates, create, read, update, delete and test all existed, and every one of them except templates
 * needed an id nothing would give you. `GET /api/v1/policies` closes that, and this is the screen it
 * was added for.
 *
 * PUBLISH IS ON THIS SCREEN, AND IT IS NOT AN ADMIN CURIOSITY. `POST /policies/publish` compiles the
 * enabled policies into a signed bundle and activates it. Nothing downstream works without one: the
 * governance chokepoint refuses every change-set submission from a device that is not pinned to the
 * tenant's current digest, so a freshly created project with unpublished policies fails at the very
 * last step of the onboarding path with a stale-bundle error four layers from its cause. That is
 * exactly what the SSE paint test discovered. So publish sits beside the editor, and says what it is
 * for.
 */
export default function PoliciesPage() {
  const queryClient = useQueryClient();
  // `null` means "no editor open"; `{ policy: null }` means "the editor is open on a new policy".
  // A plain `Policy | null` cannot express the difference, and conflating them is how a New button
  // ends up editing whichever policy was selected last.
  const [editing, setEditing] = useState<{ policy: Policy | null } | null>(null);
  const [deleting, setDeleting] = useState<Policy | null>(null);

  const policies = useQuery({
    queryKey: queryKeys.policies.list(PAGE_LIMIT),
    queryFn: () => api.get<PolicyPage>(`/policies?limit=${PAGE_LIMIT}`),
    retry: false,
  });

  const templates = useQuery({
    queryKey: queryKeys.policies.templates(),
    queryFn: () => api.get<PolicyTemplate[]>("/policies/templates"),
    retry: false,
  });

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: queryKeys.policies.all });
  };

  const remove = useMutation({
    mutationFn: (id: string) => api.delete<void>(`/policies/${id}`),
    onSuccess: () => {
      setDeleting(null);
      invalidate();
    },
  });

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Policies</h1>
        <p className="mt-1 text-muted-foreground">
          The Rego your tenant&apos;s governance chokepoint evaluates. Read from{" "}
          <code>GET /api/v1/policies</code>; validated by <code>opa check</code> on save and
          testable against an input document before you publish.
        </p>
      </div>

      <PublishPanel />

      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 className="text-lg font-semibold">Stored policies</h2>
        <button
          type="button"
          onClick={() => setEditing({ policy: null })}
          data-testid="new-policy"
          className="rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          New policy
        </button>
      </div>

      {editing ? (
        <PolicyEditor
          // The remount key. Switching which policy is being edited discards the editor and builds a
          // fresh one, so its state cannot be left holding the previous policy's Rego under the new
          // policy's name. See the editor's own note for why this is not an effect.
          key={editing.policy?.id ?? "new"}
          policy={editing.policy}
          templates={templates.data ?? []}
          onSaved={(saved) => setEditing({ policy: saved })}
          onCancel={() => setEditing(null)}
        />
      ) : null}

      <AsyncState
        isPending={policies.isPending}
        error={policies.error}
        isEmpty={policies.data?.policies.length === 0}
        emptyMessage="No policies are stored for this tenant. Until one is, the chokepoint has nothing to evaluate — and an absent policy is a deny, not permission. Create one above; the templates give you a starting point."
        label="policies"
      >
        <ul className="space-y-3" data-testid="policy-list">
          {policies.data?.policies.map((policy) => (
            <li
              key={policy.id}
              className="rounded-lg border border-border bg-background p-4"
              data-testid={`policy-${policy.id}`}
            >
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="min-w-0">
                  <h3 className="text-sm font-semibold">{policy.name}</h3>
                  <p className="mt-0.5 text-xs text-muted-foreground">
                    <span data-testid={`policy-enabled-${policy.id}`}>
                      {policy.enabled ? "enabled" : "disabled"}
                    </span>
                    {" · "}
                    {policy.project_id === null
                      ? "applies to every project in this tenant"
                      : "scoped to one project"}
                    {policy.template_id ? ` · from the ${policy.template_id} template` : ""}
                    {policy.updated_at ? ` · updated ${policy.updated_at}` : ""}
                  </p>
                </div>
                <div className="flex gap-2">
                  <button
                    type="button"
                    onClick={() => setEditing({ policy })}
                    data-testid={`edit-${policy.id}`}
                    className="rounded-md border border-border px-2 py-1 text-xs focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  >
                    Edit and test
                  </button>
                  <button
                    type="button"
                    onClick={() => setDeleting(policy)}
                    data-testid={`delete-policy-${policy.id}`}
                    className="rounded-md border border-destructive/50 px-2 py-1 text-xs text-destructive focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  >
                    Delete
                  </button>
                </div>
              </div>
              <pre className="mt-3 max-h-40 overflow-auto rounded bg-muted p-3 text-xs">
                <code>{policy.rego_rules}</code>
              </pre>
            </li>
          ))}
        </ul>
      </AsyncState>

      {deleting ? (
        <div
          role="alertdialog"
          aria-labelledby="delete-policy-heading"
          className="space-y-3 rounded-lg border border-destructive/40 bg-destructive/5 p-4 text-sm"
        >
          <h2 id="delete-policy-heading" className="font-semibold">
            Delete {deleting.name}?
          </h2>
          <p className="text-xs text-muted-foreground">
            The policy row goes. Any bundle already published keeps the rule it compiled, so devices
            pinned to that digest are unaffected until a new bundle is published — which is the
            point of superseded bundles being kept rather than deleted. Publish after deleting if
            you want the rule to stop applying.
          </p>
          <div className="flex gap-3">
            <button
              type="button"
              onClick={() => remove.mutate(deleting.id)}
              disabled={remove.isPending}
              data-testid="confirm-delete-policy"
              className="rounded-md bg-destructive px-3 py-1.5 text-xs font-medium text-destructive-foreground disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              {remove.isPending ? "Deleting…" : "Delete policy"}
            </button>
            <button
              type="button"
              onClick={() => setDeleting(null)}
              className="rounded-md border border-border px-3 py-1.5 text-xs focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              Cancel
            </button>
          </div>
          <GovernanceRefusal error={remove.error} action="delete this policy" />
        </div>
      ) : null}

      <section aria-labelledby="templates-heading" className="space-y-3">
        <h2 id="templates-heading" className="text-lg font-semibold">
          Templates
        </h2>
        <p className="text-sm text-muted-foreground">
          Read-only starting points from <code>GET /api/v1/policies/templates</code>. Choosing one
          in the editor copies its Rego; it does not link to it, so editing your copy cannot change
          the template.
        </p>
        <AsyncState
          isPending={templates.isPending}
          error={templates.error}
          isEmpty={templates.data?.length === 0}
          emptyMessage="No policy templates are registered."
          label="policy templates"
        >
          <ul className="space-y-4">
            {templates.data?.map((t) => (
              <li key={t.id} className="rounded-lg border border-border bg-background p-4">
                <div className="flex flex-wrap items-baseline justify-between gap-2">
                  <h3 className="text-sm font-semibold">{t.name}</h3>
                  <code className="text-xs text-muted-foreground">{t.id}</code>
                </div>
                <p className="mt-1 text-sm text-muted-foreground">{t.description}</p>
                <pre className="mt-3 max-h-40 overflow-auto rounded bg-muted p-3 text-xs">
                  <code>{t.rego_rules}</code>
                </pre>
              </li>
            ))}
          </ul>
        </AsyncState>
      </section>
    </div>
  );
}

/**
 * Publish the bundle — the step whose absence breaks everything downstream.
 *
 * `POST /policies/publish` returns `202` with the digest it is publishing. 202 rather than 200 because
 * the activation is dispatched as a task, so the response means "accepted", and the panel says that
 * rather than claiming the bundle is live.
 */
function PublishPanel() {
  const publish = useMutation({
    mutationFn: () => api.post<{ digest: string; status: string }>("/policies/publish"),
  });

  return (
    <section
      aria-labelledby="publish-heading"
      className="space-y-3 rounded-lg border border-border bg-background p-4"
    >
      <h2 id="publish-heading" className="text-sm font-semibold">
        Publish the policy bundle
      </h2>
      <p className="text-xs text-muted-foreground">
        Compiles every enabled policy into a signed bundle and makes it the tenant&apos;s active
        one.
        <strong> Nothing downstream works until you have done this at least once.</strong> The
        chokepoint refuses a change-set submission from any agent that is not pinned to the current
        digest, so an unpublished tenant fails at the last step of a generation run with a
        stale-bundle error four layers from its cause. Publish again after every policy change you
        want agents to enforce.
      </p>
      <button
        type="button"
        onClick={() => publish.mutate()}
        disabled={publish.isPending}
        data-testid="publish-bundle"
        className="rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        {publish.isPending ? "Publishing…" : "Publish bundle"}
      </button>

      {publish.data ? (
        <p role="status" data-testid="publish-result" className="text-sm">
          Accepted. Digest <code className="break-all">{publish.data.digest}</code> — status{" "}
          <strong>{publish.data.status}</strong>. Activation is dispatched as a task, so this says
          the publish was accepted rather than that every agent has picked it up; a paired agent
          takes the new digest on its next connection.
        </p>
      ) : null}

      <GovernanceRefusal error={publish.error} action="publish the policy bundle" />
    </section>
  );
}
