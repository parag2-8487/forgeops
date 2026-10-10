// SPDX-License-Identifier: FSL-1.1-ALv2
"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import { env } from "@/lib/env";
import { readSSEResponse } from "@/lib/sse-reader";
import type {
  AutonomousRunPublicResponse,
  StagePublicResponse,
} from "./JenkinsPipelineDashboard";

export type AutonomousLogLevel = "INFO" | "WARN" | "ERROR" | string;

export interface AutonomousLogEntry {
  id?: number | null;
  run_id?: string;
  stage_name: string;
  log_seq: number;
  level: AutonomousLogLevel;
  message: string;
  created_at?: string | null;
}

export interface UseAutonomousDeployStreamOptions {
  projectId: string;
  runId: string;
  initialRun?: AutonomousRunPublicResponse | null;
  initialStages?: StagePublicResponse[];
  initialLogs?: AutonomousLogEntry[];
  enabled?: boolean;
}

export interface UseAutonomousDeployStreamReturn {
  run: AutonomousRunPublicResponse | null;
  stages: StagePublicResponse[];
  logs: AutonomousLogEntry[];
  isConnected: boolean;
  isReconnecting: boolean;
  error: string | null;
  refetchSnapshot: () => Promise<void>;
}

export function buildWebSocketUrl(
  projectId: string,
  runId: string,
  sinceEventSeq: number,
): string {
  const base = env.NEXT_PUBLIC_API_BASE_URL || "http://localhost:8000/api/v1";
  const wsBase = base.replace(/^http(s?):/, "ws$1:");
  return `${wsBase}/projects/${projectId}/autonomous-deploy/${runId}/ws?since_event_seq=${sinceEventSeq}`;
}

export function buildSseUrl(
  projectId: string,
  runId: string,
  sinceEventSeq: number,
): string {
  const base = env.NEXT_PUBLIC_API_BASE_URL || "http://localhost:8000/api/v1";
  return `${base}/projects/${projectId}/autonomous-deploy/${runId}/events?since_event_seq=${sinceEventSeq}`;
}

