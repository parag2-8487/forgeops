# SPDX-License-Identifier: FSL-1.1-ALv2
"""HEALTHY fixture property for the mutation harness's meta tests.

It asserts something its negative control genuinely destroys, so the harness must
observe a FAIL and report `OK`.

Deliberately tiny and dependency-free: the meta test is about the harness, not
about the subject, and a fixture that needed a database would make the harness's
own tests skip — which is the failure mode §0.4.4 exists to prevent.
"""

from __future__ import annotations

from tests.meta.fixtures.mutation import subject


def test_counter_strictly_decreases() -> None:
    """The property: `step` always decrements, so iteration terminates."""
    remaining = 3
    seen = [remaining]
    for _ in range(3):
        remaining = subject.step(remaining)
        seen.append(remaining)
    assert seen == [3, 2, 1, 0], seen


def test_step_never_returns_its_input() -> None:
    assert subject.step(5) != 5
