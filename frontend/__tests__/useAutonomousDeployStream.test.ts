// SPDX-License-Identifier: FSL-1.1-ALv2
import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mockApiGet = vi.fn();
const mockApiStream = vi.fn();

vi.mock("@/lib/api", () => ({
  api: {
    get: (...args: unknown[]) => mockApiGet(...args),
    stream: (...args: unknown[]) => mockApiStream(...args),
  },
}));

vi.mock("@/lib/env", () => ({
  env: {
    NEXT_PUBLIC_API_BASE_URL: "http://localhost:8000/api/v1",
    NEXT_PUBLIC_APP_NAME: "ForgeOps",
  },
}));

import {
  buildSseUrl,
  buildWebSocketUrl,
  useAutonomousDeployStream,
  type AutonomousLogEntry,
} from "@/features/deployments/useAutonomousDeployStream";
import type {
  AutonomousRunPublicResponse,
  StagePublicResponse,
} from "@/features/deployments/JenkinsPipelineDashboard";

// Mock WebSocket class
class MockWebSocket {
  static instances: MockWebSocket[] = [];
  url: string;
  onopen: (() => void) | null = null;
  onmessage: ((ev: { data: string }) => void) | null = null;
  onerror: ((ev: unknown) => void) | null = null;
  onclose: ((ev: { code: number }) => void) | null = null;
  readyState: number = 0;

  constructor(url: string) {
    this.url = url;
    MockWebSocket.instances.push(this);
  }

  open() {
    this.readyState = 1;
    this.onopen?.();
  }

  sendServerMessage(data: unknown) {
    this.onmessage?.({ data: JSON.stringify(data) });
  }

  triggerError(err: unknown = new Error("WS error")) {
    this.onerror?.(err);
  }

  close(code: number = 1000) {
    this.readyState = 3;
    this.onclose?.({ code });
  }
}

// Mock EventSource class
class MockEventSource {
  static instances: MockEventSource[] = [];
  url: string;
  onopen: (() => void) | null = null;
  onmessage: ((ev: { data: string }) => void) | null = null;
  onerror: ((ev: unknown) => void) | null = null;
  listeners: Map<string, ((ev: { data: string }) => void)[]> = new Map();

  constructor(url: string) {
    this.url = url;
    MockEventSource.instances.push(this);
  }

  addEventListener(type: string, listener: (ev: { data: string }) => void) {
    const list = this.listeners.get(type) || [];
    list.push(listener);
    this.listeners.set(type, list);
  }

  open() {
    this.onopen?.();
  }

  sendServerMessage(data: unknown, eventType: string = "message") {
    const ev = { data: JSON.stringify(data) };
    if (eventType === "message") {
      this.onmessage?.(ev);
    }
    const handlers = this.listeners.get(eventType);
    if (handlers) {
      for (const h of handlers) h(ev);
    }
  }

  triggerError(err: unknown = new Error("SSE error")) {
    this.onerror?.(err);
  }

  close() {}
}

