# SPDX-License-Identifier: FSL-1.1-ALv2
"""The §11.2 RBAC matrix against a REAL Cerbos (design.md §11.2, §13.3; task 6.4).

What this proves that `matrix_test.yaml` cannot
-----------------------------------------------
`policies/cerbos/matrix_test.yaml` runs inside Cerbos's own test runner and proves the
*policies* say what §11.2's table says. It says nothing about whether the production
`CerbosClient` reaches them, builds a request Cerbos understands, or reads the answer the
right way round — and "reads the answer the right way round" is exactly the kind of thing
that fails silently in the safe direction during development and the unsafe direction in
production. D-55 makes that gap load-bearing: the wire format is this repository's, so it
needs a test against the pinned server rather than against a library's promise.

So this module drives the **production** client against the digest-pinned
`ghcr.io/cerbos/cerbos:0.54.0` over the whole matrix — every role against every resource
kind and every action §11.2 names, allow *and* deny — and then drives `require_permission`
through a real FastAPI app to prove the mapping onto HTTP.

Gating
------
`require_capability("cerbos")` when `FORGEOPS_TEST_CERBOS_URL` is unset: skips locally,
**fails** under `FORGEOPS_REQUIRE_INTEGRATION=1`.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio

from .capability import require_capability

pytestmark = [pytest.mark.mandatory, pytest.mark.cerbos]

CERBOS_URL_ENV = "FORGEOPS_TEST_CERBOS_URL"

#: Stable synthetic ids. The developer owns `project_dev_owned` and the viewer is a
#: member of it, which is what lets one resource cover both of §11.2's qualifiers.
ADMIN_ID = "11111111-1111-1111-1111-111111111111"
DEVELOPER_ID = "22222222-2222-2222-2222-222222222222"
VIEWER_ID = "33333333-3333-3333-3333-333333333333"

#: §11.2's table, transcribed as `(resource_kind, action) -> {role: allowed}`.
#:
#: Transcribed rather than derived from the policy files on purpose: deriving it would
#: make this test assert that the policies agree with themselves. The table is the
#: authority and this is a second, independent statement of it, so a policy edit that
#: changes behaviour has to change this file too — which is the review the edit deserves.
#:
#: Every cell is present, including the denies. A matrix with only the allows would pass
#: against a policy set that allowed everything.
MATRIX: dict[tuple[str, str], dict[str, bool]] = {
    # project — admin: create, read, update, delete, pair
    #           developer: create, read, update (own/member)
    #           viewer: read
    ("project", "create"): {"admin": True, "developer": True, "viewer": False},
    ("project", "read"): {"admin": True, "developer": True, "viewer": True},
    ("project", "update"): {"admin": True, "developer": True, "viewer": False},
    ("project", "delete"): {"admin": True, "developer": False, "viewer": False},
    ("project", "pair"): {"admin": True, "developer": False, "viewer": False},
    # change_set — admin: read, approve, reject, apply, revert
    #              developer: create, read, approve (unless policy forbids self-approval)
    #              viewer: read
    #
    # `create` is a developer verb and NOT an admin one, exactly as §11.2's table has it.
    # That reads like an omission and is not: an admin can approve and apply, and letting
    # one identity author and approve defeats the approval gate. Transcribing it as an
    # admin allow was this table's first defect and the policy set caught it.
    ("change_set", "create"): {"admin": False, "developer": True, "viewer": False},
    ("change_set", "read"): {"admin": True, "developer": True, "viewer": True},
    # `approve` here is against a change-set authored by SOMEBODY ELSE (see
    # `CHANGE_SET_AUTHOR`). Self-approval is barred for every role including admin, and
    # `test_self_approval_is_barred_by_policy` covers that separately — a single cell
    # cannot say both things.
    ("change_set", "approve"): {"admin": True, "developer": True, "viewer": False},
    ("change_set", "reject"): {"admin": True, "developer": False, "viewer": False},
    ("change_set", "apply"): {"admin": True, "developer": False, "viewer": False},
    ("change_set", "revert"): {"admin": True, "developer": False, "viewer": False},
    # policy — admin: create, read, update, delete; developer/viewer: read
    ("policy", "create"): {"admin": True, "developer": False, "viewer": False},
    ("policy", "read"): {"admin": True, "developer": True, "viewer": True},
    ("policy", "update"): {"admin": True, "developer": False, "viewer": False},
    ("policy", "delete"): {"admin": True, "developer": False, "viewer": False},
    # secret — admin: create, read_metadata, update, delete
    #          developer: create, read_metadata, update
    #          viewer: read_metadata
    #          NOBODY: read_value
    ("secret", "create"): {"admin": True, "developer": True, "viewer": False},
    ("secret", "read_metadata"): {"admin": True, "developer": True, "viewer": True},
    ("secret", "update"): {"admin": True, "developer": True, "viewer": False},
    ("secret", "delete"): {"admin": True, "developer": False, "viewer": False},
    ("secret", "read_value"): {"admin": False, "developer": False, "viewer": False},
    # agent_device — admin: pair, revoke, read; developer: pair, read; viewer: read
    ("agent_device", "pair"): {"admin": True, "developer": True, "viewer": False},
    ("agent_device", "revoke"): {"admin": True, "developer": False, "viewer": False},
    ("agent_device", "read"): {"admin": True, "developer": True, "viewer": True},
    # audit — every role reads; developer and viewer only their own project, which the
    # derived roles express and the resource below satisfies.
    ("audit", "read"): {"admin": True, "developer": True, "viewer": True},
}

#: The resource instance every check above is made against: owned by the developer with
#: the viewer as a member, so "own/member" is satisfied for both.
RESOURCE_ATTR: dict[str, Any] = {
    "owner_id": DEVELOPER_ID,
    "member_ids": [DEVELOPER_ID, VIEWER_ID],
    "created_by": DEVELOPER_ID,
}

#: A fourth identity that appears in no principal. `change_set` resources in the matrix
#: are authored by them, because the self-approval bar applies to EVERY role — a
#: change-set authored by the developer would make the developer's `approve` cell a deny
#: and the admin's too if it were authored by the admin. Approval of somebody else's
#: change-set and the self-approval bar are two different statements, so they get two
#: different resources.
CHANGE_SET_AUTHOR = "44444444-4444-4444-4444-444444444444"


def _cerbos_url() -> str:
    url = os.environ.get(CERBOS_URL_ENV, "").strip()
    if not url:
        require_capability(
            "cerbos",
            f"{CERBOS_URL_ENV} is not set; this module needs the digest-pinned Cerbos "
            "sidecar (`docker compose up -d cerbos`, or the backend job's service)",
        )
    return url.rstrip("/")


@pytest.fixture(scope="module")
def cerbos_url() -> str:
    """The sidecar's base URL, probed once with a blocking request.

    Synchronous and module-scoped on purpose. An `httpx.AsyncClient` created in a
    module-scoped async fixture binds its connection pool to the event loop that built
    it, and pytest-asyncio gives each test a fresh loop — so every test after the first
    fails with `WriteTimeout`, which reads exactly like a Cerbos outage and is not one.
    Probing here means an unreachable sidecar is reported once, before 80-odd
    parametrised cases each rediscover it.
    """
    import urllib.error
    import urllib.request

    from src.auth.cerbos import HEALTH_PATH

    url = _cerbos_url()
    try:
        with urllib.request.urlopen(f"{url}{HEALTH_PATH}", timeout=10) as response:  # noqa: S310 - fixed loopback URL
            assert response.status == 200, response.status
    except (urllib.error.URLError, OSError) as exc:
        pytest.fail(
            f"{CERBOS_URL_ENV}={url} but the sidecar did not answer {HEALTH_PATH}: {exc}. "
            "Start it with `docker compose up -d cerbos`."
        )
    return url


@pytest_asyncio.fixture()
async def cerbos_client(cerbos_url: str) -> AsyncIterator[Any]:
    """The PRODUCTION client, not a test double, pointed at the real sidecar."""
    from src.auth.cerbos import CerbosClient

    async with httpx.AsyncClient(timeout=10.0) as http:
        yield CerbosClient(cerbos_url, http=http)


def _principal(role: str) -> Any:
    from src.auth.cerbos import CerbosPrincipal

    ids = {"admin": ADMIN_ID, "developer": DEVELOPER_ID, "viewer": VIEWER_ID}
    return CerbosPrincipal(id=ids[role], roles=(role,), attr={"kind": "user", "blast_radius": "workspace"})


def _resource(kind: str) -> Any:
    from src.auth.cerbos import CerbosResource

    attr = dict(RESOURCE_ATTR)
    if kind == "change_set":
        attr["created_by"] = CHANGE_SET_AUTHOR
    return CerbosResource(kind=kind, id=f"{kind}-1", attr=attr)


class TestTheSidecarIsTheOnePinnedInCompose:
    async def test_health_answers(self, cerbos_client: Any) -> None:
        await cerbos_client.health()

    async def test_the_matrix_table_is_not_empty(self) -> None:
        """The vacuity guard. A parametrised suite over an empty table is 0 tests and a
        green run, which is the failure mode §0.4.5 exists to close."""
        assert len(MATRIX) >= 24, len(MATRIX)
        kinds = {kind for kind, _ in MATRIX}
        assert kinds == {"project", "change_set", "policy", "secret", "agent_device", "audit"}, kinds


@pytest.mark.parametrize(
    ("kind", "action", "role", "expected"),
    [
        pytest.param(kind, action, role, expected, id=f"{role}-{action}-{kind}")
        for (kind, action), per_role in MATRIX.items()
        for role, expected in per_role.items()
    ],
)
class TestTheProductionClientAgainstTheRealPolicySet:
    async def test_the_decision_matches_section_11_2(
        self, cerbos_client: Any, kind: str, action: str, role: str, expected: bool
    ) -> None:
        allowed = await cerbos_client.is_allowed(
            principal=_principal(role),
            resource=_resource(kind),
            action=action,
        )
        verb = "allow" if expected else "deny"
        assert allowed is expected, f"§11.2 says {role} must be {verb}ed {action} on {kind}; Cerbos said {allowed}"


class TestTheRulesThatAdmitNoException:
    """The three §11.2 rules that are easy to lose in a refactor, asserted directly."""

    @pytest.mark.parametrize("role", ["admin", "developer", "viewer"])
    async def test_no_role_can_read_a_secret_value(self, cerbos_client: Any, role: str) -> None:
        """ "Not even admin." A reveal endpoint would turn the vault into a distribution
        channel, so the policy denies the action rather than relying on no route existing."""
        assert not await cerbos_client.is_allowed(
            principal=_principal(role), resource=_resource("secret"), action="read_value"
        )

    async def test_a_viewer_who_owns_the_project_still_cannot_update_it(self, cerbos_client: Any) -> None:
        """The derived roles list all three coarse roles as parents so a viewer can be a
        member for audit reads — which means `update` keyed on derived roles alone would
        hand an owning viewer the update. The explicit DENY is what closes it."""
        from src.auth.cerbos import CerbosPrincipal, CerbosResource

        owned_by_viewer = CerbosResource(
            kind="project", id="project-2", attr={"owner_id": VIEWER_ID, "member_ids": [VIEWER_ID]}
        )
        assert not await cerbos_client.is_allowed(
            principal=CerbosPrincipal(id=VIEWER_ID, roles=("viewer",)),
            resource=owned_by_viewer,
            action="update",
        )

    async def test_self_approval_is_barred_by_policy(self, cerbos_client: Any) -> None:
        """§11.2: "a developer ... may not approve their own change-set for a
        `prod`-scoped policy". The attribute carries who created it and what it touches;
        the policy decides."""
        from src.auth.cerbos import CerbosPrincipal, CerbosResource

        own_prod_change_set = CerbosResource(
            kind="change_set",
            id="change-set-prod-1",
            attr={"created_by": DEVELOPER_ID, "owner_id": DEVELOPER_ID, "member_ids": [DEVELOPER_ID], "scope": "prod"},
        )
        assert not await cerbos_client.is_allowed(
            principal=CerbosPrincipal(id=DEVELOPER_ID, roles=("developer",)),
            resource=own_prod_change_set,
            action="approve",
        )

    async def test_an_action_no_policy_mentions_is_denied(self, cerbos_client: Any) -> None:
        """Deny-by-default at the last layer. A typo in an action string must not become
        an allow because no rule matched it."""
        assert not await cerbos_client.is_allowed(
            principal=_principal("admin"), resource=_resource("project"), action="exfiltrate"
        )

    async def test_an_unknown_resource_kind_is_denied(self, cerbos_client: Any) -> None:
        from src.auth.cerbos import CerbosResource

        assert not await cerbos_client.is_allowed(
            principal=_principal("admin"),
            resource=CerbosResource(kind="not_a_modelled_resource", id="x"),
            action="read",
        )


class TestRequirePermissionMapsDecisionsOntoHttp:
    """The other half: `require_permission` over the real client, real statuses."""

    async def test_a_deny_is_403_with_the_fixed_body(self, cerbos_client: Any) -> None:
        from src.auth.cerbos import CerbosResource
        from src.auth.dependencies import require_permission
        from src.auth.models import UserRole
        from src.auth.principal import Principal
        from src.core.errors import FORBIDDEN_DETAIL, ProblemException

        request = _fake_request(cerbos_client)
        principal = Principal.for_user(
            user_id=uuid.UUID(VIEWER_ID), subject="test-only-viewer", email="v@forgeops.invalid", role=UserRole.VIEWER
        )
        with pytest.raises(ProblemException) as caught:
            await require_permission(
                request, principal, resource=CerbosResource(kind="project", id="p"), action="delete"
            )
        assert caught.value.problem.status == 403
        assert caught.value.problem.detail == FORBIDDEN_DETAIL

    async def test_an_allow_returns_none(self, cerbos_client: Any) -> None:
        from src.auth.cerbos import CerbosResource
        from src.auth.dependencies import require_permission
        from src.auth.models import UserRole
        from src.auth.principal import Principal

        request = _fake_request(cerbos_client)
        principal = Principal.for_user(
            user_id=uuid.UUID(ADMIN_ID), subject="test-only-admin", email="a@forgeops.invalid", role=UserRole.ADMIN
        )
        assert (
            await require_permission(
                request, principal, resource=CerbosResource(kind="project", id="p"), action="delete"
            )
            is None
        )

    async def test_the_403_body_is_identical_for_a_resource_that_does_not_exist(self, cerbos_client: Any) -> None:
        """§4.2 and Q-20: the shape must not be an enumeration oracle. Two denies, one
        over a resource with real attributes and one over a bare id, must be byte-equal."""
        import orjson
        from src.auth.cerbos import CerbosResource
        from src.auth.dependencies import require_permission
        from src.auth.models import UserRole
        from src.auth.principal import Principal
        from src.core.errors import ProblemException

        request = _fake_request(cerbos_client)
        principal = Principal.for_user(
            user_id=uuid.UUID(VIEWER_ID), subject="test-only-viewer", email="v@forgeops.invalid", role=UserRole.VIEWER
        )
        bodies = []
        for resource in (
            CerbosResource(kind="project", id="project-1", attr=dict(RESOURCE_ATTR)),
            CerbosResource(kind="project", id="does-not-exist-" + uuid.uuid4().hex),
        ):
            with pytest.raises(ProblemException) as caught:
                await require_permission(request, principal, resource=resource, action="delete")
            payload = caught.value.problem.model_dump()
            payload.pop("instance", None)
            payload.pop("trace_id", None)
            bodies.append(orjson.dumps(payload, option=orjson.OPT_SORT_KEYS))
        assert bodies[0] == bodies[1], bodies

    async def test_an_outage_is_503_authorization_unavailable_not_403(self) -> None:
        """D-56. Proved against a real closed port, so the transport failure is real."""
        import socket
        from contextlib import closing

        from src.auth.cerbos import CerbosClient, CerbosResource
        from src.auth.dependencies import require_permission
        from src.auth.models import UserRole
        from src.auth.principal import Principal
        from src.core.errors import ProblemException

        with closing(socket.socket()) as probe:
            probe.bind(("127.0.0.1", 0))
            closed_port = probe.getsockname()[1]

        async with httpx.AsyncClient(timeout=2.0) as http:
            request = _fake_request(CerbosClient(f"http://127.0.0.1:{closed_port}", http=http))
            principal = Principal.for_user(
                user_id=uuid.UUID(ADMIN_ID), subject="test-only-admin", email="a@forgeops.invalid", role=UserRole.ADMIN
            )
            with pytest.raises(ProblemException) as caught:
                await require_permission(
                    request, principal, resource=CerbosResource(kind="project", id="p"), action="read"
                )
        assert caught.value.problem.status == 503
        assert caught.value.problem.type.endswith("/authorization-unavailable")

    async def test_a_missing_composition_is_a_runtime_error_not_a_403(self) -> None:
        """A wiring bug must not look like a correctly-enforced authorisation layer."""
        from src.auth.cerbos import CerbosResource
        from src.auth.dependencies import require_permission
        from src.auth.models import UserRole
        from src.auth.principal import Principal

        request = _fake_request(None)
        principal = Principal.for_user(
            user_id=uuid.UUID(ADMIN_ID), subject="test-only-admin", email="a@forgeops.invalid", role=UserRole.ADMIN
        )
        with pytest.raises(RuntimeError, match="app.state.cerbos"):
            await require_permission(request, principal, resource=CerbosResource(kind="project", id="p"), action="read")


def _fake_request(cerbos: Any) -> Any:
    """A real Starlette `Request` whose app state carries the client.

    Built from a real ASGI scope rather than substituted: `require_permission` reads
    `request.app.state.cerbos`, and a double for `Request` would be a double for a
    framework object, which §0.4.1 forbids in the integration suite.
    """
    from starlette.applications import Starlette
    from starlette.requests import Request

    app = Starlette()
    if cerbos is not None:
        app.state.cerbos = cerbos
    return Request({"type": "http", "method": "GET", "path": "/", "headers": [], "app": app})
