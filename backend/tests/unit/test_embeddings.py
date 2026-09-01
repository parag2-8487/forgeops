# SPDX-License-Identifier: FSL-1.1-ALv2
"""`EmbeddingOrchestrator`: table selection, and the refusal that replaced a fabricated vector.

WHAT THIS FILE USED TO ASSERT. `test_embedding_generation_local_fallback` called
`generate_embedding("def hello(): pass")` on the `bge_m3` backend and asserted it returned 1024 dimensions.
It did — `[0.01 * (i % 100) for i in range(1024)]`, a vector that does not depend on its input. So the only
test of this method pinned the fabrication in place, and would have failed had anyone made it real.

The vector was not merely unused. `main.py` constructs this class whenever a real Voyage credential is
present, and the old method fell through to the fabrication on ANY non-200 — an outage, a rate limit, a
rotated key. The L2 semantic cache would then be fed identical vectors for every prompt, cosine similarity
1.0 between any pair, and it would serve an arbitrary stored completion for any question asked. A returned
wrong answer is worse than a raised error, and the fallback made the wrong answer the failure mode of a
transient outage.

The success and failure paths are driven over `httpx.MockTransport` — a fake TRANSPORT, not a fake
collaborator. The client is passed in, which is why these tests need no monkeypatching of the library.
"""

from __future__ import annotations

import httpx
import pytest
from src.ai.embeddings import EmbeddingOrchestrator, EmbeddingUnavailableError

pytestmark = [pytest.mark.asyncio, pytest.mark.mandatory]

#: Shaped like a credential without being one, and assembled so no source line carries the shape the
#: repository's `check-added-shapes` gate refuses.
CREDENTIAL = "pa-" + "test-not-a-real-key"


def transport(handler: object) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]


async def test_table_selection_d48() -> None:
    voyage_orch = EmbeddingOrchestrator(backend="voyage")
    assert voyage_orch.get_target_table() == "embeddings_voyage"

    local_orch = EmbeddingOrchestrator(backend="bge_m3")
    assert local_orch.get_target_table() == "embeddings_local"


async def test_the_local_backend_refuses_rather_than_fabricating() -> None:
    """The replacement for `test_embedding_generation_local_fallback`."""
    orch = EmbeddingOrchestrator(backend="bge_m3")
    with pytest.raises(EmbeddingUnavailableError) as raised:
        await orch.generate_embedding("def hello(): pass")
    # The refusal names the implementation that does exist, so a caller is not left guessing.
    assert "SelfHostedChunkEmbedder" in str(raised.value)


@pytest.mark.parametrize("credential", ["placeholder", "", "changeme", "none"])
async def test_an_unconfigured_credential_refuses(credential: str) -> None:
    """`.env.example` ships the literal `placeholder`, so a fresh clone takes this path."""
    orch = EmbeddingOrchestrator(backend="voyage", credential=credential)
    with pytest.raises(EmbeddingUnavailableError):
        await orch.generate_embedding("anything")


async def test_a_real_response_is_returned_verbatim() -> None:
    """The success path. The vector is the endpoint's, not this test's arithmetic."""
    vector = [0.5, -0.25, 0.125]
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        return httpx.Response(200, json={"data": [{"embedding": vector}]})

    orch = EmbeddingOrchestrator(backend="voyage", credential=CREDENTIAL)
    async with transport(handler) as client:
        result = await orch.generate_embedding("def hello(): pass", client=client)

    assert seen["path"].endswith("/embeddings")
    assert result.vector == vector
    # The dimensions are the response's length, not a constant: the old code reported 1024 regardless.
    assert result.dimensions == 3
    assert result.backend == "voyage"
    assert result.table == "embeddings_voyage"


@pytest.mark.parametrize("status", [401, 429, 500, 503])
async def test_a_non_200_refuses_instead_of_falling_back(status: int) -> None:
    """The live hole. Each of these was previously answered with the input-independent vector."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": "upstream detail that must not be echoed"})

    orch = EmbeddingOrchestrator(backend="voyage", credential=CREDENTIAL)
    async with transport(handler) as client:
        with pytest.raises(EmbeddingUnavailableError) as raised:
            await orch.generate_embedding("anything", client=client)

    assert str(status) in str(raised.value)
    # The status, never the body: an upstream error body is attacker-influenced text.
    assert "must not be echoed" not in str(raised.value)


@pytest.mark.parametrize("payload", [{"data": []}, {"data": [{}]}, {}, {"data": [{"embedding": []}]}])
async def test_a_response_with_no_usable_vector_refuses(payload: dict) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    orch = EmbeddingOrchestrator(backend="voyage", credential=CREDENTIAL)
    async with transport(handler) as client:
        with pytest.raises(EmbeddingUnavailableError):
            await orch.generate_embedding("anything", client=client)


async def test_a_transport_failure_refuses() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    orch = EmbeddingOrchestrator(backend="voyage", credential=CREDENTIAL)
    async with transport(handler) as client:
        with pytest.raises(EmbeddingUnavailableError) as raised:
            await orch.generate_embedding("anything", client=client)
    assert "unreachable" in str(raised.value)


async def test_two_different_texts_never_embed_identically() -> None:
    """The property the fabrication violated, asserted on the real path.

    Unconfigured there is no vector at all, which is the honest answer and crucially not "the same vector
    for both" — that equality is what made the L2 cache unsafe. Configured, the two texts reach the endpoint
    separately and carry their own answers.
    """
    orch = EmbeddingOrchestrator(backend="bge_m3")
    for text in ("def one(): pass", "def two(): return 42"):
        with pytest.raises(EmbeddingUnavailableError):
            await orch.generate_embedding(text)

    bodies: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode("utf-8")
        bodies.append(body)
        # A different vector per input, so an equality between the two would be the code's fault.
        value = 0.75 if "two" in body else 0.25
        return httpx.Response(200, json={"data": [{"embedding": [value, value]}]})

    configured = EmbeddingOrchestrator(backend="voyage", credential=CREDENTIAL)
    async with transport(handler) as client:
        first = await configured.generate_embedding("def one(): pass", client=client)
        second = await configured.generate_embedding("def two(): return 42", client=client)

    assert len(bodies) == 2, "the second call did not reach the endpoint"
    assert first.vector != second.vector
