# SPDX-License-Identifier: FSL-1.1-ALv2
import pytest
from src.generation.rubric import AdvisoryRubric, BlockingGate

pytestmark = [pytest.mark.mandatory]


def test_blocking_gate_valid():
    gate = BlockingGate()
    res = gate.evaluate("FROM python:3.11\nCMD ['python', 'main.py']")
    assert res.passed is True
    assert len(res.violations) == 0


def test_blocking_gate_violation():
    gate = BlockingGate()
    res = gate.evaluate("RUN eval('bad_code')")
    assert res.passed is False
    assert len(res.violations) == 1


def test_advisory_rubric_non_interference():
    gate = BlockingGate()
    rubric = AdvisoryRubric()

    content = "FROM python:3.11\nRUN eval('bad')"
    gate_res = gate.evaluate(content)
    rubric_res = rubric.evaluate(content)

    # Gate must fail due to security violation
    assert gate_res.passed is False
    # Advisory rubric score has no effect on gate_res.passed
    assert rubric_res.advisory_score > 0
