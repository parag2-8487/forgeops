# SPDX-License-Identifier: FSL-1.1-ALv2
"""Deployment-readiness scoring, re-exported from `core.readiness`.

WHY THE ENGINE MOVED AND THIS FILE REMAINS

The scoring engine used to live here, and `analysis.indexer` imported it so a scan could record the
score it produced into `analysis_reports`. That is a cross-domain import, which §2.2.1 bans and
`scripts/check-chokepoint.sh` rejects by parsing the import graph — the ban is what keeps a domain
replaceable, and it caught this the first time the two were wired together.

The engine is a PURE function of `IndexEvidence`: stdlib and pydantic, no session, no domain models,
no I/O. So `core` is where it belongs once two domains need it, and moving it is not a compromise —
it is the same reasoning that puts `IndexEvidence` in `core.index_evidence`.

This module stays because `projects` is where a reader looks for a project's readiness, and because
`GET /projects/{id}/readiness` is the endpoint that serves it. It re-exports rather than wraps: a
wrapper would be a second place for the argument order to drift.
"""

from __future__ import annotations

from ..core.index_evidence import CONTENT_PATTERNS, IndexEvidence, load_index_evidence
from ..core.readiness import (
    CATEGORY_FIELDS,
    CATEGORY_WEIGHTS,
    ReadinessBreakdown,
    ReadinessCheck,
    ReadinessEngine,
    ReadinessResult,
    apply_ignore_globs,
)

__all__ = [
    "CATEGORY_FIELDS",
    "CATEGORY_WEIGHTS",
    "CONTENT_PATTERNS",
    "IndexEvidence",
    "ReadinessBreakdown",
    "ReadinessCheck",
    "ReadinessEngine",
    "ReadinessResult",
    "apply_ignore_globs",
    "load_index_evidence",
]
