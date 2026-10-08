"use client";

/**
 * §2.4's Docker management dashboard.
 *
 * THE ONE THING THIS SCREEN MUST NOT DO. Every panel here shows a number a human will act on — restart
 * that container, remove that image, the host is out of memory — and a measurement this screen could not
 * obtain must never render as a measurement of zero. So three states are distinguished everywhere, and the
 * words are different in each case:
 *
 *   never reported   the agent did not sample this. Rendered as "not measured", never as 0%.
 *   stale            the agent sampled it, and the sample is older than the panel's freshness window.
 *                    Rendered with its age, because a number from four minutes ago is still information —
 *                    it is just not the current information.
 *   healthy/current  sampled within the window.
 *
 * The tri-state comes from the wire, not from this component's imagination: every measured field is
 * `number | null`, and `stats_sampled` says whether a sample was requested at all. A `0` here is a real
 * zero — an idle container — and is shown as one.
 *
 * WHY STATS ARE OPT-IN. `docker stats` costs about a second of wall time per call, so the list asks
 * without it and the measurement panel asks with it. The two answers are cached under different keys,
 * because serving the statless answer to the panel that asked to measure would show every figure as "not
 * measured" on a host that is reporting perfectly well.
 *
 * WHY EVERY ACTION BUTTON REPORTS A GOVERNANCE OUTCOME RATHER THAN A RESULT. A container action is a
 * mutation: it travels the chokepoint, and it can be delivered, held for a human, or blocked. The button
 * therefore reports which of those happened. A spinner that resolved into "restarted" would be a lie in
 * two cases out of three — and the case it would lie about most often is the one where an approver is
 * waiting.
 *
 * WHY `remove` ASKS FOR CONFIRMATION AND `restart` DOES NOT. Removing a container destroys state that
 * nothing here can restore; restarting one is reversible by doing it again. The confirmation is on the
 * irreversible action only, because a confirmation on everything is a dialog people learn to dismiss.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Fragment, useState } from "react";

import { ContainerLogs } from "@/features/hostops/LogPanel";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { api, queryKeys } from "@/lib/api";

/** How old a sample may be before the panel calls it stale. */
const FRESHNESS_WINDOW_MS = 90_000;

export type DockerContainer = {
  id: string;
  name: string;
  image: string;
  state: string;
  status: string;
  ports: string;
  /**
   * `null` means no sample was taken. NOT zero — an idle container reports 0 and an unmeasured one
   * reports nothing, and a panel that showed both as "0%" would be the defect this file exists to avoid.
   */
  cpu_percent: number | null;
  memory_bytes: number | null;
  memory_limit_bytes: number | null;
  network_rx_bytes: number | null;
  network_tx_bytes: number | null;
};

export type DockerImage = {
  id: string;
  repository: string;
  tag: string;
  size: string;
  created_at: string;
};

export type DockerNamed = { name: string; driver: string };

export type DockerInventory = {
  containers: DockerContainer[];
  images: DockerImage[];
  volumes: DockerNamed[];
  networks: DockerNamed[];
  docker_version: string;
  /** RFC 3339, from the host that took the reading. Not the time this browser rendered it. */
  observed_at: string;
  stats_sampled: boolean;
};

type ActionAccepted = {
  change_set_id: string;
  status: string;
  outcome: string;
  blast_radius_score: number | null;
  blast_radius_verdict: string | null;
};

/** The three freshness states, derived from the agent's own timestamp. */
export type Freshness =
  | { kind: "never-reported" }
  | { kind: "stale"; ageSeconds: number }
  | { kind: "current"; ageSeconds: number };

/**
 * Classify a reading's age.
 *
 * Exported because the Kubernetes dashboard needs the identical rule and two copies of a freshness
 * threshold is how one panel comes to call four minutes fresh while its neighbour calls it stale.
 */
