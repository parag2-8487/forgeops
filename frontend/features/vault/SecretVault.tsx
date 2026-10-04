// SPDX-License-Identifier: FSL-1.1-ALv2
"use client";

import { useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api, queryKeys } from "@/lib/api";
import { GovernanceRefusal } from "@/components/ui/governance-refusal";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

/**
 * A secret REFERENCE. Deliberately no value field: `SecretResponse` on the backend returns the
 * key, the environment and where the material lives, never the material.
 */
export interface SecretRefUI {
  id: string;
  key: string;
  environment: string;
  infisical_path: string | null;
  is_local: boolean;
}

/**
 * The vault: list, add, rotate, delete — phases.md §1.8.
 */
export function SecretVault({
  secrets,
  projectId,
  readOnly = false,
}: {
  secrets: SecretRefUI[];
  /** Required to create. Absent on screens that only display, which is what `readOnly` expresses. */
  projectId?: string;
  /** True on screens where the vault is shown for context rather than edited. */
  readOnly?: boolean;
}) {
  const queryClient = useQueryClient();
  const [rotating, setRotating] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<SecretRefUI | null>(null);

  const invalidate = () => {
    if (projectId) {
      void queryClient.invalidateQueries({ queryKey: queryKeys.secrets.list(projectId) });
    }
  };

  const remove = useMutation({
    mutationFn: (id: string) => api.delete<void>(`/secrets/${id}`),
    onSuccess: () => {
      setDeleting(null);
      invalidate();
    },
  });

  return (
    <div className="space-y-6">
      <Card className="border border-border">
        <CardHeader>
          <div className="flex items-center justify-between">
            <CardTitle className="text-base font-semibold">Secret references</CardTitle>
            <Badge variant="outline">{secrets.length} references</Badge>
          </div>
          <CardDescription>
            References link credentials (API keys, tokens, passwords) to deployment manifests
            without exposing raw values.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          {secrets.length === 0 ? (
            <div className="rounded-lg border border-dashed border-border p-6 text-center text-sm text-muted-foreground">
              <p>
                None registered yet. Add a secret reference below to inject credentials into your
                deployment manifests.
              </p>
            </div>
          ) : (
            <ul className="divide-y divide-border rounded-lg border border-border bg-card">
              {secrets.map((secret) => (
                <li
                  key={secret.id}
                  className="flex flex-wrap items-center justify-between gap-3 p-4"
                  data-testid={`secret-${secret.id}`}
                >
                  <div className="min-w-0">
                    <p className="font-mono text-sm font-semibold text-foreground">{secret.key}</p>
                    <p className="text-xs text-muted-foreground mt-0.5">
                      Target Environment:{" "}
                      <span className="font-medium text-foreground">{secret.environment}</span>
                      {secret.infisical_path ? ` · ${secret.infisical_path}` : ""}
                    </p>
                  </div>
                  <div className="flex items-center gap-2">
                    <Badge variant="outline">{secret.is_local ? "local" : "Infisical"}</Badge>
                    {readOnly ? null : (
                      <>
                        <Button
                          type="button"
                          variant="outline"
                          size="sm"
                          onClick={() =>
                            setRotating((current) => (current === secret.id ? null : secret.id))
                          }
                          aria-expanded={rotating === secret.id}
                          data-testid={`rotate-${secret.id}`}
                        >
                          Rotate
                        </Button>
                        <Button
                          type="button"
                          variant="destructive"
                          size="sm"
                          onClick={() => setDeleting(secret)}
                          data-testid={`delete-secret-${secret.id}`}
                        >
                          Delete
                        </Button>
                      </>
                    )}
                  </div>

                  {rotating === secret.id && !readOnly ? (
                    <div className="w-full pt-3">
                      <RotateForm
                        secret={secret}
                        onDone={() => {
                          setRotating(null);
                          invalidate();
                        }}
                      />
                    </div>
                  ) : null}
                </li>
              ))}
            </ul>
          )}
          <p className="text-xs text-muted-foreground pt-1">
            References only. The API returns the key, the environment and the storage path — never
            the secret material — so there is nothing here to reveal, including for secrets created
            on this screen. A value can be written and rotated; it can never be read back.
          </p>
        </CardContent>
      </Card>

      {deleting ? (
        <div
          role="alertdialog"
          aria-labelledby="delete-secret-heading"
          className="space-y-3 rounded-lg border border-destructive/40 bg-destructive/5 p-4 text-sm"
        >
          <h3 id="delete-secret-heading" className="font-semibold text-foreground">
            Delete the reference to {deleting.key}?
          </h3>
          <p className="text-xs text-muted-foreground leading-relaxed">
            This removes the metadata record, so deployments that inject this key will stop finding
            it. For an Infisical-backed secret the material in Infisical is <strong>not</strong>{" "}
            removed — this platform does not own that store, and silently deleting from it would be
            acting outside what it manages.
          </p>
          <div className="flex gap-3 pt-1">
            <Button
              type="button"
              variant="destructive"
              size="sm"
              onClick={() => remove.mutate(deleting.id)}
              disabled={remove.isPending}
              data-testid="confirm-delete-secret"
            >
              {remove.isPending ? "Deleting…" : "Delete reference"}
            </Button>
            <Button type="button" variant="outline" size="sm" onClick={() => setDeleting(null)}>
              Cancel
            </Button>
          </div>
          <GovernanceRefusal error={remove.error} action="delete this secret reference" />
        </div>
      ) : null}

      {!readOnly && projectId ? (
        <CreateSecretForm projectId={projectId} onDone={invalidate} />
      ) : null}
    </div>
  );
}

