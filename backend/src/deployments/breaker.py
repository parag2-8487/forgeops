"""The deployment pipeline's circuit breaker. Phase 2 §2.2.

WHAT IT IS FOR, stated precisely, because a breaker built for the wrong failure is worse than none. The box
said a breaker chosen before there is a pipeline to break is a guess about which failures repeat — and now
there is a pipeline, the answer is observable: a deployment fails on VALIDATION (a manifest the cluster
refuses, an image that does not exist, a CPU request no node can satisfy) in the same way every time until
somebody changes the manifests. Retrying it costs an approval, a signed envelope, an agent round trip and a
governance row, and produces the identical failure.

So the breaker trips on REPEATED VALIDATION FAILURE for one project and environment, and what it does when
tripped is refuse the deployment BEFORE the approval is requested. That is the whole value: it saves the
human from approving something already known to fail.

WHAT IT DOES NOT TRIP ON, and this is the more important half:

  * A POLICY DENY is not a failure. It is the system working, and a breaker counting denies would open
    because governance was doing its job.
  * A HELD APPROVAL is not a failure either. It is a human being asked.
  * AN AGENT TIMEOUT is not counted. A slow or offline agent is an infrastructure condition, not a broken
    manifest, and opening the breaker for it would mean an operator who restarts their agent finds their
    deployments refused for a reason that has already been fixed.

WHY IT IS PERSISTED AND NOT IN MEMORY. Two backend replicas with in-memory breakers disagree about whether
one is open, and a request that fails against one and succeeds against the other is worse than no breaker.
The state is a row, so every replica reads the same answer.

HALF-OPEN IS A SINGLE TRIAL, NOT A WINDOW. After the cooldown, exactly one deployment is admitted. If it
validates, the breaker closes; IF IT FAILS, IT RE-OPENS with the cooldown restarted and the count reset
to one. Admitting "all traffic for ten seconds" -- the usual HTTP breaker shape -- is wrong here because
a deployment is not a request: ten seconds admits one or fifty depending on who is clicking, and the
point is to test the hypothesis once.

The re-open was NOT the first behaviour written here. The first version left a failed trial `half_open`
with the count at one, on the argument that re-opening immediately would lock the environment for
another cooldown with no new evidence. A test caught the leftover state, and re-reading the argument it
was wrong: by then there have been four failures, which is more evidence than the three that opened it
in the first place. The count still resets to one because it means failures since the last DECISION,
not a continuation of the run that opened the breaker -- a display of `4` would describe nothing.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

#: How many consecutive validation failures open the breaker.
#:
#: THREE, not one. One failure is often a genuine typo the operator fixes on the next attempt, and a
#: breaker that opened on it would refuse the corrected deployment. Three consecutive failures with no
#: success in between is a pattern.
FAILURE_THRESHOLD: Final[int] = 3

#: How long an open breaker stays open before admitting one trial.
#:
#: Five minutes. Long enough that an operator notices and reads the reason rather than retrying through it;
#: short enough that a fix does not wait on an administrator.
COOLDOWN: Final[timedelta] = timedelta(minutes=5)

#: The three states. Persisted as text, with a CHECK, for the reason every other vocabulary in this
#: codebase is: a typo becomes a constraint violation rather than a state nothing handles.
STATE_CLOSED: Final[str] = "closed"
STATE_OPEN: Final[str] = "open"
STATE_HALF_OPEN: Final[str] = "half_open"

BREAKER_STATES: Final[tuple[str, ...]] = (STATE_CLOSED, STATE_OPEN, STATE_HALF_OPEN)


class CircuitOpenError(Exception):
    """Raised when a deployment is refused by an open breaker.

    AN EXCEPTION AND NOT A FALSY RETURN, deliberately. The same judgement a policy deny makes: a caller
    that forgot to check a boolean would deploy anyway, and the failure mode of a breaker nobody checked is
    a breaker that does nothing. `reason` and `retry_after` are carried so the route can render an RFC 9457
    problem that says when to try again rather than just refusing.
    """

    def __init__(self, reason: str, retry_after_seconds: int) -> None:
        super().__init__(reason)
        self.reason = reason
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True)
class BreakerState:
    """What the breaker currently says for one project and environment."""

    state: str
    consecutive_failures: int
    opened_at: datetime | None
    last_failure_reason: str | None

    @property
    def is_open(self) -> bool:
        return self.state == STATE_OPEN

    def describe(self) -> str:
        """A sentence for a panel.

        NEVER-TRIPPED, OPEN AND HALF-OPEN ARE DIFFERENT THINGS AND THE WORDS SAY SO. A panel that showed
        "deployments unavailable" for all three would leave an operator unable to tell a system that has
        never failed from one waiting to retry.
        """
        if self.state == STATE_CLOSED and self.consecutive_failures == 0:
            return "No recent validation failures for this environment."
        if self.state == STATE_CLOSED:
            return (
                f"{self.consecutive_failures} consecutive validation failure(s); "
                f"{FAILURE_THRESHOLD - self.consecutive_failures} more would stop further attempts."
            )
        if self.state == STATE_HALF_OPEN:
            return "Stopped after repeated validation failures. The next deployment is a single trial."
        reason = self.last_failure_reason or "no reason was recorded"
        return f"Stopped after {self.consecutive_failures} consecutive validation failures. The last one said: {reason}"


class DeploymentCircuitBreaker:
    """Reads and writes the breaker row for one project and environment."""

    def __init__(self, *, threshold: int = FAILURE_THRESHOLD, cooldown: timedelta = COOLDOWN) -> None:
        self._threshold = threshold
        self._cooldown = cooldown

    async def state_for(self, session: AsyncSession, *, project_id: uuid.UUID, environment: str) -> BreakerState:
        """Read the current state, applying the cooldown.

        The cooldown is applied on READ rather than by a scheduled job, so there is no window in which the
        row says `open` and the truth is `half_open`. A background sweeper would be a second source of
        truth for the same fact.
        """
        row = (
            (
                await session.execute(
                    text(
                        "SELECT state, consecutive_failures, opened_at, last_failure_reason "
                        "FROM deployment_circuit_breakers "
                        "WHERE project_id = :project_id AND environment = :environment"
                    ),
                    {"project_id": str(project_id), "environment": environment},
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            return BreakerState(STATE_CLOSED, 0, None, None)

        state = str(row["state"])
        opened_at = row["opened_at"]
        if state == STATE_OPEN and opened_at is not None:
            elapsed = datetime.now(UTC) - _as_aware(opened_at)
            if elapsed >= self._cooldown:
                # The cooldown has passed, so the next attempt is the single trial. Reported as
                # `half_open` without writing, because the write belongs to the attempt that takes the
                # trial — writing here would consume the trial for a caller that only looked.
                state = STATE_HALF_OPEN
        return BreakerState(
            state=state,
            consecutive_failures=int(row["consecutive_failures"]),
            opened_at=_as_aware(opened_at) if opened_at is not None else None,
            last_failure_reason=row["last_failure_reason"],
        )

    async def guard(self, session: AsyncSession, *, project_id: uuid.UUID, environment: str) -> None:
        """Raise `CircuitOpenError` when the breaker refuses this deployment.

        Called BEFORE the approval is requested. That ordering is the entire point: refusing after a human
        approved would waste the approval, and refusing after the envelope is signed would leave a signed
        command nobody sent.
        """
        current = await self.state_for(session, project_id=project_id, environment=environment)
        if not current.is_open:
            return
        retry_after = self._cooldown
        if current.opened_at is not None:
            remaining = self._cooldown - (datetime.now(UTC) - current.opened_at)
            retry_after = remaining if remaining > timedelta(0) else timedelta(0)
        raise CircuitOpenError(current.describe(), int(retry_after.total_seconds()))

    async def record_validation_failure(
        self, session: AsyncSession, *, project_id: uuid.UUID, environment: str, reason: str
    ) -> BreakerState:
        """Count a validation failure, opening the breaker at the threshold.

        ONLY A VALIDATION FAILURE REACHES HERE. A policy deny, a held approval and an agent timeout do not
        call this, and the tests assert that by driving each of them and reading the row back — a breaker
        that opened because governance refused a deployment would be a breaker punishing correct behaviour.
        """
        await session.execute(
            text(
                "INSERT INTO deployment_circuit_breakers "
                "(id, project_id, environment, state, consecutive_failures, last_failure_reason, "
                " opened_at, updated_at) "
                "VALUES (:id, :project_id, :environment, :closed, 1, :reason, NULL, now()) "
                "ON CONFLICT (project_id, environment) DO UPDATE SET "
                # The count resets to 1 rather than incrementing when the breaker was half-open, because a
                # trial that fails is one failure since the last decision, not a continuation of the run
                # that opened it. Without this, one trial failure after three could show four.
                "  consecutive_failures = CASE "
                "    WHEN deployment_circuit_breakers.state = :half_open THEN 1 "
                "    ELSE deployment_circuit_breakers.consecutive_failures + 1 END, "
                "  last_failure_reason = :reason, "
                "  updated_at = now()"
            ),
            {
                "id": str(uuid.uuid4()),
                "project_id": str(project_id),
                "environment": environment,
                "closed": STATE_CLOSED,
                "half_open": STATE_HALF_OPEN,
                "reason": reason[:1000],
            },
        )
        # Then open it if the threshold is met. Two statements rather than one CASE expression, because the
        # threshold has to be compared against the count AFTER the increment and expressing that inline
        # duplicates the increment logic in the condition.
        await session.execute(
            text(
                "UPDATE deployment_circuit_breakers "
                "SET state = :open, opened_at = now() "
                "WHERE project_id = :project_id AND environment = :environment "
                "  AND (consecutive_failures >= :threshold OR state = :half_open) "
                "  AND state <> :open"
            ),
            {
                "open": STATE_OPEN,
                "half_open": STATE_HALF_OPEN,
                "project_id": str(project_id),
                "environment": environment,
                "threshold": self._threshold,
            },
        )
        return await self.state_for(session, project_id=project_id, environment=environment)

    async def record_success(self, session: AsyncSession, *, project_id: uuid.UUID, environment: str) -> None:
        """A deployment that validated closes the breaker and clears the count.

        `last_failure_reason` is KEPT rather than cleared. After a recovery, "what was failing" is the
        thing an operator wants to read, and clearing it would erase the only record of why deployments
        stopped in the first place.
        """
        await session.execute(
            text(
                "UPDATE deployment_circuit_breakers "
                "SET state = :closed, consecutive_failures = 0, opened_at = NULL, updated_at = now() "
                "WHERE project_id = :project_id AND environment = :environment"
            ),
            {"closed": STATE_CLOSED, "project_id": str(project_id), "environment": environment},
        )


def _as_aware(value: datetime) -> datetime:
    """Treat a naive timestamp as UTC.

    Postgres `timestamptz` comes back aware, but a value read through a driver configured otherwise does
    not, and subtracting an aware from a naive datetime raises — an error about types in the middle of a
    cooldown calculation, which is the least legible place for one.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value
