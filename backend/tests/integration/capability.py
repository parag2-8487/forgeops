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

Phase 1 additions (design.md §0.4.4)
------------------------------------
Every new capability is registered through **this function and no other**, with a
named key drawn from `CAPABILITIES` below. The key matters for two reasons: it is
what `scripts/check-no-skips.py` reports when a mandatory test skips, and a typo'd
capability name would otherwise create a silent second gate — the exact D-26
failure. `require_capability` therefore rejects an unregistered key outright,
rather than accepting free-form text.
"""

from __future__ import annotations

import os
from typing import Final

import pytest

REQUIRE_ENV = "FORGEOPS_REQUIRE_INTEGRATION"

#: Every capability an integration test may gate on, with where CI provides it.
#: Adding a row here is the only way to add a gate; see the module docstring.
CAPABILITIES: Final[dict[str, str]] = {
    # Phase 0
    "postgres": "backend job service (pgvector/pgvector:pg17)",
    "redis": "backend job service (Redis Stack)",
    "tofu": "agent job (opentofu/setup-opentofu)",
    "docker": "a reachable Docker daemon on the runner",
    # Phase 1 (design.md §0.4.4 table)
    "opa": "backend job service (OPA, rootless)",
    "cerbos": "backend job service (Cerbos sidecar)",
    "oidc": "fixture issuer in the backend job; real Authentik in the auth job",
    "kubernetes": "k8s job (kind cluster, D-28)",
    "trivy": "agent job (pinned trivy in the agent-dev devtools image)",
    "infisical": "secrets job (digest-pinned Infisical container)",
    "agent_binary": "e2e job (the real forgeops-agent binary)",
    # The local OpenAI-compatible model server that makes §11.7's `self_hosted` tier reachable.
    # Every hosted endpoint in `config/model-tiers.yaml` needs a key and `.env.example` ships
    # placeholders, so without this there is no endpoint any test can genuinely call — which is why
    # `served_from='provider'` had never been produced by anything.
    "self_hosted_model": "the `ollama` Compose service, with SELF_HOSTED_MODEL_ID pulled",
}


class UnknownCapabilityError(LookupError):
    """Raised when a test gates on a capability that is not registered.

    This is deliberately an error rather than a skip. A misspelled capability that
    silently skipped would be a second, invisible gate — D-26 all over again.
    """


def integration_is_mandatory() -> bool:
    """True when the environment declares that integration tests must run."""
    return os.environ.get(REQUIRE_ENV, "").strip().lower() in {"1", "true", "yes"}


def require_capability(capability: str, reason: str | None = None) -> None:
    """Skip locally, fail in CI.

    Call this instead of `pytest.skip` so an environment that promised the
    capability cannot silently drop the test.

    Args:
        capability: a key from `CAPABILITIES`.
        reason: what specifically is missing, appended to the message. Optional,
            because the capability key alone is often the whole story.
    """
    if capability not in CAPABILITIES:
        raise UnknownCapabilityError(
            f"unregistered capability {capability!r}; add it to "
            f"tests/integration/capability.py::CAPABILITIES with the CI job that "
            f"provides it. Known: {sorted(CAPABILITIES)}"
        )

    detail = f"capability {capability!r} unavailable"
    if reason:
        detail += f": {reason}"
    provided_by = CAPABILITIES[capability]

    if integration_is_mandatory():
        pytest.fail(f"{REQUIRE_ENV} is set but {detail}. CI provides it via: {provided_by}")
    pytest.skip(f"{detail} (CI provides it via: {provided_by})")
