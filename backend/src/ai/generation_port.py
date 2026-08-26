# SPDX-License-Identifier: FSL-1.1-ALv2
"""The `ArtifactModelPort` implementation: `ModelRouter` behind `src.core`'s seam (design §2.2.1).

`src/generation/` must not import `src.ai` — the ban is re-asserted by parsing in
`scripts/chokepoint_graph.py`, so it cannot be silenced with a lint ignore. This is the adapter on
the routing side of `core/model_port.py`: it holds the router, the tier and the model identifier,
and answers the one question a domain has.

It also owns the ONE translation between the two `served_from` vocabularies. `TieredSemanticCache`
reports `L1_exact`/`L2_semantic` and `ModelRouter` reports `endpoint`, while
`generation_runs.served_from` has a CHECK constraint over `l1`/`l2`/`provider`. Translating at the
persistence call site instead would put the mapping next to a string literal in an INSERT, which is
how a run ends with the database refusing `L1_exact` after everything else succeeded.
"""

from __future__ import annotations

from ..core.model_port import ModelCompletion, TokenSink
from ..secrets.redaction import RedactedPrompt
from .routing.endpoints import CompletionRequest
from .routing.router import ModelRouter, RoutingOutcome
from .routing.tiers import ModelTier, TierConfig

#: Routing's vocabulary on the left, the database's on the right.
_TO_SERVED_FROM: dict[str, str] = {
    "L1_exact": "l1",
    "L2_semantic": "l2",
    "endpoint": "provider",
}


class RoutedArtifactModel:
    """One tier of the six-tier cascade, presented as a single model a domain can call."""

    def __init__(
        self,
        *,
        router: ModelRouter,
        tier: ModelTier,
        model: str,
        temperature: float = 0.1,
        max_tokens: int = 4096,
    ) -> None:
        self._router = router
        self._tier = tier
        self._model = model
        # Low rather than the 0.7 default. Generated artifacts are judged by a DETERMINISTIC gate,
        # so sampling variety buys nothing and costs retries; it also makes the L1 cache useful,
        # because the same request twice should produce the same answer.
        self._temperature = temperature
        self._max_tokens = max_tokens

    @property
    def tier_name(self) -> str:
        return self._tier.value

    async def complete(
        self,
        *,
        prompt: RedactedPrompt,
        on_token: TokenSink | None = None,
    ) -> ModelCompletion:
        """Route one completion, streaming to `on_token` when the endpoint supports it."""
        request = CompletionRequest(
            model=self._model,
            messages=[{"role": "user", "content": str(prompt)}],
            temperature=self._temperature,
            max_tokens=self._max_tokens,
        )
        try:
            result = await self._router.complete(tier=self._tier, request=request, prompt=prompt, on_token=on_token)
        except Exception as exc:  # noqa: BLE001 - reported as a failed completion, not a crash
            # The router classifies per-endpoint failures into attempt records; an exception
            # escaping it is a fault in the call itself. Reported as `ok=False` because the caller
            # is a bounded retry loop with a template fallback behind it — raising here would turn
            # a degraded provider into a failed request.
            return ModelCompletion(
                ok=False,
                failure_reasons=(f"the model call raised {type(exc).__name__}: {str(exc)[:200]}",),
            )

        if result.outcome is not RoutingOutcome.OK or not result.content:
            reasons = tuple(
                f"endpoint {attempt.endpoint_id} -> {attempt.result}"
                + (f" ({attempt.reason})" if attempt.reason else "")
                for attempt in result.attempts
            )
            return ModelCompletion(
                ok=False,
                failure_reasons=reasons or ("every endpoint in the tier chain was skipped or unavailable",),
            )

        return ModelCompletion(
            ok=True,
            # `provider` is the default rather than an error for an unrecognised routing value: a
            # completion that came back with content came from somewhere, and the safe reading is
            # the one that does NOT claim a cache served it.
            served_from=_TO_SERVED_FROM.get(result.served_from or "", "provider"),
            content=result.content,
            endpoint_id=result.endpoint_id,
            usage=result.usage,
            streamed=result.streamed,
        )


def build_artifact_model(
    *,
    router: ModelRouter | None,
    tier_config: TierConfig | None,
    tier_name: str,
    max_tokens: int = 4096,
) -> RoutedArtifactModel | None:
    """Compose the port from what the lifespan already built, or `None` if it cannot.

    `None` rather than an exception, and that is a configuration statement: a deployment with no
    router runs generation's template path and records `served_from='template'`, which is a true
    row. Refusing to compose would turn a degraded configuration into an outage on a surface that
    still works.

    THE MODEL IDENTIFIER COMES FROM THE TIER'S PRIMARY ENDPOINT
    `CompletionRequest.model` is what goes in the provider's request body, so naming a model the
    endpoint does not serve produces 404 `model "..." not found` — a failure that reads like a
    routing fault. `/api/v1/ai/complete` passes the TIER NAME as the model, which works only for a
    provider that ignores the field.

    The known limit, stated: a cascade that falls through to a secondary serving something else
    would send that secondary the primary's model id. That is inherent in routing one identifier
    through a chain of heterogeneous endpoints, and it is why the self-hosted chain — where every
    entry is the same local server — is the configured default for generation.
    """
    if router is None or tier_config is None:
        return None
    try:
        tier = ModelTier(tier_name)
    except ValueError:
        # `Settings.generation_tier` is a `Literal` over the six tier names, so reaching here means
        # the configuration and the enum disagree — a code defect rather than an operator's.
        return None
    chain = tier_config.tiers.get(tier)
    if chain is None:
        return None
    descriptor = tier_config.endpoints.get(chain.primary)
    if descriptor is None or not descriptor.model.strip():
        return None
    return RoutedArtifactModel(router=router, tier=tier, model=descriptor.model, max_tokens=max_tokens)
