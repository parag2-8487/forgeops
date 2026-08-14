# SPDX-License-Identifier: FSL-1.1-ALv2
"""Property test Q-09: Rubric non-interference (Leaf 13.11)."""

import pytest
from hypothesis import given
from hypothesis import strategies as st
from src.generation.rubric import AdvisoryRubric, BlockingGate

pytestmark = [pytest.mark.mandatory]


@given(text=st.text(min_size=1, max_size=500))
def test_property_q09_rubric_non_interference(text: str):
    gate = BlockingGate()
    rubric = AdvisoryRubric()

    gate_res1 = gate.evaluate(text)
    rubric.evaluate(text)
    gate_res2 = gate.evaluate(text)

    # Advisory rubric evaluation MUST NOT mutate or influence gate results
    assert gate_res1.passed == gate_res2.passed
    assert gate_res1.violations == gate_res2.violations
