# SPDX-License-Identifier: FSL-1.1-ALv2
"""A run that produced most of what it asked for has not failed.

THE REGRESSION THIS PINS. Making the required artifact set derive from the compiled plan's write targets
was correct — the parser's hard-coded four paths were the reason the product could produce four kinds of
file and no others. But it was passed as `required`, which is all-or-nothing, and a compiled plan asks for
a dozen files. So one omission discarded eleven correct files and substituted canned template output, and
the thirteen-step end-to-end journey recorded a run that had produced most of what it asked for as
`template_fallback`.

Canned output in place of real partial work is strictly worse than the partial work. A shortfall is now a
shortfall: the files that came back are used, the checks whose files did not come back are still failing,
and the score says so. The user sees which recommendations were addressed and which were not, which is
true, rather than a set of generic templates that address none of them specifically.

What did NOT change: a file outside the requested set is still dropped here rather than at the governance
chokepoint, because an unrequested file in a change set is a write nobody asked for.
"""

from __future__ import annotations

import pytest
from src.generation.model_prompt import REQUIRED_ARTIFACTS, ArtifactParseError, parse_artifacts

pytestmark = [pytest.mark.mandatory]

FENCE = "`" * 3


def _block(path: str, body: str) -> str:
    return f"### FILE: {path}\n{FENCE}\n{body}\n{FENCE}\n"


TARGETS = (
    "Dockerfile",
    "k8s/deployment.yaml",
    "k8s/service.yaml",
    ".github/workflows/ci.yml",
)


class TestAShortfallIsNotAFailure:
    def test_the_files_that_came_back_are_used(self) -> None:
        raw = _block("Dockerfile", "FROM python:3.12") + _block("k8s/deployment.yaml", "apiVersion: apps/v1")
        parsed = parse_artifacts(raw, required=(), requested=TARGETS)
        assert sorted(parsed) == ["Dockerfile", "k8s/deployment.yaml"]

    def test_one_missing_file_does_not_discard_the_rest(self) -> None:
        """The precise shape of the journey failure: three of four is not zero of four."""
        raw = "".join(_block(path, "content") for path in TARGETS[:3])
        parsed = parse_artifacts(raw, required=(), requested=TARGETS)
        assert len(parsed) == 3

    def test_content_is_preserved_verbatim(self) -> None:
        """A manifest is whitespace-significant, so a partial accept must not reformat."""
        body = "FROM python:3.12\n    # indented comment\n\nUSER 10001"
        parsed = parse_artifacts(_block("Dockerfile", body), required=(), requested=("Dockerfile",))
        assert parsed["Dockerfile"] == body + "\n"


class TestWhatIsStillRefused:
    def test_nothing_usable_is_still_an_error(self) -> None:
        """A model that produced none of the requested files has not partially succeeded."""
        with pytest.raises(ArtifactParseError):
            parse_artifacts(_block("README.md", "hello"), required=(), requested=TARGETS)

    def test_the_error_names_every_requested_path(self) -> None:
        """The retry prompt is built from this list, so it has to be complete."""
        with pytest.raises(ArtifactParseError) as caught:
            parse_artifacts(_block("README.md", "hello"), required=(), requested=TARGETS)
        message = str(caught.value)
        for path in TARGETS:
            assert path in message

    def test_an_unrequested_file_is_dropped(self) -> None:
        """An unrequested file in a change set is a write nobody asked for."""
        raw = _block("Dockerfile", "FROM scratch") + _block("README.md", "unasked for")
        parsed = parse_artifacts(raw, required=(), requested=TARGETS)
        assert "README.md" not in parsed

    def test_an_empty_body_is_not_an_artifact(self) -> None:
        raw = _block("Dockerfile", "FROM scratch") + f"### FILE: k8s/service.yaml\n{FENCE}\n{FENCE}\n"
        parsed = parse_artifacts(raw, required=(), requested=TARGETS)
        assert "k8s/service.yaml" not in parsed


class TestTheFreeTextContractIsUnchanged:
    """An operator typing a free prompt compiles no plan, and must get the previous behaviour."""

    def test_a_required_set_is_still_all_or_nothing(self) -> None:
        raw = _block("Dockerfile", "FROM python:3.12")
        with pytest.raises(ArtifactParseError):
            parse_artifacts(raw, required=REQUIRED_ARTIFACTS)

    def test_the_required_set_still_returns_exactly_itself(self) -> None:
        raw = "".join(_block(path, "content") for path in REQUIRED_ARTIFACTS)
        raw += _block("README.md", "extra")
        parsed = parse_artifacts(raw, required=REQUIRED_ARTIFACTS)
        assert sorted(parsed) == sorted(REQUIRED_ARTIFACTS)
