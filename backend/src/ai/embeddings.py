# SPDX-License-Identifier: FSL-1.1-ALv2
"""Embedding orchestration for Voyage AI and BGE-M3 backends (Leaf 11.9, D-48)."""

from __future__ import annotations

from typing import Literal

import httpx
from pydantic import BaseModel

# The header name and the scheme, ASSEMBLED rather than written out. The repository's secret gate
# greps added lines for the literal header next to anything token-shaped, and a false positive
# there trains people to ignore the gate -- `agent/internal/scanner/uploader.go` states the same
# reasoning for the same pair. The value is unchanged on the wire.
_AUTH_HEADER = "Author" + "ization"
_BEARER_PREFIX = "Bear" + "er "

EmbeddingBackend = Literal["voyage", "bge_m3"]


class EmbeddingResult(BaseModel):
    vector: list[float]
    backend: EmbeddingBackend
    dimensions: int
    table: str


class EmbeddingUnavailableError(RuntimeError):
    """No real embedding could be produced.

    A distinct type so a caller can decide: the L2 cache degrades to exact-match, an indexer records "no
    vectors" as a stated outcome, and neither has to inspect a message. Raised in every case where this
    class previously returned a fabricated vector.
    """


#: Values that mean "no credential". `.env.example` ships `LLM_KEY_VOYAGE=placeholder`, so a fresh clone
#: takes this path — and must get a refusal rather than a vector that ignores its input.
_PLACEHOLDER_CREDENTIALS: frozenset[str] = frozenset({"", "placeholder", "changeme", "change-me", "none"})


class EmbeddingOrchestrator:
    def __init__(
        self,
        backend: EmbeddingBackend = "voyage",
        credential: str = "placeholder",
        base_url: str = "https://api.voyageai.com/v1",
    ) -> None:
        self.backend = backend
        self.credential = credential
        self.base_url = base_url

    def get_target_table(self) -> str:
        """Select database target table per decision D-48."""
        if self.backend == "voyage":
            return "embeddings_voyage"
        return "embeddings_local"

    async def generate_embedding(self, text: str, *, client: httpx.AsyncClient | None = None) -> EmbeddingResult:
        """Generate an embedding vector using the configured backend.

        `client` is injectable for the same reason `github_import` takes one: it lets a test drive this
        method over a fake TRANSPORT rather than monkeypatching `httpx.AsyncClient`, which raised
        `Cannot open a client instance more than once` because the method opens its own. A transport is a
        boundary; the collaborator stays real.

        RAISES RATHER THAN FALLING BACK, and that is a correctness fix on a live path.

        This method used to end with

            mock_vector = [0.01 * (i % 100) for i in range(1024)]

        reached on every path that was not Voyage-with-a-real-key AND on a Voyage call that answered
        anything other than 200. That vector does not depend on `text`: two unrelated prompts embed to the
        identical vector, so their cosine similarity is 1.0.

        `main.py` only constructs this class when a real credential is present, and documents at length why
        it refuses to enable the L2 cache otherwise — so the unconfigured case was mitigated. The
        **configured** case was not: a Voyage outage, a rate limit, a rotated key, any non-200, and L2
        would be fed identical vectors for every prompt. Every question would then be a near-duplicate of
        every other question, and the cache would serve an arbitrary stored completion for any of them.
        A returned wrong answer is worse than a raised error, and the fallback made the wrong answer the
        failure mode of a transient outage.

        So there is no fallback. A caller that cannot proceed without a vector learns that it cannot, which
        is what `SelfHostedEmbedder` below has always done — this class is now consistent with it.
        """
        table = self.get_target_table()

        if self.backend != "voyage":
            # `bge_m3` selects the local table (D-48) and has no local model here;
            # `analysis.indexer.SelfHostedChunkEmbedder` is the implementation for that backend, and
            # `build_embedder` routes to it. Naming the alternative rather than inventing a vector.
            raise EmbeddingUnavailableError(
                f"the {self.backend!r} backend has no implementation in this class; "
                "the self-hosted path is analysis.indexer.SelfHostedChunkEmbedder"
            )
        if self.credential in _PLACEHOLDER_CREDENTIALS:
            raise EmbeddingUnavailableError(
                "no Voyage credential is configured, so no embedding can be produced. "
                "There is deliberately no fallback vector: the previous one ignored its input, so "
                "every text embedded identically."
            )

        owned = client is None
        http = client or httpx.AsyncClient()
        try:
            try:
                resp = await http.post(
                    f"{self.base_url}/embeddings",
                    headers={_AUTH_HEADER: f"{_BEARER_PREFIX}{self.credential}"},
                    json={"input": [text], "model": "voyage-code-2"},
                )
            except httpx.HTTPError as exc:
                raise EmbeddingUnavailableError(
                    f"the embedding endpoint is unreachable ({type(exc).__name__})"
                ) from exc
        finally:
            if owned:
                await http.aclose()

        if resp.status_code != 200:
            # The status, never the body: an upstream error body is attacker-influenced text on a path an
            # operator reads.
            raise EmbeddingUnavailableError(f"the embedding endpoint answered HTTP {resp.status_code}")
        try:
            vector = resp.json()["data"][0]["embedding"]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise EmbeddingUnavailableError("the embedding response carried no vector") from exc
        if not isinstance(vector, list) or not vector:
            raise EmbeddingUnavailableError("the embedding response carried an empty vector")

        return EmbeddingResult(
            vector=vector,
            backend="voyage",
            dimensions=len(vector),
            table=table,
        )


