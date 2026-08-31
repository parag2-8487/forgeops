# SPDX-License-Identifier: FSL-1.1-ALv2
"""The repository facts a readiness score is computed from, and the query that reads them.

WHY THIS IS IN `core` AND NOT IN EITHER DOMAIN

Two domains need it and §2.2.1 bans them from importing each other. `analysis` owns the tables the
evidence is read from (`file_tree`, `file_contents`); `projects` owns the scoring engine that
consumes it. Putting the type in either one forces the other to import across a domain boundary,
which `scripts/check-chokepoint.sh` rejects by parsing the import graph — and rightly, because the
ban is what keeps a domain replaceable. `src.core` is the sanctioned shared floor.

The alternative considered and rejected was passing a plain `dict`. A dict would type-check
everywhere and drift silently: the engine reads `paths` and `contents` with specific normalisation
rules, and a caller that spelled a key differently would score every project as unindexed rather
than fail. A frozen model makes the contract checkable at the boundary.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Final

from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

#: The files whose BODIES the score depends on, as SQL LIKE patterns against a lowercased path.
#:
#: A path alone cannot answer "is this a multi-stage build" or "does this container run as non-root";
#: only the body can. Loading every body to answer six questions would read the whole repository out
#: of the database on every readiness request, so the set is explicit and small.
CONTENT_PATTERNS: Final[tuple[str, ...]] = (
    "%dockerfile%",
    "%docker-compose%.yml",
    "%docker-compose%.yaml",
    "%.github/workflows/%",
    "%k8s/%.yaml",
    "%k8s/%.yml",
    # Widened for FR-20. The three Kubernetes checks PARSE the manifest, so the body has to be loaded —
    # and manifests live under more than one conventional directory. Without these the checks would read
    # an empty body and report every repository as unbounded: the fail-closed direction, but not a true
    # answer, and a readiness score that is wrong in the pessimistic direction is still wrong.
    "%kubernetes/%.yaml",
    "%kubernetes/%.yml",
    "%manifests/%.yaml",
    "%manifests/%.yml",
    "%deploy/%.yaml",
    "%deploy/%.yml",
    "%deployment/%.yaml",
    "%deployment/%.yml",
    "%charts/%/templates/%.yaml",
)


class IndexEvidence(BaseModel):
    """The repository facts a score may be computed from.

    Deliberately not a `Project` or a session: the engine is pure, so the same evidence always
    produces the same score and the property test (Q-18) can construct evidence directly rather
    than through a database.
    """

    #: Slash-separated repository-relative paths from `file_tree`.
    paths: tuple[str, ...] = ()
    #: `file_contents.content` (REDACTED text) keyed by lowercased path, for the decision-relevant
    #: files only. Content is needed for multi-stage and non-root, which a path cannot answer.
    contents: Mapping[str, str] = Field(default_factory=dict)
    #: An explicit statement about test presence. `None` means "derive it from the paths", which is
    #: the honest default — the field exists because a caller may know something the path list does
    #: not, NOT so that it can be assumed true.
    has_tests: bool | None = None
    #: `file_contents.redaction_count` keyed by path, for files where it is above zero (FR-42).
    #:
    #: A redaction IS a secret finding: the agent's scanner matched something, and the body that reached
    #: the database has a marker where the value was. That evidence has been persisted since revision
    #: `0003` and nothing read it, so "secret scanning of the codebase" happened on every index and was
    #: invisible afterwards — which is what made FR-42 partial.
    #:
    #: COUNTS, NEVER VALUES. §7.11 puts `file_contents` in the "redacted text only" class, and the whole
    #: point of the redaction is that the value did not survive. A count and a path are enough to send an
    #: operator to the line.
    redaction_counts: Mapping[str, int] = Field(default_factory=dict)

    model_config = {"frozen": True}


async def load_index_evidence(session: AsyncSession, *, project_id: uuid.UUID) -> IndexEvidence:
    """Read the project's indexed paths, plus the bodies the score depends on.

    Two statements rather than one join: the path list is every row and the body list is a handful,
    so a single join would carry every file's content for the sake of six of them.

    An unindexed project yields empty evidence rather than an error. That is what makes the score
    honest: `ReadinessEngine` reports `indexed=False` and a zero, instead of the caller having to
    decide what an exception means.
    """
    path_rows = await session.execute(
        text("SELECT path FROM file_tree WHERE project_id = :project_id ORDER BY path"),
        {"project_id": project_id},
    )
    paths = [str(row[0]) for row in path_rows]

    content_rows = await session.execute(
        text(
            "SELECT f.path, c.content FROM file_contents c JOIN file_tree f ON f.id = c.file_id "
            "WHERE f.project_id = :project_id AND ("
            + " OR ".join(f"lower(f.path) LIKE :p{i}" for i in range(len(CONTENT_PATTERNS)))
            + ")"
        ),
        {"project_id": project_id, **{f"p{i}": pattern for i, pattern in enumerate(CONTENT_PATTERNS)}},
    )
    contents = {str(row[0]).replace("\\", "/").lower(): str(row[1]) for row in content_rows}

    # A third statement, and a deliberately narrow one: only the files with a redaction, which in a clean
    # repository is none. `WHERE c.redaction_count > 0` rather than reading every row and filtering here,
    # because the interesting set is small and the whole table is not.
    redaction_rows = await session.execute(
        text(
            "SELECT f.path, c.redaction_count FROM file_contents c JOIN file_tree f ON f.id = c.file_id "
            "WHERE f.project_id = :project_id AND c.redaction_count > 0 ORDER BY c.redaction_count DESC, f.path"
        ),
        {"project_id": project_id},
    )
    redaction_counts = {str(row[0]).replace("\\", "/"): int(row[1]) for row in redaction_rows}

    return IndexEvidence(paths=tuple(paths), contents=contents, redaction_counts=redaction_counts)
