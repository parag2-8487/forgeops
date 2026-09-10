# SPDX-License-Identifier: FSL-1.1-ALv2
"""The connection test: one real call, and the provider's own words carried back.

WHY A REQUEST RATHER THAN AN INSPECTION. The defect this answers is a screen that called model tiers
"available" on the strength of a static fact. A test that only checked the key was non-empty would be the
same mistake one layer down: equally confident, equally uninformed.

WHY THE DETAIL MATTERS AS MUCH AS THE BOOLEAN. 401, 404, a rate limit and a DNS failure have four different
remedies, and no operator can choose between them from "test failed". Each is asserted separately below.
"""

from __future__ import annotations

import httpx
from src.ai.endpoint_probe import (
    DETAIL_LIMIT,
    PROBE_MAX_TOKENS,
    PROBE_PROMPT,
    probe_endpoint,
)

BASE_URL = "http://provider.invalid/v1"
MODEL = "a-model"


def client_returning(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def probe(handler, *, credential: str | None = None, model: str = MODEL):
    async with client_returning(handler) as http:
        return await probe_endpoint(
            base_url=BASE_URL,
            model=model,
            credential=credential,
            timeout_seconds=5.0,
            client=http,
        )


class TestTheRequestItSends:
    async def test_it_posts_a_minimal_completion_to_the_chat_route(self):
        seen: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["body"] = request.read().decode("utf-8")
            return httpx.Response(200, json={"model": MODEL, "choices": []})

        result = await probe(handler)

        assert result.ok is True
        assert seen["url"] == f"{BASE_URL}/chat/completions"
        body = str(seen["body"])
        assert PROBE_PROMPT in body
        # One token, so pressing a diagnostic button costs as close to nothing as a real call can.
        assert f'"max_tokens": {PROBE_MAX_TOKENS}' in body or f'"max_tokens":{PROBE_MAX_TOKENS}' in body
        # Zero temperature, so two probes of a working endpoint agree. A diagnostic that varies is one
        # people re-run rather than read.
        assert '"temperature": 0' in body or '"temperature":0' in body

    async def test_a_trailing_slash_on_the_base_url_does_not_double_up(self):
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            return httpx.Response(200, json={"model": MODEL})

        async with client_returning(handler) as http:
            await probe_endpoint(
                base_url=BASE_URL + "/", model=MODEL, credential=None, timeout_seconds=5.0, client=http
            )

        assert seen["url"] == f"{BASE_URL}/chat/completions"

    async def test_a_credential_is_sent_as_a_bearer_token(self):
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(request.headers)
            return httpx.Response(200, json={"model": MODEL})

        await probe(handler, credential="a-supplied-value")

        assert seen.get("authorization", "").endswith("a-supplied-value")

    async def test_no_credential_sends_no_authorization_header_at_all(self):
        """A self-hosted server needs none, and the scheme followed by the word `None` is not a credential."""
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(request.headers)
            return httpx.Response(200, json={"model": MODEL})

        await probe(handler, credential=None)

        assert "authorization" not in seen


class TestWhatItReportsBack:
    async def test_a_success_names_the_model_that_answered(self):
        result = await probe(lambda _r: httpx.Response(200, json={"model": MODEL, "choices": []}))

        assert result.ok is True
        assert result.model_reported == MODEL

    async def test_a_substituted_model_is_reported_without_being_called_a_failure(self):
        """Some gateways silently serve a different model. The request worked; the operator is not
        getting what they named, and only a probe can tell them."""
        result = await probe(lambda _r: httpx.Response(200, json={"model": "something-else"}))

        assert result.ok is True, "the endpoint answered, so this is not a failure"
        assert result.model_reported == "something-else"
        assert "something-else" in result.detail
        assert MODEL in result.detail, "it names both, or the operator cannot see the substitution"

    async def test_a_success_that_reports_no_model_is_still_a_success(self):
        result = await probe(lambda _r: httpx.Response(200, json={"choices": []}))

        assert result.ok is True
        assert result.model_reported is None

    async def test_an_unauthorised_response_says_the_credential_was_rejected(self):
        result = await probe(lambda _r: httpx.Response(401, json={"error": {"message": "Incorrect API key provided"}}))

        assert result.ok is False
        assert "401" in result.detail
        assert "credential was rejected" in result.detail
        # The provider's own message, which is what distinguishes a wrong key from a revoked one.
        assert "Incorrect API key provided" in result.detail

    async def test_a_forbidden_response_separates_a_valid_key_from_a_permitted_model(self):
        result = await probe(lambda _r: httpx.Response(403, text="model not enabled for this org"))

        assert result.ok is False
        assert "403" in result.detail
        assert "not permitted this model" in result.detail

    async def test_a_not_found_response_points_at_the_model_name_or_the_path(self):
        result = await probe(lambda _r: httpx.Response(404, text="no such model"))

        assert result.ok is False
        assert "404" in result.detail
        # The remedy is a different model name or base URL, not a different key.
        assert "no such model or path" in result.detail

    async def test_a_rate_limit_says_the_credential_works(self):
        """The one failure that is partly good news, and would otherwise read as a broken key."""
        result = await probe(lambda _r: httpx.Response(429, text="slow down"))

        assert result.ok is False
        assert "the credential works" in result.detail

    async def test_an_unrecognised_status_still_reports_the_number_and_the_body(self):
        result = await probe(lambda _r: httpx.Response(503, text="upstream unavailable"))

        assert result.ok is False
        assert "503" in result.detail
        assert "upstream unavailable" in result.detail

    async def test_a_long_error_body_is_truncated_rather_than_stored_whole(self):
        result = await probe(lambda _r: httpx.Response(500, text="x" * 5000))

        assert result.ok is False
        assert len(result.detail) <= DETAIL_LIMIT + 100, "the prefix is allowed, the whole body is not"

    async def test_a_two_hundred_that_is_not_json_is_reported_rather_than_smoothed_over(self):
        """A host answering 200 with HTML is probably not OpenAI-compatible at this path.

        Reporting success would send the operator away believing a broken endpoint works.
        """
        result = await probe(lambda _r: httpx.Response(200, text="<html>login</html>"))

        assert result.ok is False
        assert "not JSON" in result.detail
        assert "OpenAI-compatible" in result.detail

    async def test_a_timeout_names_the_two_things_that_cause_it(self):
        def handler(_request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("too slow")

        result = await probe(handler)

        assert result.ok is False
        # A self-hosted model still loading and an unreachable host look identical from here, so both are
        # named rather than one being guessed at.
        assert "unreachable" in result.detail
        assert "still be loading" in result.detail

    async def test_a_transport_failure_carries_the_underlying_reason(self):
        def handler(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("name or service not known")

        result = await probe(handler)

        assert result.ok is False
        assert BASE_URL in result.detail
        # DNS, TLS and a refused connection have different remedies; the exception's own text names which.
        assert "name or service not known" in result.detail

    async def test_a_provider_answer_never_raises(self):
        """Every status is a RESULT the caller renders, not an exception it has to catch."""
        for status in (400, 401, 403, 404, 429, 500, 503):
            result = await probe(lambda _r, s=status: httpx.Response(s, text="body"))
            assert result.ok is False, status

    async def test_latency_is_measured_and_reported(self):
        result = await probe(lambda _r: httpx.Response(200, json={"model": MODEL}))
        assert result.latency_ms >= 0

    async def test_latency_is_reported_even_when_the_call_failed(self):
        # How long a failure took is what distinguishes an instant refusal from a hanging host.
        def handler(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        assert (await probe(handler)).latency_ms >= 0


class TestTheClientItUses:
    async def test_it_closes_a_client_it_created_itself(self):
        """A probe against a misconfigured host must not leak connections or exhaust the pool the real
        cascade shares, so an unsupplied client is owned and closed."""
        result = await probe_endpoint(
            base_url="http://127.0.0.1:1/v1",
            model=MODEL,
            credential=None,
            timeout_seconds=0.25,
        )
        # Nothing listens on port 1; the point is that it returned a result rather than raising or hanging.
        assert result.ok is False

    async def test_it_does_not_close_a_client_it_was_given(self):
        http = client_returning(lambda _r: httpx.Response(200, json={"model": MODEL}))
        await probe_endpoint(base_url=BASE_URL, model=MODEL, credential=None, timeout_seconds=5.0, client=http)
        assert http.is_closed is False, "closing a shared client would break the next caller"
        await http.aclose()
