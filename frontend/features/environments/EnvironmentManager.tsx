"use client";

/**
 * §2.1's two frontend deliverables: environment management, and the selector the rest of the dashboard
 * uses.
 *
 * WHAT THIS SCREEN HAS TO BE HONEST ABOUT, and why it is unusually careful for a CRUD panel.
 *
 * The one field here that changes what the platform will do without a human is `requires_approval`. So
 * it is never rendered as a bare checkbox state: an environment that will deploy unattended says so in
 * words, next to its name, every time it is listed. A reader skimming the list must not have to infer
 * the dangerous case from an unticked box.
 *
 * ABSENCE IS DISTINGUISHED FROM EMPTINESS. "No environments yet" and "could not reach the server" are
 * different sentences, because the first invites an action and the second invites a retry. The same rule
 * the pairing screen's tri-state heartbeat follows.
 *
 * A SECRET'S VALUE IS NEVER RENDERED, because the server never sends one: a secret arrives as
 * `value: null, is_secret: true`, and this component shows "set, not shown" rather than an empty input
 * that would look like an unset variable. Rendering a blank field for a configured secret is how an
 * operator overwrites one by saving a form they only meant to look at.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useId, useState } from "react";

import { api, queryKeys } from "@/lib/api";

/** One environment as `GET /projects/{id}/environments` reports it. */
export type Environment = {
  id: string;
  name: string;
  kind: string;
  k8s_context: string | null;
  requires_approval: boolean;
  position: number;
};

type EnvironmentList = { environments: Environment[] };

type Variable = { key: string; value: string | null; is_secret: boolean };
type VariableList = { variables: Variable[] };

/** The closed set the backend enforces. Mirrored here so the form cannot offer an invalid kind. */
const KINDS = ["development", "test", "staging", "production", "custom"] as const;

/**
 * The environment selector, for every screen that acts on one environment at a time.
 *
 * Controlled rather than stateful: a deployment screen owns which environment it is deploying to, and a
 * selector holding its own copy would let the two disagree about the target of a mutation.
 *
 * IT NAMES THE APPROVAL CONSEQUENCE IN THE OPTION TEXT. Choosing an environment is choosing whether the
 * next action needs a human, and that belongs where the choice is made rather than in a tooltip.
 */
export function EnvironmentSelector({
  projectId,
  value,
  onChange,
  label = "Environment",
}: {
  projectId: string;
  value: string | null;
  onChange: (environmentId: string) => void;
  label?: string;
}) {
  const selectId = useId();
  const environments = useQuery<EnvironmentList>({
    queryKey: queryKeys.environments.list(projectId),
    queryFn: () => api.get<EnvironmentList>(`/projects/${projectId}/environments`),
  });

  if (environments.isPending) {
    return <p data-testid="environment-selector-loading">Loading environments…</p>;
  }
  if (environments.isError) {
    return (
      <p role="alert" data-testid="environment-selector-error">
        Environments could not be loaded, so no deployment target can be chosen. This is not the
        same as having none configured — retry, or check the server.
      </p>
    );
  }

  const list = environments.data?.environments ?? [];
  if (list.length === 0) {
    return (
      <p data-testid="environment-selector-empty">
        This project has no environments yet. Add one below before deploying.
      </p>
    );
  }

  return (
    <div>
      <label htmlFor={selectId}>{label}</label>
      <select
        id={selectId}
        data-testid="environment-selector"
        value={value ?? ""}
        onChange={(event) => onChange(event.target.value)}
      >
        <option value="" disabled>
          Choose an environment
        </option>
        {list.map((environment) => (
          <option key={environment.id} value={environment.id}>
            {environment.name} ({environment.kind}) —{" "}
            {environment.requires_approval ? "needs approval" : "deploys unattended"}
          </option>
        ))}
      </select>
    </div>
  );
}

