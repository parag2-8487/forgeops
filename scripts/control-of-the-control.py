#!/usr/bin/env python3
# SPDX-License-Identifier: FSL-1.1-ALv2
"""Control of the control: prove a `VACUOUS` verdict is reachable for each mutation row.

`scripts/mutation-harness.py` answers "does the property fail under its own mutation?". This answers
the question one level up: **would the harness notice if the mutation stopped mutating?** A row that
reports `OK` because its overlay silently became a byte-copy of the original, or because its patch
threw and the failure was swallowed, is a negative control that has stopped being negative — and the
harness cannot tell that from a healthy row.

The method is to neutralise each mutation and require the verdict to flip to `VACUOUS`:

* a **go** row's `overlay` is pointed at its own `original`, so `go build -overlay` substitutes a
  file for itself. That is the byte-copy control in its cheapest form: no temporary source file, and
  nothing in the working tree changes;
* a **python** row's `patch` becomes a comment, so the property runs against unmutated code.

The neutralised row is written to a **one-row manifest in a temporary directory**, rebuilt from the
parsed values rather than by editing the original text. Text surgery was the first attempt and it
produced invalid TOML: the patch bodies contain their own triple-quoted strings, so no regex for
"the end of this block" is safe. Rebuilding from `tomllib` cannot get that wrong, and it also means
this script reads the manifest exactly the way the harness does.

Because the manifest lives outside the repository and no source file is touched,
`git status --porcelain` stays empty — the same clause the harness asserts about itself.

Every leaf that lands a `mutations.toml` row runs this for that row, so "the OK verdict is
attributable to the mutation rather than to the mechanism" is a command someone can re-run rather
than a sentence in a commit message.

Usage, from the repository root, with `scripts/local-env.ps1` already dot-sourced:

    python scripts/control-of-the-control.py Q-14 Q-15 Q-16 Q-17 Q-31
    python scripts/control-of-the-control.py --all
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST = REPO_ROOT / "backend" / "tests" / "mutation" / "mutations.toml"
HARNESS = REPO_ROOT / "scripts" / "mutation-harness.py"

#: The keys the harness reads, per runtime, plus `description` — which it requires present even
#: though only a human reads it, so the neutralised row carries a one-line one.
_GO_KEYS = ("runtime", "module_dir", "package", "test_run", "original", "overlay", "property")
_PYTHON_KEYS = ("runtime", "property", "target", "patch")
_NEUTRALISED = {
    "mutation": "NEUTRALISED (control of the control)",
    "description": "The mutation is removed, so the property must PASS and the harness must report VACUOUS.",
}


def load_manifest() -> dict[str, Any]:
    return tomllib.loads(MANIFEST.read_text(encoding="utf-8"))


def neutralised_row(name: str, row: dict[str, Any]) -> dict[str, Any]:
    """Return the row with its mutation made a no-op."""
    out = dict(row)
    if row.get("runtime") == "go":
        original = row.get("original")
        if not original:
            raise SystemExit(f"{name} is a go row with no `original`; it cannot be neutralised")
        out["overlay"] = original
        return {key: out[key] for key in _GO_KEYS if key in out} | _NEUTRALISED
    out["patch"] = "# control of the control: this patch deliberately does nothing\n"
    return {key: out[key] for key in _PYTHON_KEYS if key in out} | _NEUTRALISED


def emit(name: str, row: dict[str, Any]) -> str:
    """Write one row as TOML.

    `json.dumps` produces a valid TOML basic string for every value here — they are ASCII paths,
    identifiers and one comment — and it escapes the backslashes in a Windows-shaped path, which a
    naive f-string would not.
    """
    lines = [f"[{name}]"]
    for key, value in row.items():
        lines.append(f"{key} = {json.dumps(value)}")
    return "\n".join(lines) + "\n"


def run(name: str, manifest: Path) -> tuple[str, str]:
    """Run the harness for one row against a manifest, and return (verdict, output)."""
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            sys.executable,
            str(HARNESS),
            "--manifest",
            str(manifest),
            "--only",
            name,
            "--allow-incomplete",
            "--skip-git-check",
        ],
        capture_output=True,
        cwd=str(REPO_ROOT),
        encoding="utf-8",
        errors="replace",
    )
    output = result.stdout + result.stderr
    for line in output.splitlines():
        if line.startswith(name):
            if "VACUOUS" in line:
                return "VACUOUS", output
            if line.rstrip().endswith("OK"):
                return "OK", output
            return line.strip(), output
    return "no verdict", output


#: Rows whose neutralised verdict cannot be established without some part of the environment, each
#: naming the variable that part is detected by, plus a reason.
#:
#: Format: `Q-NN: requires=ENV_VAR  reason...`
#:
#: The `requires=` clause is what keeps this from becoming the very trap this pass spent its time
#: removing. A flat exemption list would make the check pass where the environment is missing and
#: fail where it is present -- an assertion whose verdict depends on who runs it, which is how the
#: embedding-model constant survived for months as an "environment note". Instead the row is skipped
#: only when its named variable is ABSENT; wherever the variable is set, the row must flip to VACUOUS
#: like any other. So the exemption cannot hide a genuine regression, and it self-clears in exactly
#: the environment that could detect one.
BASELINE = Path(__file__).resolve().parent / "control-of-the-control-baseline.txt"


def load_baseline(path: Path) -> dict[str, tuple[str, str]]:
    """Return `{row: (required_env_var, reason)}`."""
    exempt: dict[str, tuple[str, str]] = {}
    if not path.exists():
        return exempt
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, _, rest = line.partition(":")
        name, rest = name.strip(), rest.strip()
        if not rest.startswith("requires="):
            raise SystemExit(
                f"{path.name}: '{name}' must start its reason with `requires=ENV_VAR`, naming the "
                "environment variable whose absence makes the row unjudgeable. Without it the "
                "exemption would apply everywhere, including where a regression could be seen."
            )
        clause, _, reason = rest.partition(" ")
        variable = clause.removeprefix("requires=").strip()
        reason = reason.strip()
        if not variable or not reason:
            raise SystemExit(
                f"{path.name}: '{name}' needs both a `requires=ENV_VAR` clause and a reason saying "
                "which part of the environment is missing and where the row is proven instead."
            )
        exempt[name] = (variable, reason)
    return exempt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("rows", nargs="*", metavar="Q-NN", help="rows to check")
    parser.add_argument("--all", action="store_true", help="check every row in the manifest")
    parser.add_argument(
        "--baseline",
        type=Path,
        default=BASELINE,
        help="rows exempted with a reason; an exempted row that passes is an error",
    )
    args = parser.parse_args(argv)

    manifest = load_manifest()
    names = [key for key in manifest if key.startswith("Q-")]
    targets = names if args.all else args.rows
    if not targets:
        parser.error("name at least one row, or pass --all")

    exempt = load_baseline(args.baseline)
    unknown = sorted(set(exempt) - set(names))
    if unknown:
        raise SystemExit(
            f"{args.baseline.name} names {', '.join(unknown)}, which is not a row in {MANIFEST.name}"
        )
    # An exemption applies only where its named variable is absent. Where it is set, the row is
    # judged like any other -- so the exemption cannot mask a regression in any environment capable
    # of detecting one.
    skipped: dict[str, str] = {}
    for name, (variable, reason) in exempt.items():
        if os.environ.get(variable, "").strip():
            continue
        skipped[name] = f"{variable} is unset -- {reason}"

    failures = 0
    for name in targets:
        if name not in manifest:
            raise SystemExit(f"{name} is not a row in {MANIFEST.name}; rows are {', '.join(names)}")
        if name in skipped:
            print(f"{name:6} not judged            EXEMPT: {skipped[name]}")
            continue
        row = neutralised_row(name, manifest[name])
        with tempfile.TemporaryDirectory(prefix="forgeops-cotc-") as directory:
            path = Path(directory) / "mutations.toml"
            path.write_text(emit(name, row), encoding="utf-8")
            verdict, output = run(name, path)

        if verdict == "VACUOUS":
            print(f"{name:6} neutralised -> VACUOUS   the OK verdict is attributable to the mutation")
            continue
        failures += 1
        print(f"{name:6} neutralised -> {verdict}")
        print("  the harness did NOT report VACUOUS with the mutation removed, so an OK verdict for")
        print("  this row does not prove the mutation is what the property objected to.")
        print("\n".join(f"  | {line}" for line in output.splitlines()[-12:]))

    if failures:
        print(f"\n{failures} row(s) could not be shown to depend on their mutation")
        return 1
    checked = len([n for n in targets if n not in skipped])
    print(
        f"\nall {checked} judged row(s) flip to VACUOUS when their mutation is removed"
        + (
            f"; {len(skipped)} not judged here for want of its environment ({', '.join(sorted(skipped))})"
            if skipped
            else ""
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
