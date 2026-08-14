// SPDX-License-Identifier: Apache-2.0
import { describe, it, expect } from "vitest";
import { readSSEResponse } from "../lib/sse-reader";

describe("Typed SSE Reader", () => {
  it("parses valid SSE event stream losslessly", async () => {
    const rawStream =
      'event: progress\ndata: {"percent": 50}\n\nevent: complete\ndata: {"percent": 100}\n\n';
    const encoder = new TextEncoder();
    const stream = new ReadableStream({
      start(controller) {
        controller.enqueue(encoder.encode(rawStream));
        controller.close();
      },
    });

    const mockResponse = new Response(stream);
    const messages = [];

    for await (const msg of readSSEResponse(mockResponse)) {
      messages.push(msg);
    }

    expect(messages).toHaveLength(2);
    expect(messages[0]).toEqual({ event: "progress", data: { percent: 50 }, id: undefined });
    expect(messages[1]).toEqual({ event: "complete", data: { percent: 100 }, id: undefined });
  });
});
