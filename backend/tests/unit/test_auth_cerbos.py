# SPDX-License-Identifier: FSL-1.1-ALv2
"""The Cerbos client's wire format and fail-closed behaviour (design.md §11.2, D-55).

Scope split, deliberately
-------------------------
`tests/integration/test_cerbos_matrix.py` proves this client against the real
digest-pinned sidecar — that is where the *format* is confirmed. This module covers what a
real server cannot be made to produce on demand: a 500, a body that is not JSON, a body
that is an array, an answer that omits the action asked about, and a transport that dies.
Each of those must fail **closed**, and each must be distinguishable from a deny.

The transport is substituted with `httpx.MockTransport`, which §0.4.1 permits explicitly.
No collaborator is replaced and there is no `Mock` object here, so the `FO-TD00N` rules
have nothing to object to.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from src.auth.cerbos import (
    CHECK_RESOURCES_PATH,
    CerbosClient,
    CerbosPrincipal,
    CerbosResource,
    CerbosUnavailableError,
)

pytestmark = pytest.mark.mandatory

BASE = "http://cerbos.invalid:3592"
PRINCIPAL = CerbosPrincipal(id="11111111-1111-1111-1111-111111111111", roles=("admin",), attr={"kind": "user"})
RESOURCE = CerbosResource(kind="project", id="project-1", attr={"owner_id": "22222222-2222-2222-2222-222222222222"})


def _client(handler: Any) -> CerbosClient:
    return CerbosClient(BASE, http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


class TestTheRequestIsWhatCerbosDocuments:
    async def test_the_payload_shape_matches_the_check_resources_api(self) -> None:
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["method"] = request.method
            seen["body"] = json.loads(request.content)
            seen["content_type"] = request.headers.get("content-type", "")
            return httpx.Response(
                200,
                json={
                    "requestId": seen["body"]["requestId"],
                    "results": [
                        {"resource": {"kind": "project", "id": "project-1"}, "actions": {"read": "EFFECT_ALLOW"}}
                    ],
                },
            )

        assert await _client(handler).is_allowed(principal=PRINCIPAL, resource=RESOURCE, action="read")

        assert seen["method"] == "POST"
        assert seen["url"] == f"{BASE}{CHECK_RESOURCES_PATH}"
        assert seen["content_type"].startswith("application/json")
        body = seen["body"]
        assert set(body) == {"requestId", "principal", "resources"}
        assert body["principal"] == {
            "id": PRINCIPAL.id,
            "roles": ["admin"],
            "attr": {"kind": "user"},
        }
        assert body["resources"] == [
            {
                "resource": {
                    "kind": "project",
                    "id": "project-1",
                    "attr": {"owner_id": "22222222-2222-2222-2222-222222222222"},
                },
                "actions": ["read"],
            }
        ]

    async def test_each_call_carries_a_fresh_request_id(self) -> None:
        """Cerbos echoes `requestId` back for correlation. A constant would make two
        concurrent decisions indistinguishable in an audit trail."""
        ids: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            ids.append(json.loads(request.content)["requestId"])
            return httpx.Response(200, json={"results": []})

        client = _client(handler)
        await client.is_allowed(principal=PRINCIPAL, resource=RESOURCE, action="read")
        await client.is_allowed(principal=PRINCIPAL, resource=RESOURCE, action="read")
        assert len(set(ids)) == 2, ids

    async def test_a_batch_asks_one_question_per_resource_in_one_round_trip(self) -> None:
        calls: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            body = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "resource": entry["resource"],
                            "actions": dict.fromkeys(entry["actions"], "EFFECT_ALLOW"),
                        }
                        for entry in body["resources"]
                    ]
                },
            )

        results = await _client(handler).check_resources(
            principal=PRINCIPAL,
            resources=[
                (CerbosResource(kind="project", id="a"), ("read", "update")),
                (CerbosResource(kind="project", id="b"), ("read",)),
            ],
        )
        assert len(calls) == 1
        assert results[("project", "a")] == {"read": True, "update": True}
        assert results[("project", "b")] == {"read": True}


class TestEveryUnansweredQuestionIsADeny:
    async def test_effect_deny_is_false(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"resource": {"kind": "project", "id": "project-1"}, "actions": {"read": "EFFECT_DENY"}}
                    ]
                },
            )

        assert not await _client(handler).is_allowed(principal=PRINCIPAL, resource=RESOURCE, action="read")

    async def test_an_unrecognised_effect_string_is_false(self) -> None:
        """Anything that is not exactly `EFFECT_ALLOW` denies. A future Cerbos effect —
        or a typo — must not be read as permission."""

        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"resource": {"kind": "project", "id": "project-1"}, "actions": {"read": "EFFECT_ALLOW_MAYBE"}}
                    ]
                },
            )

        assert not await _client(handler).is_allowed(principal=PRINCIPAL, resource=RESOURCE, action="read")

    async def test_an_answer_that_omits_the_action_is_false(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"resource": {"kind": "project", "id": "project-1"}, "actions": {"update": "EFFECT_ALLOW"}}
                    ]
                },
            )

        assert not await _client(handler).is_allowed(principal=PRINCIPAL, resource=RESOURCE, action="read")

    async def test_an_answer_about_a_different_resource_is_false(self) -> None:
        """The result is keyed by `(kind, id)` precisely so an answer about resource B
        cannot authorise an action on resource A."""

        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "results": [{"resource": {"kind": "project", "id": "other"}, "actions": {"read": "EFFECT_ALLOW"}}]
                },
            )

        assert not await _client(handler).is_allowed(principal=PRINCIPAL, resource=RESOURCE, action="read")

    async def test_an_empty_results_list_is_false(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"results": []})

        assert not await _client(handler).is_allowed(principal=PRINCIPAL, resource=RESOURCE, action="read")

    async def test_no_resources_asks_nothing_and_returns_nothing(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
            raise AssertionError("an empty question must not reach the network")

        assert await _client(handler).check_resources(principal=PRINCIPAL, resources=[]) == {}


class TestAnOutageIsNeverADeny:
    """D-56: `CerbosUnavailableError` rather than `False`, so the caller can answer 503."""

    @pytest.mark.parametrize("status", [400, 401, 403, 404, 500, 502, 503])
    async def test_a_non_200_raises(self, status: int) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(status, json={"results": []})

        with pytest.raises(CerbosUnavailableError, match=str(status)):
            await _client(handler).is_allowed(principal=PRINCIPAL, resource=RESOURCE, action="read")

    async def test_a_body_that_is_not_json_raises(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"<html>gateway</html>", headers={"content-type": "text/html"})

        with pytest.raises(CerbosUnavailableError, match="not JSON"):
            await _client(handler).is_allowed(principal=PRINCIPAL, resource=RESOURCE, action="read")

    async def test_a_body_that_is_not_an_object_raises(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[{"resource": {"kind": "project", "id": "project-1"}}])

        with pytest.raises(CerbosUnavailableError, match="non-object"):
            await _client(handler).is_allowed(principal=PRINCIPAL, resource=RESOURCE, action="read")

    async def test_a_transport_failure_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        with pytest.raises(CerbosUnavailableError, match="ConnectError"):
            await _client(handler).is_allowed(principal=PRINCIPAL, resource=RESOURCE, action="read")

    async def test_a_timeout_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("slow", request=request)

        with pytest.raises(CerbosUnavailableError, match="ReadTimeout"):
            await _client(handler).is_allowed(principal=PRINCIPAL, resource=RESOURCE, action="read")

    async def test_the_error_message_carries_no_url(self) -> None:
        """A 503 detail must not leak the sidecar's address (Appendix C.1, D-27)."""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        with pytest.raises(CerbosUnavailableError) as caught:
            await _client(handler).is_allowed(principal=PRINCIPAL, resource=RESOURCE, action="read")
        assert "cerbos.invalid" not in str(caught.value)
        assert "3592" not in str(caught.value)