describe("useAutonomousDeployStream", () => {
  const projectId = "11111111-1111-4111-8111-111111111111";
  const runId = "22222222-2222-4222-8222-222222222222";

  const initialStages: StagePublicResponse[] = [
    {
      id: "s1",
      run_id: runId,
      stage_name: "G1_blueprint",
      gate_id: "G1",
      position: 1,
      status: "pending",
      progress_pct: 0,
    },
    {
      id: "s2",
      run_id: runId,
      stage_name: "G2_artifact",
      gate_id: "G2",
      position: 2,
      status: "pending",
      progress_pct: 0,
    },
  ];

  const initialRun: AutonomousRunPublicResponse = {
    id: runId,
    project_id: projectId,
    attempt_number: 1,
    status: "pending",
    strategy: "docker_github_vercel",
    progress_pct: 0,
    agent_connected: true,
    stages: initialStages,
  };

  beforeEach(() => {
    vi.clearAllMocks();
    MockWebSocket.instances = [];
    MockEventSource.instances = [];
    // @ts-expect-error test mock injection
    globalThis.WebSocket = MockWebSocket;
    // @ts-expect-error test mock injection
    globalThis.EventSource = MockEventSource;
  });

  afterEach(() => {
    // @ts-expect-error cleanup mock
    delete globalThis.WebSocket;
    // @ts-expect-error cleanup mock
    delete globalThis.EventSource;
  });

  it("builds correct WebSocket and SSE endpoint URLs with since_event_seq", () => {
    const wsUrl = buildWebSocketUrl(projectId, runId, 15);
    expect(wsUrl).toBe(
      `ws://localhost:8000/api/v1/projects/${projectId}/autonomous-deploy/${runId}/ws?since_event_seq=15`,
    );

    const sseUrl = buildSseUrl(projectId, runId, 15);
    expect(sseUrl).toBe(
      `http://localhost:8000/api/v1/projects/${projectId}/autonomous-deploy/${runId}/events?since_event_seq=15`,
    );
  });

  it("connects to WebSocket and tracks isConnected state", async () => {
    const { result } = renderHook(() =>
      useAutonomousDeployStream({
        projectId,
        runId,
        initialRun,
      }),
    );

    expect(MockWebSocket.instances.length).toBe(1);
    const ws = MockWebSocket.instances[0];
    expect(ws.url).toContain(`/ws?since_event_seq=0`);
    expect(result.current.isConnected).toBe(false);

    act(() => {
      ws.open();
    });

    expect(result.current.isConnected).toBe(true);
    expect(result.current.isReconnecting).toBe(false);
  });

  it("deduplicates events using the watermark event_seq <= seen_event_seq", () => {
    const { result } = renderHook(() =>
      useAutonomousDeployStream({
        projectId,
        runId,
        initialRun,
      }),
    );

    const ws = MockWebSocket.instances[0];
    act(() => {
      ws.open();
    });

    // Send first event with seq=1 (run_update to running)
    act(() => {
      ws.sendServerMessage({
        event_seq: 1,
        event_type: "run_update",
        payload: { status: "running", progress_pct: 10 },
      });
    });

    expect(result.current.run?.status).toBe("running");
    expect(result.current.run?.progress_pct).toBe(10);

    // Send duplicate event with seq=1 but modified progress_pct=99
    // Watermark rule: event_seq <= seen_event_seq MUST be dropped
    act(() => {
      ws.sendServerMessage({
        event_seq: 1,
        event_type: "run_update",
        payload: { status: "running", progress_pct: 99 },
      });
    });

    // Should remain 10, NOT updated to 99
    expect(result.current.run?.progress_pct).toBe(10);
  });

  it("detects sequence gaps and triggers authoritative REST snapshot refetch", async () => {
    const freshSnapshot: AutonomousRunPublicResponse = {
      ...initialRun,
      status: "running",
      progress_pct: 50,
      stages: [
        { ...initialStages[0], status: "succeeded", progress_pct: 100 },
        { ...initialStages[1], status: "running", progress_pct: 50 },
      ],
    };
    mockApiGet.mockResolvedValueOnce(freshSnapshot);
    mockApiGet.mockResolvedValueOnce({ logs: [], total_lines: 0 });

    const { result } = renderHook(() =>
      useAutonomousDeployStream({
        projectId,
        runId,
        initialRun,
      }),
    );

    const ws = MockWebSocket.instances[0];
    act(() => {
      ws.open();
    });

    // First event: event_seq=1
    act(() => {
      ws.sendServerMessage({
        event_seq: 1,
        event_type: "stage_update",
        payload: { stage_name: "G1_blueprint", status: "running", progress_pct: 20 },
      });
    });

    expect(mockApiGet).not.toHaveBeenCalled();

    // Sequence gap: event_seq=4 (missed 2 and 3!)
    act(() => {
      ws.sendServerMessage({
        event_seq: 4,
        event_type: "stage_update",
        payload: { stage_name: "G2_artifact", status: "running", progress_pct: 50 },
      });
    });

    // Authoritative snapshot must have been triggered
    expect(mockApiGet).toHaveBeenCalledWith(`/projects/${projectId}/autonomous-deploy/${runId}`);

    // Wait for snapshot reconciliation to resolve
    await act(async () => {
      await Promise.resolve();
    });

    expect(result.current.run?.progress_pct).toBe(50);
  });

  it("falls back from WebSocket to SSE when WebSocket connection drops or errors", () => {
    const { result } = renderHook(() =>
      useAutonomousDeployStream({
        projectId,
        runId,
        initialRun,
      }),
    );

    expect(MockWebSocket.instances.length).toBe(1);
    const ws = MockWebSocket.instances[0];

    // Trigger error on WebSocket
    act(() => {
      ws.triggerError();
      ws.close(1006);
    });

    expect(result.current.isConnected).toBe(false);
    expect(result.current.isReconnecting).toBe(true);

    // EventSource fallback should have been instantiated
    expect(MockEventSource.instances.length).toBe(1);
    const sse = MockEventSource.instances[0];
    expect(sse.url).toContain(`/events?since_event_seq=0`);

    // Open SSE and send event
    act(() => {
      sse.open();
    });

    expect(result.current.isConnected).toBe(true);
    expect(result.current.isReconnecting).toBe(false);

    act(() => {
      sse.sendServerMessage(
        {
          event_seq: 2,
          event_type: "stage_update",
          payload: {
            stage_name: "G1_blueprint",
            status: "succeeded",
            progress_pct: 100,
          },
        },
        "stage_update",
      );
    });

    const g1 = result.current.stages.find((s) => s.stage_name === "G1_blueprint");
    expect(g1?.status).toBe("succeeded");
    expect(g1?.progress_pct).toBe(100);
  });

  it("appends and deduplicates logs strictly sorted by log_seq ASC", () => {
    const { result } = renderHook(() =>
      useAutonomousDeployStream({
        projectId,
        runId,
        initialRun,
      }),
    );

    const ws = MockWebSocket.instances[0];
    act(() => {
      ws.open();
    });

    // Ingest out-of-order logs in batch 1
    const batch1: AutonomousLogEntry[] = [
      { log_seq: 3, stage_name: "G1_blueprint", level: "INFO", message: "Third line" },
      { log_seq: 1, stage_name: "G1_blueprint", level: "INFO", message: "First line" },
      { log_seq: 4, stage_name: "G1_blueprint", level: "WARN", message: "Fourth line" },
    ];

    act(() => {
      ws.sendServerMessage({
        event_seq: 1,
        event_type: "log_batch",
        payload: { logs: batch1 },
      });
    });

    expect(result.current.logs.map((l) => l.log_seq)).toEqual([1, 3, 4]);

    // Ingest batch 2 filling the gap with log_seq=2 and duplicate log_seq=3
    const batch2: AutonomousLogEntry[] = [
      { log_seq: 2, stage_name: "G1_blueprint", level: "INFO", message: "Second line" },
      { log_seq: 3, stage_name: "G1_blueprint", level: "INFO", message: "Third line dup" },
      { log_seq: 5, stage_name: "G2_artifact", level: "INFO", message: "Fifth line" },
    ];

    act(() => {
      ws.sendServerMessage({
        event_seq: 2,
        event_type: "log_batch",
        payload: { logs: batch2 },
      });
    });

    // Strictly ascending without duplicate sequence numbers: 1, 2, 3, 4, 5
    expect(result.current.logs.map((l) => l.log_seq)).toEqual([1, 2, 3, 4, 5]);
    expect(result.current.logs[0].message).toBe("First line");
    expect(result.current.logs[1].message).toBe("Second line");
    expect(result.current.logs[4].message).toBe("Fifth line");
  });

  it("reconciles state on window visibility change", async () => {
    const freshRun: AutonomousRunPublicResponse = {
      ...initialRun,
      status: "succeeded",
      progress_pct: 100,
    };
    mockApiGet.mockResolvedValueOnce(freshRun);
    mockApiGet.mockResolvedValueOnce({ logs: [], total_lines: 0 });

    const { result } = renderHook(() =>
      useAutonomousDeployStream({
        projectId,
        runId,
        initialRun,
      }),
    );

    // Mock document.visibilityState
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      value: "visible",
    });

    act(() => {
      document.dispatchEvent(new Event("visibilitychange"));
    });

    expect(mockApiGet).toHaveBeenCalledWith(`/projects/${projectId}/autonomous-deploy/${runId}`);

    await act(async () => {
      await Promise.resolve();
    });

    expect(result.current.run?.status).toBe("succeeded");
    expect(result.current.run?.progress_pct).toBe(100);
  });

  it("falls back to SSE when WebSocket closes abnormally", async () => {
    const { result } = renderHook(() =>
      useAutonomousDeployStream({
        projectId,
        runId,
        initialRun,
      }),
    );

    const ws = MockWebSocket.instances[MockWebSocket.instances.length - 1];
    expect(ws).toBeDefined();

    act(() => {
      ws.open();
    });
    expect(result.current.isConnected).toBe(true);

    // Abnormally close WS (code 1006)
    act(() => {
      ws.close(1006);
    });

    // Check fallback to EventSource
    expect(MockEventSource.instances.length).toBeGreaterThan(0);
    const sse = MockEventSource.instances[MockEventSource.instances.length - 1];

    act(() => {
      sse.open();
      sse.sendServerMessage({
        event_seq: 1,
        event_type: "run_progress",
        payload: { progress_pct: 75, status: "running" },
      });
    });

    expect(result.current.run?.progress_pct).toBe(75);
  });

  it("falls back to SSE when WebSocket is undefined in global scope", () => {
    const origWS = window.WebSocket;
    // @ts-expect-error simulating environment without WebSocket
    delete window.WebSocket;

    try {
      const { result } = renderHook(() =>
        useAutonomousDeployStream({
          projectId,
          runId,
          initialRun,
        }),
      );

      expect(MockEventSource.instances.length).toBeGreaterThan(0);
      const sse = MockEventSource.instances[MockEventSource.instances.length - 1];

      act(() => {
        sse.open();
        sse.sendServerMessage({
          event_seq: 1,
          event_type: "run_progress",
          payload: { progress_pct: 90, status: "running" },
        });
      });

      expect(result.current.run?.progress_pct).toBe(90);
    } finally {
      window.WebSocket = origWS;
    }
  });

  it("reconciles pending stages when initialRun updates with authoritative stage results", () => {
    const pendingStages: StagePublicResponse[] = [
      {
        id: "s1",
        run_id: runId,
        stage_name: "G1_blueprint",
        position: 1,
        status: "pending",
        progress_pct: 0,
      },
      {
        id: "s2",
        run_id: runId,
        stage_name: "G4_build",
        position: 2,
        status: "pending",
        progress_pct: 0,
      },
    ];

    const initialPendingRun: AutonomousRunPublicResponse = {
      ...initialRun,
      status: "pending",
      progress_pct: 0,
      stages: pendingStages,
    };

    const { result, rerender } = renderHook(
      ({ runProp }) =>
        useAutonomousDeployStream({
          projectId,
          runId,
          initialRun: runProp,
        }),
      { initialProps: { runProp: initialPendingRun } },
    );

    expect(result.current.stages[0].status).toBe("pending");
    expect(result.current.stages[1].status).toBe("pending");

    // Server snapshot updates with G1 succeeded and G4 failed
    const updatedAuthoritativeRun: AutonomousRunPublicResponse = {
      ...initialRun,
      status: "failed",
      progress_pct: 32,
      stages: [
        { ...pendingStages[0], status: "succeeded", progress_pct: 100 },
        {
          ...pendingStages[1],
          status: "failed",
          progress_pct: 10,
          error_message: "image not found",
        },
      ],
    };

    rerender({ runProp: updatedAuthoritativeRun });

    expect(result.current.stages[0].status).toBe("succeeded");
    expect(result.current.stages[0].progress_pct).toBe(100);
    expect(result.current.stages[1].status).toBe("failed");
    expect(result.current.stages[1].error_message).toBe("image not found");
    expect(result.current.run?.status).toBe("failed");
    expect(result.current.run?.progress_pct).toBe(32);
  });

  it("resets internal state when runId changes on retry navigation", () => {
    const { result, rerender } = renderHook(
      ({ rId, currentRun }: { rId: string; currentRun?: AutonomousRunPublicResponse }) =>
        useAutonomousDeployStream({
          projectId,
          runId: rId,
          initialRun: currentRun,
        }),
      {
        initialProps: {
          rId: "run-1",
          currentRun: {
            ...initialRun,
            id: "run-1",
            status: "failed",
            progress_pct: 32,
          } as AutonomousRunPublicResponse,
        },
      },
    );

    expect(result.current.run?.id).toBe("run-1");

    // Navigate to new retry attempt
    rerender({
      rId: "run-2",
      currentRun: {
        ...initialRun,
        id: "run-2",
        attempt_number: 2,
        parent_run_id: "run-1",
        status: "pending" as const,
        progress_pct: 0,
        stages: [
          {
            id: "s-retry-1",
            run_id: "run-2",
            stage_name: "G1_blueprint",
            position: 1,
            status: "pending",
            progress_pct: 0,
          },
        ],
      },
    });

    expect(result.current.run?.id).toBe("run-2");
    expect(result.current.run?.status).toBe("pending");
    expect(result.current.run?.progress_pct).toBe(0);
    expect(result.current.stages[0].run_id).toBe("run-2");
  });

  it("preserves monotonic progress and completed stages when snapshot refetch returns degraded/stale state", async () => {
    mockApiStream.mockReturnValue(
      new ReadableStream({
        start() {},
      }),
    );

    const initialRunningRun: AutonomousRunPublicResponse = {
      ...initialRun,
      status: "running",
      progress_pct: 75,
      current_stage: "G6_workload",
      stages: [
        { ...initialStages[0], status: "succeeded", progress_pct: 100 },
        { ...initialStages[1], status: "succeeded", progress_pct: 100 },
      ],
    };

    const { result } = renderHook(() =>
      useAutonomousDeployStream({
        projectId,
        runId,
        initialRun: initialRunningRun,
      }),
    );

    expect(result.current.run?.progress_pct).toBe(75);
    expect(result.current.stages[0].status).toBe("succeeded");

    // Simulate backend rollback scenario where snapshot returns 0% and all pending stages
    const degradedRolledBackSnapshot: AutonomousRunPublicResponse = {
      ...initialRun,
      status: "running",
      progress_pct: 0,
      current_stage: undefined,
      stages: initialStages.map((s) => ({ ...s, status: "pending", progress_pct: 0 })),
    };

    mockApiGet.mockResolvedValueOnce(degradedRolledBackSnapshot);
    mockApiGet.mockResolvedValueOnce({ logs: [] });

    await act(async () => {
      await result.current.refetchSnapshot();
    });

    // Progress and succeeded stages MUST NOT be wiped out
    expect(result.current.run?.progress_pct).toBe(75);
    expect(result.current.run?.current_stage).toBe("G6_workload");
    expect(result.current.stages[0].status).toBe("succeeded");
    expect(result.current.stages[1].status).toBe("succeeded");
  });

  it("ignores regress in stage status and progress from stale event frames", async () => {
    mockApiStream.mockReturnValue(
      new ReadableStream({
        start() {},
      }),
    );

    const stableRunningRun: AutonomousRunPublicResponse = {
      ...initialRun,
      status: "running",
      progress_pct: 50,
      stages: [
        { ...initialStages[0], status: "succeeded", progress_pct: 100 },
        { ...initialStages[1], status: "running", progress_pct: 40 },
      ],
    };

    const { result } = renderHook(() =>
      useAutonomousDeployStream({
        projectId,
        runId,
        initialRun: stableRunningRun,
      }),
    );

    // Stale event attempting to reset G1 back to pending with 0% progress
    act(() => {
      const ws = MockWebSocket.instances[0];
      ws?.sendServerMessage({
        event_seq: 10,
        event_type: "stage_update",
        payload: {
          stage_name: "G1_blueprint",
          status: "pending",
          progress_pct: 0,
          run_progress_pct: 20,
        },
      });
    });

    expect(result.current.stages[0].status).toBe("succeeded");
    expect(result.current.stages[0].progress_pct).toBe(100);
    expect(result.current.run?.progress_pct).toBe(50);
  });

  it("detects silent stale stream and reconciles snapshot over REST API", async () => {
    vi.useFakeTimers();
    try {
      const runningRun: AutonomousRunPublicResponse = {
        ...initialRun,
        status: "running",
        progress_pct: 25,
      };

      const freshSnapshot: AutonomousRunPublicResponse = {
        ...initialRun,
        status: "running",
        progress_pct: 70,
        stages: [
          { ...initialStages[0], status: "succeeded", progress_pct: 100 },
          { ...initialStages[1], status: "running", progress_pct: 40 },
        ],
      };
      mockApiGet.mockResolvedValueOnce(freshSnapshot);
      mockApiGet.mockResolvedValueOnce({
        logs: [{ log_seq: 1, stage_name: "G1_blueprint", level: "INFO", message: "Snapshot log" }],
        total_lines: 1,
      });

      const { result } = renderHook(() =>
        useAutonomousDeployStream({
          projectId,
          runId,
          initialRun: runningRun,
          stalenessThresholdMs: 200,
          watchdogIntervalMs: 100,
        }),
      );

      // Stream opens but receives no events (simulating silent freeze or quiet build)
      const ws = MockWebSocket.instances[MockWebSocket.instances.length - 1];
      act(() => {
        ws.open();
      });

      expect(mockApiGet).not.toHaveBeenCalled();

      // Advance time past the staleness threshold (250ms)
      await act(async () => {
        vi.advanceTimersByTime(250);
        await Promise.resolve();
      });

      // Watchdog must have triggered snapshot reconciliation
      expect(mockApiGet).toHaveBeenCalledWith(`/projects/${projectId}/autonomous-deploy/${runId}`);
      expect(result.current.run?.progress_pct).toBe(70);
      expect(result.current.stages[0].status).toBe("succeeded");
      expect(result.current.logs.some((l) => l.message === "Snapshot log")).toBe(true);
    } finally {
      vi.useRealTimers();
    }
  });

  it("stops watchdog polling when deployment reaches a terminal state", async () => {
    vi.useFakeTimers();
    try {
      const terminalRun: AutonomousRunPublicResponse = {
        ...initialRun,
        status: "succeeded",
        progress_pct: 100,
      };

      renderHook(() =>
        useAutonomousDeployStream({
          projectId,
          runId,
          initialRun: terminalRun,
          stalenessThresholdMs: 100,
          watchdogIntervalMs: 50,
        }),
      );

      mockApiGet.mockClear();

      // Advance time by 300ms
      await act(async () => {
        vi.advanceTimersByTime(300);
        await Promise.resolve();
      });

      // Because the run is terminal, watchdog timer must not poll
      expect(mockApiGet).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });

  it("cleans up watchdog interval and active sockets on component unmount", async () => {
    vi.useFakeTimers();
    try {
      const runningRun: AutonomousRunPublicResponse = {
        ...initialRun,
        status: "running",
        progress_pct: 10,
      };

      const { unmount } = renderHook(() =>
        useAutonomousDeployStream({
          projectId,
          runId,
          initialRun: runningRun,
          stalenessThresholdMs: 100,
          watchdogIntervalMs: 50,
        }),
      );

      const ws = MockWebSocket.instances[MockWebSocket.instances.length - 1];
      act(() => {
        ws.open();
      });

      mockApiGet.mockClear();

      // Unmount component
      unmount();

      // Advance time by 500ms
      await act(async () => {
        vi.advanceTimersByTime(500);
        await Promise.resolve();
      });

      // No watchdog calls made after unmount
      expect(mockApiGet).not.toHaveBeenCalled();
      // WebSocket closed on unmount
      expect(ws.readyState).toBe(3);
    } finally {
      vi.useRealTimers();
    }
  });

  it("triggers immediate snapshot reconciliation and initiates reconnect on EventSource error", async () => {
    const freshRun: AutonomousRunPublicResponse = {
      ...initialRun,
      status: "running",
      progress_pct: 55,
    };
    mockApiGet.mockResolvedValueOnce(freshRun);
    mockApiGet.mockResolvedValueOnce({ logs: [] });

    renderHook(() =>
      useAutonomousDeployStream({
        projectId,
        runId,
        initialRun,
      }),
    );

    const ws = MockWebSocket.instances[MockWebSocket.instances.length - 1];
    // WS error falls back to SSE
    act(() => {
      ws.triggerError();
      ws.close(1006);
    });

    await act(async () => {
      await Promise.resolve();
    });

    const sse = MockEventSource.instances[MockEventSource.instances.length - 1];
    act(() => {
      sse.open();
    });

    mockApiGet.mockClear();
    mockApiGet.mockResolvedValueOnce(freshRun);
    mockApiGet.mockResolvedValueOnce({ logs: [] });

    // SSE encounters transport error
    act(() => {
      sse.triggerError(new Error("Network drop"));
    });

    // Must immediately invoke snapshot reconciliation to avoid freezing UI
    expect(mockApiGet).toHaveBeenCalledWith(`/projects/${projectId}/autonomous-deploy/${runId}`);
  });

  it("reconnects stream without duplicate logs or regressions in stage status", async () => {
    const { result } = renderHook(() =>
      useAutonomousDeployStream({
        projectId,
        runId,
        initialRun,
      }),
    );

    const ws = MockWebSocket.instances[MockWebSocket.instances.length - 1];
    act(() => {
      ws.open();
      ws.sendServerMessage({
        event_seq: 1,
        event_type: "log_batch",
        payload: {
          logs: [
            { log_seq: 1, stage_name: "G1_blueprint", level: "INFO", message: "Blueprint ok" },
            { log_seq: 2, stage_name: "G4_build", level: "INFO", message: "Build starting" },
          ],
        },
      });
      ws.sendServerMessage({
        event_seq: 2,
        event_type: "stage_update",
        payload: { stage_name: "G1_blueprint", status: "succeeded", progress_pct: 100 },
      });
    });

    expect(result.current.logs.map((l) => l.log_seq)).toEqual([1, 2]);
    expect(result.current.stages[0].status).toBe("succeeded");

    // Replay on SSE after connection drop with overlapping and new logs
    act(() => {
      ws.triggerError();
      ws.close(1006);
    });

    const sse = MockEventSource.instances[MockEventSource.instances.length - 1];
    act(() => {
      sse.open();
      // Replayed batch overlapping log 2 and adding log 3
      sse.sendServerMessage(
        {
          event_seq: 3,
          event_type: "log_batch",
          payload: {
            logs: [
              { log_seq: 2, stage_name: "G4_build", level: "INFO", message: "Build starting" },
              { log_seq: 3, stage_name: "G4_build", level: "INFO", message: "npm ci done" },
            ],
          },
        },
        "log_batch",
      );
    });

    // Duplicate log 2 must be deduplicated, monotonic ordering: [1, 2, 3]
    expect(result.current.logs.map((l) => l.log_seq)).toEqual([1, 2, 3]);
    // Succeeded stage G1 must not regress
    expect(result.current.stages[0].status).toBe("succeeded");
  });
});
