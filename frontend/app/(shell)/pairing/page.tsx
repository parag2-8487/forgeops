// SPDX-License-Identifier: FSL-1.1-ALv2
"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, queryKeys } from "@/lib/api";
import { AsyncState } from "@/components/ui/async-state";
import { GovernanceRefusal } from "@/components/ui/governance-refusal";
import { ProjectPicker } from "@/components/ui/project-picker";
import { useCapability } from "@/hooks/use-role";
import { AgentConnectPanel } from "@/features/agent/AgentConnectPanel";
import type { ProjectPage } from "@/features/projects/types";
import { CodeCountdown } from "@/features/agent/CodeCountdown";

/**
 * Mirrors `DeviceRead` in `backend/src/auth/device_read_routes.py`.
 *
 * `heartbeat_fresh` is tri-state on purpose and this screen exists because of it.
 * `features/pairing/AgentPairing.tsx` displayed a fixed `SPIFFE Trust Domain: spiffe://cluster.local`
 * and the status "Connected & Attested" with no props and no fetch — a security control reported as
 * passing by a component that could not observe it. `null` here means the device has never reported,
 * which is different from `false` ("it reported, and that was too long ago"), and a boolean cannot
 * carry that difference.
 */
interface DeviceRead {
  id: string;
  project_id: string;
  status: "pending" | "active" | "policy_stale" | "revoked" | "abandoned";
  agent_version: string;
  platform: string;
  cert_serial: string | null;
  cert_fingerprint: string | null;
  cert_not_after: string | null;
  last_seq: number;
  last_seen: string | null;
  pairing_expires_at: string | null;
  revoked_at: string | null;
  created_at: string;
  seconds_since_last_seen: number | null;
  heartbeat_fresh: boolean | null;
  heartbeat_timeout_seconds: number;
}

interface DevicePage {
  devices: DeviceRead[];
  next_cursor: string | null;
}

/** Mirrors `PairingCodeResponse`. The code exists in this response and nowhere else, ever. */
interface PairingCode {
  code: string;
  device_id: string;
  expires_at: string;
}

/** What §3.7's five states mean, so the screen explains rather than colour-codes. */
const STATUS_MEANING: Record<DeviceRead["status"], string> = {
  pending: "A pairing code has been minted and not yet exchanged.",
  active: "Paired, with a certificate issued by the internal CA.",
  policy_stale: "Paired, but its policy bundle digest no longer matches the backend's.",
  revoked: "Revoked. Its certificate and tokens are no longer accepted.",
  abandoned: "Its pairing code expired before it was exchanged.",
};

/**
 * The page size `ProjectPicker` fetches with.
 *
 * Declared here so the query below shares the picker's cache entry exactly. A different number is a
 * different key, which would mean a second request and two lists that could disagree.
 */
const PICKER_PAGE_SIZE = 100;

