# SPDX-License-Identifier: FSL-1.1-ALv2
"""What the real external tools said about the artifacts this repository already has.

Six genuine validators run on the agent — `docker compose config`, `kubeconform`, `helm lint`, `yamllint`
and two others — and every one of them only ever judged files this system GENERATED. The `validations`
table is keyed by `change_item_id`, so a file the user wrote themselves could never appear in it, and the
table held no rows at all.

The consequence was the sharpest form of the fault this whole area is about: a hand-written
`docker-compose.yml` was scored on whether its PATH existed, while the tool that could have said "this
does not parse" was installed on the same machine and ran during the same scan without being asked. The
score said one thing and `docker compose config` said another, and only one of them was right.

THE RULE THAT GOVERNS EVERYTHING HERE: NOT CHECKED IS NOT PASSED.

Four statuses arrive, and they mean four different things:

  * `passed` — the tool ran and was satisfied. This is the only one that earns points.
  * `failed` — the tool ran and objected. The artifact is wrong, and the tool's own words say how.
  * `tool_missing` — the binary is not installed on the developer's machine. NOTHING is known about the
    artifact.
  * `errored` — the tool could not run to completion. Also nothing known.

A project with no verdicts at all — an older agent, or one whose tools are all absent — emits NO check
rather than a passing one. The agent's validator package exists because the implementations it replaced
fabricated passes for anything they did not understand; reproducing that here would move the identical
defect one layer up, where it would be harder to see and easier to trust.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Final

from pydantic import BaseModel

#: Statuses that mean the artifact was actually judged.
CONCLUSIVE: Final[frozenset[str]] = frozenset({"passed", "failed"})

#: Statuses that mean nothing is known, and must never be scored as a pass.
INCONCLUSIVE: Final[frozenset[str]] = frozenset({"tool_missing", "errored"})

#: Human names for the artifact kinds, for a finding a reader can act on.
KIND_NAMES: Final[Mapping[str, str]] = {
    "compose": "Compose file",
    "k8s": "Kubernetes manifest",
    "helm": "Helm chart",
    "yaml_schema": "YAML document",
}


class ToolVerdicts(BaseModel):
    """The scan's external-tool results, summarised for the score.

    `judged` counts only the artifacts a tool actually reached a conclusion about. `unchecked` counts the
    ones where a tool was missing or failed to run, and it is reported SEPARATELY rather than folded into
    either outcome — a user whose `kubeconform` is not installed needs to be told to install it, not told
    their manifests are fine and not told they are broken.
    """

    judged: int = 0
    satisfied: int = 0
    #: Artifacts a tool objected to, as `(path, kind, tool, detail, line)`.
    failures: tuple[tuple[str, str, str, str, int | None], ...] = ()
    #: Artifacts nothing could judge, as `(path, tool_or_kind, status)`.
    unchecked: tuple[tuple[str, str, str], ...] = ()
    #: Tool names that were absent, deduplicated, for a single actionable sentence.
    missing_tools: tuple[str, ...] = ()

    model_config = {"frozen": True}

    @property
    def conclusive(self) -> bool:
        """Whether any artifact was actually judged.

        False means the check must not be emitted. There is a real difference between "your artifacts are
        valid" and "nothing here could tell me", and only the first is a readiness claim.
        """
        return self.judged > 0

    @property
    def passed(self) -> bool:
        """True only when something was judged and every judged artifact satisfied its tool."""
        return self.judged > 0 and self.satisfied == self.judged

    @property
    def first_failure_detail(self) -> str:
        """A sentence naming the artifact, the tool, and what the tool said.

        The TOOL is named because "it failed" is not actionable and "kubeconform rejected this" is: the
        reader can run the same command and see the same output. The tool's own words are quoted rather
        than paraphrased for the same reason.
        """
        if not self.failures:
            return ""
        path, kind, tool, detail, line = self.failures[0]
        where = f"{path}:{line}" if line else path
        label = KIND_NAMES.get(kind, kind)
        remainder = ""
        if len(self.failures) > 1:
            remainder = f" ({len(self.failures) - 1} other artifact(s) also failed)"
        return f"{tool} rejected the {label} {where}: {detail}{remainder}"

    @property
    def unchecked_detail(self) -> str:
        """A sentence naming what could not be judged and which tool was absent."""
        if not self.unchecked:
            return ""
        tools = ", ".join(self.missing_tools) if self.missing_tools else "the required tool"
        return (
            f"{len(self.unchecked)} artifact(s) could not be validated because {tools} "
            "is not installed on the machine running the agent"
        )


def summarise(
    validations: Sequence[tuple[str, str, str, str, int, str, int | None]],
) -> ToolVerdicts:
    """Fold the stored verdicts into what the score needs.

    Rows arrive as `(path, kind, tool, status, error_count, detail, line)`.
    """
    judged = 0
    satisfied = 0
    failures: list[tuple[str, str, str, str, int | None]] = []
    unchecked: list[tuple[str, str, str]] = []
    missing: list[str] = []

    for path, kind, tool, status, _error_count, detail, line in validations:
        if status in INCONCLUSIVE:
            unchecked.append((path, tool or kind, status))
            if status == "tool_missing" and tool and tool not in missing:
                missing.append(tool)
            continue
        if status not in CONCLUSIVE:
            # An unrecognised status is treated as inconclusive rather than guessed at. A future writer
            # adding a fifth state must not have it silently counted as a pass.
            unchecked.append((path, tool or kind, status))
            continue

        judged += 1
        if status == "passed":
            satisfied += 1
        else:
            failures.append(
                (
                    path,
                    kind,
                    tool or "the validator",
                    detail or "the tool reported no detail",
                    line,
                )
            )

    return ToolVerdicts(
        judged=judged,
        satisfied=satisfied,
        failures=tuple(failures),
        unchecked=tuple(unchecked),
        missing_tools=tuple(sorted(missing)),
    )
