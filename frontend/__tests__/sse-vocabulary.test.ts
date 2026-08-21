// SPDX-License-Identifier: FSL-1.1-ALv2
/**
 * The client and the server must agree on §7.4's six event names.
 *
 * WHY THIS TEST EXISTS, AND WHY IT READS A GENERATED FILE
 * The backend's only SSE producer emitted `run_start`, `token_chunk` and `run_complete`. None of the
 * three is in `SSEEventType`. The backend property test named "SSE well-formedness" passed on every
 * frame, because all three of its clauses were about framing — prefix, separator, blank-line
 * terminator — and a frame with an invented name is still perfectly well framed. The only test that
 * named the vocabulary asserted the invented names, so it required the defect.
 *
 * The failure mode is silence at both ends. A consumer registered for `token` never fires for
 * `token_chunk`; nothing raises, nothing is rejected, and the stream looks empty rather than wrong.
 *
 * So a client-side test comparing `lib/api/sse-events.ts` against a list retyped in this file would
 * be exactly as blind as the property was: two copies of the same belief agreeing with each other.
 * This reads the BACKEND's enum out of `openapi.json`, which is generated from
 * `src/core/sse.py` by `scripts/dump-openapi.py`. If the two ends diverge, one of them fails here.
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import {
  SSE_EVENTS,
  TERMINAL_SSE_EVENTS,
  isSseEvent,
  isTerminalSseEvent,
} from "@/lib/api/sse-events";

/** Written by `scripts/dump-openapi.py`, which boots `create_app()` and dumps the real schema. */
const OPENAPI_PATH = join(process.cwd(), "..", "docs", "openapi.json");

interface OpenApiDocument {
  components?: { schemas?: Record<string, { enum?: string[] }> };
}

function backendSseEventNames(): string[] {
  const raw = readFileSync(OPENAPI_PATH, "utf8");
  const document = JSON.parse(raw) as OpenApiDocument;
  const schema = document.components?.schemas?.SSEEventType;
  if (!schema?.enum) {
    throw new Error(
      "SSEEventType is not published in docs/openapi.json. Regenerate it with " +
        "`python scripts/dump-openapi.py`; this test cannot verify agreement against a file that " +
        "does not describe the vocabulary.",
    );
  }
  return schema.enum;
}

describe("the SSE vocabulary agrees across the wire", () => {
  it("the client's list is exactly the backend's enum", () => {
    const backend = backendSseEventNames();
    // Sets, not arrays: the two ends need the same MEMBERS. Declaration order in a StrEnum is not
    // part of the contract and pinning it would fail for a reason that does not matter.
    expect(new Set(SSE_EVENTS)).toEqual(new Set(backend));
    expect(SSE_EVENTS).toHaveLength(backend.length);
  });

  it("is exactly six names, as §7.4 states", () => {
    expect(backendSseEventNames()).toHaveLength(6);
    expect(SSE_EVENTS).toHaveLength(6);
  });

  it("accepts every name the backend can emit", () => {
    for (const name of backendSseEventNames()) {
      expect(isSseEvent(name)).toBe(true);
    }
  });

  it("rejects the three names the producer used to emit", () => {
    // The historical defect, named explicitly so a regression is recognisable rather than merely
    // red. These passed the backend's own well-formedness property.
    for (const invented of ["run_start", "token_chunk", "run_complete"]) {
      expect(isSseEvent(invented)).toBe(false);
      expect(backendSseEventNames()).not.toContain(invented);
    }
  });

  it("names both terminal events, and they are real members of the vocabulary", () => {
    const backend = new Set(backendSseEventNames());
    for (const terminal of TERMINAL_SSE_EVENTS) {
      expect(backend.has(terminal)).toBe(true);
      expect(isTerminalSseEvent(terminal)).toBe(true);
    }
    expect(TERMINAL_SSE_EVENTS).toHaveLength(2);
    // A non-terminal name must not be treated as one, or the reader would stop mid-stream.
    expect(isTerminalSseEvent("token")).toBe(false);
    expect(isTerminalSseEvent("status")).toBe(false);
  });
});