export default function PairingPage() {
  const [detailId, setDetailId] = useState<string | null>(null);
  const [revoking, setRevoking] = useState<DeviceRead | null>(null);

  const devices = useQuery({
    queryKey: queryKeys.devices.list(),
    queryFn: () => api.get<DevicePage>("/agents/devices?limit=100"),
    // Devices heartbeat, so a stale panel is misleading in exactly the way this screen exists to
    // avoid. Refetched on an interval rather than once on mount.
    refetchInterval: 15_000,
    retry: false,
  });

  const revoke = useCapability("revoke_device");

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Agent pairing</h1>
        <p className="mt-1 text-muted-foreground">
          Read from <code>GET /api/v1/agents/devices</code>. Every field below is an observation
          recorded in <code>agent_devices</code>, not a claim about the agent.
        </p>
      </div>

      <MintPairingCode />

      <AsyncState
        isPending={devices.isPending}
        error={devices.error}
        isEmpty={devices.data?.devices.length === 0}
        emptyMessage="No agent devices exist for this tenant. Mint a pairing code above; the panel beneath it prints the exact command to run on the machine that holds the working tree."
        label="agent devices"
      >
        <ul className="space-y-4">
          {devices.data?.devices.map((device) => (
            <li
              key={device.id}
              className="rounded-lg border border-border bg-background p-4 text-sm"
            >
              <div className="flex flex-wrap items-baseline justify-between gap-2">
                <h2 className="font-semibold">
                  {device.platform} · {device.agent_version}
                </h2>
                <code className="text-xs text-muted-foreground">{device.id}</code>
              </div>

              <dl className="mt-3 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1">
                <dt className="font-medium">Status</dt>
                <dd>
                  <span data-testid={`status-${device.id}`}>{device.status}</span>
                  <span className="ml-2 text-muted-foreground">
                    {STATUS_MEANING[device.status]}
                  </span>
                </dd>

                <dt className="font-medium">Heartbeat</dt>
                <dd data-testid={`heartbeat-${device.id}`}>
                  {device.heartbeat_fresh === null ? (
                    // The case the old component could not express. Not "disconnected" — unobserved.
                    <span>
                      Never reported. This device has no recorded heartbeat, so nothing is known
                      about whether it is running.
                    </span>
                  ) : device.heartbeat_fresh ? (
                    <span>
                      Heartbeating — last seen {device.seconds_since_last_seen}s ago, within the{" "}
                      {device.heartbeat_timeout_seconds}s timeout.
                    </span>
                  ) : (
                    <span>
                      Stale — last seen {device.seconds_since_last_seen}s ago, beyond the{" "}
                      {device.heartbeat_timeout_seconds}s timeout.
                    </span>
                  )}
                </dd>

                <dt className="font-medium">Certificate</dt>
                <dd>
                  {device.cert_fingerprint ? (
                    <>
                      <code className="break-all text-xs">{device.cert_fingerprint}</code>
                      {device.cert_not_after ? (
                        <span className="ml-2 text-muted-foreground">
                          expires <time>{device.cert_not_after}</time>
                        </span>
                      ) : null}
                    </>
                  ) : (
                    <span className="text-muted-foreground">
                      No certificate has been issued to this device.
                    </span>
                  )}
                </dd>

                <dt className="font-medium">Envelope sequence</dt>
                <dd>
                  {device.last_seq}
                  <span className="ml-2 text-muted-foreground">
                    Evidence only — Redis is the authority for replay rejection.
                  </span>
                </dd>

                {device.revoked_at ? (
                  <>
                    <dt className="font-medium">Revoked</dt>
                    <dd>
                      <time>{device.revoked_at}</time>
                    </dd>
                  </>
                ) : null}
              </dl>

              <div className="mt-3 flex flex-wrap items-center gap-2">
                <button
                  type="button"
                  aria-expanded={detailId === device.id}
                  onClick={() => setDetailId((c) => (c === device.id ? null : device.id))}
                  data-testid={`device-detail-${device.id}`}
                  className="rounded-md border border-border px-2 py-1 text-xs focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                >
                  {detailId === device.id ? "Hide detail" : "Re-read from the API"}
                </button>

                {/*
                  Removed entirely for a viewer rather than disabled, because there is nothing useful
                  a viewer can do with a revoke control — and a permanently greyed button on every row
                  is visual noise that teaches people to ignore disabled states. The reason is stated
                  once below the list instead of once per row.
                */}
                {device.revoked_at === null && revoke.allowed ? (
                  <button
                    type="button"
                    onClick={() => setRevoking(device)}
                    data-testid={`revoke-${device.id}`}
                    className="rounded-md border border-destructive/50 px-2 py-1 text-xs text-destructive focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  >
                    Revoke
                  </button>
                ) : null}
              </div>

              {detailId === device.id ? <DeviceDetail deviceId={device.id} /> : null}
            </li>
          ))}
        </ul>
      </AsyncState>

      {!revoke.allowed && revoke.reason ? (
        <p role="status" className="text-xs text-muted-foreground">
          {revoke.reason} Revocation is not offered here rather than offered and refused.
        </p>
      ) : null}

      {revoking ? (
        <RevokeDialog
          device={revoking}
          onDone={() => setRevoking(null)}
          onCancel={() => setRevoking(null)}
        />
      ) : null}

      <aside className="rounded-lg border border-border bg-muted/30 p-4 text-xs text-muted-foreground">
        <p className="font-medium text-foreground">
          Why this screen reports absence rather than status.
        </p>
        <p className="mt-2">
          Until recently the pairing routes were write-only — mint a code, exchange it, revoke a
          device — with <strong>no GET</strong>, so a paired agent could not be observed at all. The
          component that used to be here filled that gap by asserting{" "}
          <em>Connected &amp; Attested</em> and a fixed SPIFFE trust domain with no props and no
          fetch: a security control reported as passing by something that could not check it.
        </p>
        <p className="mt-2">
          So the heartbeat field distinguishes three cases, not two. <strong>Never reported</strong>{" "}
          is not the same as stale, and neither is the same as heartbeating. Attestation is not
          shown because there is no attestation surface to read; when one exists it will be a field
          here rather than a word in a heading.
        </p>
      </aside>
    </div>
  );
}

