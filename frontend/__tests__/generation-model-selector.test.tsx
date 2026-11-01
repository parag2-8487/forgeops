// SPDX-License-Identifier: FSL-1.1-ALv2
/**
 * Manual Generation's model selector: Ollama stays the default, Gemini is sent only when chosen, an
 * unconfigured provider cannot be chosen, and the live phase is shown while the run streams.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { GeneratorWizard } from "@/features/generation/GeneratorWizard";

const { mockStream, mockGet } = vi.hoisted(() => ({ mockStream: vi.fn(), mockGet: vi.fn() }));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, api: { ...actual.api, stream: mockStream, get: mockGet } };
});

const PROJECT = "00000000-0000-0000-0000-000000000001";
const OLLAMA = { endpoint_id: null, provider: "self_hosted", model: "qwen2.5-coder:7b", available: true, reason: null, default: true };
const GEMINI = { endpoint_id: "gemini-3.8-flash", provider: "google", model: "gemini-3.8-flash", available: true, reason: null, default: false };

function frame(event: string, data: unknown): string {
  return `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
}

function sse(chunks: string[]): Response {
  const encoder = new TextEncoder();
  return new Response(
    new ReadableStream<Uint8Array>({
      start(controller) {
        for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
        controller.close();
      },
    }),
    { status: 200, headers: { "content-type": "text/event-stream" } },
  );
}

beforeEach(() => {
  mockStream.mockReset();
  mockGet.mockReset();
  mockStream.mockResolvedValue(
    sse([frame("status", { run_id: "r1" }), frame("complete", { run_id: "r1", state: "accepted" })]),
  );
});

async function start(choose?: string) {
  render(<GeneratorWizard projectId={PROJECT} />);
  await waitFor(() => expect(screen.getByTestId("generation-model")).toHaveTextContent(/qwen2\.5-coder/));
  if (choose !== undefined) await userEvent.selectOptions(screen.getByTestId("generation-model"), choose);
  await userEvent.type(screen.getByLabelText(/what should be generated/i), "a static site");
  await userEvent.click(screen.getByRole("button", { name: /generate artifacts/i }));
  await waitFor(() => expect(mockStream).toHaveBeenCalledTimes(1));
  return JSON.parse(mockStream.mock.calls[0][1].body as string);
}

describe("the model selector", () => {
  it("keeps Ollama as the default and sends no endpoint", async () => {
    mockGet.mockResolvedValue([OLLAMA, GEMINI]);
    const body = await start();
    expect(body).toEqual({ project_id: PROJECT, prompt: "a static site" });
  });

  it("sends the Gemini endpoint only when Gemini is selected", async () => {
    mockGet.mockResolvedValue([OLLAMA, GEMINI]);
    const body = await start("gemini-3.8-flash");
    expect(body.endpoint_id).toBe("gemini-3.8-flash");
  });

  it("offers an unconfigured Gemini as disabled, with the reason", async () => {
    mockGet.mockResolvedValue([OLLAMA, { ...GEMINI, available: false, reason: "no credential is configured for 'google'" }]);
    render(<GeneratorWizard projectId={PROJECT} />);
    await waitFor(() => expect(screen.getByTestId("model-unavailable")).toHaveTextContent(/no credential/));
    const option = screen.getByRole("option", { name: /gemini-3\.8-flash/ }) as HTMLOptionElement;
    expect(option.disabled).toBe(true);
  });

  it("shows the phase the run is in while it streams", async () => {
    mockGet.mockResolvedValue([OLLAMA, GEMINI]);
    let release: () => void = () => undefined;
    const encoder = new TextEncoder();
    mockStream.mockResolvedValue(
      new Response(
        new ReadableStream<Uint8Array>({
          start(controller) {
            controller.enqueue(encoder.encode(frame("status", { run_id: "r2" })));
            controller.enqueue(
              encoder.encode(frame("progress", { state: "requesting_model", attempt: 1, max_attempts: 3 })),
            );
            release = () => {
              controller.enqueue(encoder.encode(frame("complete", { run_id: "r2", state: "accepted" })));
              controller.close();
            };
          },
        }),
        { status: 200, headers: { "content-type": "text/event-stream" } },
      ),
    );
    await start("gemini-3.8-flash");
    await waitFor(() =>
      expect(screen.getByTestId("generation-phase")).toHaveTextContent(/gemini-3\.8-flash: Waiting for the model.*attempt 1 of 3/),
    );
    release();
    await waitFor(() => expect(screen.queryByTestId("generation-phase")).toBeNull());
  });
});
