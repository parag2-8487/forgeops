# SPDX-License-Identifier: FSL-1.1-ALv2
"""Guard-railed self-healing. Phase 2 §2.12.

THIS IS THE MOST SAFETY-CRITICAL MODULE IN THE PROJECT, because it is the only one where the system acts on
production without a human in the loop. Four properties make that defensible, and each is enforced by
STRUCTURE rather than by a condition somebody could get wrong later.

1. THE SAFE SET IS CLOSED AND AUTO-EXECUTION IS DERIVED FROM MEMBERSHIP IN IT.
   `SAFE_REMEDIES` is a frozenset of two verbs. `is_auto_executable` is `remedy in SAFE_REMEDIES` and nothing
   else -- no severity check, no confidence threshold, no "unless" clause. A remedy nobody has classified is
   therefore risky, which is the fail-safe direction: adding a remedy without thinking about it gets a human,
   not an auto-execution. The alternative shape -- a `risky` flag on each remedy -- fails open the moment
   somebody adds a remedy and forgets the flag.

2. AN AUTO ACTION CANNOT WIDEN ITS OWN SCOPE, because it does not choose its own target.
   `plan_from_incident` derives the operation arguments from the INCIDENT ROW. There is no parameter through
   which a caller supplies a container name, a namespace or a replica count for an auto action. So an auto
   restart can only ever restart the container the incident is about. A remedy that took its target as an
   argument would let a mis-analysis restart the database because the incident mentioned it.

3. A HEALING LOOP CANNOT AMPLIFY.
   Three independent bounds, because one is not enough:
     * `MAX_ATTEMPTS_PER_INCIDENT` caps how many times ANY remedy fires for one incident, ever.
     * `COOLDOWN_SECONDS` caps how often, so a crash loop producing an occurrence every two seconds cannot
       produce a restart every two seconds.
     * A FAILED ACTION DISQUALIFIES ITS OWN REMEDY for that incident permanently. This is the one that
       matters most: a restart that fails is evidence the restart is not the answer, and retrying it is how
       an automated system turns one broken container into a hundred restarts and a filled disk.
   The bound is checked against COMMITTED ROWS, not held in memory, so two workers cannot each pass it.

4. EVERY HEALING ACTION IS A MUTATION AND GOES THROUGH THE CHOKEPOINT.
   Auto-execution changes WHO APPROVES -- policy may auto-approve a safe remedy -- and changes nothing else.
   Blast radius, audit and the rollback handle all still happen. "Auto" here means "no human waited", never
   "no governance".

WHAT IS DELIBERATELY NOT HERE. There is no remedy that scales, rolls back, edits configuration, deletes
anything, or runs a command line. The first three are the box's own examples of RISKY and they go through
approval as `propose` rather than `execute`; the last two are not offered at any tier, because a self-healing
system with a general-purpose action is a self-harming system with good intentions.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# --- the closed vocabulary ------------------------------------------------------------------------------

#: Remedies this system will perform WITHOUT a human waiting.
#:
#: Two, and both share one property: they are idempotent and reversible by the system's own next observation.
#: Restarting a container that has already exited changes nothing an operator would not have changed; if the
#: restart was wrong, the container exits again and the incident reopens. Nothing here can destroy data,
#: reduce capacity, or alter a declared desired state.
SAFE_REMEDIES: Final[frozenset[str]] = frozenset(
    {
        "restart_container",
        "restart_pod",
    }
)

#: Remedies that exist but always require an approval. Listed so the catalogue is complete and a caller can
#: PROPOSE one -- not so that any of them can ever be auto-executed.
RISKY_REMEDIES: Final[frozenset[str]] = frozenset(
    {
        "rollback_deployment",
        "scale_workload",
        "apply_configuration_change",
    }
)

REMEDIES: Final[frozenset[str]] = SAFE_REMEDIES | RISKY_REMEDIES

#: Ever, for one incident, across all remedies. Three is enough to cover a transient fault that needs one
#: nudge plus a retry, and low enough that a wrong remedy stops being applied long before it does damage.
MAX_ATTEMPTS_PER_INCIDENT: Final = 3

#: The floor between two actions for one incident. Five minutes, which is longer than any container takes to
#: crash again -- so a crash loop cannot drive the restart rate.
COOLDOWN_SECONDS: Final = 300

ACTION_STATES: Final[tuple[str, ...]] = (
    "proposed",  # a risky remedy awaiting a human
    "executing",  # sent to the agent
    "succeeded",
    "failed",
    "refused",  # a guard rail said no; the reason is recorded
)


class HealingRefusedError(Exception):
    """A guard rail said no. Carries the sentence an operator reads."""


def is_auto_executable(remedy: str) -> bool:
    """The entire auto-execution decision.

    Membership in a closed set, with no second condition. A remedy nobody has classified is risky, which is
    the fail-safe direction -- and the reason this is not a `risky: bool` field on a remedy descriptor, which
    would fail OPEN the first time somebody added a remedy and forgot to set it.
    """
    return remedy in SAFE_REMEDIES


@dataclass(frozen=True, slots=True)
class HealingPlan:
    """What will be done, derived from the incident rather than supplied by a caller."""

    remedy: str
    #: The agent operation this becomes. Named here so the chokepoint and the audit row agree.
    operation: str
    #: The arguments, DERIVED. See `plan_from_incident` for why there is no way to pass these in.
    arguments: dict[str, Any]
    auto: bool
    #: Prose for the activity log and for the approval request.
    rationale: str


def plan_from_incident(*, source: str, detail: dict[str, Any], remedy: str | None = None) -> HealingPlan:
    """Derive a plan from an incident. THE ONLY WAY A PLAN IS BUILT.

    `remedy` is an optional OVERRIDE, and it can only ever make the outcome MORE restrictive: an override
    naming a risky remedy produces `auto=False`, and an override naming an unknown remedy is refused. It
    cannot supply arguments -- those come from `detail` in every case, which is what makes property 2 true.
    A caller wanting to restart a different container has to file an incident about that container.
    """
    if remedy is not None and remedy not in REMEDIES:
        raise HealingRefusedError(
            f"{remedy!r} is not a known remedy. The remedy set is closed: an unrecognised name is refused "
            "rather than attempted, because a typo that reached an agent would be an unreviewed action."
        )

    if source == "container_exit":
        container = str(detail.get("container", "")).strip()
        if not container:
            raise HealingRefusedError(
                "the incident does not name a container, so there is nothing to restart. Guessing a target "
                "from the incident title is not attempted."
            )
        chosen = remedy or "restart_container"
        return HealingPlan(
            remedy=chosen,
            operation="docker.container_action",
            # Derived. There is no caller-supplied name here, so an auto restart can only ever touch the
            # container this incident is about.
            arguments={"action": "restart", "container": container},
            auto=is_auto_executable(chosen),
            rationale=(
                f"Container {container} exited with code {detail.get('exit_code')}. A restart is the "
                "smallest action that could return it to service, and if the restart is wrong the container "
                "exits again and this incident reopens."
            ),
        )

    if source == "kubernetes_event":
        namespace = str(detail.get("namespace", "")).strip()
        name = str(detail.get("name", "")).strip()
        if not namespace or not name:
            raise HealingRefusedError(
                "the incident does not name a namespace and a workload, so there is nothing to restart."
            )
        if str(detail.get("kind", "")) != "Pod":
            # NOT generalised to any kind. Deleting a Deployment is not a restart, and a remedy that treated
            # every kind the same would do very different things under one name.
            raise HealingRefusedError(
                f"a {detail.get('kind')!r} is not restartable by this remedy. Only a Pod is: deleting other "
                "kinds is not a restart, and one remedy meaning different things per kind is how an "
                "automated action does something nobody intended."
            )
        chosen = remedy or "restart_pod"
        return HealingPlan(
            remedy=chosen,
            operation="kubernetes.pod_action",
            arguments={"action": "restart", "namespace": namespace, "pod": name},
            auto=is_auto_executable(chosen),
            rationale=(
                f"Pod {namespace}/{name} reported {detail.get('reason')}. Restarting it lets its controller "
                "reschedule; the desired state is unchanged."
            ),
        )

    # NO DEFAULT REMEDY. A source without a mapping gets a stated refusal rather than a generic action, for
    # the same reason `plan_from_incident` derives arguments: a fallback remedy is one that runs against
    # incidents nobody considered.
    raise HealingRefusedError(
        f"no remedy is defined for an incident observed by {source!r}. This is a refusal rather than a "
        "generic action: a fallback remedy would run against incidents nobody had thought about."
    )


async def check_guard_rails(
    session: AsyncSession, *, incident_id: uuid.UUID, remedy: str, now: datetime | None = None
) -> None:
    """Refuse if any bound is reached. Raises `HealingRefusedError` with the operator-facing sentence.

    READ FROM COMMITTED ROWS rather than from memory, so two workers each observing the same incident cannot
    each conclude the budget is free. The row-level accounting is the arbiter.
    """
    moment = now or datetime.now(UTC)

    row = (
        (
            await session.execute(
                text(
                    """
                SELECT
                    count(*) AS attempts,
                    max(created_at) AS latest,
                    count(*) FILTER (WHERE state = 'failed' AND remedy = :remedy) AS remedy_failures
                FROM healing_actions
                WHERE incident_id = :incident_id
                  AND state <> 'refused'
                """
                ),
                {"incident_id": incident_id, "remedy": remedy},
            )
        )
        .mappings()
        .one()
    )

    # BOUND 3, CHECKED FIRST because it is the most important and the cheapest to explain. A remedy that
    # already failed for this incident is evidence that it is not the answer; retrying it is how one broken
    # container becomes a hundred restarts.
    if int(row["remedy_failures"]) > 0:
        raise HealingRefusedError(
            f"{remedy} has already been tried for this incident and failed. It will not be tried again: a "
            "remedy that failed is evidence it is not the answer, and repeating it is how an automated "
            "system amplifies one failure into many. A different remedy, or a human, is needed."
        )

    attempts = int(row["attempts"])
    if attempts >= MAX_ATTEMPTS_PER_INCIDENT:
        raise HealingRefusedError(
            f"this incident has already had {attempts} healing action(s), which is the limit of "
            f"{MAX_ATTEMPTS_PER_INCIDENT}. Further automated attempts are refused so that a problem the "
            "system cannot fix does not keep being acted on. The incident remains open for a human."
        )

    latest = row["latest"]
    if latest is not None:
        # `latest` arrives timezone-aware from Postgres; a naive comparison would raise, and catching that
        # would hide a real ordering bug.
        elapsed = (moment - latest).total_seconds()
        if elapsed < COOLDOWN_SECONDS:
            raise HealingRefusedError(
                f"the last healing action for this incident was {int(elapsed)} seconds ago, inside the "
                f"{COOLDOWN_SECONDS}-second cooldown. A crash loop reports an occurrence every few seconds, "
                "and without this floor it would drive one restart per occurrence."
            )


async def record_action(
    session: AsyncSession,
    *,
    incident_id: uuid.UUID,
    plan: HealingPlan,
    state: str,
    change_set_id: uuid.UUID | None = None,
    note: str = "",
) -> uuid.UUID:
    """Write the action row. Every attempt is recorded INCLUDING refusals.

    A refused action is a row, not a silence. "The system did nothing" and "the system decided not to" are
    different facts, and an operator debugging why a container was never restarted needs the second.
    """
    if state not in ACTION_STATES:
        raise ValueError(f"{state!r} is not a healing action state")
    action_id = uuid.uuid4()
    await session.execute(
        text(
            """
            INSERT INTO healing_actions (
                id, incident_id, remedy, operation, arguments, auto, state, change_set_id, note
            )
            VALUES (
                :id, :incident_id, :remedy, :operation, CAST(:arguments AS jsonb), :auto, :state,
                :change_set_id, :note
            )
            """
        ),
        {
            "id": action_id,
            "incident_id": incident_id,
            "remedy": plan.remedy,
            "operation": plan.operation,
            "arguments": _json(plan.arguments),
            "auto": plan.auto,
            "state": state,
            "change_set_id": change_set_id,
            "note": note or plan.rationale,
        },
    )
    return action_id


def _json(value: dict[str, Any]) -> str:
    import json

    return json.dumps(value, default=str)