/**
 * Mint a pairing code — the endpoint whose absence made this screen's empty state a set of
 * instructions for using curl.
 *
 * THE CODE IS SHOWN ONCE, AND THAT IS A PROPERTY OF THE SYSTEM RATHER THAN A UI CHOICE. It exists in
 * the clear in exactly one place: the body of this 201. No log line carries it, no audit row carries
 * it, and the database stores only `HMAC(pepper, code)` — so there is no endpoint that could show it
 * again, and a "reveal" control would have nothing to reveal. The component therefore says so
 * plainly, keeps it in component state that dies with the panel, and never writes it anywhere.
 *
 * It is deliberately NOT put on the clipboard automatically. A credential silently placed on the
 * clipboard is a credential in every clipboard-reading application on the machine, and the operator
 * did not ask for that.
 */
function MintPairingCode() {
  const queryClient = useQueryClient();
  const [projectId, setProjectId] = useState("");

  // The chosen project's recorded working-tree path, so the printed `connect` command can carry
  // `--workspace`. Unset, the agent falls back to its current working directory — which is what every
  // path in a change set is resolved against, so it decides where an applied change WRITES rather than
  // merely what is indexed.
  //
  // THE SAME QUERY KEY `ProjectPicker` USES, so React Query serves both from one cache entry and this
  // costs no extra request. Duplicating the key with a different page size would silently double the
  // fetch and could disagree about which projects exist.
  const projects = useQuery({
    queryKey: queryKeys.projects.list(PICKER_PAGE_SIZE),
    queryFn: () => api.get<ProjectPage>(`/projects?limit=${PICKER_PAGE_SIZE}`),
    retry: false,
  });
  const selectedProjectPath = projects.data?.projects?.find((p) => p.id === projectId)?.path;
  const [issued, setIssued] = useState<PairingCode | null>(null);
  const { allowed, reason } = useCapability("mint_pairing_code");

  const mint = useMutation({
    mutationFn: () => api.post<PairingCode>("/agents/pairing-codes", { project_id: projectId }),
    onSuccess: (code) => {
      setIssued(code);
      // A pending device row now exists, so the list below is stale.
      void queryClient.invalidateQueries({ queryKey: queryKeys.devices.all });
    },
  });

  if (!allowed) {
    return (
      <section
        aria-labelledby="mint-heading"
        className="rounded-lg border border-border bg-background p-4"
      >
        <h2 id="mint-heading" className="text-sm font-semibold">
          Pair an agent
        </h2>
        <p className="mt-2 text-xs text-muted-foreground">{reason}</p>
      </section>
    );
  }

  return (
    <section
      aria-labelledby="mint-heading"
      className="space-y-3 rounded-lg border border-border bg-background p-4"
    >
      <h2 id="mint-heading" className="text-sm font-semibold">
        Pair an agent
      </h2>
      <p className="text-xs text-muted-foreground">
        A code pairs one device to one project and expires in five minutes. Minting a new code for a
        project revokes any code already live for it, so there is never more than one usable code
        per project.
      </p>

      <ProjectPicker value={projectId} onChange={setProjectId} id="pairing-project" />

      <button
        type="button"
        onClick={() => mint.mutate()}
        disabled={projectId === "" || mint.isPending}
        data-testid="mint-code"
        className="rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        {mint.isPending ? "Minting…" : "Mint a pairing code"}
      </button>

      {issued ? (
        <div
          // `role="alert"` and `aria-live="assertive"`: this is shown once and cannot be recovered, so
          // a screen-reader user must hear it now rather than on next focus.
          role="alert"
          aria-live="assertive"
          data-testid="pairing-code"
          className="space-y-2 rounded-md border border-amber-500/40 bg-amber-500/5 p-3"
        >
          <p className="text-sm font-semibold">Copy this now — it is not recoverable.</p>
          <p className="font-mono text-2xl tracking-widest" data-testid="pairing-code-value">
            {issued.code}
          </p>
          <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 text-xs text-muted-foreground">
            <dt className="font-medium">Device</dt>
            <dd>
              <code>{issued.device_id}</code>
            </dd>
          </dl>
          <p className="text-xs text-muted-foreground">
            This code exists in the clear only in the response you are reading. It is in no log, no
            audit record and no database column — only its HMAC is stored — so there is no way to
            show it again, and nothing here has saved it. If you lose it, mint another.
          </p>
          <CodeCountdown
            expiresAt={issued.expires_at}
            onRemint={() => mint.mutate()}
            isReminting={mint.isPending}
          />
        </div>
      ) : null}

      {/* The whole connect flow, with every command correct for the reader's platform.
       *
       * Rendered whether or not a code exists: a user can download the binary and put it on PATH
       * before minting, which is what makes the five-minute window comfortable instead of a race.
       * That ordering is the actual fix for code expiry — the countdown only tells you about it. */}
      <AgentConnectPanel
        code={issued?.code}
        projectId={projectId === "" ? undefined : projectId}
        workspacePath={selectedProjectPath}
      />

      <GovernanceRefusal error={mint.error} action="mint a pairing code" />
    </section>
  );
}

