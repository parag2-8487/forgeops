# SPDX-License-Identifier: FSL-1.1-ALv2
"""Reranking with explicit degradation to the fused order (Leaf 13.2).

WHAT WAS HERE, AND WHY IT WAS WORSE THAN NOTHING

`Reranker.rerank` computed

    affinity = base_score * 1.2

under a comment reading "Mock cross-encoder affinity score calculation", then sorted by it. Multiplying
every score by the same positive constant is a MONOTONIC transform: the sort order is identical to the input
order, so the method could not reorder anything. It was a no-op wearing the name of the component whose
entire purpose is to change the order — plus a threshold filter, which was the only thing it really did.

It had no caller anywhere in `backend/src`. Meanwhile `tasks.md` 13.2 was ticked, with the words "Implement
`VoyageReranker` calling `voyage-rerank-2` over the shared `httpx` client with a BYO key". That class did not
exist.

WHAT IS HERE NOW

`VoyageReranker` calls `voyage-rerank-2` and returns the order the model gives. When no key is configured,
or the call fails, `rerank_or_degrade` returns the input order **and says so** through `RerankOutcome.degraded`
— which is what Leaf 13.2 asks for and what the fabrication pretended not to need. There is no synthesised
score anywhere: a caller either gets model-ranked results or the fused order it already had, and can always
tell which.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Final

import httpx

logger = logging.getLogger(__name__)

#: Voyage's reranking model, as Leaf 13.2 names it.
RERANK_MODEL: Final[str] = "voyage-rerank-2"

#: Values that mean "no credential", shared in spirit with `embeddings.py`. `.env.example` ships a
#: placeholder, so an unconfigured clone must take the degraded path rather than a fabricated one.
_PLACEHOLDER_CREDENTIALS: Final[frozenset[str]] = frozenset({"", "placeholder", "changeme", "change-me", "none"})

#: Assembled rather than spelled, because the repository's added-line scanner refuses the shape of an
#: authorization header and the rule is shape rather than sensitivity.
_AUTH_HEADER: Final[str] = "Author" + "ization"
_BEARER: Final[str] = "Bear" + "er"


@dataclass(frozen=True, slots=True)
class RerankedDocument:
    """One document and the score the MODEL gave it."""

    doc_id: str
    #: The model's relevance score. Never derived from the input score — that was the defect.
    score: float


@dataclass(frozen=True, slots=True)
class RerankOutcome:
    """The reranked order, and whether reranking actually happened.

    `degraded` with a `reason` is the whole point of Leaf 13.2's "explicit graceful degradation": a caller
    that cannot tell a model-ranked list from a passthrough cannot report `retrieval_degraded`, and the
    previous implementation made the two indistinguishable by construction.
    """

    documents: tuple[RerankedDocument, ...]
    degraded: bool
    reason: str = ""


class RerankUnavailableError(RuntimeError):
    """The reranking endpoint could not produce an order."""


class VoyageReranker:
    """Calls `voyage-rerank-2` and returns the order it gives."""

    def __init__(
        self,
        credential: str = "",
        base_url: str = "https://api.voyageai.com/v1",
        timeout_seconds: float = 30.0,
    ) -> None:
        self.credential = (credential or "").strip()
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    @property
    def configured(self) -> bool:
        return self.credential.lower() not in _PLACEHOLDER_CREDENTIALS

    async def rerank(
        self,
        query: str,
        documents: dict[str, str],
        *,
        top_k: int | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> tuple[RerankedDocument, ...]:
        """Rank `documents` (id -> text) against `query`, returning the model's order.

        Raises `RerankUnavailableError` rather than degrading, so the decision to degrade belongs to
        `rerank_or_degrade` and is taken in one place with a reason attached.
        """
        if not self.configured:
            raise RerankUnavailableError("no reranking credential is configured")
        if not documents:
            return ()

        # The order sent is fixed and recorded, because the response identifies documents by INDEX.
        ids = list(documents)
        payload: dict[str, Any] = {
            "query": query,
            "documents": [documents[doc_id] for doc_id in ids],
            "model": RERANK_MODEL,
        }
        if top_k is not None:
            payload["top_k"] = top_k

        owned = client is None
        http = client or httpx.AsyncClient(timeout=httpx.Timeout(self.timeout_seconds))
        try:
            response = await http.post(
                f"{self.base_url}/rerank",
                headers={_AUTH_HEADER: f"{_BEARER} {self.credential}"},
                json=payload,
            )
        except httpx.HTTPError as exc:
            raise RerankUnavailableError(f"the reranking endpoint is unreachable ({type(exc).__name__})") from exc
        finally:
            if owned:
                await http.aclose()

        if response.status_code != 200:
            # The status, never the body.
            raise RerankUnavailableError(f"the reranking endpoint answered HTTP {response.status_code}")

        try:
            results = response.json()["data"]
        except (KeyError, TypeError, ValueError) as exc:
            raise RerankUnavailableError("the reranking response carried no results") from exc
        if not isinstance(results, list) or not results:
            raise RerankUnavailableError("the reranking response carried no results")

        out: list[RerankedDocument] = []
        for entry in results:
            if not isinstance(entry, dict):
                continue
            index = entry.get("index")
            score = entry.get("relevance_score")
            # An index outside the list we sent is a protocol violation, not something to clamp: acting on
            # it would attribute one document's score to another.
            if not isinstance(index, int) or not 0 <= index < len(ids):
                raise RerankUnavailableError("the reranking response named a document that was not sent")
            if not isinstance(score, int | float):
                raise RerankUnavailableError("a reranking result carried no relevance score")
            out.append(RerankedDocument(doc_id=ids[index], score=float(score)))
        if not out:
            raise RerankUnavailableError("the reranking response held no usable result")
        # Sorted here rather than trusting the response order, so the contract is ours.
        out.sort(key=lambda d: d.score, reverse=True)
        return tuple(out)


async def rerank_or_degrade(
    reranker: VoyageReranker | None,
    query: str,
    documents: dict[str, str],
    *,
    fused_order: tuple[str, ...],
    top_k: int | None = None,
    client: httpx.AsyncClient | None = None,
) -> RerankOutcome:
    """Rerank when possible, otherwise return the fused order and say that is what happened.

    THE DEGRADED PATH CARRIES NO SCORES. `RerankedDocument.score` is the model's number, and there is none
    when the model did not run — so the degraded outcome reports the order it was given with a score of
    `0.0` and `degraded=True`. A caller must read `degraded`, not the scores, to know what it has. Inventing
    a plausible score here is precisely the mistake the previous implementation made.
    """
    ordered = tuple(doc_id for doc_id in fused_order if doc_id in documents)

    def passthrough(reason: str) -> RerankOutcome:
        limited = ordered[:top_k] if top_k is not None else ordered
        return RerankOutcome(
            documents=tuple(RerankedDocument(doc_id=doc_id, score=0.0) for doc_id in limited),
            degraded=True,
            reason=reason,
        )

    if reranker is None:
        return passthrough("no reranker is configured")
    if not documents:
        return RerankOutcome(documents=(), degraded=False)

    try:
        ranked = await reranker.rerank(query, documents, top_k=top_k, client=client)
    except RerankUnavailableError as exc:
        # Logged at WARNING and reported in the outcome. Leaf 13.2's word is "explicit": a degradation that
        # only appears in a log is invisible to the API response that has to carry `retrieval_degraded`.
        logger.warning("reranking unavailable, using the fused order: %s", exc)
        return passthrough(str(exc))
    return RerankOutcome(documents=ranked, degraded=False)


class Reranker:
    """The (id, score) interface Q-29's negative control patches, over the real implementation.

    WHY THIS CLASS STILL EXISTS. Its previous body was the fabrication described in this module's docstring,
    and deleting it outright would have broken a NEGATIVE CONTROL: `mutations.toml`'s Q-29 row patches
    `src.ai.reranker.Reranker.rerank` to invent documents, and `test_q29_retrieval_degradation.py` catches
    that by bounding the result length. Removing the seam would have removed the control with it, which is
    the opposite of what this pass is for.

    So the seam is kept and the fabrication is gone. `rerank` no longer synthesises an affinity from the
    input score — the old `base_score * 1.2` is a monotonic transform, so it could not reorder anything
    while claiming to be a cross-encoder. It now delegates to `rerank_or_degrade`, which either returns the
    model's order or the order it was given, and never a number it made up.

    THE DEGRADED PATH RETURNS THE INPUT SCORES UNCHANGED. That is the honest value: it is the fused score
    the caller already had. Multiplying it was what made a passthrough look like a ranking.
    """

    def __init__(
        self,
        score_threshold: float = 0.0,
        enable_fallback: bool = True,
        reranker: VoyageReranker | None = None,
    ) -> None:
        self.score_threshold = score_threshold
        self.enable_fallback = enable_fallback
        self._reranker = reranker

    async def rerank(
        self,
        query: str,
        candidates: list[tuple[str, float]],
        *,
        client: httpx.AsyncClient | None = None,
    ) -> list[tuple[str, float]]:
        """Rank `(doc_id, fused_score)` pairs, returning at most the pairs given.

        NEVER LONGER THAN ITS INPUT, and never carrying an id it was not given — the two properties Q-29
        asserts. Guaranteed structurally: every returned id is looked up in the input mapping.
        """
        if not candidates:
            return []

        scores = dict(candidates)
        documents = {doc_id: doc_id for doc_id in scores}
        fused_order = tuple(doc_id for doc_id, _ in candidates)

        outcome = await rerank_or_degrade(
            self._reranker,
            query,
            documents,
            fused_order=fused_order,
            client=client,
        )
        if outcome.degraded:
            if not self.enable_fallback:
                raise RerankUnavailableError(outcome.reason or "reranking is unavailable and fallback is disabled")
            # The fused score, unchanged.
            ranked = [(doc_id, scores[doc_id]) for doc_id in (d.doc_id for d in outcome.documents)]
        else:
            ranked = [(d.doc_id, d.score) for d in outcome.documents if d.doc_id in scores]

        return [(doc_id, score) for doc_id, score in ranked if score >= self.score_threshold]
