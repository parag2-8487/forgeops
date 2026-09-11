#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Invariant 1, over the template library: an artifact must pass the checks it targets.

WHY THIS EXISTS

A generation run whose instruction said "`kubernetes_probes_declared` failed" produced a Deployment
with no probes, and the score did not move. The gate that reviewed it asked "is this well formed and
the right shape", which the artifact was. Nothing asked the one question that mattered: does this
file satisfy the check it was written for.

The template library is where that hurts most, because it is the FLOOR. When the model under-produces
the cascade falls back to a template, and a template that fails the target check turns the floor into
a trapdoor: the run reports success, a change set applies, and the number the operator was trying to
raise stays where it was.

THE RULE IS NOT IMPLEMENTED HERE

`src.core.target_checks.unsatisfied_targets` is the rule, and `GenerationService._validate` applies
the same function to model output at runtime. This script is the TEMPLATE call site: it renders the
real template path and asks the shared question about the result. Restating the rule here would let
CI and the runtime disagree about what satisfying a check means, and each would pass its own suite.

`GenerationService._render` is called unbound because it reads no instance state — so the gate sees
production's bytes rather than a copy that could pass while production shipped something else.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND = REPO_ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from src.core.readiness_findings import (  # noqa: E402
    CHECK_EXPLANATIONS,
    GENERATED_ARTIFACT_KINDS,
)
from src.core.target_checks import (  # noqa: E402
    kind_for_path,
    score_files,
    unmapped_kinds,
    unsatisfied_targets,
)
from src.generation.service import GenerationService  # noqa: E402


def render_template_artifacts(
    project_name: str = "auditapp", port: int = 8080
) -> dict[str, str]:
    """The real template-path artifacts, keyed by repository path."""
    project = {"name": project_name, "settings": {"port": port}}
    rendered = GenerationService._render(
        None, "generate the deployment artifacts", project
    )  # type: ignore[arg-type]
    return {item.path: item.content for item in rendered}


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
            f"\nFAIL: the product can generate {len(stray)} kind(s) the rule cannot recognise: {', '.join(stray)}"
        )
        print(
            "      add a rule to src/core/target_checks.py, or the kind's checks are audited by nothing"
        )
        return 1

    files = render_template_artifacts()
    print(f"\nrendered {len(files)} artifact(s) from the real template path:")
    for path in sorted(files):
        print(
            f"  {kind_for_path(path) or '(unmapped)':22} {path:44} {len(files[path]):>6} bytes"
        )

    result = score_files(files)
    print(
        f"\nthe template library's own output scores {result.overall_score} ({result.level})"
    )

    if args.verbose:
        kinds_present = {kind_for_path(p) for p in files} - {""}
        for check_id, explanation in sorted(CHECK_EXPLANATIONS.items()):
            if explanation.generatable_today and explanation.artifact in kinds_present:
                check = next((c for c in result.checks if c.id == check_id), None)
                if check is not None and check.passed:
                    print(f"  ok   {check_id:38} {check.points}/{check.max_points}")

    failures = unsatisfied_targets(files)

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