export function useAutonomousDeployStream(
  projectIdOrOptions: string | UseAutonomousDeployStreamOptions,
  maybeRunId?: string,
  maybeOptions?: Partial<UseAutonomousDeployStreamOptions>,
): UseAutonomousDeployStreamReturn {
  const options: UseAutonomousDeployStreamOptions =
    typeof projectIdOrOptions === "object"
      ? projectIdOrOptions
      : {
          projectId: projectIdOrOptions,
          runId: maybeRunId ?? "",
          ...maybeOptions,
        };

  const {
    projectId,
    runId,
    initialRun = null,
    initialStages = [],
    initialLogs = [],
    enabled = true,
  } = options;

  const [run, setRun] = useState<AutonomousRunPublicResponse | null>(initialRun);
  const [stages, setStages] = useState<StagePublicResponse[]>(() => {
    if (initialStages.length > 0) return initialStages;
    if (initialRun?.stages && initialRun.stages.length > 0) {
      return [...initialRun.stages].sort((a, b) => a.position - b.position);
    }
    return [];
  });
  const [logs, setLogs] = useState<AutonomousLogEntry[]>(() => [...initialLogs]);
  const [isConnected, setIsConnected] = useState<boolean>(false);
  const [isReconnecting, setIsReconnecting] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  const seenEventSeqRef = useRef<number>(0);
  const seenLogSeqRef = useRef<number>(
    initialLogs.reduce((max, l) => Math.max(max, l.log_seq || 0), 0),
  );
  const isFetchingSnapshotRef = useRef<boolean>(false);

  // Synchronize when initial props change
  useEffect(() => {
    if (initialRun) {
      setRun((prev) => prev ?? initialRun);
      if (initialRun.stages && initialRun.stages.length > 0) {
        setStages((prev) => (prev.length > 0 ? prev : [...initialRun.stages]));
      }
    }
  }, [initialRun]);

  const mergeLogs = useCallback((incoming: AutonomousLogEntry[]) => {
    if (!incoming || incoming.length === 0) return;
    setLogs((prev) => {
      const map = new Map<number, AutonomousLogEntry>();
      for (const item of prev) {
        map.set(item.log_seq, item);
      }
      for (const item of incoming) {
        if (item && typeof item.log_seq === "number") {
          map.set(item.log_seq, {
            ...item,
            created_at: item.created_at || new Date().toISOString(),
          });
          seenLogSeqRef.current = Math.max(seenLogSeqRef.current, item.log_seq);
        }
      }
      return Array.from(map.values()).sort((a, b) => a.log_seq - b.log_seq);
    });
  }, []);

  const refetchSnapshot = useCallback(async () => {
    if (!projectId || !runId || isFetchingSnapshotRef.current) return;
    isFetchingSnapshotRef.current = true;
    try {
      const freshRun = await api.get<AutonomousRunPublicResponse>(
        `/projects/${projectId}/autonomous-deploy/${runId}`,
      );
      if (freshRun) {
        setRun(freshRun);
        if (freshRun.stages && freshRun.stages.length > 0) {
          setStages([...freshRun.stages].sort((a, b) => a.position - b.position));
        }
      }

      // Reconcile missed logs via pagination
      try {
        const paginatedLogs = await api.get<{
          logs: AutonomousLogEntry[];
          total_lines?: number;
        }>(
          `/projects/${projectId}/autonomous-deploy/${runId}/logs?since_log_seq=${seenLogSeqRef.current}&limit=1000`,
        );
        if (paginatedLogs?.logs && paginatedLogs.logs.length > 0) {
          mergeLogs(paginatedLogs.logs);
        }
      } catch {
        // Log pagination retrieval is non-fatal during snapshot reconciliation
      }
      setError(null);
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : "Failed to refetch authoritative snapshot";
      setError(msg);
    } finally {
      isFetchingSnapshotRef.current = false;
    }
  }, [projectId, runId, mergeLogs]);

  // Dispatch individual event payloads
  const processEventFrame = useCallback(
    (frame: unknown) => {
      if (!frame || typeof frame !== "object") return;
      const f = frame as Record<string, unknown>;

      const rawSeq = f.event_seq ?? f.seq ?? (f.payload as Record<string, unknown> | undefined)?.event_seq;
      const eventSeq = typeof rawSeq === "number" ? rawSeq : undefined;

      const rawType = f.event_type ?? f.type ?? f.event ?? (f.payload as Record<string, unknown> | undefined)?.event_type;
      const eventType = typeof rawType === "string" ? rawType.toLowerCase() : "";

      const rawPayload = f.payload ?? f;
      const payload = (typeof rawPayload === "object" && rawPayload !== null ? rawPayload : {}) as Record<string, unknown>;

      // 1. Watermark deduplication check
      if (eventSeq !== undefined && eventSeq > 0) {
        if (eventSeq <= seenEventSeqRef.current) {
          // Event was already processed; drop duplicate
          return;
        }

        // 2. Sequence gap detection
        if (seenEventSeqRef.current > 0 && eventSeq > seenEventSeqRef.current + 1) {
          // Detected sequence gap between seenEventSeqRef.current and eventSeq
          refetchSnapshot();
        }

        seenEventSeqRef.current = eventSeq;
      }

      // 3. Event ingestion by domain type
      // Stage Update
      if (
        eventType === "stage_update" ||
        eventType.startsWith("stage_") ||
        eventType === "stage_transition" ||
        eventType === "progress_updated"
      ) {
        const stageName = (payload.stage_name ?? payload.name) as string | undefined;
        const stageStatus = payload.status as StagePublicResponse["status"] | undefined;
        const stageProgress = typeof payload.progress_pct === "number" ? payload.progress_pct : undefined;
        const runProgress = typeof payload.run_progress_pct === "number" ? payload.run_progress_pct : undefined;
        const errorMessage = (payload.error_message as string | undefined) ?? null;
        const metadata = (payload.metadata ?? payload.stage_metadata ?? {}) as Record<string, unknown>;
        const startedAt = payload.started_at as string | undefined;
        const completedAt = payload.completed_at as string | undefined;

        if (stageName) {
          setStages((prev) => {
            const index = prev.findIndex((s) => s.stage_name === stageName);
            if (index >= 0) {
              const updated = [...prev];
              updated[index] = {
                ...updated[index],
                status: stageStatus ?? updated[index].status,
                progress_pct: stageProgress !== undefined ? stageProgress : updated[index].progress_pct,
                error_message: errorMessage !== undefined ? errorMessage : updated[index].error_message,
                stage_metadata: {
                  ...(updated[index].stage_metadata || {}),
                  ...metadata,
                },
                metadata: {
                  ...(updated[index].metadata || {}),
                  ...metadata,
                },
                started_at: startedAt ?? updated[index].started_at,
                completed_at: completedAt ?? updated[index].completed_at,
              };
              return updated;
            }
            return prev;
          });

          setRun((prev) => {
            if (!prev) return prev;
            const nextRun = { ...prev };
            nextRun.current_stage = stageName;
            if (runProgress !== undefined) {
              nextRun.progress_pct = runProgress;
            }
            if (stageStatus === "failed" && nextRun.status === "running") {
              nextRun.status = "failed";
              nextRun.error_summary = errorMessage || nextRun.error_summary;
            }
            if (
              (stageName === "G7_verification" || payload.gate_id === "G7") &&
              stageStatus === "succeeded"
            ) {
              nextRun.status = "succeeded";
              nextRun.progress_pct = 100;
            }
            return nextRun;
          });
        }
      }

      // Run Update
      if (
        eventType === "run_update" ||
        eventType.startsWith("run_") ||
        eventType === "run_started" ||
        eventType === "run_succeeded" ||
        eventType === "run_failed" ||
        eventType === "run_cancelled" ||
        eventType === "run_completed"
      ) {
        setRun((prev) => {
          if (!prev) return payload as unknown as AutonomousRunPublicResponse;
          return {
            ...prev,
            status: (payload.status as AutonomousRunPublicResponse["status"]) ?? prev.status,
            progress_pct:
              typeof payload.progress_pct === "number" ? payload.progress_pct : prev.progress_pct,
            current_stage: (payload.current_stage as string) ?? prev.current_stage,
            error_summary: (payload.error_summary as string) ?? prev.error_summary,
            started_at: (payload.started_at as string) ?? prev.started_at,
            completed_at: (payload.completed_at as string) ?? prev.completed_at,
            agent_connected:
              payload.agent_connected !== undefined
                ? Boolean(payload.agent_connected)
                : prev.agent_connected,
          };
        });

        if (Array.isArray(payload.stages)) {
          setStages([...(payload.stages as StagePublicResponse[])].sort((a, b) => a.position - b.position));
        }
      }

      // Log Batch or Single Log
      if (eventType === "log_batch" || eventType === "log" || eventType === "log_emitted") {
        let incoming: AutonomousLogEntry[] = [];
        if (Array.isArray(payload.logs)) {
          incoming = payload.logs as AutonomousLogEntry[];
        } else if (Array.isArray(payload.entries)) {
          incoming = payload.entries as AutonomousLogEntry[];
        } else if (Array.isArray(payload.log_entries)) {
          incoming = payload.log_entries as AutonomousLogEntry[];
        } else if (Array.isArray(payload)) {
          incoming = payload as unknown as AutonomousLogEntry[];
        } else if (payload.log_seq !== undefined) {
          incoming = [payload as unknown as AutonomousLogEntry];
        }
        mergeLogs(incoming);
      }
    },
    [refetchSnapshot, mergeLogs],
  );

  // Hook connection lifecycle: WebSocket with SSE fallback
  useEffect(() => {
    if (!enabled || !projectId || !runId) return;

    let isMounted = true;
    let ws: WebSocket | null = null;
    let sseSource: EventSource | null = null;
    let sseAbortController: AbortController | null = null;
    let hasFallenBackToSse = false;

    const connectSSE = () => {
      if (!isMounted || hasFallenBackToSse) return;
      hasFallenBackToSse = true;
      setIsReconnecting(true);

      const sseUrl = buildSseUrl(projectId, runId, seenEventSeqRef.current);

      if (typeof EventSource !== "undefined") {
        try {
          const es = new EventSource(sseUrl);
          sseSource = es;

          es.onopen = () => {
            if (!isMounted) return;
            setIsConnected(true);
            setIsReconnecting(false);
            setError(null);
          };

          const handleSseMsg = (ev: MessageEvent) => {
            if (!isMounted) return;
            try {
              const data = typeof ev.data === "string" ? JSON.parse(ev.data) : ev.data;
              processEventFrame(data);
            } catch {
              // ignore unparseable frame
            }
          };

          es.onmessage = handleSseMsg;
          es.addEventListener("stage_update", handleSseMsg as EventListener);
          es.addEventListener("run_update", handleSseMsg as EventListener);
          es.addEventListener("log_batch", handleSseMsg as EventListener);
          es.addEventListener("log", handleSseMsg as EventListener);
          es.addEventListener("status", handleSseMsg as EventListener);
          es.addEventListener("progress", handleSseMsg as EventListener);

          es.onerror = () => {
            if (!isMounted) return;
            setIsConnected(false);
          };

          return;
        } catch {
          // EventSource instantiation failed; fall back to api.stream
        }
      }

      // Fall back to api.stream (fetch reader)
      sseAbortController = new AbortController();
      (async () => {
        try {
          const streamPath = `/projects/${projectId}/autonomous-deploy/${runId}/events?since_event_seq=${seenEventSeqRef.current}`;
          const response = await api.stream(streamPath, {
            signal: sseAbortController?.signal,
          });

          if (!isMounted) return;
          setIsConnected(true);
          setIsReconnecting(false);
          setError(null);

          for await (const msg of readSSEResponse<unknown>(response)) {
            if (!isMounted) break;
            processEventFrame(msg.data);
          }
        } catch (err: unknown) {
          if (!isMounted) return;
          setIsConnected(false);
          setIsReconnecting(false);
          if (err instanceof Error && err.name !== "AbortError") {
            setError(err.message);
          }
        }
      })();
    };

    const connectWS = () => {
      const wsUrl = buildWebSocketUrl(projectId, runId, seenEventSeqRef.current);

      if (typeof WebSocket === "undefined") {
        connectSSE();
        return;
      }

      try {
        ws = new WebSocket(wsUrl);

        ws.onopen = () => {
          if (!isMounted) return;
          setIsConnected(true);
          setIsReconnecting(false);
          setError(null);
        };

        ws.onmessage = (event) => {
          if (!isMounted) return;
          try {
            const data = typeof event.data === "string" ? JSON.parse(event.data) : event.data;
            processEventFrame(data);
          } catch {
            // ignore non-json frame
          }
        };

        ws.onerror = () => {
          if (!isMounted) return;
          if (!hasFallenBackToSse) {
            connectSSE();
          }
        };

        ws.onclose = (event) => {
          if (!isMounted) return;
          setIsConnected(false);
          if (event.code !== 1000 && !hasFallenBackToSse) {
            connectSSE();
          }
        };
      } catch {
        connectSSE();
      }
    };

    // Initiate primary connection
    connectWS();

    // Reconcile state on window visibility change
    const handleVisibilityChange = () => {
      if (typeof document !== "undefined" && document.visibilityState === "visible") {
        refetchSnapshot();
      }
    };

    if (typeof document !== "undefined") {
      document.addEventListener("visibilitychange", handleVisibilityChange);
    }

    return () => {
      isMounted = false;
      if (typeof document !== "undefined") {
        document.removeEventListener("visibilitychange", handleVisibilityChange);
      }
      if (ws) {
        ws.close(1000);
      }
      if (sseSource) {
        sseSource.close();
      }
      if (sseAbortController) {
        sseAbortController.abort();
      }
    };
  }, [projectId, runId, enabled, processEventFrame, refetchSnapshot]);

  return {
    run,
    stages,
    logs,
    isConnected,
    isReconnecting,
    error,
    refetchSnapshot,
  };
}
