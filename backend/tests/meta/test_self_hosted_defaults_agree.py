# SPDX-License-Identifier: FSL-1.1-ALv2
"""The self-hosted model settings must mean the same thing everywhere they are written down.

A value that differs between the committed default, `.env.example` and the CI workflow is a trap,
and this repository had one. `test_self_hosted_generation.py` carried
`DEFAULT_EMBEDDING_MODEL = "nomic-embed-text:latest"` under a comment saying "they are the values
`.env.example` ships", while `.env.example` had shipped `bge-m3:567m` since D-48 moved the local
embedding width to 1024. The result was three tests that failed on a developer's machine and passed
in CI, which reads as a broken machine rather than a broken constant -- so it was recorded as an
environment note and left.

Nothing compared the three places, so this does. It is a meta test rather than a comment because a
comment is what was there.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
DOTENV_EXAMPLE = REPO_ROOT / ".env.example"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

#: The keys whose value must be identical in all three places, and the constant in the test module
#: that restates each one.
PINNED = {
    "SELF_HOSTED_MODEL_ID": "DEFAULT_MODEL",
    "SELF_HOSTED_EMBEDDING_MODEL_ID": "DEFAULT_EMBEDDING_MODEL",
}


def _dotenv_value(key: str) -> str | None:
    for raw in DOTENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line.startswith(f"{key}="):
            continue
        # `.env.example` puts explanatory comments after the value on the same line.
        return line.split("=", 1)[1].split("#", 1)[0].strip()
    return None


def _workflow_value(key: str) -> str | None:
    pattern = re.compile(rf"^\s*{re.escape(key)}:\s*(\S+)\s*$")
    for raw in CI_WORKFLOW.read_text(encoding="utf-8").splitlines():
        found = pattern.match(raw)
        if found:
            return found.group(1)
    return None


def _module_constant(name: str) -> str:
    from tests.integration import test_self_hosted_generation as module

    value = getattr(module, name)
    assert isinstance(value, str), f"{name} must be a string, got {type(value)!r}"
    return value


@pytest.mark.parametrize(("key", "constant"), sorted(PINNED.items()))
def test_the_committed_default_matches_dotenv_example(key: str, constant: str) -> None:
    """The test module's fallback must be the value a developer's `.env` would carry."""
    shipped = _dotenv_value(key)
    assert shipped, f".env.example declares no {key}; it is the source the constants restate"
    assert _module_constant(constant) == shipped, (
        f"{constant} is {_module_constant(constant)!r} but .env.example ships {key}={shipped!r}. "
        f"A committed default that disagrees with the shipped environment fails locally and passes "
        f"in CI, which is how this became an environment note instead of a bug."
    )


@pytest.mark.parametrize(("key", "constant"), sorted(PINNED.items()))
def test_dotenv_example_matches_the_ci_workflow(key: str, constant: str) -> None:
    """And CI must be running the same model, or "passes in CI" means something else again."""
    del constant
    shipped = _dotenv_value(key)
    in_ci = _workflow_value(key)
    assert in_ci, f"ci.yml sets no {key}; the backend job needs it to select the model"
    assert shipped == in_ci, (
        f".env.example ships {key}={shipped!r} but ci.yml sets {in_ci!r}. These must agree, or a "
        f"local run and a CI run exercise different models and only one of them is evidence."
    )


def test_the_pinned_set_is_not_empty() -> None:
    """A parametrised check over an empty set passes without asserting anything."""
    assert PINNED, "the pinned key set is empty, so both tests above would be vacuous"
