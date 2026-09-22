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
import { useState } from "react";

import { ContainerLogs } from "@/features/hostops/LogPanel";
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
  const [withStats, setWithStats] = useState(false);
  const [pendingRemoval, setPendingRemoval] = useState<string | null>(null);
  const [showLogs, setShowLogs] = useState<string | null>(null);
  const [lastOutcome, setLastOutcome] = useState<string | null>(null);

  const inventory = useQuery<DockerInventory>({
    queryKey: queryKeys.hostops.dockerInventory(projectId, withStats),
    queryFn: () =>
      api.get<DockerInventory>(
        `/projects/${projectId}/docker/inventory${withStats ? "?stats=true" : ""}`,
      ),
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

  const imageAction = useMutation<ActionAccepted, Error, { action: string; image: string }>({
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
      <section aria-label="Docker" data-testid="docker-dashboard">
        <p data-testid="docker-error" role="alert">
          The agent did not report this host&apos;s containers: {inventory.error.message}. This is
          not the same as the host running nothing.
        </p>
        <button type="button" onClick={() => void inventory.refetch()}>
          Ask again
        </button>
      </section>
    );
  }

  const data = inventory.data;
  if (!data) return null;
  const freshness = freshnessOf(data.observed_at);

  return (
    <section aria-label="Docker" data-testid="docker-dashboard">
      <header>
        <h2>Containers on this machine</h2>
        <p>
          <span data-testid="docker-version">Docker {data.docker_version}</span>{" "}
          <FreshnessBadge freshness={freshness} />
        </p>
        <label>
          <input
            type="checkbox"
            checked={withStats}
            onChange={(event) => setWithStats(event.target.checked)}
            data-testid="docker-stats-toggle"
          />{" "}
          Sample CPU, memory and network (adds about a second)
        </label>
        {!data.stats_sampled && (
          <p data-testid="docker-stats-absent" className="text-sm text-slate-500">
            No resource sample was taken, so the figures below read “not measured” rather than zero.
          </p>
        )}
      </header>

      {lastOutcome && (
        <p data-testid="docker-last-outcome" role="status">
          {lastOutcome === "applying"
            ? "Sent to the agent."
            : lastOutcome === "approval-required"
              ? "Waiting for a human to approve it. Nothing has changed on the host yet."
              : `The governance gate answered: ${lastOutcome}.`}
        </p>
      )}

      <h3>Containers</h3>
      {data.containers.length === 0 ? (
        <p data-testid="docker-containers-empty">
          The agent reached the daemon and it is running no containers.
        </p>
      ) : (
        <table data-testid="docker-containers">
          <thead>
            <tr>
              <th scope="col">Name</th>
              <th scope="col">State</th>
              <th scope="col">Image</th>
              <th scope="col">CPU</th>
              <th scope="col">Memory</th>
              <th scope="col">Network in / out</th>
              <th scope="col">Actions</th>
            </tr>
          </thead>
          <tbody>
            {data.containers.map((container) => (
              <tr key={container.id} data-testid={`docker-container-${container.name}`}>
                <td>{container.name}</td>
                <td data-testid={`docker-state-${container.name}`}>{container.state}</td>
                <td>{container.image}</td>
                <td data-testid={`docker-cpu-${container.name}`}>
                  {measurement(container.cpu_percent, (value) => `${value.toFixed(2)}%`)}
                </td>
                <td data-testid={`docker-memory-${container.name}`}>
                  {measurement(container.memory_bytes, (value) =>
                    container.memory_limit_bytes === null
                      ? bytes(value)
                      : `${bytes(value)} of ${bytes(container.memory_limit_bytes)}`,
                  )}
                </td>
                <td data-testid={`docker-network-${container.name}`}>
                  {measurement(container.network_rx_bytes, bytes)} /{" "}
                  {measurement(container.network_tx_bytes, bytes)}
                </td>
                <td>
                  {(["start", "stop", "restart"] as const).map((action) => (
                    <button
                      key={action}
                      type="button"
                      data-testid={`docker-${action}-${container.name}`}
                      disabled={containerAction.isPending}
                      onClick={() => containerAction.mutate({ action, container: container.name })}
                    >
                      {action}
                    </button>
                  ))}
                  {/* The irreversible one, and the only one that asks. */}
                  <button
                    type="button"
                    data-testid={`docker-logs-${container.name}`}
                    onClick={() => setShowLogs(showLogs === container.name ? null : container.name)}
                  >
                    {showLogs === container.name ? "hide logs" : "logs"}
                  </button>
                  {showLogs === container.name && (
                    <ContainerLogs projectId={projectId} container={container.name} />
                  )}
                  {pendingRemoval === container.name ? (
                    <>
                      <span data-testid={`docker-remove-confirm-${container.name}`}>
                        Remove {container.name}? Anything it holds that is not in a volume is lost.
                      </span>
                      <button
                        type="button"
                        data-testid={`docker-remove-yes-${container.name}`}
                        onClick={() =>
                          containerAction.mutate({ action: "remove", container: container.name })
                        }
                      >
                        Remove it
                      </button>
                      <button type="button" onClick={() => setPendingRemoval(null)}>
                        Keep it
                      </button>
                    </>
                  ) : (
                    <button
                      type="button"
                      data-testid={`docker-remove-${container.name}`}
                      onClick={() => setPendingRemoval(container.name)}
                    >
                      remove
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <h3>Images</h3>
      {data.images.length === 0 ? (
        <p data-testid="docker-images-empty">The daemon holds no images.</p>
      ) : (
        <table data-testid="docker-images">
          <thead>
            <tr>
              <th scope="col">Repository</th>
              <th scope="col">Tag</th>
              <th scope="col">Size</th>
              <th scope="col">Actions</th>
            </tr>
          </thead>
          <tbody>
            {data.images.map((image) => {
              const reference = `${image.repository}:${image.tag}`;
              return (
                <tr key={image.id} data-testid={`docker-image-${image.repository}`}>
                  <td>{image.repository}</td>
                  <td>{image.tag}</td>
                  <td>{image.size}</td>
                  <td>
                    <button
                      type="button"
                      data-testid={`docker-pull-${image.repository}`}
                      onClick={() => imageAction.mutate({ action: "pull", image: reference })}
                    >
                      pull
                    </button>
                    <button
                      type="button"
                      data-testid={`docker-rmi-${image.repository}`}
                      onClick={() => imageAction.mutate({ action: "remove", image: reference })}
                    >
                      remove
                    </button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}

      <h3>Volumes and networks</h3>
      <ul data-testid="docker-volumes">
        {data.volumes.length === 0 ? (
          <li>No volumes.</li>
        ) : (
          data.volumes.map((volume) => (
            <li key={volume.name}>
              {volume.name} ({volume.driver})
            </li>
          ))
        )}
      </ul>
      <ul data-testid="docker-networks">
        {data.networks.map((network) => (
          <li key={network.name}>
            {network.name} ({network.driver})
          </li>
        ))}
      </ul>
    </section>
  );
}
