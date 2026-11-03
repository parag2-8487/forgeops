"use client";

/**
 * One log viewer, used by the deployment, container and pod panels.
 *
 * The property it exists to keep: a TAIL IS MARKED AS A TAIL. An unmarked truncation lets a reader conclude
 * an error never happened when it fell off the top, which is the same class of lie as a fabricated metric.
 * `truncated` and the applied bounds come from the agent and are rendered, not inferred.
 *
 * An empty log is stated in words and distinguished from a failed read, and — for a pod — from "no logs but
 * here are the events", which is the normal state of a pod that never scheduled and the case where the
 * events are the entire explanation.
 */

import { useQuery } from "@tanstack/react-query";

import { api, queryKeys } from "@/lib/api";

export type LogReport = {
  target: string;
  lines: string[];
  tail_lines: number;
  since_seconds: number;
  truncated: boolean;
  events?: { type: string; reason: string; message: string; count: number; last_seen: string }[];
  observed_at: string;
};

/**
 * The sentences a failed read produces.
 *
 * Exported as values because they are the deliverable: the distinction between "this could not be read" and
 * "this is quiet" lives in the words, and a test that drives the query plumbing to reach them proved to be
 * about vitest's uncaught-error handling rather than about the product.
 */
export const CONTAINER_LOG_READ_FAILED =
  "The logs could not be read. This is not the same as the container being quiet.";
export const POD_DETAIL_READ_FAILED =
  "Neither logs nor events could be read. This is not the same as a quiet pod.";

export function LogPanel({ report, testId }: { report: LogReport; testId: string }) {
  const events = report.events ?? [];
  return (
    <div data-testid={testId}>
      <p data-testid={`${testId}-bounds`}>
        {/* THE BOUNDS THAT WERE APPLIED, which may differ from what was asked for. */}
        last {report.tail_lines} line(s)
        {report.since_seconds > 0 ? ` within ${report.since_seconds}s` : ""}, read at{" "}
        {report.observed_at}
        {report.truncated ? " — this is a tail; earlier output is not shown" : ""}
      </p>
      {report.lines.length === 0 ? (
        <p data-testid={`${testId}-empty`}>
          {events.length > 0
            ? "This has produced no log output. The events below are the explanation."
            : "This has produced no log output."}
        </p>
      ) : (
        <pre data-testid={`${testId}-lines`}>{report.lines.join("\n")}</pre>
      )}
      {events.length > 0 && (
        <ul data-testid={`${testId}-events`}>
          {events.map((event, index) => (
            <li key={`${event.reason}-${index}`}>
              {event.type} {event.reason} ×{event.count} — {event.message} ({event.last_seen})
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/** A container's log, fetched on demand. */
export function ContainerLogs({ projectId, container }: { projectId: string; container: string }) {
  const logs = useQuery<LogReport>({
    queryKey: queryKeys.hostops.containerLogs(projectId, container),
    queryFn: () =>
      api.get<LogReport>(
        `/projects/${projectId}/docker/containers/${encodeURIComponent(container)}/logs`,
      ),
  });

  if (logs.isLoading)
    return <p data-testid={`container-logs-loading-${container}`}>Reading logs…</p>;
  if (logs.isError) {
    return (
      <p data-testid={`container-logs-error-${container}`} role="alert">
        {/* A failed read is not an empty log. */}
        The logs could not be read: {logs.error.message}. This is not the same as the container
        being quiet.
      </p>
    );
  }
  return logs.data ? <LogPanel report={logs.data} testId={`container-logs-${container}`} /> : null;
}

/** A pod's logs and events together. */
export function PodDetail({
  projectId,
  namespace,
  pod,
}: {
  projectId: string;
  namespace: string;
  pod: string;
}) {
  const detail = useQuery<LogReport>({
    queryKey: queryKeys.hostops.podDetail(projectId, namespace, pod),
    queryFn: () =>
      api.get<LogReport>(
        `/projects/${projectId}/kubernetes/namespaces/${encodeURIComponent(namespace)}/pods/${encodeURIComponent(pod)}`,
      ),
  });

  if (detail.isLoading)
    return <p data-testid={`pod-detail-loading-${pod}`}>Reading logs and events…</p>;
  if (detail.isError) {
    return (
      <p data-testid={`pod-detail-error-${pod}`} role="alert">
        Neither logs nor events could be read: {detail.error.message}. This is not the same as a
        quiet pod.
      </p>
    );
  }
  return detail.data ? <LogPanel report={detail.data} testId={`pod-detail-${pod}`} /> : null;
}