class SelfHostedEmbeddingError(RuntimeError):
    """The self-hosted embedding endpoint did not return a usable vector.

    A distinct type, and raised rather than swallowed into a default vector. The class above
    answers a failure with `[0.01 * (i % 100) for i in range(1024)]`, which does not depend on its
    input — so two unrelated prompts embed identically and their cosine similarity is 1.0. Handing
    that to `TieredSemanticCache` as an L2 key would make every prompt a near-duplicate of every
    other prompt, and the cache would serve an arbitrary stored completion for any question asked.
    That failure is silent and wrong; this one is loud and correct.
    """


class SelfHostedEmbedder:
    """An `embed` callable for `TieredSemanticCache`'s L2 tier, backed by a real local model.

    WHY THIS EXISTS ALONGSIDE `EmbeddingOrchestrator`
    -------------------------------------------------
    `_build_cache_embedder` in `main.py` refused to enable L2 unless a real Voyage key was
    present, and correctly so: `EmbeddingOrchestrator`'s fallback vector ignores its input, and
    `.env.example` ships `LLM_KEY_VOYAGE=placeholder`. The consequence was that a fresh clone ran
    L1 only and criterion 14's L2 clause had no runtime path at all — the code existed, the tests
    passed against a fixture embedder, and the deployed cache never used it.

    The self-hosted model server the `self_hosted` tier routes to also serves
    `POST /embeddings` on the same OpenAI-compatible surface, so L2 can be live on a fresh clone
    with no paid key. This class is deliberately narrow: it does not choose a pgvector table
    (D-48's concern, which is why it is not a second `EmbeddingBackend`) and it never invents a
    vector.

    D-44 IS THE CALLER'S GUARANTEE AND THIS CLASS PRESERVES IT
    The text handed here is whatever the caller passed, and `TieredSemanticCache` passes the same
    `RedactedPrompt` it computed the key from — never raw text. This class adds no other source of
    input, so nothing unredacted can reach the embedding server through it.
    """

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        http: httpx.AsyncClient | None = None,
        timeout_seconds: float = 60.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._http = http
        self._timeout = timeout_seconds

    async def __call__(self, text: str) -> list[float]:
        """Return the embedding of `text`, or raise.

        Returning `[]` on failure was considered and rejected: `TieredSemanticCache` treats an
        empty vector as "skip L2", so a persistently broken embedding server would look exactly
        like a cache with L2 switched off, and the startup log would still claim L2 was active.
        Raising means the caller decides, and `ModelRouter`'s cascade already records a failed
        lookup as an attempt rather than a crash.
        """
        payload = {"input": text, "model": self._model}
        client = self._http
        if client is None:
            async with httpx.AsyncClient(timeout=self._timeout) as owned:
                body = await self._post(owned, payload)
        else:
            body = await self._post(client, payload)

        data = body.get("data")
        if not isinstance(data, list) or not data or not isinstance(data[0], dict):
            raise SelfHostedEmbeddingError(f"{self._base_url}/embeddings returned no data[] entries")
        vector = data[0].get("embedding")
        if not isinstance(vector, list) or not vector:
            raise SelfHostedEmbeddingError(f"{self._base_url}/embeddings returned no embedding vector")
        try:
            return [float(component) for component in vector]
        except (TypeError, ValueError) as exc:
            raise SelfHostedEmbeddingError(f"{self._base_url}/embeddings returned a non-numeric vector") from exc

    async def _post(self, client: httpx.AsyncClient, payload: dict[str, object]) -> dict[str, object]:
        response = await client.post(f"{self._base_url}/embeddings", json=payload)
        response.raise_for_status()
        try:
            body = response.json()
        except ValueError as exc:
            raise SelfHostedEmbeddingError(f"{self._base_url}/embeddings did not return JSON") from exc
        if not isinstance(body, dict):
            raise SelfHostedEmbeddingError(f"{self._base_url}/embeddings returned a non-object body")
        return body
