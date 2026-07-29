# SPDX-License-Identifier: FSL-1.1-ALv2
"""One place that decides whether a missing capability is a skip or a failure.

The problem this solves
-----------------------
`tests/integration/conftest.py` skipped the seven `test_initial_schema.py` tests
whenever `FORGEOPS_TEST_DATABASE_URL` was unset. That variable was set nowhere —
not in `ci.yml`, not anywhere in the repository — so criterion 14's only
executable evidence (the `vector(1536)` column, the HNSW `vector_cosine_ops`
index, transaction-scoped `hnsw.ef_search`, a clean autogenerate and the
downgrade) never ran, in CI or locally, while the CI job paid to start a real
`pgvector/pgvector:pg17` service beside it. The skip reason was printed, so
nothing looked wrong.

A skip that is invisible in a green run is indistinguishable from coverage. So:

* locally, a missing database or Docker daemon still skips, with a clear reason;
* in CI, `FORGEOPS_REQUIRE_INTEGRATION=1` turns that same skip into a failure.

Use `require_capability` for every capability probe in the integration suite.
"""

from __future__ import annotations

import os

import pytest

REQUIRE_ENV = "FORGEOPS_REQUIRE_INTEGRATION"


def integration_is_mandatory() -> bool:
    """True when the environment declares that integration tests must run."""
    return os.environ.get(REQUIRE_ENV, "").strip().lower() in {"1", "true", "yes"}


def require_capability(reason: str) -> None:
    """Skip locally, fail in CI.

    Call this instead of `pytest.skip` so an environment that promised the
    capability cannot silently drop the test.
    """
    if integration_is_mandatory():
        pytest.fail(f"{REQUIRE_ENV} is set but this test cannot run: {reason}")
    pytest.skip(reason)
