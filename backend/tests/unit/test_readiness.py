# SPDX-License-Identifier: FSL-1.1-ALv2
import pytest
from src.projects.readiness import ReadinessEngine

pytestmark = [pytest.mark.mandatory]

def test_readiness_scoring_determinism():
    engine = ReadinessEngine()
    data = {
        "manifests": ["package.json", "Dockerfile"],
        "config_files": ["README.md", ".github/workflows/ci.yml"],
        "has_tests": True,
    }
    res1 = engine.evaluate_project(data)
    res2 = engine.evaluate_project(data)

    assert res1.overall_score == res2.overall_score
    assert res1.level == res2.level
    assert 0 <= res1.overall_score <= 100

def test_readiness_recommendations():
    engine = ReadinessEngine()
    data = {"manifests": [], "config_files": [], "has_tests": False}
    res = engine.evaluate_project(data)

    assert res.level == "blocked"
    assert len(res.recommendations) >= 1
