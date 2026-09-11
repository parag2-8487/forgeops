# SPDX-License-Identifier: FSL-1.1-ALv2
"""Invariant 1, as a rule two callers share: an artifact must pass the checks it targets.

THE DEFECT THIS CLOSES, MEASURED

`scripts/check-template-readiness.py` proved the template library failed nine of the checks it was
offered as the fix for, and those templates were repaired. But the gate only ever saw templates, and
the model path had no equivalent. Then a real run through `POST /generation/runs` on the repaired code
did this:

    served_from='provider', completion_tokens=296, findings=[], change_set applied
    k8s/deployment.yaml on disk afterwards: 335 bytes, probes=False, resources=False, image=:latest

The instruction named `kubernetes_probes_declared`, `kubernetes_resource_limits_declared` and
`kubernetes_image_tags_pinned`, quoting the offending line numbers. The model returned a Deployment
failing all three. §11.5.5's gate passed it with ZERO findings, because that gate asks "is this well
formed and the right shape" and the answer was yes. The change set applied and the score did not move.

So fixing the templates fixed the floor and left the ceiling open. The question "does this artifact
satisfy the check it was generated to satisfy" was asked of templates in CI and of nothing at runtime.

WHY THE RULE LIVES HERE AND NOT IN EITHER CALLER

`GenerationService._validate` needs it to withhold a bad artifact, and the CI gate needs it to fail a
bad template. Two implementations of "does this artifact satisfy its check" would drift, and the
drift would be invisible: each would pass its own suite. So the rule is here, and both import it.

HOW IT DECIDES

The product's own tables, never a copy:

  * `CHECK_EXPLANATIONS[id].artifact` — which artifact kind fixes which check
  * `generatable_today` — that AND-ed with what the generator actually emits
  * `ReadinessEngine.evaluate` — the same scorer the readiness screen uses, pure over `IndexEvidence`

A check is enforced against an artifact only when the artifact's KIND is the kind the check names. A
Dockerfile is never failed for a Kubernetes check, and a repository with no manifests is never failed
for manifest content — the same scoping rule the checks themselves use.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Final

from .index_evidence import IndexEvidence
from .readiness import ReadinessEngine
from .readiness_findings import CHECK_EXPLANATIONS, GENERATED_ARTIFACT_KINDS

#: Path shape to the artifact kind `CHECK_EXPLANATIONS` names. First match wins, so the specific
#: `.github/workflows/` rule precedes anything that would also match a bare YAML file.
#:
#: `unmapped_kinds` asserts this covers every member of `GENERATED_ARTIFACT_KINDS`, so a kind added to
#: the product without a rule here becomes a failure rather than an artifact audited by nothing — the
#: defect class this module exists to catch, applied to the module.
_PATH_KINDS: Final[tuple[tuple[str, str], ...]] = (
    (".github/workflows/", "github_workflow"),
    ("k8s/", "k8s"),
    ("charts/", "helm"),
    ("helm/", "helm"),
    (".dockerignore", "dockerignore"),
    (".env.example", "env_example"),
    ("docker-compose", "compose"),
    ("dockerfile", "dockerfile"),
    (".tf", "opentofu"),
    ("package.json", "dependency_manifest"),
    ("requirements.txt", "dependency_manifest"),
    ("pyproject.toml", "dependency_manifest"),
    ("go.mod", "dependency_manifest"),
    (".gitleaks.toml", "secret_scanner_config"),
    ("security.md", "security_policy"),
    (".eslintrc", "lint_config"),
    (".ruff.toml", "lint_config"),
)


def kind_for_path(path: str) -> str:
    """The artifact kind a repository path represents, or `""` when it is not a generated kind."""
    lowered = path.replace("\\", "/").lower()
    for needle, kind in _PATH_KINDS:
        if needle in lowered:
            return kind
    return ""


def unmapped_kinds() -> tuple[str, ...]:
    """Kinds the product claims it generates that no rule above recognises."""
    mapped = {kind for _needle, kind in _PATH_KINDS}
    return tuple(sorted(set(GENERATED_ARTIFACT_KINDS) - mapped))


def score_files(files: Mapping[str, str]) -> object:
    """Score a synthetic repository made of exactly these files, with the product's own engine."""
    return ReadinessEngine().evaluate(
        IndexEvidence(
            paths=tuple(sorted(files)),
            contents={path.lower(): body for path, body in files.items()},
        )
    )


