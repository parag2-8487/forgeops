# SPDX-License-Identifier: FSL-1.1-ALv2
"""The score must report what the external tools said, and must never guess when they said nothing.

Six real validators run on the agent and every one of them only ever judged GENERATED files: the
`validations` table is keyed by `change_item_id` and held no rows. So a hand-written `docker-compose.yml`
was scored on whether its PATH existed, while `docker compose config` — installed, on the same machine,
during the same scan — was never asked.

THE INVARIANT THESE TESTS DEFEND: NOT CHECKED IS NOT PASSED.

The agent's `internal/validator` package exists because the implementations it replaced were substring
matching wearing a validator's name: the Kubernetes one checked for the text `apiVersion:` and returned
success under a comment claiming a server-side dry run; the OpenTofu one never executed its binary. That
is a security control fabricating a pass. Scoring `tool_missing` as a pass here would move the identical
defect one layer up, where it is harder to see and easier to trust.
"""

from __future__ import annotations

import pytest
from src.core.index_evidence import IndexEvidence
from src.core.readiness import ReadinessEngine
from src.core.tool_verdicts import summarise

pytestmark = [pytest.mark.mandatory]

CHECK_ID = "artifacts_pass_their_validators"


def _row(path: str, status: str, *, kind="k8s", tool="kubeconform", detail="", line=None, errors=0):
    return (path, kind, tool, status, errors, detail, line)


def _check(*rows):
    result = ReadinessEngine().evaluate(
        IndexEvidence(paths=("k8s/deployment.yaml",), contents={}, artifact_validations=rows)
    )
    return next((c for c in result.checks if c.id == CHECK_ID), None)


class TestNothingKnownIsNotAPass:
    def test_a_missing_tool_emits_no_check_at_all(self) -> None:
        """A developer without kubeconform installed has learnt nothing about their manifests."""
        assert _check(_row("k8s/a.yaml", "tool_missing", detail="tool not found on PATH")) is None

    def test_a_tool_that_could_not_run_emits_no_check(self) -> None:
        assert _check(_row("k8s/a.yaml", "errored", detail="context deadline exceeded")) is None

    def test_a_report_with_no_verdicts_emits_no_check(self) -> None:
        """An older agent reports no validations. That is unknown, not perfect."""
        assert _check() is None

    def test_an_unrecognised_status_is_not_counted_as_a_pass(self) -> None:
        """A future writer adding a fifth state must not have it silently scored as success."""
        verdicts = summarise((_row("k8s/a.yaml", "skipped_for_some_new_reason"),))
        assert verdicts.judged == 0
        assert not verdicts.passed
        assert len(verdicts.unchecked) == 1

    def test_an_unchecked_artifact_beside_a_passing_one_does_not_inflate_the_score(self) -> None:
        """The unchecked one is excluded from the denominator, not counted as satisfied."""
        verdicts = summarise(
            (
                _row("k8s/a.yaml", "passed"),
                _row("k8s/b.yaml", "tool_missing", tool="kubeconform"),
            )
        )
        assert verdicts.judged == 1
        assert verdicts.satisfied == 1
        assert verdicts.passed
        # And the fact that something was not checked survives, so it can be said out loud.
        assert len(verdicts.unchecked) == 1
        assert "kubeconform" in verdicts.unchecked_detail
        assert "not installed" in verdicts.unchecked_detail


class TestWhatAFailureTells:
    def test_the_tool_and_the_path_and_the_line_are_named(self) -> None:
        """ "It failed" is not actionable. "kubeconform rejected this, at this line" is."""
        check = _check(
            _row(
                "k8s/deployment.yaml",
                "failed",
                detail="missing required field metadata.name",
                line=3,
            )
        )
        assert check is not None
        assert not check.passed
        assert "kubeconform" in check.found
        assert "k8s/deployment.yaml:3" in check.found
        assert check.line == 3

    def test_the_tools_own_words_are_quoted_rather_than_paraphrased(self) -> None:
        """So the reader can run the same command and see the same output."""
        message = "for field spec.replicas: expected integer, got string"
        check = _check(_row("k8s/a.yaml", "failed", detail=message))
        assert check is not None
        assert message in check.found

    def test_a_failure_with_no_detail_says_so_rather_than_inventing_one(self) -> None:
        check = _check(_row("k8s/a.yaml", "failed", detail=""))
        assert check is not None
        assert "no detail" in check.found

    def test_the_other_failures_are_counted(self) -> None:
        """A reader fixing one artifact needs to know how many remain."""
        check = _check(
            _row("k8s/a.yaml", "failed", detail="bad a"),
            _row("k8s/b.yaml", "failed", detail="bad b"),
            _row("k8s/c.yaml", "failed", detail="bad c"),
        )
        assert check is not None
        assert "2 other artifact(s) also failed" in check.found

    def test_the_artifact_kind_is_named_in_words(self) -> None:
        check = _check(_row("docker-compose.yml", "failed", kind="compose", tool="docker", detail="x"))
        assert check is not None
        assert "Compose file" in check.found


class TestScoring:
    def test_every_judged_artifact_passing_earns_full_marks(self) -> None:
        check = _check(_row("k8s/a.yaml", "passed"), _row("k8s/b.yaml", "passed"))
        assert check is not None
        assert check.passed
        assert check.points == check.max_points

    def test_a_partial_pass_earns_a_partial_score(self) -> None:
        check = _check(
            _row("k8s/a.yaml", "passed"),
            _row("k8s/b.yaml", "passed"),
            _row("k8s/c.yaml", "failed", detail="bad"),
        )
        assert check is not None
        assert not check.passed
        assert 0 < check.points < check.max_points

    def test_one_artifact_short_of_correct_cannot_round_up_to_full_marks(self) -> None:
        """The last point is earned by finishing, not by rounding."""
        rows = [_row(f"k8s/{i}.yaml", "passed") for i in range(99)]
        rows.append(_row("k8s/bad.yaml", "failed", detail="bad"))
        check = _check(*rows)
        assert check is not None
        assert check.points < check.max_points

    def test_the_check_is_not_offered_as_generatable(self) -> None:
        """There is no file to write that satisfies an external tool's verdict directly.

        Offering to generate a passing verdict is the exact fabrication the validator package was
        rewritten to remove.
        """
        check = _check(_row("k8s/a.yaml", "failed", detail="bad"))
        assert check is not None
        assert not check.generatable
        assert "external tool's verdict" in check.blocked_because

    def test_the_remedy_names_the_command_that_reproduces_the_finding(self) -> None:
        check = _check(_row("k8s/a.yaml", "failed", detail="bad"))
        assert check is not None
        assert "kubeconform" in check.remedy
