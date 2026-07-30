# SPDX-License-Identifier: FSL-1.1-ALv2
"""Subject under mutation for the harness's meta tests.

Deliberately trivial: the meta tests are about the harness, not about this code.
"""

from __future__ import annotations

UNRELATED = 42


def step(remaining: int) -> int:
    """Decrement toward zero. The negative control removes the decrement."""
    if remaining <= 0:
        return 0
    return remaining - 1
