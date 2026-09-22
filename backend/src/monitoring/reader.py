"""Reading the metrics store -- Phase 2 §2.10.

THE FRESHNESS VERDICT IS COMPUTED HERE, NOT IN THE PANEL. Three states a panel must be able to tell apart in
words, and the distinction is only available at this layer:

  * UNCONFIGURED  -- no Prometheus URL is set. Monitoring is optional in this product, so this is a normal
                     state and not an error, but a panel must not render it as zero.
  * UNREACHABLE   -- configured and did not answer. The last known value, if any, is meaningless now.
  * NEVER_REPORTED-- the store answered and holds no series at all for that query. Distinct from unreachable:
                     one means "we cannot see", the other "there is genuinely nothing", and the operator
                     response differs completely.
  * STALE         -- the store answered with a series whose newest sample is older than the staleness
                     threshold. The number is real but describes the past; an exporter that died an hour ago
                     leaves a perfectly plausible value sitting there forever.
  * FRESH         -- answered, has a series, and it is recent.

The reason this is not left to the frontend: a panel receiving `{"value": 0}` cannot distinguish any of these,
and the plausible-number-when-the-source-is-down failure is this codebase's recurring defect. Making the
verdict a required field of the response means a panel CANNOT render a number without also having been told
how much to trust it.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

from .queries import ALLOWED_WINDOWS, CatalogueEntry

Verdict = Literal["unconfigured", "unreachable", "never_reported", "stale", "fresh"]

# A sample older than this is described as stale. Four scrape intervals: long enough that one missed scrape
# is not an alarm, short enough that a dead exporter is visible within a minute.
STALENESS_SECONDS = 60.0


@dataclass(frozen=True, slots=True)
class SeriesPoint:
    at: float
    value: float | None  # None where Prometheus returned NaN -- see `_parse_sample`.


@dataclass(frozen=True, slots=True)
class Series:
    labels: dict[str, str]
    points: tuple[SeriesPoint, ...]


@dataclass(frozen=True, slots=True)
class QueryResult:
    """A query's answer together with how much of it to believe."""

    query: str
    verdict: Verdict
    series: tuple[Series, ...] = ()
    # Plain prose, rendered by the panel as-is. Composed here so every panel says the same thing about the
    # same condition rather than each inventing its own wording.
    explanation: str = ""
    newest_sample_age_seconds: float | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def has_numbers(self) -> bool:
        """Whether a panel may render a figure at all."""
        return self.verdict in ("stale", "fresh") and bool(self.series)


def _parse_sample(raw: Any) -> SeriesPoint:
    """One `[timestamp, "value"]` pair.

    A NaN becomes `None` rather than 0.0. Prometheus returns NaN for a division by an absent denominator --
    the error-ratio query over a route with no traffic is exactly this -- and 0.0 would tell an operator
    there were no errors on a route nobody called. `float("nan")` also survives JSON serialisation as
    `NaN`, which is not valid JSON and breaks the frontend's parser at a point far from the cause.
    """
    at, value = raw[0], raw[1]
    if value in ("NaN", "nan", "+Inf", "-Inf"):
        return SeriesPoint(at=float(at), value=None)
    return SeriesPoint(at=float(at), value=float(value))


class MetricsReader:
    """Reads Prometheus. Constructed with an empty URL when monitoring is not configured."""

    def __init__(self, *, base_url: str, timeout_seconds: float = 5.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds

    @property
    def configured(self) -> bool:
        return bool(self._base_url)

    async def run(
        self,
        entry: CatalogueEntry,
        *,
        tenant_id: uuid.UUID | None,
        arguments: dict[str, str],
        window: str = "1h",
    ) -> QueryResult:
        """Render the catalogue entry and execute it.

        `ValueError` from `render` is allowed to propagate: the route turns it into a 422, and swallowing it
        here would answer a rejected query with an empty result that looks like "no data".
        """
        if window not in ALLOWED_WINDOWS:
            raise ValueError(f"window must be one of {list(ALLOWED_WINDOWS)}")

        promql = entry.render(tenant_id=tenant_id, arguments=arguments)

        if not self.configured:
            return QueryResult(
                query=promql,
                verdict="unconfigured",
                explanation=(
                    "No metrics store is configured for this deployment, so nothing has been measured. "
                    "This is not a failure: monitoring is optional here. It does mean the figures on this "
                    "panel are unavailable rather than zero."
                ),
            )

        try:
            payload = await self._fetch(entry, promql, window)
        except (httpx.HTTPError, ValueError) as exc:
            return QueryResult(
                query=promql,
                verdict="unreachable",
                explanation=(
                    "The metrics store did not answer, so the current value of this figure is unknown. "
                    "Anything previously shown here describes an earlier moment and may no longer hold."
                ),
                detail={"error": type(exc).__name__},
            )

        series = _series_from_payload(payload)
        if not series:
            return QueryResult(
                query=promql,
                verdict="never_reported",
                explanation=(
                    "The metrics store answered and holds no data for this query. Nothing has ever "
                    "reported it -- which is different from the value being zero, and different again "
                    "from the store being unreachable."
                ),
            )

        newest = max(
            (point.at for one in series for point in one.points),
            default=None,
        )
        age = None if newest is None else max(0.0, time.time() - newest)
        if age is not None and age > STALENESS_SECONDS:
            return QueryResult(
                query=promql,
                verdict="stale",
                series=series,
                newest_sample_age_seconds=age,
                explanation=(
                    f"The newest sample is {int(age)} seconds old, past the {int(STALENESS_SECONDS)}-second "
                    "freshness threshold. The figure is real but describes the past: whatever reports it "
                    "has most likely stopped."
                ),
            )

        return QueryResult(
            query=promql,
            verdict="fresh",
            series=series,
            newest_sample_age_seconds=age,
            explanation="Reported within the last minute.",
        )

    async def _fetch(self, entry: CatalogueEntry, promql: str, window: str) -> dict[str, Any]:
        now = time.time()
        if entry.kind == "instant":
            path, params = "/api/v1/query", {"query": promql, "time": f"{now:.3f}"}
        else:
            seconds = _window_seconds(window)
            path, params = (
                "/api/v1/query_range",
                {
                    "query": promql,
                    "start": f"{now - seconds:.3f}",
                    "end": f"{now:.3f}",
                    # Roughly 120 points whatever the window, so a 30-day range does not return a quarter
                    # of a million samples the browser then has to render.
                    "step": f"{max(15, int(seconds // 120))}s",
                },
            )

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            # POST, not GET: a long recorded-rule query exceeds some proxies' URL length limits, and the
            # failure mode is a 414 that looks like a routing problem.
            response = await client.post(f"{self._base_url}{path}", data=params)
        response.raise_for_status()
        payload = response.json()
        if payload.get("status") != "success":
            raise ValueError(f"metrics store returned status {payload.get('status')!r}")
        return payload


def _window_seconds(window: str) -> int:
    unit = window[-1]
    amount = int(window[:-1])
    return amount * {"m": 60, "h": 3600, "d": 86400}[unit]


def _series_from_payload(payload: dict[str, Any]) -> tuple[Series, ...]:
    data = payload.get("data", {})
    out: list[Series] = []
    for raw in data.get("result", []):
        labels = {str(k): str(v) for k, v in raw.get("metric", {}).items()}
        if "value" in raw:
            points = (_parse_sample(raw["value"]),)
        else:
            points = tuple(_parse_sample(sample) for sample in raw.get("values", []))
        out.append(Series(labels=labels, points=points))
    return tuple(out)
