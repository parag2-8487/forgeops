# SPDX-License-Identifier: FSL-1.1-ALv2
"""Learning history routes. Phase 2 §2.13.

THE EDITING ROUTES ARE THE POINT OF THIS FILE, not an afterthought.

A preference store shapes every future prompt. One the user cannot see is one they cannot disagree with, and
a system that cannot be corrected gets quietly worse with every wrong inference -- so `PATCH` and `DELETE` on
a preference are as important as the reflection that creates them, and `POST /preferences` lets a user state
one directly without waiting for the system to infer it.

DEACTIVATION IS OFFERED ALONGSIDE DELETION, and they mean different things. Deactivating stops a preference
being injected while keeping the record that the system once inferred it -- which is what a user wants when
the inference was reasonable but unwanted. Deleting removes it. Offering only deletion would force the user
to destroy the evidence in order to stop acting on it.

WHY `GET .../skill-file` EXISTS SEPARATELY FROM THE PREFERENCE LIST. They answer different questions: the
list is what the system believes, the skill file is what actually reached the last prompt. Those differ
whenever the budget cut something, and a user debugging "why did it ignore my preference" needs the second.

These are all reads and writes of this domain's own bookkeeping. Nothing here mutates infrastructure, mints
an agent command or compiles a change set, so the chokepoint is not involved -- the same reasoning as the
incident analysis route.
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
from .models import FEEDBACK_VERDICTS, PREFERENCE_SCOPES
from .reflector import (
    MAX_SESSION_TURNS,
    compile_skill_file,
    reflect,
    scope_for_path,
    statement_digest,
)

router = APIRouter(tags=["learning"])


class FeedbackRequest(BaseModel):
    model_config = {"extra": "forbid"}

    artifact_path: str = Field(min_length=1, max_length=1024)
    verdict: str
    generation_run_id: uuid.UUID | None = None
    comment: str = Field(default="", max_length=4000)
    #: Required for `edited`, and the constraint enforces it. The difference between what was produced and
    #: what was kept is the highest-signal thing this whole feature collects.
    final_content: str = ""


class PreferenceRequest(BaseModel):
    model_config = {"extra": "forbid"}

    scope: str
    statement: str = Field(min_length=1, max_length=4000)


class PreferenceUpdate(BaseModel):
    """A correction. Both fields optional so a user can retext without reactivating, or vice versa."""

    model_config = {"extra": "forbid"}

    statement: str | None = Field(default=None, min_length=1, max_length=4000)
    active: bool | None = None


class TurnRequest(BaseModel):
    model_config = {"extra": "forbid"}

    session_key: str = Field(min_length=1, max_length=128)
    role: str
    content: str = Field(min_length=1, max_length=20_000)


@router.post("/projects/{project_id}/learning/feedback")
async def record_feedback(
    project_id: uuid.UUID,
    body: FeedbackRequest,
    principal: Annotated[Principal, Depends(require_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """Record what a human did with an artifact. Append-only."""
    if body.verdict not in FEEDBACK_VERDICTS:
        raise problem(
            "learning-verdict-invalid",
            detail=f"verdict must be one of {list(FEEDBACK_VERDICTS)}.",
        )
    if body.verdict == "edited" and not body.final_content.strip():
        raise problem(
            "learning-verdict-invalid",
            detail=(
                "an 'edited' verdict must carry the content that was kept. Recording that something changed "
                "without recording what it became discards the only part a preference can be derived from."
            ),
        )

    feedback_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO learning_feedback "
            "(id, project_id, generation_run_id, artifact_path, verdict, comment, final_content, created_by) "
            "VALUES (:id, :p, :run, :path, :verdict, :comment, :final, :who)"
        ),
        {
            "id": feedback_id,
            "p": project_id,
            "run": body.generation_run_id,
            "path": body.artifact_path,
            "verdict": body.verdict,
            "comment": body.comment,
            "final": body.final_content,
            "who": principal.user_id,
        },
    )
    await session.commit()
    return {
        "feedback_id": str(feedback_id),
        "inferred_scope": scope_for_path(body.artifact_path),
        "explanation": (
            "Recorded. Nothing has been inferred from it yet: reflection is a separate step, and a single "
            "event is below the evidence floor in any case."
        ),
    }


@router.post("/projects/{project_id}/learning/reflect")
async def run_reflection(
    project_id: uuid.UUID,
    request: Request,
    principal: Annotated[Principal, Depends(require_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """Synthesise preferences from recorded feedback. The Reflector's production caller."""
    result = await reflect(
        session,
        project_id=project_id,
        model=getattr(request.app.state, "artifact_model", None),
    )
    await session.commit()
    return {
        "state": result.state,
        "preferences_written": result.preferences_written,
        "preferences_strengthened": result.preferences_strengthened,
        "feedback_considered": result.feedback_considered,
        "explanation": result.explanation,
    }


