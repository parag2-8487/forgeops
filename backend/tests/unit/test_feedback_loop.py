# SPDX-License-Identifier: FSL-1.1-ALv2
import pytest
from src.generation.feedback_loop import BoundedFeedbackLoop

pytestmark = [pytest.mark.asyncio, pytest.mark.mandatory]


async def test_feedback_loop_success_first_try():
    loop = BoundedFeedbackLoop(max_iterations=3)
    gen = lambda errs: "valid_content"
    val = lambda out: (True, [])

    res = await loop.execute_loop(gen, val)
    assert res.success is True
    assert res.iterations_used == 1


async def test_feedback_loop_max_iteration_termination():
    loop = BoundedFeedbackLoop(max_iterations=3)
    gen = lambda errs: "invalid_content"
    val = lambda out: (False, ["error_found"])

    res = await loop.execute_loop(gen, val)
    assert res.success is False
    assert res.iterations_used == 3
    assert "error_found" in res.errors
