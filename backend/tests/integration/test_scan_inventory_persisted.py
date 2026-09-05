# SPDX-License-Identifier: FSL-1.1-ALv2
"""The inventory the agent computes must survive the scan, not only feed a hash.

`_record_analysis_report` has accepted an `inventory` argument since revision 0015, and its docstring
says in as many words that "The INVENTORY is persisted here too … and that is FR-11". Its ONLY call site
omitted the argument, so the parameter took its `None` default and every row in `analysis_reports` was
written with `inventory = {}`.

Verified against the live database before the fix: every report, for every project, `length(inventory) =
2`. The agent detected the languages, manifests, config files, entry points, frameworks and package
managers on every single scan, sent them, had them validated by `ScanInventoryIn` — and they survived
only as an input to `inventory_hash`. The hash proves two scans agreed; it cannot say what they agreed
about. So nothing could show an operator what was detected, and nothing could put it in a generation
prompt, which is the whole point of detecting it.

This is the same defect family as the rest of this pass: a column, a parameter, a validated payload and
a docstring all describing a feature that one missing keyword argument made unreachable.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.analysis.indexer import (
    ScanFrameworkIn,
    ScanInventoryIn,
    ScanReportIn,
    persist_scan_report,
)

pytestmark = pytest.mark.asyncio


def _report() -> ScanReportIn:
    """A report shaped like the one the agent really sends, with two ecosystems detected."""
    return ScanReportIn(
        schema_version=1,
        inventory=ScanInventoryIn(
            languages=["javascript", "python"],
            manifests=["package.json", "requirements.txt"],
            config_files=[],
            entry_points=["src/main.py"],
            file_count=5,
            total_size_bytes=1234,
            package_managers=["npm", "pip"],
            frameworks=[
                ScanFrameworkIn(
                    name="FastAPI",
                    kind="web",
                    confidence="declared",
                    evidence="requirements.txt",
                    version="==0.115.0",
                ),
                ScanFrameworkIn(
                    name="Express",
                    kind="web",
                    confidence="declared",
                    evidence="package.json",
                    version="^4.18.2",
                ),
            ],
        ),
        files=[],
        dependencies=[],
        inventory_hash="a" * 64,
    )


async def _project(session: AsyncSession) -> uuid.UUID:
    project_id = uuid.uuid4()
    # The same three columns `chokepoint_support` uses, so this cannot drift from the real schema.
    await session.execute(
        text("INSERT INTO projects (id, name, path) VALUES (:i, :n, :p)"),
        {"i": project_id, "n": f"inv-{project_id.hex[:8]}", "p": "/tmp/inv"},
    )
    return project_id


class TestTheInventoryIsPersisted:
    async def test_a_scan_writes_the_detected_facts(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        """The regression test for the missing keyword argument."""
        async with sessions() as session:
            project_id = await _project(session)
            await persist_scan_report(
                session,
                project_id=project_id,
                tenant_id=None,
                report=_report(),
                embedder=None,
                embedder_absent_reason="no embedder configured for this test",
            )
            await session.commit()

            stored = (
                await session.execute(
                    text(
                        "SELECT inventory FROM analysis_reports WHERE project_id = :p ORDER BY created_at DESC LIMIT 1"
                    ),
                    {"p": project_id},
                )
            ).scalar_one()

        assert stored, "the report row carried no inventory at all"
        assert stored != {}, (
            "the inventory was written as an empty object; that is the exact symptom of the call site "
            "omitting the argument, and it made FR-10 and FR-11 unreachable"
        )
        assert stored["languages"] == ["javascript", "python"]
        assert stored["manifests"] == ["package.json", "requirements.txt"]
        assert stored["entry_points"] == ["src/main.py"]
        assert stored["package_managers"] == ["npm", "pip"]

    async def test_the_frameworks_keep_their_evidence_and_confidence(
        self, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        """Both are load-bearing for a generation prompt.

        `declared` means a manifest names the dependency; `inferred` means the layout looks
        characteristic and nothing declares it. A prompt may act on the first and must not act on the
        second, and the evidence path is what makes either checkable. Persisting the names alone would
        turn a checkable detection into an assertion.
        """
        async with sessions() as session:
            project_id = await _project(session)
            await persist_scan_report(
                session,
                project_id=project_id,
                tenant_id=None,
                report=_report(),
                embedder=None,
                embedder_absent_reason="no embedder configured for this test",
            )
            await session.commit()
            stored = (
                await session.execute(
                    text(
                        "SELECT inventory FROM analysis_reports WHERE project_id = :p ORDER BY created_at DESC LIMIT 1"
                    ),
                    {"p": project_id},
                )
            ).scalar_one()

        by_name = {f["name"]: f for f in stored["frameworks"]}
        assert set(by_name) == {"FastAPI", "Express"}
        assert by_name["FastAPI"]["evidence"] == "requirements.txt"
        assert by_name["FastAPI"]["confidence"] == "declared"
        # The declared constraint verbatim, not a resolved version: resolving would mean running the
        # package manager against the operator's tree.
        assert by_name["FastAPI"]["version"] == "==0.115.0"
        assert by_name["Express"]["evidence"] == "package.json"

    async def test_an_agent_that_reports_no_inventory_still_indexes(
        self, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        """An older agent must not be broken by this, and its silence must not look like a finding.

        An empty inventory is recorded as empty lists rather than omitted, because "this agent does not
        report frameworks" and "this project has no frameworks" are different claims and only one of them
        is knowable here.
        """
        async with sessions() as session:
            project_id = await _project(session)
            report = ScanReportIn(schema_version=1, files=[], dependencies=[], inventory_hash="b" * 64)
            await persist_scan_report(
                session,
                project_id=project_id,
                tenant_id=None,
                report=report,
                embedder=None,
                embedder_absent_reason="no embedder configured for this test",
            )
            await session.commit()
            stored = (
                await session.execute(
                    text(
                        "SELECT inventory FROM analysis_reports WHERE project_id = :p ORDER BY created_at DESC LIMIT 1"
                    ),
                    {"p": project_id},
                )
            ).scalar_one()

        assert stored["languages"] == []
        assert stored["frameworks"] == []
        assert stored["file_count"] == 0
