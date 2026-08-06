# SPDX-License-Identifier: FSL-1.1-ALv2
"""Structurally bounded AI feedback repair loop (Leaf 13.6)."""

from __future__ import annotations

from typing import Callable, Any
from pydantic import BaseModel


class LoopResult(BaseModel):
    success: bool
    iterations_used: int
    final_output: str
    errors: list[str]


class BoundedFeedbackLoop:
    def __init__(self, max_iterations: int = 3) -> None:
        self.max_iterations = max_iterations

    async def execute_loop(
        self,
        generator_fn: Callable[[list[str]], str],
        validator_fn: Callable[[str], tuple[bool, list[str]]],
    ) -> LoopResult:
        """Run generation-validation loop up to max_iterations."""
        errors: list[str] = []
        output = ""

        for iter_num in range(1, self.max_iterations + 1):
            output = generator_fn(errors)
            valid, errs = validator_fn(output)

            if valid:
                return LoopResult(
                    success=True,
                    iterations_used=iter_num,
                    final_output=output,
                    errors=[],
                )
            errors = errs

        return LoopResult(
            success=False,
            iterations_used=self.max_iterations,
            final_output=output,
            errors=errors,
        )
