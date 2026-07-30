# SPDX-License-Identifier: FSL-1.1-ALv2
"""VACUOUS fixture property for the mutation harness's meta tests.

This is P-09's situation in miniature: the assertions are real, they pass, and they
say nothing about the behaviour the control removes. It never touches `step`, so
mutating `step` cannot make it fail and the harness must report `VACUOUS`.

Without this fixture the harness's own green result would prove only that it can
observe a failure, not that it can catch a property which fails to observe one.
"""

from __future__ import annotations

from tests.meta.fixtures.mutation import subject


def test_the_module_is_importable() -> None:
    """True, useless, and entirely insensitive to the control."""
    assert subject is not None


def test_an_unrelated_constant_is_unchanged() -> None:
    assert subject.UNRELATED == 42
