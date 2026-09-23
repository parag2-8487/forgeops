"""The compiled prompt's Dockerfile requirements are achievable. Phase 2.

WHY THIS TEST EXISTS, and it is a different question from "does the model comply".

`dockerfile_base_pinned` failed every generation attempt. The harness showed why -- the model was answering
`FROM alpine:latest` -- and the remedy followed the documented `USER 10001` precedent: give the model a shape
to copy rather than prose to synthesise from. But after adding the multi-stage and HEALTHCHECK shapes, base
pinning began failing again, and at that point there were two candidate explanations:

  (a) the instructions now contradict each other or prescribe something the gate refuses, or
  (b) the model is not following them.

Those need opposite responses, and each round of asking the model costs seven minutes. So this test answers
(a) directly and permanently: it constructs the Dockerfile that `GATE_REQUIREMENTS["dockerfile"]` literally
describes and asserts the REAL gate accepts it. If this passes, the instructions are satisfiable and any
remaining failure is the model, which is an honest thing to report rather than a bug to chase.

It is also a regression guard with a sharper edge than the harness: an instruction edited into something
unachievable fails here in milliseconds, at the moment it is written, instead of surfacing as a mysterious
generation failure weeks later.
"""

from __future__ import annotations

from src.core.index_evidence import IndexEvidence
from src.core.manifest_facts import dockerfile_base_pinned
from src.core.readiness import ReadinessEngine
from src.generation.prompt_compiler import GATE_REQUIREMENTS


def _checks(dockerfile: str) -> dict[str, bool]:
    """Run the REAL readiness engine over a project whose only artifact is this Dockerfile.

    Through the engine rather than against individual predicates, because multi-stage and non-root are
    computed inline there -- so a test calling predicates directly would be testing a path production
    does not use, which is the mistake this file exists to avoid making about the gate.
    """
    evidence = IndexEvidence(
        paths=("Dockerfile", "app/main.py"),
        contents={"dockerfile": dockerfile, "app/main.py": "app = 1\n"},
    )
    report = ReadinessEngine().evaluate(evidence)
    return {check.id: check.passed for check in report.checks}


#: The Dockerfile the requirements describe, written by following them literally and nothing more.
#:
#: Every line here traces to a requirement: two FROMs with the first `AS builder`, a pinned exact tag on
#: BOTH stages, `COPY --from=builder`, the verbatim `USER 10001` after the last RUN, and a HEALTHCHECK with
#: all three options. If a requirement is added that this cannot satisfy, this file is the place that says so.
INSTRUCTED_DOCKERFILE = """\
FROM python:3.13-slim AS builder

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir --target /install -r requirements.txt

FROM python:3.13-slim

WORKDIR /app
COPY --from=builder /install /usr/local/lib/python3.13/site-packages
COPY app ./app

RUN mkdir -p /app/data

USER 10001

HEALTHCHECK --interval=30s --timeout=3s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
"""


