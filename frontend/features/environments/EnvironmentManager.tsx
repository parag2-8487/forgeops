// SPDX-License-Identifier: FSL-1.1-ALv2
"use client";

/**
 * §2.1's two frontend deliverables: environment management, and the selector the rest of the dashboard
 * uses.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useId, useState } from "react";

import { api, queryKeys } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

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
    return (
      <div className="rounded-md border border-border p-3 text-sm text-muted-foreground">
        <p data-testid="environment-selector-loading">Loading environments…</p>
      </div>
    );
  }
  if (environments.isError) {
    return (
      <div className="rounded-md border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive">
        <p role="alert" data-testid="environment-selector-error">
          Environments could not be loaded, so no deployment target can be chosen. This is not the
          same as having none configured — retry, or check the server.
        </p>
      </div>
    );
  }

  const list = environments.data?.environments ?? [];
  if (list.length === 0) {
    return (
      <div className="rounded-md border border-border bg-muted/20 p-3 text-sm text-muted-foreground">
        <p data-testid="environment-selector-empty">
          This project has no environments yet. Add one below before deploying.
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-1.5">
      <label htmlFor={selectId} className="block text-sm font-medium">
        {label}
      </label>
      <select
        id={selectId}
        data-testid="environment-selector"
        value={value ?? ""}
        onChange={(event) => onChange(event.target.value)}
        className="flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm shadow-sm transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
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
      <section
        aria-label="Environments"
        className="rounded-lg border border-destructive/40 bg-destructive/10 p-4"
      >
        <p role="alert" data-testid="environments-error" className="text-sm text-destructive">
          Environments could not be loaded. Nothing has been changed.
        </p>
      </section>
    );
  }

  const list = environments.data?.environments ?? [];

  return (
    <section aria-label="Environments" className="space-y-6">
      <div>
        <h2 className="text-lg font-semibold tracking-tight">Environments</h2>
        <p className="mt-1 text-sm text-muted-foreground">
          Listed in promotion order. Each environment promotes to the one below it; the last
          promotes to nothing.
        </p>
      </div>

      {environments.isPending ? (
        <div className="rounded-lg border border-border p-4 text-sm text-muted-foreground">
          <p data-testid="environments-loading">Loading…</p>
        </div>
      ) : list.length === 0 ? (
        <div className="rounded-lg border border-dashed border-border p-6 text-center text-sm text-muted-foreground">
          <p data-testid="environments-empty">
            No environments configured yet. The first one you add becomes the start of the pipeline.
          </p>
        </div>
      ) : (
        <ol data-testid="environment-list" className="space-y-3">
          {list.map((environment, index) => (
            <li
              key={environment.id}
              data-testid={`environment-${environment.name}`}
              className="flex flex-wrap items-center justify-between gap-4 rounded-lg border border-border bg-card p-4 shadow-sm"
            >
              <div className="flex flex-wrap items-center gap-3">
                <span className="flex h-6 w-6 items-center justify-center rounded-full bg-muted text-xs font-semibold text-muted-foreground">
                  {index + 1}
                </span>
                <strong className="font-mono text-base font-semibold">{environment.name}</strong>
                <Badge variant="outline">{environment.kind}</Badge>
                <Badge
                  variant={environment.requires_approval ? "warning" : "success"}
                  data-testid={`environment-gate-${environment.name}`}
                >
                  {environment.requires_approval
                    ? "Deployments here need human approval"
                    : "Deployments here run unattended"}
                </Badge>
                <span className="rounded bg-muted px-2 py-0.5 font-mono text-xs text-muted-foreground">
                  {environment.k8s_context
                    ? `context ${environment.k8s_context}`
                    : "no Kubernetes context wired yet"}
                </span>
              </div>
              <Button
                type="button"
                variant="destructive"
                size="sm"
                onClick={() => remove.mutate(environment.id)}
                disabled={remove.isPending}
                aria-label={`Remove ${environment.name}`}
              >
                Remove
              </Button>
            </li>
          ))}
        </ol>
      )}

      <Card className="border border-border">
        <CardHeader>
          <CardTitle className="text-base font-semibold">Add environment</CardTitle>
          <CardDescription>
            Configure a target deployment tier in this promotion sequence.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <form
            className="space-y-4"
            onSubmit={(event) => {
              event.preventDefault();
              create.mutate();
            }}
          >
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
              <div className="space-y-1.5">
                <label htmlFor={nameId} className="block text-sm font-medium">
                  Name
                </label>
                <input
                  id={nameId}
                  value={name}
                  onChange={(event) => setName(event.target.value)}
                  required
                  maxLength={64}
                  placeholder="e.g. staging"
                  className="flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm shadow-sm transition-colors placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                />
              </div>

              <div className="space-y-1.5">
                <label htmlFor={kindId} className="block text-sm font-medium">
                  Kind
                </label>
                <select
                  id={kindId}
                  value={kind}
                  onChange={(event) => setKind(event.target.value)}
                  className="flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm shadow-sm transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                >
                  {KINDS.map((option) => (
                    <option key={option} value={option}>
                      {option}
                    </option>
                  ))}
                </select>
              </div>
            </div>

            <div className="space-y-1.5">
              <label htmlFor={contextId} className="block text-sm font-medium">
                Kubernetes context (optional)
              </label>
              <input
                id={contextId}
                value={context}
                onChange={(event) => setContext(event.target.value)}
                placeholder="e.g. cluster-staging"
                className="flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 font-mono text-sm shadow-sm transition-colors placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              />
            </div>

            <div className="rounded-lg border border-border bg-muted/20 p-4 space-y-2">
              <label className="flex items-center gap-2.5 text-sm font-medium cursor-pointer">
                <input
                  type="checkbox"
                  checked={waiveApproval}
                  onChange={(event) => setWaiveApproval(event.target.checked)}
                  className="h-4 w-4 rounded border-input focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                />
                Deploy to this environment without human approval
              </label>
              <p className="text-xs text-muted-foreground leading-relaxed pl-6.5">
                Leaving this unticked is the safe default: every deployment to this environment will
                wait for a human. A production environment cannot waive it at all.
              </p>
            </div>

            <Button
              type="submit"
              disabled={create.isPending || name.trim().length === 0}
              className="px-5 py-2 font-medium"
            >
              {create.isPending ? "Adding…" : "Add environment"}
            </Button>
          </form>

          {problem ? (
            <div
              role="alert"
              data-testid="environment-problem"
              className="mt-4 rounded-md border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive"
            >
              {problem}
            </div>
          ) : null}
        </CardContent>
      </Card>
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
      api.put(`/projects/${projectId}/environments/${environmentId}/variables`, {
        key,
        value,
        is_secret: isSecret,
      }),
    onSuccess: async () => {
      setKey("");
      setValue("");
      setIsSecret(false);
      await queryClient.invalidateQueries({
        queryKey: queryKeys.environments.variables(environmentId),
      });
    },
  });

  return (
    <div className="space-y-4 rounded-lg border border-border p-4">
      <h3 className="text-sm font-semibold">Environment variables</h3>

      {variables.isPending ? (
        <p className="text-sm text-muted-foreground">Loading variables…</p>
      ) : (variables.data?.variables.length ?? 0) === 0 ? (
        <p data-testid="variables-empty" className="text-sm text-muted-foreground">
          No variables set for this environment.
        </p>
      ) : (
        <ul
          data-testid="variable-list"
          className="divide-y divide-border rounded-md border border-border bg-card"
        >
          {variables.data?.variables.map((variable) => (
            <li
              key={variable.key}
              data-testid={`variable-${variable.key}`}
              className="flex items-center justify-between p-3 text-sm"
            >
              <span className="font-mono font-medium">{variable.key}</span>
              <span className="font-mono text-muted-foreground">
                {variable.is_secret ? "set, not shown" : variable.value}
              </span>
            </li>
          ))}
        </ul>
      )}

      <form
        className="space-y-3"
        onSubmit={(event) => {
          event.preventDefault();
          save.mutate();
        }}
      >
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <div className="space-y-1">
            <label htmlFor={keyId} className="block text-xs font-medium">
              Key
            </label>
            <input
              id={keyId}
              value={key}
              onChange={(event) => setKey(event.target.value)}
              required
              className="flex h-9 w-full rounded-md border border-input bg-background px-3 py-1 text-sm shadow-sm"
            />
          </div>
          <div className="space-y-1">
            <label htmlFor={valueId} className="block text-xs font-medium">
              Value
            </label>
            <input
              id={valueId}
              type={isSecret ? "password" : "text"}
              value={value}
              onChange={(event) => setValue(event.target.value)}
              required
              className="flex h-9 w-full rounded-md border border-input bg-background px-3 py-1 text-sm shadow-sm"
            />
          </div>
        </div>
        <label className="flex items-center gap-2 text-xs">
          <input
            type="checkbox"
            checked={isSecret}
            onChange={(event) => setIsSecret(event.target.checked)}
          />
          Store as a secret
        </label>
        <Button type="submit" size="sm" disabled={save.isPending || !key.trim()}>
          Save variable
        </Button>
      </form>
    </div>
  );
}

function errorText(caught: unknown): string {
  if (caught && typeof caught === "object" && "problem" in caught) {
    const problem = (caught as { problem: { detail?: string; title?: string } }).problem;
    return problem.detail || problem.title || "The request was refused";
  }
  return caught instanceof Error ? caught.message : "The request failed";
}
