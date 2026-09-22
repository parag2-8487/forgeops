// SPDX-License-Identifier: FSL-1.1-ALv2
/**
 * §2.2's live log stream.
 *
 * `EventSource` is stubbed rather than mocked away: the component's whole behaviour is which listener it
 * registers and what it does with each event, so a stub that records listeners and lets the test fire them
 * exercises exactly that. What is asserted is the three states staying apart — streaming, settled, and
 * failed — because a stream that failed and one that ended look identical without that distinction.
 */

import { render, screen } from "@testing-library/react";
import { act } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { DeploymentLogStream } from "@/features/deployments/DeploymentResult";

type Listener = (event: unknown) => void;

class StubEventSource {
  static last: StubEventSource | null = null;

  url: string;
  closed = false;
  private listeners: Record<string, Listener[]> = {};

  constructor(url: string) {
    this.url = url;
    StubEventSource.last = this;
  }

  addEventListener(name: string, listener: Listener): void {
    this.listeners[name] = [...(this.listeners[name] ?? []), listener];
  }

  close(): void {
    this.closed = true;
  }

  /** Fire one event, as the server would. */
  emit(name: string, data: unknown): void {
    for (const listener of this.listeners[name] ?? []) listener({ data });
  }
}

const original = globalThis.EventSource;

beforeEach(() => {
  StubEventSource.last = null;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any -- a stub stands in for a browser global
  (globalThis as any).EventSource = StubEventSource;
});

afterEach(() => {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any -- restore the browser global
  (globalThis as any).EventSource = original;
  vi.restoreAllMocks();
});

describe("the deployment log stream", () => {
  it("opens no stream for a deployment that was never delivered", () => {
    render(<DeploymentLogStream projectId="p1" deploymentId="d1" deliverable={false} />);
    // An EventSource on a pending_approval deployment would show an empty log, and an operator would read
    // that as "nothing is happening" rather than "a human has not approved it".
    expect(StubEventSource.last).toBeNull();
    expect(screen.getByTestId("deployment-log-undeliverable")).toHaveTextContent(
      "has not been sent",
    );
  });

  it("subscribes to the deployment's own stream", () => {
    render(<DeploymentLogStream projectId="p1" deploymentId="d1" deliverable />);
    expect(StubEventSource.last?.url).toContain("/projects/p1/deployments/d1/logs");
    expect(screen.getByTestId("deployment-log-state")).toHaveTextContent("streaming");
    // Nothing yet is stated, not left blank.
    expect(screen.getByTestId("deployment-log-empty")).toBeInTheDocument();
  });

  it("appends each log frame in order", () => {
    render(<DeploymentLogStream projectId="p1" deploymentId="d1" deliverable />);
    act(() => {
      StubEventSource.last?.emit("log", "applying 2 manifest(s)");
      StubEventSource.last?.emit("log", "waiting for deployment/api");
    });
    const lines = screen.getByTestId("deployment-log-lines");
    expect(lines).toHaveTextContent("applying 2 manifest(s)");
    expect(lines).toHaveTextContent("waiting for deployment/api");
    // Order matters in a log: the second line must follow the first.
    expect(lines.textContent?.indexOf("applying")).toBeLessThan(
      lines.textContent?.indexOf("waiting") ?? -1,
    );
  });

  it("closes the stream when the deployment settles, and says so", () => {
    render(<DeploymentLogStream projectId="p1" deploymentId="d1" deliverable />);
    act(() => {
      StubEventSource.last?.emit("complete", '{"status":"applied"}');
    });
    expect(screen.getByTestId("deployment-log-state")).toHaveTextContent("settled");
    // The socket is actually closed, not merely relabelled — a stream left open outlives the page.
    expect(StubEventSource.last?.closed).toBe(true);
  });

  it("keeps a failed stream apart from a finished one", () => {
    render(<DeploymentLogStream projectId="p1" deploymentId="d1" deliverable />);
    act(() => {
      StubEventSource.last?.emit("error", null);
    });
    const state = screen.getByTestId("deployment-log-state");
    expect(state).toHaveTextContent("the stream failed");
    // THE DISTINCTION: a failure must not read as "the deployment produced no output".
    expect(state).toHaveTextContent("not the same as the deployment producing no output");
  });

  it("closes the stream when the component goes away", () => {
    const view = render(<DeploymentLogStream projectId="p1" deploymentId="d1" deliverable />);
    const source = StubEventSource.last;
    view.unmount();
    expect(source?.closed).toBe(true);
  });
});
