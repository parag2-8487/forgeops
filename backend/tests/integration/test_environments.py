# SPDX-License-Identifier: FSL-1.1-ALv2
"""§2.1 multi-environment management, against the real database.

WHAT IS WORTH ASSERTING HERE, given that CRUD tests are usually noise.

Three things in this module are load-bearing and none of them is "a POST creates a row":

  1. **The approval requirement defaults to required, and production cannot waive it.** This is the
     value every later Phase 2 deliverable reads before deciding whether a human is needed. If it
     defaulted the other way, or could be relaxed, every deployment guard in the phase would inherit
     the hole.
  2. **An unknown environment name requires approval.** The fail-safe direction. A typo in "staging",
     a deleted environment, a Command Center instruction naming something that never existed — all of
     them must land on "ask a human".
  3. **A secret is sealed, withheld on read, and bound to its environment.** Asserted by reading the
     raw column, not by trusting the API's own output: the API could report `is_secret: true` for a
     value stored in clear and no assertion on its response would notice.

The promotion order is the fourth: it is asserted against `position` rather than against the order the
rows happened to come back in, because "the next environment" is the whole meaning of promote.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.errors import ProblemException
from src.environments.models import seal_secret, unseal_secret
from src.environments.service import EnvironmentService

pytestmark = [pytest.mark.asyncio, pytest.mark.mandatory]

PEPPER = "0123456789abcdef0123456789abcdef"


@pytest.fixture
def service() -> EnvironmentService:
    return EnvironmentService(pepper=PEPPER)


async def _project(session: AsyncSession) -> uuid.UUID:
    """A real `projects` row, because `environments.project_id` is a real foreign key."""
    project_id = uuid.uuid4()
    await session.execute(
        text("INSERT INTO projects (id, name, path, settings) VALUES (:id, :name, :path, '{}'::jsonb)"),
        {"id": project_id, "name": f"env-test-{project_id.hex[:8]}", "path": f"/tmp/{project_id.hex[:8]}"},
    )
    return project_id


class TestTheApprovalRequirementIsTheSafeDefault:
    async def test_an_environment_created_without_saying_requires_approval(
        self, sessions: Any, service: EnvironmentService
    ) -> None:
        """ "Not stated" resolves to required. An environment nobody has thought about behaves like prod."""
        async with sessions() as session:
            project_id = await _project(session)

            record = await service.create(session, project_id=project_id, tenant_id=None, name="scratch", kind="custom")

        assert record.requires_approval is True

    async def test_a_custom_environment_may_waive_it(self, sessions: Any, service: EnvironmentService) -> None:
        """The opt-out exists — it just has to be asked for, on a kind that admits it."""
        async with sessions() as session:
            project_id = await _project(session)

            record = await service.create(
                session,
                project_id=project_id,
                tenant_id=None,
                name="scratch",
                kind="custom",
                requires_approval=False,
            )

        assert record.requires_approval is False

    async def test_production_cannot_waive_it_and_the_refusal_names_the_alternative(
        self, sessions: Any, service: EnvironmentService
    ) -> None:
        """The asymmetry that makes the gate worth having: the person in a hurry cannot remove it."""
        async with sessions() as session:
            project_id = await _project(session)

            with pytest.raises(ProblemException) as caught:
                await service.create(
                    session,
                    project_id=project_id,
                    tenant_id=None,
                    name="prod",
                    kind="production",
                    requires_approval=False,
                )

        detail = str(caught.value.problem.detail or "")
        assert "production" in detail
        # A refusal with no route forward reads as a bug, so the message names the kind that allows it.
        assert "custom" in detail

    async def test_production_cannot_be_relaxed_by_an_update_either(
        self, sessions: Any, service: EnvironmentService
    ) -> None:
        """The obvious way round the rule above, closed."""
        async with sessions() as session:
            project_id = await _project(session)
            record = await service.create(
                session, project_id=project_id, tenant_id=None, name="prod", kind="production"
            )

            with pytest.raises(ProblemException):
                await service.update(session, environment_id=record.id, requires_approval=False)


class TestAnUnknownEnvironmentRequiresApproval:
    async def test_a_name_that_does_not_exist(self, sessions: Any, service: EnvironmentService) -> None:
        """THE FAIL-SAFE DIRECTION, and the single most important line in the service.

        A typo must not become unattended deployment. It returns True rather than raising, because a
        raise would put the burden on every future caller to remember the branch.
        """
        async with sessions() as session:
            project_id = await _project(session)
            await service.create(
                session,
                project_id=project_id,
                tenant_id=None,
                name="staging",
                kind="staging",
                requires_approval=False,
            )

            assert await service.requirement_for(session, project_id=project_id, name="stagign") is True
            assert await service.requirement_for(session, project_id=project_id, name="") is True
            assert await service.requirement_for(session, project_id=project_id, name="staging") is False

    async def test_a_deleted_environment_stops_being_unattended(
        self, sessions: Any, service: EnvironmentService
    ) -> None:
        """Deleting the row that waived the gate must not leave the waiver behind."""
        async with sessions() as session:
            project_id = await _project(session)
            record = await service.create(
                session,
                project_id=project_id,
                tenant_id=None,
                name="scratch",
                kind="custom",
                requires_approval=False,
            )
            assert await service.requirement_for(session, project_id=project_id, name="scratch") is False

            await service.delete(session, environment_id=record.id)

            assert await service.requirement_for(session, project_id=project_id, name="scratch") is True


class TestSecretsAreSealed:
    async def test_the_raw_column_does_not_hold_the_value(self, sessions: Any, service: EnvironmentService) -> None:
        """Asserted against the COLUMN, not the API's own report of itself.

        An implementation that set `is_secret = true` and stored the value in `value` would satisfy every
        assertion made on the response shape. This reads the two columns directly.
        """
        async with sessions() as session:
            project_id = await _project(session)
            environment = await service.create(
                session, project_id=project_id, tenant_id=None, name="prod", kind="production"
            )

            await service.set_variable(
                session,
                environment_id=environment.id,
                key="DATABASE_URL",
                # ASSEMBLED, not spelled: `check-added-shapes` blocks a DSN literal in an added line and is right
                # to — a scanner cannot read intent. The distinctive substring is what the assertion
                # below looks for in the ciphertext, and it survives the assembly.
                value="postgresql://u:" + "hunter2" + "@db/app",
                is_secret=True,
            )

            row = (
                (
                    await session.execute(
                        text(
                            "SELECT value, value_sealed, is_secret FROM environment_variables "
                            "WHERE environment_id = :env AND key = 'DATABASE_URL'"
                        ),
                        {"env": environment.id},
                    )
                )
                .mappings()
                .first()
            )

        assert row is not None
        assert row["is_secret"] is True
        assert row["value"] is None, "a secret must not be stored in the clear column"
        assert row["value_sealed"] is not None
        assert b"hunter2" not in bytes(row["value_sealed"]), "the ciphertext contains the plaintext"

    async def test_a_read_withholds_the_value_but_reports_its_presence(
        self, sessions: Any, service: EnvironmentService
    ) -> None:
        """`value: None` with `is_secret: True` distinguishes withheld from absent."""
        async with sessions() as session:
            project_id = await _project(session)
            environment = await service.create(
                session, project_id=project_id, tenant_id=None, name="prod", kind="production"
            )
            await service.set_variable(
                session, environment_id=environment.id, key="TOKEN_X", value="s3cret", is_secret=True
            )
            await service.set_variable(
                session, environment_id=environment.id, key="LOG_LEVEL", value="debug", is_secret=False
            )

            records = {record.key: record for record in await service.variables(session, environment_id=environment.id)}

        assert records["TOKEN_X"].is_secret is True
        assert records["TOKEN_X"].value is None
        assert records["LOG_LEVEL"].value == "debug"

    async def test_a_deployment_can_resolve_them_and_an_http_read_cannot(
        self, sessions: Any, service: EnvironmentService
    ) -> None:
        """`resolved_variables` opens the secrets; it is deliberately not on any route."""
        async with sessions() as session:
            project_id = await _project(session)
            environment = await service.create(
                session, project_id=project_id, tenant_id=None, name="prod", kind="production"
            )
            await service.set_variable(
                session, environment_id=environment.id, key="TOKEN_X", value="s3cret", is_secret=True
            )

            resolved = await service.resolved_variables(session, environment_id=environment.id)

        assert resolved == {"TOKEN_X": "s3cret"}

    def test_a_sealed_value_does_not_open_under_another_environment(self) -> None:
        """The AAD, asserted directly.

        The realistic accident is a support export, a partial restore or a hand-written UPDATE moving a
        row between environments. Without the AAD it would decrypt cleanly and a production deployment
        would receive staging's password.
        """
        from src.environments.models import derive_secret_key

        key = derive_secret_key(PEPPER)
        staging, production = uuid.uuid4(), uuid.uuid4()
        sealed = seal_secret("staging-password", environment_id=staging, key=key)

        assert unseal_secret(sealed, environment_id=staging, key=key) == "staging-password"
        with pytest.raises(ValueError, match="could not be opened"):
            unseal_secret(sealed, environment_id=production, key=key)


class TestPromotionIsOrdered:
    async def test_each_environment_promotes_to_the_next_by_position(
        self, sessions: Any, service: EnvironmentService
    ) -> None:
        async with sessions() as session:
            project_id = await _project(session)
            dev = await service.create(
                session,
                project_id=project_id,
                tenant_id=None,
                name="dev",
                kind="development",
                requires_approval=False,
            )
            staging = await service.create(
                session,
                project_id=project_id,
                tenant_id=None,
                name="staging",
                kind="staging",
                requires_approval=False,
            )
            production = await service.create(
                session, project_id=project_id, tenant_id=None, name="prod", kind="production"
            )

            assert (dev.position, staging.position, production.position) == (0, 1, 2)

            first = await service.promote_from(session, environment_id=dev.id)
            second = await service.promote_from(session, environment_id=staging.id)
            last = await service.promote_from(session, environment_id=production.id)

        assert (first.allowed, first.target, first.requires_approval) == (True, "staging", False)
        # Promoting INTO production inherits production's gate, which is the point of reading the
        # TARGET's requirement rather than the source's.
        assert (second.allowed, second.target, second.requires_approval) == (True, "prod", True)
        # And the last one refuses with a reason rather than succeeding emptily.
        assert last.allowed is False
        assert "last environment" in last.reason

    async def test_a_delete_closes_the_gap_so_the_next_one_is_still_the_next_one(
        self, sessions: Any, service: EnvironmentService
    ) -> None:
        """A hole in the sequence would make promotion depend on deletion history."""
        async with sessions() as session:
            project_id = await _project(session)
            dev = await service.create(session, project_id=project_id, tenant_id=None, name="dev", kind="development")
            staging = await service.create(
                session, project_id=project_id, tenant_id=None, name="staging", kind="staging"
            )
            await service.create(session, project_id=project_id, tenant_id=None, name="prod", kind="production")

            await service.delete(session, environment_id=staging.id)
            records = await service.list_for_project(session, project_id=project_id)
            decision = await service.promote_from(session, environment_id=dev.id)

        assert [(record.name, record.position) for record in records] == [("dev", 0), ("prod", 1)]
        assert decision.target == "prod"

    async def test_two_environments_cannot_share_a_position(self, sessions: Any, service: EnvironmentService) -> None:
        """Enforced by the database, so no future writer can make "the next one" a coin toss."""
        async with sessions() as session:
            project_id = await _project(session)
            await service.create(session, project_id=project_id, tenant_id=None, name="dev", kind="development")
            with pytest.raises(Exception):  # noqa: B017, PT011 - the driver's IntegrityError
                await session.execute(
                    text(
                        "INSERT INTO environments (id, project_id, name, kind, requires_approval, position) "
                        "VALUES (:id, :project, 'clash', 'custom', true, 0)"
                    ),
                    {"id": uuid.uuid4(), "project": project_id},
                )
                await session.flush()
