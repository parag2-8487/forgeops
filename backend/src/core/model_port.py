# SPDX-License-Identifier: FSL-1.1-ALv2
"""The port a domain uses to reach a model, so no domain has to import `src.ai` (design §2.2.1).

WHY THIS MODULE EXISTS
----------------------
`src/generation/` has to reach `ModelRouter` — §1.5's whole pipeline is six-tier routing — and it
must not import `src.ai`. The cross-domain ban is not decorative: `scripts/chokepoint_graph.py`
re-asserts it by PARSING the tree, precisely because a per-file `["TID251"]` in `pyproject.toml`
suppresses the whole rule and would also unban §2.2.1's private surface (finding 55). So the
first version of the generation wiring imported `ai.routing.router` directly and the parse check
failed it, correctly.

`src/core` is what every domain may depend on, so the SEAM lives here: a Protocol describing what
generation needs from a model, and a result type carrying what it gets back. `src/ai` implements
it (`ai/generation_port.py`), `main.py` composes it, and `src/generation` names only this module.
That is the arrangement that keeps the monolith extractable (§5.2) — generation could be lifted
out with this file and no knowledge of how routing works.

WHY THE PORT SPEAKS `provider`/`l1`/`l2` AND NOT `endpoint`/`L1_exact`/`L2_semantic`
-----------------------------------------------------------------------------------
`TieredSemanticCache` reports `L1_exact` and `ModelRouter` reports `endpoint`; `generation_runs`
carries a CHECK constraint over `l1`, `l2` and `provider`. Two vocabularies, and the translation
has to happen exactly once or a run ends with the database refusing `L1_exact` after everything
else succeeded. It happens in the adapter, and `SERVED_FROM` below is the port's declared output
so the two ends have one written contract rather than a convention.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from ..secrets.redaction import RedactedPrompt

#: Called once per content delta as a completion arrives.
#:
#: A callback rather than an async iterator because a streaming call has two results — the deltas
#: and the assembled response with its usage counts. See `ai/routing/endpoints.py::TokenSink`, which
#: this mirrors; the duplication is deliberate, because the point of the port is that a domain need
#: not name anything in `src.ai`.
TokenSink = Callable[[str], Awaitable[None]]

#: Every value `ModelCompletion.served_from` may carry.
#:
#: A SUBSET of `generation.models.SERVED_FROM`, which also has `template` (no model involved) and
#: `pending` (no outcome yet) — neither of which a model port can produce. Asserted in
#: `tests/unit/test_generation_routing.py`, so a value added here without a migration fails a test
#: rather than a database write at the end of a run.
SERVED_FROM: tuple[str, ...] = ("provider", "l1", "l2")


@dataclass(frozen=True, slots=True)
class ModelCompletion:
    """What one trip through the port produced.

    `ok=False` carries `failure_reasons` rather than raising, because the caller is a bounded
    retry loop that has to put the reason into the next prompt (§11.5). An exception would make
    "the provider is down" and "the provider answered something unusable" the same event, and the
    second is repairable.
    """

    ok: bool
    #: One of `SERVED_FROM`. Meaningless when `ok` is False.
    served_from: str = ""
    content: str | None = None
    #: Which endpoint answered, for `generation_runs.endpoint_id`. `None` on a cache hit, because
    #: no endpoint was called.
    endpoint_id: str | None = None
    #: Provider-reported token counts, or `None` when nothing was called — a cache hit cost no
    #: tokens, and reporting the original call's usage against it would inflate NFR-04's evidence
    #: every time the cache did its job.
    usage: dict[str, int] | None = None
    #: True when the content reached the sink as it was produced rather than in one piece.
    streamed: bool = False
    #: Why it failed, in the words the caller can quote back to the model.
    failure_reasons: tuple[str, ...] = field(default_factory=tuple)


@runtime_checkable
class ArtifactModelPort(Protocol):
    """A configured model a domain can ask for one completion.

    Deliberately narrow. It does NOT expose a tier chain, a breaker, a cache or an endpoint
    registry: those are routing concerns, and a domain that could name them would be coupled to
    how routing works rather than to the fact that it happens. `tier_name` is present only because
    `generation_runs.tier` records it and an SSE `progress` frame reports it.
    """

    @property
    def tier_name(self) -> str: ...

    async def complete(
        self,
        *,
        prompt: RedactedPrompt,
        on_token: TokenSink | None = None,
    ) -> ModelCompletion: ...
