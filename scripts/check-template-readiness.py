#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Invariant 1: an artifact must pass the readiness checks it is generated to fix.

WHY THIS EXISTS

A generation run whose instruction said "`kubernetes_probes_declared` failed" produced a Deployment
with no probes, and the score did not move. The gate that reviewed it asked "is this well formed and
the right shape", which the artifact was. Nothing asked the one question that mattered: does this
file satisfy the check it was written for.

The template library is where that hurts most, because it is the FLOOR. When the model under-produces
the cascade falls back to a template, and a template that fails the target check turns the floor into
a trapdoor: the run reports success, a change set applies, and the number the operator was trying to
raise stays where it was.

HOW IT DECIDES, WITHOUT A SECOND IMPLEMENTATION

Three things already in the product are composed here rather than re-expressed:

  * `GenerationService._render` - the REAL template path. Called unbound (it touches no `self`), so
    this gate exercises the same bytes a fallback run would write. A copy of the templates here
    could pass while production shipped something else.
  * `CHECK_EXPLANATIONS[...].artifact` - the product's own statement of which artifact kind fixes
    which check, and `generatable_today`, which is that AND-ed with what the generator actually emits.
  * `ReadinessEngine.evaluate` - the same scorer the readiness screen uses. Pure over `IndexEvidence`,
    so no database is needed and the gate cannot drift from the number the user sees.

So the rule is mechanical: for every check the product claims is generatable, if this run rendered
the artifact kind that check names, that check MUST pass. A template that cannot satisfy the check it
exists to satisfy fails the build here.

WHAT A FAILURE MEANS. Either the template is wrong, or the check is wrong, or the `artifact` mapping
claims a kind fixes a check it does not. All three are defects and all three should stop a release.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND = REPO_ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from src.core.index_evidence import IndexEvidence  # noqa: E402
from src.core.readiness import ReadinessEngine  # noqa: E402
from src.core.readiness_findings import (  # noqa: E402
    CHECK_EXPLANATIONS,
    GENERATED_ARTIFACT_KINDS,
)
from src.generation.service import GenerationService  # noqa: E402

#: Path shape to the artifact kind `CHECK_EXPLANATIONS` names. The first match wins, so the specific
#: `.github/workflows/` rule precedes anything that would also match a bare YAML file.
#:
#: `unmapped_kinds` asserts this covers every member of `GENERATED_ARTIFACT_KINDS`, so adding a kind
#: to the product without teaching this gate to recognise it is itself a failure - the defect class
#: this file guards against, applied to the guard.
_PATH_KINDS: tuple[tuple[str, str], ...] = (
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
    lowered = path.lower()
    for needle, kind in _PATH_KINDS:
        if needle in lowered:
            return kind
    return ""


def unmapped_kinds() -> tuple[str, ...]:
    """Kinds the product claims it generates that no rule above recognises."""
    mapped = {kind for _needle, kind in _PATH_KINDS}
    return tuple(sorted(set(GENERATED_ARTIFACT_KINDS) - mapped))


def render_template_artifacts(
    project_name: str = "auditapp", port: int = 8080
) -> dict[str, str]:
    """The real template-path artifacts, keyed by repository path.

    `_render` is called unbound because it reads no instance state. That is deliberate: the gate must
    see production's bytes, and an instance would drag a session, a router and a settings object into
    a check that needs none of them.
    """
    project = {"name": project_name, "settings": {"port": port}}
    rendered = GenerationService._render(
        None, "generate the deployment artifacts", project
    )  # type: ignore[arg-type]
    return {item.path: item.content for item in rendered}


def score(files: dict[str, str]) -> object:
    """Run the product's own scorer over a synthetic repository made only of these files."""
    evidence = IndexEvidence(
        paths=tuple(sorted(files)),
        contents={path.lower(): body for path, body in files.items()},
    )
    return ReadinessEngine().evaluate(evidence)


def audit(files: dict[str, str], *, verbose: bool) -> list[str]:
    """Every check that a rendered artifact kind targets and does not satisfy."""
    kinds_present = {kind_for_path(path) for path in files} - {""}
    result = score(files)
    by_id = {check.id: check for check in result.checks}

    failures: list[str] = []
    for check_id, explanation in sorted(CHECK_EXPLANATIONS.items()):
        if not explanation.generatable_today:
            continue
        if explanation.artifact not in kinds_present:
            continue
        check = by_id.get(check_id)
        if check is None:
            failures.append(
                f"{check_id}: the explanation table claims artifact '{explanation.artifact}' fixes this "
                f"check, but the scorer emitted no such check - the mapping names a check that does not exist"
            )
            continue
        if not check.passed:
            owners = sorted(
                p for p in files if kind_for_path(p) == explanation.artifact
            )
            failures.append(
                f"{check_id} ({check.points}/{check.max_points}) is targeted by artifact "
                f"'{explanation.artifact}' but the rendered template does not satisfy it\n"
                f"      rendered: {', '.join(owners)}\n"
                f"      evidence: {check.evidence}"
            )
        elif verbose:
            print(f"  ok   {check_id:38} {check.points}/{check.max_points}")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--verbose", action="store_true", help="list the checks that passed too"
    )
    args = parser.parse_args()

    print(
        "Invariant 1: every rendered template satisfies the checks its artifact kind targets"
    )

    stray = unmapped_kinds()
    if stray:
        print(
            f"\nFAIL: the product can generate {len(stray)} kind(s) this gate cannot recognise: {', '.join(stray)}"
        )
        print(
            "      add a rule to _PATH_KINDS, or the kind's checks are audited by nothing"
        )
        return 1

    files = render_template_artifacts()
    print(f"\nrendered {len(files)} artifact(s) from the real template path:")
    for path in sorted(files):
        print(
            f"  {kind_for_path(path) or '(unmapped)':20} {path:44} {len(files[path]):>6} bytes"
        )

    result = score(files)
    print(
        f"\nthe template library's own output scores {result.overall_score} ({result.level})"
    )

    failures = audit(files, verbose=args.verbose)

    kinds_present = {kind_for_path(p) for p in files} - {""}
    missing = sorted(set(GENERATED_ARTIFACT_KINDS) - kinds_present)
    if missing:
        print(
            f"\nnote: {len(missing)} generatable kind(s) the template path never renders: {', '.join(missing)}"
        )
        print(
            "      checks needing these cannot be fixed by a fallback run, so the reachable score excludes them"
        )

    if failures:
        print(
            f"\nFAIL: {len(failures)} check(s) are targeted by a template that does not satisfy them\n"
        )
        for line in failures:
            print(f"  - {line}")
        print(
            "\nA template that fails the check it exists to fix turns the fallback floor into a trapdoor."
        )
        return 1

    print(
        "\nPASS: every check a rendered artifact kind targets is satisfied by that artifact"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
