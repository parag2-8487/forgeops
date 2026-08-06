# SPDX-License-Identifier: FSL-1.1-ALv2
"""Model tier selection logic (Leaf 13.4)."""

from __future__ import annotations

from typing import Literal

ModelTier = Literal["tier_1_fast", "tier_2_balanced", "tier_3_advanced"]


def select_model_tier(prompt_token_count: int, requires_complex_reasoning: bool = False) -> ModelTier:
    """Select LLM model tier based on prompt length and task complexity.

    - tier_1_fast: token count < 500 and simple task
    - tier_2_balanced: token count < 2000 or moderate reasoning
    - tier_3_advanced: token count >= 2000 or complex reasoning required
    """
    if requires_complex_reasoning or prompt_token_count >= 2000:
        return "tier_3_advanced"
    if prompt_token_count >= 500:
        return "tier_2_balanced"
    return "tier_1_fast"
