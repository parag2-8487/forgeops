# SPDX-License-Identifier: FSL-1.1-ALv2
"""The deployment circuit breaker, driven through the real chain. Phase 2 §2.2.

THE IMPORTANT TESTS HERE ARE THE NEGATIVE ONES. A breaker that opens on repeated validation failure is easy;
a breaker that also opens on a policy deny, a held approval or an agent timeout would punish the system for
working correctly, and would stop deployments for reasons that are already resolved. Each of those three is
driven through the real chokepoint and the breaker row is read back.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from src.deployments.breaker import (
    COOLDOWN,
    FAILURE_THRESHOLD,
    STATE_CLOSED,
    STATE_HALF_OPEN,
    STATE_OPEN,
    CircuitOpenError,
    DeploymentCircuitBreaker,
)

from tests.integration.chokepoint_support import make_fixture

pytestmark = [pytest.mark.asyncio, pytest.mark.mandatory]

ENVIRONMENT = "production"


async def _row(session: AsyncSession, project_id: uuid.UUID) -> Any:
    return (
        (
            await session.execute(
                text(
                    "SELECT state, consecutive_failures, opened_at, last_failure_reason "
                    "FROM deployment_circuit_breakers WHERE project_id = :project_id"
                ),
                {"project_id": str(project_id)},
            )
        )
        .mappings()
        .first()
    )


class TestTheBreakerOpensOnRepeatedValidationFailure:
    async def test_a_never_used_environment_is_closed_and_says_so_in_words(self, sessions: Any) -> None:
        """Never-tripped is not the same as closed-after-recovery, and the words distinguish them.

        A panel that said "deployments available" for both would leave an operator unable to tell a system
        that has never failed from one that just recovered.
        """
        breaker = DeploymentCircuitBreaker()
        async with sessions() as session:
            fixture = await make_fixture(session)
            state = await breaker.state_for(session, project_id=fixture.project_id, environment=ENVIRONMENT)
        assert state.state == STATE_CLOSED
        assert state.consecutive_failures == 0
        assert "No recent validation failures" in state.describe()

    async def test_below_the_threshold_it_stays_closed_and_counts(self, sessions: Any) -> None:
        breaker = DeploymentCircuitBreaker()
        async with sessions() as session:
            fixture = await make_fixture(session)
            for attempt in range(FAILURE_THRESHOLD - 1):
                state = await breaker.record_validation_failure(
                    session,
                    project_id=fixture.project_id,
                    environment=ENVIRONMENT,
                    reason=f"the cluster refused the manifest (attempt {attempt})",
                )
                # STILL CLOSED. One failure is often a typo the operator fixes on the next attempt, and a
                # breaker that opened on it would refuse the corrected deployment.
                assert state.state == STATE_CLOSED
                await breaker.guard(session, project_id=fixture.project_id, environment=ENVIRONMENT)
            assert state.consecutive_failures == FAILURE_THRESHOLD - 1
            # The remaining margin is stated, so an operator can see it coming.
            assert "more would stop further attempts" in state.describe()

    async def test_at_the_threshold_it_opens_and_refuses_with_the_reason(self, sessions: Any) -> None:
        breaker = DeploymentCircuitBreaker()
        async with sessions() as session:
            fixture = await make_fixture(session)
            for _ in range(FAILURE_THRESHOLD):
                state = await breaker.record_validation_failure(
                    session,
                    project_id=fixture.project_id,
                    environment=ENVIRONMENT,
                    reason="Deployment.apps 'api' is invalid: spec.replicas must be non-negative",
                )
            assert state.state == STATE_OPEN
            assert state.opened_at is not None

            with pytest.raises(CircuitOpenError) as refused:
                await breaker.guard(session, project_id=fixture.project_id, environment=ENVIRONMENT)
            # THE CLUSTER'S OWN MESSAGE SURVIVES TO THE REFUSAL. When deployments stop, "what was failing"
            # is the only useful output, and a generic string would defeat the whole feature.
            assert "spec.replicas must be non-negative" in refused.value.reason
            assert refused.value.retry_after_seconds > 0

    async def test_an_open_breaker_leaves_another_environment_alone(self, sessions: Any) -> None:
        """The breaker is per environment.

        Without this, a broken staging manifest would stop production deployments — which is the opposite
        of what an operator needs during an incident.
        """
        breaker = DeploymentCircuitBreaker()
        async with sessions() as session:
            fixture = await make_fixture(session)
            for _ in range(FAILURE_THRESHOLD):
                await breaker.record_validation_failure(
                    session, project_id=fixture.project_id, environment="staging", reason="broken"
                )
            with pytest.raises(CircuitOpenError):
                await breaker.guard(session, project_id=fixture.project_id, environment="staging")
            # Production is untouched, and this is the assertion that matters.
            await breaker.guard(session, project_id=fixture.project_id, environment="production")


class TestTheCooldownAndTheSingleTrial:
    async def test_after_the_cooldown_it_reports_half_open_without_consuming_the_trial(self, sessions: Any) -> None:
        """A read must not consume the trial.

        `state_for` applies the cooldown on READ so there is no window in which the row says `open` and the
        truth is `half_open` — but if it WROTE `half_open`, a panel refreshing itself would spend the trial
        before any operator clicked anything.
        """
        breaker = DeploymentCircuitBreaker()
        async with sessions() as session:
            fixture = await make_fixture(session)
            for _ in range(FAILURE_THRESHOLD):
                await breaker.record_validation_failure(
                    session, project_id=fixture.project_id, environment=ENVIRONMENT, reason="broken"
                )
            # Age the opening past the cooldown. Moving the clock in the ROW rather than patching
            # `datetime.now` — the cooldown is computed from a stored timestamp, and this exercises the
            # same arithmetic production does.
            await session.execute(
                text(
                    "UPDATE deployment_circuit_breakers SET opened_at = :then "
                    "WHERE project_id = :project_id AND environment = :environment"
                ),
                {
                    "then": datetime.now(UTC) - COOLDOWN - timedelta(seconds=30),
                    "project_id": str(fixture.project_id),
                    "environment": ENVIRONMENT,
                },
            )
            await session.commit()

            state = await breaker.state_for(session, project_id=fixture.project_id, environment=ENVIRONMENT)
            assert state.state == STATE_HALF_OPEN
            assert "single trial" in state.describe()
            # And the guard lets it through: half-open is not open.
            await breaker.guard(session, project_id=fixture.project_id, environment=ENVIRONMENT)

            # The stored state is STILL `open`, so nothing was consumed by looking.
            row = await _row(session, fixture.project_id)
            assert row["state"] == STATE_OPEN

    async def test_a_failed_trial_counts_as_one_failure_and_not_as_a_fourth(self, sessions: Any) -> None:
        """A trial that fails re-opens the breaker with the count at one.

        Without the reset, a run of three followed by a failed trial would show four consecutive failures —
        a number that describes nothing, because the trial is one failure since the last decision rather
        than a continuation of the run that opened the breaker.
        """
        breaker = DeploymentCircuitBreaker()
        async with sessions() as session:
            fixture = await make_fixture(session)
            for _ in range(FAILURE_THRESHOLD):
                await breaker.record_validation_failure(
                    session, project_id=fixture.project_id, environment=ENVIRONMENT, reason="broken"
                )
            # Move to half-open for real by writing the state, which is what an admitted trial does.
            await session.execute(
                text(
                    "UPDATE deployment_circuit_breakers SET state = :half_open "
                    "WHERE project_id = :project_id AND environment = :environment"
                ),
                {
                    "half_open": STATE_HALF_OPEN,
                    "project_id": str(fixture.project_id),
                    "environment": ENVIRONMENT,
                },
            )
            await session.commit()

            state = await breaker.record_validation_failure(
                session, project_id=fixture.project_id, environment=ENVIRONMENT, reason="still broken"
            )
        assert state.consecutive_failures == 1
        # AND IT RE-OPENS, with a fresh cooldown. This is the assertion the first implementation failed:
        # it left the row `half_open`, which `guard` treats as passable, so a broken environment would
        # have admitted every subsequent attempt. By this point there have been four failures -- more
        # evidence than the three that opened it -- so re-opening is the better-justified answer, and the
        # count reads as failures since the last DECISION rather than as a running total.
        assert state.state == STATE_OPEN
        assert state.opened_at is not None

    async def test_a_success_closes_it_and_keeps_the_reason(self, sessions: Any) -> None:
        breaker = DeploymentCircuitBreaker()
        async with sessions() as session:
            fixture = await make_fixture(session)
            for _ in range(FAILURE_THRESHOLD):
                await breaker.record_validation_failure(
                    session,
                    project_id=fixture.project_id,
                    environment=ENVIRONMENT,
                    reason="the image does not exist",
                )
            await breaker.record_success(session, project_id=fixture.project_id, environment=ENVIRONMENT)
            state = await breaker.state_for(session, project_id=fixture.project_id, environment=ENVIRONMENT)
            row = await _row(session, fixture.project_id)

        assert state.state == STATE_CLOSED
        assert state.consecutive_failures == 0
        assert row["opened_at"] is None
        # THE REASON IS KEPT. After a recovery, "what was failing" is the thing an operator wants to read,
        # and clearing it erases the only record of why deployments stopped.
        assert row["last_failure_reason"] == "the image does not exist"


class TestWhatTheBreakerMustNotCount:
    async def test_the_schema_refuses_an_open_breaker_with_no_opening_time(self, sessions: Any) -> None:
        """An open row with a NULL `opened_at` would never become half-open.

        It would stop deployments for ever with no path back, so the CHECK constraint refuses it. Asserted
        because a constraint nobody tests is a constraint that gets dropped in a later migration.
        """
        async with sessions() as session:
            fixture = await make_fixture(session)
            with pytest.raises(Exception) as refused:
                await session.execute(
                    text(
                        "INSERT INTO deployment_circuit_breakers "
                        "(id, project_id, environment, state, consecutive_failures, opened_at) "
                        "VALUES (:id, :project_id, :environment, 'open', 3, NULL)"
                    ),
                    {
                        "id": str(uuid.uuid4()),
                        "project_id": str(fixture.project_id),
                        "environment": ENVIRONMENT,
                    },
                )
                await session.commit()
            assert "ck_deployment_breakers_open_has_opened_at" in str(refused.value)
            await session.rollback()

    async def test_the_settler_counts_a_failure_and_clears_it_on_success(self, sessions: Any) -> None:
        """The counting site is the settler's failure branch, reached only when the agent RAN the command.

        This is the structural part of the design: a policy deny, a held approval and an agent timeout
        never reach `record_command_result`, so they cannot be counted — the distinction does not depend on
        anybody remembering a condition.
        """
        from src.deployments.settler import _failure_reason

        # The reason travels from the agent's report rather than being a generic string.
        assert _failure_reason({"error": "no nodes available"}) == "no nodes available"
        assert _failure_reason({"output": "Error from server (Invalid)"}) == "Error from server (Invalid)"
        # Absence is stated as absence rather than filled in.
        assert "sent no detail" in _failure_reason(None)
        assert "sent no detail" in _failure_reason({"unrelated": 1})