@router.get("/projects/{project_id}/learning/preferences")
async def list_preferences(
    project_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """Everything the system believes about this project, active or not.

    INACTIVE ONES ARE INCLUDED. A user who switched a preference off needs to see that it is still there and
    still off; hiding it would make the list look like the system had forgotten, and the next reflection pass
    finding the same statement would then look like it had learned something new.
    """
    rows = [
        dict(row)
        for row in (
            await session.execute(
                text(
                    "SELECT id, scope, statement, source, evidence_count, active, created_at, updated_at "
                    "FROM learning_preferences WHERE project_id = :p "
                    "ORDER BY (source = 'stated') DESC, evidence_count DESC, created_at"
                ),
                {"p": project_id},
            )
        ).mappings()
    ]
    active = [row for row in rows if row["active"]]
    return {
        "preferences": rows,
        "scopes": list(PREFERENCE_SCOPES),
        "explanation": (
            f"{len(active)} active preference(s) of {len(rows)} recorded."
            + (
                " A preference written by a person outranks an inferred one and is never rewritten by reflection."
                if any(row["source"] == "stated" for row in rows)
                else ""
            )
            if rows
            else (
                "Nothing has been learned about this project yet. Prompts for it are identical to those for "
                "a project with no history -- which is the honest state before any feedback exists."
            )
        ),
    }


@router.post("/projects/{project_id}/learning/preferences")
async def state_preference(
    project_id: uuid.UUID,
    body: PreferenceRequest,
    principal: Annotated[Principal, Depends(require_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """State a preference directly. Needs no model and no feedback."""
    if body.scope not in PREFERENCE_SCOPES:
        raise problem(
            "learning-scope-invalid",
            detail=(
                f"scope must be one of {list(PREFERENCE_SCOPES)}. An unrecognised scope is refused rather "
                "than filed as 'general', because a misfiled preference is injected into prompts it has "
                "nothing to do with."
            ),
        )

    digest = statement_digest(body.statement)
    existing = (
        await session.execute(
            text(
                "SELECT id FROM learning_preferences "
                "WHERE project_id = :p AND scope = :scope AND statement_digest = :digest"
            ),
            {"p": project_id, "scope": body.scope, "digest": digest},
        )
    ).scalar_one_or_none()

    if existing is not None:
        # PROMOTED to `stated` and reactivated. A user restating a preference the system had inferred, or one
        # they had switched off, is unambiguously asking for it -- so this is the one path that may set
        # `active` back to true.
        await session.execute(
            text(
                "UPDATE learning_preferences SET source = 'stated', active = true, statement = :statement, "
                "updated_at = now() WHERE id = :id"
            ),
            {"id": existing, "statement": body.statement},
        )
        await session.commit()
        return {
            "preference_id": str(existing),
            "explanation": (
                "This preference already existed, so it has been promoted to one you stated and switched "
                "back on. Reflection can strengthen it from now on but will not rewrite it."
            ),
        }

    preference_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO learning_preferences "
            "(id, project_id, scope, statement, statement_digest, source, evidence_count) "
            "VALUES (:id, :p, :scope, :statement, :digest, 'stated', 0)"
        ),
        {
            "id": preference_id,
            "p": project_id,
            "scope": body.scope,
            "statement": body.statement,
            "digest": digest,
        },
    )
    await session.commit()
    return {
        "preference_id": str(preference_id),
        "explanation": (
            "Recorded as stated by you. Its evidence count is zero because no feedback produced it, which is "
            "not a weakness: a preference you wrote outranks an inferred one regardless of counts."
        ),
    }


@router.patch("/learning/preferences/{preference_id}")
async def correct_preference(
    preference_id: uuid.UUID,
    body: PreferenceUpdate,
    principal: Annotated[Principal, Depends(require_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """Correct or deactivate a preference. The feature that makes this store trustworthy."""
    if body.statement is None and body.active is None:
        raise problem(
            "learning-preference-invalid",
            detail="the request changes nothing: supply a statement, an active flag, or both.",
        )

    existing = (
        (
            await session.execute(
                text("SELECT id, source FROM learning_preferences WHERE id = :id"),
                {"id": preference_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    if existing is None:
        raise problem("learning-preference-absent", detail="No such preference.")

    if body.statement is not None:
        # EDITING PROMOTES IT TO `stated`, because the text is now the user's. Leaving it `reflected` would
        # let the next reflection pass treat it as its own and overwrite the correction.
        await session.execute(
            text(
                "UPDATE learning_preferences SET statement = :statement, statement_digest = :digest, "
                "source = 'stated', updated_at = now() WHERE id = :id"
            ),
            {"id": preference_id, "statement": body.statement, "digest": statement_digest(body.statement)},
        )
    if body.active is not None:
        await session.execute(
            text("UPDATE learning_preferences SET active = :active, updated_at = now() WHERE id = :id"),
            {"id": preference_id, "active": body.active},
        )
    await session.commit()

    return {
        "preference_id": str(preference_id),
        "explanation": (
            (
                "Rewritten, and now recorded as stated by you so reflection cannot overwrite it. "
                if body.statement is not None
                else ""
            )
            + (
                (
                    "It will be injected into future prompts."
                    if body.active
                    else "It will no longer be injected, and is kept so you can see it was once inferred."
                )
                if body.active is not None
                else ""
            )
        ).strip(),
    }


@router.delete("/learning/preferences/{preference_id}")
async def forget_preference(
    preference_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """Delete a preference outright.

    Offered alongside deactivation because they mean different things -- see the module docstring. The
    feedback that produced it is NOT deleted: that is the evidence, and removing a conclusion should not
    remove the observations behind it.
    """
    result = await session.execute(
        text("DELETE FROM learning_preferences WHERE id = :id RETURNING id"), {"id": preference_id}
    )
    if result.first() is None:
        raise problem("learning-preference-absent", detail="No such preference.")
    await session.commit()
    return {
        "explanation": (
            "Deleted. The feedback events it was derived from are kept: removing a conclusion should not "
            "remove the observations behind it, and a future reflection may reach a different one."
        )
    }


@router.get("/projects/{project_id}/learning/skill-file")
async def read_skill_file(
    project_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """What would actually be injected, and what was left out for budget.

    Compiled fresh rather than read from the last stored row, because the question is "what would a prompt
    get now" -- a stale answer would be worse than none for a user debugging why their preference had no
    effect.
    """
    skill_file = await compile_skill_file(session, project_id=project_id)
    excluded = []
    if skill_file.excluded:
        excluded = [
            dict(row)
            for row in (
                await session.execute(
                    text("SELECT id, scope, statement, evidence_count FROM learning_preferences WHERE id = ANY(:ids)"),
                    {"ids": skill_file.excluded},
                )
            ).mappings()
        ]
    return {
        "content": skill_file.content,
        "included_preference_ids": [str(one) for one in skill_file.included],
        "excluded_preferences": excluded,
        "explanation": skill_file.explanation,
    }


@router.get("/projects/{project_id}/learning/history")
async def read_history(
    project_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: int = 100,
) -> dict[str, Any]:
    """The raw feedback record, and every skill file that was injected.

    Both halves, because they answer "what did I tell it" and "what did it use" -- and the gap between them
    is where a user's confusion about why a preference had no effect actually lives.
    """
    feedback = [
        dict(row)
        for row in (
            await session.execute(
                text(
                    "SELECT id, generation_run_id, artifact_path, verdict, comment, created_at, "
                    "length(final_content) AS final_content_bytes FROM learning_feedback "
                    "WHERE project_id = :p ORDER BY created_at DESC LIMIT :limit"
                ),
                {"p": project_id, "limit": min(limit, 500)},
            )
        ).mappings()
    ]
    injections = [
        dict(row)
        for row in (
            await session.execute(
                text(
                    "SELECT id, content, included_preference_ids, excluded_preference_ids, created_at "
                    "FROM learning_skill_files WHERE project_id = :p ORDER BY created_at DESC LIMIT 20"
                ),
                {"p": project_id},
            )
        ).mappings()
    ]
    return {
        "feedback": feedback,
        "injections": injections,
        "explanation": (
            f"{len(feedback)} feedback event(s) and {len(injections)} recorded injection(s)."
            if feedback or injections
            else (
                "No feedback has been recorded and nothing has been injected. The system has not been told "
                "anything about this project, which is different from having been told and ignoring it."
            )
        ),
    }


@router.post("/projects/{project_id}/learning/turns")
async def record_turn(
    project_id: uuid.UUID,
    body: TurnRequest,
    principal: Annotated[Principal, Depends(require_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """Append a conversation turn to short-term memory, trimming to the bound.

    TRIMMED AT THE WRITE SITE rather than by a periodic sweep, so the bound holds at every moment rather
    than on average -- a sweep leaves the window unbounded between runs, which is exactly when a long
    conversation would blow a prompt budget.
    """
    if body.role not in ("user", "assistant"):
        raise problem("learning-turn-invalid", detail="role must be 'user' or 'assistant'.")

    next_turn = (
        await session.execute(
            text("SELECT COALESCE(max(turn), -1) + 1 FROM learning_sessions WHERE session_key = :key"),
            {"key": body.session_key},
        )
    ).scalar_one()

    await session.execute(
        text(
            "INSERT INTO learning_sessions (id, project_id, session_key, turn, role, content) "
            "VALUES (:id, :p, :key, :turn, :role, :content)"
        ),
        {
            "id": uuid.uuid4(),
            "p": project_id,
            "key": body.session_key,
            "turn": next_turn,
            "role": body.role,
            "content": body.content,
        },
    )
    await session.execute(
        text("DELETE FROM learning_sessions WHERE session_key = :key AND turn <= :cutoff"),
        {"key": body.session_key, "cutoff": next_turn - MAX_SESSION_TURNS},
    )
    await session.commit()
    return {
        "turn": int(next_turn),
        "explanation": (
            f"Recorded as turn {next_turn}. Short-term memory keeps the most recent "
            f"{MAX_SESSION_TURNS} turns and is never promoted to a preference: thinking aloud in a "
            "conversation is not stating a standing preference."
        ),
    }