def score_delta(
    files: Sequence[tuple[str, str]] | Mapping[str, str],
    existing: Mapping[str, str] | None = None,
) -> tuple[int, int, tuple[str, ...]]:
    """`(before, after, per_check_losses)` for applying `files` over `existing`.

    Invariant 2: no change set may lower the readiness score.

    WHY AN AGGREGATE CHECK WHEN `content_regression` ALREADY EXISTS. That guard is per-property and
    per-file: it catches a rewrite that drops the probes from the Deployment it replaces. It cannot see
    a change set that improves one file and quietly costs points somewhere else - a rewritten workflow
    that no longer builds the Dockerfile, say, where nothing was removed from any single file's own
    guarded list but `ci_builds_an_existing_dockerfile` stops holding. The score is the thing the
    operator is watching, so the score is what has to be defended.

    Returns the numbers rather than a verdict so the caller can decide, and so the reachable-score
    contract can quote the same arithmetic rather than recomputing it differently.
    """
    produced: dict[str, str] = dict(files) if isinstance(files, Mapping) else {p: b for p, b in files}
    baseline = dict(existing or {})
    if not baseline:
        # Nothing to compare against. A first-time generation into an unindexed project cannot lower a
        # score that does not exist, and inventing a zero baseline would report every artifact as an
        # improvement, which is true but says nothing.
        return (0, 0, ())

    merged = dict(baseline)
    merged.update(produced)

    before = score_files(baseline)
    after = score_files(merged)

    before_checks = {c.id: c for c in before.checks}  # type: ignore[attr-defined]
    losses: list[str] = []
    for check in after.checks:  # type: ignore[attr-defined]
        was = before_checks.get(check.id)
        if was is not None and check.points < was.points:
            losses.append(
                f"{check.id} falls from {was.points} to {check.points} of {check.max_points}"
                f"{'; ' + check.evidence if check.evidence else ''}"
            )
    return (before.overall_score, after.overall_score, tuple(losses))  # type: ignore[attr-defined]


def score_lowering_findings(
    files: Sequence[tuple[str, str]] | Mapping[str, str],
    existing: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """A refusal naming the check and the delta, when the change set would lower the score.

    Deliberately NOT prefixed with a path. D-106's per-file withholding answers "which artifact is bad";
    this answers "this set, taken together, makes the repository worse", and withholding one file from
    it could leave a partial application that is worse still. The whole set is refused.
    """
    before, after, losses = score_delta(files, existing)
    if after >= before:
        return ()
    detail = "; ".join(losses) if losses else "no single check regressed, so the loss is in partial credit"
    return (f"this change set lowers the readiness score from {before} to {after} ({after - before}): {detail}",)


def unsatisfied_targets(
    files: Sequence[tuple[str, str]] | Mapping[str, str],
    existing: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """Checks a generated artifact's kind targets and the artifact still does not satisfy.

    `files` is what this run produced, `existing` what the index already holds. The score is computed
    over the UNION, because that is the repository as it would be after the apply — judging the
    generated files alone would fail a Deployment for a Dockerfile check that a perfectly good existing
    Dockerfile already satisfies.

    Only kinds THIS RUN PRODUCED are enforced. A run that touches one manifest is not answerable for a
    workflow it never wrote, and holding it answerable would make every partial run unappliable.

    Findings carry the `"{path}: "` prefix so D-106's per-file withholding applies unchanged: the model
    is told what is still wrong while an attempt remains, and only the offending artifact is withheld.
    """
    produced: dict[str, str] = dict(files) if isinstance(files, Mapping) else {p: b for p, b in files}
    if not produced:
        return ()

    merged: dict[str, str] = dict(existing or {})
    merged.update(produced)

    kinds_produced = {kind_for_path(path) for path in produced} - {""}
    if not kinds_produced:
        return ()

    findings: list[str] = []
    for kind in sorted(kinds_produced):
        # JUDGE THE ARTIFACT, NOT THE REPOSITORY.
        #
        # Scoring the plain union was the first attempt and a real run exposed it immediately. The
        # generated `.github/workflows/build.yml` pins its one action to a SHA and is correct. The
        # project also has a hand-written `.github/workflows/ci.yml` that does not, so
        # `pipeline_actions_pinned` failed over the union and the finding blamed `build.yml` while its
        # own evidence pointed at `ci.yml`. Generation would have been refused for a file it did not
        # write and was never asked to write, making every partial run on an imperfect repository
        # unappliable.
        #
        # So existing files OF THE KIND THIS RUN PRODUCED are set aside and the generated ones stand in
        # their place. Everything else is kept, because scoping needs it: a Kubernetes check must still
        # see that manifests exist, and a cross-artifact check must still see the Dockerfile it compares
        # against. What is left is the question worth asking - is the artifact this run produced good
        # enough to satisfy the check it was produced for.
        isolated = {path: body for path, body in merged.items() if kind_for_path(path) != kind or path in produced}
        result = score_files(isolated)
        by_id = {check.id: check for check in result.checks}  # type: ignore[attr-defined]

        for check_id, explanation in sorted(CHECK_EXPLANATIONS.items()):
            if not explanation.generatable_today or explanation.artifact != kind:
                continue
            check = by_id.get(check_id)
            if check is None or check.passed:
                continue
            # Name the artifact of that kind this run produced. When a run wrote several of one kind the
            # first by path is cited: the finding is about the kind's content, and every file of the
            # kind is part of the same answer.
            offenders = sorted(path for path in produced if kind_for_path(path) == kind)
            findings.append(
                f"{offenders[0]}: still fails {check_id} ({check.points}/{check.max_points}), "
                f"the check this artifact is generated to satisfy; {check.evidence or explanation.looked_for}"
            )
    return tuple(findings)