/**
 * The value input: uncontrolled, by design.
 */
function SecretValueField({
  id,
  inputRef,
  label,
}: {
  id: string;
  inputRef: React.RefObject<HTMLInputElement | null>;
  label: string;
}) {
  return (
    <div className="space-y-1.5">
      <label htmlFor={id} className="block text-sm font-medium">
        {label}
      </label>
      <input
        id={id}
        ref={inputRef}
        type="password"
        required
        autoComplete="new-password"
        aria-describedby={`${id}-help`}
        placeholder="Paste or enter secret value"
        className="flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 font-mono text-sm shadow-sm transition-colors placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      />
      <p id={`${id}-help`} className="mt-1 text-xs text-muted-foreground">
        Write-only. This field is uncontrolled, so the value never enters application state, and it
        is cleared whether the request succeeds or fails. You will not be able to read it back — if
        you need it again, store it in your own password manager now.
      </p>
    </div>
  );
}

function CreateSecretForm({ projectId, onDone }: { projectId: string; onDone: () => void }) {
  const [key, setKey] = useState("");
  const [environment, setEnvironment] = useState("development");
  const valueRef = useRef<HTMLInputElement | null>(null);

  const create = useMutation({
    mutationFn: () => {
      const value = valueRef.current?.value ?? "";
      if (valueRef.current) valueRef.current.value = "";
      return api.post<SecretRefUI>("/secrets", {
        project_id: projectId,
        environment,
        key: key.trim(),
        value,
      });
    },
    onSuccess: () => {
      setKey("");
      onDone();
    },
  });

  return (
    <Card className="border border-border">
      <CardHeader>
        <CardTitle className="text-base font-semibold">Add a secret reference</CardTitle>
        <CardDescription>
          Enter the secret key name, target environment, and write-only value to store.
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
          <div className="grid gap-4 sm:grid-cols-2">
            <div className="space-y-1.5">
              <label htmlFor="secret-key" className="block text-sm font-medium">
                Key
              </label>
              <input
                id="secret-key"
                value={key}
                onChange={(event) => setKey(event.target.value)}
                required
                placeholder="DATABASE_PASSWORD"
                className="flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 font-mono text-sm shadow-sm transition-colors placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              />
            </div>
            <div className="space-y-1.5">
              <label htmlFor="secret-environment" className="block text-sm font-medium">
                Environment
              </label>
              <input
                id="secret-environment"
                value={environment}
                onChange={(event) => setEnvironment(event.target.value)}
                required
                placeholder="development"
                className="flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm shadow-sm transition-colors placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              />
            </div>
          </div>

          <SecretValueField id="secret-value" inputRef={valueRef} label="Value" />

          <div className="flex flex-wrap items-center gap-3 pt-2">
            <Button
              type="submit"
              disabled={key.trim() === "" || create.isPending}
              data-testid="create-secret"
              className="px-5 py-2 font-medium"
            >
              {create.isPending ? "Storing…" : "Store secret"}
            </Button>
            {create.isSuccess ? (
              <p
                role="status"
                className="text-sm text-emerald-600 dark:text-emerald-400 font-medium"
              >
                Stored. The reference is listed above; the value is not readable from here.
              </p>
            ) : null}
          </div>

          <GovernanceRefusal error={create.error} action="store this secret" />
        </form>
      </CardContent>
    </Card>
  );
}

function RotateForm({ secret, onDone }: { secret: SecretRefUI; onDone: () => void }) {
  const valueRef = useRef<HTMLInputElement | null>(null);

  const rotate = useMutation({
    mutationFn: () => {
      const value = valueRef.current?.value ?? "";
      if (valueRef.current) valueRef.current.value = "";
      return api.patch<SecretRefUI>(`/secrets/${secret.id}`, { value });
    },
    onSuccess: onDone,
  });

  return (
    <form
      className="space-y-3 rounded-lg border border-border bg-muted/30 p-4"
      onSubmit={(event) => {
        event.preventDefault();
        rotate.mutate();
      }}
    >
      <p className="text-xs text-muted-foreground leading-relaxed">
        Rotating replaces the stored material for <code>{secret.key}</code> in {secret.environment}.
        The key and the environment do not change, so nothing that injects this secret needs
        reconfiguring — which is the point of rotation being a PATCH of the value rather than a
        delete and a re-create.
      </p>
      <SecretValueField id={`rotate-value-${secret.id}`} inputRef={valueRef} label="New value" />
      <Button
        type="submit"
        size="sm"
        disabled={rotate.isPending}
        data-testid={`confirm-rotate-${secret.id}`}
      >
        {rotate.isPending ? "Rotating…" : "Rotate"}
      </Button>
      <GovernanceRefusal error={rotate.error} action="rotate this secret" />
    </form>
  );
}
