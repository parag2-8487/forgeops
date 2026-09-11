# SPDX-License-Identifier: FSL-1.1-ALv2
"""Deterministic deployment-readiness scoring engine (phases.md §1.4, Leaf 12.3).

**What changed and why.** The engine used to score a `project_data` dict that its only
caller built from `projects.settings` — the JSONB blob an operator types into the create
form. `config_files` was literally `sorted(settings.keys())`, so a project scored points
for *having been configured*, and `"README.md"` was substituted when the blob was empty.
The number that reached the readiness screen therefore described what somebody had typed,
not what was in the repository, and it moved when the settings changed and stayed still
when the repository did.

It now scores the INDEX: the `file_tree` paths and `file_contents` bodies a real agent scan
persisted. `projects.settings` may still REFINE the evidence — `ignore_globs` removes paths
from consideration, because a path the operator has declared out of scope is not evidence
about the deployment — but it can no longer substitute for it. A project with no scan
scores zero and says so, which is the honest answer: the alternative, scoring the settings,
is a number that looks like a measurement.

**The category set is phases.md §1.4's**: Containerization, CI/CD, Orchestration, Env
Config, Security, IaC. §1.4 names them but fixes no weights, so the weights below are a
decision recorded here rather than a quotation; they sum to 100 so the overall score is a
weighted mean and not an arbitrary sum that happens to land near 100.

**`has_tests` no longer defaults to true.** It defaulted to `True`, which handed every
project 25 of its 100 points for tests nobody had looked for — the single largest source of
inflation in the old score. Test evidence is now a check inside CI/CD, derived from paths
that are actually test files, and an unstated `has_tests` is derived rather than assumed.
"""

from __future__ import annotations

import fnmatch
import posixpath
import re
from collections.abc import Iterable, Mapping
from typing import Any, Final

from pydantic import BaseModel, Field

from .content_validation import (
    dockerfile_has_no_baked_secrets,
    dockerfile_secrets_audit,
    kubernetes_containers_are_unprivileged,
    kubernetes_manifests_are_well_formed,
    kubernetes_privileged_audit,
    kubernetes_schema_audit,
)
from .dependency_manifest import ECOSYSTEM_FILES, reconcile
from .index_evidence import IndexEvidence
from .manifest_facts import (
    ItemAudit,
    dockerfile_base_pinned,
    dockerfile_healthcheck,
    kubernetes_image_tags_audit,
    kubernetes_image_tags_pinned,
    kubernetes_probes,
    kubernetes_probes_audit,
    kubernetes_resource_limits,
    kubernetes_resource_limits_audit,
    pipeline_actions_pinned,
    pipeline_actions_pinned_audit,
    pipeline_runs_tests,
    pipeline_stages_declared,
)
from .readiness_cross_checks import (
    ci_builds_an_image_that_has_a_dockerfile,
    env_example_matches_the_deployment,
    kubernetes_images_are_produced_by_ci,
)
from .readiness_findings import (
    CHECK_EXPLANATIONS,
    GENERATED_ARTIFACT_KINDS,
    CheckExplanation,
)
from .source_analysis import centralisation, test_substance
from .tool_verdicts import summarise

#: The §1.4 categories and their weights. Summing to 100 makes the overall score a
#: weighted mean of six 0-100 category scores, which is what lets one category's absence
#: be read off the breakdown instead of inferred from a total.
#: The last-resort `found` value.
#:
#: Used only when a caller reports no finding for a failure. It is a real statement — the check
#: looked in the places `looked_in` names and nothing was there — rather than the empty string,
#: which told a reader nothing and was indistinguishable from a bug in the check itself.
_NOTHING_FOUND: Final = "nothing matched in any of the locations searched"


#: Max points for the four checks scored proportionally, named so `_proportional` and the `max_points`
#: argument cannot drift apart. A partial score computed against a different total than the one
#: reported would render a bar that disagrees with its own label.
KUBERNETES_RESOURCE_LIMITS_DECLARED_POINTS: Final = 35
KUBERNETES_PROBES_DECLARED_POINTS: Final = 25
KUBERNETES_IMAGE_TAGS_PINNED_POINTS: Final = 20
#: A1 content checks. Weighted against the faults already in each category rather than by novelty.
#:
#: A baked secret is scored as heavily as any other security finding because the exposure is permanent:
#: unlike a missing scanner, it cannot be fixed by adding a file — the image must be rebuilt and the old
#: tags deleted. An invalid manifest is scored highest of the three because its failure is TOTAL: the
#: deploy does not degrade, it stops.
DOCKERFILE_NO_BAKED_SECRETS_POINTS: Final = 25
#: A1b. Weighted highest of the content checks because the verdict comes from the DEPLOYMENT TOOL ITSELF:
#: a manifest `kubeconform` rejects will not apply, and no amount of the rest of the repository being
#: correct changes that. It is the one check whose failure is confirmed by the software that will refuse.
ARTIFACT_VALIDATION_POINTS: Final = 30
KUBERNETES_UNPRIVILEGED_POINTS: Final = 20
KUBERNETES_MANIFEST_VALID_POINTS: Final = 25
PIPELINE_ACTIONS_PINNED_POINTS: Final = 15


def _proportional(audit: ItemAudit, max_points: int) -> int:
    """Points for the share of items that satisfied the check.

    Rounded DOWN, and capped below `max_points` until every item passes, so a repository one container
    short of correct can never round up to a full score. The last point is earned by finishing.
    """
    if audit.examined <= 0:
        return 0
    if audit.satisfied >= audit.examined:
        return max_points
    earned = (max_points * audit.satisfied) // audit.examined
    return min(earned, max_points - 1)


