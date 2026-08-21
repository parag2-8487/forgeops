# SPDX-License-Identifier: FSL-1.1-ALv2
"""Property test Q-26: SSE stream well-formedness (Leaf 13.12, design.md §7.4).

WHY THIS FILE GREW
Its first version asserted three things about every frame: it starts `event: `, contains
`\\ndata: `, and ends with a blank line. All three are about FRAMING, and none is about the
event NAME. So the only producer in the tree emitted `run_start`, `token_chunk` and
`run_complete` — not one of which is in `SSEEventType` — and this property passed on every
frame, because each malformed-vocabulary frame was perfectly well-framed.

That failure mode is unusually quiet. An `EventSource` consumer registered for `token` never
fires for `token_chunk`; the stream looks empty rather than wrong, and no error surfaces at
either end. §7.4 says "no divergent names may be invented", and nothing enforced it.

`mutations.toml` also recorded that Appendix B's stated control for Q-26 — "emit a second
`COMPLETE` after an `ERROR`" — could not fail this test, because terminal-event uniqueness was
not asserted either. The manifest substituted a different mutation and recorded the gap as a
known weakness. Both clauses are added here, so the row can state Appendix B's control as
written.
"""

import json
import uuid

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from src.core.sse import SSEEventType
from src.generation.service import GenerationService

pytestmark = [pytest.mark.asyncio, pytest.mark.mandatory]

#: The two events that end a stream. §7.4 gives six names; a well-formed run ends with exactly
#: one of these, and anything after it is unreachable by a client that has closed the connection.
TERMINAL_EVENTS = frozenset({SSEEventType.COMPLETE.value, SSEEventType.ERROR.value})


def parse_frames(raw: list[str]) -> list[tuple[str, str]]:
    """Split each frame into its event name and data payload.

    Deliberately a hand-rolled parse of the wire bytes rather than a look at what the service
    intended. The clause under test is what a browser would receive.
    """
    parsed: list[tuple[str, str]] = []
    for frame in raw:
        head, _, tail = frame.partition("\n")
        parsed.append((head.removeprefix("event: "), tail.removeprefix("data: ").rstrip("\n")))
    return parsed


@given(prompt=st.text(min_size=1, max_size=100))
@settings(deadline=None, max_examples=25)
async def test_property_q26_sse_stream_well_formedness(prompt: str):
    """Clause A: every frame is well-framed for `EventSource`."""
    service = GenerationService()
    pid = uuid.uuid4()

    async for event_str in service.stream_generation(pid, prompt):
        assert event_str.startswith("event: ")
        assert "\ndata: " in event_str
        # The blank line is the frame terminator. Without it `EventSource` never dispatches.
        assert event_str.endswith("\n\n")


@given(prompt=st.text(min_size=1, max_size=100))
@settings(deadline=None, max_examples=25)
async def test_property_q26_every_event_name_is_in_the_closed_vocabulary(prompt: str):
    """Clause B: §7.4's vocabulary is closed, and this is what enforces it.

    The clause whose absence let `run_start`/`token_chunk`/`run_complete` ship. Asserted against
    `SSEEventType` rather than a list restated here, so a name added to the enum cannot make this
    test stale and a name invented in a producer cannot pass it.
    """
    service = GenerationService()
    allowed = {member.value for member in SSEEventType}

    frames = [frame async for frame in service.stream_generation(uuid.uuid4(), prompt)]
    assert frames, "a run must emit at least one frame"

    for name, _ in parse_frames(frames):
        assert name in allowed, (
            f"event name {name!r} is not in §7.4's vocabulary {sorted(allowed)}. A consumer "
            f"registered for a documented name never fires for this frame."
        )


@given(prompt=st.text(min_size=1, max_size=100))
@settings(deadline=None, max_examples=25)
async def test_property_q26_a_stream_carries_exactly_one_terminal_event(prompt: str):
    """Clause C: Appendix B's control targets this, and it did not exist.

    A second terminal event after the first is not a cosmetic fault: a client that closes on
    `complete` never sees it, so the two ends disagree about whether the run finished, and a
    `complete` following an `error` would report success for a failed run to any consumer that
    read to the end.
    """
    service = GenerationService()

    frames = [frame async for frame in service.stream_generation(uuid.uuid4(), prompt)]
    names = [name for name, _ in parse_frames(frames)]
    terminals = [name for name in names if name in TERMINAL_EVENTS]

    assert len(terminals) == 1, (
        f"expected exactly one terminal event, got {terminals}. §7.4's terminal events are "
        f"{sorted(TERMINAL_EVENTS)} and a stream ends at the first one."
    )
    # And it is last, so nothing is stranded behind it.
    assert names[-1] in TERMINAL_EVENTS, f"stream ended on {names[-1]!r}, not a terminal event"


@given(prompt=st.text(min_size=1, max_size=100))
@settings(deadline=None, max_examples=25)
async def test_property_q26_every_payload_is_json_on_a_single_line(prompt: str):
    """Clause D: the payload survives the wire.

    A raw newline inside a payload would split one frame into two and the remainder would be
    parsed as a field name, so this is the encoding clause that makes clause A hold for arbitrary
    content rather than only for the content a template happens to produce.
    """
    service = GenerationService()

    frames = [frame async for frame in service.stream_generation(uuid.uuid4(), prompt)]
    for name, payload in parse_frames(frames):
        assert "\n" not in payload, f"{name} payload contains a raw newline and would split"
        json.loads(payload)


class TestTheEncoderIsTheOnlyWayIn:
    """`format_event` refuses a bare string, which is what makes the vocabulary load-bearing.

    Without this the enum is documentation: a producer can hand-format a frame with any name and
    every clause above still passes, because the frame is well-formed and the name is whatever the
    producer chose. That is precisely the state the tree was in.
    """

    def test_it_refuses_a_name_outside_the_enum(self) -> None:
        from src.core.sse import format_event

        for invented in ("run_start", "token_chunk", "run_complete", "status"):
            with pytest.raises(TypeError, match="must be an SSEEventType"):
                format_event(invented, {"a": 1})  # type: ignore[arg-type]

    def test_it_escapes_a_newline_rather_than_emitting_it(self) -> None:
        from src.core.sse import format_event

        frame = format_event(SSEEventType.TOKEN, {"text": "line one\nline two"})
        assert frame.endswith("\n\n")
        # Exactly two newlines: the field separator and the terminator. The payload's newline is
        # escaped inside the JSON string rather than reaching the wire.
        assert frame.count("\n") == 3
        assert "\\n" in frame
