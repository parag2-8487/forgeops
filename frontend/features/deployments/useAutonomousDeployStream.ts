// SPDX-License-Identifier: FSL-1.1-ALv2
"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import { env } from "@/lib/env";
import { getAccessToken, subscribe as subscribeSession } from "@/lib/session";
import { readSSEResponse } from "@/lib/sse-reader";
import type { AutonomousRunPublicResponse, StagePublicResponse } from "./JenkinsPipelineDashboard";

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
  stalenessThresholdMs?: number;
  watchdogIntervalMs?: number;
  reconnectThresholdMs?: number;
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
  token?: string | null,
): string {
  const base = env.NEXT_PUBLIC_API_BASE_URL || "http://localhost:8000/api/v1";
  const wsBase = base.replace(/^http(s?):/, "ws$1:");
  const tokenParam = token ? `&token=${encodeURIComponent(token)}` : "";
  return `${wsBase}/projects/${projectId}/autonomous-deploy/${runId}/ws?since_event_seq=${sinceEventSeq}${tokenParam}`;
}

export function buildSseUrl(
  projectId: string,
  runId: string,
  sinceEventSeq: number,
  token?: string | null,
): string {
  const base = env.NEXT_PUBLIC_API_BASE_URL || "http://localhost:8000/api/v1";
  const tokenParam = token ? `&token=${encodeURIComponent(token)}` : "";
  return `${base}/projects/${projectId}/autonomous-deploy/${runId}/events?since_event_seq=${sinceEventSeq}${tokenParam}`;
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
    stalenessThresholdMs = 20000,
    watchdogIntervalMs = 10000,
    reconnectThresholdMs = 45000,
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
  const [authToken, setAuthToken] = useState<string | null>(() => getAccessToken());

  useEffect(() => {
    return subscribeSession(() => {
      setAuthToken(getAccessToken());
    });
  }, []);

  const seenEventSeqByRunRef = useRef<Record<string, number>>({});
  const seenLogSeqByRunRef = useRef<Record<string, number>>({});
  const isFetchingSnapshotRef = useRef<boolean>(false);

  const isTerminal =
    run?.status === "succeeded" ||
    run?.status === "failed" ||
    run?.status === "cancelled" ||
    run?.status === "rolled_back";

  const isTerminalRef = useRef<boolean>(Boolean(isTerminal));
  useEffect(() => {
    isTerminalRef.current = Boolean(isTerminal);
  }, [isTerminal]);

  const lastEventTimestampRef = useRef<number>(Date.now());
  const reconnectStreamRef = useRef<(() => void) | null>(null);

  // Reset stream state when switching to a different run or when initialRun prop changes
  const [prevRunId, setPrevRunId] = useState(runId);
  const [prevInitialRun, setPrevInitialRun] = useState(initialRun);

  if (runId !== prevRunId) {
    setPrevRunId(runId);
    setPrevInitialRun(initialRun);
    setLogs([...initialLogs]);
    setRun(initialRun ?? null);
    if (initialStages.length > 0) {
      setStages(initialStages);
    } else if (initialRun?.stages && initialRun.stages.length > 0) {
      setStages([...initialRun.stages].sort((a, b) => a.position - b.position));
    } else {
      setStages([]);
    }
  } else if (initialRun && initialRun !== prevInitialRun) {
    setPrevInitialRun(initialRun);
    setRun((prev) => (prev ? { ...prev, ...initialRun } : initialRun));
    if (initialRun.stages && initialRun.stages.length > 0) {
      setStages((prev) => {
        if (prev.length === 0 || (prev[0] && prev[0].run_id !== initialRun.id)) {
          return [...initialRun.stages].sort((a, b) => a.position - b.position);
        }
        const map = new Map(prev.map((s) => [s.stage_name, s]));
        for (const inc of initialRun.stages) {
          const existing = map.get(inc.stage_name);
          if (!existing) {
            map.set(inc.stage_name, inc);
          } else {
            const incActive = inc.status !== "pending";
            const existPending = existing.status === "pending";
            if (incActive || !existPending || inc.progress_pct >= existing.progress_pct) {
              map.set(inc.stage_name, {
                ...existing,
                ...inc,
                status: inc.status,
                progress_pct: Math.max(existing.progress_pct, inc.progress_pct),
                started_at: inc.started_at ?? existing.started_at,
                completed_at: inc.completed_at ?? existing.completed_at,
                error_message: inc.error_message ?? existing.error_message,
                metadata: { ...(existing.metadata || {}), ...(inc.metadata || {}) },
                stage_metadata: {
                  ...(existing.stage_metadata || {}),
                  ...(inc.stage_metadata || {}),
                },
              });
            }
          }
        }
        return Array.from(map.values()).sort((a, b) => a.position - b.position);
      });
    }
  }

  const mergeLogs = useCallback(
    (incoming: AutonomousLogEntry[]) => {
      if (!incoming || incoming.length === 0) return;
      setLogs((prev) => {
        const map = new Map<number, AutonomousLogEntry>();
        for (const item of prev) {
          map.set(item.log_seq, item);
        }
        let maxSeq = seenLogSeqByRunRef.current[runId] ?? 0;
        for (const item of incoming) {
          if (item && typeof item.log_seq === "number") {
            map.set(item.log_seq, {
              ...item,
              created_at: item.created_at || new Date().toISOString(),
            });
            maxSeq = Math.max(maxSeq, item.log_seq);
          }
        }
        seenLogSeqByRunRef.current[runId] = maxSeq;
        return Array.from(map.values()).sort((a, b) => a.log_seq - b.log_seq);
      });
    },
    [runId],
  );

  const refetchSnapshot = useCallback(async () => {
    if (!projectId || !runId || isFetchingSnapshotRef.current) return;
    isFetchingSnapshotRef.current = true;
    try {
      const freshRun = await api.get<AutonomousRunPublicResponse>(
        `/projects/${projectId}/autonomous-deploy/${runId}`,
      );
      if (freshRun) {
        setRun((prev) => {
          if (!prev) return freshRun;
          const isNewAttempt = (freshRun.attempt_number ?? 1) > (prev.attempt_number ?? 1);
          const monotonicProgress = isNewAttempt
            ? (freshRun.progress_pct ?? 0)
            : Math.max(prev.progress_pct ?? 0, freshRun.progress_pct ?? 0);

          const isTerminal = (status?: string) =>
            status === "succeeded" || status === "failed" || status === "cancelled";
          const preserveStatus = !isNewAttempt && isTerminal(prev.status) && !isTerminal(freshRun.status);

          return {
            ...freshRun,
            status: preserveStatus ? prev.status : freshRun.status,
            progress_pct: monotonicProgress,
            current_stage:
              !isNewAttempt && !freshRun.current_stage && prev.current_stage
                ? prev.current_stage
                : freshRun.current_stage,
            error_summary: freshRun.error_summary || prev.error_summary,
          };
        });

        if (freshRun.stages && freshRun.stages.length > 0) {
          setStages((prevStages) => {
            const sortedFresh = [...freshRun.stages].sort((a, b) => a.position - b.position);
            if (!prevStages || prevStages.length === 0) return sortedFresh;

            return sortedFresh.map((freshStage) => {
              const prevStage = prevStages.find((s) => s.stage_name === freshStage.stage_name);
              if (!prevStage) return freshStage;

              const isTerminal = prevStage.status === "succeeded" || prevStage.status === "failed";
              const freshIsTerminal =
                freshStage.status === "succeeded" || freshStage.status === "failed";
              const shouldPreservePrevStage = isTerminal && !freshIsTerminal;

              if (shouldPreservePrevStage) {
                return {
                  ...freshStage,
                  status: prevStage.status,
                  progress_pct: Math.max(prevStage.progress_pct ?? 0, freshStage.progress_pct ?? 0),
                  completed_at: prevStage.completed_at || freshStage.completed_at,
                  started_at: prevStage.started_at || freshStage.started_at,
                  error_message: prevStage.error_message || freshStage.error_message,
                  stage_metadata: {
                    ...(prevStage.stage_metadata || {}),
                    ...(freshStage.stage_metadata || {}),
                  },
                };
              }
              return {
                ...freshStage,
                progress_pct: Math.max(prevStage.progress_pct ?? 0, freshStage.progress_pct ?? 0),
              };
            });
          });
        }
      }

      // Reconcile missed logs via pagination
      try {
        const currentLogSeq = seenLogSeqByRunRef.current[runId] ?? 0;
        const paginatedLogs = await api.get<{
          logs: AutonomousLogEntry[];
          total_lines?: number;
        }>(
          `/projects/${projectId}/autonomous-deploy/${runId}/logs?since_log_seq=${currentLogSeq}&limit=1000`,
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
      lastEventTimestampRef.current = Date.now();
    }
  }, [projectId, runId, mergeLogs]);

  // Dispatch individual event payloads
  const processEventFrame = useCallback(
    (frame: unknown) => {
      if (!frame || typeof frame !== "object") return;
      lastEventTimestampRef.current = Date.now();
      const f = frame as Record<string, unknown>;

      const rawSeq =
        f.event_seq ?? f.seq ?? (f.payload as Record<string, unknown> | undefined)?.event_seq;
      const eventSeq = typeof rawSeq === "number" ? rawSeq : undefined;

      const rawType =
        f.event_type ??
        f.type ??
        f.event ??
        (f.payload as Record<string, unknown> | undefined)?.event_type;
      const eventType = typeof rawType === "string" ? rawType.toLowerCase() : "";

      const rawPayload = f.payload ?? f;
      const payload = (
        typeof rawPayload === "object" && rawPayload !== null ? rawPayload : {}
      ) as Record<string, unknown>;

      // 1. Watermark deduplication check
      if (eventSeq !== undefined && eventSeq > 0) {
        const currentSeenEventSeq = seenEventSeqByRunRef.current[runId] ?? 0;
        if (eventSeq <= currentSeenEventSeq) {
          // Event was already processed; drop duplicate
          return;
        }

        // 2. Sequence gap detection
        if (currentSeenEventSeq > 0 && eventSeq > currentSeenEventSeq + 1) {
          // Detected sequence gap between currentSeenEventSeq and eventSeq
          refetchSnapshot();
        }

        seenEventSeqByRunRef.current[runId] = eventSeq;
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
        const stageProgress =
          typeof payload.progress_pct === "number" ? payload.progress_pct : undefined;
        const runProgress =
          typeof payload.run_progress_pct === "number" ? payload.run_progress_pct : undefined;
        const errorMessage = (payload.error_message as string | undefined) ?? null;
        const metadata = (payload.metadata ?? payload.stage_metadata ?? {}) as Record<
          string,
          unknown
        >;
        const startedAt = payload.started_at as string | undefined;
        const completedAt = payload.completed_at as string | undefined;

        if (stageName) {
          setStages((prev) => {
            const index = prev.findIndex((s) => s.stage_name === stageName);
            if (index >= 0) {
              const updated = [...prev];
              const prevStage = updated[index];
              const isTerminal = prevStage.status === "succeeded" || prevStage.status === "failed";
              const newStatus =
                isTerminal && stageStatus === "pending"
                  ? prevStage.status
                  : stageStatus ?? prevStage.status;
              const monotonicStageProgress =
                stageProgress !== undefined
                  ? Math.max(prevStage.progress_pct ?? 0, stageProgress)
                  : prevStage.progress_pct;

              updated[index] = {
                ...prevStage,
                status: newStatus,
                progress_pct: monotonicStageProgress,
                error_message:
                  errorMessage !== undefined ? errorMessage : prevStage.error_message,
                stage_metadata: {
                  ...(prevStage.stage_metadata || {}),
                  ...metadata,
                },
                metadata: {
                  ...(prevStage.metadata || {}),
                  ...metadata,
                },
                started_at: startedAt ?? prevStage.started_at,
                completed_at: completedAt ?? prevStage.completed_at,
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
              nextRun.progress_pct = Math.max(nextRun.progress_pct ?? 0, runProgress);
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
          const nextStatus =
            (payload.status as AutonomousRunPublicResponse["status"]) ?? prev.status;
          const nextAttempt =
            typeof payload.attempt_number === "number"
              ? payload.attempt_number
              : prev.attempt_number;
          const isNewAttempt = (nextAttempt ?? 1) > (prev.attempt_number ?? 1);
          const incomingProgress =
            typeof payload.progress_pct === "number" ? payload.progress_pct : prev.progress_pct;
          const monotonicProgress = isNewAttempt
            ? incomingProgress
            : Math.max(prev.progress_pct ?? 0, incomingProgress ?? 0);

          return {
            ...prev,
            status: nextStatus,
            attempt_number: nextAttempt,
            progress_pct: monotonicProgress,
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
          setStages((prevStages) => {
            const incomingStages = [
              ...(payload.stages as StagePublicResponse[]),
            ].sort((a, b) => a.position - b.position);
            if (!prevStages || prevStages.length === 0) return incomingStages;

            return incomingStages.map((freshStage) => {
              const prevStage = prevStages.find((s) => s.stage_name === freshStage.stage_name);
              if (!prevStage) return freshStage;

              const isTerminal = prevStage.status === "succeeded" || prevStage.status === "failed";
              const freshIsTerminal =
                freshStage.status === "succeeded" || freshStage.status === "failed";
              const shouldPreservePrevStage = isTerminal && !freshIsTerminal;

              if (shouldPreservePrevStage) {
                return {
                  ...freshStage,
                  status: prevStage.status,
                  progress_pct: Math.max(
                    prevStage.progress_pct ?? 0,
                    freshStage.progress_pct ?? 0,
                  ),
                  completed_at: prevStage.completed_at || freshStage.completed_at,
                  started_at: prevStage.started_at || freshStage.started_at,
                  error_message: prevStage.error_message || freshStage.error_message,
                  stage_metadata: {
                    ...(prevStage.stage_metadata || {}),
                    ...(freshStage.stage_metadata || {}),
                  },
                };
              }
              return {
                ...freshStage,
                progress_pct: Math.max(
                  prevStage.progress_pct ?? 0,
                  freshStage.progress_pct ?? 0,
                ),
              };
            });
          });
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
    [runId, refetchSnapshot, mergeLogs],
  );

  // Hook connection lifecycle: WebSocket with SSE fallback
  useEffect(() => {
    if (!enabled || !projectId || !runId) return;

    let isMounted = true;
    let ws: WebSocket | null = null;
    let sseSource: EventSource | null = null;
    let sseAbortController: AbortController | null = null;
    let hasFallenBackToSse = false;
    let reconnectTimeout: ReturnType<typeof setTimeout> | null = null;

    const cleanupActiveTransports = () => {
      if (reconnectTimeout) {
        clearTimeout(reconnectTimeout);
        reconnectTimeout = null;
      }
      if (ws) {
        try {
          ws.close();
        } catch {
          // ignore close error
        }
        ws = null;
      }
      if (sseSource) {
        try {
          sseSource.close();
        } catch {
          // ignore close error
        }
        sseSource = null;
      }
      if (sseAbortController) {
        try {
          sseAbortController.abort();
        } catch {
          // ignore abort error
        }
        sseAbortController = null;
      }
    };

    const scheduleReconnect = (delayMs: number = 3000) => {
      if (!isMounted || isTerminalRef.current) return;
      setIsConnected(false);
      setIsReconnecting(true);
      if (reconnectTimeout) clearTimeout(reconnectTimeout);
      reconnectTimeout = setTimeout(() => {
        if (!isMounted || isTerminalRef.current) return;
        if (hasFallenBackToSse) {
          connectSSE();
        } else {
          connectWS();
        }
      }, delayMs);
    };

    reconnectStreamRef.current = () => {
      if (!isMounted || isTerminalRef.current) return;
      cleanupActiveTransports();
      scheduleReconnect(500);
    };

    const startFetchStream = () => {
      if (!isMounted) return;
      sseAbortController = new AbortController();
      (async () => {
        try {
          const sinceSeq = seenEventSeqByRunRef.current[runId] ?? 0;
          const streamPath = `/projects/${projectId}/autonomous-deploy/${runId}/events?since_event_seq=${sinceSeq}`;
          const response = await api.stream(streamPath, {
            signal: sseAbortController?.signal,
          });

          if (!isMounted) return;
          lastEventTimestampRef.current = Date.now();
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
          if (err instanceof Error && err.name !== "AbortError") {
            setError(err.message);
          }
        } finally {
          if (isMounted) {
            setIsConnected(false);
            if (!isTerminalRef.current) {
              scheduleReconnect(3000);
            } else {
              setIsReconnecting(false);
            }
          }
        }
      })();
    };

    const connectSSE = () => {
      if (!isMounted) return;
      hasFallenBackToSse = true;
      setIsReconnecting(true);

      const sinceSeq = seenEventSeqByRunRef.current[runId] ?? 0;
      const sseUrl = buildSseUrl(projectId, runId, sinceSeq, authToken);

      if (typeof EventSource !== "undefined") {
        try {
          const es = new EventSource(sseUrl, { withCredentials: true });
          sseSource = es;

          es.onopen = () => {
            if (!isMounted) return;
            lastEventTimestampRef.current = Date.now();
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
            refetchSnapshot();
            es.close();
            sseSource = null;
            startFetchStream();
          };

          return;
        } catch {
          // EventSource instantiation failed; fall back to api.stream
        }
      }

      startFetchStream();
    };

    const connectWS = () => {
      const currentSinceSeq = seenEventSeqByRunRef.current[runId] ?? 0;
      const wsUrl = buildWebSocketUrl(projectId, runId, currentSinceSeq, authToken);

      if (typeof WebSocket === "undefined") {
        connectSSE();
        return;
      }

      try {
        ws = new WebSocket(wsUrl);

        ws.onopen = () => {
          if (!isMounted) return;
          lastEventTimestampRef.current = Date.now();
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
          refetchSnapshot();
          if (!hasFallenBackToSse) {
            connectSSE();
          } else {
            scheduleReconnect(3000);
          }
        };

        ws.onclose = (event) => {
          if (!isMounted) return;
          setIsConnected(false);
          if (event.code !== 1000) {
            refetchSnapshot();
            if (!hasFallenBackToSse) {
              connectSSE();
            } else {
              scheduleReconnect(3000);
            }
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
      reconnectStreamRef.current = null;
      cleanupActiveTransports();
      if (typeof document !== "undefined") {
        document.removeEventListener("visibilitychange", handleVisibilityChange);
      }
    };
  }, [projectId, runId, enabled, authToken, processEventFrame, refetchSnapshot]);

  // Bounded watchdog: detects quiet/stale streams and silent disconnections during long operations (e.g. docker build, npm ci)
  useEffect(() => {
    if (!enabled || !projectId || !runId || isTerminal) return;

    const intervalId = setInterval(() => {
      if (isTerminalRef.current) {
        clearInterval(intervalId);
        return;
      }
      const now = Date.now();
      const elapsed = now - (lastEventTimestampRef.current || now);

      if (elapsed >= stalenessThresholdMs) {
        // Reconcile snapshot over REST API to detect advances in backend build/status
        refetchSnapshot();
      }

      if (elapsed >= reconnectThresholdMs) {
        // Silent connection drop suspected: trigger reconnect with latest watermark
        lastEventTimestampRef.current = Date.now();
        reconnectStreamRef.current?.();
      }
    }, watchdogIntervalMs);

    return () => {
      clearInterval(intervalId);
    };
  }, [
    enabled,
    projectId,
    runId,
    isTerminal,
    stalenessThresholdMs,
    watchdogIntervalMs,
    reconnectThresholdMs,
    refetchSnapshot,
  ]);

  return {
    run,
    stages,
    logs,
    isConnected,
    isReconnecting: isTerminal ? false : isReconnecting,
    error: isTerminal ? null : error,
    refetchSnapshot,
  };
}