export function freshnessOf(
  observedAt: string | null | undefined,
  now: number = Date.now(),
): Freshness {
  if (!observedAt) return { kind: "never-reported" };
  const parsed = Date.parse(observedAt);
  // AN UNPARSABLE TIMESTAMP IS "NEVER REPORTED", not "now". Treating it as current would be the one
  // mistake that makes a stale panel look live.
  if (Number.isNaN(parsed)) return { kind: "never-reported" };
  const ageSeconds = Math.max(0, Math.round((now - parsed) / 1000));
  return now - parsed > FRESHNESS_WINDOW_MS
    ? { kind: "stale", ageSeconds }
    : { kind: "current", ageSeconds };
}

/** Render a measured figure, or say plainly that it was not measured. */
export function measurement(value: number | null, render: (value: number) => string): string {
  return value === null ? "not measured" : render(value);
}

function bytes(value: number): string {
  const units = ["B", "KiB", "MiB", "GiB"];
  let scaled = value;
  let unit = 0;
  while (scaled >= 1024 && unit < units.length - 1) {
    scaled /= 1024;
    unit += 1;
  }
  return `${scaled.toFixed(scaled < 10 && unit > 0 ? 1 : 0)} ${units[unit]}`;
}

function FreshnessBadge({ freshness }: { freshness: Freshness }) {
  // THE WORDS ARE DIFFERENT IN EACH STATE, because that is the whole contract. A shared colour with a
  // different shade would not survive a screenshot, a colour-blind reader, or a screen reader.
  if (freshness.kind === "never-reported") {
    return (
      <span data-testid="docker-freshness" className="text-sm text-slate-500">
        never reported
      </span>
    );
  }
  if (freshness.kind === "stale") {
    return (
      <span data-testid="docker-freshness" className="text-sm text-amber-700">
        stale — last reported {freshness.ageSeconds}s ago
      </span>
    );
  }
  return (
    <span data-testid="docker-freshness" className="text-sm text-emerald-700">
      reported {freshness.ageSeconds}s ago
    </span>
  );
}

