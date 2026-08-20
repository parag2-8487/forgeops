# SPDX-License-Identifier: FSL-1.1-ALv2
"""SSE event vocabulary and frame encoding (design.md §7.4).

Exactly six event types: status, token, progress, validation, complete, error.

The enum was the whole of this module, and that turned out to be the problem. §7.4 says "Phase 1
producers use only these values; no divergent names may be invented", and the only producer in the
tree — `generation/service.py` — emitted `run_start`, `token_chunk` and `run_complete`, none of
which are in the enum, by hand-formatting its own `f"event: ...\\ndata: ...\\n\\n"` strings. A
vocabulary with no encoder is a vocabulary nothing has to go through, so `format_event` exists to
make the enum load-bearing rather than documentary.
"""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Any


class SSEEventType(StrEnum):
    """The fixed SSE event-type vocabulary from Research §0.

    Phase 1 producers use only these values; no divergent names may be invented.
    """

    STATUS = "status"
    TOKEN = "token"
    PROGRESS = "progress"
    VALIDATION = "validation"
    COMPLETE = "complete"
    ERROR = "error"


#: The media type an SSE response must carry. Held here so a route cannot spell it slightly
#: differently and lose the browser's `EventSource` parsing.
SSE_MEDIA_TYPE = "text/event-stream"


def format_event(event: SSEEventType, data: Any) -> str:
    """Encode one SSE frame.

    Takes `SSEEventType` rather than `str`, which is the point: an invented event name is now a
    type error at the call site instead of a string that reaches a client and is silently ignored
    by a listener registered for a different name. That failure mode is unusually quiet — an
    `EventSource` consumer subscribed to `token` simply never fires for `token_chunk`, and the
    stream looks empty rather than wrong.

    `data` is JSON-encoded on one line. Newlines are the SSE frame separator, so a payload
    containing one would split into two frames and the second would be parsed as a field name;
    `json.dumps` escapes them, which is why the payload is never interpolated raw.
    """
    if not isinstance(event, SSEEventType):
        raise TypeError(
            f"event must be an SSEEventType, got {type(event).__name__}. §7.4 fixes the vocabulary "
            f"at {[e.value for e in SSEEventType]}; a new name needs a design change, not a string."
        )
    payload = json.dumps(data, separators=(",", ":"), default=str)
    return f"event: {event.value}\ndata: {payload}\n\n"
