# SPDX-License-Identifier: FSL-1.1-ALv2
"""Evidence collection for root-cause analysis. Phase 2 §2.11.

THE SOURCES ARE NOT MERGED, and that is the design rather than an omission. Metrics, logs, Kubernetes state
and deployment history measure different things over different windows by different mechanisms: a metric is
a scraped series, a log line is a moment, a pod's status is a point read. Combining them into one
"confidence" number would be the same mistake as averaging a container point-sample with a process series --
it produces a figure nobody measured, and an operator cannot tell which part of it to distrust.

So each source produces its own `EvidenceRecord` carrying its own reachability and its own prose, and the
analysis counts how many were reachable and REFUSES TO CONCLUDE below a floor. That floor is the honest
part: an RCA drawn from one of five sources is not wrong, but it is differently trustworthy, and a system
presenting it identically to one drawn from five is lying by omission.

METRICS EVIDENCE ARRIVES THROUGH `core.metrics_port`, not from a query written here. `check-chokepoint`
refused `incidents` importing `monitoring` and was right; the answer was a narrow Protocol rather than an
exemption or a second query path. See that module for why the second option was the worse of the two.
"""

from __future__ import annotations

from typing import Protocol

from ..core.metrics_port import EvidenceRecord

# `Finding` is the core `EvidenceRecord`, aliased rather than redefined. A parallel dataclass here would be
# two shapes for one fact, and the day they drift is the day an unreachable source is recorded as reachable
# because one of them defaulted differently.
Finding = EvidenceRecord


class LogReader(Protocol):
    """Reading recent errors for a service. A Protocol so `incidents` does not import a log client.

    Deliberately narrow: a caller cannot pass a query. The implementation decides what "recent errors for
    this service" means, for the same reason the PromQL catalogue exists -- a free-text log query with a
    label selector in it can read another tenant's streams.

    Returns `(reachable, note, lines)`. `note` is prose explaining an unreachable result, so the caller
    never has to invent the sentence it shows the operator.
    """

    async def recent_errors(self, *, service: str, limit: int) -> tuple[bool, str, list[str]]: ...


async def collect_log_evidence(logs: LogReader | None, *, service: str) -> Finding:
    """Recent error lines for the service, or a stated reason there are none to show."""
    if logs is None:
        return Finding(
            kind="logs",
            reachable=False,
            summary=(
                "No log store is configured for this deployment, so no log lines were consulted. The "
                "analysis is working without them."
            ),
        )
    try:
        reachable, note, lines = await logs.recent_errors(service=service, limit=50)
    except Exception as exc:  # noqa: BLE001 - a log store failure must not fail the analysis
        # Recorded as unreachable rather than raised. An RCA that crashes because the log store is down is
        # useless precisely when it is needed; one that says "logs unavailable" is still worth reading.
        return Finding(
            kind="logs",
            reachable=False,
            summary=f"The log store could not be read ({type(exc).__name__}), so no log lines were consulted.",
        )
    if not reachable:
        return Finding(kind="logs", reachable=False, summary=note)
    if not lines:
        # DISTINCT FROM UNREACHABLE, and the distinction is the point: the store answered and there are
        # genuinely no error lines, which is real evidence that whatever failed did not log an error.
        return Finding(
            kind="logs",
            reachable=True,
            summary=(
                "The log store answered and holds no error lines for this service in the window. That is a "
                "finding rather than a gap: whatever failed did not log an error."
            ),
            detail={"lines": []},
        )
    return Finding(
        kind="logs",
        reachable=True,
        summary=f"{len(lines)} error line(s) found in the window.",
        detail={"lines": lines[:50]},
    )


def evidence_floor(findings: list[Finding]) -> tuple[bool, str]:
    """Whether there is enough to conclude anything, and the sentence explaining the answer.

    TWO REACHABLE SOURCES, and the number is a judgement worth stating. One source can establish that
    something broke but not what: a spike in the error ratio says users saw failures, not why. The floor is
    deliberately low rather than high, because an RCA is a starting point for a human and not a verdict --
    but it is non-zero, because a conclusion drawn from nothing is the fabrication this refuses.
    """
    reachable = [finding for finding in findings if finding.reachable]
    if len(reachable) < 2:
        unreachable = sorted({finding.kind for finding in findings if not finding.reachable})
        return False, (
            f"Only {len(reachable)} of {len(findings)} evidence sources could be read"
            + (f" ({', '.join(unreachable)} unavailable)" if unreachable else "")
            + ". That is not enough to identify a cause: one source can show that something broke but not "
            "what broke it. The incident and the evidence collected are recorded; no cause has been "
            "inferred, which is different from no cause existing."
        )
    return True, f"{len(reachable)} of {len(findings)} evidence sources were readable."