/** The management screen: the pipeline in order, plus the form that appends to it. */
export function EnvironmentManager({ projectId }: { projectId: string }) {
  const queryClient = useQueryClient();
  const nameId = useId();
  const kindId = useId();
  const contextId = useId();

  const [name, setName] = useState("");
  const [kind, setKind] = useState<string>("development");
  const [context, setContext] = useState("");
  // UNDEFINED UNTIL TOUCHED, and sent as absent. The server treats "not stated" as "approval required";
  // sending `false` by default from a UI that had not asked would silently waive the gate.
  const [waiveApproval, setWaiveApproval] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  const environments = useQuery<EnvironmentList>({
    queryKey: queryKeys.environments.list(projectId),
    queryFn: () => api.get<EnvironmentList>(`/projects/${projectId}/environments`),
  });

  const create = useMutation({
    mutationFn: () =>
      api.post<Environment>(`/projects/${projectId}/environments`, {
        name,
        kind,
        k8s_context: context.trim() || null,
        requires_approval: waiveApproval ? false : null,
      }),
    onSuccess: async () => {
      setName("");
      setContext("");
      setWaiveApproval(false);
      setProblem(null);
      await queryClient.invalidateQueries({ queryKey: queryKeys.environments.list(projectId) });
    },
    // THE SERVER'S REASON IS SHOWN VERBATIM. The interesting refusal here is "a production environment
    // cannot waive approval — create a 'custom' one instead", and that sentence names the way forward.
    // Replacing it with "could not create environment" would throw away the only useful part.
    onError: (error: unknown) => setProblem(errorText(error)),
  });

  const remove = useMutation({
    mutationFn: (environmentId: string) =>
      api.delete<{ deleted: string }>(`/projects/${projectId}/environments/${environmentId}`),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: queryKeys.environments.list(projectId) });
    },
    onError: (error: unknown) => setProblem(errorText(error)),
  });

  if (environments.isError) {
    return (
      <section aria-label="Environments">
        <p role="alert" data-testid="environments-error">
          Environments could not be loaded. Nothing has been changed.
        </p>
      </section>
    );
  }

  const list = environments.data?.environments ?? [];

  return (
    <section aria-label="Environments">
      <h2>Environments</h2>
      <p>
        Listed in promotion order. Each environment promotes to the one below it; the last promotes
        to nothing.
      </p>

      {environments.isPending ? (
        <p data-testid="environments-loading">Loading…</p>
      ) : list.length === 0 ? (
        <p data-testid="environments-empty">
          No environments configured yet. The first one you add becomes the start of the pipeline.
        </p>
      ) : (
        <ol data-testid="environment-list">
          {list.map((environment) => (
            <li key={environment.id} data-testid={`environment-${environment.name}`}>
              <strong>{environment.name}</strong> <span>{environment.kind}</span>{" "}
              <span data-testid={`environment-gate-${environment.name}`}>
                {environment.requires_approval
                  ? "Deployments here need human approval"
                  : "Deployments here run unattended"}
              </span>{" "}
              <span>
                {environment.k8s_context
                  ? `context ${environment.k8s_context}`
                  : "no Kubernetes context wired yet"}
              </span>
              <button
                type="button"
                onClick={() => remove.mutate(environment.id)}
                aria-label={`Remove ${environment.name}`}
              >
                Remove
              </button>
            </li>
          ))}
        </ol>
      )}

      <form
        onSubmit={(event) => {
          event.preventDefault();
          create.mutate();
        }}
      >
        <label htmlFor={nameId}>Name</label>
        <input
          id={nameId}
          value={name}
          onChange={(event) => setName(event.target.value)}
          required
          maxLength={64}
        />

        <label htmlFor={kindId}>Kind</label>
        <select id={kindId} value={kind} onChange={(event) => setKind(event.target.value)}>
          {KINDS.map((option) => (
            <option key={option} value={option}>
              {option}
            </option>
          ))}
        </select>

        <label htmlFor={contextId}>Kubernetes context (optional)</label>
        <input
          id={contextId}
          value={context}
          onChange={(event) => setContext(event.target.value)}
        />

        <label>
          <input
            type="checkbox"
            checked={waiveApproval}
            onChange={(event) => setWaiveApproval(event.target.checked)}
          />
          Deploy to this environment without human approval
        </label>
        {/* The consequence, stated where the choice is made rather than after it is made. */}
        <p>
          Leaving this unticked is the safe default: every deployment to this environment will wait
          for a human. A production environment cannot waive it at all.
        </p>

        <button type="submit" disabled={create.isPending || name.trim().length === 0}>
          {create.isPending ? "Adding…" : "Add environment"}
        </button>
      </form>

      {problem ? (
        <p role="alert" data-testid="environment-problem">
          {problem}
        </p>
      ) : null}
    </section>
  );
}

