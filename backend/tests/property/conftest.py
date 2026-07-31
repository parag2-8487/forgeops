# SPDX-License-Identifier: FSL-1.1-ALv2
"""Fixtures for the property suite (design.md §0.4.5, Appendix B).

Most `Q-` properties are pure and need nothing here. Two do not: Q-04 quantifies "exactly one
`audit_events` row per chokepoint transit, in the same transaction as the state change" and Q-05
quantifies tamper evidence, and both are properties of a **transaction** rather than of a
function. They therefore need the same real Postgres and Redis the integration suite uses.

The fixtures are re-exported rather than redefined, from
`tests/integration/chokepoint_support.py`. Two copies of "how a transit is set up" is how the
property comes to quantify over a shape the integration tests never exercise — and then a green
property says nothing about the system the integration tests describe.

Re-exported through a conftest rather than imported into each test module because pytest
discovers fixtures by module-level name: importing `sessions` into a module whose test methods
take a `sessions` parameter shadows all of them, which the first attempt at this refactor
demonstrated as 88 `F811` findings.
"""

from __future__ import annotations

from ..integration.chokepoint_support import (  # noqa: F401 - re-exported fixtures
    head_engine,
    redis_client,
    redis_url,
    schema_at_head,
    sessions,
    sink,
)
from ..integration.conftest import database_url, sync_database_url  # noqa: F401 - re-exported fixtures
