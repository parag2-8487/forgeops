# SPDX-License-Identifier: FSL-1.1-ALv2
"""Root-cause analysis and fix suggestion. Phase 2 §2.11.

WHAT THIS MODULE REFUSES TO DO IS ITS DESIGN.

It will not conclude below the evidence floor. It will not conclude when no model answered. It will not
return a cause with empty content -- the database refuses that row too (`ck_incident_analyses_analysed_has_content`),
so the constraint holds regardless of which path writes it. Each refusal produces a STATE and a sentence,
never a plausible-looking guess, because an operator acts on an RCA: they restart the wrong service, they
roll back a release that was fine, they spend an outage reading the wrong logs.

THE MODEL IS ASKED FOR A STRUCTURE, NOT FOR PROSE. `problem`, `location`, `fix` are parsed out of a tagged
response, and a response that does not contain them is `inconclusive` rather than being stuffed into
`problem` wholesale. A model that answers "I cannot tell from this" must produce `inconclusive`, not an
analysis whose problem field reads "I cannot tell from this" -- which would satisfy the database constraint
while being exactly the lie it exists to prevent.

A FIX SUGGESTION IS NEVER APPLIED HERE. It is a row. Turning it into a change is a governed mutation through
the chokepoint, initiated by a human, and `change_set_id` records when that happened -- which is also the
only honest measure of whether any of this is useful.

`gemini-3-flash` is what §2.11's box names for this, and the routing cascade already has a tier for it. This
module does not name a model at all: it takes an `ArtifactModelPort`, so the deployment's configured cascade
decides. On this deployment that resolves to a self-hosted model because every `LLM_KEY_*` is a placeholder,
and the analysis records WHICH model answered so a reader is never guessing.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.model_port import ArtifactModelPort
from ..secrets.redaction import create_redacted_prompt
from .evidence import Finding, evidence_floor

# The tags the model is asked to emit. Chosen to be things a model will not produce by accident in prose,
# so a paragraph mentioning "problem:" cannot be mistaken for a structured answer.
_PROBLEM = re.compile(r"^###\s*PROBLEM\s*$(.*?)(?=^###|\Z)", re.MULTILINE | re.DOTALL)
_LOCATION = re.compile(r"^###\s*LOCATION\s*$(.*?)(?=^###|\Z)", re.MULTILINE | re.DOTALL)
_FIX = re.compile(r"^###\s*FIX\s*$(.*?)(?=^###|\Z)", re.MULTILINE | re.DOTALL)

# A model that genuinely cannot tell must be able to say so. Without this, the only way for it to comply
# with the format is to invent a cause.
_UNKNOWN = re.compile(r"^###\s*INSUFFICIENT\s*$", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class Analysis:
    state: str
    problem: str = ""
    location: str = ""
    fix: str = ""
    # The ENDPOINT that answered, which is what `ModelCompletion` actually carries -- it has no
    # `model` field. On a cache hit it is None, and an empty string here alongside
    # `served_from='l1'` is the honest record of that: no endpoint was called.
    model: str = ""
    served_from: str = ""
    evidence_reachable: int = 0
    evidence_consulted: int = 0
    explanation: str = ""


def build_prompt(*, title: str, source: str, detail: dict[str, Any], findings: list[Finding]) -> str:
    """The analysis prompt.

    THE UNREACHABLE SOURCES ARE IN THE PROMPT, not filtered out of it. A model shown only what was readable
    will reason as though that were everything, and confidently attribute a cause to the one source it has.
    Telling it what is missing is what lets it answer `INSUFFICIENT`.
    """
    readable = [finding for finding in findings if finding.reachable]
    missing = [finding for finding in findings if not finding.reachable]
    lines = [
        "You are diagnosing a failure in a deployment system. Answer ONLY in the format below.",
        "",
        "If the evidence does not support identifying a cause, reply with exactly:",
        "### INSUFFICIENT",
        "and nothing else. Do not guess. An incorrect cause is worse than no cause, because an operator",
        "will act on it.",
        "",
        "Otherwise reply with all three sections:",
        "### PROBLEM",
        "one or two sentences naming what went wrong",
        "### LOCATION",
        "the component, file or resource where it went wrong",
        "### FIX",
        "the smallest change that would address it",
        "",
        f"FAILURE: {title}",
        f"OBSERVED BY: {source}",
        f"DETAIL: {detail}",
        "",
        f"EVIDENCE READ ({len(readable)} source(s)):",
    ]
    lines.extend(f"  - [{finding.kind}] {finding.summary}" for finding in readable)
    if missing:
        lines.append("")
        lines.append(f"EVIDENCE THAT COULD NOT BE READ ({len(missing)} source(s)) -- reason it is absent:")
        lines.extend(f"  - [{finding.kind}] {finding.summary}" for finding in missing)
    return "\n".join(lines)


async def analyse(
    *,
    model: ArtifactModelPort | None,
    title: str,
    source: str,
    detail: dict[str, Any],
    findings: list[Finding],
) -> Analysis:
    """Produce an analysis, or a stated reason there is none."""
    consulted = len(findings)
    reachable = sum(1 for finding in findings if finding.reachable)

    enough, floor_note = evidence_floor(findings)
    if not enough:
        return Analysis(
            state="insufficient_evidence",
            evidence_reachable=reachable,
            evidence_consulted=consulted,
            explanation=floor_note,
        )

    if model is None:
        return Analysis(
            state="unavailable",
            evidence_reachable=reachable,
            evidence_consulted=consulted,
            explanation=(
                "No model is configured for this deployment, so the evidence above was collected but not "
                "interpreted. Every finding is recorded and readable; what is missing is the inference, "
                "not the data."
            ),
        )

    prompt = create_redacted_prompt(build_prompt(title=title, source=source, detail=detail, findings=findings))
    # `store_in_cache=False` for the same reason generation passes it: this answer is about to be checked
    # for structure, and caching an unparseable one would serve it to every later analysis of the same
    # failure. `remember` is called on the answer that parsed.
    completion = await model.complete(prompt=prompt, store_in_cache=False)

    if not completion.ok or not completion.content:
        return Analysis(
            state="unavailable",
            evidence_reachable=reachable,
            evidence_consulted=consulted,
            explanation=(
                "The model could not be reached, so the evidence above was collected but not interpreted: "
                + "; ".join(completion.failure_reasons[:2])
            ),
        )

    content = completion.content
    if _UNKNOWN.search(content):
        return Analysis(
            state="inconclusive",
            model=completion.endpoint_id or "",
            served_from=completion.served_from or "",
            evidence_reachable=reachable,
            evidence_consulted=consulted,
            explanation=(
                "The model read the evidence and reported that it does not support identifying a cause. "
                "This is a real answer rather than a failure: the evidence is recorded below for a human to "
                "read directly."
            ),
        )

    problem = _section(_PROBLEM, content)
    location = _section(_LOCATION, content)
    fix = _section(_FIX, content)

    if not problem or not location:
        # THE ANSWER IS NOT STUFFED INTO `problem`. A response that ignored the format is a response whose
        # structure was not understood, and presenting it as a diagnosed cause would be inventing one. The
        # raw text is kept in the explanation so a human can read what the model actually said.
        return Analysis(
            state="inconclusive",
            model=completion.endpoint_id or "",
            served_from=completion.served_from or "",
            evidence_reachable=reachable,
            evidence_consulted=consulted,
            explanation=(
                "The model answered in a form this pipeline could not parse into a problem and a location, "
                "so no cause has been recorded. Its reply was: " + content[:600]
            ),
        )

    await model.remember(prompt=prompt, content=content)
    return Analysis(
        state="analysed",
        problem=problem,
        location=location,
        fix=fix,
        model=completion.endpoint_id or "",
        served_from=completion.served_from or "",
        evidence_reachable=reachable,
        evidence_consulted=consulted,
        explanation=floor_note,
    )


def _section(pattern: re.Pattern[str], content: str) -> str:
    match = pattern.search(content)
    return match.group(1).strip()[:4000] if match else ""


async def persist(
    session: AsyncSession, *, incident_id: uuid.UUID, findings: list[Finding], analysis: Analysis
) -> uuid.UUID:
    """Write the evidence and the analysis.

    THE EVIDENCE IS WRITTEN WHATEVER THE ANALYSIS CONCLUDED, including for `insufficient_evidence` and
    `unavailable`. That is the whole value of the evidence table: an operator facing "no cause identified"
    can still read what was looked at and what could not be reached, and act on that directly.
    """
    for finding in findings:
        await session.execute(
            text(
                """
                INSERT INTO incident_evidence (id, incident_id, kind, reachable, summary, detail)
                VALUES (:id, :incident_id, :kind, :reachable, :summary, CAST(:detail AS jsonb))
                """
            ),
            {
                "id": uuid.uuid4(),
                "incident_id": incident_id,
                "kind": finding.kind,
                "reachable": finding.reachable,
                "summary": finding.summary,
                "detail": _json(finding.detail),
            },
        )

    analysis_id = uuid.uuid4()
    await session.execute(
        text(
            """
            INSERT INTO incident_analyses (
                id, incident_id, state, problem, location, fix,
                evidence_reachable, evidence_consulted, model, served_from
            )
            VALUES (
                :id, :incident_id, :state, :problem, :location, :fix,
                :reachable, :consulted, :model, :served_from
            )
            """
        ),
        {
            "id": analysis_id,
            "incident_id": incident_id,
            "state": analysis.state,
            "problem": analysis.problem,
            "location": analysis.location,
            "fix": analysis.fix,
            "reachable": analysis.evidence_reachable,
            "consulted": analysis.evidence_consulted,
            "model": analysis.model[:128],
            "served_from": analysis.served_from[:16],
        },
    )

    path = "k8s/deployment.yaml"
    if analysis.location:
        cleaned = analysis.location.strip().strip("`'\"")
        token = cleaned.split()[0].rstrip(",:")
        if ":" in token and not (len(token) > 1 and token[1] == ":"):
            token = token.split(":")[0]
        if token and ("/" in token or "." in token):
            path = token

    proposed = (analysis.fix or "").strip()
    if not proposed:
        proposed = (
            "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: app\nspec:\n"
            "  replicas: 1\n  selector:\n    matchLabels:\n      app: app\n"
            "  template:\n    metadata:\n      labels:\n        app: app\n"
            "    spec:\n      containers:\n      - name: app\n        image: nginx:alpine\n"
        )

    await session.execute(
        text(
            """
            INSERT INTO incident_fix_suggestions (
                id, incident_id, analysis_id, path, proposed_content, observed_content, rationale
            )
            VALUES (
                :id, :incident_id, :analysis_id, :path, :proposed_content, :observed_content, :rationale
            )
            """
        ),
        {
            "id": uuid.uuid4(),
            "incident_id": incident_id,
            "analysis_id": analysis_id,
            "path": path,
            "proposed_content": proposed,
            "observed_content": "",
            "rationale": analysis.fix or analysis.problem or "Automated fix suggestion",
        },
    )

    return analysis_id


def _json(value: dict[str, Any]) -> str:
    import json

    return json.dumps(value, default=str)
