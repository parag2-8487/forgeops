# SPDX-License-Identifier: FSL-1.1-ALv2
"""Incident ingestion. Phase 2 §2.11.

EVERY SOURCE HERE ALREADY EXISTS AND ALREADY FAILS. Nothing in this module invents a feed: a deployment
failure is the settler's failure branch, a build failure is the validation gate rejecting an artifact set,
a Kubernetes warning and a non-zero container exit are read by agent operations that already exist, and a
circuit breaker opening is a row the breaker already writes. That is deliberate -- an ingestion path with no
producer is the defect class this project keeps finding (`DeploymentService.complete` with no caller,
`record_command_result`'s unreachable failure branch), and the way to avoid it is to ingest from things that
were already happening.

DEDUPLICATION IS BY FINGERPRINT AND IT INCREMENTS RATHER THAN INSERTING. A crash-looping pod produces an
event every few seconds; without this an operator opens the incident list during an outage and finds four
hundred rows describing one problem, which is worse than finding none because it buries everything else.
The fingerprint is STORED rather than recomputed on read, because the inputs that produce it may be gone by
the time anyone looks.

WHAT THIS MODULE DOES NOT DO: conclude anything. `ingest` writes what happened. Analysis is a separate
module with a separate table, so an incident can exist with no analysis -- which is the normal state when no
model is configured, and must not look like "analysed, nothing found".
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .models import INCIDENT_SOURCES, SEVERITIES


class IncidentSourceError(Exception):
    """A programming error in a caller, raised rather than coerced. See `Observation.__post_init__`."""


@dataclass(frozen=True, slots=True)
class Observation:
    """One thing that happened, as the source saw it.

    Frozen: an ingestion path that could edit the severity or the fingerprint after construction would make
    the deduplication non-deterministic, and two callers observing the same failure could file it twice.
    """

    project_id: uuid.UUID
    source: str
    severity: str
    title: str
    # The parts that identify this failure as distinct from another of the same kind. A container name and
    # an exit code, or a pod name and a reason. NOT a timestamp -- including one would make every
    # occurrence a new incident, which is the mistake this whole mechanism exists to avoid.
    identity: tuple[str, ...]
    detail: dict[str, Any] = field(default_factory=dict)
    origin_kind: str = ""
    origin_id: str = ""

    def __post_init__(self) -> None:
        if self.source not in INCIDENT_SOURCES:
            raise IncidentSourceError(
                f"{self.source!r} is not an incident source; add it to INCIDENT_SOURCES and to "
                "ck_incidents_source in a migration, or the database will refuse the row"
            )
        if self.severity not in SEVERITIES:
            raise IncidentSourceError(f"{self.severity!r} is not a severity")
        if not self.identity:
            raise IncidentSourceError(
                "an observation with no identity cannot be deduplicated, so every occurrence would file a "
                "new incident and a crash loop would bury the list"
            )
        if not self.title.strip():
            raise IncidentSourceError("an incident with no title is one nobody will read")

    @property
    def fingerprint(self) -> str:
        """Stable across occurrences, distinct across problems.

        The project and source are included so two projects with identically-named containers do not share
        an incident, and so the same identity from two different sources stays two incidents -- a container
        that exits non-zero and a log pattern naming it are different observations even when the cause is
        one.
        """
        material = "\x1f".join([str(self.project_id), self.source, *self.identity])
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:64]


@dataclass(frozen=True, slots=True)
class IngestResult:
    incident_id: uuid.UUID
    # True when this observation created the row rather than incrementing one. The caller uses it to decide
    # whether to notify: a 400th occurrence of a known crash loop should not page anyone again.
    created: bool
    occurrences: int


async def ingest(session: AsyncSession, observation: Observation) -> IngestResult:
    """Record an observation, deduplicating by fingerprint.

    ONE STATEMENT, not a select-then-insert. Two agents reporting the same crash-looping pod within the
    same second would both find no row and both insert, producing the duplicate this function exists to
    prevent -- and the window is exactly when duplicates are most likely, because that is when the source
    is firing fastest. `ON CONFLICT` makes the database arbitrate instead.
    """
    row = (
        await session.execute(
            text(
                """
                INSERT INTO incidents (
                    id, project_id, source, severity, title, fingerprint,
                    occurrences, origin_kind, origin_id, detail, detected_at, last_seen_at
                )
                VALUES (
                    :id, :project_id, :source, :severity, :title, :fingerprint,
                    1, :origin_kind, :origin_id, CAST(:detail AS jsonb), :now, :now
                )
                ON CONFLICT (fingerprint) WHERE resolved_at IS NULL DO UPDATE SET
                    occurrences = incidents.occurrences + 1,
                    last_seen_at = :now,
                    -- The severity can only RISE on a repeat. A warning that recurs as critical is now
                    -- critical; a critical that recurs as a warning is still critical, because the worst
                    -- thing this incident has ever been is what an operator needs to see.
                    severity = CASE
                        WHEN :severity = 'critical' THEN 'critical'
                        WHEN incidents.severity = 'critical' THEN 'critical'
                        WHEN :severity = 'warning' OR incidents.severity = 'warning' THEN 'warning'
                        ELSE incidents.severity
                    END
                RETURNING id, occurrences, (occurrences = 1) AS created
                """
            ),
            {
                "id": uuid.uuid4(),
                "project_id": observation.project_id,
                "source": observation.source,
                "severity": observation.severity,
                "title": observation.title[:512],
                "fingerprint": observation.fingerprint,
                "origin_kind": observation.origin_kind[:32],
                "origin_id": observation.origin_id[:128],
                "detail": _json(observation.detail),
                "now": datetime.now(UTC),
            },
        )
    ).one()
    return IngestResult(incident_id=row.id, created=bool(row.created), occurrences=int(row.occurrences))


def _json(value: dict[str, Any]) -> str:
    import json

    return json.dumps(value, default=str)


# --- the adapters, one per source ----------------------------------------------------------------------
#
# Each turns something that already happens into an Observation. They are functions rather than a class
# hierarchy because there is no shared behaviour -- only a shared output type.


def from_deployment_failure(
    *,
    project_id: uuid.UUID,
    deployment_id: uuid.UUID,
    environment: str,
    operation: str,
    reason: str,
) -> Observation:
    """A deployment whose command the agent RAN and which failed.

    Identity excludes the deployment id on purpose: the same operation failing for the same reason in the
    same environment is one recurring problem, and keying on the deployment would file a fresh incident per
    attempt -- so a redeploy loop would look like progress.
    """
    return Observation(
        project_id=project_id,
        source="deployment_failure",
        severity="critical",
        title=f"{operation} failed in {environment}: {reason[:200]}",
        identity=(environment, operation, reason[:120]),
        detail={"environment": environment, "operation": operation, "reason": reason},
        origin_kind="deployment",
        origin_id=str(deployment_id),
    )


def from_build_failure(*, project_id: uuid.UUID, run_id: uuid.UUID, findings: tuple[str, ...]) -> Observation:
    """A generation run whose artifacts the validation gate rejected.

    The identity is the SET of failing checks rather than the run, so twenty runs failing the same check
    are one incident saying so -- which is the signal worth having, because it says the prompt or the model
    cannot satisfy that check rather than that a run went wrong.
    """
    checks = tuple(sorted({finding.split(" ")[0].rstrip(":") for finding in findings if finding}))
    return Observation(
        project_id=project_id,
        source="build_failure",
        severity="warning",
        title=(
            f"generated artifacts rejected by {len(checks)} check(s): {', '.join(checks[:3])}"
            if checks
            else "generated artifacts rejected by the validation gate"
        ),
        identity=checks or ("unnamed-check",),
        detail={"findings": list(findings)},
        origin_kind="generation_run",
        origin_id=str(run_id),
    )


def from_kubernetes_event(
    *, project_id: uuid.UUID, namespace: str, kind: str, name: str, reason: str, message: str
) -> Observation:
    """A Warning event read through the agent's existing Kubernetes reads."""
    return Observation(
        project_id=project_id,
        source="kubernetes_event",
        severity="warning" if reason not in ("Failed", "BackOff", "CrashLoopBackOff") else "critical",
        title=f"{kind}/{name} in {namespace}: {reason}",
        identity=(namespace, kind, name, reason),
        detail={"namespace": namespace, "kind": kind, "name": name, "reason": reason, "message": message},
        origin_kind="kubernetes",
        origin_id=f"{namespace}/{kind}/{name}",
    )