/** One environment's variables, with secrets reported as present and withheld. */
export function EnvironmentVariables({
  projectId,
  environmentId,
}: {
  projectId: string;
  environmentId: string;
}) {
  const queryClient = useQueryClient();
  const keyId = useId();
  const valueId = useId();
  const [key, setKey] = useState("");
  const [value, setValue] = useState("");
  const [isSecret, setIsSecret] = useState(false);

  const variables = useQuery<VariableList>({
    queryKey: queryKeys.environments.variables(environmentId),
    queryFn: () =>
      api.get<VariableList>(`/projects/${projectId}/environments/${environmentId}/variables`),
  });

  const save = useMutation({
    mutationFn: () =>
      api.put<Variable>(`/projects/${projectId}/environments/${environmentId}/variables`, {
        key,
        value,
        is_secret: isSecret,
      }),
    onSuccess: async () => {
      setKey("");
      setValue("");
      await queryClient.invalidateQueries({
        queryKey: queryKeys.environments.variables(environmentId),
      });
    },
  });

  const list = variables.data?.variables ?? [];

  return (
    <section aria-label="Environment variables">
      <h3>Variables</h3>
      {variables.isError ? (
        <p role="alert">Variables could not be loaded.</p>
      ) : variables.isPending ? (
        // LOADING IS ITS OWN BRANCH. The first version of this component fell through to the list while
        // the query was pending and rendered an EMPTY one, which reads as "this environment has no
        // variables" — the exact confusion between absence and not-yet-known that the rest of this file
        // is careful about. Its own test caught it.
        <p data-testid="variables-loading">Loading variables…</p>
      ) : list.length === 0 ? (
        <p data-testid="variables-empty">No variables set for this environment.</p>
      ) : (
        <dl data-testid="variable-list">
          {list.map((variable) => (
            <div key={variable.key}>
              <dt>{variable.key}</dt>
              {/* SET-BUT-WITHHELD IS ITS OWN STATE. An empty value here would read as unset, and an
                  operator would overwrite a working secret by saving a form they meant to read. */}
              <dd data-testid={`variable-${variable.key}`}>
                {variable.is_secret ? "secret — set, not shown" : variable.value}
              </dd>
            </div>
          ))}
        </dl>
      )}

      <form
        onSubmit={(event) => {
          event.preventDefault();
          save.mutate();
        }}
      >
        <label htmlFor={keyId}>Key</label>
        <input id={keyId} value={key} onChange={(event) => setKey(event.target.value)} required />
        <label htmlFor={valueId}>Value</label>
        <input
          id={valueId}
          type={isSecret ? "password" : "text"}
          value={value}
          onChange={(event) => setValue(event.target.value)}
          required
        />
        <label>
          <input
            type="checkbox"
            checked={isSecret}
            onChange={(event) => setIsSecret(event.target.checked)}
          />
          Store as a secret (sealed; never shown again)
        </label>
        <button type="submit" disabled={save.isPending || key.trim().length === 0}>
          Save variable
        </button>
      </form>
    </section>
  );
}

/** The problem document's detail when there is one, so a refusal keeps the reason the server gave. */
function errorText(error: unknown): string {
  const detail = (error as { problem?: { detail?: string } })?.problem?.detail;
  if (typeof detail === "string" && detail.length > 0) {
    return detail;
  }
  return error instanceof Error ? error.message : "The request failed.";
}
