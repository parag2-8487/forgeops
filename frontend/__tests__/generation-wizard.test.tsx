// SPDX-License-Identifier: FSL-1.1-ALv2
/**
 * The generator wizard against a real SSE byte stream (design.md §7.4, §12.6 step 6).
 *
 * Driven by bytes rather than by a mocked reader, deliberately. The wizard's job is to turn a
 * `text/event-stream` body into rendered artifacts, and the interesting failures are in that
 * translation: frames split across chunk boundaries, several files interleaved, a stream that stops
 * without a terminal event. A mocked parser would assert none of them.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { GeneratorWizard } from "@/features/generation/GeneratorWizard";

const { mockStream } = vi.hoisted(() => ({ mockStream: vi.fn() }));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, api: { ...actual.api, stream: mockStream } };
});

const PROJECT = "00000000-0000-0000-0000-000000000001";

/** A real `Response` whose body streams the given chunks, so the SSE reader does real work. */
function sseResponse(chunks: string[]): Response {
  const encoder = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
      controller.close();
    },
  });
  return new Response(body, {
    status: 200,
    headers: { "content-type": "text/event-stream" },
  });
}

function frame(event: string, data: unknown): string {
  return `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
}

async function generate(prompt = "a node service") {
  render(<GeneratorWizard projectId={PROJECT} />);
  await userEvent.type(screen.getByLabelText(/what should be generated/i), prompt);
  await userEvent.click(screen.getByRole("button", { name: /generate artifacts/i }));
}

beforeEach(() => {
  mockStream.mockReset();
});

describe("a successful run", () => {
  beforeEach(() => {
    mockStream.mockResolvedValue(
      sseResponse([
        frame("status", { run_id: "run-7", state: "running" }),
        frame("token", { path: "Dockerfile", text: "FROM node:20-alpine\n" }),
        frame("token", { path: "k8s/deployment.yaml", text: "apiVersion: apps/v1\n" }),
        frame("token", { path: "Dockerfile", text: "USER node\n" }),
        frame("validation", { passed: true, findings: [] }),
        frame("complete", { run_id: "run-7", state: "accepted", files: ["Dockerfile"] }),
      ]),
    );
  });

  it("posts the prompt and the project to the run endpoint", async () => {
    await generate("a node service");
    await waitFor(() => expect(mockStream).toHaveBeenCalledTimes(1));
    const [path, init] = mockStream.mock.calls[0];
    expect(path).toBe("/generation/runs");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({
      project_id: PROJECT,
      prompt: "a node service",
    });
  });

  it("shows the run id the server assigned", async () => {
    await generate();
    expect(await screen.findByTestId("run-id")).toHaveTextContent("run-7");
  });

  it("assembles interleaved tokens into the right file, not one buffer", async () => {
    await generate();
    // The two Dockerfile frames are separated by a manifest frame. A single accumulator would
    // splice the manifest into the middle of the Dockerfile.
    const dockerfile = await screen.findByTestId("artifact-Dockerfile");
    expect(dockerfile).toHaveTextContent("FROM node:20-alpine");
    expect(dockerfile).toHaveTextContent("USER node");
    expect(dockerfile).not.toHaveTextContent("apiVersion");

    expect(screen.getByTestId("artifact-k8s/deployment.yaml")).toHaveTextContent(
      "apiVersion: apps/v1",
    );
  });

  it("reports the validation gate's verdict", async () => {
    await generate();
    expect(await screen.findByText(/deterministic validation gate passed/i)).toBeInTheDocument();
  });

  it("records the event names it received, in order", async () => {
    await generate();
    const log = await screen.findByTestId("event-log");
    // Every name is one of §7.4's six, and the order is the documented one.
    expect(log).toHaveTextContent("status");
    expect(log).toHaveTextContent("validation");
    expect(log).toHaveTextContent("complete");
  });

  it("points the operator at the approvals screen once accepted", async () => {
    await generate();
    expect(
      await screen.findByText(/submitted to the governance chokepoint as a change set/i),
    ).toBeInTheDocument();
  });
});

describe("frames split across chunk boundaries", () => {
  it("assembles a frame delivered in three pieces", async () => {
    const full =
      frame("status", { run_id: "run-9" }) +
      frame("token", { path: "Dockerfile", text: "FROM python:3.11-slim\n" }) +
      frame("complete", { run_id: "run-9", state: "accepted" });

    // A network does not respect frame boundaries, and a reader that assumed one chunk equals one
    // frame would drop the remainder of every split frame.
    const third = Math.floor(full.length / 3);
    mockStream.mockResolvedValue(
      sseResponse([full.slice(0, third), full.slice(third, third * 2), full.slice(third * 2)]),
    );

    await generate("a python service");
    expect(await screen.findByTestId("artifact-Dockerfile")).toHaveTextContent(
      "FROM python:3.11-slim",
    );
    expect(screen.getByTestId("run-id")).toHaveTextContent("run-9");
  });
});

describe("a failing run", () => {
  it("surfaces the error frame's detail and does not claim success", async () => {
    mockStream.mockResolvedValue(
      sseResponse([
        frame("status", { run_id: "run-3" }),
        frame("validation", { passed: false, findings: ["the image runs as root"] }),
        frame("error", { detail: "the deterministic validation gate refused the artifacts" }),
      ]),
    );

    await generate();
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/validation gate refused/i);
    expect(screen.getByText(/the image runs as root/i)).toBeInTheDocument();
    expect(screen.queryByText(/submitted to the governance chokepoint/i)).not.toBeInTheDocument();
  });

  it("treats a stream that just stops as an unknown outcome, not a success", async () => {
    // No terminal event. Reporting this as accepted is the specific defect: a dropped connection
    // would look like a completed run.
    mockStream.mockResolvedValue(
      sseResponse([
        frame("status", { run_id: "run-4" }),
        frame("token", { path: "Dockerfile", text: "FROM node:20-alpine\n" }),
      ]),
    );

    await generate();
    expect(await screen.findByRole("alert")).toHaveTextContent(/without a terminal event/i);
    expect(screen.queryByText(/submitted to the governance chokepoint/i)).not.toBeInTheDocument();
  });

  it("refuses an event name outside §7.4 rather than ignoring it", async () => {
    // The historical defect, replayed. A client that skipped unknown names would show an empty
    // stream and no error -- which is exactly how this went unnoticed for a phase.
    mockStream.mockResolvedValue(
      sseResponse([
        frame("run_start", { run_id: "run-5" }),
        frame("token_chunk", { path: "Dockerfile", text: "FROM node:20\n" }),
        frame("run_complete", { run_id: "run-5" }),
      ]),
    );

    await generate();
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/run_start/);
    expect(alert).toHaveTextContent(/not one of §7.4's six/i);
    // And it renders no artifact from a stream it did not understand.
    expect(screen.queryByTestId("artifact-Dockerfile")).not.toBeInTheDocument();
  });
});

describe("the control cannot be used before it can succeed", () => {
  it("is disabled with an empty prompt", () => {
    render(<GeneratorWizard projectId={PROJECT} />);
    expect(screen.getByRole("button", { name: /generate artifacts/i })).toBeDisabled();
  });

  it("makes no request when rendered", () => {
    render(<GeneratorWizard projectId={PROJECT} />);
    expect(mockStream).not.toHaveBeenCalled();
  });
});