def _earned_from_counts(satisfied: int, examined: int, max_points: int) -> int | None:
    """`_proportional` for a caller that already holds the two counts.

    Returns None when nothing was examined, which the caller must read as "do not override the pass/fail
    decision" rather than as zero — a check with nothing to count is not a check scoring nought.
    """
    if examined <= 0:
        return None
    if satisfied >= examined:
        return max_points
    return min((max_points * satisfied) // examined, max_points - 1)


CATEGORY_WEIGHTS: Final[dict[str, int]] = {
    "containerization": 25,
    "ci_config": 20,
    "orchestration": 15,
    "env_config": 15,
    "security_policy": 15,
    "iac": 10,
}

#: Category name → the breakdown field it is reported under. Three of the six keep the
#: names the previous five-category response used (`containerization_score`,
#: `ci_config_score`, `security_policy_score`) so an existing client reading those
#: continues to read the same concept.
CATEGORY_FIELDS: Final[dict[str, str]] = {
    "containerization": "containerization_score",
    "ci_config": "ci_config_score",
    "orchestration": "orchestration_score",
    "env_config": "env_config_score",
    "security_policy": "security_policy_score",
    "iac": "iac_score",
}

#: Paths that are test evidence. Matched on the whole slash-separated path, so
#: `internal/scanner/scanreport_test.go` counts and `contest.py` does not.
_TEST_PATTERNS: Final[tuple[str, ...]] = (
    "*_test.go",
    "test_*.py",
    "*_test.py",
    "*.test.ts",
    "*.test.tsx",
    "*.test.js",
    "*.spec.ts",
    "*.spec.tsx",
    "*.spec.js",
    "*_spec.rb",
    "*Test.java",
    "*Tests.cs",
)

#: Directory names whose presence is test evidence on its own.
_TEST_DIRECTORIES: Final[tuple[str, ...]] = ("tests", "test", "__tests__", "spec", "e2e")

_LOCKFILES: Final[tuple[str, ...]] = (
    "requirements.lock",
    "requirements-dev.lock",
    "poetry.lock",
    "pdm.lock",
    "uv.lock",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "go.sum",
    "cargo.lock",
    "gemfile.lock",
    "composer.lock",
)

#: Files whose presence in a repository is committed key material. Absence scores; it is
#: the one check where the honest signal is a negative, and stating it that way is
#: deliberate — "no private keys in the tree" is a fact about the tree, not about config.
_SECRET_MATERIAL_PATTERNS: Final[tuple[str, ...]] = (
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "id_rsa",
    "id_ed25519",
    "credentials.json",
    "service-account*.json",
)

_LINT_CONFIGS: Final[tuple[str, ...]] = (
    ".pre-commit-config.yaml",
    ".golangci.yml",
    ".golangci.yaml",
    "eslint.config.mjs",
    "eslint.config.js",
    ".eslintrc.json",
    ".eslintrc.js",
    "ruff.toml",
    ".ruff.toml",
    ".flake8",
    "setup.cfg",
)

_CI_PATTERNS: Final[tuple[str, ...]] = (
    ".github/workflows/*.yml",
    ".github/workflows/*.yaml",
    ".gitlab-ci.yml",
    "azure-pipelines.yml",
    ".circleci/config.yml",
    "jenkinsfile",
    ".drone.yml",
    "bitbucket-pipelines.yml",
    ".woodpecker.yml",
)

_COMPOSE_PATTERNS: Final[tuple[str, ...]] = (
    "docker-compose*.yml",
    "docker-compose*.yaml",
    "compose.yml",
    "compose.yaml",
)

_IAC_PATTERNS: Final[tuple[str, ...]] = ("*.tf", "*.tofu", "*.tfvars", "*.bicep")

_ENV_EXAMPLE_PATTERNS: Final[tuple[str, ...]] = (
    ".env.example",
    ".env.sample",
    ".env.template",
    ".env.dist",
    "*.env.example",
)

#: `FROM` at the start of a line. A `FROM` inside a RUN heredoc is not a build stage, and
#: counting one would report a single-stage Dockerfile as multi-stage.
_FROM_LINE = re.compile(r"(?im)^\s*FROM\s+\S+")
#: `USER` with anything other than root/0. A `USER root` is not a non-root container, and
#: the check exists precisely to catch the image that sets USER and sets it to root.
_NON_ROOT_USER = re.compile(r"(?im)^\s*USER\s+(?!root\b|0\b)\S+")
_K8S_KIND = re.compile(r"(?im)^\s*kind:\s*(Deployment|StatefulSet|DaemonSet|CronJob|Service)\b")
_TF_BACKEND = re.compile(r"(?im)^\s*backend\s+\"")


class ReadinessCheck(BaseModel):
    """One checklist item (phases.md §1.4 "Checklist checks", "why it matters").

    `evidence` names the indexed path that satisfied the check, or is empty when nothing
    did. That is what makes a score auditable: a category at 40 without evidence is
    indistinguishable from a bug.
    """

    id: str
    category: str
    passed: bool
    points: int
    max_points: int
    evidence: str = ""
    why_it_matters: str

    # ─── What a reader needs when a check FAILS ──────────────────────────────
    #
    # `evidence` was blanked on failure at every call site (`x if passed else ""`), so the one outcome
    # that most needs explaining reported nothing at all. "Why it matters" states a principle and says
    # nothing about this repository, which left a reader who did not already know the answer with a
    # problem and no way to locate or fix it.
    #
    # All of these come from `readiness_findings.CHECK_EXPLANATIONS` except `found`, which can only
    # come from the code that actually looked.

    #: What would have satisfied the check, in this repository's terms.
    looked_for: str = ""
    #: Where it searched, so "not found" is falsifiable rather than an assertion.
    looked_in: str = ""
    #: What was there instead. Never empty on a failure.
    found: str = ""
    #: The concrete change, as text that can be pasted.
    remedy: str = ""
    #: The file to put it in, or the convention when the path depends on the project.
    remedy_path: str = ""
    #: The line the failing property sits on, when the check parsed something that has lines. `None`
    #: when the failure is an ABSENCE — there is no line number for a file that does not exist, and
    #: inventing one would be worse than admitting there is none.
    line: int | None = None
    #: Whether generation can satisfy this check, so the readiness screen can state the position per
    #: check instead of disclaiming the whole score.
    generatable: bool = False
    #: Why generation cannot, when it cannot. Empty when it can.
    blocked_because: str = ""
    #: The nearest safe thing generation can offer when it cannot do the whole job.
    partial_offer: str = ""


class ReadinessBreakdown(BaseModel):
    """The §1.4 categories, each 0-100.

    Percentages rather than the previous point contributions, because the readiness screen
    renders each value as a bar width in percent — a category worth 20 points rendered as
    "20%" was reporting its weight as its score.
    """

    containerization_score: int
    ci_config_score: int
    orchestration_score: int
    env_config_score: int
    security_policy_score: int
    iac_score: int


class ReadinessResult(BaseModel):
    overall_score: int
    level: str  # "production_ready", "needs_improvement", "blocked"
    breakdown: ReadinessBreakdown
    recommendations: list[str]
    checks: list[ReadinessCheck] = Field(default_factory=list)
    #: False when the project has no indexed files at all. A caller must be able to tell
    #: "scored zero" from "never scanned", and a score alone cannot.
    indexed: bool = True
    #: How many indexed paths the score was computed from. Determinism evidence for the
    #: person reading the number, not a metric.
    evaluated_paths: int = 0


# `IndexEvidence` lives in `core.index_evidence` and is re-exported here for the callers that
# read a score and its input together. It is defined there rather than here because `analysis`
# produces it and this module consumes it, and §2.2.1 bans those two domains from importing each
# other — the shared floor is `core`, and a type both sides name has to sit on it.


def _normalise(paths: Iterable[str]) -> tuple[str, ...]:
    """Lower-cased, slash-separated, de-duplicated, sorted.

    Sorted because the first matching path becomes the `evidence` string, and a score
    whose evidence changes between identical evaluations is not deterministic even when
    the number is.
    """
    seen = {p.replace("\\", "/").strip().removeprefix("./").lower() for p in paths if p and p.strip()}
    return tuple(sorted(seen - {""}))


def apply_ignore_globs(paths: Iterable[str], ignore_globs: Iterable[str] | None) -> tuple[str, ...]:
    """Drop paths the project's `ignore_globs` setting excludes.

    This is the ONLY way `projects.settings` may influence the score, and it is a
    refinement of the index rather than a substitute for it: the operator says which parts
    of the repository are out of scope, and the remaining evidence is still the
    repository's. A glob that matches everything yields an unindexed project, not a
    perfect one.
    """
    normalised = _normalise(paths)
    patterns = [g.replace("\\", "/").lower() for g in (ignore_globs or []) if isinstance(g, str) and g.strip()]
    if not patterns:
        return normalised
    kept = []
    for path in normalised:
        if any(fnmatch.fnmatch(path, pattern) or path.startswith(pattern.rstrip("*/") + "/") for pattern in patterns):
            continue
        kept.append(path)
    return tuple(kept)


def _match(paths: tuple[str, ...], patterns: Iterable[str]) -> str:
    """First path matching any pattern, by basename or by full path."""
    lowered = [p.lower() for p in patterns]
    for path in paths:
        base = posixpath.basename(path)
        for pattern in lowered:
            if fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(base, pattern):
                return path
    return ""


def _match_exact(paths: tuple[str, ...], names: Iterable[str]) -> str:
    wanted = {n.lower() for n in names}
    for path in paths:
        if path in wanted or posixpath.basename(path) in wanted:
            return path
    return ""


def _dockerfile(paths: tuple[str, ...]) -> str:
    return _match(paths, ("dockerfile", "dockerfile.*", "*.dockerfile", "containerfile"))


def _content_of(evidence: IndexEvidence, paths: tuple[str, ...], patterns: Iterable[str]) -> str:
    """Concatenated bodies of every indexed path matching `patterns`.

    Concatenated rather than "the first one": a repository with a backend and a frontend
    Dockerfile is multi-stage if either is, and picking one arbitrarily would make the
    answer depend on sort order.
    """
    lowered = [p.lower() for p in patterns]
    bodies = []
    for path in paths:
        base = posixpath.basename(path)
        if any(fnmatch.fnmatch(path, p) or fnmatch.fnmatch(base, p) for p in lowered):
            body = evidence.contents.get(path)
            if body:
                bodies.append(body)
    return "\n".join(bodies)


def _has_test_evidence(paths: tuple[str, ...]) -> str:
    found = _match(paths, _TEST_PATTERNS)
    if found:
        return found
    for path in paths:
        segments = path.split("/")[:-1]
        if any(segment in _TEST_DIRECTORIES for segment in segments):
            return path
    return ""


def _blocked_reason(explanation: CheckExplanation) -> str:
    """Why the generator cannot satisfy a check, distinguishing the two different reasons.

    A check can be out of reach for two unrelated reasons and a reader needs to know which:

      * the fix is not a file at all - a test has to encode intent, a leaked credential has to be
        rotated at the system that issued it. No amount of generator work changes that, and the table
        states the reason.
      * the fix IS a file and the generator does not emit that kind yet. That is a gap in this product,
        not a fact about the repository, and saying so is the honest form - it tells the reader the work
        is possible and where the limit currently sits.

    Collapsing the two into one sentence would tell somebody a `.env.example` is impossible.
    """
    if not explanation.fixability.generatable:
        return explanation.fixability.blocked_because
    if explanation.artifact not in GENERATED_ARTIFACT_KINDS:
        kind = explanation.artifact or "this kind of file"
        return (
            f"the fix is a file the generator does not emit yet ({kind}), so this is a gap in the "
            "generator rather than something about your repository"
        )
    return ""


def _first_action(remedy: str) -> str:
    """The opening instruction of a remedy, without its worked example.

    A remedy is written to be read in full on the readiness screen, so it carries a block of
    configuration to paste. A recommendation is one line in a list, so it carries the instruction
    and leaves the block to the check it names. Split on the blank line that separates the two
    rather than truncating at a character count, which would cut a sentence mid-clause.
    """
    head = remedy.split("\n\n", 1)[0].strip()
    collapsed = " ".join(head.split())
    # A remedy that introduces its example ends with a colon. Once the example is left behind the colon
    # points at nothing, so it becomes a full stop - the sentence is still a complete instruction.
    if collapsed.endswith(":"):
        collapsed = collapsed[:-1] + "."
    return collapsed


def _recommendation_line(check: ReadinessCheck) -> str:
    """One failed check as an instruction naming the file, the line and the change.

    THIS IS WHAT IT USED TO PRODUCE:

        A tag is mutable, so an unpinned action is code this repository does not control.
        (check: pipeline_actions_pinned)

    A true principle and an identifier. It does not say which action, in which file, on which line,
    or what to change it to, so a reader who did not already know the answer could not act on it -
    and a reader who did know did not need the sentence.

    The order is deliberate: WHERE, then WHAT IS WRONG, then WHAT TO DO, then why, then the id. A
    reader scanning a list is looking for the file first, and the principle is the part they are
    most likely to already accept.
    """
    where = check.remedy_path or check.evidence
    if check.line is not None:
        # Only when the check parsed something with lines. An absence has no line, and inventing
        # one would send a reader confidently to the wrong place.
        where = f"{where}:{check.line}" if where else f"line {check.line}"

    parts: list[str] = []
    if where:
        parts.append(f"{where} -")
    if check.found:
        parts.append(f"{check.found}.")
    action = _first_action(check.remedy)
    if action:
        parts.append(action)
    parts.append(check.why_it_matters)
    parts.append(f"(check: {check.id})")
    return " ".join(part for part in parts if part)


class ReadinessEngine:
    """Deterministic readiness scoring engine.

    Deterministic in the strict sense: the score is a pure function of the evidence, with
    no clock, no randomness and no iteration over an unordered set. `analysis_reports`
    stores the score alongside an `inventory_hash` so two reports can be compared, and
    that comparison is only meaningful if identical evidence scores identically.
    """

    def evaluate(self, evidence: IndexEvidence) -> ReadinessResult:
        """Score indexed repository evidence."""
        paths = _normalise(evidence.paths)
        checks: list[ReadinessCheck] = []

        # ─── Containerization (§1.4) ─────────────────────────────────────────
        dockerfile = _dockerfile(paths)
        docker_body = _content_of(evidence, paths, ("dockerfile", "dockerfile.*", "*.dockerfile", "containerfile"))
        checks.append(
            self._check(
                "dockerfile_present",
                "containerization",
                bool(dockerfile),
                40,
                dockerfile,
                "Without a Dockerfile the deployment target is whatever the last person's machine had installed.",
                found=f"no Dockerfile among the {len(paths)} indexed path(s)",
            )
        )
        multi_stage = len(_FROM_LINE.findall(docker_body)) >= 2
        checks.append(
            self._check(
                "dockerfile_multi_stage",
                "containerization",
                multi_stage,
                25,
                dockerfile if multi_stage else "",
                "A single-stage image ships the compiler and the source alongside the binary, "
                "which is both a larger image and a larger attack surface.",
                found=(
                    f"{dockerfile} has {len(_FROM_LINE.findall(docker_body))} FROM instruction(s); "
                    "a multi-stage build needs at least two"
                    if dockerfile
                    else "there is no Dockerfile to examine"
                ),
            )
        )
        non_root = bool(_NON_ROOT_USER.search(docker_body))
        checks.append(
            self._check(
                "dockerfile_non_root",
                "containerization",
                non_root,
                20,
                dockerfile if non_root else "",
                "A container with no USER runs as root, so a process escape starts with root in the namespace.",
                found=(
                    f"{dockerfile} declares no USER, so the image runs as root"
                    if dockerfile
                    else "there is no Dockerfile to examine"
                ),
            )
        )
        # The two remaining checks the design's Phase 1 list names — "pins a base image digest" and
        # "has a `HEALTHCHECK`" — neither of which existed. Both read INSTRUCTIONS rather than search the
        # text, so a commented-out `HEALTHCHECK` and a `FROM` in a comment do not count.
        base_pinned = dockerfile_base_pinned(docker_body)
        checks.append(
            self._check(
                "dockerfile_base_pinned",
                "containerization",
                base_pinned,
                20,
                dockerfile if base_pinned else "",
                "An unpinned base means two builds of one Dockerfile can produce different images.",
                found=(
                    f"{dockerfile} pins no base image; its FROM instructions use a floating tag"
                    if dockerfile
                    else "there is no Dockerfile to examine"
                ),
            )
        )
        healthcheck = dockerfile_healthcheck(docker_body)
        checks.append(
            self._check(
                "dockerfile_healthcheck_present",
                "containerization",
                healthcheck,
                15,
                dockerfile if healthcheck else "",
                "Without a HEALTHCHECK the runtime cannot tell a wedged container from a busy one.",
                found=(f"{dockerfile} declares no HEALTHCHECK" if dockerfile else "there is no Dockerfile to examine"),
            )
        )
        dockerignore = _match_exact(paths, (".dockerignore",))
        checks.append(
            self._check(
                "dockerignore_present",
                "containerization",
                bool(dockerignore),
                15,
                dockerignore,
                "Without .dockerignore the build context carries .git and local secrets into the image layer cache.",
                found="no .dockerignore at the repository root",
            )
        )
        # A1: SCORED UNDER SECURITY, not containerization, because the fault is not that the image is
        # badly built — it is that a credential now exists in every registry the image reaches, survives
        # `docker history`, and survives being overwritten by a later layer. Rotating the value does not
        # help until the image is rebuilt and the old tags are deleted.
        secrets_ok, secrets_evidence = dockerfile_has_no_baked_secrets(docker_body)
        secrets_audit = dockerfile_secrets_audit(docker_body, dockerfile or "Dockerfile")
        checks.append(
            self._check(
                "dockerfile_no_baked_secrets",
                "security",
                # A BODY THAT WAS NOT READ IS NOT A CLEAN BODY. `docker_body` is empty when the file was
                # found in the tree but its content was never indexed, and passing then would award full
                # marks for a file nobody looked inside — the exact path-presence fault this check exists
                # to remove. The three states are kept apart: no Dockerfile, one that was not read, and
                # one that was read and is clean.
                bool(dockerfile) and bool(docker_body) and secrets_ok,
                DOCKERFILE_NO_BAKED_SECRETS_POINTS,
                secrets_evidence if secrets_ok and docker_body else "",
                "A credential in an ENV or ARG is written into the image layer and travels with the "
                "image to every registry and every host that pulls it.",
                found=(
                    secrets_audit.detail
                    if secrets_audit.detail
                    else (
                        f"{dockerfile} was found but its content was not indexed, so its ENV and ARG "
                        "instructions could not be read"
                        if dockerfile and not docker_body
                        else (
                            "an ENV or ARG assigns a literal value to a name that denotes a credential"
                            if dockerfile
                            else "there is no Dockerfile to examine"
                        )
                    )
                ),
                line=secrets_audit.line,
                earned=(
                    _proportional(secrets_audit, DOCKERFILE_NO_BAKED_SECRETS_POINTS)
                    if dockerfile and secrets_audit.examined
                    else None
                ),
            )
        )

        # ─── CI/CD (§1.4) ────────────────────────────────────────────────────
        ci = _match(paths, _CI_PATTERNS)
        checks.append(
            self._check(
                "ci_pipeline_present",
                "ci_config",
                bool(ci),
                45,
                ci,
                "Without a pipeline definition nothing is verified before a change reaches a branch.",
                found=f"no pipeline definition among the {len(paths)} indexed path(s)",
            )
        )
        # A TEST FILE HAS TO CONTAIN A TEST THAT CAN FAIL.
        #
        # This passed on `_has_test_evidence(paths)` - a path matched `tests/` or `*_test.py`. So
        # `tests/test_placeholder.py` containing nothing earned 35 points, the largest single award in
        # the check set, and a generator asked to raise the score could take it by writing an empty
        # file. That is the defect this whole area keeps producing: a name with nothing behind it.
        #
        # `evidence.has_tests` still wins when set, because a caller may know something the paths do
        # not - but it no longer means "a path looked like a test".
        substance = test_substance(paths, evidence.contents)
        test_path = _has_test_evidence(paths)
        if evidence.has_tests is not None:
            has_tests = evidence.has_tests
        else:
            has_tests = bool(test_path) and substance.passed
        checks.append(
            self._check(
                "automated_tests_present",
                "ci_config",
                has_tests,
                35,
                test_path if has_tests else "",
                "A pipeline with no tests to run reports green for every change, which is worse than no pipeline.",
                found=(
                    "no test file or test directory was indexed"
                    if not test_path
                    else (
                        f"{substance.files} test file(s) were indexed but nothing in them can fail: "
                        f"{substance.cases} case(s), {substance.meaningful_assertions} meaningful "
                        f"assertion(s), {substance.vacuous_assertions} vacuous"
                        + (f" - {'; '.join(substance.examples)}" if substance.examples else "")
                    )
                ),
            )
        )
        lint = _match_exact(paths, _LINT_CONFIGS)
        checks.append(
            self._check(
                "lint_configuration_present",
                "ci_config",
                bool(lint),
                20,
                lint,
                "A committed linter configuration is what makes style and a class of bugs a machine's problem.",
                found="no linter or formatter configuration was indexed",
            )
        )

        # ─── FR-20's "pipeline stages" ────────────────────────────────────────
        #
        # `ci_pipeline_present` matched a PATH, so a workflow file with a `jobs:` key and no steps
        # satisfied it while running nothing. These three read the parsed workflow instead.
        stages_ok, stages_evidence = pipeline_stages_declared(paths, evidence.contents)
        checks.append(
            self._check(
                "pipeline_stages_declared",
                "ci_config",
                stages_ok,
                25,
                stages_evidence,
                "A workflow with no runnable step and no trigger is a file, not a pipeline.",
                found=(
                    stages_evidence
                    or (f"{ci} declares no runnable step or no trigger" if ci else "there is no workflow to examine")
                ),
            )
        )
        ci_tests_ok, ci_tests_evidence = pipeline_runs_tests(paths, evidence.contents)
        checks.append(
            self._check(
                "pipeline_runs_tests",
                "ci_config",
                ci_tests_ok,
                30,
                ci_tests_evidence,
                "A pipeline that does not run the tests reports green for every change.",
                found=(
                    ci_tests_evidence
                    or (f"no step in {ci} invokes a test runner" if ci else "there is no workflow to examine")
                ),
            )
        )
        # Scored only when a workflow exists, for the reason the orchestration block gives: a repository
        # with no CI already fails `ci_pipeline_present`, and a second failure would misdescribe why.
        pinned_audit = pipeline_actions_pinned_audit(paths, evidence.contents)
        pinned_ok, pinned_evidence = pipeline_actions_pinned(paths, evidence.contents)
        checks.append(
            self._check(
                "pipeline_actions_pinned",
                "ci_config",
                pinned_ok if ci else False,
                15,
                # `or ci` is the evidence fallback for the case where the workflow PATH is indexed but
                # its body is not: the check then has nothing to examine and no finding to cite, and
                # `test_every_check_that_passes_names_its_evidence` requires a passing check to name
                # something. The workflow is the honest citation, since it is the file the answer is
                # about.
                pinned_evidence or ci,
                "A tag is mutable, so an unpinned action is code this repository does not control.",
                found=(
                    pinned_audit.detail
                    or (
                        f"{ci} references at least one action by tag rather than by commit"
                        if ci
                        else "there is no workflow to examine"
                    )
                ),
                line=pinned_audit.line,
                earned=(
                    _proportional(pinned_audit, PIPELINE_ACTIONS_PINNED_POINTS)
                    if ci and pinned_audit.examined
                    else None
                ),
            )
        )

        # ─── Orchestration (§1.4) ────────────────────────────────────────────
        k8s_body = _content_of(evidence, paths, ("*.yaml", "*.yml"))
        k8s_path = _match(paths, ("k8s/*", "kubernetes/*", "deploy/*.yaml", "manifests/*.yaml"))
        has_k8s = bool(_K8S_KIND.search(k8s_body)) or bool(k8s_path)
        checks.append(
            self._check(
                "kubernetes_manifests_present",
                "orchestration",
                has_k8s,
                45,
                k8s_path,
                "Without a manifest the runtime shape - replicas, probes, limits - lives only in a shell history.",
                found="no manifest declaring a Kubernetes kind was indexed under any conventional directory",
            )
        )
        helm = _match_exact(paths, ("chart.yaml",))
        checks.append(
            self._check(
                "helm_chart_present",
                "orchestration",
                bool(helm),
                30,
                helm,
                "A chart is what makes the same manifests deployable to another environment unedited.",
                found="no Chart.yaml was indexed",
            )
        )
        compose = _match(paths, _COMPOSE_PATTERNS)
        checks.append(
            self._check(
                "compose_file_present",
                "orchestration",
                bool(compose),
                25,
                compose,
                "A compose file is the reproducible local topology; without one, 'works here' is unfalsifiable.",
                found="no compose file was indexed",
            )
        )

        # ─── FR-20's missing orchestration checks ─────────────────────────────
        #
        # These three had no implementation. A manifest with no `resources` block, no probes and
        # `image: app:latest` scored full marks for orchestration, because the only question asked was
        # whether a manifest existed. FR-20 names "K8s resource limits" explicitly.
        #
        # Each is scored ONLY when a manifest exists. Failing a repository with no Kubernetes for having
        # no resource limits would double-count the absence: `kubernetes_manifests_present` already
        # reports it, and the second failure would say the manifests are wrong rather than absent.
        # THE AUDITS, not the booleans, for the three container checks and for action pinning.
        #
        # `(passed, path)` short-circuits on the first offender, which cannot express "four of five
        # containers are correct" and cannot name which property was missing on which line. Points were
        # therefore all-or-nothing: a user who fixed four containers of five saw the score not move,
        # which teaches that the number does not respond to work.
        limits_audit = kubernetes_resource_limits_audit(paths, evidence.contents)
        limits_ok, limits_evidence = kubernetes_resource_limits(paths, evidence.contents)
        checks.append(
            self._check(
                "kubernetes_resource_limits_declared",
                "orchestration",
                limits_ok if has_k8s else False,
                35,
                limits_evidence,
                "An unbounded container evicts its neighbours; one missing limit is enough to take a node.",
                found=(
                    limits_audit.detail
                    if limits_audit.detail
                    else (
                        "at least one container declares no cpu/memory requests and limits"
                        if has_k8s
                        else "there is no manifest to examine"
                    )
                ),
                line=limits_audit.line,
                earned=(
                    _proportional(limits_audit, KUBERNETES_RESOURCE_LIMITS_DECLARED_POINTS)
                    if has_k8s and limits_audit.examined
                    else None
                ),
            )
        )
        probes_audit = kubernetes_probes_audit(paths, evidence.contents)
        probes_ok, probes_evidence = kubernetes_probes(paths, evidence.contents)
        checks.append(
            self._check(
                "kubernetes_probes_declared",
                "orchestration",
                probes_ok if has_k8s else False,
                25,
                probes_evidence,
                "Without probes a wedged container keeps serving traffic, because nothing is asking it.",
                found=(
                    probes_audit.detail
                    if probes_audit.detail
                    else (
                        "at least one container declares no livenessProbe and readinessProbe"
                        if has_k8s
                        else "there is no manifest to examine"
                    )
                ),
                line=probes_audit.line,
                earned=(
                    _proportional(probes_audit, KUBERNETES_PROBES_DECLARED_POINTS)
                    if has_k8s and probes_audit.examined
                    else None
                ),
            )
        )
        tags_audit = kubernetes_image_tags_audit(paths, evidence.contents)
        tags_ok, tags_evidence = kubernetes_image_tags_pinned(paths, evidence.contents)
        checks.append(
            self._check(
                "kubernetes_image_tags_pinned",
                "orchestration",
                tags_ok if has_k8s else False,
                20,
                tags_evidence,
                "A `latest` tag means two deployments of one manifest can run different code.",
                found=(
                    tags_audit.detail
                    if tags_audit.detail
                    else (
                        "at least one container image uses latest or carries no tag"
                        if has_k8s
                        else "there is no manifest to examine"
                    )
                ),
                line=tags_audit.line,
                earned=(
                    _proportional(tags_audit, KUBERNETES_IMAGE_TAGS_PINNED_POINTS)
                    if has_k8s and tags_audit.examined
                    else None
                ),
            )
        )

        # ─── A1: two content faults that a path check awards full marks for ───
        #
        # Both live inside a file whose EXISTENCE already scores, which is what makes them worth reading
        # the body for: `kubernetes_manifests_present` passes on a Deployment the API server would reject.
        privileged_audit = kubernetes_privileged_audit(paths, evidence.contents)
        privileged_ok, privileged_evidence = kubernetes_containers_are_unprivileged(paths, evidence.contents)
        checks.append(
            self._check(
                "kubernetes_containers_unprivileged",
                "orchestration",
                privileged_ok if has_k8s else False,
                KUBERNETES_UNPRIVILEGED_POINTS,
                privileged_evidence if privileged_ok else "",
                "A privileged container shares the host's kernel boundaries, so a compromise inside it "
                "is a compromise of the node.",
                found=(
                    privileged_audit.detail
                    if privileged_audit.detail
                    else (
                        "at least one container runs privileged or shares a host namespace"
                        if has_k8s
                        else "there is no manifest to examine"
                    )
                ),
                line=privileged_audit.line,
                earned=(
                    _proportional(privileged_audit, KUBERNETES_UNPRIVILEGED_POINTS)
                    if has_k8s and privileged_audit.examined
                    else None
                ),
            )
        )
        schema_audit = kubernetes_schema_audit(paths, evidence.contents)
        schema_ok, schema_evidence = kubernetes_manifests_are_well_formed(paths, evidence.contents)
        checks.append(
            self._check(
                "kubernetes_manifests_are_valid",
                "orchestration",
                schema_ok if has_k8s else False,
                KUBERNETES_MANIFEST_VALID_POINTS,
                schema_evidence if schema_ok else "",
                "A document missing apiVersion, kind or metadata.name is rejected by `kubectl apply`, so "
                "the deploy stops rather than degrades.",
                found=(
                    schema_audit.detail
                    if schema_audit.detail
                    else (
                        "at least one document omits apiVersion, kind or metadata.name"
                        if has_k8s
                        else "there is no manifest to examine"
                    )
                ),
                line=schema_audit.line,
                earned=(
                    _proportional(schema_audit, KUBERNETES_MANIFEST_VALID_POINTS)
                    if has_k8s and schema_audit.examined
                    else None
                ),
            )
        )

        # ─── Env Config (§1.4) ───────────────────────────────────────────────
        env_example = _match(paths, _ENV_EXAMPLE_PATTERNS)
        checks.append(
            self._check(
                "env_example_present",
                "env_config",
                bool(env_example),
                40,
                env_example,
                "Without a checked-in example, the variables the service needs are found only by crashing it.",
                found="no .env.example, .env.sample, .env.template or example.env was indexed",
            )
        )
        committed_env = _match_exact(paths, (".env",))
        checks.append(
            self._check(
                "no_committed_env_file",
                "env_config",
                # `and bool(paths)`: this and `no_committed_key_material` are the two checks
                # whose passing condition is an ABSENCE, and an empty index satisfies every
                # absence trivially. Without this an unscanned project scored 8/100 for the
                # bad files it did not have, which is a conclusion drawn from no evidence.
                not committed_env and bool(paths),
                35,
                committed_env,
                "A committed .env puts live credentials in every clone and in the image build context.",
                found=(
                    f"{committed_env} is committed to the repository"
                    if committed_env
                    else "nothing was indexed, so the absence of a .env proves nothing"
                ),
            )
        )
        # CONFIGURATION HAS TO ACTUALLY FLOW THROUGH THE MODULE.
        #
        # This asked `bool(config_dir)` - does a path matching `config/*` exist. A directory proves
        # nothing, and the failure the check exists to catch survives it intact: a service that reads
        # `os.getenv("DATABASE_URL")` in six modules and dies in production because the seventh spelled
        # it differently HAS a `config/` directory. Worse, a generator could earn 25 points by writing an
        # empty module, which is the score moving while the project does not improve.
        #
        # Now every environment read in the project's own sources is located by parsing, and the check
        # asks whether they happen in the configuration module. Partial credit is proportional, so
        # fixing four of five reads moves the number - a check that does not respond to work teaches
        # that the work is pointless.
        config_flow = centralisation(paths, evidence.contents)
        config_dir = config_flow.module_path or _match(
            paths, ("config/*", "configs/*", "*/settings.py", "*/config.py", "*.config.ts")
        )
        # A passing check must cite a file - `test_every_check_that_passes_names_its_evidence` enforces
        # it, and rightly: a pass with no evidence tells an operator nothing about why it passed. When
        # there is nothing to centralise the honest citation is the source that WAS examined and found
        # to read no environment, which is the same reasoning `pipeline_actions_pinned` uses when a
        # workflow references no external action.
        config_evidence = config_dir or config_flow.examined_path
        checks.append(
            self._check(
                "centralised_configuration",
                "env_config",
                # `bool(paths) and` is not redundant: an UNSCANNED project has no reads to find, and
                # without this it would pass for having nothing scattered - the fail-open reading of an
                # absent scan. `no_secrets_found_by_scan` guards the same way for the same reason.
                #
                # A PROJECT WITH NO ENVIRONMENT READS PASSES WITHOUT NEEDING A MODULE, and that clause is
                # what closes the last way to earn these points for nothing. Requiring `config_dir`
                # unconditionally meant a generator could write an empty `config/settings.py` into a
                # project that reads no environment at all and collect 25 points for an artifact that
                # centralised nothing. Now the module only earns credit when there is something for it to
                # hold, so the points follow the work rather than the filename.
                bool(paths)
                and config_flow.passed
                # Something must have been READ for a pass to mean anything. A repository of YAML with
                # no source files offers nothing to examine, and passing it for having no scattered
                # reads would be the same fail-open reading as passing an unscanned project.
                and bool(config_evidence)
                and (config_flow.total == 0 or bool(config_dir)),
                25,
                config_evidence if config_flow.passed else "",
                "Configuration read in one validated place fails at boot; read ad hoc it fails in production.",
                found=(
                    f"{config_flow.scattered} of {config_flow.total} environment read(s) happen outside "
                    f"{config_dir or 'any configuration module'}: {'; '.join(config_flow.examples)}"
                    if config_flow.scattered
                    else (
                        "no configuration module or config directory was indexed"
                        if not config_dir
                        else "no environment reads were found to centralise"
                    )
                ),
                earned=(
                    _earned_from_counts(config_flow.centralised, config_flow.total, 25)
                    if config_flow.total and config_dir
                    else None
                ),
            )
        )

        # ─── Security (§1.4) ─────────────────────────────────────────────────
        policy = _match(paths, ("security.md", "policies/*", "*.rego", "policies/*.yaml"))
        checks.append(
            self._check(
                "security_policy_present",
                "security_policy",
                bool(policy),
                30,
                policy,
                "A written policy is what turns a security decision into something reviewable rather than remembered.",
                found="no SECURITY.md and no policy-as-code source was indexed",
            )
        )
        secret_scanning = _match_exact(paths, (".gitleaks.toml", ".trufflehog.yaml", ".secrets.baseline"))
        checks.append(
            self._check(
                "secret_scanning_configured",
                "security_policy",
                bool(secret_scanning),
                25,
                secret_scanning,
                "Secret scanning in the repository is the only control that catches a credential before it is pushed.",
                found="no secret-scanner configuration was indexed",
            )
        )
        # FR-42's other half, and the one that was missing. `secret_scanning_configured` asks whether a
        # scanner is CONFIGURED; this asks what the scan FOUND. The agent's scanner ran on every index and
        # `file_contents.redaction_count` has recorded the answer since revision `0003`, and nothing read
        # it — so "secret scanning of the codebase" happened every time and was invisible afterwards.
        #
        # A redaction IS a finding: the scanner matched something and the value did not survive into the
        # database. The check reports the worst-affected PATH and never a value, for the reason §7.11
        # gives.
        worst_offender = max(evidence.redaction_counts.items(), key=lambda item: (item[1], item[0]), default=("", 0))[0]
        checks.append(
            self._check(
                "no_secrets_found_by_scan",
                "security_policy",
                # `bool(paths) and` is not redundant: an UNINDEXED project has an empty
                # `redaction_counts` and would otherwise pass, which is the fail-open reading of an absent
                # scan — no findings and no assurance are not the same thing.
                # `test_an_unscanned_project_scores_zero_and_says_it_is_unindexed` caught it, and the
                # `/secrets` route makes the same distinction through its `clean` field.
                bool(paths) and not evidence.redaction_counts,
                30,
                worst_offender,
                "A credential in the tree is already disclosed to everyone who can read the repository.",
                found=(
                    f"{worst_offender} required "
                    f"{evidence.redaction_counts.get(worst_offender, 0)} redaction(s) during the scan"
                    if worst_offender
                    else "nothing was indexed, so no scan result exists to report"
                ),
            )
        )
        lockfile = _match_exact(paths, _LOCKFILES)
        checks.append(
            self._check(
                "dependency_lockfile_present",
                "security_policy",
                bool(lockfile),
                25,
                lockfile,
                "Without a lockfile the dependency tree differs per build, so a fix cannot be proven applied.",
                found="no lockfile was indexed for any package manager",
            )
        )
        key_material = _match(paths, _SECRET_MATERIAL_PATTERNS)
        checks.append(
            self._check(
                "no_committed_key_material",
                "security_policy",
                # See `no_committed_env_file`: an absence is not evidence when nothing was read.
                not key_material and bool(paths),
                20,
                key_material,
                "A private key in the tree is compromised the moment the repository is cloned, and history keeps it.",
                found=(
                    f"{key_material} looks like private key material and is committed"
                    if key_material
                    else "nothing was indexed, so the absence of key material proves nothing"
                ),
            )
        )

        # ─── IaC (§1.4) ──────────────────────────────────────────────────────
        iac = _match(paths, _IAC_PATTERNS)
        checks.append(
            self._check(
                "iac_sources_present",
                "iac",
                bool(iac),
                50,
                iac,
                "Infrastructure defined in code is reviewable and revertible; a console click is neither.",
                found="no OpenTofu, Terraform, Pulumi or CloudFormation source was indexed",
            )
        )
        iac_lock = _match_exact(paths, (".terraform.lock.hcl",))
        checks.append(
            self._check(
                "iac_provider_lock_present",
                "iac",
                bool(iac_lock),
                25,
                iac_lock,
                "Without a provider lock, the same plan can produce different infrastructure on different days.",
                found=(
                    f"{iac} declares infrastructure but no .terraform.lock.hcl was indexed beside it"
                    if iac
                    else "no .terraform.lock.hcl was indexed"
                ),
            )
        )
        iac_body = _content_of(evidence, paths, _IAC_PATTERNS)
        remote_state = bool(_TF_BACKEND.search(iac_body))
        checks.append(
            self._check(
                "iac_remote_state_configured",
                "iac",
                remote_state,
                25,
                iac if remote_state else "",
                "Local state cannot be shared or locked, so two applies can silently overwrite each other.",
                found=(
                    f"{iac} declares no backend block, so state is written locally"
                    if iac
                    else "there is no infrastructure source to examine"
                ),
            )
        )

        # ─── Dependency manifest (ADD1) ──────────────────────────────────────
        #
        # `dependency_lockfile_present` above asks whether a lockfile EXISTS. These ask whether the
        # manifest and the code AGREE, which is stronger: a repository can hold a manifest, a lockfile,
        # and a build that only works on the machine where somebody installed the missing package by
        # hand. `file_dependencies` has recorded the import graph since revision `0003` and the score
        # never read it, so that failure was invisible.
        #
        # Scored per ECOSYSTEM the scan detected, and only for ecosystems this can parse. An ecosystem
        # it cannot read produces no check rather than a passing one — "I cannot judge this" and
        # "this is fine" are different answers.
        for ecosystem in sorted(ECOSYSTEM_FILES):
            facts = reconcile(
                ecosystem=ecosystem,
                paths=paths,
                contents=evidence.contents,
                specifiers=evidence.dependency_specifiers,
            )
            # Nothing of this ecosystem in the repository at all: no manifest, no imports. Emitting a
            # failure would fault a Python project for having no Cargo.toml.
            if facts is None or (not facts.imported and not facts.declared):
                continue

            checks.append(
                self._check(
                    "dependency_manifest_present",
                    "env_config",
                    bool(facts.manifest_path),
                    20,
                    facts.manifest_path,
                    "Undeclared dependencies exist only in whatever was installed on the machine the "
                    "build last worked on.",
                    found=(
                        f"the scan found {ecosystem} imports and no manifest declaring them"
                        if not facts.manifest_path
                        else ""
                    ),
                )
            )
            if not facts.manifest_path:
                # Without declarations the three comparisons below have nothing to compare against, and
                # the check above already reports the absence.
                continue

            # THE NAMES, not a count. "dependencies are undeclared" sends a reader to read the whole
            # manifest; naming `requests` sends them to one line.
            shown_undeclared = ", ".join(facts.undeclared[:8])
            checks.append(
                self._check(
                    "every_imported_package_is_declared",
                    "env_config",
                    not facts.undeclared,
                    25,
                    facts.manifest_path,
                    "An undeclared import is a build that already fails on any clean checkout.",
                    found=(
                        f"{facts.manifest_path} does not declare "
                        f"{len(facts.undeclared)} imported package(s): {shown_undeclared}"
                        f"{'…' if len(facts.undeclared) > 8 else ''}"
                        if facts.undeclared
                        else ""
                    ),
                )
            )
            shown_unused = ", ".join(facts.unused[:8])
            checks.append(
                self._check(
                    "no_unused_declared_packages",
                    "env_config",
                    not facts.unused,
                    10,
                    facts.manifest_path,
                    "An unused dependency is install time, image size and attack surface bought for nothing.",
                    found=(
                        f"{facts.manifest_path} declares {len(facts.unused)} package(s) nothing "
                        f"imports: {shown_unused}{'…' if len(facts.unused) > 8 else ''}"
                        if facts.unused
                        else ""
                    ),
                )
            )
            shown_unpinned = ", ".join(facts.unpinned[:8])
            checks.append(
                self._check(
                    "declared_versions_are_pinned",
                    "env_config",
                    not facts.unpinned,
                    15,
                    facts.lockfile_path or facts.manifest_path,
                    "A floating constraint means two builds of identical source can install different code.",
                    found=(
                        f"{facts.manifest_path} leaves {len(facts.unpinned)} package(s) unpinned with "
                        f"no lockfile to pin them: {shown_unpinned}"
                        f"{'…' if len(facts.unpinned) > 8 else ''}"
                        if facts.unpinned
                        else ""
                    ),
                )
            )

        # ─── Cross-category consistency ──────────────────────────────────────
        #
        # Everything above judges one artifact at a time, so a repository can hold six internally
        # correct categories that describe six different pieces of software and score full marks. These
        # three compare two categories against each other.
        #
        # Each is scored ONLY when both sides exist. `comparable=False` means there was nothing to
        # reconcile, and scoring that as a failure would report a missing artifact twice — once
        # honestly, by the check that looks for it, and once as a consistency fault it is not.
        builds = ci_builds_an_image_that_has_a_dockerfile(paths, evidence.contents, dockerfile)
        if builds.comparable:
            checks.append(
                self._check(
                    "ci_builds_an_existing_dockerfile",
                    "ci_config",
                    builds.passed,
                    20,
                    builds.evidence,
                    "A pipeline whose build step cannot find its Dockerfile fails on every run, and "
                    "both halves look correct on their own.",
                    found=builds.detail,
                    line=builds.line,
                )
            )

        produced = kubernetes_images_are_produced_by_ci(paths, evidence.contents)
        if produced.comparable:
            checks.append(
                self._check(
                    "kubernetes_images_are_built_here",
                    "orchestration",
                    produced.passed,
                    20,
                    produced.evidence,
                    "A manifest deploying an image nothing here pushes applies whatever is in the "
                    "registry under that name, which this repository never produced or reviewed.",
                    found=produced.detail,
                    line=produced.line,
                )
            )

        example_body = _content_of(evidence, paths, _ENV_EXAMPLE_PATTERNS)
        documented = env_example_matches_the_deployment(env_example, example_body, paths, evidence.contents)
        if documented.comparable:
            checks.append(
                self._check(
                    "env_example_matches_the_deployment",
                    "env_config",
                    documented.passed,
                    20,
                    documented.evidence,
                    "An incomplete example reads as a complete list, so somebody follows it exactly "
                    "and gets a service that cannot start.",
                    found=documented.detail,
                    line=documented.line,
                )
            )

        # ─── A1b: what the real external tools said about the user's own artifacts ───
        #
        # EMITTED ONLY WHEN A TOOL REACHED A CONCLUSION. A project whose agent is older, or whose developer
        # has none of the binaries installed, produces no check at all — "your artifacts are valid" and
        # "nothing here could tell me" are different claims and only the first is a readiness statement.
        # The agent's validator package exists because the implementations it replaced fabricated passes
        # for anything they did not understand, and emitting a pass here would move that defect one layer
        # up, where it is harder to see and easier to trust.
        verdicts = summarise(evidence.artifact_validations)
        if verdicts.conclusive:
            checks.append(
                self._check(
                    "artifacts_pass_their_validators",
                    "orchestration",
                    verdicts.passed,
                    ARTIFACT_VALIDATION_POINTS,
                    f"{verdicts.satisfied} of {verdicts.judged} artifact(s) satisfied their validator"
                    if verdicts.passed
                    else "",
                    "An artifact the deployment tool itself rejects cannot deploy, whatever the rest of "
                    "the repository looks like.",
                    found=verdicts.first_failure_detail or _NOTHING_FOUND,
                    line=verdicts.failures[0][4] if verdicts.failures else None,
                    earned=_earned_from_counts(verdicts.satisfied, verdicts.judged, ARTIFACT_VALIDATION_POINTS),
                )
            )

        category_scores = self._category_scores(checks)
        overall = round(sum(CATEGORY_WEIGHTS[c] * category_scores[c] for c in CATEGORY_WEIGHTS) / 100)
        overall = min(max(overall, 0), 100)

        if overall >= 80:
            level = "production_ready"
        elif overall >= 50:
            level = "needs_improvement"
        else:
            level = "blocked"

        return ReadinessResult(
            overall_score=overall,
            level=level,
            breakdown=ReadinessBreakdown(
                **{CATEGORY_FIELDS[name]: score for name, score in category_scores.items()},
            ),
            recommendations=self._recommendations(checks, indexed=bool(paths)),
            checks=checks,
            indexed=bool(paths),
            evaluated_paths=len(paths),
        )

    def evaluate_project(self, project_data: dict[str, Any]) -> ReadinessResult:
        """Score a project from a mapping of evidence.

        Retained as the engine's dict-shaped entry point, but its meaning has changed: the
        keys are now READ AS PATHS from the index, not as configuration. `paths` is the
        preferred key; `manifests` and `config_files` are accepted because they are what
        the agent inventory calls the same thing, and `has_tests` is honoured when stated
        and DERIVED when absent — it no longer defaults to true.
        """
        paths: list[str] = []
        for key in ("paths", "manifests", "config_files"):
            value = project_data.get(key)
            if isinstance(value, str):
                paths.append(value)
            elif isinstance(value, Iterable):
                paths.extend(str(v) for v in value)

        stated = project_data.get("has_tests")
        contents = project_data.get("contents")
        return self.evaluate(
            IndexEvidence(
                paths=tuple(paths),
                contents=dict(contents) if isinstance(contents, Mapping) else {},
                has_tests=stated if isinstance(stated, bool) else None,
            )
        )

    @staticmethod
    def _check(
        check_id: str,
        category: str,
        passed: bool,
        max_points: int,
        evidence: str,
        why_it_matters: str,
        *,
        found: str = "",
        line: int | None = None,
        earned: int | None = None,
    ) -> ReadinessCheck:
        """Build one check, joining what was observed to the explanation of what was wanted.

        `earned` enables PARTIAL scoring. Points were `max_points if passed else 0`, so a check
        examining several things — three containers, two workflows — was all-or-nothing: two correct
        containers and one without limits scored the same zero as three without. Callers that can
        count pass a proportion; callers whose check is genuinely binary do not, and behave as before.

        `found` is what was there instead, and its absence is what produced the empty `Evidence:` a
        reader could do nothing with. Ignored on a pass: a passing check is already described by its
        evidence, and repeating it as 'what was found instead' would read as a complaint about the
        file that satisfied the check.
        """
        explanation = CHECK_EXPLANATIONS.get(check_id)
        if explanation is None:
            # Loud rather than silent. A check with no explanation is the exact state the table exists
            # to prevent, and a blank fallback would reintroduce it quietly.
            raise KeyError(
                f"readiness check {check_id!r} has no entry in CHECK_EXPLANATIONS; add one so a "
                "failure can be explained to somebody who does not already know the answer"
            )

        # A PASS ALWAYS SCORES FULL MARKS, and `earned` is consulted only on a failure.
        #
        # Pass/fail comes from the check's own predicate and the proportion comes from a counting audit,
        # and the two can legitimately count different things - the audit may include a container the
        # predicate skips. Letting `earned` reduce a passing check produced a row reading "Pass" beside
        # "17 of 35", which is incoherent: a reader cannot tell whether they have work left to do.
        # `test_the_score_is_bounded_and_the_levels_follow_it` caught it by scoring a correct repository
        # 98 instead of 100.
        if passed:
            points = max_points
        elif earned is None:
            points = 0
        else:
            points = max(0, min(int(earned), max_points))

        return ReadinessCheck(
            id=check_id,
            category=category,
            passed=passed,
            points=points,
            max_points=max_points,
            # The path that DECIDED the check, whichever way it went. For a positive check
            # that is the file that satisfied it, and is empty when nothing did; for an
            # absence check (`no_committed_env_file`) it is the offending file, which is the
            # single most useful thing a failed check can report.
            evidence=evidence,
            why_it_matters=why_it_matters,
            looked_for=explanation.looked_for,
            looked_in=explanation.looked_in,
            found=("" if passed else (found or _NOTHING_FOUND)),
            remedy="" if passed else explanation.remedy,
            remedy_path="" if passed else explanation.remedy_path,
            line=line,
            # WHAT THE GENERATOR CAN DO TODAY, not what is generatable in principle. A screen that
            # offered a CI workflow the generator cannot emit would be the blanket disclaimer's mistake
            # inverted - a promise instead of a refusal, and equally untrue.
            generatable=explanation.generatable_today,
            blocked_because=_blocked_reason(explanation),
            partial_offer=explanation.fixability.partial_offer,
        )

    @staticmethod
    def _category_scores(checks: list[ReadinessCheck]) -> dict[str, int]:
        scores: dict[str, int] = {}
        for category in CATEGORY_WEIGHTS:
            earned = sum(c.points for c in checks if c.category == category)
            possible = sum(c.max_points for c in checks if c.category == category)
            # `possible` is a constant of the check table, never zero; guarded anyway
            # because a category whose checks were all removed should score 0 rather
            # than raise.
            scores[category] = round(100 * earned / possible) if possible else 0
        return scores

    @staticmethod
    def _recommendations(checks: list[ReadinessCheck], *, indexed: bool) -> list[str]:
        """Failed checks as plain-language actions, heaviest category first.

        The unindexed case is called out explicitly rather than producing eighteen
        recommendations: telling an operator to add a Dockerfile when the repository has
        never been scanned is advice about a repository nobody has read.
        """
        if not indexed:
            return [
                "No indexed files for this project: run an agent scan so readiness is measured "
                "from the repository rather than assumed."
            ]
        failed = [c for c in checks if not c.passed]
        failed.sort(key=lambda c: (-CATEGORY_WEIGHTS.get(c.category, 0), -c.max_points, c.id))
        return [_recommendation_line(c) for c in failed]
