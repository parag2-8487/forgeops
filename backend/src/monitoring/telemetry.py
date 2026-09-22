# SPDX-License-Identifier: FSL-1.1-ALv2
"""OTel instrumentation with `gen_ai.*` semantic conventions. Phase 2 §2.10.

WHY THE SEMANTIC CONVENTIONS MATTER MORE THAN THE INSTRUMENTATION. Any code can emit a counter called
`llm_tokens`. `gen_ai.usage.input_tokens` is the name every Grafana dashboard, every OTel-aware backend and
every future tool already understands — so using the convention is the difference between telemetry that
works with the ecosystem and telemetry that needs a bespoke dashboard nobody maintains.

The names here follow OTel's GenAI semantic conventions:

  * `gen_ai.system`           — the provider (`ollama`, `openai`, ...)
  * `gen_ai.request.model`    — the model asked for
  * `gen_ai.response.model`   — the model that answered, which is NOT always the same and the difference is
                                the whole point of recording both: a routing cascade that fell back to a
                                standby model is invisible if only the request is recorded.
  * `gen_ai.usage.input_tokens` / `gen_ai.usage.output_tokens`
  * `gen_ai.cost.total`       — §2.10's per-tenant cost metric

WHAT IS DELIBERATELY NOT RECORDED: prompts and completions. OTel's conventions allow them as opt-in span
events, and this product will not: a prompt contains the operator's source code and a completion contains
generated credentials-shaped text, and both would then sit in a metrics store with a different retention
policy and a different access model from the database. `file_contents` is a redacted-only store for exactly
this reason, and telemetry must not become the unredacted copy.

COST IS ATTRIBUTED PER TENANT AND PER MODEL, which is what makes it actionable — a single total tells an
operator they spent money and nothing about where. `tenant_id` is a metric ATTRIBUTE, and that is a
cardinality decision made deliberately: tenants are few and long-lived, whereas `user_id` or `request_id`
would be unbounded and would make Prometheus's memory grow without limit. Attaching a high-cardinality label
to a metric is the classic way to take a monitoring system down with monitoring.

WHY A NO-OP IS THE DEFAULT. When no collector endpoint is configured, every function here is a cheap no-op
rather than an error: telemetry is not a runtime dependency, and a deployment without a collector must work
exactly as one with it. What it must NOT do is silently produce zeros that look like real measurements —
which is why the readiness report distinguishes "not configured" from "configured and unreachable".
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Final

#: OTel GenAI attribute names. Constants rather than inline strings, so a typo is a NameError here instead of
#: a metric nothing queries — a mis-typed attribute is invisible: it produces a second, empty series.
GEN_AI_SYSTEM: Final[str] = "gen_ai.system"
GEN_AI_REQUEST_MODEL: Final[str] = "gen_ai.request.model"
GEN_AI_RESPONSE_MODEL: Final[str] = "gen_ai.response.model"
GEN_AI_OPERATION_NAME: Final[str] = "gen_ai.operation.name"
GEN_AI_INPUT_TOKENS: Final[str] = "gen_ai.usage.input_tokens"
GEN_AI_OUTPUT_TOKENS: Final[str] = "gen_ai.usage.output_tokens"

#: §2.10's cost metric, named by the box.
GEN_AI_COST_TOTAL: Final[str] = "gen_ai.cost.total"

#: Attributes this product adds beyond the convention. Prefixed, because an unprefixed custom attribute may
#: collide with a future convention and then means two things in one series.
FORGEOPS_TENANT: Final[str] = "forgeops.tenant_id"
FORGEOPS_TIER: Final[str] = "forgeops.routing_tier"
FORGEOPS_SERVED_FROM: Final[str] = "forgeops.served_from"

#: Attribute names that must NEVER appear on a span or metric.
#:
#: Asserted by a test rather than left to review. A prompt in a metrics store is an unredacted copy of the
#: operator's source code sitting under a different retention policy and a different access model from the
#: database it was redacted for.
FORBIDDEN_ATTRIBUTES: Final[frozenset[str]] = frozenset(
    {
        "gen_ai.prompt",
        "gen_ai.completion",
        "gen_ai.content.prompt",
        "gen_ai.content.completion",
        "prompt",
        "completion",
        "messages",
    }
)


@dataclass(frozen=True)
class GenerationUsage:
    """What one generation consumed. The input to a cost metric.

    `response_model` IS SEPARATE FROM `request_model` and both are required. A routing cascade that fell
    back to a standby model is invisible when only the request is recorded, and "why did this cost more than
    I expected" is unanswerable without it.
    """

    system: str
    request_model: str
    response_model: str
    input_tokens: int
    output_tokens: int
    #: In whole currency units. A float, and the imprecision is accepted deliberately: a metric is an
    #: observation for a dashboard, not a ledger. Billing reads the database.
    cost_total: float
    tenant_id: uuid.UUID | None
    routing_tier: str
    served_from: str

    def attributes(self) -> dict[str, Any]:
        """The attribute map for a span or metric.

        `tenant_id` becomes the string `"none"` rather than being omitted when absent. An omitted label
        creates a SECOND SERIES in Prometheus, so a query summing by tenant would silently exclude every
        untenanted generation — and this deployment's tenants are deferred, so that is currently all of them.
        """
        return {
            GEN_AI_SYSTEM: self.system,
            GEN_AI_REQUEST_MODEL: self.request_model,
            GEN_AI_RESPONSE_MODEL: self.response_model,
            FORGEOPS_TENANT: str(self.tenant_id) if self.tenant_id else "none",
            FORGEOPS_TIER: self.routing_tier,
            FORGEOPS_SERVED_FROM: self.served_from,
        }


class Telemetry:
    """The instrumentation surface. A no-op when no collector is configured.

    ONE CLASS RATHER THAN MODULE-LEVEL FUNCTIONS, so a test can construct one against an in-memory exporter
    and read what was emitted. Module-level globals would make "what did this emit" answerable only by
    inspecting a process-wide provider, which two tests running in one session would then share.
    """

    def __init__(self, *, endpoint: str = "", service_name: str = "forgeops-backend") -> None:
        self._endpoint = endpoint.strip()
        self._service_name = service_name
        self._tracer: Any = None
        self._meter: Any = None
        # Kept so `instrument_process` can attach to THIS provider rather than the global one. The
        # global provider is set below, but passing it explicitly means an instrumentor attached before
        # that assignment cannot silently bind to a no-op default.
        self._meter_provider: Any = None
        self._cost_counter: Any = None
        self._input_tokens: Any = None
        self._output_tokens: Any = None
        self._generation_duration: Any = None
        if self._endpoint:
            self._compose()

    @property
    def enabled(self) -> bool:
        """True when a collector is configured.

        Read by the readiness report, so "not configured" and "configured and unreachable" are distinct
        answers. Collapsing them would make a broken collector look like a deliberate choice.
        """
        return bool(self._endpoint) and self._meter is not None

    def _compose(self) -> None:
        """Build the providers. Import-local, so a deployment without the SDK still runs."""
        try:
            from opentelemetry import metrics, trace
            from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
            from opentelemetry.sdk.metrics import MeterProvider
            from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor
        except ImportError:
            # NOT AN ERROR. The SDK being absent means telemetry is off, which is a supported configuration.
            # Raising here would make an optional dependency mandatory at startup.
            return

        resource = Resource.create({"service.name": self._service_name})

        tracer_provider = TracerProvider(resource=resource)
        tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=self._endpoint, insecure=True)))
        trace.set_tracer_provider(tracer_provider)
        self._tracer = trace.get_tracer(__name__)

        reader = PeriodicExportingMetricReader(
            OTLPMetricExporter(endpoint=self._endpoint, insecure=True),
            export_interval_millis=15_000,
        )
        meter_provider = MeterProvider(resource=resource, metric_readers=[reader])
        self._meter_provider = meter_provider
        metrics.set_meter_provider(meter_provider)
        self._meter = metrics.get_meter(__name__)

        self._cost_counter = self._meter.create_counter(
            GEN_AI_COST_TOTAL,
            unit="1",
            description="Cumulative generation cost, attributed per tenant and per model",
        )
        self._input_tokens = self._meter.create_counter(
            GEN_AI_INPUT_TOKENS, unit="1", description="Input tokens consumed"
        )
        self._output_tokens = self._meter.create_counter(
            GEN_AI_OUTPUT_TOKENS, unit="1", description="Output tokens produced"
        )
        self._generation_duration = self._meter.create_histogram(
            "gen_ai.client.operation.duration",
            unit="s",
            description="Wall-clock duration of one generation",
        )

    def record_generation(self, usage: GenerationUsage) -> None:
        """Record one generation's tokens and cost.

        A NEGATIVE OR ABSURD COST IS REFUSED rather than recorded. A counter is monotonic: OTel and
        Prometheus both treat a negative increment as undefined, and Prometheus's `rate()` reads a counter
        that went down as a RESET — so one bad value makes every cost query for that series wrong from that
        point on, not just at that point.
        """
        if usage.cost_total < 0:
            raise ValueError(
                f"refusing to record a negative generation cost ({usage.cost_total}): a counter is "
                "monotonic, and Prometheus reads a decrease as a counter reset, which corrupts every "
                "later rate() over that series rather than just this sample"
            )
        if usage.input_tokens < 0 or usage.output_tokens < 0:
            raise ValueError("refusing to record negative token counts for the same reason")
        if not self.enabled:
            return

        attributes = usage.attributes()
        _assert_no_forbidden(attributes)
        self._cost_counter.add(usage.cost_total, attributes)
        self._input_tokens.add(usage.input_tokens, attributes)
        self._output_tokens.add(usage.output_tokens, attributes)

    @contextmanager
    def generation_span(self, *, operation: str, system: str, request_model: str) -> Iterator[Any]:
        """A span around one generation, named by OTel's convention.

        `gen_ai.operation.name` plus the model, which is the span name OTel's conventions prescribe. A
        bespoke name would be correct and invisible to every dashboard that already knows this one.
        """
        if not self.enabled or self._tracer is None:
            yield None
            return
        with self._tracer.start_as_current_span(f"{operation} {request_model}") as span:
            span.set_attribute(GEN_AI_OPERATION_NAME, operation)
            span.set_attribute(GEN_AI_SYSTEM, system)
            span.set_attribute(GEN_AI_REQUEST_MODEL, request_model)
            yield span

    def instrument_app(self, app: Any) -> None:
        """Attach FastAPI and SQLAlchemy instrumentation.

        The health and readiness endpoints are EXCLUDED. A liveness probe every second would otherwise be
        the overwhelming majority of every trace sample and every request-rate panel, which makes both
        useless — the signal an operator wants is what their users did.
        """
        if not self.enabled:
            return
        try:
            from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        except ImportError:
            return
        FastAPIInstrumentor.instrument_app(app, excluded_urls="health,health/ready,metrics")

    def instrument_process(self) -> bool:
        """Emit this process's own CPU and memory as OTel metrics. Returns whether it was attached.

        2.4's resource-utilisation box needs a SECOND source for a quantity the Docker probe already
        reports, and this is the honest one. The alternative was giving the collector the Docker socket
        so it could read container stats, which was rejected: mounting that socket `:ro` restricts
        writes to the socket FILE and not to the Docker API through it, so the collector would gain
        container-create -- the bind-mount-and-privileged authority this whole catalogue avoids -- in
        order to draw a chart.

        What this measures is deliberately NOT the same thing the Docker probe measures. This is the
        Python process; the probe reports the container, which includes everything else in it. Reading
        them as one number is the misreading 2.4's box exists to prevent, so the panel labels each by
        its source and its method rather than reconciling them.
        """
        if not self.enabled:
            return False
        try:
            from opentelemetry.instrumentation.system_metrics import SystemMetricsInstrumentor
        except ImportError:
            return False
        # An explicit, NARROW configuration rather than the default. The default set includes per-CPU
        # time buckets and per-interface network counters, which on a many-core host multiplies series
        # into the thousands -- high cardinality is how a monitoring system takes down what it monitors.
        SystemMetricsInstrumentor(
            config={
                "process.runtime.cpu.utilization": None,
                "process.runtime.memory": ["rss"],
                # `gc_count` is deliberately NOT collected: it emits one series per GC generation
                # and answers no question 2.4's box asks, which is cardinality bought for nothing.
            }
        ).instrument(meter_provider=self._meter_provider)
        return True


def _assert_no_forbidden(attributes: dict[str, Any]) -> None:
    """Refuse an attribute map holding a prompt or a completion.

    A PROGRAMMING ERROR, so it raises: the only way here is for somebody to add a field, and the right
    moment to find out is the first test run after they do. This is the same shape as
    `_assert_no_credential` in the chokepoint, and for the same reason — the thing being protected is what
    gets written somewhere it cannot be unwritten from.
    """
    offending = sorted(set(attributes) & FORBIDDEN_ATTRIBUTES)
    if offending:
        raise ValueError(
            f"refusing to emit telemetry attributes {', '.join(offending)}: a prompt or completion in a "
            "metrics store is an unredacted copy of the operator's source code under a different retention "
            "policy and a different access model from the database it was redacted for"
        )