/**
 * Re-read one device from `GET /api/v1/agents/devices/{id}`.
 *
 * `queryKeys.devices.detail` existed and was unused, and so was the route. Worth having beside the
 * list rather than instead of it: the list is polled on an interval, and after a revoke or a pairing
 * an operator wants to know the state of ONE device right now rather than waiting up to fifteen
 * seconds for the next poll of all of them.
 */
function DeviceDetail({ deviceId }: { deviceId: string }) {
  const device = useQuery({
    queryKey: queryKeys.devices.detail(deviceId),
    queryFn: () => api.get<DeviceRead>(`/agents/devices/${deviceId}`),
    retry: false,
  });

  return (
    <div className="mt-3 border-t border-border pt-3">
      <AsyncState isPending={device.isPending} error={device.error} label="device">
        {device.data ? (
          <dl
            data-testid={`device-detail-panel-${deviceId}`}
            className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-xs"
          >
            <dt className="font-medium">Read at</dt>
            <dd className="text-muted-foreground">
              this moment, directly from the API rather than from the polled list
            </dd>
            <dt className="font-medium">Project</dt>
            <dd>
              <code>{device.data.project_id}</code>
            </dd>
            <dt className="font-medium">Created</dt>
            <dd>
              <time dateTime={device.data.created_at}>{device.data.created_at}</time>
            </dd>
            <dt className="font-medium">Certificate serial</dt>
            <dd>
              <code>{device.data.cert_serial ?? "none issued"}</code>
            </dd>
            <dt className="font-medium">Pairing code expires</dt>
            <dd>{device.data.pairing_expires_at ?? "not pending"}</dd>
            <dt className="font-medium">Last envelope sequence</dt>
            <dd>{device.data.last_seq}</dd>
          </dl>
        ) : null}
      </AsyncState>
      <p className="mt-2 text-xs text-muted-foreground">
        The token HMACs and the wrapped envelope key are columns on this row and are deliberately
        absent from the response: a read surface that returned them would turn &ldquo;list my
        devices&rdquo; into credential exfiltration.
      </p>
    </div>
  );
}

/** Revoke a device. ADMIN only, and the reason is required for the same NFR-14 reason as elsewhere. */
function RevokeDialog({
  device,
  onDone,
  onCancel,
}: {
  device: DeviceRead;
  onDone: () => void;
  onCancel: () => void;
}) {
  const queryClient = useQueryClient();
  const [reason, setReason] = useState("");

  const revoke = useMutation({
    mutationFn: () => api.deleteWith<void>(`/agents/${device.id}`, { reason: reason.trim() }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.devices.all });
      onDone();
    },
  });

  return (
    <section
      aria-labelledby="revoke-heading"
      className="space-y-3 rounded-lg border border-destructive/40 bg-destructive/5 p-4 text-sm"
    >
      <h2 id="revoke-heading" className="font-semibold">
        Revoke {device.platform} · {device.agent_version}
      </h2>
      <p className="text-xs text-muted-foreground">
        The device&apos;s certificate and tokens stop being accepted immediately — revocation is
        checked per message rather than once per connection, so an agent mid-session is cut off at
        its next envelope rather than at its next reconnect. Pairing again needs a new code.
        Idempotent: a second revoke succeeds and writes no second audit row.
      </p>
      <div>
        <label htmlFor="revoke-reason" className="block text-sm font-medium">
          Reason
        </label>
        <input
          id="revoke-reason"
          value={reason}
          onChange={(event) => setReason(event.target.value)}
          required
          maxLength={500}
          aria-describedby="revoke-reason-help"
          className="mt-1 w-full rounded-md border border-border bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        />
        <p id="revoke-reason-help" className="mt-1 text-xs text-muted-foreground">
          Required by the endpoint and written to the audit log. A revocation with no stated reason
          is the record that is useless six months later.
        </p>
      </div>
      <div className="flex gap-3">
        <button
          type="button"
          onClick={() => revoke.mutate()}
          disabled={reason.trim() === "" || revoke.isPending}
          data-testid="confirm-revoke"
          className="rounded-md bg-destructive px-3 py-1.5 text-xs font-medium text-destructive-foreground disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          {revoke.isPending ? "Revoking…" : "Revoke device"}
        </button>
        <button
          type="button"
          onClick={onCancel}
          className="rounded-md border border-border px-3 py-1.5 text-xs focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          Cancel
        </button>
      </div>
      <GovernanceRefusal error={revoke.error} action="revoke this device" />
    </section>
  );
}
