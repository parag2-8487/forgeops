# SPDX-License-Identifier: FSL-1.1-ALv2
"""The Reflector: feedback in, preferences out. Phase 2 §2.13.

WHAT REFLECTION READS, AND WHAT IT DELIBERATELY DOES NOT.

It reads `learning_feedback` -- accepted, rejected and edited artifacts. It does NOT read
`learning_sessions`. A user thinking aloud in a conversation is not stating a preference, and treating
chatter as instruction is how a system comes to believe something nobody decided. The short-term tier exists
so a follow-up question makes sense; that is all it is for.

FOUR PROPERTIES THAT MAKE THIS CORRECTABLE RATHER THAN MERELY ADAPTIVE:

1. A STATED PREFERENCE IS NEVER OVERWRITTEN BY REFLECTION. If the user wrote it, reflection may strengthen
   its evidence count but cannot change its text or reactivate it. Otherwise the user corrects the system,
   the system reverts on its next pass, and the correction feature is a lie.

2. A DEACTIVATED PREFERENCE STAYS DEACTIVATED. Reflection finding the same statement again does not switch it
   back on. The user's decision outranks the inference that produced it -- the alternative is a checkbox that
   un-checks itself.

3. EVERY PREFERENCE CARRIES ITS EVIDENCE COUNT, and the API returns the feedback rows behind it. "Derived
   from one edit" and "derived from forty" deserve different confidence, and a store that cannot express the
   difference invites the user to trust both equally.

4. REFLECTION IS IDEMPOTENT on statement text, via the unique index on `(project_id, scope,
   statement_digest)`. Running it twice strengthens rows rather than duplicating them; without that the skill
   file fills with the same sentence and the token budget spends itself on repetition.

THE MINIMUM EVIDENCE THRESHOLD. A single rejection is noise -- the user may have wanted something else that
day. Two occurrences of the same signal is the floor for inferring a standing preference, and the number is
stated here rather than buried so it can be argued with.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.model_port import ArtifactModelPort
from ..secrets.redaction import create_redacted_prompt
from .models import PREFERENCE_SCOPES

#: Below this, a signal is noise rather than a preference. See the module docstring.
MIN_EVIDENCE = 2

#: The token budget a skill file may occupy in a prompt. Small deliberately: a skill file is one section of a
#: prompt that also has to carry the project's facts and the gate's requirements, and a memory that crowds
#: out the instruction makes the model worse at the task it was asked to do.
SKILL_FILE_CHAR_BUDGET = 2_000

#: Turns kept in short-term memory. Bounded at the write site, not by a sweep, so the bound holds between
#: sweeps rather than on average.
MAX_SESSION_TURNS = 40

_SCOPE_HINTS: dict[str, tuple[str, ...]] = {
    "dockerfile": ("dockerfile", "containerfile"),
    "kubernetes": ("k8s/", "kubernetes/", "manifest", "helm", "chart"),
    "ci": (".github/workflows", ".gitlab-ci", "jenkinsfile", "pipeline"),
    "iac": ("terraform/", ".tf", "opentofu"),
}


def scope_for_path(path: str) -> str:
    """Which preference scope an artifact path belongs to.

    Defaults to `general` rather than guessing. A misfiled preference is worse than an unscoped one: it is
    injected into prompts it has nothing to do with, where it is noise the model has to ignore.
    """
    lowered = path.lower()
    for scope, hints in _SCOPE_HINTS.items():
        if any(hint in lowered for hint in hints):
            return scope
    return "general"


def statement_digest(statement: str) -> str:
    """Normalised digest, so trivially different wordings of one preference collide deliberately.

    Case and internal whitespace are normalised; punctuation is NOT, because "pin the tag" and "pin the tag?"
    are the same preference but "use port 8080" and "use port 8081" are not, and stripping digits or symbols
    to be clever would merge those.
    """
    normalised = re.sub(r"\s+", " ", statement.strip().lower())
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()[:64]


@dataclass(frozen=True, slots=True)
class ReflectionResult:
    state: str
    preferences_written: int = 0
    preferences_strengthened: int = 0
    feedback_considered: int = 0
    skipped_stated: int = 0
    explanation: str = ""


_STATEMENT = re.compile(r"^-\s*\[(?P<scope>[a-z]+)\]\s*(?P<statement>.+)$", re.MULTILINE)


def build_prompt(feedback: list[dict[str, Any]]) -> str:
    """The reflection prompt.

    The EDITED rows carry the most signal and are presented as such: the difference between what was produced
    and what was kept is a preference stated by demonstration, and a prompt that listed edits as mere
    acceptances would throw that away.
    """
    lines = [
        "You are inferring standing preferences for one project from a record of what a human accepted,",
        "rejected or edited. Answer ONLY as lines in this form:",
        "",
        "- [scope] the preference, as one sentence in the imperative",
        "",
        f"where scope is one of: {', '.join(PREFERENCE_SCOPES)}.",
        "",
        "Infer ONLY what the record supports. Do not state general best practice -- that is already in the",
        "generator's own requirements, and repeating it here spends the prompt budget on nothing. If the",
        "record supports no standing preference, reply with exactly:",
        "### NONE",
        "",
        "RECORD:",
    ]
    for row in feedback:
        verdict = row["verdict"]
        line = f"  - {verdict.upper()} {row['artifact_path']}"
        if row.get("comment"):
            line += f" -- the person said: {row['comment']}"
        lines.append(line)
        if verdict == "edited" and row.get("final_content"):
            # The kept version, truncated. This is the highest-signal field in the record.
            kept = str(row["final_content"])[:800]
            lines.append(f"    what they kept instead:\n      {kept.replace(chr(10), chr(10) + '      ')}")
    return "\n".join(lines)


async def reflect(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    model: ArtifactModelPort | None,
    limit: int = 50,
) -> ReflectionResult:
    """Read feedback, infer preferences, write them. Idempotent on statement text."""
    feedback = [
        dict(row)
        for row in (
            await session.execute(
                text(
                    "SELECT verdict, artifact_path, comment, final_content FROM learning_feedback "
                    "WHERE project_id = :p ORDER BY created_at DESC LIMIT :limit"
                ),
                {"p": project_id, "limit": limit},
            )
        ).mappings()
    ]

    if len(feedback) < MIN_EVIDENCE:
        return ReflectionResult(
            state="insufficient_evidence",
            feedback_considered=len(feedback),
            explanation=(
                f"Only {len(feedback)} feedback event(s) recorded, below the floor of {MIN_EVIDENCE}. A "
                "single accept or reject is noise rather than a standing preference -- the person may have "
                "wanted something different that day. Nothing has been inferred."
            ),
        )

    if model is None:
        return ReflectionResult(
            state="unavailable",
            feedback_considered=len(feedback),
            explanation=(
                "No model is configured, so nothing was inferred. Every feedback event is recorded and "
                "readable, and preferences can still be written directly -- a stated preference needs no "
                "model."
            ),
        )

    prompt = create_redacted_prompt(build_prompt(feedback))
    completion = await model.complete(prompt=prompt, store_in_cache=False)
    if not completion.ok or not completion.content:
        return ReflectionResult(
            state="unavailable",
            feedback_considered=len(feedback),
            explanation=(
                "The model could not be reached, so nothing was inferred: " + "; ".join(completion.failure_reasons[:2])
            ),
        )

    if "### NONE" in completion.content:
        return ReflectionResult(
            state="nothing_inferred",
            feedback_considered=len(feedback),
            explanation=(
                "The model read the record and found no standing preference in it. That is a real answer "
                "rather than a failure: the feedback so far does not show a consistent pattern."
            ),
        )

    written = strengthened = skipped = 0
    for match in _STATEMENT.finditer(completion.content):
        scope = match.group("scope").strip().lower()
        statement = match.group("statement").strip()
        if scope not in PREFERENCE_SCOPES or not statement:
            # An unrecognised scope is DROPPED rather than coerced to `general`. Coercing would file the
            # preference somewhere it gets injected into unrelated prompts, and a misfiled preference is
            # worse than a missing one.
            continue

        digest = statement_digest(statement)
        existing = (
            (
                await session.execute(
                    text(
                        "SELECT id, source, active FROM learning_preferences "
                        "WHERE project_id = :p AND scope = :scope AND statement_digest = :digest"
                    ),
                    {"p": project_id, "scope": scope, "digest": digest},
                )
            )
            .mappings()
            .one_or_none()
        )

        if existing is not None:
            if existing["source"] == "stated":
                # PROPERTY 1: a human wrote this. Reflection strengthens the evidence and touches nothing
                # else -- not the text, not the active flag.
                skipped += 1
            await session.execute(
                text(
                    "UPDATE learning_preferences SET evidence_count = evidence_count + 1, "
                    "updated_at = now() WHERE id = :id"
                ),
                {"id": existing["id"]},
            )
            # PROPERTY 2: `active` is deliberately absent from that UPDATE. A preference the user switched
            # off stays off; reflection finding it again is not permission to re-enable it.
            strengthened += 1
            continue

        await session.execute(
            text(
                "INSERT INTO learning_preferences "
                "(id, project_id, scope, statement, statement_digest, source, evidence_count) "
                "VALUES (:id, :p, :scope, :statement, :digest, 'reflected', 1)"
            ),
            {
                "id": uuid.uuid4(),
                "p": project_id,
                "scope": scope,
                "statement": statement[:4000],
                "digest": digest,
            },
        )
        written += 1

    await model.remember(prompt=prompt, content=completion.content)
    return ReflectionResult(
        state="reflected",
        preferences_written=written,
        preferences_strengthened=strengthened,
        feedback_considered=len(feedback),
        skipped_stated=skipped,
        explanation=(
            f"Read {len(feedback)} feedback event(s): wrote {written} new preference(s) and strengthened "
            f"{strengthened}."
            + (f" {skipped} were written by a person, so their text was left alone." if skipped else "")
        ),
    )


@dataclass(frozen=True, slots=True)
class SkillFile:
    content: str
    included: list[uuid.UUID]
    excluded: list[uuid.UUID]
    explanation: str


async def compile_skill_file(
    session: AsyncSession, *, project_id: uuid.UUID, scopes: tuple[str, ...] | None = None
) -> SkillFile:
    """Select active preferences into a token-bounded block, and record what was left out.

    ORDERED BY SOURCE THEN EVIDENCE: a stated preference comes before a reflected one regardless of counts,
    because the user said it. Within a source, better-supported preferences come first. That ordering is what
    makes the budget cut the least-supported inference rather than an arbitrary row.

    THE EXCLUDED IDS ARE RETURNED, not discarded. A preference that never reaches a prompt is
    indistinguishable from one that does not exist, judging by the output alone -- so the exclusions are
    recorded and shown, and the user can see that their memory is larger than what fits.
    """
    parameters: dict[str, Any] = {"p": project_id}
    scope_filter = ""
    if scopes:
        scope_filter = " AND scope = ANY(:scopes)"
        parameters["scopes"] = list(scopes)

    rows = [
        dict(row)
        for row in (
            await session.execute(
                text(
                    "SELECT id, scope, statement, source, evidence_count FROM learning_preferences "
                    f"WHERE project_id = :p AND active = true{scope_filter} "
                    "ORDER BY (source = 'stated') DESC, evidence_count DESC, created_at"
                ),
                parameters,
            )
        ).mappings()
    ]

    if not rows:
        return SkillFile(
            content="",
            included=[],
            excluded=[],
            explanation=(
                "No active preferences, so nothing is injected. The prompt is unchanged from what it would "
                "be for a project with no history -- which is the honest state for a project nobody has "
                "given feedback on yet."
            ),
        )

    header = "## What this project's maintainers prefer\n\n"
    body: list[str] = []
    included: list[uuid.UUID] = []
    excluded: list[uuid.UUID] = []
    used = len(header)

    for row in rows:
        line = f"- [{row['scope']}] {row['statement']}\n"
        if used + len(line) > SKILL_FILE_CHAR_BUDGET:
            excluded.append(row["id"])
            continue
        body.append(line)
        included.append(row["id"])
        used += len(line)

    return SkillFile(
        content=header + "".join(body) if body else "",
        included=included,
        excluded=excluded,
        explanation=(
            f"{len(included)} preference(s) injected"
            + (
                f"; {len(excluded)} did not fit the {SKILL_FILE_CHAR_BUDGET}-character budget and were left "
                "out. Those are listed so a run can be judged against what actually reached the model."
                if excluded
                else "."
            )
        ),
    )


async def record_skill_file(session: AsyncSession, *, project_id: uuid.UUID, skill_file: SkillFile) -> uuid.UUID:
    """Store exactly what was injected, so a run can be judged rather than guessed at."""
    import json

    skill_file_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO learning_skill_files "
            "(id, project_id, content, included_preference_ids, excluded_preference_ids) "
            "VALUES (:id, :p, :content, CAST(:included AS jsonb), CAST(:excluded AS jsonb))"
        ),
        {
            "id": skill_file_id,
            "p": project_id,
            "content": skill_file.content,
            "included": json.dumps([str(one) for one in skill_file.included]),
            "excluded": json.dumps([str(one) for one in skill_file.excluded]),
        },
    )
    return skill_file_id
