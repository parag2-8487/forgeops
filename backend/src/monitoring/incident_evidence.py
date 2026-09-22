# SPDX-License-Identifier: FSL-1.1-ALv2
"""Metrics evidence for an incident analysis. Phase 2 §2.11.

THIS LIVES IN `monitoring` BECAUSE MONITORING OWNS THE CATALOGUE. `incidents` needs metrics evidence and
`check-chokepoint` refused it importing this domain; the answer is `core.metrics_port.MetricsEvidencePort`,
which this class implements. Which reviewed queries constitute incident evidence is a question about
metrics, so it is answered here; what to conclude from them is a question about failures, so it is answered
in `incidents`.

Every query below comes from `queries.CATALOGUE` -- the same enumerated catalogue the read route uses, with
the tenant matcher composed from the caller's tenant. There is no second query path.
"""

from __future__ import annotations

import uuid

from ..core.metrics_port import EvidenceRecord
from .queries import CATALOGUE
from .reader import MetricsReader

# WHY THESE THREE. An error ratio says whether users saw failures; a latency percentile says whether the
# system was degraded rather than broken; collector shedding says whether to believe either. The third is
# the one an RCA would omit and must not -- an analysis that read a healthy error ratio from a collector
# dropping 40% of its spans has read a filtered view of reality and does not know it.
_EVIDENCE_QUERIES: tuple[tuple[str, str], ...] = (
    ("http_error_ratio", "whether requests were failing"),
    ("http_latency_p95", "whether the system was slow"),
    ("collector_refused_spans", "whether the telemetry itself was complete"),
)


class MetricsEvidence:
    """Reads the incident-relevant series through the shared catalogue."""

    def __init__(self, reader: MetricsReader) -> None:
        self._reader = reader

    async def incident_evidence(self, *, tenant_id: uuid.UUID | None) -> list[EvidenceRecord]:
        records: list[EvidenceRecord] = []
        for name, question in _EVIDENCE_QUERIES:
            entry = CATALOGUE[name]
            arguments = {"window": "15m"} if "window" in entry.parameters else {}
            result = await self._reader.run(entry, tenant_id=tenant_id, arguments=arguments, window="15m")

            if result.verdict == "unconfigured":
                records.append(
                    EvidenceRecord(
                        kind="metrics",
                        reachable=False,
                        summary=(
                            f"No metrics store is configured, so nothing could be read about {question}. "
                            "This deployment has not chosen to run monitoring; that is supported, and it "
                            "means the analysis is working from fewer sources rather than from a healthy "
                            "signal."
                        ),
                        detail={"query": name, "verdict": result.verdict},
                    )
                )
                continue

            if result.verdict in ("unreachable", "never_reported"):
                records.append(
                    EvidenceRecord(
                        kind="metrics",
                        reachable=False,
                        summary=(
                            f"The metrics store could not answer about {question} "
                            f"({result.verdict.replace('_', ' ')}). The absence of a figure here is not a "
                            "healthy figure."
                        ),
                        detail={"query": name, "verdict": result.verdict},
                    )
                )
                continue

            # A STALE READING IS EVIDENCE. It may describe exactly the moment the incident occurred, which
            # is often the window wanted -- so it is admitted, with its age carried so the analysis and the
            # operator both know.
            values = [point.value for series in result.series for point in series.points if point.value is not None]
            peak = max(values) if values else None
            age = result.newest_sample_age_seconds
            records.append(
                EvidenceRecord(
                    kind="metrics",
                    reachable=True,
                    summary=(
                        f"{entry.describes} Peak observed: "
                        + (f"{peak}" if peak is not None else "no numeric value")
                        + (
                            f". This reading is {int(age or 0)} seconds old, so it describes an earlier moment."
                            if result.verdict == "stale"
                            else "."
                        )
                    ),
                    detail={
                        "query": name,
                        "verdict": result.verdict,
                        "peak": peak,
                        "promql": result.query,
                        "series_count": len(result.series),
                    },
                )
            )
        return records
