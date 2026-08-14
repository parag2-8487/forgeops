# SPDX-License-Identifier: FSL-1.1-ALv2
"""Property test Q-18: Readiness scoring determinism and monotonicity (Leaf 12.5)."""

import pytest
from hypothesis import given
from hypothesis import strategies as st
from src.projects.readiness import ReadinessEngine

pytestmark = [pytest.mark.mandatory]


@given(
    has_docker=st.booleans(),
    has_ci=st.booleans(),
    has_tests=st.booleans(),
)
def test_property_q18_readiness_determinism_and_monotonicity(has_docker: bool, has_ci: bool, has_tests: bool):
    engine = ReadinessEngine()

    manifests = ["Dockerfile"] if has_docker else []
    config_files = [".github/workflows/ci.yml"] if has_ci else []

    project_data = {
        "manifests": manifests,
        "config_files": config_files,
        "has_tests": has_tests,
    }

    res1 = engine.evaluate_project(project_data)
    res2 = engine.evaluate_project(project_data)

    # 1. Determinism
    assert res1.overall_score == res2.overall_score
    assert res1.level == res2.level
    assert 0 <= res1.overall_score <= 100

    # 2. Monotonicity: Adding tests never decreases score
    more_data = dict(project_data)
    more_data["has_tests"] = True
    res_more = engine.evaluate_project(more_data)
    assert res_more.overall_score >= res1.overall_score
