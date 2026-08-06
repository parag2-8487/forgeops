# SPDX-License-Identifier: FSL-1.1-ALv2
"""Property test Q-08: Iteration-bound termination (Leaf 13.10)."""

import pytest
import asyncio
from hypothesis import given, strategies as st
from src.generation.feedback_loop import BoundedFeedbackLoop

pytestmark = [pytest.mark.asyncio, pytest.mark.mandatory]

@given(max_iters=st.integers(min_value=1, max_value=10))
async def test_property_q08_iteration_bound_termination(max_iters: int):
    loop = BoundedFeedbackLoop(max_iterations=max_iters)
    gen = lambda errs: "content"
    val = lambda out: (False, ["error"])

    res = await loop.execute_loop(gen, val)
    assert res.iterations_used <= max_iters
    assert res.success is False
