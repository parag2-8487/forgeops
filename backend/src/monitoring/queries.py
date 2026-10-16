"""The enumerated PromQL catalogue -- Phase 2 §2.10's read surface.

WHY THERE IS NO RAW PROMQL PARAMETER ANYWHERE IN THIS FILE.

The obvious read route accepts a query string and forwards it. It cannot be made safe. A PromQL string can
select any series in the store by many independent routes -- `{__name__=~".+"}`, a subquery, `label_replace`
rewriting a tenant label after the matcher has been checked, `group_left` joining a series the caller was
never entitled to, or simply a metric name the sanitiser did not know about. Every one of those defeats a
filter that inspects the string, and the filter cannot be complete because PromQL is a language and the
attack surface is its whole grammar. A allowlist of *substrings* is the losing shape of this problem.

So the client names a query and supplies parameters. The PromQL is written here, in the repository, reviewed
once. This is the same decision as `devtools.run` taking a KIND rather than a command line, and for the same
reason: a fixed operation set is the only version of this that is safe by construction rather than by the
diligence of a filter.

WHAT A CALLER CAN STILL INFLUENCE, and why each is bounded:

  * which catalogue entry runs -- enumerated, and an unknown name is a 422 rather than a passthrough
  * label values, only for parameters the entry declares, each validated against a strict pattern
  * the time window, only from an enumerated set of durations

THE TENANT MATCHER IS NOT A PARAMETER. It is composed by the server from the verified principal and spliced
into a `{tenant_matcher}` slot the template must contain. A tenant-scoped template that lacks that slot is
refused AT IMPORT, not at request time -- the whole catalogue fails to load. This is the parsed-document
lesson from the ArgoCD manifests: a missing safety element that merely renders as absent is one nobody
notices, so the absence has to be an error at the earliest possible moment.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Final, Literal

# The slot every tenant-scoped template must contain. Naming it once means the assertion below and the
# splice below cannot drift apart.
TENANT_SLOT: Final = "{tenant_matcher}"

# Label values are the only free text that reaches PromQL, and this is deliberately narrower than what
# Prometheus permits: alphanumerics, and the four punctuation characters that appear in real metric labels
# (`_` in label names, `.` and `-` in service names, `:` in recorded-series names). No braces, no quotes, no
# backslash, no comma, no parenthesis -- so a value cannot close its own matcher and open another. A UUID,
# a route path segment and a model name all pass; `foo",job="bar` does not.
_LABEL_VALUE: Final = re.compile(r"^[A-Za-z0-9_.:/-]{1,128}$")

# Durations come from a fixed set rather than a pattern. A pattern would admit `999999d`, which is a cheap
# way to make the metrics store do unbounded work on behalf of an authenticated but unprivileged caller.
ALLOWED_WINDOWS: Final = ("5m", "15m", "1h", "6h", "24h", "7d", "30d")

QueryKind = Literal["instant", "range"]


class CatalogueError(Exception):
    """The catalogue itself is wrong. Raised at import, never per-request."""


@dataclass(frozen=True, slots=True)
class CatalogueEntry:
    """One reviewed query. Frozen, so a handler cannot edit the PromQL it is about to run."""

    name: str
    kind: QueryKind
    # The PromQL, with `{tenant_matcher}` and any declared parameter slots.
    template: str
    # Parameter names the caller may supply. Anything else in the request is a 422 -- an ignored unknown
    # parameter would let a caller believe they had filtered when they had not.
    parameters: tuple[str, ...] = ()
    # False ONLY for series that carry no tenant dimension at all: collector internals, host metrics, the
    # store's own health. Each such entry states why, because "this one does not need scoping" is exactly
    # the sentence that introduces a leak.
    tenant_scoped: bool = True
    unscoped_reason: str = ""
    # Shown by the panel so a human reads a sentence rather than inferring meaning from a series name.
    describes: str = ""

    def __post_init__(self) -> None:
        if self.tenant_scoped:
            if TENANT_SLOT not in self.template:
                raise CatalogueError(
                    f"catalogue entry {self.name!r} is tenant-scoped but its template has no "
                    f"{TENANT_SLOT} slot; it would return every tenant's series"
                )
        else:
            if TENANT_SLOT in self.template:
                raise CatalogueError(f"catalogue entry {self.name!r} is not tenant-scoped yet has a {TENANT_SLOT} slot")
            if not self.unscoped_reason:
                raise CatalogueError(
                    f"catalogue entry {self.name!r} opts out of tenant scoping without a stated reason"
                )
        for parameter in self.parameters:
            if "{" + parameter + "}" not in self.template:
                raise CatalogueError(
                    f"catalogue entry {self.name!r} declares parameter {parameter!r} that its template "
                    "never uses; a caller would believe they had filtered when they had not"
                )

    def render(self, *, tenant_id: uuid.UUID | None, arguments: dict[str, str]) -> str:
        """Compose the final PromQL. The only place a query string is built.

        Raises `ValueError` on anything unexpected rather than dropping it: an ignored argument is a silent
        widening, and the caller cannot tell the difference from the response.
        """
        unknown = set(arguments) - set(self.parameters)
        if unknown:
            raise ValueError(f"query {self.name!r} does not accept {sorted(unknown)!r}")
        missing = set(self.parameters) - set(arguments)
        if missing:
            raise ValueError(f"query {self.name!r} requires {sorted(missing)!r}")

        slots: dict[str, str] = {}
        for key, value in arguments.items():
            if not _LABEL_VALUE.match(value):
                raise ValueError(f"value for {key!r} contains characters that are not permitted in a label value")
            slots[key] = value

        if self.tenant_scoped:
            slots["tenant_matcher"] = _tenant_matcher(tenant_id)

        return self.template.format(**slots)


def _tenant_matcher(tenant_id: uuid.UUID | None) -> str:
    """The matcher spliced into every tenant-scoped query.

    An absent tenant becomes `forgeops_tenant_id="none"` and NOT an empty matcher. An empty matcher would
    select every series -- so the one principal most likely to lack a tenant (a single-tenant deployment's
    operator, which with tenancy deferred is every principal) would be the one that saw everything. The
    telemetry layer writes the literal label `"none"` for the same reason, so the two agree.
    """
    if tenant_id is None:
        return 'forgeops_tenant_id="none"'
    # `str(uuid)` cannot contain a quote or a brace, so this cannot break out of the matcher. Asserted
    # rather than assumed, because the day this becomes a string from elsewhere is the day it matters.
    rendered = str(tenant_id)
    if not _LABEL_VALUE.match(rendered):
        raise ValueError("tenant id is not label-safe")
    return f'forgeops_tenant_id="{rendered}"'


# --- the catalogue -------------------------------------------------------------------------------------
#
# Every entry below is a query some panel in §2.10 needs. Adding a panel means adding an entry here and
# having it reviewed, which is the intended friction.

_ENTRIES: tuple[CatalogueEntry, ...] = (
    # --- AI cost, §2.10's cost dashboard ---------------------------------------------------------------
    CatalogueEntry(
        name="ai_cost_by_model",
        kind="instant",
        # Reads the RECORDING RULE rather than computing `increase()` live: the raw form recomputes the
        # whole range on every dashboard refresh, at which point the dashboard is the load.
        template="sum by (gen_ai_response_model) (tenant_model:gen_ai_cost:increase24h{{{tenant_matcher}}})",
        describes="Spend per model over the last 24 hours, in the provider's currency units.",
    ),
    CatalogueEntry(
        name="ai_cost_total",
        kind="instant",
        template="sum (tenant_model:gen_ai_cost:increase24h{{{tenant_matcher}}})",
        describes="Total spend over the last 24 hours.",
    ),
    CatalogueEntry(
        name="ai_cost_series",
        kind="range",
        template="sum by (gen_ai_response_model) (tenant_model:gen_ai_cost:increase1h{{{tenant_matcher}}})",
        describes="Spend per model over time, one point per hour of accrual.",
    ),
    CatalogueEntry(
        name="ai_tokens_by_direction",
        kind="instant",
        # Input and output are kept SEPARATE because they price differently on every provider, so a
        # combined total cannot be converted back into money.
        template=(
            "label_replace(sum (tenant_model:gen_ai_input_tokens:increase24h"
            '{{{tenant_matcher}}}), "direction", "input", "", "")'
            " or "
            "label_replace(sum (tenant_model:gen_ai_output_tokens:increase24h"
            '{{{tenant_matcher}}}), "direction", "output", "", "")'
        ),
        describes="Tokens over 24 hours, split by direction because the two price differently.",
    ),
    CatalogueEntry(
        name="ai_requests_by_outcome",
        kind="instant",
        template=(
            "sum by (forgeops_served_from) (increase(gen_ai_client_operation_duration_milliseconds_count"
            "{{{tenant_matcher}}}[24h]))"
        ),
        describes="Generations over 24 hours by where the answer came from: a provider, or a cache tier.",
    ),
    # --- application metrics, §2.10's request-rate/latency/errors panel --------------------------------
    CatalogueEntry(
        name="http_request_rate",
        kind="range",
        # NOT tenant-scoped, and this is the one place that needs its reason spelled out: HTTP server
        # metrics are emitted by the FastAPI instrumentation, which has no tenant in scope at the point a
        # request is measured -- the principal is resolved inside the handler, after the timer starts.
        # Adding a tenant label here would mean either a wrong label or a middleware that resolves a
        # principal twice. The series carries no tenant dimension, so there is nothing to leak between
        # tenants; what it does expose is deployment-wide traffic shape, which is why the route requires an
        # operator role for unscoped entries.
        template="sum by (http_route) (rate(http_server_duration_milliseconds_count[{window}]))",
        parameters=("window",),
        tenant_scoped=False,
        unscoped_reason=(
            "HTTP server metrics are recorded by instrumentation that has no tenant in scope: the timer "
            "starts before the principal is resolved. The series has no tenant dimension to scope by, so "
            "the route requires an operator role instead."
        ),
        describes="Requests per second per route.",
    ),
    CatalogueEntry(
        name="http_error_ratio",
        kind="range",
        # Reads the recorded ratio, which deliberately does not use `or vector(0)`: an absent series must
        # reach the panel as absent, so it can say "no data" rather than "no errors".
        template="route:http_errors:ratio5m",
        tenant_scoped=False,
        unscoped_reason="Derived from the same untenanted HTTP server metrics as http_request_rate.",
        describes="Fraction of requests answered 5xx. Absent where there was no traffic to divide by.",
    ),
    CatalogueEntry(
        name="http_latency_p95",
        kind="range",
        template="route:http_latency_p95:5m",
        tenant_scoped=False,
        unscoped_reason="Derived from the same untenanted HTTP server metrics as http_request_rate.",
        describes="95th percentile request duration in milliseconds, per route.",
    ),
    # --- infrastructure health, §2.10's overview panel -------------------------------------------------
    CatalogueEntry(
        name="collector_accepted_spans",
        kind="range",
        template="sum by (job) (rate(otelcol_receiver_accepted_spans_total[{window}]))",
        parameters=("window",),
        tenant_scoped=False,
        unscoped_reason="A collector's own throughput is a property of the deployment, not of a tenant.",
        describes="Spans per second entering each collector tier.",
    ),
    CatalogueEntry(
        name="collector_refused_spans",
        kind="range",
        # The signal that matters most on this panel. A collector refusing data is working as designed --
        # `memory_limiter` shedding load rather than being OOM-killed -- but it means the traces an operator
        # is about to look at are incomplete, and that must be visible rather than inferred from a gap.
        template="sum by (job) (rate(otelcol_processor_refused_spans_total[{window}]))",
        parameters=("window",),
        tenant_scoped=False,
        unscoped_reason="A collector's own shedding is a property of the deployment, not of a tenant.",
        describes="Spans per second being shed. Non-zero means the traces you are reading are incomplete.",
    ),
    CatalogueEntry(
        name="collector_queue_size",
        kind="range",
        template="max by (job) (otelcol_exporter_queue_size)",
        parameters=(),
        tenant_scoped=False,
        unscoped_reason="A collector's export queue is a property of the deployment, not of a tenant.",
        describes="Telemetry waiting to be exported. A queue that only grows means the store is unreachable.",
    ),
    # --- 2.4's resource-utilisation box: the metrics-tier half ----------------------------------------
    #
    # These measure THE APPLICATION PROCESS, which is not what the Docker probe measures even though both
    # are called CPU and memory: the probe reports a container, this reports one process inside it. The
    # panel labels each by source and method rather than reconciling them, because presenting two
    # differently-obtained numbers as one quantity is how a stale reading gets read as a live one.
    CatalogueEntry(
        name="process_cpu_utilisation",
        kind="range",
        # THE NAME CARRIES THE RUNTIME AND THE UNIT, and both were got wrong on the first attempt:
        # the SDK interpolates the implementation (`cpython`) into the metric name and the collector
        # appends the unit (`_ratio`), so the obvious `process_runtime_cpu_utilization` matches
        # nothing. Found by reading the collector's own /metrics output rather than by guessing --
        # a wrong name here returns no series, which the panel would have honestly reported as
        # `never_reported`, so this would have shipped as a permanently empty chart.
        template="max by (service_name) (process_runtime_cpython_cpu_utilization_ratio)",
        tenant_scoped=False,
        unscoped_reason=(
            "A process's own CPU is a property of the deployment's runtime, not of any tenant; work for "
            "every tenant runs in the same process, so there is no dimension to scope by."
        ),
        describes="Fraction of a core this application process is using, as the runtime reports it.",
    ),
    CatalogueEntry(
        name="process_memory_rss",
        kind="range",
        template="max by (service_name) (process_runtime_cpython_memory_bytes)",
        tenant_scoped=False,
        unscoped_reason="Process memory is shared across every tenant served by this process.",
        describes="Resident memory of this application process, in bytes.",
    ),
)


CATALOGUE: Final[dict[str, CatalogueEntry]] = {entry.name: entry for entry in _ENTRIES}

if len(CATALOGUE) != len(_ENTRIES):  # pragma: no cover - import-time structural check
    raise CatalogueError("two catalogue entries share a name; one would shadow the other")


def entry_for(name: str) -> CatalogueEntry:
    """Look a query up, raising `KeyError` for anything not in the catalogue."""
    return CATALOGUE[name]


def catalogue_listing() -> list[dict[str, object]]:
    """What the frontend reads to know which queries exist, so panel names are not duplicated there."""
    return [
        {
            "name": entry.name,
            "kind": entry.kind,
            "parameters": list(entry.parameters),
            "tenant_scoped": entry.tenant_scoped,
            "describes": entry.describes,
        }
        for entry in _ENTRIES
    ]
