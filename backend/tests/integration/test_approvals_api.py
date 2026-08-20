# SPDX-License-Identifier: FSL-1.1-ALv2
"""The approvals surface is mounted, authenticated, and speaks the stored vocabulary.

This file exists because the previous version of `src/approvals/` was a working-looking module that
could not have worked: an in-process dict, an uppercase status vocabulary Postgres would have
refused, and a caller-supplied approver defaulting to `admin`. Each test below pins one of those
defects shut, so a future edit that reintroduces it fails here rather than in a demo.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from src.auth.dependencies import require_principal
from src.auth.models import UserRole
from src.auth.principal import Principal
from src.governance.chokepoint import GovernanceAction
from src.governance.models import CHANGE_SET_STATUSES, CHANGE_SET_TRANSITIONS

from tests.integration.production_app import apply_committed_baseline_env

pytestmark = [pytest.mark.asyncio, pytest.mark.mandatory]

TENANT = uuid.UUID("11111111-1111-1111-1111-111111111111")
USER = uuid.UUID("22222222-2222-2222-2222-222222222222")

#: Every route the approvals router publishes, with a method that reaches it.
APPROVAL_ROUTES: tuple[tuple[str, str], ...] = (
    ("GET", "/api/v1/approvals"),
    ("GET", f"/api/v1/approvals/{uuid.uuid4()}"),
    ("POST", f"/api/v1/approvals/{uuid.uuid4()}/approve"),
    ("POST", f"/api/v1/approvals/{uuid.uuid4()}/reject"),
    ("POST", f"/api/v1/approvals/{uuid.uuid4()}/revert"),
)


def _principal() -> Principal:
    """A real `Principal`, not a `CerbosPrincipal`.

    The routes read `principal.tenant_id` for row scoping and `principal.user_id` for the approver,
    so a stand-in without those fields would make the test pass while proving nothing about either.
    """
    return Principal.for_user(
        user_id=USER,
        subject="test-subject",
        email="reviewer@example.invalid",
        role=UserRole.ADMIN,
        tenant_id=TENANT,
    )


@pytest_asyncio.fixture
async def unauthenticated_app(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Any]:
    """The real app with NO dependency override, so the auth layer is live."""
    from src.main import create_app

    apply_committed_baseline_env(monkeypatch)
    monkeypatch.setenv("APP_ENV", "test")
    app = create_app()
    async with LifespanManager(app):
        yield app


@pytest_asyncio.fixture
async def authed_app(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Any]:
    from src.main import create_app

    apply_committed_baseline_env(monkeypatch)
    monkeypatch.setenv("APP_ENV", "test")
    app = create_app()
    app.dependency_overrides[require_principal] = _principal
    async with LifespanManager(app):
        yield app
    app.dependency_overrides.clear()


async def _client(app: Any) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")


class TestItIsMountedAtAll:
    """The router was absent from `create_app` for the whole of Phase 1."""

    async def test_every_approval_route_is_registered(self, authed_app: Any) -> None:
        spec = authed_app.openapi()
        registered = {
            (method.upper(), path)
            for path, ops in spec["paths"].items()
            for method in ops
            if "approvals" in path
        }
        # Compared against the templated paths the schema uses, not the concrete uuids above.
        assert registered == {
            ("GET", "/api/v1/approvals"),
            ("GET", "/api/v1/approvals/{change_set_id}"),
            ("POST", "/api/v1/approvals/{change_set_id}/approve"),
            ("POST", "/api/v1/approvals/{change_set_id}/reject"),
            ("POST", "/api/v1/approvals/{change_set_id}/revert"),
        }


class TestDenyByDefault:
    """Mounting this router was a security question, so the answer is asserted per route."""

    @pytest.mark.parametrize(("method", "path"), APPROVAL_ROUTES)
    async def test_route_refuses_an_unauthenticated_caller(
        self, unauthenticated_app: Any, method: str, path: str
    ) -> None:
        client = await _client(unauthenticated_app)
        async with client:
            response = await client.request(method, path, json={})
        # 401 specifically: not 404 (which would mean unmounted) and not 200.
        assert response.status_code == 401, f"{method} {path} answered {response.status_code}"

    async def test_no_approval_route_is_listed_public(self) -> None:
        from src.auth.public_routes import is_public

        for method, path in APPROVAL_ROUTES:
            assert not is_public(path, method), f"{method} {path} must not be public"


class TestTheApproverCannotBeSuppliedByTheCaller:
    """The sharpest defect of the old surface: `approver: str = 'admin'` in the query string."""

    async def test_approve_accepts_no_approver_parameter(self, authed_app: Any) -> None:
        spec = authed_app.openapi()
        operation = spec["paths"]["/api/v1/approvals/{change_set_id}/approve"]["post"]
        parameter_names = {p["name"] for p in operation.get("parameters", [])}
        # `change_set_id` is the only parameter. An `approver` or `rejector` here would mean the
        # identity on the audit record is whatever the caller typed.
        assert parameter_names == {"change_set_id"}
        assert "approver" not in parameter_names
        assert "rejector" not in parameter_names

    async def test_the_request_body_has_no_identity_field(self, authed_app: Any) -> None:
        spec = authed_app.openapi()
        schema = spec["components"]["schemas"]["ApprovalDecisionRequest"]
        assert set(schema["properties"]) == {"comment", "expected_version"}

    def test_the_decision_body_type_declares_no_approver(self) -> None:
        from src.approvals.schemas import ApprovalDecisionRequest

        assert "approver" not in ApprovalDecisionRequest.model_fields
        assert "rejector" not in ApprovalDecisionRequest.model_fields


class TestOneStatusVocabulary:
    """Two vocabularies existed for one concept; only one was enforced by the database."""

    def test_the_uppercase_enum_is_gone(self) -> None:
        import src.approvals.schemas as schemas

        # `ApprovalStatus.PENDING` and friends were not in CHANGE_SET_STATUSES, so a row carrying
        # one would have violated `ck_change_sets_status_allowed` from revision 0010.
        assert not hasattr(schemas, "ApprovalStatus")

    def test_the_status_filter_is_generated_from_the_stored_vocabulary(self) -> None:
        from src.approvals.routes import ChangeSetStatusFilter

        assert {member.value for member in ChangeSetStatusFilter} == set(CHANGE_SET_STATUSES)

    async def test_the_schema_advertises_exactly_the_stored_states(self, authed_app: Any) -> None:
        spec = authed_app.openapi()
        parameters = spec["paths"]["/api/v1/approvals"]["get"]["parameters"]
        status_param = next(p for p in parameters if p["name"] == "status")
        # Chase the anyOf/$ref that FastAPI emits for an optional enum.
        rendered = str(status_param)
        enum_schema = next(
            s for name, s in spec["components"]["schemas"].items() if name == "ChangeSetStatusFilter"
        )
        assert set(enum_schema["enum"]) == set(CHANGE_SET_STATUSES)
        assert "status" in rendered


class TestTheRejectEdgeExists:
    """§3.6 defines `pending_approval → rejected` and nothing implemented it."""

    def test_the_chokepoint_owns_a_reject_transition(self) -> None:
        from src.governance.chokepoint import GovernanceChokepoint

        assert hasattr(GovernanceChokepoint, "reject")

    def test_the_edge_it_implements_is_in_the_state_machine(self) -> None:
        assert ("pending_approval", "rejected") in CHANGE_SET_TRANSITIONS

    def test_a_rejection_has_its_own_audit_action(self) -> None:
        # Without this, a refusal would either go unlogged or be logged as something it is not.
        assert GovernanceAction.CHANGE_SET_REJECTED.value == "change_set_rejected"
        assert GovernanceAction.CHANGE_SET_REJECTED is not GovernanceAction.CHANGE_SET_APPROVED

    def test_revert_is_named_after_the_edge_it_takes(self) -> None:
        # The old handler was `rollback`, which matches no edge out of `applied`. `rolled_back` is
        # a different state, reached from `applying` when an apply fails.
        assert ("applied", "reverted") in CHANGE_SET_TRANSITIONS
        assert ("applied", "rolled_back") not in CHANGE_SET_TRANSITIONS


class TestTheStoreIsNotADictionary:
    def test_the_service_holds_no_per_process_state(self) -> None:
        from src.approvals.service import ApprovalService

        service = ApprovalService()
        # The old service carried `_store`. Any attribute holding change sets in memory would make
        # two workers disagree and would lose everything on restart.
        assert not hasattr(service, "_store")
        assert service.__dict__ == {}

    def test_every_read_requires_a_session(self) -> None:
        import inspect

        from src.approvals.service import ApprovalService

        for name in ("list_change_sets", "get_change_set"):
            signature = inspect.signature(getattr(ApprovalService, name))
            assert "session" in signature.parameters, f"{name} must take the caller's session"

    def test_writes_are_not_reimplemented_here(self) -> None:
        from src.approvals.service import ApprovalService

        # Approve, reject and revert live on the chokepoint. A second implementation of a §3.6
        # transition would be a second state machine, and only one of them would be asserted by Q-22.
        for forbidden in ("approve_changeset", "reject_changeset", "rollback_changeset", "create_changeset"):
            assert not hasattr(ApprovalService, forbidden)


class TestPaginationIsKeyset:
    def test_a_cursor_round_trips(self) -> None:
        from datetime import UTC, datetime

        from src.approvals.service import decode_cursor, encode_cursor

        now = datetime(2026, 8, 21, 4, 30, tzinfo=UTC)
        change_set_id = uuid.uuid4()
        timestamp, decoded_id = decode_cursor(encode_cursor(now, change_set_id))
        assert decoded_id == change_set_id
        assert timestamp.startswith("2026-08-21T04:30")

    @pytest.mark.parametrize("bad", ["", "no-separator", "|", "2026-08-21T00:00:00+00:00|"])
    def test_a_malformed_cursor_is_rejected_rather_than_ignored(self, bad: str) -> None:
        from src.approvals.service import decode_cursor

        # Silently treating a bad cursor as "start again" would send a paging client round page
        # one forever, which looks like data rather than like an error.
        with pytest.raises(ValueError):
            decode_cursor(bad)
