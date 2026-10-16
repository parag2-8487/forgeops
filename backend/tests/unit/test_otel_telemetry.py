# SPDX-License-Identifier: FSL-1.1-ALv2
"""OTel instrumentation and the two-tier collector configuration. Phase 2 §2.10.

TWO KINDS OF ASSERTION HERE.

**The instrumentation** is exercised against the SDK's in-memory readers, so what is asserted is what would
reach a collector — attribute names, counter values, the refusal of a negative cost. A test that checked my
own wrapper called my own double would establish nothing about the metric a dashboard queries.

**The collector configuration** is parsed as YAML and its load-bearing decisions asserted: that the agent
does not sample, that the gateway's tail policies are or-ed so a 10% probabilistic policy beside a 100% error
policy yields "all errors plus a tenth of the rest", and that metrics and logs are never sampled. A sampled
counter is a wrong number, not a cheaper one, and prose in a comment does not stop somebody adding a
processor.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
import yaml
from src.monitoring.telemetry import (
    FORBIDDEN_ATTRIBUTES,
    FORGEOPS_SERVED_FROM,
    FORGEOPS_TENANT,
    FORGEOPS_TIER,
    GEN_AI_COST_TOTAL,
    GEN_AI_REQUEST_MODEL,
    GEN_AI_RESPONSE_MODEL,
    GEN_AI_SYSTEM,
    GenerationUsage,
    Telemetry,
    _assert_no_forbidden,
)

INFRA = Path(__file__).resolve().parents[2].parent / "infra" / "observability"


def _usage(**overrides: Any) -> GenerationUsage:
    base = {
        "system": "ollama",
        "request_model": "qwen3-coder-next",
        "response_model": "qwen3-coder-standby",
        "input_tokens": 1200,
        "output_tokens": 340,
        "cost_total": 0.0042,
        "tenant_id": None,
        "routing_tier": "self_hosted",
        "served_from": "provider",
    }
    base.update(overrides)
    return GenerationUsage(**base)  # type: ignore[arg-type]


class TestTheConventionNamesAreUsed:
    def test_the_attribute_map_uses_otel_gen_ai_names(self) -> None:
        """The convention is the point.

        Any code can emit `llm_tokens`. `gen_ai.usage.input_tokens` is what every Grafana dashboard and every
        OTel-aware backend already understands, so the convention is the difference between telemetry that
        works with the ecosystem and telemetry needing a bespoke dashboard nobody maintains.
        """
        attributes = _usage().attributes()
        assert attributes[GEN_AI_SYSTEM] == "ollama"
        assert attributes[GEN_AI_REQUEST_MODEL] == "qwen3-coder-next"
        # BOTH MODELS. A routing cascade that fell back to a standby is invisible when only the request is
        # recorded, and "why did this cost more than I expected" is then unanswerable.
        assert attributes[GEN_AI_RESPONSE_MODEL] == "qwen3-coder-standby"
        assert attributes[FORGEOPS_TIER] == "self_hosted"
        assert attributes[FORGEOPS_SERVED_FROM] == "provider"

    def test_an_absent_tenant_becomes_a_label_rather_than_being_omitted(self) -> None:
        """An omitted label creates a SECOND SERIES in Prometheus.

        A query summing cost by tenant would then silently exclude every untenanted generation — and tenants
        are deferred in this deployment, so that is currently all of them.
        """
        assert _usage(tenant_id=None).attributes()[FORGEOPS_TENANT] == "none"
        tenant = uuid.uuid4()
        assert _usage(tenant_id=tenant).attributes()[FORGEOPS_TENANT] == str(tenant)

    def test_a_prompt_or_completion_attribute_is_refused(self) -> None:
        """A prompt in a metrics store is an unredacted copy of the operator's source code.

        It would sit under a different retention policy and a different access model from the database it was
        redacted for. `file_contents` is a redacted-only store for exactly this reason.
        """
        for forbidden in sorted(FORBIDDEN_ATTRIBUTES):
            with pytest.raises(ValueError, match="refusing to emit"):
                _assert_no_forbidden({forbidden: "some text"})
        # And the legitimate map passes, so the check is not simply refusing everything.
        _assert_no_forbidden(_usage().attributes())


class TestTheCostCounterIsMonotonic:
    def test_a_negative_cost_is_refused(self) -> None:
        """One bad value corrupts every later query over that series, not just this sample.

        Prometheus reads a counter that went down as a RESET, so `rate()` over the whole window afterwards is
        wrong. That is why this raises rather than clamping to zero: clamping hides a bug that is producing
        wrong numbers somewhere upstream.
        """
        telemetry = Telemetry()
        with pytest.raises(ValueError, match="counter reset"):
            telemetry.record_generation(_usage(cost_total=-0.01))
        with pytest.raises(ValueError, match="negative token counts"):
            telemetry.record_generation(_usage(input_tokens=-1))

    def test_a_disabled_telemetry_records_nothing_and_raises_nothing(self) -> None:
        """Telemetry is not a runtime dependency.

        A deployment with no collector must behave exactly as one with it, so every call is a cheap no-op —
        and `enabled` stays False so the readiness report can distinguish "not configured" from "configured
        and unreachable".
        """
        telemetry = Telemetry()
        assert telemetry.enabled is False
        telemetry.record_generation(_usage())
        with telemetry.generation_span(operation="chat", system="ollama", request_model="m") as span:
            assert span is None

    def test_the_cost_and_token_counters_reach_a_reader_with_their_attributes(self) -> None:
        """WHAT WOULD REACH A COLLECTOR, read back from the SDK's own in-memory reader.

        This is the assertion that the metric a dashboard queries actually carries the tenant and model
        labels — a wrapper test would only prove this code calls itself.
        """
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import InMemoryMetricReader

        reader = InMemoryMetricReader()
        provider = MeterProvider(metric_readers=[reader])
        meter = provider.get_meter("test")

        telemetry = Telemetry()
        # The provider is substituted rather than the code under test: `record_generation`'s arithmetic, its
        # attribute map and its refusals are all the production ones.
        telemetry._endpoint = "in-memory"  # noqa: SLF001
        telemetry._meter = meter  # noqa: SLF001
        telemetry._cost_counter = meter.create_counter(GEN_AI_COST_TOTAL)  # noqa: SLF001
        telemetry._input_tokens = meter.create_counter("gen_ai.usage.input_tokens")  # noqa: SLF001
        telemetry._output_tokens = meter.create_counter("gen_ai.usage.output_tokens")  # noqa: SLF001

        tenant = uuid.uuid4()
        telemetry.record_generation(_usage(tenant_id=tenant, cost_total=0.25))
        telemetry.record_generation(_usage(tenant_id=tenant, cost_total=0.75))

        data = reader.get_metrics_data()
        points: dict[str, list[Any]] = {}
        for resource_metric in data.resource_metrics:
            for scope_metric in resource_metric.scope_metrics:
                for metric in scope_metric.metrics:
                    points.setdefault(metric.name, []).extend(metric.data.data_points)

        assert GEN_AI_COST_TOTAL in points, f"the cost metric was not emitted; got {sorted(points)}"
        cost_points = points[GEN_AI_COST_TOTAL]
        # CUMULATIVE, so two generations sum. A counter that replaced rather than added would show only the
        # most recent generation's cost and a bill would look a hundredth of its size.
        assert sum(point.value for point in cost_points) == pytest.approx(1.0)
        assert cost_points[0].attributes[FORGEOPS_TENANT] == str(tenant)
        assert cost_points[0].attributes[GEN_AI_RESPONSE_MODEL] == "qwen3-coder-standby"

        assert sum(point.value for point in points["gen_ai.usage.input_tokens"]) == 2400


class TestTheTwoTierCollectorConfiguration:
    def test_both_tiers_exist_and_parse(self) -> None:
        for name in ("otel-agent.yaml", "otel-gateway.yaml"):
            path = INFRA / name
            assert path.exists(), f"{name} is missing, so the two-tier architecture is one tier"
            assert isinstance(yaml.safe_load(path.read_text(encoding="utf-8")), dict)

    def test_the_agent_does_not_sample(self) -> None:
        """Tail sampling needs every span of a trace in one place, and a trace crosses hosts.

        An agent sampling locally would decide on fragments, and the case it would most often get wrong is
        the one that matters: an error span whose siblings were dropped looks like a complete, uneventful
        trace.
        """
        config = yaml.safe_load((INFRA / "otel-agent.yaml").read_text(encoding="utf-8"))
        assert "tail_sampling" not in config["processors"]
        assert "probabilistic_sampler" not in config["processors"]
        for pipeline in config["service"]["pipelines"].values():
            for processor in pipeline["processors"]:
                assert "sampl" not in processor, f"the agent tier samples via {processor}"

    def test_the_agent_listens_only_on_localhost(self) -> None:
        """An agent on 0.0.0.0 accepts telemetry from anything that can reach the host — an injection route
        into an operator's own dashboards and a denial-of-service surface."""
        config = yaml.safe_load((INFRA / "otel-agent.yaml").read_text(encoding="utf-8"))
        protocols = config["receivers"]["otlp"]["protocols"]
        assert protocols["grpc"]["endpoint"].startswith("127.0.0.1")
        assert protocols["http"]["endpoint"].startswith("127.0.0.1")

    def test_the_gateway_keeps_every_error_and_every_slow_trace(self) -> None:
        """The half that makes sampling acceptable at all.

        Sampling is only safe when the interesting traces are exempt from it.
        """
        config = yaml.safe_load((INFRA / "otel-gateway.yaml").read_text(encoding="utf-8"))
        policies = {policy["name"]: policy for policy in config["processors"]["tail_sampling"]["policies"]}

        assert policies["all-errors"]["type"] == "status_code"
        assert policies["all-errors"]["status_code"]["status_codes"] == ["ERROR"]

        assert policies["slow-traces"]["type"] == "latency"
        assert policies["slow-traces"]["latency"]["threshold_ms"] > 0

        # AND THE HEAD-BASED HALF: 10% of everything else. The policies are or-ed by the processor, which is
        # why this sits beside the 100% policies rather than conflicting with them.
        assert policies["routine-sample"]["type"] == "probabilistic"
        assert policies["routine-sample"]["probabilistic"]["sampling_percentage"] == 10

    def test_every_generation_trace_is_kept(self) -> None:
        """A `gen_ai` span is expensive, rare and the thing this product exists to do.

        Sampling it away to save storage would save the wrong thing, and it is what makes per-tenant cost
        attribution complete rather than an estimate scaled up from a sample.
        """
        config = yaml.safe_load((INFRA / "otel-gateway.yaml").read_text(encoding="utf-8"))
        policies = {policy["name"]: policy for policy in config["processors"]["tail_sampling"]["policies"]}
        generation = policies["all-generations"]
        assert generation["type"] == "string_attribute"
        assert generation["string_attribute"]["key"] == GEN_AI_SYSTEM

    def test_tail_sampling_runs_before_batching(self) -> None:
        """It needs complete traces; batching first would hand it arbitrary groupings."""
        config = yaml.safe_load((INFRA / "otel-gateway.yaml").read_text(encoding="utf-8"))
        processors = config["service"]["pipelines"]["traces"]["processors"]
        assert processors.index("tail_sampling") < processors.index("batch")

    def test_metrics_and_logs_are_never_sampled(self) -> None:
        """A sampled counter is a WRONG NUMBER, not a cheaper one.

        A 10% sample of `gen_ai.cost.total` under-reports every tenant's bill by an order of magnitude and
        nothing downstream could tell. For logs the reason is sharper still: the one line an operator needs
        during an incident is exactly the one a sampler would have dropped.
        """
        config = yaml.safe_load((INFRA / "otel-gateway.yaml").read_text(encoding="utf-8"))
        for pipeline_name in ("metrics", "logs"):
            processors = config["service"]["pipelines"][pipeline_name]["processors"]
            assert "tail_sampling" not in processors, f"the {pipeline_name} pipeline samples"
            for processor in processors:
                assert "sampl" not in processor, f"the {pipeline_name} pipeline samples via {processor}"

    def test_exemplars_are_enabled_so_a_metric_can_reach_a_trace(self) -> None:
        """Exemplars are what turn "latency rose at 14:02" into "here is a request that was slow".

        Without them §2.10's trace-correlation panel has nothing to correlate.
        """
        config = yaml.safe_load((INFRA / "otel-gateway.yaml").read_text(encoding="utf-8"))
        assert config["exporters"]["prometheus"]["enable_open_metrics"] is True

    def test_the_memory_limiter_precedes_the_batcher_in_both_tiers(self) -> None:
        """A collector killed by the OOM killer loses everything it held.

        One refusing data loses only what it refused, and says so.
        """
        for name in ("otel-agent.yaml", "otel-gateway.yaml"):
            config = yaml.safe_load((INFRA / name).read_text(encoding="utf-8"))
            for pipeline in config["service"]["pipelines"].values():
                processors = pipeline["processors"]
                assert processors.index("memory_limiter") < processors.index("batch"), name

    def test_the_agent_queue_is_bounded(self) -> None:
        """An unbounded queue is a memory leak wearing a reliability costume.

        A gateway down for an hour would grow it until the collector is killed, losing the very data the
        queue existed to protect.
        """
        config = yaml.safe_load((INFRA / "otel-agent.yaml").read_text(encoding="utf-8"))
        queue = config["exporters"]["otlp/gateway"]["sending_queue"]
        assert queue["enabled"] is True
        assert isinstance(queue["queue_size"], int) and queue["queue_size"] > 0
