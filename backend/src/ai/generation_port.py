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
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> None:
        self._router = router
        self._tier = tier
        self._model = model
        # ZERO, not 0.1, and the previous comment is why: it already said "the same request twice
        # should produce the same answer". At 0.1 that is false. Sampling with a non-zero temperature
        # is still sampling — a user who submitted an identical prompt twice got a different NUMBER OF
        # FILES and different contents, which is exactly the promise the comment was making and the
        # value was breaking.
        #
        # Nothing is lost by removing it. Generated artifacts are judged by a DETERMINISTIC gate, so
        # sampling variety cannot produce a better outcome — only a different one, and a different one
        # costs a repair attempt when it fails. Greedy decoding also makes the L1 exact-match cache
        # actually useful: an identical prompt becomes a cache hit rather than a fresh run that happens
        # to disagree with the last.
        #
        # THIS DOES NOT MAKE GENERATION BIT-EXACT, and claiming so would be overselling it. Two things
        # still vary: the retrieval context is part of the prompt, so re-scanning a changed tree changes
        # the request and correctly misses the cache; and a provider may batch requests differently
        # between calls, which perturbs floating-point reduction order. What zero removes is the
        # variation this code chooses.
        self._temperature = temperature
        self._max_tokens = max_tokens

    @property
    def tier_name(self) -> str:
        return self._tier.value

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def primary_endpoint_descriptor(self) -> Any:
        chain = self._router._tier_config.tiers.get(self._tier)
        if chain is None:
            return None
        return self._router._tier_config.endpoints.get(chain.primary)

    async def ensure_ready(
        self,
        *,
        warmup_timeout_seconds: float = 360.0,
        heartbeat_callback: Any = None,
    ) -> None:
        """Warms up self-hosted / Ollama endpoints before streaming generation."""
        descriptor = self.primary_endpoint_descriptor
        if descriptor is None:
            return
        base_url = str(descriptor.base_url or "").lower()
        if "11434" in base_url or "ollama" in base_url or getattr(descriptor, "provider", "") == "ollama":
            from ..core.ollama_readiness import ensure_ollama_model_ready

            await ensure_ollama_model_ready(
                descriptor.base_url,
                self._model,
                warmup_timeout_seconds=warmup_timeout_seconds,
                heartbeat_callback=heartbeat_callback,
            )

    async def complete(
        self,
        *,
        prompt: RedactedPrompt,
        on_token: TokenSink | None = None,
        may_serve_from_cache: bool = True,
        store_in_cache: bool = True,
    ) -> ModelCompletion:
        """Route one completion, streaming to `on_token` when the endpoint supports it."""
        request = CompletionRequest(
            model=self._model,
            messages=[{"role": "user", "content": str(prompt)}],
            temperature=self._temperature,
            max_tokens=self._max_tokens,
        )
        try:
            result = await self._router.complete(
                tier=self._tier,
                request=request,
                prompt=prompt,
                on_token=on_token,
                may_serve_from_cache=may_serve_from_cache,
                store_in_cache=store_in_cache,
            )
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

    async def remember(self, *, prompt: RedactedPrompt, content: str) -> None:
        """Cache an ACCEPTED answer.

        The params must match the ones `complete` used or the key differs and the entry is never found --
        which would fail silently as a permanent cache miss, so they are built by the same expression
        rather than restated.
        """
        await self._router.remember(
            model=self._model,
            prompt=prompt,
            params=self._cache_params(),
            content=content,
        )

    def _cache_params(self) -> dict[str, object]:
        """The cache-key params. ONE definition, used by both the lookup and the store."""
        return {"temperature": self._temperature, "max_tokens": self._max_tokens}


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


def create_artifact_model_from_settings(
    settings: Any | None = None,
    redis_client: Any | None = None,
    http_client: Any | None = None,
) -> RoutedArtifactModel | None:
    """Construct a RoutedArtifactModel directly from application Settings.

    Enables worker processes (like ARQ tasks) to run model routing without needing FastAPI app state.
    """
    import logging
    import os
    from pathlib import Path
    import httpx
    from ..core.config import get_settings
    from .routing.credentials import EnvKeyResolver, LayeredKeyResolver
    from .routing.endpoints import EndpointRegistry
    from .routing.circuit_breaker import CircuitBreaker
    from .routing.cache import TieredSemanticCache
    from .routing.tiers import load_tier_config

    logger = logging.getLogger(__name__)
    settings = settings or get_settings()
    configured_path = Path(settings.model_tier_config_path or "")
    if not configured_path.is_absolute():
        configured_path = Path(__file__).resolve().parent.parent.parent / configured_path

    try:
        tier_config = load_tier_config(configured_path, env=os.environ)
    except Exception as exc:
        logger.warning("Could not load tier_config from %s: %s", configured_path, exc)
        return None

    model_http = http_client or httpx.AsyncClient(timeout=settings.model_http_timeout_seconds)
    key_resolver = LayeredKeyResolver(fallback=EnvKeyResolver())
    endpoint_registry = EndpointRegistry.from_config(tier_config, http=model_http, key_resolver=key_resolver)
    breakers = {
        endpoint_id: CircuitBreaker(
            failure_threshold=settings.cb_failure_threshold,
            failure_window_seconds=float(settings.cb_window_seconds),
            cooldown_seconds=float(settings.cb_open_seconds),
        )
        for endpoint_id in tier_config.endpoints
    }
    semantic_cache = TieredSemanticCache(redis=redis_client)
    router = ModelRouter(
        tier_config=tier_config,
        registry=endpoint_registry,
        cache=semantic_cache,
        breakers=breakers,
        key_resolver=key_resolver,
    )
    return build_artifact_model(
        router=router,
        tier_config=tier_config,
        tier_name=settings.generation_tier,
    )

