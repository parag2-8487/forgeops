# SPDX-License-Identifier: FSL-1.1-ALv2
"""Compile the scan facts and the failing readiness checks into one explicit instruction.

WHY A COMPILER RATHER THAN A PROMPT

Generation used to receive whatever the operator typed and was expected to work the rest out. That is
the condition under which a model invents: asked for "a Dockerfile" with no facts, it has to guess the
language, the package manager, the entry point, the port and the layout, and a guess that reads
plausibly is indistinguishable from a fact until it fails.

So the model is told, rather than asked to infer. Everything in the output of this module comes from the
index or from a readiness check that examined the repository; nothing is a default, a template value or
an assumption. Where a fact is not known, the prompt says it is not known for that artifact instead of
filling the gap — which is the same rule this codebase already applies to an absent embedding, to
`served_from`, and to an unresolved dependency version.

THE FOUR PROPERTIES THAT MAKE IT TRUSTWORTHY

* DERIVED. Two repositories produce visibly different prompts, because every section is built from
  their own paths, frameworks and findings. A template with substitutions would read the same.
* VERIFIABLE. Every path the prompt states as existing is in the index, and every path it instructs a
  write to is listed as a target. `test_every_path_in_a_compiled_prompt_is_real` asserts it — a prompt
  naming a path the scan never saw is the exact failure this module exists to prevent.
* DETERMINISTIC. No clock, no randomness, no set iteration. The same index and the same failing checks
  compile byte-identically, so a generation run can be reproduced and two runs can be compared.
* BOUNDED. It will be long. When it exceeds the budget of the tier it is routed to, whole artifact
  sections are DROPPED, lowest-weight first, and the ones dropped are named on the result. Nothing is
  ever cut mid-sentence: a truncated instruction is worse than an absent one, because the model acts on
  the half it received.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

from pydantic import BaseModel

from ..core.readiness import CATEGORY_WEIGHTS, ReadinessCheck
from ..core.readiness_findings import CHECK_EXPLANATIONS
from .model_prompt import output_format_section

#: Roughly four characters to a token for English prose and configuration. Deliberately an ESTIMATE and
#: named as one: the real count depends on the tokeniser of whichever model the tier resolves to, and
#: this module must not pretend to know that. Used only to decide whether to defer a section, and a
#: pessimistic estimate errs towards deferring — which is the safe direction, because a deferred artifact
#: is reported and a truncated one is not.
CHARS_PER_TOKEN_ESTIMATE: Final = 4

#: What will actually be run against each artifact kind, so the model knows the bar before it writes.
#:
#: Empty means there is no executable validator for that kind and the readiness checks are the whole
#: criterion. Stated rather than omitted: "this will be checked by a tool" and "this will be checked by
#: the score" are different promises, and claiming the first where only the second is true would be the
#: kind of overstatement this file exists to avoid.
#: The BLOCKING gate's requirements, stated mechanically, per artifact kind.
#:
#: WHY PROSE WAS NOT ENOUGH. Section 2 already tells the model what is wrong — "declares no USER, so the
#: image runs as root" — and that is a good explanation for a human. A 1.5b model given an explanation
#: produced a Dockerfile that the gate then rejected for exactly the named fault, so the file was withheld
#: and the user's most important recommendation went unaddressed. The prompt this replaced stated the same
#: requirements LITERALLY ("contains a line that is exactly `USER 1001`") and its output passed.
#:
#: These are the checks in `generation/artifact_checks.py`, which is blocking under §11.5.5. An artifact
#: that fails one is discarded, so the model is told the rule rather than only the consequence.
GATE_REQUIREMENTS: Final[Mapping[str, tuple[str, ...]]] = {
    "dockerfile": (
        "The FIRST instruction is `FROM` (an `ARG` may precede it; nothing else may).",
        "There is a `USER` instruction that switches to a non-root account, placed after the RUN "
        "instructions and before CMD or ENTRYPOINT.",
        "The file contains at least one real instruction, not only comments.",
    ),
    "k8s": (
        "Every document declares top-level `apiVersion`, `kind` and `metadata.name`.",
        "Separate documents with `---` and put no document in a comment.",
    ),
    "compose": (
        "There is a top-level `services` mapping with at least one service.",
        "No service uses the `latest` tag or omits its tag.",
    ),
    "helm": ("`Chart.yaml` declares `apiVersion`, `name` and `version`.",),
    "github_workflow": (
        "There is a top-level `on` trigger and a `jobs` mapping with at least one job.",
        "Every job has `runs-on` and at least one step.",
    ),
    "opentofu": ("There is at least one `terraform`, `provider` or `resource` block, and the file parses as HCL.",),
}

ARTIFACT_VALIDATORS: Final[Mapping[str, str]] = {
    "k8s": "validate.k8s (kubectl apply --dry-run=server against the cluster's own schema)",
    "compose": "validate.compose (docker compose config)",
    "helm": "validate.helm (helm lint, then helm template --validate)",
    "opentofu": "validate.tofu (tofu validate)",
    "github_workflow": "validate.yaml (yamllint, plus the workflow schema)",
    "dockerfile": "validate.trivy (image vulnerability scan, failing at HIGH and above)",
}

#: The maximum number of lines of an existing file quoted back into the prompt.
#:
#: A modify instruction has to show the model what it is editing, and the whole file is often too much.
#: Quoting the head is chosen over a summary because a summary is a paraphrase, and a paraphrase of the
#: file the model must preserve is exactly where content gets silently dropped.
#: How many artifacts one run may ask a model to write.
#:
#: A COUNT CAP AND A TOKEN CAP BOUND DIFFERENT THINGS. `token_budget` limits the instruction sent; this
#: limits the answer expected, and nothing limited that before. A run whose write targets came from every
#: failing check asked for eleven files, and a small model — the end-to-end journey uses
#: `qwen2.5-coder:1.5b` — cannot emit eleven complete valid files in one response. It produced a handful,
#: malformed, none passed the gate, and the run fell back to canned templates: nothing delivered, for
#: minutes of compute. A capable model handles the same prompt, which is why it was invisible until a
#: small one ran it.
#:
#: Six because the contract this replaced asked for four and that was demonstrably within reach, and six
#: leaves headroom for the checks a user selected. The remainder is DEFERRED, not dropped: it is recorded
#: on the run and named in the UI, so the user knows what to ask for next.
DEFAULT_MAX_WRITE_TARGETS: Final = 6

MAX_QUOTED_LINES: Final = 120


#: Where each ecosystem puts a linter configuration, keyed by the language the scan detected.
#:
#: The findings table gives a CONVENTION for these — "(depends on language: ruff.toml,
#: eslint.config.mjs, .golangci.yml)" — because the table is repository-independent and cannot know
#: which applies. Resolving it needs a fact, and the fact is in the inventory, so it is resolved here
#: rather than left as a parenthetical the model would have to interpret.
#:
#: A language not listed produces NO instruction rather than a guessed filename. That is the difference
#: between deriving and inventing: an unlisted ecosystem is a gap in this table, and writing
#: `lint.config` into somebody's repository because nothing better was known is exactly the fabrication
#: the prohibitions forbid.
LINT_CONFIG_BY_LANGUAGE: Final[Mapping[str, str]] = {
    "python": "ruff.toml",
    "javascript": "eslint.config.mjs",
    "typescript": "eslint.config.mjs",
    "go": ".golangci.yml",
    "rust": "rustfmt.toml",
    "ruby": ".rubocop.yml",
    "php": ".php-cs-fixer.php",
}


def _slugify(name: str) -> str:
    """A DNS-label-safe form of the project name, or "" when nothing usable remains.

    Empty rather than a fallback like "app": a chart directory named after a default is named after
    nothing, and the caller reads "" as "this path cannot be resolved" and says so in the prompt.
    """
    slug = "".join(c.lower() if c.isalnum() else "-" for c in name.strip()).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug[:63]


def _resolve_target(artifact: str, remedy_path: str, languages: Sequence[str], project_slug: str = "") -> str:
    """Turn a remedy path into a concrete file, or return "" when it cannot be known.

    A remedy path containing a parenthesis is a CONVENTION rather than a location, and one containing
    `<…>` carries a PLACEHOLDER the table cannot fill. Both need a fact from this repository, and the
    facts come from the scan: the detected languages resolve a linter configuration, and the project's own
    name resolves a chart directory.

    Returning "" is deliberate for anything unresolvable. The artifact is then not requested and the
    reason appears in the prompt's "cannot address" section. Naming a plausible file instead would put a
    fabricated path into an instruction — and `charts/<name>/Chart.yaml` reaching a model verbatim is
    exactly that, because the model would either invent a name or write the angle brackets into the tree.
    """
    if remedy_path and "(" not in remedy_path and "<" not in remedy_path:
        return remedy_path.split(",")[0].strip()

    if artifact == "lint_config":
        for language in languages:
            resolved = LINT_CONFIG_BY_LANGUAGE.get(language.lower())
            if resolved:
                return resolved

    if "<name>" in remedy_path and project_slug:
        # Slugified rather than used raw: a chart directory becomes part of a Helm release name, which
        # Kubernetes requires to be a DNS label.
        return remedy_path.split(",")[0].strip().replace("<name>", project_slug)

    return ""


class ArtifactInstruction(BaseModel):
    """One file the model must produce or edit, and everything it needs to know about it."""

    path: str
    #: "create" or "modify". Never inferred by the model: the compiler knows whether the path is in the
    #: index, and letting the model decide is how an existing file gets overwritten.
    action: str
    artifact: str
    #: The readiness check ids this artifact would satisfy.
    satisfies: tuple[str, ...]
    #: For a modify: what is specifically wrong, one entry per failing property, with line numbers where
    #: the check could locate them.
    faults: tuple[str, ...] = ()
    #: For a modify: the current content, or its head when the file is long.
    current_content: str = ""
    current_line_count: int = 0
    #: What the model must not remove. Derived from the file itself, not from a guess about intent.
    preserve: tuple[str, ...] = ()
    #: The validator that will be run, or "" when only the readiness checks apply.
    validator: str = ""
    #: The weight this artifact carries, used to decide what to defer when the budget is exceeded.
    weight: int = 0

    model_config = {"frozen": True}


class CompiledPrompt(BaseModel):
    """The instruction, plus everything needed to audit it."""

    text: str
    #: Every path the prompt asserts EXISTS. A test checks each against the index.
    referenced_paths: tuple[str, ...] = ()
    #: Every path the prompt instructs a write to.
    write_targets: tuple[str, ...] = ()
    #: The failing checks this prompt sets out to fix.
    addressed_checks: tuple[str, ...] = ()
    #: Failing checks left out because the budget could not hold them, lowest weight first. NAMED rather
    #: than silently dropped, so a user can see that one run cannot cover everything and why.
    deferred_checks: tuple[str, ...] = ()
    #: Failing checks no generated artifact can address, with the reason from the findings table.
    unaddressable: tuple[str, ...] = ()
    token_estimate: int = 0
    token_budget: int = 0
    #: One sentence naming what was done about the budget. Always populated, including when nothing had
    #: to be done, so a reader never has to infer it from the absence of a warning.
    budget_strategy: str = ""

    model_config = {"frozen": True}


def _fmt_list(values: Sequence[str], *, empty: str) -> str:
    return ", ".join(values) if values else empty


def _quote(path: str, body: str) -> tuple[str, int]:
    """The file as the model will see it, with real line numbers, and its true length."""
    lines = body.splitlines()
    shown = lines[:MAX_QUOTED_LINES]
    numbered = "\n".join(f"{n:>5} | {text}" for n, text in enumerate(shown, start=1))
    if len(lines) > MAX_QUOTED_LINES:
        numbered += (
            f"\n      | … {len(lines) - MAX_QUOTED_LINES} further line(s) not shown. "
            f"They are part of {path} and must be preserved."
        )
    return numbered, len(lines)


def _preserve_notes(body: str) -> tuple[str, ...]:
    """What a modify must keep, read off the file rather than assumed.

    Comment blocks and named build stages are the two things a wholesale regeneration destroys most
    often, and both are visible in the text. Nothing here guesses at intent: it points at regions that
    exist and says they must survive.
    """
    notes: list[str] = []
    lines = body.splitlines()

    comment_runs: list[tuple[int, int]] = []
    start: int | None = None
    for number, text in enumerate(lines, start=1):
        stripped = text.strip()
        if stripped.startswith("#") or stripped.startswith("//"):
            start = number if start is None else start
        else:
            if start is not None and number - start >= 2:
                comment_runs.append((start, number - 1))
            start = None
    if start is not None and len(lines) - start >= 1:
        comment_runs.append((start, len(lines)))
    for first, last in comment_runs[:4]:
        notes.append(f"the comment block at lines {first} to {last}")

    for number, text in enumerate(lines, start=1):
        upper = text.strip().upper()
        if upper.startswith("FROM ") and " AS " in upper:
            stage = text.strip().rsplit(" AS ", 1)[-1].strip()
            notes.append(f"the build stage {stage!r} declared at line {number}")
    return tuple(notes)


def _facts_section(
    *,
    paths: Sequence[str],
    inventory: Mapping[str, Any],
    devops_paths: Sequence[str],
) -> list[str]:
    """Everything established about the repository, each item with where it came from."""
    out = [
        "## 1. ESTABLISHED FACTS ABOUT THIS REPOSITORY",
        "",
        "Every line in this section was read from the index built by scanning the repository. None of it",
        "is a default or an assumption. Treat it as the complete set of what is known: if something you",
        "need is not here, it is not known, and you must say so rather than supply a plausible value.",
        "",
        f"Files indexed: {inventory.get('file_count', len(paths))}",
    ]

    languages = [str(v) for v in (inventory.get("languages") or [])]
    out.append(f"Languages detected: {_fmt_list(languages, empty='none detected')}")

    managers = [str(v) for v in (inventory.get("package_managers") or [])]
    out.append(f"Package managers detected: {_fmt_list(managers, empty='none detected')}")

    frameworks = inventory.get("frameworks") or []
    if frameworks:
        out.append("")
        out.append("Frameworks, each with the file that establishes it and how firmly:")
        for framework in sorted(frameworks, key=lambda f: (str(f.get("name", "")))):
            name = framework.get("name", "?")
            kind = framework.get("kind", "?")
            confidence = framework.get("confidence", "?")
            evidence = framework.get("evidence", "?")
            version = framework.get("version") or "no version declared"
            note = (
                "declared in a manifest, so you may rely on it"
                if confidence == "declared"
                else "INFERRED from the layout only, nothing declares it — do not rely on it"
            )
            out.append(f"  - {name} ({kind}), version {version}, from {evidence}: {note}")
    else:
        out.append("Frameworks: none detected.")

    entry_points = [str(v) for v in (inventory.get("entry_points") or [])]
    out.append("")
    out.append(f"Entry points: {_fmt_list(entry_points, empty='none identified by the scan')}")

    manifests = [str(v) for v in (inventory.get("manifests") or [])]
    out.append(f"Dependency manifests present: {_fmt_list(manifests, empty='none')}")

    configs = [str(v) for v in (inventory.get("config_files") or [])]
    out.append(f"Configuration files present: {_fmt_list(configs, empty='none')}")

    out.append("")
    out.append(f"DevOps configuration already in the repository: {_fmt_list(list(devops_paths), empty='none')}")
    out.append(
        "Match the layout this repository already uses. Where it places something, put related files "
        "beside it rather than in the location a tutorial would choose."
    )
    return out


def _prohibitions() -> list[str]:
    return [
        "## 4. PROHIBITIONS",
        "",
        "These are the failure modes that make generated infrastructure dangerous rather than merely",
        "wrong, because each produces output that reads correctly and does not work.",
        "",
        "  1. Do not reference any file, package, module, image or path that is not named in section 1.",
        "  2. Do not invent a dependency name or a version number. If a version is needed and section 1",
        "     does not give it, say so for that artifact.",
        "  3. Do not assume a framework, package manager, port or base image that section 1 does not",
        "     state. An INFERRED framework is not a statement you may build on.",
        "  4. Do not write to any path outside the targets listed in section 2.",
        "  5. When modifying a file, do not remove existing content unless the instruction says to. If",
        "     you must remove a line, say which line and why.",
        "  6. If the facts are insufficient for one artifact, produce the others and state plainly what",
        "     is missing for that one. A plausible guess is worse than an omission here: an omission is",
        "     visible and a guess is not.",
    ]


def compile_prompt(
    *,
    checks: Sequence[ReadinessCheck],
    paths: Sequence[str],
    contents: Mapping[str, str],
    inventory: Mapping[str, Any],
    selected_check_ids: Sequence[str] | None = None,
    token_budget: int = 24_000,
    max_write_targets: int = DEFAULT_MAX_WRITE_TARGETS,
    project_name: str = "",
) -> CompiledPrompt:
    """Build the instruction for the failing checks a generated artifact can satisfy.

    `selected_check_ids` narrows the work to checks the user picked; `None` means every failing one. A
    check that PASSES never produces an instruction, so a repository with a correct Dockerfile is never
    told to write one.
    """
    indexed = {p.replace("\\", "/") for p in paths}
    project_slug = _slugify(project_name)
    lowered = {p.lower(): p for p in indexed}

    failing = [c for c in checks if not c.passed]
    if selected_check_ids is not None:
        wanted = set(selected_check_ids)
        failing = [c for c in failing if c.id in wanted]

    # Group by the artifact that would fix them, so one instruction covers every fault in one file.
    by_artifact: dict[str, list[ReadinessCheck]] = {}
    unaddressable: list[str] = []
    for check in sorted(failing, key=lambda c: c.id):
        explanation = CHECK_EXPLANATIONS.get(check.id)
        if explanation is None or not explanation.artifact or not check.generatable:
            reason = check.blocked_because or "no generated artifact addresses this check"
            unaddressable.append(f"{check.id}: {reason}")
            continue
        by_artifact.setdefault(explanation.artifact, []).append(check)

    instructions: list[ArtifactInstruction] = []
    for artifact in sorted(by_artifact):
        group = by_artifact[artifact]
        # The path comes from the check's own remedy_path, which is the table's statement of where this
        # ecosystem puts the file. A path containing a parenthetical is a convention rather than a
        # location and is resolved against the repository instead of used literally.
        languages = [str(v) for v in (inventory.get("languages") or [])]
        resolved = [path for c in group if (path := _resolve_target(artifact, c.remedy_path, languages, project_slug))]
        target = resolved[0] if resolved else ""
        if not target:
            # No concrete path could be derived, so no instruction is written. The failing checks are
            # reported as unaddressable with their reason rather than being silently dropped.
            unaddressable.extend(
                f"{c.id}: the file that would fix this has no location this scan can determine "
                f"(convention: {c.remedy_path or 'none stated'})"
                for c in group
            )
            continue

        existing = lowered.get(target.lower())
        body = contents.get(target.lower(), "") or contents.get(target, "")
        action = "modify" if existing else "create"

        faults = tuple(
            f"{c.found}{f' (line {c.line})' if c.line is not None else ''} — required: {c.looked_for}" for c in group
        )
        quoted, line_count = _quote(target, body) if body else ("", 0)
        instructions.append(
            ArtifactInstruction(
                path=existing or target,
                action=action,
                artifact=artifact,
                satisfies=tuple(c.id for c in group),
                faults=faults,
                current_content=quoted,
                current_line_count=line_count,
                preserve=_preserve_notes(body) if body else (),
                validator=ARTIFACT_VALIDATORS.get(artifact, ""),
                weight=sum(CATEGORY_WEIGHTS.get(c.category, 0) + c.max_points for c in group),
            )
        )

    # Heaviest first, so a budget cut drops the least valuable work rather than an arbitrary tail.
    instructions.sort(key=lambda i: (-i.weight, i.path))

    devops_paths = sorted(
        p
        for p in indexed
        if any(
            marker in p.lower()
            for marker in (
                "dockerfile",
                "docker-compose",
                ".github/workflows/",
                "k8s/",
                "kubernetes/",
                "chart.yaml",
                ".tf",
                ".dockerignore",
            )
        )
    )

    header = [
        "# GENERATION INSTRUCTION",
        "",
        "You are producing DevOps configuration for one specific repository. Everything you need is",
        "stated below. Produce only the files listed in section 2, at exactly the paths given.",
        "",
    ]

    kept: list[ArtifactInstruction] = []
    deferred: list[ArtifactInstruction] = []

    def render(selected: Sequence[ArtifactInstruction]) -> str:
        body_lines = list(header)
        body_lines += _facts_section(paths=paths, inventory=inventory, devops_paths=devops_paths)
        body_lines += ["", "## 2. WHAT TO PRODUCE", ""]
        if not selected:
            body_lines.append(
                "Nothing. No failing check in this report can be satisfied by a file this generator "
                "produces, and section 5 says which and why."
            )
        for number, item in enumerate(selected, start=1):
            body_lines.append(f"### 2.{number} {item.action.upper()} `{item.path}`")
            body_lines.append("")
            if item.action == "create":
                body_lines.append(f"This file does not exist in the repository. Create it at exactly `{item.path}`.")
            else:
                body_lines.append(
                    f"This file EXISTS, at {item.current_line_count} line(s). MODIFY it — do not replace "
                    "it and do not regenerate it from scratch. What is wrong with it:"
                )
                for fault in item.faults:
                    body_lines.append(f"  - {fault}")
                if item.preserve:
                    body_lines.append("")
                    body_lines.append("You must preserve, unchanged:")
                    for note in item.preserve:
                        body_lines.append(f"  - {note}")
                if item.current_content:
                    body_lines.append("")
                    body_lines.append("Its current content, with line numbers:")
                    body_lines.append("")
                    body_lines.append(item.current_content)
            if item.action == "create" and item.faults:
                body_lines.append("")
                body_lines.append("It must satisfy:")
                for fault in item.faults:
                    body_lines.append(f"  - {fault}")
            body_lines.append("")
        body_lines += ["## 3. HOW EACH FILE WILL BE CHECKED", ""]
        body_lines.append(
            "Your output is not accepted on inspection. Each file is run through a validator and then "
            "re-scored by the readiness checks named above. Write for these:"
        )
        body_lines.append("")
        for item in selected:
            criterion = item.validator or (
                "no executable validator exists for this kind; the readiness checks above are the whole criterion"
            )
            body_lines.append(f"  - `{item.path}`: {criterion}")

        # THE BLOCKING RULES, STATED AS RULES. Section 2 explains the FAULT; this states the REQUIREMENT.
        # A small model given "declares no USER, so the image runs as root" produced a Dockerfile that the
        # gate rejected for exactly that fault, so the file was withheld and the user's most important
        # recommendation went unaddressed. The prompt this replaced said "contains a line that is exactly
        # `USER 1001`" and its output passed. An explanation is for a human; a rule is for a checker.
        mechanical = [
            (item.path, GATE_REQUIREMENTS[item.artifact]) for item in selected if item.artifact in GATE_REQUIREMENTS
        ]
        if mechanical:
            body_lines += [
                "",
                "These are checked mechanically and are not matters of judgement. A file that breaks one",
                "is discarded in full, so satisfy them literally:",
                "",
            ]
            for path, rules in mechanical:
                body_lines.append(f"`{path}`:")
                for rule in rules:
                    body_lines.append(f"  - {rule}")
                body_lines.append("")
        body_lines += ["", *_prohibitions()]
        if unaddressable:
            body_lines += [
                "",
                "## 5. FAILING CHECKS THIS RUN CANNOT ADDRESS",
                "",
                "Listed so nothing here looks like an oversight. Do not attempt these.",
                "",
            ]
            body_lines += [f"  - {entry}" for entry in unaddressable]

        # THE SECTION WITHOUT WHICH NOTHING ELSE MATTERS, and it was missing.
        #
        # Every section above describes WHAT to write. None described HOW to format the answer, and
        # `parse_artifacts` reads exactly one shape: `### FILE: <path>` followed by a fenced block. So the
        # model complied with everything it was told, returned plausible artifacts with zero markers, the
        # parse found nothing, all attempts were recorded as failures and the run served canned templates.
        # Measured back to back on one service and one model: `accepted` with four files without a compiled
        # prompt, `template_fallback` with one.
        #
        # Shared with `build_generation_prompt` rather than restated, so the contract and its parser cannot
        # drift apart again. It goes LAST because it is the instruction the model should still have in view
        # when it starts emitting.
        body_lines += output_format_section([i.path for i in selected])
        return "\n".join(body_lines) + "\n"

    kept = list(instructions)
    text = render(kept)
    strategy = "the whole instruction fits the tier's context budget; nothing was deferred"
    budget_chars = token_budget * CHARS_PER_TOKEN_ESTIMATE

    # A COUNT CAP AS WELL AS A TOKEN CAP, because they bound different things and only one of them was
    # bounded. `token_budget` limits the INSTRUCTION; nothing limited the ANSWER.
    #
    # THE FAILURE THIS FIXES, MEASURED. Deriving the write targets from the failing checks let one run ask
    # for eleven artifacts. The end-to-end journey's model is `qwen2.5-coder:1.5b`, and a 1.5-billion
    # parameter model cannot emit eleven complete, valid files in one response — it produced a few,
    # malformed, none passed the gate, and the run fell back to templates. The user got nothing for eight
    # minutes of compute. On a capable model the same prompt succeeds, which is why this was invisible
    # until the journey ran it.
    #
    # SIX IS NOT ARBITRARY. The contract this replaced asked for four (a Dockerfile and three manifests)
    # and that was demonstrably within this model's reach; six gives headroom for the checks a user
    # actually selected while staying inside a small model's single-response capacity.
    #
    # DEFERRING IS NOT LOSING. The remainder goes to `deferred_checks`, which the run records and the UI
    # names, so the user is told precisely which recommendations this run did not attempt and can run
    # generation again for them. Six artifacts delivered beats eleven requested and none delivered.
    while len(kept) > max_write_targets:
        deferred.insert(0, kept.pop())
        strategy = (
            f"the run asked for more than {max_write_targets} artifacts, so "
            f"{len(deferred)} were deferred to a later run, lowest weight first. A model asked for "
            "more files than it can complete in one answer returns none of them usable."
        )
        text = render(kept)

    # WHOLE SECTIONS, LOWEST WEIGHT FIRST, and never a partial one. A truncated instruction is acted on
    # by the model as though it were complete, so it is strictly more dangerous than a missing one.
    while kept and len(text) > budget_chars:
        deferred.insert(0, kept.pop())
        text = render(kept)
        strategy = (
            f"the instruction exceeded the {token_budget}-token budget, so "
            f"{len(deferred)} artifact section(s) were deferred to a later run, lowest weight first. "
            "Nothing was truncated: a cut instruction is acted on as though it were whole."
        )

    return CompiledPrompt(
        text=text,
        referenced_paths=tuple(sorted({i.path for i in kept if i.action == "modify"} | set(devops_paths))),
        write_targets=tuple(sorted(i.path for i in kept)),
        addressed_checks=tuple(sorted(cid for i in kept for cid in i.satisfies)),
        deferred_checks=tuple(sorted(cid for i in deferred for cid in i.satisfies)),
        unaddressable=tuple(unaddressable),
        token_estimate=len(text) // CHARS_PER_TOKEN_ESTIMATE,
        token_budget=token_budget,
        budget_strategy=strategy,
    )
