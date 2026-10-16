# SPDX-License-Identifier: FSL-1.1-ALv2
"""The seam between an incident analysis and the metrics tier. Phase 2 §2.11.

WHY THIS FILE EXISTS. §2.11's analysis needs metrics evidence, and `check-chokepoint` refused
`incidents` importing `monitoring` -- correctly, because they are separate domains. The two bad answers
were available and both were rejected:

  * An exemption for the import. That is the thing the gate exists to refuse, and it would have opened
    `monitoring` to every domain in order to serve one.
  * A second query path inside `incidents`. That is worse than the first, because the PromQL catalogue is
    what makes tenant scoping structural -- a caller names a reviewed query and the matcher is composed
    from the verified principal. A second path, even one that only ran queries written by hand, reopens
    exactly what the catalogue closes: the next person needing "just one more query" adds it to whichever
    path is easier.

So the catalogue stays in `monitoring`, which owns it, and this Protocol is what `incidents` depends on.
There is one query path. `monitoring` decides which reviewed queries constitute incident evidence, because
that is a question about metrics; `incidents` decides what to conclude from them, because that is a question
about failures.

THE RECORD CARRIES `reachable` AS A REQUIRED FIELD. An evidence record cannot be constructed without stating
whether its source answered, which is what stops an analysis from silently treating a gap as a healthy
reading.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    """One source consulted during an analysis, and whether it said anything.

    `reachable=False` with prose is a COMPLETE record rather than a failed one: it is stored, shown to the
    operator, and counted against the evidence floor. Frozen, so a pipeline cannot upgrade an unreachable
    source to a reachable one after the fact.
    """

    kind: str
    reachable: bool
    #: Plain prose, rendered to the operator verbatim. Must read as a sentence, not a status code.
    summary: str
    detail: dict[str, Any] = field(default_factory=dict)


class MetricsEvidencePort(Protocol):
    """What an analysis needs from the metrics tier, and nothing more.

    Deliberately narrow: there is no method taking a query. A domain that could pass PromQL here would have
    the second query path this file exists to prevent.
    """

    async def incident_evidence(self, *, tenant_id: uuid.UUID | None) -> list[EvidenceRecord]:
        """Read the series that bear on almost any incident, as evidence records."""
        ...
