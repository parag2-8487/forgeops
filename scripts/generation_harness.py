#!/usr/bin/env python
"""The generation isolation harness. Phase 2 tooling.

WHY THIS EXISTS. Diagnosing why a generated artifact fails the validation gate used to mean running
`test_generation_run_rows.py`, which with `qwen2.5-coder:7b` takes 19m30s: it drives three attempts through
the router, the cache, the chokepoint and the database. Almost none of that is relevant to the question
"which readiness check does the artifact fail". One iteration of the actual question is one model call plus
two pure functions -- around 90 seconds against a warm model.

An unaffordable debug loop is one nobody runs, so the bug survives. That is the whole argument for this file.

WHAT IT DRIVES, and it is the REAL chain rather than a reimplementation of it:

    IndexEvidence  ->  ReadinessEngine.evaluate  ->  compile_prompt  ->  the model  ->  parse_artifacts
                                                                                    ->  GenerationService._validate

Every one of those is the production object. `_validate` is deliberately reached through the service rather
than copied, because a harness with its own idea of the gate would agree with itself and disagree with
production -- which is worse than no harness, since it produces confident wrong answers.

WHAT IT DOES NOT DO: no database, no chokepoint, no cache, no HTTP. Those are what make the full test slow
and none of them can change which check an artifact fails.

USAGE
    python scripts/generation_harness.py                      # one attempt, default fixture
    python scripts/generation_harness.py --attempts 3         # the retry loop, findings fed forward
    python scripts/generation_harness.py --model qwen2.5-coder:1.5b
    python scripts/generation_harness.py --print-prompt       # the exact text the model is sent
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from src.generation.model_prompt import ArtifactParseError, parse_artifacts  # noqa: E402
from src.generation.prompt_compiler import compile_prompt  # noqa: E402
from src.generation.service import GeneratedFile, GenerationService  # noqa: E402
from src.core.index_evidence import IndexEvidence  # noqa: E402
from src.core.readiness import ReadinessEngine  # noqa: E402

# A MINIMAL BUT REAL PROJECT. Small enough that the prompt is readable, complete enough that the readiness
# engine has something to score: a Python service with no Dockerfile, no compose file and no CI, which is the
# shape that makes the interesting checks fail. Contents are keyed lowercased, as `IndexEvidence` documents.
FIXTURE_PATHS: tuple[str, ...] = (
    "app/main.py",
    "app/__init__.py",
    "requirements.txt",
    "README.md",
    "tests/test_main.py",
)

FIXTURE_CONTENTS: dict[str, str] = {
    "app/main.py": (
        "from fastapi import FastAPI\n\n"
        "app = FastAPI()\n\n\n"
        '@app.get("/health")\n'
        "def health() -> dict[str, str]:\n"
        '    return {"status": "ok"}\n'
    ),
    "requirements.txt": "fastapi==0.139.2\nuvicorn==0.34.0\n",
    "readme.md": "# checkout\n\nA checkout service.\n",
    "tests/test_main.py": "def test_health() -> None:\n    assert True\n",
}


def _evidence() -> IndexEvidence:
    return IndexEvidence(paths=FIXTURE_PATHS, contents=FIXTURE_CONTENTS)


def _ask(model: str, prompt: str, *, base_url: str, timeout: float) -> tuple[str, float]:
    body = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            # Zero, matching `RoutedArtifactModel`'s own temperature. A harness that sampled differently
            # from production would produce a different answer to the question being asked.
            "temperature": 0.0,
            "max_tokens": 2048,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url}/chat/completions", data=body, headers={"Content-Type": "application/json"}
    )
    started = time.time()
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.load(response)
    return payload["choices"][0]["message"]["content"], time.time() - started


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", default="qwen2.5-coder:7b")
    parser.add_argument("--base-url", default="http://localhost:11434/v1")
    parser.add_argument("--attempts", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=900.0)
    parser.add_argument("--print-prompt", action="store_true")
    parser.add_argument("--print-artifacts", action="store_true")
    parser.add_argument(
        "--check",
        action="append",
        default=None,
        help="narrow to specific check ids, as the route's selected_check_ids does",
    )
    args = parser.parse_args()

    evidence = _evidence()
    report = ReadinessEngine().evaluate(evidence)
    failing = [check for check in report.checks if not check.passed]

    print(f"readiness score: {report.overall_score} ({report.level}), from {report.evaluated_paths} path(s)")
    print(f"failing checks ({len(failing)}):")
    for check in failing:
        print(f"  - {check.id} [{check.category}] {check.points}/{check.max_points}")
    print()

    compiled = compile_prompt(
        checks=report.checks,
        paths=evidence.paths,
        contents=evidence.contents,
        inventory={},
        selected_check_ids=args.check,
        project_name="checkout",
    )
    targets = getattr(compiled, "write_targets", None) or getattr(compiled, "paths", ())
    print(f"prompt: {len(compiled.text)} chars, targets: {list(targets)}")
    print(f"checks the prompt is FOR: {list(getattr(compiled, 'check_ids', []) or [])}")
    print()

    if args.print_prompt:
        print("-" * 70)
        print(compiled.text)
        print("-" * 70)
        print()

    service = GenerationService(model=None, max_attempts=args.attempts)
    findings: tuple[str, ...] = ()
    prompt_text = compiled.text

    for attempt in range(1, args.attempts + 1):
        if findings:
            # THE RETRY SHAPE THE SERVICE USES: the previous findings are appended rather than merged, so
            # the model sees the original instruction and what went wrong with its last answer.
            prompt_text = (
                compiled.text
                + "\n\nYour previous answer did not pass these checks. Fix exactly these:\n"
                + "\n".join(f"  - {finding}" for finding in findings)
            )

        try:
            content, elapsed = _ask(
                args.model, prompt_text, base_url=args.base_url, timeout=args.timeout
            )
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            print(f"attempt {attempt}: MODEL UNREACHABLE ({type(exc).__name__}: {exc})")
            return 2

        try:
            # EXACTLY the production call: `required=()` with `requested=write_targets`, which is what
            # the compiled-prompt branch of the service uses. A harness parsing differently would
            # disagree with production about what the model produced.
            parsed = parse_artifacts(content, required=(), requested=tuple(targets))
        except ArtifactParseError as exc:
            print(f"attempt {attempt}: {elapsed:.0f}s PARSE FAILED: {exc}")
            print(f"  first 300 chars of the reply: {content[:300]!r}")
            findings = (str(exc),)
            continue

        artifacts = tuple(
            GeneratedFile(path=path, content=body) for path, body in parsed.items()
        )
        passed, gate_findings = service._validate(artifacts, evidence.contents)  # noqa: SLF001
        print(
            f"attempt {attempt}: {elapsed:.0f}s "
            f"parsed {len(artifacts)} artifact(s) {[a.path for a in artifacts]} "
            f"gate={'PASS' if passed else 'FAIL'}"
        )
        for finding in gate_findings:
            print(f"    - {finding}")

        if args.print_artifacts:
            for artifact in artifacts:
                print(f"  === {artifact.path} ===")
                print("  " + artifact.content.replace("\n", "\n  "))

        if passed:
            print(f"\nPASSED on attempt {attempt}.")
            return 0
        findings = gate_findings

    print(f"\nFAILED after {args.attempts} attempt(s). The findings above are what the gate refused.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
