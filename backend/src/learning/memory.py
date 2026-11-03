# SPDX-License-Identifier: FSL-1.1-ALv2
"""The `MemoryPort` implementation. Phase 2 2.13.

Thin on purpose: the selection, the budget and the ordering all live in `reflector.py`, which owns them. This
is the adapter that lets `generation` depend on `core.memory_port` instead of on this domain -- see that
module for why an exemption and a duplicated selection were both rejected.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from ..core.memory_port import InjectedMemory
from .reflector import compile_skill_file, record_skill_file


class LearningMemory:
    """Satisfies `core.memory_port.MemoryPort`.

    Takes the session per call rather than holding one: prompt assembly is already inside a request's
    transaction, and a memory adapter with its own session would write the injection record outside the
    transaction that wrote the run -- so a rolled-back run would leave an injection claiming to have shaped it.
    """

    async def memory_for_prompt(self, session: AsyncSession, *, project_id: uuid.UUID) -> InjectedMemory:
        skill_file = await compile_skill_file(session, project_id=project_id)
        return InjectedMemory(
            content=skill_file.content,
            included=list(skill_file.included),
            excluded=list(skill_file.excluded),
        )

    async def record_injection(self, session: AsyncSession, *, project_id: uuid.UUID, memory: InjectedMemory) -> None:
        from .reflector import SkillFile

        await record_skill_file(
            session,
            project_id=project_id,
            skill_file=SkillFile(
                content=memory.content,
                included=memory.included,
                excluded=memory.excluded,
                explanation="",
            ),
        )