class TestTheInstructedDockerfileSatisfiesTheGate:
    """Each check the compiled prompt claims to target, against the shape it prescribes."""

    def test_base_images_are_pinned(self) -> None:
        """Both stages, not only the final one: a floating builder is an unreproducible build."""
        assert _checks(INSTRUCTED_DOCKERFILE)["dockerfile_base_pinned"] is True

    def test_it_is_multi_stage(self) -> None:
        assert _checks(INSTRUCTED_DOCKERFILE)["dockerfile_multi_stage"] is True

    def test_it_runs_as_a_non_root_uid(self) -> None:
        assert _checks(INSTRUCTED_DOCKERFILE)["dockerfile_non_root"] is True

    def test_it_declares_a_healthcheck(self) -> None:
        assert _checks(INSTRUCTED_DOCKERFILE)["dockerfile_healthcheck_present"] is True

    def test_every_requirement_is_reflected_in_the_shape(self) -> None:
        """A cheap consistency check on the requirement list itself.

        Not a substitute for the predicates above -- it cannot be, since it only looks for tokens -- but it
        catches the specific mistake of adding a requirement naming a literal that the instructed shape does
        not contain, which is how the two drift apart.
        """
        requirements = " ".join(GATE_REQUIREMENTS["dockerfile"])
        for literal in ("USER 10001", "AS builder", "COPY --from=builder", "HEALTHCHECK"):
            assert literal in requirements, f"{literal!r} is not in the requirements"
            assert literal in INSTRUCTED_DOCKERFILE, f"{literal!r} is required but the shape omits it"

    def test_the_requirements_forbid_the_thing_the_model_kept_writing(self) -> None:
        """`FROM alpine:latest` was the actual failure, three attempts running.

        The requirement must name the forbidden token, not merely describe pinning: the previous wording said
        "never a floating tag" and the model wrote `:latest` anyway.
        """
        requirements = " ".join(GATE_REQUIREMENTS["dockerfile"])
        assert ":latest" in requirements
        assert "alpine:latest" not in INSTRUCTED_DOCKERFILE
        assert ":latest" not in INSTRUCTED_DOCKERFILE

    def test_a_single_stage_dockerfile_is_correctly_refused(self) -> None:
        """The negative control. Without it the assertions above could pass on a gate that accepts anything."""
        single_stage = 'FROM python:3.13-slim\nUSER 10001\nCMD ["python", "-m", "app"]\n'
        assert _checks(single_stage)["dockerfile_multi_stage"] is False

    def test_a_latest_tag_is_correctly_refused(self) -> None:
        floating = (
            "FROM python:3.13-slim AS builder\nRUN true\n"
            "FROM alpine:latest\nCOPY --from=builder /app /app\nUSER 10001\n"
        )
        assert _checks(floating)["dockerfile_base_pinned"] is False

    def test_a_builder_stage_that_floats_is_refused_even_when_the_final_stage_is_pinned(self) -> None:
        """The asymmetry worth testing: pinning only what ships is not pinning."""
        floating_builder = (
            "FROM golang:latest AS builder\nRUN true\nFROM alpine:3.21\nCOPY --from=builder /app /app\nUSER 10001\n"
        )
        # Asserted on the predicate directly too, because this asymmetry is the one a future refactor of
        # the engine is most likely to lose.
        assert dockerfile_base_pinned(floating_builder) is False
        assert _checks(floating_builder)["dockerfile_base_pinned"] is False

    def test_a_build_arg_base_image_is_correctly_refused(self) -> None:
        """THE CONSTRUCT THE MODEL ACTUALLY CHOSE, found by the harness on the third iteration.

        With `:latest` forbidden and an exact-version example given, 7b wrote:

            ARG BASE_IMAGE=python:3.13-slim
            FROM $BASE_IMAGE AS builder

        which looks more careful than a hard-coded tag and is refused -- correctly. A build argument can
        be overridden at build time, so the file does not establish what it builds on. The requirement now
        names the construct rather than only describing the goal, which is the same lesson as `USER 10001`
        for the third time: a small model needs the forbidden shape named, not inferred.
        """
        deferred = (
            "ARG BASE_IMAGE=python:3.13-slim\n"
            "FROM $BASE_IMAGE AS builder\nRUN true\n"
            "FROM $BASE_IMAGE\nCOPY --from=builder /app /app\nUSER 10001\n"
        )
        assert dockerfile_base_pinned(deferred) is False
        assert _checks(deferred)["dockerfile_base_pinned"] is False

    def test_the_requirement_names_the_build_arg_construct(self) -> None:
        """Describing the goal was not enough twice; the shape has to be named."""
        requirements = " ".join(GATE_REQUIREMENTS["dockerfile"])
        assert "ARG" in requirements
        assert "$SOMETHING" in requirements or "$" in requirements
        # And the instructed shape must not itself use one.
        assert "ARG " not in INSTRUCTED_DOCKERFILE
