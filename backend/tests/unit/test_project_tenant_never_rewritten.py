# SPDX-License-Identifier: FSL-1.1-ALv2
"""No runtime code may change a project's tenant after it is created.

THE DEFECT THIS GUARDS. Autonomous deployment once ran
`UPDATE projects SET tenant_id = :t WHERE id = :p AND tenant_id IS NULL` with a tenant derived from the
project id. Every project query is tenant-scoped, so the project disappeared from its owner's list the
moment a deployment started -- the row, its files and its runs all still in the database. Users deleted
and re-imported the same project dozens of times (49 `project_deleted` audit rows) chasing it.

A project's tenant is set by INSERT and never moved. This scans the source for any statement or ORM
assignment that would move it, so a reintroduction fails here rather than in a user's project list.
"""

from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"

_SQL_REWRITE = re.compile(r"UPDATE\s+projects\s+SET[^\"']*\btenant_id\b", re.IGNORECASE)
_ORM_REWRITE = re.compile(r"\b(proj|project)\.tenant_id\s*=(?!=)")
_DERIVED_TENANT = "urn:forgeops:project-tenant:"


def test_no_source_moves_a_project_between_tenants() -> None:
    offenders: list[str] = []
    for path in SRC.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        for number, line in enumerate(source.splitlines(), start=1):
            if _SQL_REWRITE.search(line) or _ORM_REWRITE.search(line) or _DERIVED_TENANT in line:
                offenders.append(f"{path.relative_to(SRC)}:{number}: {line.strip()}")
    assert offenders == [], "a project's tenant must never be rewritten after creation:\n" + "\n".join(offenders)
