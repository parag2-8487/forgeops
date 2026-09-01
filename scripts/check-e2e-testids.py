#!/usr/bin/env python3
# SPDX-License-Identifier: FSL-1.1-ALv2
"""Every `data-testid` an end-to-end spec selects must exist in the application.

WHY THIS GATE EXISTS

`printed-instructions.spec.ts` selected `getByTestId("create-project")`. No such `data-testid` exists
anywhere in the app -- it was invented while writing the spec. Playwright therefore waited the full
per-test timeout for an element that could never appear, and the failure surfaced as
`locator.click: Test timeout of 900000ms exceeded` after **twenty-nine minutes** of CI, on a job that
first builds four images, pulls two models and provisions an identity provider.

The cost is the point. A wrong selector is a spelling mistake with a half-hour feedback loop, and it
is statically checkable: the spec names a string, the app either contains that string or it does not.

WHAT IT CHECKS

For every `getByTestId("x")` in `frontend/e2e/**`, `x` must appear in `frontend/app`,
`frontend/features` or `frontend/components` as `data-testid="x"`, as a `testId="x"` prop, or built by
a template literal whose static prefix matches. The template case is why the match is on the prefix
rather than the whole string: `data-testid={`project-${id}`}` is a real, correct pattern and a spec
that selects `project-<uuid>` must not be reported.

WHAT IT DOES NOT CHECK. Roles, labels and text. Those are the accessible names, they are asserted by
the component tests, and matching them statically would mean parsing JSX well enough to know what a
label resolves to -- a much weaker check for much more machinery. This gate covers the one class that
is a pure string match, which is the class that bit.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
E2E = ROOT / "frontend" / "e2e"
APP_DIRS = (
    ROOT / "frontend" / "app",
    ROOT / "frontend" / "features",
    ROOT / "frontend" / "components",
)

#: Test ids a spec may select that the app does not declare, each with a reason.
#:
#: Empty, deliberately. An entry here means "this spec waits for something the app never renders",
#: which is only ever correct for an assertion that something is ABSENT — and `expect(...).toBeNull()`
#: needs no timeout and no exemption. If this list ever grows, the reason has to say why the spec is
#: not simply wrong.
PERMITTED_UNDECLARED: dict[str, str] = {}


def declared_ids() -> set[str]:
    """Every test id the application can render, including template prefixes."""
    ids: set[str] = set()
    for directory in APP_DIRS:
        if not directory.is_dir():
            continue
        for path in directory.rglob("*.tsx"):
            text = path.read_text(encoding="utf-8")
            # Literal: data-testid="thing"
            ids.update(re.findall(r'data-testid="([^"{}]+)"', text))
            # Prop passed to a wrapper component: testId="thing"
            ids.update(re.findall(r'testId="([^"{}]+)"', text))
            # Template literal: data-testid={`thing-${x}`} — the static prefix is what a spec can
            # match against, so that is what is recorded.
            for template in re.findall(r"data-testid=\{`([^`]*)`\}", text):
                prefix = template.split("${")[0]
                if prefix:
                    ids.add(prefix)
            # Same shape, but passed as a prop.
            for template in re.findall(r"testId=\{`([^`]*)`\}", text):
                prefix = template.split("${")[0]
                if prefix:
                    ids.add(prefix)
    return ids


def selected_ids() -> dict[str, list[str]]:
    """Every test id the specs select, mapped to the files that select it."""
    out: dict[str, list[str]] = {}
    if not E2E.is_dir():
        return out
    for path in sorted(E2E.rglob("*.ts")):
        text = path.read_text(encoding="utf-8")
        for found in re.findall(r'getByTestId\(\s*"([^"]+)"\s*\)', text):
            out.setdefault(found, []).append(str(path.relative_to(ROOT)))
    return out


def main() -> int:
    declared = declared_ids()
    if not declared:
        print("check-e2e-testids: FAIL no data-testid attributes found; the app scan is broken")
        return 1

    selected = selected_ids()
    if not selected:
        print("check-e2e-testids: FAIL no getByTestId calls found; the spec scan is broken")
        return 1

    problems: list[str] = []
    for test_id, files in sorted(selected.items()):
        if test_id in PERMITTED_UNDECLARED:
            continue
        # Exact, or covered by a template prefix the app renders.
        if test_id in declared:
            continue
        if any(test_id.startswith(prefix) for prefix in declared if prefix.endswith("-")):
            continue
        problems.append(
            f"{', '.join(sorted(set(files)))} selects getByTestId({test_id!r}), which the "
            f"application never renders. Playwright will wait the full test timeout for it. Either "
            f"add the attribute to the component, or fix the selector"
        )

    if problems:
        for problem in problems:
            print(f"  - {problem}")
        print(f"check-e2e-testids: FAIL {len(problems)} selector(s) name nothing")
        return 1

    print(
        f"check-e2e-testids: ok, {len(selected)} selected test id(s) all exist among "
        f"{len(declared)} declared by the application"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
