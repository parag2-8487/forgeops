// SPDX-License-Identifier: FSL-1.1-ALv2

/**
 * §7.4's SSE event vocabulary, client side.
 *
 * WHY THIS IS A MODULE RATHER THAN STRINGS AT THE LISTENER
 * The backend's only SSE producer emitted `run_start`, `token_chunk` and `run_complete` — none of
 * which are in §7.4's six names — and its property test passed anyway, because every clause it
 * carried was about frame FRAMING and none about the event NAME. A frame with an invented name is
 * still a well-framed frame.
 *
 * The failure mode is silence in both directions. A consumer registered for `token` simply never
 * fires for `token_chunk`; no error is raised, no frame is rejected, and the stream looks empty
 * rather than wrong. Nothing on either side could detect it, because nothing compared the two ends.
 *
 * So the names live here once, and `__tests__/sse-vocabulary.test.ts` asserts this list is exactly
 * the backend's `SSEEventType`, read out of the generated OpenAPI schema rather than retyped. A
 * client-side test that checked this file against itself would have been exactly as blind as the
 * property was.
 */

export const SSE_EVENTS = [
  "status",
  "token",
  "progress",
  "validation",
  "complete",
  "error",
] as const;

export type SseEvent = (typeof SSE_EVENTS)[number];

/** The two events that end a stream. Anything after one of these is unreachable. */
export const TERMINAL_SSE_EVENTS = ["complete", "error"] as const satisfies readonly SseEvent[];

export function isSseEvent(name: string): name is SseEvent {
  return (SSE_EVENTS as readonly string[]).includes(name);
}

export function isTerminalSseEvent(name: string): boolean {
  return (TERMINAL_SSE_EVENTS as readonly string[]).includes(name);
}
