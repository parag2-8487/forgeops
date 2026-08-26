# SPDX-License-Identifier: FSL-1.1-ALv2
"""`build_embedder`'s self-hosted branch, and the dimension guard that makes it safe (D-48).

WHY THIS MATTERS MORE THAN IT LOOKS

Before this branch existed, `build_embedder` knew only Voyage. `.env.example` ships
`LLM_KEY_VOYAGE=placeholder`, `build_embedder` honours that sentinel, and so EVERY scan on a fresh
clone persisted the file tree, the contents and the dependency graph with `vectors_written = 0`.
The HNSW index on `embeddings_local` had never held a row, and retrieval was permanently
sparse-only on any deployment without a paid key — while `analysis/models.py` documented the 1024-d
table as the self-hosted path.

The dimension guard is the part that must not be omitted. `embeddings_local.embedding` is
`vector(1024)` because D-48 sizes it for BGE-M3. The previous default embedding model,
`nomic-embed-text`, is 768-d — so without the check the failure arrived from inside the INSERT,
naming neither the model nor the setting that chose it, after every vector had already been paid for.
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from src.analysis.indexer import (
    EMBEDDING_DIMS_LOCAL,
    EmbeddingProviderError,
    SelfHostedChunkEmbedder,
    build_embedder,
)

pytestmark = [pytest.mark.mandatory]


def _settings(**overrides: object) -> SimpleNamespace:
    base = {
        "embedding_backend": "bge_m3",
        "self_hosted_base_url": "http://model-server.invalid/v1",
        "self_hosted_embedding_model_id": "bge-m3",
        "outbound_http_timeout_seconds": 5.0,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class TestTheBackendSettingChoosesTheTable:
    def test_the_self_hosted_backend_writes_the_1024_dimension_table(self) -> None:
        """The whole point of D-48: two columns of different widths, one chosen per project."""
        embedder, reason = build_embedder(_settings())
        assert reason == ""
        assert isinstance(embedder, SelfHostedChunkEmbedder)
        assert embedder.table == "embeddings_local"
        assert embedder.dimensions == EMBEDDING_DIMS_LOCAL == 1024
        assert embedder.model_id == "bge-m3"

    def test_voyage_still_refuses_a_placeholder_credential(self) -> None:
        """Unchanged behaviour, asserted so the new branch cannot have moved it.

        A doomed HTTPS request per scan reporting "provider unavailable" would describe the wrong
        problem: nothing was ever configured.
        """
        embedder, reason = build_embedder(_settings(embedding_backend="voyage", llm_key_voyage="placeholder"))
        assert embedder is None
        assert "LLM_KEY_VOYAGE" in reason

    @pytest.mark.parametrize(
        ("missing", "expected_key"),
        [
            ("self_hosted_base_url", "SELF_HOSTED_BASE_URL"),
            ("self_hosted_embedding_model_id", "SELF_HOSTED_EMBEDDING_MODEL_ID"),
        ],
    )
    def test_an_unconfigured_endpoint_names_the_setting_rather_than_inventing_one(
        self, missing: str, expected_key: str
    ) -> None:
        """A default URL would turn a configuration gap into a connection error on every scan."""
        embedder, reason = build_embedder(_settings(**{missing: ""}))
        assert embedder is None
        assert expected_key in reason
        # The reason travels into `IndexResult.vectors_absent_reason`, so it has to say which
        # column is empty, not merely that something failed.
        assert str(EMBEDDING_DIMS_LOCAL) in reason


class TestTheEmbedderRefusesAWrongWidthRatherThanLettingTheInsertFail:
    def _embedder(self, handler: object) -> SelfHostedChunkEmbedder:
        embedder = SelfHostedChunkEmbedder(base_url="http://model-server.invalid/v1", model="bge-m3")
        transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
        # The class owns its client per call, so the transport is injected by patching the factory.
        original = httpx.AsyncClient

        def factory(*_args: object, **kwargs: object) -> httpx.AsyncClient:
            kwargs.pop("timeout", None)
            return original(transport=transport, **kwargs)  # type: ignore[arg-type]

        self._factory = factory
        return embedder

    @pytest.mark.asyncio
    async def test_a_768_dimension_model_is_named_in_the_refusal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`nomic-embed-text` was the previous default and is 768-d."""

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"data": [{"embedding": [0.1] * 768}]})

        embedder = self._embedder(handler)
        monkeypatch.setattr(httpx, "AsyncClient", self._factory)
        with pytest.raises(EmbeddingProviderError) as caught:
            await embedder.embed(["def f(): pass"])
        message = str(caught.value)
        assert "768" in message
        assert "1024" in message
        # The setting to change has to be in the message, or an operator has to go read the code.
        assert "SELF_HOSTED_EMBEDDING_MODEL_ID" in message

    @pytest.mark.asyncio
    async def test_a_correct_width_round_trips(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"data": [{"embedding": [0.25] * EMBEDDING_DIMS_LOCAL}]})

        embedder = self._embedder(handler)
        monkeypatch.setattr(httpx, "AsyncClient", self._factory)
        vectors = await embedder.embed(["one", "two"])
        # One vector per input, in order. A batched request that returned a single vector for an
        # array input would pair the second chunk's text with the first chunk's vector — a wrong
        # answer no dimension check can catch, which is why the request is serial.
        assert len(vectors) == 2
        assert all(len(v) == EMBEDDING_DIMS_LOCAL for v in vectors)

    @pytest.mark.asyncio
    async def test_an_error_status_is_raised_rather_than_returning_no_vector(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A silent empty result would persist chunks with no vectors and report success."""

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, json={"error": {"message": "model not loaded"}})

        embedder = self._embedder(handler)
        monkeypatch.setattr(httpx, "AsyncClient", self._factory)
        with pytest.raises(EmbeddingProviderError) as caught:
            await embedder.embed(["x"])
        assert "503" in str(caught.value)

    @pytest.mark.asyncio
    async def test_no_texts_makes_no_request(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            return httpx.Response(200, json={"data": [{"embedding": [0.0] * EMBEDDING_DIMS_LOCAL}]})

        embedder = self._embedder(handler)
        monkeypatch.setattr(httpx, "AsyncClient", self._factory)
        assert await embedder.embed([]) == []
        assert calls == []
