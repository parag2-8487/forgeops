# SPDX-License-Identifier: FSL-1.1-ALv2
"""Self-healing routes. Phase 2 §2.12.

THREE ROUTES, AND WHAT SEPARATES THEM IS WHO WAITS.

`POST .../heal` executes a SAFE remedy. No human waits, but governance still happens in full: the action goes
through `transit_host_action`, so it faces policy, blast radius, audit and a rollback handle exactly as an
operator-initiated restart does. "Auto" means nobody was asked, never that nothing was checked.

`POST .../heal/propose` records a RISKY remedy as `proposed` and stops. It mints no command and sends nothing.
A human then acts on it through the existing governed routes.

**THE SPLIT IS NOT A PARAMETER.** `/heal` derives the remedy from the incident and REFUSES if the result is
not in the safe set -- so there is no request body through which a caller could ask for a rollback to be
auto-executed. A single route with an `auto: bool` field would have been the natural shape and would have put
the most dangerous decision in the request.

WHY THE GUARD RAILS ARE CHECKED BEFORE THE CHOKEPOINT IS ASKED. A refused heal must not produce a change set,
an approval request, or an audit row describing an action nobody will take -- the same placement argument as
the deployment circuit breaker, which is checked before the deployment row is created and before a human is
asked.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.dependencies import require_principal
from ..auth.principal import Principal
from ..core.db import get_session
from ..core.errors import problem
from ..governance.chokepoint import GovernanceChokepoint
from .healing import (
    MAX_ATTEMPTS_PER_INCIDENT,
    RISKY_REMEDIES,
    SAFE_REMEDIES,
    HealingRefusedError,
    check_guard_rails,
    is_auto_executable,
    plan_from_incident,
    record_action,
)
from .postmortem import generate as generate_postmortem
from .postmortem import persist as persist_postmortem

router = APIRouter(tags=["self-healing"])


class HealRequest(BaseModel):
    """What a caller may say about a heal.

    NOTE WHAT IS ABSENT: no container name, no namespace, no replica count, no `auto` flag. The target is
    derived from the incident, which is what makes "an auto action cannot widen its own scope" true by
    construction rather than by validation. `remedy` can only ever make the outcome MORE restrictive.
    """

    model_config = {"extra": "forbid"}

    environment: str = Field(min_length=1, max_length=64)
    #: Optional override. A risky name here produces a refusal from `/heal`, never an auto-execution.
    remedy: str | None = None


def _chokepoint(request: Request) -> GovernanceChokepoint:
    chokepoint = getattr(request.app.state, "governance_chokepoint", None)
    if chokepoint is None:
        raise problem(
            "healing-governance-absent",
            detail=(
                "The governance chokepoint is not composed, so no healing action can be taken. Acting "
                "outside governance is not offered as an alternative -- an automated action with no audit "
                "row is the one thing this feature must never do."
            ),
        )
    return chokepoint


async def _incident(session: AsyncSession, incident_id: uuid.UUID) -> Any:
    row = (
        (
            await session.execute(
                text("SELECT id, project_id, source, severity, title, detail FROM incidents WHERE id = :id"),
                {"id": incident_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise problem("incident-absent", detail="No such incident.")
    return row


@router.get("/healing/remedies")
async def list_remedies(
    principal: Annotated[Principal, Depends(require_principal)],
) -> dict[str, Any]:
    """The closed remedy vocabulary, and which tier each is in.

    Published so the split is inspectable rather than implied. An operator can see exactly what this system
    will do without being asked, which is a precondition for trusting it with anything.
    """
    return {
        "safe": sorted(SAFE_REMEDIES),
        "risky": sorted(RISKY_REMEDIES),
        "max_attempts_per_incident": MAX_ATTEMPTS_PER_INCIDENT,
        "explanation": (
            "Safe remedies execute without a human waiting and are logged; they are idempotent and "
            "reversible by the system's next observation. Risky remedies are only ever proposed. The safe "
            "set is closed: a remedy not listed there cannot be auto-executed, and the database refuses a "
            "row claiming otherwise. Nothing in either tier runs a command line."
        ),
    }


@router.post("/incidents/{incident_id}/heal")
async def heal(
    incident_id: uuid.UUID,
    body: HealRequest,
    principal: Annotated[Principal, Depends(require_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
    chokepoint: Annotated[GovernanceChokepoint, Depends(_chokepoint)],
) -> dict[str, Any]:
    """Execute a safe remedy. Refuses anything else."""
    incident = await _incident(session, incident_id)

    try:
        plan = plan_from_incident(
            source=str(incident["source"]),
            detail=dict(incident["detail"] or {}),
            remedy=body.remedy,
        )
    except HealingRefusedError as exc:
        raise problem("healing-refused", detail=str(exc)) from exc

    if not is_auto_executable(plan.remedy):
        # THE REFUSAL THAT MATTERS. A risky remedy reaching this route is not escalated, not queued, not
        # executed with a warning -- it is refused and the caller is pointed at the propose route. There is
        # no branch here that leads to execution.
        raise problem(
            "healing-requires-approval",
            detail=(
                f"{plan.remedy} is not in the safe set, so it cannot be executed without an approval. "
                "Propose it instead, and a human will decide. The safe set is closed on purpose: a remedy "
                "nobody has classified is treated as risky."
            ),
        )

    try:
        await check_guard_rails(session, incident_id=incident_id, remedy=plan.remedy)
    except HealingRefusedError as exc:
        # RECORDED AS A ROW, then refused. "The system did nothing" and "the system decided not to, for this
        # reason" are different facts, and only the second is debuggable.
        await record_action(session, incident_id=incident_id, plan=plan, state="refused", note=str(exc))
        await session.commit()
        raise problem("healing-refused", detail=str(exc)) from exc

    action_id = await record_action(session, incident_id=incident_id, plan=plan, state="executing")
    await session.commit()

    try:
        submission = await chokepoint.transit_host_action(
            session,
            project_id=incident["project_id"],
            principal=principal,
            operation=plan.operation,
            target=str(plan.arguments.get("container") or plan.arguments.get("pod") or ""),
            args=plan.arguments,
            environment_name=body.environment,
            # FALSE because the remedy is in the safe set -- which is the ONLY thing that sets this, and it
            # is read from the closed set rather than from the request. Policy may still require approval on
            # its own terms; this says only that self-healing is not itself demanding one.
            environment_requires_approval=False,
            reason=f"self-healing: {plan.rationale}",
        )
    except Exception as exc:  # noqa: BLE001 - a governance refusal is an outcome, not a server fault
        # A REFUSAL BY GOVERNANCE MARKS THE ACTION FAILED, which disqualifies this remedy for this incident
        # permanently. That is deliberate: if policy refuses a restart, retrying it every cooldown is an
        # automated system arguing with its own policy.
        await session.execute(
            text("UPDATE healing_actions SET state = 'failed', completed_at = now(), note = :note WHERE id = :id"),
            {"id": action_id, "note": f"governance refused: {str(exc)[:500]}"},
        )
        await session.commit()
        raise problem(
            "healing-refused",
            detail=(
                f"Governance refused this action: {exc}. It has been recorded as failed, so it will not be "
                "retried automatically."
            ),
        ) from exc

    change_set_id = getattr(submission, "change_set_id", None)
    await session.execute(
        text("UPDATE healing_actions SET change_set_id = :cs WHERE id = :id"),
        {"cs": change_set_id, "id": action_id},
    )
    await session.commit()

    return {
        "action_id": str(action_id),
        "remedy": plan.remedy,
        "auto": True,
        "change_set_id": str(change_set_id) if change_set_id else None,
        "explanation": (
            f"{plan.remedy} was executed without waiting for a human. It still passed policy, blast radius "
            "and audit: 'automatic' means nobody was asked, not that nothing was checked. If this remedy "
            "fails it will not be tried again for this incident."
        ),
    }


@router.post("/incidents/{incident_id}/heal/propose")
async def propose(
    incident_id: uuid.UUID,
    body: HealRequest,
    principal: Annotated[Principal, Depends(require_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """Record a remedy as proposed. Mints nothing and sends nothing.

    Accepts SAFE remedies too: an operator may legitimately want a human to look at a restart before it
    happens, and refusing to propose something merely because it could have been automatic would be a worse
    default than allowing the more cautious path.
    """
    incident = await _incident(session, incident_id)
    try:
        plan = plan_from_incident(
            source=str(incident["source"]),
            detail=dict(incident["detail"] or {}),
            remedy=body.remedy,
        )
    except HealingRefusedError as exc:
        raise problem("healing-refused", detail=str(exc)) from exc

    action_id = await record_action(session, incident_id=incident_id, plan=plan, state="proposed")
    await session.commit()
    return {
        "action_id": str(action_id),
        "remedy": plan.remedy,
        "auto": False,
        "explanation": (
            "Recorded as proposed. Nothing has been sent to an agent and no command has been signed. A human "
            "decides whether this runs."
        ),
    }


@router.get("/incidents/{incident_id}/healing")
async def healing_log(
    incident_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """Every healing action for this incident, INCLUDING refusals, with the remaining budget."""
    await _incident(session, incident_id)
    actions = [
        dict(row)
        for row in (
            await session.execute(
                text(
                    "SELECT id, remedy, operation, arguments, auto, state, change_set_id, note, "
                    "created_at, completed_at FROM healing_actions "
                    "WHERE incident_id = :id ORDER BY created_at"
                ),
                {"id": incident_id},
            )
        ).mappings()
    ]
    counted = [action for action in actions if action["state"] != "refused"]
    remaining = max(0, MAX_ATTEMPTS_PER_INCIDENT - len(counted))
    failed_remedies = sorted({action["remedy"] for action in actions if action["state"] == "failed"})
    return {
        "actions": actions,
        "attempts_used": len(counted),
        "attempts_remaining": remaining,
        "disqualified_remedies": failed_remedies,
        "explanation": (
            f"{len(counted)} of {MAX_ATTEMPTS_PER_INCIDENT} automated attempts used."
            + (
                f" {', '.join(failed_remedies)} failed and will not be retried for this incident."
                if failed_remedies
                else ""
            )
            + (" The automated budget is exhausted; this incident is now a human's." if remaining == 0 else "")
        )
        if actions
        else (
            "No healing action has been attempted for this incident. That is not the same as one having "
            "been attempted and declined -- a declined action would be listed here with its reason."
        ),
    }


@router.post("/incidents/{incident_id}/postmortem")
async def write_postmortem(
    incident_id: uuid.UUID,
    request: Request,
    principal: Annotated[Principal, Depends(require_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """Generate the post-incident summary and the long-term recommendations."""
    incident = await _incident(session, incident_id)

    latest = (
        (
            await session.execute(
                text(
                    "SELECT state, problem FROM incident_analyses WHERE incident_id = :id "
                    "ORDER BY created_at DESC LIMIT 1"
                ),
                {"id": incident_id},
            )
        )
        .mappings()
        .one_or_none()
    )

    actions = [
        dict(row)
        for row in (
            await session.execute(
                text(
                    "SELECT remedy, auto, state, note FROM healing_actions WHERE incident_id = :id ORDER BY created_at"
                ),
                {"id": incident_id},
            )
        ).mappings()
    ]

    postmortem = await generate_postmortem(
        model=getattr(request.app.state, "artifact_model", None),
        title=str(incident["title"]),
        source=str(incident["source"]),
        analysis_state=str(latest["state"]) if latest else "not_analysed",
        problem=str(latest["problem"]) if latest else "",
        actions=actions,
    )
    postmortem_id = await persist_postmortem(session, incident_id=incident_id, postmortem=postmortem)
    await session.commit()

    return {
        "postmortem_id": str(postmortem_id),
        "state": postmortem.state,
        "summary": postmortem.summary,
        "recommendations": postmortem.recommendations,
        "actions_considered": postmortem.actions_considered,
        "explanation": postmortem.explanation,
    }


@router.get("/incidents/{incident_id}/postmortem")
async def read_postmortems(
    incident_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """Every postmortem for this incident, newest first."""
    await _incident(session, incident_id)
    rows = [
        dict(row)
        for row in (
            await session.execute(
                text(
                    "SELECT id, state, summary, recommendations, model, actions_considered, created_at "
                    "FROM incident_postmortems WHERE incident_id = :id ORDER BY created_at DESC"
                ),
                {"id": incident_id},
            )
        ).mappings()
    ]
    return {
        "postmortems": rows,
        "explanation": (
            f"{len(rows)} post-incident record(s)."
            if rows
            else (
                "No post-incident record has been written. The incident, its evidence and its healing "
                "actions are all readable directly; what is absent is the narrative, not the facts."
            )
        ),
    }
