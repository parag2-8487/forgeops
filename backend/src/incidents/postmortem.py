# SPDX-License-Identifier: FSL-1.1-ALv2
"""Post-incident summaries and long-term recommendations. Phase 2 §2.12.

TWO OUTPUTS WITH DIFFERENT AUDIENCES, which is why they are separate fields rather than one block of prose.
A summary is read once, during review, by somebody reconstructing what happened. A recommendation is acted on
later, by somebody deciding what to change. Merging them produces a document where the actionable part is
buried in narrative.

THE SAME REFUSALS AS THE RCA PIPELINE, for the same reason: a fabricated postmortem is worse than none,
because it becomes the record. No model means `unavailable`, not an empty summary. An unparseable answer means
`insufficient`, not the raw text dropped into the summary field. The database enforces both
(`ck_incident_postmortems_generated_has_content` and its converse), so the honesty survives a future caller.

WHAT THE PROMPT IS GIVEN, and the one thing it is NOT. It gets the incident, the analysis, and every healing
action including the refused ones -- because "the system declined to act, three times, for these reasons" is
the most useful sentence a postmortem can contain and it is invisible without the refusals. It is NOT given
permission to recommend an action; recommendations are prose for a human, and nothing here turns one into a
remedy. A recommendation that could execute itself would be a self-healing system that rewrites its own
guard rails.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.model_port import ArtifactModelPort
from ..secrets.redaction import create_redacted_prompt

_SUMMARY = re.compile(r"^###\s*SUMMARY\s*$(.*?)(?=^###|\Z)", re.MULTILINE | re.DOTALL)
_RECOMMENDATIONS = re.compile(r"^###\s*RECOMMENDATIONS\s*$(.*?)(?=^###|\Z)", re.MULTILINE | re.DOTALL)


@dataclass(frozen=True, slots=True)
class Postmortem:
    state: str
    summary: str = ""
    recommendations: list[str] = field(default_factory=list)
    model: str = ""
    actions_considered: int = 0
    explanation: str = ""


def build_prompt(
    *,
    title: str,
    source: str,
    analysis_state: str,
    problem: str,
    actions: list[dict[str, Any]],
) -> str:
    """The postmortem prompt.

    The REFUSED actions are included and labelled as refusals. A postmortem that listed only what ran would
    make an incident where every remedy was declined look like an incident nobody responded to.
    """
    lines = [
        "Write a post-incident record. Answer ONLY in the format below.",
        "",
        "### SUMMARY",
        "two to four sentences: what happened, what was done, and how it ended",
        "### RECOMMENDATIONS",
        "one per line, each starting with '- ', naming a change that would reduce the chance of recurrence.",
        "Recommend nothing you cannot support from the record below. If the record does not support any",
        "recommendation, write '- none supported by this record'.",
        "",
        f"INCIDENT: {title}",
        f"OBSERVED BY: {source}",
        f"ANALYSIS STATE: {analysis_state}",
    ]
    if problem:
        lines.append(f"IDENTIFIED CAUSE: {problem}")
    else:
        lines.append("IDENTIFIED CAUSE: none was identified. Do not infer one; describe what was observed instead.")

    lines.append("")
    if actions:
        lines.append(f"AUTOMATED RESPONSE ({len(actions)} action(s), including ones that were declined):")
        for action in actions:
            verdict = action.get("state")
            prefix = "DECLINED" if verdict == "refused" else str(verdict).upper()
            lines.append(
                f"  - [{prefix}] {action.get('remedy')} "
                f"({'automatic' if action.get('auto') else 'required approval'}): {action.get('note', '')}"
            )
    else:
        lines.append("AUTOMATED RESPONSE: none. No healing action was attempted for this incident.")
    return "\n".join(lines)


async def generate(
    *,
    model: ArtifactModelPort | None,
    title: str,
    source: str,
    analysis_state: str,
    problem: str,
    actions: list[dict[str, Any]],
) -> Postmortem:
    """Produce a postmortem, or a stated reason there is none."""
    considered = len(actions)

    if model is None:
        return Postmortem(
            state="unavailable",
            actions_considered=considered,
            explanation=(
                "No model is configured, so no summary was written. The incident, its evidence and every "
                "healing action are recorded and readable directly; what is missing is the narrative."
            ),
        )

    prompt = create_redacted_prompt(
        build_prompt(
            title=title,
            source=source,
            analysis_state=analysis_state,
            problem=problem,
            actions=actions,
        )
    )
    completion = await model.complete(prompt=prompt, store_in_cache=False)
    if not completion.ok or not completion.content:
        return Postmortem(
            state="unavailable",
            actions_considered=considered,
            explanation=(
                "The model could not be reached, so no summary was written: "
                + "; ".join(completion.failure_reasons[:2])
            ),
        )

    summary_match = _SUMMARY.search(completion.content)
    summary = summary_match.group(1).strip()[:8000] if summary_match else ""
    if not summary:
        return Postmortem(
            state="insufficient",
            model=completion.endpoint_id or "",
            actions_considered=considered,
            explanation=(
                "The model answered in a form this pipeline could not parse into a summary, so nothing has "
                "been recorded as one. Its reply was: " + completion.content[:600]
            ),
        )

    recommendations = _parse_recommendations(completion.content)
    await model.remember(prompt=prompt, content=completion.content)
    return Postmortem(
        state="generated",
        summary=summary,
        recommendations=recommendations,
        model=completion.endpoint_id or "",
        actions_considered=considered,
        explanation=f"Written from the incident, its analysis and {considered} recorded action(s).",
    )


def _parse_recommendations(content: str) -> list[str]:
    """Bullet lines, or an empty list.

    An empty list is a legitimate outcome and is DISTINCT from the model's own "none supported by this
    record", which is filtered out here: carrying that sentence through as a recommendation would put a
    non-recommendation in a list an operator scans for things to do.
    """
    match = _RECOMMENDATIONS.search(content)
    if match is None:
        return []
    out: list[str] = []
    for raw in match.group(1).splitlines():
        line = raw.strip()
        if not line.startswith("-"):
            continue
        text_value = line.lstrip("-").strip()
        if not text_value:
            continue
        if text_value.lower().startswith("none supported"):
            continue
        out.append(text_value[:1000])
    return out[:20]


async def persist(session: AsyncSession, *, incident_id: uuid.UUID, postmortem: Postmortem) -> uuid.UUID:
    """Write the postmortem. Appends rather than replacing, like the analyses."""
    import json

    postmortem_id = uuid.uuid4()
    await session.execute(
        text(
            """
            INSERT INTO incident_postmortems (
                id, incident_id, state, summary, recommendations, model, actions_considered
            )
            VALUES (
                :id, :incident_id, :state, :summary, CAST(:recommendations AS jsonb), :model, :considered
            )
            """
        ),
        {
            "id": postmortem_id,
            "incident_id": incident_id,
            "state": postmortem.state,
            "summary": postmortem.summary,
            "recommendations": json.dumps(postmortem.recommendations),
            "model": postmortem.model[:128],
            "considered": postmortem.actions_considered,
        },
    )
    return postmortem_id