class TestHealth:
    async def test_a_serving_sidecar_returns_none(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/_cerbos/health", request.url
            return httpx.Response(200, json={"status": "SERVING"})

        assert await _client(handler).health() is None

    @pytest.mark.parametrize("status", [404, 500, 503])
    async def test_a_non_200_raises(self, status: int) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(status)

        with pytest.raises(CerbosUnavailableError):
            await _client(handler).health()

    async def test_a_transport_failure_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        with pytest.raises(CerbosUnavailableError):
            await _client(handler).health()


class TestTheInputsCannotBeMutatedAfterTheCheck:
    def test_the_resource_is_frozen(self) -> None:
        with pytest.raises((AttributeError, TypeError)):
            RESOURCE.kind = "secret"  # type: ignore[misc]

    def test_the_principal_is_frozen(self) -> None:
        with pytest.raises((AttributeError, TypeError)):
            PRINCIPAL.roles = ("viewer",)  # type: ignore[misc]

    def test_the_payload_copies_the_attributes(self) -> None:
        """A caller holding the dict it passed in must not be able to change what was
        asked after the fact."""
        attr = {"owner_id": "x"}
        resource = CerbosResource(kind="project", id="p", attr=attr)
        payload = resource.to_payload()
        attr["owner_id"] = "y"
        assert payload["attr"] == {"owner_id": "x"}


class TestCerbosPrincipalProjection:
    def test_exactly_one_role_travels(self) -> None:
        """§11.2's model is one role per user. Passing several would let a policy see an
        authority combination the product does not model."""
        import uuid

        from src.auth.dependencies import cerbos_principal
        from src.auth.models import UserRole
        from src.auth.principal import Principal

        principal = Principal.for_user(
            user_id=uuid.uuid4(),
            subject="test-only-subject",
            email="dev@forgeops.invalid",
            role=UserRole.DEVELOPER,
        )
        projected = cerbos_principal(principal)
        assert projected.roles == ("developer",)
        assert projected.id == str(principal.user_id)
        assert projected.attr["kind"] == "user"
        assert projected.attr["blast_radius"] == principal.blast_radius

    def test_no_email_or_subject_reaches_the_policy_engine(self) -> None:
        """Nothing a policy does not need. An identifier and a role are the authority;
        an email in a policy input is PII crossing a boundary for no decision."""
        import uuid

        from src.auth.dependencies import cerbos_principal
        from src.auth.models import UserRole
        from src.auth.principal import Principal

        principal = Principal.for_user(
            user_id=uuid.uuid4(),
            subject="test-only-subject",
            email="dev@forgeops.invalid",
            role=UserRole.VIEWER,
        )
        rendered = json.dumps(cerbos_principal(principal).to_payload())
        assert "dev@forgeops.invalid" not in rendered
        assert "test-only-subject" not in rendered
