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
    expect(mockApiGet).toHaveBeenCalledWith(
      `/projects/${projectId}/autonomous-deploy/${runId}`,
    );

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

    expect(mockApiGet).toHaveBeenCalledWith(
      `/projects/${projectId}/autonomous-deploy/${runId}`,
    );

    await act(async () => {
      await Promise.resolve();
    });

    expect(result.current.run?.status).toBe("succeeded");
    expect(result.current.run?.progress_pct).toBe(100);
  });
});
