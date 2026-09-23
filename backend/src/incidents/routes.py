# SPDX-License-Identifier: FSL-1.1-ALv2
"""Incident and RCA routes. Phase 2 §2.11.

FOUR ROUTES, AND THREE OF THEM ARE READS. Listing incidents, reading one with its evidence and analyses, and
previewing a suggested fix mutate nothing and touch no change set.

THE FOURTH IS THE INTERESTING ONE. `POST .../suggestions/{id}/submit` turns a stored suggestion into a
governed change set -- and it is the only way a suggestion ever becomes a file on disk. It does not apply
anything: it submits to the chokepoint exactly as generation does, so the suggestion faces policy, approval,
blast radius and audit like any other mutation. A route that wrote the file directly would be an AI
suggestion editing a repository with no human in the path, which is the single worst thing this section could
ship.

THE PRE-IMAGE IS RE-READ AT SUBMISSION, not taken from the suggestion row. A suggestion records what the file
held when the analysis ran; by the time somebody acts on it the file may have moved on. Submitting the stale
pre-image would make the chokepoint's conflict detection compare against a state that no longer exists, and
the change would either be refused confusingly or applied over somebody else's edit. So the route reads the
file now, and `SuggestionPreview` tells the operator whether it still matches -- before they click.
"""

from __future__ import annotations

import difflib
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
from ..core.metrics_port import EvidenceRecord
from ..governance.chokepoint import ChangeItemRequest, GovernanceChokepoint, MutationRequest
from .analysis import analyse, persist
from .evidence import collect_log_evidence

router = APIRouter(tags=["incidents"])


class SuggestionSubmission(BaseModel):
    model_config = {"extra": "forbid"}

    environment: str = Field(min_length=1, max_length=64)
    # Required, not defaulted. A human has to say why they are applying an AI suggestion, and a blank
    # default would make every audit row read identically.
    reason: str = Field(min_length=10, max_length=500)


def _chokepoint(request: Request) -> GovernanceChokepoint:
    chokepoint = getattr(request.app.state, "governance_chokepoint", None)
    if chokepoint is None:
        raise problem(
            "incident-governance-absent",
            detail=(
                "The governance chokepoint is not composed, so a suggestion cannot be turned into a change "
                "set. Applying it outside governance is not offered as an alternative."
            ),
        )
    return chokepoint


@router.get("/projects/{project_id}/incidents")
async def list_incidents(
    project_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
    include_resolved: bool = False,
) -> dict[str, Any]:
    """The incident list, newest first, with the latest analysis state on each row.

    THE ANALYSIS STATE IS JOINED IN rather than left to the detail view, because a list that showed only
    titles would make an operator open every row to find which ones were diagnosed. `not_analysed` where
    there is no analysis row at all -- the LEFT JOIN's null is translated rather than rendered as blank.
    """
    rows = (
        await session.execute(
            text(
                """
                SELECT i.id, i.source, i.severity, i.title, i.occurrences,
                       i.detected_at, i.last_seen_at, i.resolved_at,
                       i.origin_kind, i.origin_id,
                       COALESCE(a.state, 'not_analysed') AS analysis_state,
                       a.evidence_reachable, a.evidence_consulted
                FROM incidents i
                LEFT JOIN LATERAL (
                    SELECT state, evidence_reachable, evidence_consulted
                    FROM incident_analyses
                    WHERE incident_id = i.id
                    ORDER BY created_at DESC
                    LIMIT 1
                ) a ON TRUE
                WHERE i.project_id = :project_id
                  AND (:include_resolved OR i.resolved_at IS NULL)
                ORDER BY i.last_seen_at DESC
                LIMIT 200
                """
            ),
            {"project_id": project_id, "include_resolved": include_resolved},
        )
    ).mappings()

    incidents = [dict(row) for row in rows]
    return {
        "incidents": incidents,
        # Stated so an empty list is never ambiguous. A panel receiving `[]` alone cannot tell "nothing has
        # failed" from "this project has never been observed", and those call for different responses.
        "explanation": (
            f"{len(incidents)} incident(s) recorded for this project."
            if incidents
            else (
                "No incidents have been recorded for this project. That means nothing has been INGESTED -- "
                "which is not the same as nothing having failed, if the sources that feed ingestion are not "
                "running."
            )
        ),
    }