def from_container_exit(*, project_id: uuid.UUID, container: str, exit_code: int, image: str) -> Observation:
    """A container that exited non-zero, from the Docker probe.

    Exit code 0 is REFUSED rather than filed as info: a container that exited cleanly is not an incident,
    and admitting it would fill the list with every completed job.
    """
    if exit_code == 0:
        raise IncidentSourceError(
            "exit code 0 is a clean exit and not an incident; filing it would fill the list with every "
            "completed job and bury the failures"
        )
    return Observation(
        project_id=project_id,
        source="container_exit",
        severity="critical" if exit_code in (137, 139) else "warning",
        title=f"container {container} exited {exit_code}",
        identity=(container, str(exit_code)),
        detail={"container": container, "exit_code": exit_code, "image": image},
        origin_kind="container",
        origin_id=container,
    )


def from_circuit_breaker(*, project_id: uuid.UUID, environment: str, failures: int) -> Observation:
    """The deployment breaker opening -- itself a signal, not just a consequence."""
    return Observation(
        project_id=project_id,
        source="circuit_breaker",
        severity="critical",
        title=f"deployment circuit breaker opened for {environment} after {failures} validation failures",
        identity=(environment, "breaker-open"),
        detail={"environment": environment, "failures": failures},
        origin_kind="environment",
        origin_id=environment,
    )


def from_log_pattern(*, project_id: uuid.UUID, service: str, pattern: str, sample: str, matches: int) -> Observation:
    """A pattern matched in the log store.

    The sample is TRUNCATED and the pattern is what identifies the incident. Keying on the sample would
    make every line with a different request id a new incident.
    """
    return Observation(
        project_id=project_id,
        source="log_pattern",
        severity="warning",
        title=f"{matches} log line(s) from {service} matched {pattern}",
        identity=(service, pattern),
        detail={"service": service, "pattern": pattern, "sample": sample[:2000], "matches": matches},
        origin_kind="service",
        origin_id=service,
    )


class IncidentRecorder(Protocol):
    """What a domain needs in order to file an incident, and nothing more.

    A Protocol rather than an import, for the reason every other cross-domain seam here is: `deployments`
    and `generation` must be able to file an incident without importing `incidents`, which
    `check-chokepoint` forbids and which would couple two domains that have no other business together.
    """

    async def record(self, observation: Observation) -> IngestResult: ...
