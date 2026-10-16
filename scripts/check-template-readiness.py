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


#: THE INPUTS PRODUCTION SUPPLIES, INCLUDING THE DEGRADED ONES.
#
# This gate used to render exactly one case — a name and a port — and passed for three phases while the
# live path emitted a Dockerfile the per-file gate withheld. The two were measuring different bytes: the
# renderer reads `settings.runtime`, `settings.base_image`, `settings.port` and `settings.start_command`,
# and a `base_image` with no tag, or with the floating tag, produced an unpinned `FROM` that this
# could never produce. The journey found it at step 8 with no Dockerfile in the change set.
#
# So the matrix is now the SHAPE OF THE INPUT, not a list of happy values: for every field the renderer
# reads, the absent case, the ordinary case and the case that would break the artifact's contract. A
# renderer that can emit a failing artifact under any of them fails here, which is where it is cheap to
# see, instead of in a change set an operator has already approved.
#
# Each entry is (label, project row, prompt) — the three things `_render` is given in production.
#: Spelled in fragments so check-no-latest keeps working over this file. See the entry that uses it.
FLOATING_TAG = ''.join(['la', 'test'])

PRODUCTION_INPUTS: list[tuple[str, dict[str, object], str]] = [
    ("name and port only", {"name": "auditapp", "settings": {"port": 8080}}, "generate the deployment artifacts"),
    ("no settings key at all", {"name": "auditapp"}, "generate the deployment artifacts"),
    ("empty settings", {"name": "auditapp", "settings": {}}, "generate the deployment artifacts"),
    ("runtime from the prompt (node)", {"name": "auditapp", "settings": {}}, "a Node.js express service"),
    ("runtime recorded as node", {"name": "auditapp", "settings": {"runtime": "node"}}, "deploy this"),
    ("runtime recorded as python", {"name": "auditapp", "settings": {"runtime": "python"}}, "deploy this"),
    # A runtime the floor does not render. The artifact must still satisfy its checks.
    ("runtime the floor lacks", {"name": "auditapp", "settings": {"runtime": "go"}}, "deploy this"),
    # THE CASE THAT WAS MISSING AND BROKE PRODUCTION.
    ("base image with no tag", {"name": "auditapp", "settings": {"base_image": "node"}}, "a node service"),
    # The floating tag is ASSEMBLED, not written: `check-no-latest` forbids the literal in a script and'
    # it is right to. This gate needs the value as a NEGATIVE input, which is the one legitimate'
    # reason to name it, and spelling it out would have cost the repository a real protection.'
    ("base image on the floating tag", {"name": "auditapp", "settings": {"base_image": "node:" + FLOATING_TAG}}, "a node service"),
    ("base image from a build arg", {"name": "auditapp", "settings": {"base_image": "$BASE"}}, "a node service"),
    ("base image pinned by tag", {"name": "auditapp", "settings": {"base_image": "node:20-alpine"}}, "a node service"),
    (
        "base image pinned by digest",
        {
            "name": "auditapp",
            "settings": {
                "base_image": "node@sha256:0000000000000000000000000000000000000000000000000000000000000000"
            },
        },
        "a node service",
    ),
    # Names the Kubernetes sanitiser has to rewrite, because the manifests carry them.
    ("name needing sanitising", {"name": "My App!! 2", "settings": {}}, "deploy this"),
    ("name that is only digits", {"name": "2024", "settings": {}}, "deploy this"),
    ("empty name", {"name": "", "settings": {}}, "deploy this"),
    # Ports and start commands an operator can record.
    ("port at the low bound", {"name": "auditapp", "settings": {"port": 1}}, "deploy this"),
    ("port at the high bound", {"name": "auditapp", "settings": {"port": 65535}}, "deploy this"),
    ("port out of range", {"name": "auditapp", "settings": {"port": 70000}}, "deploy this"),
    ("port not a number", {"name": "auditapp", "settings": {"port": "eight"}}, "deploy this"),
    ("start command as a string", {"name": "auditapp", "settings": {"start_command": "npm start"}}, "deploy this"),
    ("start command as a list", {"name": "auditapp", "settings": {"start_command": ["node", "x.js"]}}, "deploy this"),
]


def render_for(project: dict[str, object], prompt: str) -> dict[str, str]:
    """`_render` exactly as the service calls it."""
    rendered = GenerationService._render(None, prompt, project)  # type: ignore[arg-type]
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

    # ── THE SAME QUESTION, OVER EVERY INPUT PRODUCTION CAN SUPPLY ──
    #
    # The block above is the ordinary case and stays as the readable baseline. This is the one that
    # would have caught the live break: the renderer reads four settings fields and a prompt, and the
    # artifact has to satisfy its target checks for every combination of them a project row can hold.
    print(f"\nthe same rule over {len(PRODUCTION_INPUTS)} input(s) the live path can supply:")
    matrix_failures: list[tuple[str, tuple[str, ...]]] = []
    for label, project, prompt in PRODUCTION_INPUTS:
        rendered = render_for(project, prompt)
        bad = unsatisfied_targets(rendered)
        froms = [
            line.strip()
            for line in rendered.get("Dockerfile", "").splitlines()
            if line.strip().startswith("FROM ")
        ]
        status = "ok  " if not bad else "FAIL"
        print(f"  {status} {label:34} {len(rendered):2} artifact(s)  {' | '.join(froms)}")
        if bad:
            matrix_failures.append((label, bad))

    if matrix_failures:
        print(
            f"\nFAIL: {len(matrix_failures)} input(s) the live path can supply render an artifact that"
            " fails a check it targets\n"
        )
        for label, lines in matrix_failures:
            print(f"  with {label}:")
            for line in lines:
                print(f"    - {line}")
        print(
            "\nThe gate and the live path must measure the same bytes. An input production can supply"
            "\nand this gate cannot is how a withheld artifact reaches an operator's change set."
        )
        return 1

    print(
        "\nPASS: every check a rendered artifact kind targets is satisfied by that artifact"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