@router.get("/incidents/{incident_id}")
async def read_incident(
    incident_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """One incident with every piece of evidence and every analysis.

    EVERY analysis, not the latest: a first attempt that reached two sources and a later one that reached
    five are both part of the record, and replacing the first would hide that the conclusion changed.
    """
    incident = (
        (
            await session.execute(
                text(
                    """
                SELECT id, project_id, source, severity, title, fingerprint, occurrences,
                       origin_kind, origin_id, detail, detected_at, last_seen_at, resolved_at
                FROM incidents WHERE id = :id
                """
                ),
                {"id": incident_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    if incident is None:
        raise problem("incident-absent", detail="No such incident.")

    evidence = [
        dict(row)
        for row in (
            await session.execute(
                text(
                    """
                    SELECT kind, reachable, summary, detail, collected_at
                    FROM incident_evidence WHERE incident_id = :id ORDER BY collected_at
                    """
                ),
                {"id": incident_id},
            )
        ).mappings()
    ]

    analyses = [
        dict(row)
        for row in (
            await session.execute(
                text(
                    """
                    SELECT id, state, problem, location, fix, evidence_reachable,
                           evidence_consulted, model, served_from, created_at
                    FROM incident_analyses WHERE incident_id = :id ORDER BY created_at DESC
                    """
                ),
                {"id": incident_id},
            )
        ).mappings()
    ]

    suggestions = [
        dict(row)
        for row in (
            await session.execute(
                text(
                    """
                    SELECT id, analysis_id, path, rationale, change_set_id, created_at,
                           length(proposed_content) AS proposed_bytes
                    FROM incident_fix_suggestions WHERE incident_id = :id ORDER BY created_at DESC
                    """
                ),
                {"id": incident_id},
            )
        ).mappings()
    ]

    unreachable = [record["kind"] for record in evidence if not record["reachable"]]
    return {
        "incident": dict(incident),
        "evidence": evidence,
        "analyses": analyses,
        "suggestions": suggestions,
        # The sentence the detail view renders above everything. Composed here so every client says the
        # same thing about the same condition.
        "evidence_caveat": (
            "Every evidence source consulted was readable."
            if evidence and not unreachable
            else (
                f"{len(unreachable)} of {len(evidence)} evidence sources could not be read "
                f"({', '.join(sorted(set(unreachable)))}). Any conclusion below was reached without them."
            )
            if evidence
            else (
                "No evidence has been collected for this incident, so nothing below has been analysed. "
                "The incident itself was observed and recorded."
            )
        ),
    }


@router.post("/incidents/{incident_id}/analysis")
async def analyse_incident(
    incident_id: uuid.UUID,
    request: Request,
    principal: Annotated[Principal, Depends(require_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """Collect evidence and analyse. THE PRODUCTION CALLER of the RCA pipeline.

    A POST because it writes evidence and analysis rows, but NOT a governed mutation: it changes nothing
    outside this domain\'s own bookkeeping, mints no agent command and touches no file. The chokepoint is
    for actions that alter the world; reading metrics and recording what was concluded is not one.

    IT DOES NOT REPLACE THE PREVIOUS ANALYSIS. Each run appends, so a first attempt that reached two
    sources and a later one that reached five are both in the record -- overwriting would hide that the
    conclusion changed, and which evidence changed it.

    A REPEAT IS PERMITTED AND CHEAP TO JUSTIFY: evidence that was unreachable an hour ago may be readable
    now, and that is exactly when re-analysing is worth doing.
    """
    incident = (
        (
            await session.execute(
                text(
                    "SELECT i.id, i.title, i.source, i.detail, i.origin_id, p.tenant_id "
                    "FROM incidents i JOIN projects p ON p.id = i.project_id WHERE i.id = :id"
                ),
                {"id": incident_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    if incident is None:
        raise problem("incident-absent", detail="No such incident.")

    findings: list[Any] = []

    # Metrics evidence through the shared catalogue, via the core Protocol. Absent when monitoring is not
    # composed, which is recorded as an unreachable source rather than skipped -- a skipped source would
    # not be counted against the evidence floor, and the floor is what stops a one-source conclusion.
    metrics_evidence = getattr(request.app.state, "metrics_evidence", None)
    if metrics_evidence is None:
        findings.append(
            EvidenceRecord(
                kind="metrics",
                reachable=False,
                summary=("The metrics tier is not composed in this deployment, so no metric series were consulted."),
            )
        )
    else:
        findings.extend(await metrics_evidence.incident_evidence(tenant_id=incident["tenant_id"]))

    # Logs. `None` when no log reader is composed, which `collect_log_evidence` renders as an unreachable
    # source with its reason rather than as an absence of errors.
    findings.append(
        await collect_log_evidence(
            getattr(request.app.state, "log_reader", None),
            service=str(incident["origin_id"] or "unknown"),
        )
    )

    analysis = await analyse(
        model=getattr(request.app.state, "artifact_model", None),
        title=str(incident["title"]),
        source=str(incident["source"]),
        detail=dict(incident["detail"] or {}),
        findings=findings,
    )
    analysis_id = await persist(session, incident_id=incident_id, findings=findings, analysis=analysis)
    await session.commit()

    return {
        "analysis_id": str(analysis_id),
        "state": analysis.state,
        "problem": analysis.problem,
        "location": analysis.location,
        "fix": analysis.fix,
        "evidence_reachable": analysis.evidence_reachable,
        "evidence_consulted": analysis.evidence_consulted,
        "model": analysis.model,
        # The sentence explaining the state, composed by the pipeline so the API and the panel never
        # disagree about what a state means.
        "explanation": analysis.explanation,
    }


@router.get("/incidents/{incident_id}/suggestions/{suggestion_id}/preview")
async def preview_suggestion(
    incident_id: uuid.UUID,
    suggestion_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """A unified diff of the suggestion against what the file held when it was made.

    THE DIFF IS RENDERED, NOT STORED. A stored diff can disagree with the file it claims to patch once the
    file moves on, and a preview that lies about the current state is worse than no preview -- a human
    approves a change believing it does one thing.

    Whether the file STILL holds what was observed is deliberately not answered here, because this route
    cannot read the working tree -- only the agent touches the filesystem. It is answered at apply time,
    where the agent recomputes the pre-image hash and aborts the set on a mismatch. The caveat says so
    rather than implying a freshness this route cannot establish.
    """
    row = (
        (
            await session.execute(
                text(
                    """
                SELECT path, proposed_content, observed_content, rationale, change_set_id
                FROM incident_fix_suggestions WHERE id = :id AND incident_id = :incident_id
                """
                ),
                {"id": suggestion_id, "incident_id": incident_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise problem("incident-suggestion-absent", detail="No such suggestion for this incident.")

    diff = list(
        difflib.unified_diff(
            (row["observed_content"] or "").splitlines(keepends=False),
            (row["proposed_content"] or "").splitlines(keepends=False),
            fromfile=f"a/{row['path']} (as observed when analysed)",
            tofile=f"b/{row['path']} (proposed)",
            lineterm="",
        )
    )
    return {
        "path": row["path"],
        "rationale": row["rationale"],
        "diff": diff,
        "already_submitted": row["change_set_id"] is not None,
        "change_set_id": str(row["change_set_id"]) if row["change_set_id"] else None,
        "caveat": (
            "This diff compares the proposal against what the file held WHEN THE ANALYSIS RAN, which may "
            "no longer be what it holds. That pre-image travels with the change set, and the agent "
            "recomputes the hash of the file it is about to write -- so if it has changed, the whole set "
            "is refused at apply time rather than overwriting somebody else's edit."
        ),
    }


@router.post("/incidents/{incident_id}/suggestions/{suggestion_id}/submit")
async def submit_suggestion(
    incident_id: uuid.UUID,
    suggestion_id: uuid.UUID,
    body: SuggestionSubmission,
    principal: Annotated[Principal, Depends(require_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
    chokepoint: Annotated[GovernanceChokepoint, Depends(_chokepoint)],
) -> dict[str, Any]:
    """Turn a suggestion into a governed change set. The only path from suggestion to file.

    Submits through the chokepoint exactly as generation does, so the suggestion faces policy, approval,
    blast radius and audit. Nothing is written here.
    """
    row = (
        (
            await session.execute(
                text(
                    """
                SELECT s.path, s.proposed_content, s.observed_content, s.change_set_id, i.project_id
                FROM incident_fix_suggestions s
                JOIN incidents i ON i.id = s.incident_id
                WHERE s.id = :id AND s.incident_id = :incident_id
                """
                ),
                {"id": suggestion_id, "incident_id": incident_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise problem("incident-suggestion-absent", detail="No such suggestion for this incident.")

    if row["change_set_id"] is not None:
        # REFUSED rather than creating a second change set. Two change sets proposing the same edit would
        # both pass approval and the second would conflict with the first, which presents to an operator as
        # an unexplained refusal of their own change.
        raise problem(
            "incident-suggestion-already-submitted",
            detail=(
                f"This suggestion is already change set {row['change_set_id']}. Act on that one rather "
                "than creating a second proposing the same edit."
            ),
        )

    submission = await chokepoint.submit(
        session,
        MutationRequest(
            project_id=row["project_id"],
            items=(
                ChangeItemRequest(
                    file_path=row["path"],
                    # `create` when the analysis saw no file, `update` when it did. The distinction is
                    # not cosmetic: `update` REQUIRES the pre-image, and that is the protection.
                    action="update" if row["observed_content"] else "create",
                    # THE PRE-IMAGE IS WHAT THE ANALYSIS SAW, passed deliberately rather than re-read.
                    # It becomes `change_items.old_hash`, and the agent recomputes the hash of the file
                    # it is about to write and ABORTS THE WHOLE SET if they disagree. So a suggestion
                    # acted on after the file moved on is refused at apply time by the component that
                    # can actually see the file -- which is stronger than this route re-reading, since
                    # only the agent touches the filesystem and anything read here could go stale
                    # between the read and the apply.
                    old_content=row["observed_content"] or None,
                    new_content=row["proposed_content"],
                ),
            ),
            reason=f"incident {incident_id} suggested fix: {body.reason}",
            # `manual`, not a new origin value. A human read the diff and submitted, which is what
            # `manual` means -- and `approve()` branches on origin, so a fourth value risks an unhandled
            # branch for no gain. The AI provenance is not lost: it is
            # `incident_fix_suggestions.change_set_id`, a foreign key, which is a better record than an
            # enum string because it joins back to the analysis and the evidence behind it.
            origin="manual",
            environment=body.environment,
        ),
        principal=principal,
    )

    change_set_id = getattr(submission, "change_set_id", None)
    await session.execute(
        text("UPDATE incident_fix_suggestions SET change_set_id = :cs WHERE id = :id"),
        {"cs": change_set_id, "id": suggestion_id},
    )
    await session.commit()

    return {
        "change_set_id": str(change_set_id) if change_set_id else None,
        "explanation": (
            "The suggestion is now a change set awaiting the same policy, approval and audit as any other "
            "mutation. Nothing has been written to the working tree."
        ),
    }
