"use client";

/**
 * Everything a user needs to get an agent connected, on one screen, with nothing to edit.
 *
 * WHAT THIS REPLACES. Connecting required: open a terminal, cd into the source tree, know Go is
 * installed, `go build -o forgeops-agent.exe ./cmd/agent`, know to prefix `.\`, know the backend
 * URL, set an environment variable, and beat a five-minute clock. Building from source should not
 * be part of a first run at all.
 *
 * Now: download the signed binary for the detected platform, one command to put it on PATH, one
 * command to connect. Each is rendered for the user's shell and copied with a button.
 */

import { useQuery } from "@tanstack/react-query";
import { useState, useSyncExternalStore } from "react";

import { api } from "@/lib/api";

import { CommandBlock } from "./CommandBlock";
import {
  archiveName,
  detectPlatform,
  installOnPathCommand,
  PLATFORM_LABELS,
  renderCommand,
  SHELL_LABELS,
  shellsFor,
  type Platform,
  type Shell,
} from "./commands";

/** What the backend reports about how to reach it. */
export interface AgentConnectionInfo {
  backend_ws_url: string;
  agent_ws_path: string;
  release_tag: string;
  download_base_url: string;
}

const PLATFORMS: readonly Platform[] = ["windows", "macos", "linux"];

/**
 * Read the connection details from the backend.
 *
 * NOT HARDCODED, and that is the point. The correct `--backend` for a deployment is a fact about
 * that deployment: the development stack answers on `BACKEND_PORT=18000`, so the value is
 * `ws://localhost:18000/api/v1/ws/agent`, and a literal in the UI would be wrong for anybody who
 * publishes on another port. The backend computes it from its own settings.
 */
export function useConnectionInfo() {
  return useQuery({
    queryKey: ["agents", "connection-info"] as const,
    queryFn: () => api.get<AgentConnectionInfo>("/agents/connection-info"),
    // The deployment's own port does not change while a screen is open.
    staleTime: 5 * 60 * 1000,
    retry: false,
  });
}

export interface AgentConnectPanelProps {
  /** The pairing code, when one has been minted. Without it the commands are still shown, as a preview. */
  code?: string;
  /** The project to index. Optional: `connect` defaults to the project the code was minted for. */
  projectId?: string;
  /** Override platform detection. Used by tests, and by the picker below. */
  platform?: Platform;
}

/**
 * The platform this browser is on, read without a hydration mismatch.
 *
 * `useSyncExternalStore` rather than an effect that calls `setState`. `navigator` does not exist
 * during the server render, so reading it at render time would crash there, and reading it in an
 * effect causes the cascading render the lint rule objects to. This is the primitive built for
 * exactly this shape: a value that comes from outside React, never changes, and needs a distinct
 * answer on the server.
 *
 * The server snapshot is `linux`, because that is the one platform whose rendered command is
 * identical whether the guess was right or not — guessing towards Windows would flash `.exe` at a
 * macOS user before hydration corrected it.
 */
function usePlatform(override?: Platform): Platform {
  const detected = useSyncExternalStore(
    // Never changes for the life of the page, so there is nothing to subscribe to.
    () => () => {},
    () => detectPlatform(),
    () => "linux" as Platform,
  );
  return override ?? detected;
}

