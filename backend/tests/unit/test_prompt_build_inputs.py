# SPDX-License-Identifier: FSL-1.1-ALv2
"""The model is shown the CONTENTS of the files that decide how a project builds (2026-11-02).

It used to be told only that `package.json` and `vite.config.ts` existed, so the build script, the output
directory and the runtime version were guesses. These are generic: nothing here is about one project.
"""

from __future__ import annotations

from src.core.index_evidence import IndexEvidence
from src.core.readiness import ReadinessEngine
from src.generation.prompt_compiler import _build_inputs_section, compile_prompt

PACKAGE = '{"name": "site", "scripts": {"build": "tsc -b && vite build", "preview": "vite preview"}}'
PATHS = ("package.json", "package-lock.json", "vite.config.ts", "web/package.json", "src/main.tsx", ".nvmrc")
CONTENTS = {
    "package.json": PACKAGE,
    "package-lock.json": '{"lockfileVersion": 3}',
    "vite.config.ts": "export default { build: { outDir: 'build' } }",
    "web/package.json": '{"name": "web"}',
    "src/main.tsx": "console.log(1)",
    ".nvmrc": "22",
}


def test_build_files_are_quoted_and_lockfiles_are_not() -> None:
    text = "\n".join(_build_inputs_section(paths=PATHS, contents=CONTENTS, inventory={}, budget_chars=20_000))
    assert "vite build" in text, "the build script must reach the model"
    assert "outDir: 'build'" in text, "the build output directory must reach the model"
    assert "### .nvmrc" in text and "### web/package.json" in text
    assert "package-lock.json" not in text, "lockfiles are long and add nothing a Dockerfile needs"
    assert "src/main.tsx" not in text, "ordinary source is not a build input"


def test_root_manifest_comes_first_and_the_budget_is_respected() -> None:
    lines = _build_inputs_section(paths=PATHS, contents=CONTENTS, inventory={}, budget_chars=800)
    text = "\n".join(lines)
    assert "### package.json" in text, "the root manifest is the most important input"
    assert len(text) <= 800 + 200, "the section must stay within its budget (plus the omission note)"
    assert "Not quoted" in text


def test_entry_points_from_the_scan_are_included() -> None:
    contents = {**CONTENTS, "server/index.js": "app.listen(process.env.PORT || 5000)"}
    text = "\n".join(
        _build_inputs_section(
            paths=(*PATHS, "server/index.js"),
            contents=contents,
            inventory={"entry_points": ["server/index.js"]},
            budget_chars=20_000,
        )
    )
    assert "app.listen(process.env.PORT || 5000)" in text


def test_the_compiled_instruction_carries_the_build_inputs() -> None:
    report = ReadinessEngine().evaluate(IndexEvidence(paths=PATHS, contents=dict(CONTENTS)))
    compiled = compile_prompt(checks=report.checks, paths=PATHS, contents=dict(CONTENTS), inventory={})
    assert "BUILD INPUTS" in compiled.text and "vite build" in compiled.text
    assert "â€" not in compiled.text, "the instruction must not carry mis-decoded characters"


def test_script_facts_state_what_may_be_run_and_what_it_needs() -> None:
    text = "\n".join(_build_inputs_section(paths=PATHS, contents=CONTENTS, inventory={}, budget_chars=20_000))
    assert "SCRIPT FACTS for package.json: declares build, preview." in text
    assert "There is NO `start` script" in text
    assert "needs devDependencies" not in text, "the fixture declares no devDependencies"


def test_the_instruction_no_longer_suggests_npm_start_unconditionally() -> None:
    report = ReadinessEngine().evaluate(IndexEvidence(paths=PATHS, contents=dict(CONTENTS)))
    compiled = compile_prompt(checks=report.checks, paths=PATHS, contents=dict(CONTENTS), inventory={})
    assert "if you run `npm start` in the final image" not in compiled.text
