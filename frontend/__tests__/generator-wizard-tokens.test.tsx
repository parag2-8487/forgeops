// SPDX-License-Identifier: FSL-1.1-ALv2
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { GeneratorWizard } from "@/features/generation/GeneratorWizard";

/**
 * The wizard must PAINT tokens that carry no `path`.
 *
 * WHY THIS IS A DEFECT AND NOT A STYLE CHOICE
 *
 * `token` events from `generation/service.py` carry `{run_id, text, attempt}` and no `path`, and they
 * cannot carry one: while the model is still producing, the output is a single undifferentiated blob
 * and which file a given token lands in is not known until `parse_artifacts` runs at the end.
 *
 * The wizard's token branch was `if (payload.path) { ...buffer... }` with a comment explaining that
 * per-path buffering avoids "splicing a Dockerfile into a Kubernetes manifest" — a reasonable-sounding
 * assumption about a wire shape that does not exist. The condition was false for every token, so
 * nothing was buffered and no output element was ever rendered. The bytes arrived correctly and the
 * screen stayed empty.
 *
 * That is how criterion 13 — "LLM tokens stream to frontend" — was untrue of the frontend while every
 * server-side well-formedness test passed. A test at the transport cannot catch it; only one that
 * looks at what was rendered can.
 */

/** A minimal SSE body in the shape `readSSEResponse` parses. */
function sseBody(events: Array<{ event: string; data: unknown }>): string {
  return events.map((e) => `event: ${e.event}\ndata: ${JSON.stringify(e.data)}\n\n`).join("");
}

function stubStream(body: string) {
  return vi.fn().mockResolvedValue(
    new Response(body, {
      status: 200,
      headers: { "content-type": "text/event-stream" },
    }),
  );
}

vi.mock("@/lib/api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api/client")>();
  return { ...actual, api: { ...actual.api, stream: vi.fn() } };
});

describe("GeneratorWizard token rendering", () => {
  it("paints tokens that carry no path", async () => {
    const { api } = await import("@/lib/api/client");
    const run = "11111111-1111-1111-1111-111111111111";
    (api.stream as unknown as ReturnType<typeof vi.fn>) = stubStream(
      sseBody([
        { event: "status", data: { run_id: run } },
        // No `path` — exactly what the server sends.
        { event: "token", data: { run_id: run, text: "FROM node:20-alpine\n", attempt: 1 } },
        { event: "token", data: { run_id: run, text: "WORKDIR /app\n", attempt: 1 } },
        { event: "complete", data: { run_id: run } },
      ]),
    );

    render(<GeneratorWizard projectId="22222222-2222-2222-2222-222222222222" />);
    await userEvent.type(screen.getByRole("textbox"), "a node service");
    await userEvent.click(screen.getByRole("button", { name: /generate artifacts/i }));

    // The assertion that the old code failed: the text is on screen.
    await waitFor(() => {
      expect(screen.getByTestId("stream-output")).toHaveTextContent("FROM node:20-alpine");
    });
    expect(screen.getByTestId("stream-output")).toHaveTextContent("WORKDIR /app");
    // Accumulated in order rather than replaced, so the last token does not erase the first.
    const painted = screen.getByTestId("stream-output").textContent ?? "";
    expect(painted.indexOf("FROM")).toBeLessThan(painted.indexOf("WORKDIR"));
  });

  it("still buffers per path when the server does say which file a token belongs to", async () => {
    // The forward-compatible half. If the stream ever gains `path`, the per-file rendering must keep
    // working — the fix widened the behaviour rather than replacing it.
    const { api } = await import("@/lib/api/client");
    const run = "33333333-3333-3333-3333-333333333333";
    (api.stream as unknown as ReturnType<typeof vi.fn>) = stubStream(
      sseBody([
        { event: "status", data: { run_id: run } },
        {
          event: "token",
          data: { run_id: run, text: "FROM scratch\n", path: "Dockerfile", attempt: 1 },
        },
        { event: "complete", data: { run_id: run } },
      ]),
    );

    render(<GeneratorWizard projectId="44444444-4444-4444-4444-444444444444" />);
    await userEvent.type(screen.getByRole("textbox"), "a service");
    await userEvent.click(screen.getByRole("button", { name: /generate artifacts/i }));

    await waitFor(() => {
      expect(screen.getByTestId("artifact-Dockerfile")).toHaveTextContent("FROM scratch");
    });
  });
});
