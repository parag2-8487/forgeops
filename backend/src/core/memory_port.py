# SPDX-License-Identifier: FSL-1.1-ALv2
"""The seam between prompt assembly and learned preferences. Phase 2 §2.13.

WHY THIS EXISTS. Generation needs this project's learned preferences in its prompt, and `check-chokepoint`
refused `generation` importing `learning` -- correctly, they are separate domains. The two easy answers were
both rejected, for the third time in this project and for the same reasons each time:

  * An exemption. It would open `learning` to every domain in order to serve one, and the ban exists because
    a preference store reachable from anywhere is one nothing can reason about.
  * Duplicating the selection logic in `generation`. Worse, because the budget, the source ordering and the
    exclusion record are the parts that make injection auditable -- two copies of that would drift, and the
    copy nobody looked at would be the one running.

So `learning` keeps the selection and generation depends on this Protocol.

THE RETURN TYPE CARRIES THE EXCLUSIONS, and that is the load-bearing detail rather than an extra. A
preference that did not fit the budget never reaches the model, and from the output alone that is
indistinguishable from a preference that does not exist. A caller that received only the text could not
record the difference, so the record would be silently incomplete in exactly the place a user looks when
asking why their preference had no effect.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

# `session` is typed `Any` rather than `AsyncSession`: `core` must not depend on the ORM session type to
# express a seam, and every implementation and caller already has the concrete type in scope.
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class InjectedMemory:
    """What learned memory contributed to one prompt, and what it could not.

    Frozen: a prompt assembler that could edit the exclusion list after the fact would make the recorded
    injection disagree with the text actually sent, which is the one thing this record exists to prevent.
    """

    #: The block to append to the instruction. Empty when there is nothing active, which is a normal state
    #: for a project nobody has given feedback on.
    content: str
    included: list[uuid.UUID] = field(default_factory=list)
    #: Selected against but dropped for budget. See the module docstring for why this is not optional.
    excluded: list[uuid.UUID] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.content


class MemoryPort(Protocol):
    """What prompt assembly needs from the learning domain, and nothing more.

    Deliberately narrow: there is no method for reading feedback, writing a preference, or running
    reflection. A domain that could do those through this seam would be coupled to how learning works rather
    than to the fact that it happens -- and prompt assembly has no business writing preferences.
    """

    async def memory_for_prompt(self, session: Any, *, project_id: uuid.UUID) -> InjectedMemory:
        """Select this project's active preferences into a token-bounded block."""
        ...

    async def record_injection(self, session: Any, *, project_id: uuid.UUID, memory: InjectedMemory) -> None:
        """Record exactly what was injected, so a run can be judged rather than guessed at."""
        ...