export function AgentConnectPanel({ code, projectId, platform }: AgentConnectPanelProps) {
  const info = useConnectionInfo();
  const detected = usePlatform(platform);
  const [picked, setPicked] = useState<Platform | undefined>(undefined);
  const [shell, setShell] = useState<Shell | undefined>(undefined);

  const active: Platform = picked ?? detected;
  const shells = shellsFor(active);
  const activeShell: Shell = shell !== undefined && shells.includes(shell) ? shell : shells[0];

  const connectCommand = renderCommand({
    verb: "connect",
    platform: active,
    shell: activeShell,
    location: { kind: "on-path" },
    flags: {
      code: code ?? "<paste the code above>",
      backend: info.data?.backend_ws_url || "<loading>",
      ...(projectId === undefined ? {} : { project: projectId }),
    },
  });

  const doctorCommand = renderCommand({
    verb: "doctor",
    platform: active,
    shell: activeShell,
    location: { kind: "on-path" },
  });

  return (
    <section
      aria-labelledby="connect-heading"
      data-testid="agent-connect-panel"
      className="space-y-4 rounded-lg border border-border bg-background p-4"
    >
      <div>
        <h2 id="connect-heading" className="text-sm font-semibold">
          Connect an agent
        </h2>
        <p className="text-xs text-muted-foreground">
          Three steps, and every command below is correct for your platform and can be pasted without
          editing it. You do not need Go, and you do not need to build anything.
        </p>
      </div>

      <div className="flex flex-wrap items-end gap-3">
        <label className="flex flex-col gap-1 text-xs">
          <span className="font-medium">Platform</span>
          <select
            value={active}
            onChange={(event) => {
              setPicked(event.target.value as Platform);
              setShell(undefined);
            }}
            data-testid="platform-picker"
            className="rounded-md border border-border bg-background px-2 py-1 text-xs focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            {PLATFORMS.map((p) => (
              <option key={p} value={p}>
                {PLATFORM_LABELS[p]}
              </option>
            ))}
          </select>
        </label>

        {shells.length > 1 ? (
          <label className="flex flex-col gap-1 text-xs">
            <span className="font-medium">Shell</span>
            <select
              value={activeShell}
              onChange={(event) => setShell(event.target.value as Shell)}
              data-testid="shell-picker"
              className="rounded-md border border-border bg-background px-2 py-1 text-xs focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              {shells.map((s) => (
                <option key={s} value={s}>
                  {SHELL_LABELS[s]}
                </option>
              ))}
            </select>
          </label>
        ) : null}
      </div>

      <ol className="space-y-3 text-xs">
        <li className="space-y-1">
          <p className="font-medium">1. Download the agent</p>
          <DownloadOffer info={info.data} platform={active} isPending={info.isPending} />
        </li>

        <li className="space-y-1">
          <p className="font-medium">2. Put it on your PATH</p>
          <CommandBlock
            command={installOnPathCommand(active, activeShell)}
            caption="Run this from the directory you downloaded it to. After this, the bare command works from anywhere."
            testId="install-command"
          />
        </li>

        <li className="space-y-1">
          <p className="font-medium">3. Connect</p>
          <CommandBlock
            command={connectCommand}
            caption="Pairs, indexes this machine's working tree, and stays running to apply approved change sets."
            testId="connect-command"
          />
          {code === undefined ? (
            <p className="text-xs text-muted-foreground">
              Mint a pairing code above and this command will carry it.
            </p>
          ) : null}
        </li>
      </ol>

      <details className="text-xs">
        <summary className="cursor-pointer font-medium">Something went wrong?</summary>
        <div className="mt-2 space-y-2">
          <CommandBlock
            command={doctorCommand}
            caption="Reports Docker, Kubernetes, OpenTofu, the credential store — including whether a device credential will fit in it — and the pairing state."
            testId="doctor-command"
          />
        </div>
      </details>
    </section>
  );
}

/**
 * Offer the signed archive for the detected platform.
 *
 * Both architectures are offered rather than guessed. A browser cannot reliably report whether it is
 * on arm64: Apple Silicon Macs running an Intel-emulated browser say `x86_64`, and Windows on ARM
 * reports amd64 for compatibility. Handing a user the wrong architecture produces a binary that will
 * not start, so the choice is theirs and is labelled.
 */
function DownloadOffer({
  info,
  platform,
  isPending,
}: {
  info: AgentConnectionInfo | undefined;
  platform: Platform;
  isPending: boolean;
}) {
  if (isPending) {
    return <p className="text-muted-foreground">Reading this deployment&rsquo;s release…</p>;
  }
  // GUARDED ON ABSENCE, not on the empty string. `info` can be `null` from an errored query, and an
  // individual field can be `undefined` if the response does not carry it — a deployment on an older
  // backend, or a proxy returning something unexpected. `=== ""` was true for neither, so both fell
  // through to `info.download_base_url.replace(...)` and threw, taking the whole panel down with them.
  //
  // A download offer failing must never remove the connect command beside it, which is the part the
  // user actually needs.
  if (!info?.release_tag || !info.download_base_url) {
    // Said plainly rather than linking to a guess. A download link built from a tag this deployment
    // does not pin would 404, and "the page is broken" is a worse first experience than "this
    // deployment does not offer one".
    return (
      <p data-testid="download-unavailable" className="text-muted-foreground">
        This deployment does not publish an agent download. Ask whoever operates it for the
        <code className="mx-1">forgeops-agent</code>
        binary, or build it from the repository with <code>make build-agent</code>.
      </p>
    );
  }

  const base = info.download_base_url.replace(/\/$/, "");
  return (
    <div data-testid="download-offer" className="space-y-1">
      <div className="flex flex-wrap gap-2">
        {(["amd64", "arm64"] as const).map((arch) => (
          <a
            key={arch}
            href={`${base}/${info.release_tag}/${archiveName(platform, info.release_tag, arch)}`}
            data-testid={`download-${platform}-${arch}`}
            className="rounded-md border border-border px-2 py-1 font-medium hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            {PLATFORM_LABELS[platform]} {arch === "amd64" ? "Intel/AMD 64-bit" : "ARM 64-bit"}
          </a>
        ))}
      </div>
      <p className="text-muted-foreground">
        Release <code>{info.release_tag}</code>, signed with Cosign and published with a CycloneDX
        SBOM. Checksums and signatures are on the release page.
      </p>
    </div>
  );
}
