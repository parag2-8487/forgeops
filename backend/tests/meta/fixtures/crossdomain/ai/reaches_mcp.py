# SPDX-License-Identifier: FSL-1.1-ALv2
"""NEGATIVE CONTROL for the cross-domain module bans (design.md §2.2.1, finding 55).

This file must be REPORTED. It is `ai/` reaching into `mcp/`, which is the exact case that Ruff
misses for the four domains carrying a `["TID251"]` glob, and therefore the exact case the parse
exists to catch. If the check ever stops flagging this file, the check has stopped working.

Never imported, only parsed. It lives under `backend/tests/` so it is outside the walked source
tree — a permanent offender under `backend/src/` would red the build forever, which is the trap
`test_neither_fixture_lives_under_the_walked_source_tree` already guards for the other fixtures.
"""

from __future__ import annotations

from ..core.errors import ProblemException  # noqa: F401  permitted: core is cross-cutting
from ..mcp.gateway import Gateway  # noqa: F401  BANNED: ai must not reach mcp


def reaches_across() -> None:
    """Body irrelevant; the import above is the whole point of the fixture."""
