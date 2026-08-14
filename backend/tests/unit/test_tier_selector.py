# SPDX-License-Identifier: FSL-1.1-ALv2
import pytest
from src.ai.routing.tier_selector import select_model_tier

pytestmark = [pytest.mark.mandatory]


def test_tier_1_fast_reachability():
    assert select_model_tier(200, requires_complex_reasoning=False) == "tier_1_fast"


def test_tier_2_balanced_reachability():
    assert select_model_tier(1000, requires_complex_reasoning=False) == "tier_2_balanced"


def test_tier_3_advanced_reachability():
    assert select_model_tier(3000, requires_complex_reasoning=False) == "tier_3_advanced"
    assert select_model_tier(100, requires_complex_reasoning=True) == "tier_3_advanced"
