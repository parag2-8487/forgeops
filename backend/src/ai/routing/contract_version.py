# SPDX-License-Identifier: FSL-1.1-ALv2
"""The version of the agreement between a compiled prompt and the gate that judges its answer.

WHY THIS EXISTS AS A NUMBER

The semantic cache keys on the model, the prompt and the parameters. None of those change when the
product's idea of a CORRECT answer changes, and that made the cache able to undo a correctness fix:

  * a run compiled a prompt, the model answered, the answer was cached
  * the gate later learned that the answer was not acceptable - a new validator, a stricter check, a
    repaired template
  * the next run on the same repository compiled the SAME prompt, hit the cache, and was served the
    answer the new gate would have refused

The fix could not take effect, and the only way to observe the new behaviour was to delete `ai:cache:*`
by hand. That had to be done twice while verifying fixes that had already shipped, which is the symptom
that made this necessary rather than theoretical.

WHEN TO BUMP IT

Bump when anything changes what a CORRECT answer looks like:

  * `prompt_compiler` - the instruction, its ordering, or what it tells the model to preserve
  * `model_prompt` - the `### FILE:` parse contract or the required artifact set
  * `artifact_checks` / `content_regression` / `target_checks` - the validator set or the gate rules
  * the template library, since a fallback answer is part of what a run may return
  * `readiness.py` check definitions, because `target_checks` refuses an artifact that fails them

Do NOT bump for a change that cannot alter whether an answer is acceptable - a log line, a docstring, a
performance change. A needless bump costs a cold cache and nothing else, so err towards bumping.

WHY NOT A HASH OF THE SOURCE. Hashing the modules would bump on every comment and be impossible to
reason about in an incident; a hand-maintained integer is reviewable, appears in the diff, and makes the
decision explicit. `test_the_contract_version_is_in_the_cache_key` fails if the key stops including it.
"""

from __future__ import annotations

from typing import Final

#: Incremented when the prompt-and-gate contract changes. See the module docstring for the rule.
#:
#: 1 - the contract as it stood before versioning existed.
#: 2 - Invariant 1 enforced at runtime (`target_checks.unsatisfied_targets`), Invariant 2's
#:     score-lowering refusal, the repaired template library, and the two readiness checks that stopped
#:     being presence checks. Every one of those changes which answers the gate accepts, so entries
#:     cached under version 1 must not be served.
GENERATION_CONTRACT_VERSION: Final[int] = 2
