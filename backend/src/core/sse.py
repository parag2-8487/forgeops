# SPDX-License-Identifier: FSL-1.1-ALv2
"""SSE event vocabulary using FastAPI native support (design.md §7.4).

Exactly six event types: status, token, progress, validation, complete, error.
Uses FastAPI native EventSourceResponse — never sse-starlette.
"""

from __future__ import annotations

from enum import StrEnum


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