export function DockerDashboard({ projectId }: { projectId: string }) {
  const queryClient = useQueryClient();
  const [withStats, setWithStats] = useState(true);
  const [pendingRemoval, setPendingRemoval] = useState<string | null>(null);
  const [showLogs, setShowLogs] = useState<string | null>(null);
  const [lastOutcome, setLastOutcome] = useState<string | null>(null);

  const inventory = useQuery<DockerInventory>({
    queryKey: queryKeys.hostops.dockerInventory(projectId, withStats),
    queryFn: () =>
      api.get<DockerInventory>(
        `/projects/${projectId}/docker/inventory${withStats ? "?stats=true" : "?stats=false"}`,
      ),
    refetchInterval: withStats ? 5000 : false,
  });

  const containerAction = useMutation<ActionAccepted, Error, { action: string; container: string }>(
    {
      mutationFn: (body) =>
        api.post<ActionAccepted>(`/projects/${projectId}/docker/containers/actions`, body),
      onSuccess: (accepted) => {
        setLastOutcome(accepted.outcome);
        setPendingRemoval(null);
        // Invalidated rather than optimistically updated: the action may be waiting for a human, and
        // writing the hoped-for state into the cache would show a change that has not happened.
        void queryClient.invalidateQueries({ queryKey: queryKeys.hostops.all });
      },
    },
  );

  // The build form's fields. Local state rather than a form library: three optional strings, and the
  // submit is disabled until the tag is present because a build with no tag produces a dangling image
  // nothing can reference.
  const [buildTag, setBuildTag] = useState("");
  const [buildContext, setBuildContext] = useState("");
  const [buildDockerfile, setBuildDockerfile] = useState("");

  const imageAction = useMutation<
    ActionAccepted,
    Error,
    { action: string; image: string; build_context?: string; dockerfile?: string }
  >({
    mutationFn: (body) =>
      api.post<ActionAccepted>(`/projects/${projectId}/docker/images/actions`, body),
    onSuccess: (accepted) => {
      setLastOutcome(accepted.outcome);
      void queryClient.invalidateQueries({ queryKey: queryKeys.hostops.all });
    },
  });

  if (inventory.isLoading) {
    // LOADING IS NOT EMPTY. Rendering an empty table here would read as "this host runs nothing", and a
    // slow agent would be indistinguishable from a bare machine.
    return (
      <section aria-label="Docker" data-testid="docker-dashboard">
        <p data-testid="docker-loading">Asking the agent what this host is running…</p>
      </section>
    );
  }

  if (inventory.isError) {
    // AND AN ERROR IS NOT EMPTY EITHER. The message says what failed, because "no containers" and "the
    // agent did not answer" send an operator to completely different places.
    return (
      <section
        aria-label="Docker"
        data-testid="docker-dashboard"
        className="rounded-lg border border-border bg-card p-5 space-y-3"
      >
        <div className="flex items-center justify-between">
          <h3 className="font-semibold text-base">Host containers</h3>
          <span className="rounded bg-amber-500/10 text-amber-600 dark:text-amber-400 text-xs px-2 py-0.5 font-medium">
            Agent Disconnected
          </span>
        </div>
        <p data-testid="docker-error" role="alert" className="text-sm text-muted-foreground">
          The agent did not report this host&apos;s containers: {inventory.error.message}. This is
          not the same as the host running nothing.
        </p>
        <button
          type="button"
          onClick={() => void inventory.refetch()}
          className="inline-flex items-center justify-center rounded-md border border-border bg-background px-3 py-1.5 text-xs font-medium hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          Ask again
        </button>
      </section>
    );
  }

  const raw = inventory.data;
  if (!raw) return null;
  const data: DockerInventory = {
    ...raw,
    containers: raw.containers ?? [],
    images: raw.images ?? [],
    volumes: raw.volumes ?? [],
    networks: raw.networks ?? [],
  };
  const freshness = freshnessOf(data.observed_at);

  return (
    <section aria-label="Docker" data-testid="docker-dashboard" className="space-y-6">
      <Card className="p-5">
        <header className="space-y-3">
          <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border pb-3">
            <div>
              <h2 className="text-xl font-semibold tracking-tight">Containers on this machine</h2>
              <p className="flex items-center gap-2 pt-1 font-mono text-xs text-muted-foreground">
                <span data-testid="docker-version">Docker {data.docker_version}</span>
                <span>•</span>
                <FreshnessBadge freshness={freshness} />
              </p>
            </div>
            <label className="flex items-center gap-2 text-xs font-medium text-muted-foreground cursor-pointer select-none rounded-md border border-border bg-muted/20 px-3 py-1.5 hover:bg-muted/40 transition-colors">
              <input
                type="checkbox"
                checked={withStats}
                onChange={(event) => setWithStats(event.target.checked)}
                data-testid="docker-stats-toggle"
                className="h-3.5 w-3.5 rounded border-border"
              />
              Sample CPU, memory and network (adds about a second)
            </label>
          </div>
          {!data.stats_sampled && (
            <p data-testid="docker-stats-absent" className="text-xs text-muted-foreground">
              No resource sample was taken, so the figures below read “not measured” rather than zero.
            </p>
          )}
        </header>

        {lastOutcome && (
          <p
            data-testid="docker-last-outcome"
            role="status"
            className="mt-3 rounded-md border border-primary/20 bg-primary/10 p-2.5 text-xs text-foreground font-medium"
          >
            {lastOutcome === "applying"
              ? "Sent to the agent."
              : lastOutcome === "approval-required"
                ? "Waiting for a human to approve it. Nothing has changed on the host yet."
                : `The governance gate answered: ${lastOutcome}.`}
          </p>
        )}
      </Card>

      <Card className="p-5 space-y-3">
        <div className="flex items-center justify-between">
          <h3 className="text-base font-semibold tracking-tight">Containers</h3>
          <Badge variant="outline">{data.containers.length} total</Badge>
        </div>
        {data.containers.length === 0 ? (
          <p data-testid="docker-containers-empty" className="text-sm text-muted-foreground py-4 text-center">
            The agent reached the daemon and it is running no containers.
          </p>
        ) : (
          <div className="overflow-x-auto rounded-md border border-border">
            <table data-testid="docker-containers" className="w-full text-left border-collapse text-xs">
              <thead>
                <tr className="border-b border-border bg-muted/40 font-mono text-[11px] text-muted-foreground uppercase tracking-wider">
                  <th scope="col" className="p-2.5">Name</th>
                  <th scope="col" className="p-2.5">State</th>
                  <th scope="col" className="p-2.5">Image</th>
                  <th scope="col" className="p-2.5">Ports</th>
                  <th scope="col" className="p-2.5">CPU</th>
                  <th scope="col" className="p-2.5">Memory</th>
                  <th scope="col" className="p-2.5">Network in / out</th>
                  <th scope="col" className="p-2.5 text-right">Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border/60">
                {Object.entries(
                  data.containers.reduce<Record<string, DockerContainer[]>>((acc, c) => {
                    const low = c.name.toLowerCase();
                    let group = "Standalone Containers";
                    if (low.startsWith("portfolio")) group = "Project: Portfolio";
                    else if (low.startsWith("forgeops")) group = "System: ForgeOps Platform";
                    else if (low.startsWith("worknest")) group = "Project: Worknest";
                    else {
                      const prefix = c.name.split(/[-_]/)[0];
                      if (prefix && prefix !== c.name) group = `Project: ${prefix}`;
                    }
                    acc[group] = acc[group] ?? [];
                    acc[group].push(c);
                    return acc;
                  }, {})
                ).map(([groupName, groupContainers]) => (
                  <Fragment key={groupName}>
                    <tr className="bg-muted/50 border-y border-border">
                      <td colSpan={8} className="p-2 font-semibold text-xs text-foreground">
                        <div className="flex items-center gap-2">
                          <span className="font-mono text-xs font-bold text-foreground">{groupName}</span>
                          <Badge variant="outline" className="text-[10px]">
                            {groupContainers.length} container{groupContainers.length === 1 ? "" : "s"}
                          </Badge>
                        </div>
                      </td>
                    </tr>
                    {groupContainers.map((container) => {
                      const matches = container.ports ? (container.ports.match(/(?:0\.0\.0\.0|127\.0\.0\.1|\[::\])?:?(\d+)->/g) || []) : [];
                      const hostPorts = Array.from(new Set(matches.map((m) => m.replace(/[^0-9]/g, "")).filter(Boolean)));
                      return (
                        <tr key={container.id} data-testid={`docker-container-${container.name}`} className="hover:bg-muted/20 transition-colors">
                          <td className="p-2.5 font-medium font-mono text-foreground">{container.name}</td>
                          <td data-testid={`docker-state-${container.name}`} className="p-2.5">
                            <Badge
                              variant={container.state === "running" ? "success" : "outline"}
                              className="text-[10px] uppercase font-mono tracking-wider"
                            >
                              {container.state}
                            </Badge>
                          </td>
                          <td className="p-2.5 font-mono text-muted-foreground truncate max-w-[180px]" title={container.image}>
                            {container.image}
                          </td>
                          <td data-testid={`docker-ports-${container.name}`} className="p-2.5">
                            {hostPorts.length > 0 ? (
                              <div className="flex flex-wrap gap-1 items-center">
                                {hostPorts.map((port) => (
                                  <a
                                    key={port}
                                    href={`http://localhost:${port}`}
                                    target="_blank"
                                    rel="noopener noreferrer"
                                    className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded bg-primary/10 text-primary hover:bg-primary/20 font-semibold font-mono text-[11px] transition-colors"
                                    title={`Open http://localhost:${port}`}
                                  >
                                    :{port} ↗
                                  </a>
                                ))}
                              </div>
                            ) : (
                              <span className="font-mono text-muted-foreground truncate max-w-[120px] inline-block" title={container.ports || ""}>
                                {container.ports || "—"}
                              </span>
                            )}
                          </td>
                          <td data-testid={`docker-cpu-${container.name}`} className="p-2.5 font-mono">
                            {measurement(container.cpu_percent, (value) => `${value.toFixed(2)}%`)}
                          </td>
                          <td data-testid={`docker-memory-${container.name}`} className="p-2.5 font-mono text-muted-foreground">
                            {measurement(container.memory_bytes, (value) =>
                              container.memory_limit_bytes === null
                                ? bytes(value)
                                : `${bytes(value)} of ${bytes(container.memory_limit_bytes)}`,
                            )}
                          </td>
                          <td data-testid={`docker-network-${container.name}`} className="p-2.5 font-mono text-muted-foreground">
                            {measurement(container.network_rx_bytes, bytes)} /{" "}
                            {measurement(container.network_tx_bytes, bytes)}
                          </td>
                          <td className="p-2.5 text-right">
                            <div className="flex flex-wrap items-center justify-end gap-1.5">
                              {(["start", "stop", "restart"] as const).map((action) => (
                                <button
                                  key={action}
                                  type="button"
                                  data-testid={`docker-${action}-${container.name}`}
                                  disabled={containerAction.isPending}
                                  onClick={() => containerAction.mutate({ action, container: container.name })}
                                  className="inline-flex items-center px-2 py-0.5 text-xs font-medium rounded border border-border bg-background hover:bg-muted text-foreground transition-colors disabled:opacity-40"
                                >
                                  {action}
                                </button>
                              ))}
                              {/* The irreversible one, and the only one that asks. */}
                              <button
                                type="button"
                                data-testid={`docker-logs-${container.name}`}
                                onClick={() => setShowLogs(showLogs === container.name ? null : container.name)}
                                className="inline-flex items-center px-2 py-0.5 text-xs font-medium rounded border border-border bg-background hover:bg-muted text-foreground transition-colors"
                              >
                                {showLogs === container.name ? "hide logs" : "logs"}
                              </button>
                              {showLogs === container.name && (
                                <div className="w-full mt-2 text-left">
                                  <ContainerLogs projectId={projectId} container={container.name} />
                                </div>
                              )}
                              {pendingRemoval === container.name ? (
                                <div className="flex items-center gap-1.5 p-1 rounded border border-destructive/30 bg-destructive/10 text-xs">
                                  <span data-testid={`docker-remove-confirm-${container.name}`} className="text-destructive font-medium px-1">
                                    Remove {container.name}? Anything it holds that is not in a volume is lost.
                                  </span>
                                  <button
                                    type="button"
                                    data-testid={`docker-remove-yes-${container.name}`}
                                    onClick={() =>
                                      containerAction.mutate({ action: "remove", container: container.name })
                                    }
                                    className="px-2 py-0.5 rounded bg-destructive text-destructive-foreground font-semibold hover:bg-destructive/90 transition-colors"
                                  >
                                    Remove it
                                  </button>
                                  <button
                                    type="button"
                                    onClick={() => setPendingRemoval(null)}
                                    className="px-2 py-0.5 rounded border border-border hover:bg-background transition-colors"
                                  >
                                    Keep it
                                  </button>
                                </div>
                              ) : (
                                <button
                                  type="button"
                                  data-testid={`docker-remove-${container.name}`}
                                  onClick={() => setPendingRemoval(container.name)}
                                  className="inline-flex items-center px-2 py-0.5 text-xs font-medium rounded border border-destructive/40 bg-destructive/10 text-destructive hover:bg-destructive/20 transition-colors"
                                >
                                  remove
                                </button>
                              )}
                            </div>
                          </td>
                        </tr>
                      );
                    })}
                  </Fragment>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <Card className="p-5 space-y-3">
        <div className="flex items-center justify-between">
          <h3 className="text-base font-semibold tracking-tight">Images</h3>
          <Badge variant="outline">{data.images.length} images</Badge>
        </div>
        {data.images.length === 0 ? (
          <p data-testid="docker-images-empty" className="text-sm text-muted-foreground py-4 text-center">
            The daemon holds no images.
          </p>
        ) : (
          <div className="overflow-x-auto rounded-md border border-border">
            <table data-testid="docker-images" className="w-full text-left border-collapse text-xs">
              <thead>
                <tr className="border-b border-border bg-muted/40 font-mono text-[11px] text-muted-foreground uppercase tracking-wider">
                  <th scope="col" className="p-2.5">Repository</th>
                  <th scope="col" className="p-2.5">Tag</th>
                  <th scope="col" className="p-2.5">Size</th>
                  <th scope="col" className="p-2.5 text-right">Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border/60">
                {data.images.map((image) => {
                  const reference = `${image.repository}:${image.tag}`;
                  return (
                    <tr key={image.id} data-testid={`docker-image-${image.repository}`} className="hover:bg-muted/20 transition-colors">
                      <td className="p-2.5 font-medium font-mono text-foreground">{image.repository}</td>
                      <td className="p-2.5 font-mono text-muted-foreground">{image.tag}</td>
                      <td className="p-2.5 font-mono text-muted-foreground">{image.size}</td>
                      <td className="p-2.5 text-right">
                        <div className="flex items-center justify-end gap-1.5">
                          <button
                            type="button"
                            data-testid={`docker-pull-${image.repository}`}
                            onClick={() => imageAction.mutate({ action: "pull", image: reference })}
                            className="inline-flex items-center px-2 py-0.5 text-xs font-medium rounded border border-border bg-background hover:bg-muted text-foreground transition-colors"
                          >
                            pull
                          </button>
                          <button
                            type="button"
                            data-testid={`docker-push-${image.repository}`}
                            onClick={() => imageAction.mutate({ action: "push", image: reference })}
                            className="inline-flex items-center px-2 py-0.5 text-xs font-medium rounded border border-border bg-background hover:bg-muted text-foreground transition-colors"
                          >
                            push
                          </button>
                          <button
                            type="button"
                            data-testid={`docker-rmi-${image.repository}`}
                            onClick={() => imageAction.mutate({ action: "remove", image: reference })}
                            className="inline-flex items-center px-2 py-0.5 text-xs font-medium rounded border border-destructive/40 bg-destructive/10 text-destructive hover:bg-destructive/20 transition-colors"
                          >
                            remove
                          </button>
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <Card className="p-5 space-y-3">
        <div>
          <h3 className="text-base font-semibold tracking-tight">Build an image</h3>
          <p className="text-xs text-muted-foreground">
            A build takes a tag and paths contained in the workspace root.
          </p>
        </div>
        <form
          data-testid="docker-build-form"
          onSubmit={(event) => {
            event.preventDefault();
            if (buildTag.trim() === "") {
              return;
            }
            imageAction.mutate({
              action: "build",
              image: buildTag.trim(),
              ...(buildContext.trim() === "" ? {} : { build_context: buildContext.trim() }),
              ...(buildDockerfile.trim() === "" ? {} : { dockerfile: buildDockerfile.trim() }),
            });
          }}
          className="space-y-4 pt-1"
        >
          <div className="grid gap-3 sm:grid-cols-3">
            <div className="space-y-1">
              <label htmlFor="docker-build-tag" className="block text-xs font-medium text-foreground">
                Tag <span className="text-destructive">*</span>
              </label>
              <input
                id="docker-build-tag"
                data-testid="docker-build-tag"
                value={buildTag}
                onChange={(event) => setBuildTag(event.target.value)}
                placeholder="registry.example.com/app:v1"
                className="w-full h-8 px-2.5 text-xs rounded-md border border-border bg-background focus:outline-none focus:ring-1 focus:ring-primary"
              />
            </div>
            <div className="space-y-1">
              <label htmlFor="docker-build-context" className="block text-xs font-medium text-foreground">
                Context (optional, relative to root)
              </label>
              <input
                id="docker-build-context"
                data-testid="docker-build-context"
                value={buildContext}
                onChange={(event) => setBuildContext(event.target.value)}
                className="w-full h-8 px-2.5 text-xs rounded-md border border-border bg-background focus:outline-none focus:ring-1 focus:ring-primary"
              />
            </div>
            <div className="space-y-1">
              <label htmlFor="docker-build-dockerfile" className="block text-xs font-medium text-foreground">
                Dockerfile (optional, relative to context)
              </label>
              <input
                id="docker-build-dockerfile"
                data-testid="docker-build-dockerfile"
                value={buildDockerfile}
                onChange={(event) => setBuildDockerfile(event.target.value)}
                className="w-full h-8 px-2.5 text-xs rounded-md border border-border bg-background focus:outline-none focus:ring-1 focus:ring-primary"
              />
            </div>
          </div>
          <button
            type="submit"
            data-testid="docker-build-submit"
            disabled={buildTag.trim() === ""}
            className="inline-flex items-center justify-center rounded-md bg-primary px-4 py-1.5 text-xs font-medium text-primary-foreground hover:bg-primary/90 transition-colors disabled:opacity-50"
          >
            build
          </button>
        </form>
      </Card>

      <div className="grid gap-4 md:grid-cols-2">
        <Card className="p-4 space-y-2">
          <div className="flex items-center justify-between">
            <h3 className="font-semibold text-sm">Volumes</h3>
            <Badge variant="outline">{data.volumes.length}</Badge>
          </div>
          <div className="max-h-56 overflow-y-auto rounded-md border border-border bg-muted/10 p-2">
            <ul data-testid="docker-volumes" className="space-y-1 font-mono text-xs">
              {data.volumes.length === 0 ? (
                <li className="text-muted-foreground p-1">No volumes.</li>
              ) : (
                data.volumes.map((volume) => (
                  <li
                    key={volume.name}
                    className="flex items-center justify-between py-1 px-2 rounded bg-card border border-border/40 text-[11px] hover:bg-muted/30 transition-colors"
                  >
                    <span className="truncate max-w-[240px]" title={volume.name}>
                      {volume.name}
                    </span>
                    <span className="text-[10px] text-muted-foreground ml-2 shrink-0">
                      ({volume.driver})
                    </span>
                  </li>
                ))
              )}
            </ul>
          </div>
        </Card>

        <Card className="p-4 space-y-2">
          <div className="flex items-center justify-between">
            <h3 className="font-semibold text-sm">Networks</h3>
            <Badge variant="outline">{data.networks.length}</Badge>
          </div>
          <div className="max-h-56 overflow-y-auto rounded-md border border-border bg-muted/10 p-2">
            <ul data-testid="docker-networks" className="space-y-1 font-mono text-xs">
              {data.networks.length === 0 ? (
                <li className="text-muted-foreground p-1">No networks.</li>
              ) : (
                data.networks.map((network) => (
                  <li
                    key={network.name}
                    className="flex items-center justify-between py-1 px-2 rounded bg-card border border-border/40 text-[11px] hover:bg-muted/30 transition-colors"
                  >
                    <span className="font-medium text-foreground">{network.name}</span>
                    <span className="text-[10px] text-muted-foreground ml-2 shrink-0">
                      ({network.driver})
                    </span>
                  </li>
                ))
              )}
            </ul>
          </div>
        </Card>
      </div>
    </section>
  );
}
