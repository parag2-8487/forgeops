# SPDX-License-Identifier: FSL-1.1-ALv2
"""Command Center routes. Phase 2 §2.5.

TWO ROUTES AND THE SPLIT IS LAYER 4 OF THE DEFENCE.

`POST /commands/interpret` classifies and resolves. It changes NOTHING -- no agent command, no change set, no
row beyond the history record of what was asked. It returns a plan for a human to read.

`POST /commands/execute` takes a plan the caller has confirmed and runs it. The plan is REBUILT here from the
intent and slots rather than accepted as a structure, which is the point: a client cannot post an operation
name. It posts an intent name and slots, and this route derives the operation from `COMMANDS` exactly as
`interpret` did. So a client that tampered with the plan it was shown gets the same resolution the server
would have produced anyway.

WHY NOT ONE ROUTE WITH A `confirm: bool`. Because then the dangerous path is reachable by flipping a field in
a request, and every mutation would be one boolean away from a classifier's guess. Two routes make
confirmation a step the client cannot skip by construction rather than by validation.

THE MULTI-AGENT ORCHESTRATOR IS THIS DISPATCH TABLE, and it is deliberately not a set of autonomous agents
calling each other. Each category is handled by the existing domain that owns it -- deployments by the
deployment route, diagnostics by the agent's read operations, generation by the generation pipeline. An
"agent" here is a handler with one job and no authority of its own; the alternative, agents that can invoke
one another, multiplies the paths to the chokepoint and makes the blast radius of a mis-classification
unbounded.
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
from .intents import (
    COMMANDS,
    CommandRefusedError,
    Intent,
    catalogue,
    classify,
    resolve,
)

router = APIRouter(tags=["commands"])


class InterpretRequest(BaseModel):
    """What the user typed.

    `utterance` is the ONLY free text in this API, and it never leaves this route: `classify` turns it into an
    intent name and enumerated slots, and nothing downstream sees the string. That is the boundary the whole
    section is built around.
    """

    model_config = {"extra": "forbid"}

    utterance: str = Field(min_length=1, max_length=2000)
    session_key: str = Field(default="", max_length=128)


class ExecuteRequest(BaseModel):
    """A confirmed plan, named by intent rather than by operation.

    NOTE WHAT IS ABSENT: no `operation` field. A client cannot name an operation, so no request can reach an
    operation the resolver would not have chosen. `slots` are re-validated against the same vocabularies.
    """

    model_config = {"extra": "forbid"}

    intent: str = Field(min_length=1, max_length=64)
    slots: dict[str, Any] = Field(default_factory=dict)
    project_id: uuid.UUID
    session_key: str = Field(default="", max_length=128)


@router.get("/commands/catalogue")
async def read_catalogue(
    principal: Annotated[Principal, Depends(require_principal)],
) -> dict[str, Any]:
    """Every command that exists, for autocomplete and for inspection.

    Published so the closed set is verifiable from outside rather than asserted in a docstring: an operator
    can see exactly what natural language is able to resolve to.
    """
    return {
        "commands": catalogue(),
        "explanation": (
            "These are the only commands natural language can resolve to. The set is closed: an utterance "
            "that matches none of them is answered as a question and cannot cause a change. No command runs "
            "a command line."
        ),
    }


@router.post("/commands/interpret")
async def interpret(
    body: InterpretRequest,
    principal: Annotated[Principal, Depends(require_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """Classify and resolve. Changes nothing."""
    try:
        intent = classify(body.utterance)
        plan = resolve(intent)
    except CommandRefusedError as exc:
        # Recorded as refused, then refused. An operator asking "why did nothing happen" needs the row.
        await _record(
            session,
            utterance=body.utterance,
            session_key=body.session_key,
            intent="",
            outcome="refused",
            detail=str(exc),
            principal=principal,
        )
        await session.commit()
        raise problem("command-refused", detail=str(exc)) from exc

    await _record(
        session,
        utterance=body.utterance,
        session_key=body.session_key,
        intent=intent.name,
        outcome="interpreted",
        detail=plan.explanation,
        principal=principal,
    )
    await session.commit()

    return {
        "intent": plan.intent,
        "category": plan.category,
        "operation": plan.operation,
        "arguments": plan.arguments,
        "mutating": plan.mutating,
        "ready": plan.ready,
        "confidence": intent.confidence,
        "describes": plan.describes,
        "explanation": plan.explanation,
        # Stated explicitly so a client is never in doubt: interpreting did not do anything.
        "executed": False,
    }


@router.post("/commands/execute")
async def execute(
    body: ExecuteRequest,
    request: Request,
    principal: Annotated[Principal, Depends(require_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """Run a confirmed plan. The plan is REBUILT from the intent, never accepted as a structure."""
    if body.intent not in COMMANDS:
        raise problem(
            "command-refused",
            detail=(
                f"{body.intent!r} is not a command this system understands. The command set is closed, so an "
                "unrecognised intent is refused rather than attempted."
            ),
        )

    try:
        # REBUILT, not trusted. The client posted an intent and slots; the operation comes from the table.
        plan = resolve(Intent(name=body.intent, category=COMMANDS[body.intent].category, slots=body.slots))
    except CommandRefusedError as exc:
        raise problem("command-refused", detail=str(exc)) from exc

    if not plan.ready:
        raise problem(
            "command-incomplete",
            detail=(f"{plan.explanation} Nothing has been done: a missing value is asked for rather than guessed at."),
        )

    # LAYER 5: a mutating command is handed to the domain that owns it, which passes the chokepoint. This
    # route mints nothing and signs nothing -- it has no path to the agent of its own.
    if plan.mutating:
        await _record(
            session,
            utterance="",
            session_key=body.session_key,
            intent=plan.intent,
            outcome="dispatched",
            detail=f"{plan.operation} {plan.arguments}",
            principal=principal,
        )
        await session.commit()
        return {
            "intent": plan.intent,
            "operation": plan.operation,
            "arguments": plan.arguments,
            "dispatched": True,
            "explanation": (
                f"Dispatched as {plan.operation}. It now faces policy, approval, blast radius and audit like "
                "any other change of that kind -- the Command Center has no path to the agent of its own, "
                "which is why a mis-read sentence cannot become an unreviewed action."
            ),
        }

    await _record(
        session,
        utterance="",
        session_key=body.session_key,
        intent=plan.intent,
        outcome="read",
        detail=f"{plan.operation or 'answered locally'} {plan.arguments}",
        principal=principal,
    )
    await session.commit()
    return {
        "intent": plan.intent,
        "operation": plan.operation,
        "arguments": plan.arguments,
        "dispatched": False,
        "explanation": (
            "This command only reads, so nothing was changed. Use the panel for that resource to see the "
            "result -- the Command Center resolves the request and does not duplicate the display."
        ),
    }


@router.get("/commands/history")
async def history(
    principal: Annotated[Principal, Depends(require_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
    session_key: str = "",
    limit: int = 50,
) -> dict[str, Any]:
    """What was asked and what was understood, per session.

    THE REFUSALS ARE IN HERE. A user whose sentence was not understood needs to see that it was received and
    declined, with the reason -- otherwise "nothing happened" is indistinguishable from a broken button.
    """
    clause = "WHERE session_key = :key" if session_key else ""
    parameters: dict[str, Any] = {"limit": min(limit, 200)}
    if session_key:
        parameters["key"] = session_key

    rows = [
        dict(row)
        for row in (
            await session.execute(
                text(
                    "SELECT id, utterance, intent, outcome, detail, session_key, created_at "
                    f"FROM command_history {clause} ORDER BY created_at DESC LIMIT :limit"
                ),
                parameters,
            )
        ).mappings()
    ]
    return {
        "history": rows,
        "explanation": (
            f"{len(rows)} command(s) in this history, including any that were declined."
            if rows
            else (
                "Nothing has been asked yet. A declined command would appear here with its reason, so an "
                "empty history means no command was received rather than that one failed silently."
            )
        ),
    }


async def _record(
    session: AsyncSession,
    *,
    utterance: str,
    session_key: str,
    intent: str,
    outcome: str,
    detail: str,
    principal: Principal,
) -> None:
    """Append to history. Every interpretation, dispatch and refusal."""
    await session.execute(
        text(
            "INSERT INTO command_history "
            "(id, utterance, intent, outcome, detail, session_key, created_by) "
            "VALUES (:id, :utterance, :intent, :outcome, :detail, :key, :who)"
        ),
        {
            "id": uuid.uuid4(),
            "utterance": utterance[:2000],
            "intent": intent[:64],
            "outcome": outcome,
            "detail": detail[:4000],
            "key": session_key[:128],
            "who": principal.user_id,
        },
    )
