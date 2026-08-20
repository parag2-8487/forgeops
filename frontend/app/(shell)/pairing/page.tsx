// SPDX-License-Identifier: FSL-1.1-ALv2
"use client";

import { useQuery } from "@tanstack/react-query";
import { api, queryKeys } from "@/lib/api";
import { AsyncState } from "@/components/ui/async-state";

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

/** What §3.7's five states mean, so the screen explains rather than colour-codes. */
const STATUS_MEANING: Record<DeviceRead["status"], string> = {
  pending: "A pairing code has been minted and not yet exchanged.",
  active: "Paired, with a certificate issued by the internal CA.",
  policy_stale: "Paired, but its policy bundle digest no longer matches the backend's.",
  revoked: "Revoked. Its certificate and tokens are no longer accepted.",
  abandoned: "Its pairing code expired before it was exchanged.",
};

export default function PairingPage() {
  const devices = useQuery({
    queryKey: queryKeys.devices.list(),
    queryFn: () => api.get<DevicePage>("/agents/devices?limit=100"),
    // Devices heartbeat, so a stale panel is misleading in exactly the way this screen exists to
    // avoid. Refetched on an interval rather than once on mount.
    refetchInterval: 15_000,
    retry: false,
  });

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Agent pairing</h1>
        <p className="mt-1 text-muted-foreground">
          Read from <code>GET /api/v1/agents/devices</code>. Every field below is an observation
          recorded in <code>agent_devices</code>, not a claim about the agent.
        </p>
      </div>

      <AsyncState
        isPending={devices.isPending}
        error={devices.error}
        isEmpty={devices.data?.devices.length === 0}
        emptyMessage="No agent devices exist for this tenant. Pairing is agent-initiated: mint a code with POST /api/v1/agents/pairing-codes, then run `forgeops-agent pair --code <code>`."
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
            </li>
          ))}
        </ul>
      </AsyncState>

      <aside className="rounded-lg border border-border bg-muted/30 p-4 text-xs text-muted-foreground">
        <p className="font-medium text-foreground">
          Why this screen reports absence rather than status.
        </p>
        <p className="mt-2">
          Until this pass the pairing routes were write-only — mint a code, exchange it, revoke a
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
