// SPDX-License-Identifier: FSL-1.1-ALv2
"use client";

import { useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api, queryKeys } from "@/lib/api";
import { GovernanceRefusal } from "@/components/ui/governance-refusal";

/**
 * A secret REFERENCE. Deliberately no value field: `SecretResponse` on the backend returns the
 * key, the environment and where the material lives, never the material. Typing it without a
 * value keeps that property visible in the client too, so a future edit cannot casually add one.
 */
export interface SecretRefUI {
  id: string;
  key: string;
  environment: string;
  infisical_path: string | null;
  is_local: boolean;
}

/**
 * The vault: list, add, rotate, delete — phases.md §1.8 "Frontend: Secret vault UI (add, edit, delete)".
 *
 * The screen only listed. `POST /api/v1/secrets`, `PATCH /api/v1/secrets/{id}` and
 * `DELETE /api/v1/secrets/{id}` were all served and all uncalled, so a secret could be read about and
 * not created.
 *
 * WRITE-ONLY IS MADE STRUCTURAL HERE, NOT PROMISED IN A COMMENT
 * The requirement is that the UI must never display or cache a secret value it wrote. Three things
 * enforce that, and none of them is discipline:
 *
 *  1. **The value never enters React state.** `SecretValueField` below is an UNCONTROLLED input read
 *     through a ref at submit time. A `useState` value would live in the component's state for the
 *     lifetime of the form, appear in a React DevTools inspection, and — the part that actually
 *     matters — be captured in any error boundary or state snapshot. There is no `value` prop and no
 *     `onChange`, so there is nowhere for it to be held.
 *  2. **The ref is cleared in the mutation, not in a success handler.** `finally`-style clearing means
 *     a failed request clears it too. Leaving it on failure so the user can retry would keep live
 *     credential material in the DOM for as long as they left the tab open, which is the exact
 *     tradeoff not worth making — retyping a secret is cheap.
 *  3. **The mutation returns `SecretResponse`, which has no value field**, and the query cache is
 *     keyed on the list endpoint that also has none. So even TanStack Query's cache cannot hold one:
 *     there is no response shape in this module that carries a value, in either direction after the
 *     request.
 *
 * `type="password"` is on the input as well, but that is only about shoulder-surfing and is the least
 * of the three.
 */
