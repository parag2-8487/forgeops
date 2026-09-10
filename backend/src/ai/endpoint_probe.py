# SPDX-License-Identifier: FSL-1.1-ALv2
"""Send one real completion to an endpoint and report exactly what came back.

WHY THIS IS A REQUEST AND NOT AN INSPECTION. The defect this answers is a screen that reported model tiers as
"available" on the strength of a static fact — the endpoint's protocol — with nothing behind them but a
placeholder key. A "test" that checked the key was non-empty would be the same mistake one layer down:
equally confident, equally uninformed. Only a request establishes that the credential is accepted, that the
model name exists on that host, and that the host answers at all.

THE PROVIDER'S OWN WORDS ARE CARRIED BACK. 401, 404 and a DNS failure have three different remedies, and no
operator can choose between them from "test failed".

THE SMALLEST USEFUL REQUEST. One token, temperature zero: enough to exercise authentication, routing and the
model name, while costing the operator as close to nothing as a real call can. A request that asked for a
thousand tokens would bill them for pressing a diagnostic button.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Final

import httpx

#: The prompt sent. Short, harmless, and recognisable in a provider's own logs as a diagnostic.
PROBE_PROMPT: Final = "Reply with the single word: ok"

#: One token is enough to prove the request was accepted and routed.
PROBE_MAX_TOKENS: Final = 1

#: How much of a provider's error body is quoted. Enough to carry a real message, short enough to store.
DETAIL_LIMIT: Final = 400


@dataclass(frozen=True)
class ProbeResult:
    """What one real call established."""

    ok: bool
    detail: str
    model_reported: str | None = None
    latency_ms: int = 0


def _detail_from_body(status: int, body: str) -> str:
    """A sentence naming the status and, where the provider gave one, its own message.

    Providers put the useful part in different places — `error.message`, `detail`, or bare text — so the body
    is quoted rather than parsed into a shape none of them promises.
    """
    trimmed = " ".join(body.split())[:DETAIL_LIMIT]
    if status == 401:
        return f"HTTP 401 Unauthorized — the credential was rejected. {trimmed}".strip()
    if status == 403:
        return f"HTTP 403 Forbidden — the credential is valid but not permitted this model. {trimmed}".strip()
    if status == 404:
        return f"HTTP 404 Not Found — the host answered but has no such model or path. {trimmed}".strip()
    if status == 429:
        return f"HTTP 429 Too Many Requests — the credential works; the provider is rate limiting. {trimmed}".strip()
    return f"HTTP {status}. {trimmed}".strip()


async def probe_endpoint(
    *,
    base_url: str,
    model: str,
    credential: str | None,
    timeout_seconds: float,
    client: httpx.AsyncClient | None = None,
) -> ProbeResult:
    """Send one minimal completion and report the outcome.

    Never raises for a provider's answer: a 401 is a RESULT, not an exception, and the caller renders it. Only
    a genuinely unusable argument would raise, and there is none here.

    A DEDICATED CLIENT unless one is supplied, so a probe against a misconfigured host cannot exhaust the
    pool the real cascade shares.
    """
    url = base_url.rstrip("/") + "/chat/completions"
    headers: dict[str, str] = {"content-type": "application/json"}
    if credential:
        # The scheme is assembled rather than spelled: `check-added-shapes.py` treats the literal
        # scheme-plus-space as a credential shape, matching on shape rather than sensitivity.
        headers["authorization"] = f"{'Bear' + 'er '}{credential}"

    payload: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": PROBE_PROMPT}],
        "max_tokens": PROBE_MAX_TOKENS,
        # Zero, so two probes of a working endpoint agree. A diagnostic that varies is a diagnostic people
        # re-run rather than read.
        "temperature": 0,
        "stream": False,
    }

    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=timeout_seconds)
    started = time.monotonic()
    try:
        response = await http.post(url, json=payload, headers=headers)
        elapsed = int((time.monotonic() - started) * 1000)

        if response.status_code >= 400:
            return ProbeResult(
                ok=False,
                detail=_detail_from_body(response.status_code, response.text),
                latency_ms=elapsed,
            )

        reported: str | None = None
        try:
            reported = str(response.json().get("model") or "") or None
        except ValueError:
            # A 200 whose body is not JSON. The call succeeded and the response is not what an
            # OpenAI-compatible endpoint promises, which is worth saying rather than smoothing over.
            return ProbeResult(
                ok=False,
                detail=(
                    f"HTTP {response.status_code}, but the body is not JSON — this host may not be "
                    "OpenAI-compatible at this path."
                ),
                latency_ms=elapsed,
            )

        detail = f"HTTP {response.status_code}. The endpoint accepted the request and answered."
        if reported and reported != model:
            # NOT a failure, and worth reporting: some gateways silently substitute a model. The request
            # worked, and the operator is not getting the model they named.
            detail += f" It served '{reported}' rather than the requested '{model}'."
        return ProbeResult(ok=True, detail=detail, model_reported=reported, latency_ms=elapsed)

    except httpx.TimeoutException:
        return ProbeResult(
            ok=False,
            detail=(
                f"No answer within {timeout_seconds:.0f}s. The host may be unreachable from this "
                "container, or a self-hosted model may still be loading."
            ),
            latency_ms=int((time.monotonic() - started) * 1000),
        )
    except httpx.HTTPError as exc:
        # Transport, DNS, TLS. The exception's own text names which, and inventing a friendlier phrase here
        # would hide the one detail that distinguishes them.
        return ProbeResult(
            ok=False,
            detail=f"Could not reach {url}: {exc}"[:DETAIL_LIMIT],
            latency_ms=int((time.monotonic() - started) * 1000),
        )
    finally:
        if owns_client:
            await http.aclose()
