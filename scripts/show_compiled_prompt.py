# SPDX-License-Identifier: FSL-1.1-ALv2
"""Print the compiled generation prompt for a project, from its real index.

A reader has to be able to see exactly what the model was told. This is the command-line half of that;
the UI half reads the same value off the `generation_runs` row.
"""

from __future__ import annotations

import asyncio
import sys
import uuid

from sqlalchemy import text

from src.core.index_evidence import load_index_evidence
from src.core.readiness import ReadinessEngine
from src.generation.prompt_compiler import compile_prompt
from src.main import create_app


async def main() -> None:
    project_id = uuid.UUID(sys.argv[1])
    app = create_app()
    async with app.router.lifespan_context(app):
        async with app.state.sessionmaker() as session:
            evidence = await load_index_evidence(session, project_id=project_id)
            project_name = (
                await session.execute(
                    text("SELECT name FROM projects WHERE id = :p"), {"p": project_id}
                )
            ).scalar_one_or_none() or ""
            inventory = (
                await session.execute(
                    text(
                        "SELECT inventory FROM analysis_reports WHERE project_id = :p "
                        "ORDER BY created_at DESC LIMIT 1"
                    ),
                    {"p": project_id},
                )
            ).scalar_one_or_none() or {}

    result = ReadinessEngine().evaluate(evidence)
    compiled = compile_prompt(
        checks=result.checks,
        paths=evidence.paths,
        contents=evidence.contents,
        inventory=inventory,
        project_name=str(project_name),
    )
    print("=" * 78)
    print(compiled.text)
    print("=" * 78)
    print(f"score            : {result.overall_score}")
    print(f"token estimate   : {compiled.token_estimate} of {compiled.token_budget}")
    print(f"addressed checks : {', '.join(compiled.addressed_checks) or 'none'}")
    print(f"write targets    : {', '.join(compiled.write_targets) or 'none'}")
    print(f"deferred         : {', '.join(compiled.deferred_checks) or 'none'}")
    print(f"budget strategy  : {compiled.budget_strategy}")


asyncio.run(main())