export function SecretVault({
  secrets,
  projectId,
  readOnly = false,
}: {
  secrets: SecretRefUI[];
  /** Required to create. Absent on screens that only display, which is what `readOnly` expresses. */
  projectId?: string;
  /** True on the project detail page, where the vault is shown for context rather than edited. */
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
    <div className="space-y-4">
      <div className="rounded-lg border border-border bg-background p-4">
        <h3 className="text-sm font-semibold">Secret references</h3>
        {secrets.length === 0 ? (
          <p className="mt-2 text-sm text-muted-foreground">None registered.</p>
        ) : (
          <ul className="mt-2 divide-y divide-border">
            {secrets.map((secret) => (
              <li
                key={secret.id}
                className="flex flex-wrap items-center justify-between gap-3 py-3"
                data-testid={`secret-${secret.id}`}
              >
                <div className="min-w-0">
                  <p className="font-mono text-sm font-semibold">{secret.key}</p>
                  <p className="text-xs text-muted-foreground">
                    {secret.environment}
                    {secret.infisical_path ? ` · ${secret.infisical_path}` : ""}
                  </p>
                </div>
                <div className="flex items-center gap-2">
                  <span className="rounded bg-muted px-2 py-1 text-xs">
                    {secret.is_local ? "local" : "Infisical"}
                  </span>
                  {readOnly ? null : (
                    <>
                      <button
                        type="button"
                        onClick={() =>
                          setRotating((current) => (current === secret.id ? null : secret.id))
                        }
                        aria-expanded={rotating === secret.id}
                        data-testid={`rotate-${secret.id}`}
                        className="rounded-md border border-border px-2 py-1 text-xs focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                      >
                        Rotate
                      </button>
                      <button
                        type="button"
                        onClick={() => setDeleting(secret)}
                        data-testid={`delete-secret-${secret.id}`}
                        className="rounded-md border border-destructive/50 px-2 py-1 text-xs text-destructive focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                      >
                        Delete
                      </button>
                    </>
                  )}
                </div>

                {rotating === secret.id && !readOnly ? (
                  <div className="w-full">
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
        <p className="mt-4 text-xs text-muted-foreground">
          References only. The API returns the key, the environment and the storage path — never the
          secret material — so there is nothing here to reveal, including for secrets created on
          this screen. A value can be written and rotated; it can never be read back.
        </p>
      </div>

      {deleting ? (
        <div
          role="alertdialog"
          aria-labelledby="delete-secret-heading"
          className="space-y-3 rounded-lg border border-destructive/40 bg-destructive/5 p-4 text-sm"
        >
          <h3 id="delete-secret-heading" className="font-semibold">
            Delete the reference to {deleting.key}?
          </h3>
          <p className="text-xs text-muted-foreground">
            This removes the metadata record, so deployments that inject this key will stop finding
            it. For an Infisical-backed secret the material in Infisical is <strong>not</strong>{" "}
            removed — this platform does not own that store, and silently deleting from it would be
            acting outside what it manages.
          </p>
          <div className="flex gap-3">
            <button
              type="button"
              onClick={() => remove.mutate(deleting.id)}
              disabled={remove.isPending}
              data-testid="confirm-delete-secret"
              className="rounded-md bg-destructive px-3 py-1.5 text-xs font-medium text-destructive-foreground disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              {remove.isPending ? "Deleting…" : "Delete reference"}
            </button>
            <button
              type="button"
              onClick={() => setDeleting(null)}
              className="rounded-md border border-border px-3 py-1.5 text-xs focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              Cancel
            </button>
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
 *
 * See the module docstring. Exposed as its own component so the property is enforced in one place and
 * a second form cannot reintroduce a `useState` for a secret without editing this file.
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
    <div>
      <label htmlFor={id} className="block text-sm font-medium">
        {label}
      </label>
      <input
        id={id}
        ref={inputRef}
        type="password"
        required
        autoComplete="new-password"
        // No `value`, no `onChange`. The DOM node is the only place this string exists before the
        // request, and it is cleared the moment the request settles either way.
        aria-describedby={`${id}-help`}
        className="mt-1 w-full rounded-md border border-border bg-background px-3 py-2 font-mono text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
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
      // Cleared BEFORE awaiting, so the material is out of the DOM while the request is in flight
      // rather than after it returns. The local `value` is the only live copy from here, and it dies
      // with this function.
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
    <form
      className="space-y-4 rounded-lg border border-border bg-background p-4"
      onSubmit={(event) => {
        event.preventDefault();
        create.mutate();
      }}
    >
      <h3 className="text-sm font-semibold">Add a secret</h3>

      <div className="grid gap-4 sm:grid-cols-2">
        <div>
          <label htmlFor="secret-key" className="block text-sm font-medium">
            Key
          </label>
          <input
            id="secret-key"
            value={key}
            onChange={(event) => setKey(event.target.value)}
            required
            placeholder="DATABASE_PASSWORD"
            className="mt-1 w-full rounded-md border border-border bg-background px-3 py-2 font-mono text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          />
        </div>
        <div>
          <label htmlFor="secret-environment" className="block text-sm font-medium">
            Environment
          </label>
          <input
            id="secret-environment"
            value={environment}
            onChange={(event) => setEnvironment(event.target.value)}
            required
            className="mt-1 w-full rounded-md border border-border bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          />
        </div>
      </div>

      <SecretValueField id="secret-value" inputRef={valueRef} label="Value" />

      <div className="flex items-center gap-3">
        <button
          type="submit"
          disabled={key.trim() === "" || create.isPending}
          data-testid="create-secret"
          className="rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          {create.isPending ? "Storing…" : "Store secret"}
        </button>
        {create.isSuccess ? (
          <p role="status" className="text-sm text-muted-foreground">
            Stored. The reference is listed above; the value is not readable from here.
          </p>
        ) : null}
      </div>

      <GovernanceRefusal error={create.error} action="store this secret" />
    </form>
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
      className="mt-3 space-y-3 rounded-md border border-border bg-muted/30 p-3"
      onSubmit={(event) => {
        event.preventDefault();
        rotate.mutate();
      }}
    >
      <p className="text-xs text-muted-foreground">
        Rotating replaces the stored material for <code>{secret.key}</code> in {secret.environment}.
        The key and the environment do not change, so nothing that injects this secret needs
        reconfiguring — which is the point of rotation being a PATCH of the value rather than a
        delete and a re-create.
      </p>
      <SecretValueField id={`rotate-value-${secret.id}`} inputRef={valueRef} label="New value" />
      <button
        type="submit"
        disabled={rotate.isPending}
        data-testid={`confirm-rotate-${secret.id}`}
        className="rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        {rotate.isPending ? "Rotating…" : "Rotate"}
      </button>
      <GovernanceRefusal error={rotate.error} action="rotate this secret" />
    </form>
  );
}
