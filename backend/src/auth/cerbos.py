# SPDX-License-Identifier: FSL-1.1-ALv2
"""The Cerbos sidecar client — resource-scoped authorisation (design.md §11.2, D-55).

Why a wrapper and not the SDK
-----------------------------
§11.1 composes this as `CerbosClient(settings.cerbos_url, http=shared_http)`: a URL and
an **injected** `httpx.AsyncClient`. That is not the `cerbos` SDK's constructor — its
client builds and owns its own transport — so §11.1 always described a project-owned
wrapper with the pin sitting unused underneath it. D-55 records the rest: the SDK's
published metadata makes `grpcio-tools`, `protobuf`, `grpcio-status` and
`protoc-gen-openapiv2` *runtime* requirements of the backend image, which is a protoc
compiler plugin and a gRPC stack shipped into a production API container to ask Cerbos
one question. The wire format lives in this one module and is asserted against the
digest-pinned server by `test_cerbos_matrix.py`, so drift fails a mandatory check.

Fail closed
-----------
Deny-by-default is the whole posture (§4.4), so an unreachable or malformed Cerbos is a
**deny**, never an allow. It is reported as `authorization-unavailable` (503) rather than
`forbidden` (403), because the two have different remedies: 503 means retry, 403 means
stop. Collapsing them would make an outage look like a wall of correctly-refused
callers — the D-23 failure shape this phase exists to remove.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Final

import httpx

#: The one Cerbos endpoint Phase 1 uses. Versioned by Cerbos itself; the policy files
#: carry the matching `apiVersion: api.cerbos.dev/v1`.
CHECK_RESOURCES_PATH: Final[str] = "/api/check/resources"

#: Cerbos's allow effect. Anything else — deny, no matching rule, an action Cerbos did
#: not answer for — is a deny here.
EFFECT_ALLOW: Final[str] = "EFFECT_ALLOW"

#: Cerbos's own readiness endpoint. Used by `/health/ready`, not by the check path.
HEALTH_PATH: Final[str] = "/_cerbos/health"


@dataclass(frozen=True, slots=True)
class CerbosResource:
    """The resource half of a Cerbos request.

    `attr` carries exactly the attributes the policies read — `owner_id`, `member_ids`,
    `created_by`, `scope`. Frozen, because an authorisation input that a handler can
    mutate after the check is an authorisation input that means nothing.
    """

    kind: str
    id: str
    attr: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        return {"kind": self.kind, "id": self.id, "attr": dict(self.attr)}


@dataclass(frozen=True, slots=True)
class CerbosPrincipal:
    """The principal half. Derived from a verified `Principal`, never from request data."""

    id: str
    roles: tuple[str, ...]
    attr: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        return {"id": self.id, "roles": list(self.roles), "attr": dict(self.attr)}


class CerbosUnavailableError(RuntimeError):
    """Cerbos could not be consulted. Callers translate this to 503, never to 403."""


class CerbosClient:
    """Asks Cerbos one question: may this principal do this action to this resource?

    Owns only the call. Cerbos owns the policy — that split is what keeps role logic out
    of handlers (§11.2), and it is why this class has no branch on role or action.
    """

    def __init__(self, base_url: str | Any, *, http: httpx.AsyncClient, timeout_seconds: float = 2.0) -> None:
        self._base_url = str(base_url).rstrip("/")
        self._http = http
        self._timeout = timeout_seconds

    @property
    def base_url(self) -> str:
        return self._base_url

    async def is_allowed(
        self,
        *,
        principal: CerbosPrincipal,
        resource: CerbosResource,
        action: str,
    ) -> bool:
        """One action against one resource. `False` on deny; raises on outage."""
        results = await self.check_resources(principal=principal, resources=[(resource, (action,))])
        return results.get((resource.kind, resource.id), {}).get(action, False)

    async def check_resources(
        self,
        *,
        principal: CerbosPrincipal,
        resources: list[tuple[CerbosResource, tuple[str, ...]]],
    ) -> dict[tuple[str, str], dict[str, bool]]:
        """Batch form. Returns `{(kind, id): {action: allowed}}`.

        Batched because the matrix test and any future list endpoint ask about many
        resources at once, and one round trip per resource would make a list view's
        latency a function of its page size.
        """
        if not resources:
            # Not an empty allow — an empty *question*. Returning `{}` here is safe only
            # because every caller reads a specific (kind, id, action) out of the result
            # and a missing key is a deny.
            return {}

        payload = {
            "requestId": uuid.uuid4().hex,
            "principal": principal.to_payload(),
            "resources": [
                {"resource": resource.to_payload(), "actions": list(actions)} for resource, actions in resources
            ],
        }

        try:
            response = await self._http.post(
                f"{self._base_url}{CHECK_RESOURCES_PATH}",
                json=payload,
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            raise CerbosUnavailableError(f"cerbos transport failure: {type(exc).__name__}") from exc

        if response.status_code != 200:
            raise CerbosUnavailableError(f"cerbos answered {response.status_code}")

        try:
            body = response.json()
        except ValueError as exc:
            raise CerbosUnavailableError("cerbos answered with a body that is not JSON") from exc

        if not isinstance(body, dict):
            raise CerbosUnavailableError("cerbos answered with a non-object body")

        out: dict[tuple[str, str], dict[str, bool]] = {}
        for entry in body.get("results") or []:
            if not isinstance(entry, dict):
                continue
            resource_ref = entry.get("resource") or {}
            key = (str(resource_ref.get("kind", "")), str(resource_ref.get("id", "")))
            actions = entry.get("actions") or {}
            if not isinstance(actions, dict):
                continue
            out[key] = {str(name): effect == EFFECT_ALLOW for name, effect in actions.items()}
        return out

    async def health(self) -> None:
        """Readiness probe. Raises `CerbosUnavailableError` when Cerbos cannot serve.

        Cerbos's own health endpoint, not a synthetic `check` call: a check would need a
        principal and a resource kind, and inventing one for a probe means the probe
        starts failing when a policy changes.
        """
        try:
            response = await self._http.get(f"{self._base_url}{HEALTH_PATH}", timeout=self._timeout)
        except httpx.HTTPError as exc:
            raise CerbosUnavailableError(f"cerbos transport failure: {type(exc).__name__}") from exc
        if response.status_code != 200:
            raise CerbosUnavailableError(f"cerbos health answered {response.status_code}")


__all__ = [
    "CHECK_RESOURCES_PATH",
    "EFFECT_ALLOW",
    "CerbosClient",
    "CerbosPrincipal",
    "CerbosResource",
    "CerbosUnavailableError",
]
