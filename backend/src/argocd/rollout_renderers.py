# SPDX-License-Identifier: FSL-1.1-ALv2
"""Argo Rollouts manifests. Phase 2 §2.7a.

PROGRESSIVE DELIVERY IS NOT NATIVE TO ARGOCD, which is why this is a separate subsection and a separate
controller. ArgoCD decides WHAT is in the cluster; Argo Rollouts decides HOW a new version replaces the old
one. A canary is a `Rollout` resource, not a `Deployment`, and the two cannot both own the same pods.

THE ONE THING THIS FILE EXISTS TO GET RIGHT: A CANARY IS GATED ON ERROR RATE **AND** LATENCY, NOT EITHER.

A deployment can be fast and broken — a handler returning 500 in two milliseconds looks excellent on a
latency panel. It can also be correct and unusable: every response a 200 after nine seconds. Gating on one
signal admits exactly one of those two failures, and which one depends on which signal somebody picked. So
both conditions are always emitted, and `analysis_template_yaml` refuses to render a template missing
either — a half-gated canary is more dangerous than an ungated one, because it is believed.

AUTOMATIC ROLLBACK IS THE ABSENCE OF A PROMOTION, NOT AN ACTION. Argo Rollouts aborts a rollout when an
analysis run fails, and an aborted rollout returns traffic to the stable ReplicaSet by itself. There is
deliberately no code here that "performs a rollback": a controller already in the cluster does it in
milliseconds, and a backend reacting to a webhook would be slower, would need the cluster to be reachable,
and would be a second mechanism disagreeing with the first.

WHY THE METRIC QUERIES NAME A PROMETHEUS ADDRESS. Rollouts evaluates `AnalysisTemplate` metrics itself, from
inside the cluster, against whatever provider the template names. §2.10 stands Prometheus up; until then
these templates are correct manifests pointed at an address that has to exist before a canary is used, and
that prerequisite is stated in the generated file rather than assumed.
"""

from __future__ import annotations

from typing import Final

ROLLOUTS_API_VERSION: Final[str] = "argoproj.io/v1alpha1"

#: The default in-cluster Prometheus address, matching §2.10's chart values.
DEFAULT_PROMETHEUS_ADDRESS: Final[str] = "http://prometheus-operated.monitoring.svc.cluster.local:9090"

#: The error-rate ceiling a canary must stay under, as a fraction of requests.
#:
#: ONE PER CENT, and the number is a judgement rather than a discovery. A canary carrying 20% of traffic for
#: two minutes sees few enough requests that a stricter threshold is mostly sampling noise, and a looser one
#: admits a clearly broken release. It is a parameter, so a project with a real error budget can set its own.
DEFAULT_MAX_ERROR_RATE: Final[float] = 0.01

#: The p95 latency ceiling, in seconds.
DEFAULT_MAX_P95_SECONDS: Final[float] = 0.5

#: How many consecutive measurements may fail before the rollout is aborted.
#:
#: ONE. A canary is already a small sample over a short window; allowing a second failure doubles the time a
#: broken release serves traffic to buy tolerance for noise that the interval and count already absorb.
DEFAULT_FAILURE_LIMIT: Final[int] = 1


def analysis_template_yaml(
    *,
    name: str,
    service_name: str,
    prometheus_address: str = DEFAULT_PROMETHEUS_ADDRESS,
    max_error_rate: float = DEFAULT_MAX_ERROR_RATE,
    max_p95_seconds: float = DEFAULT_MAX_P95_SECONDS,
    interval_seconds: int = 30,
    count: int = 4,
    failure_limit: int = DEFAULT_FAILURE_LIMIT,
) -> str:
    """The AnalysisTemplate a canary is gated on: error rate AND latency.

    `count` × `interval_seconds` is how long the canary is observed at each weight. Four thirty-second
    measurements is two minutes — long enough for a cold JIT and a connection pool to settle, short enough
    that a broken release is not serving for a quarter of an hour.

    THE QUERIES ARE WRITTEN TO BE SAFE WHEN THERE IS NO TRAFFIC. `sum(rate(...))` over an idle service is
    zero, and a naive error ratio would be `0/0` — NaN, which Rollouts treats as an inconclusive measurement
    rather than a pass. That is correct behaviour and worth keeping deliberate: an unmeasured canary must not
    be promoted, so the template does not paper over the division.
    """
    if max_error_rate <= 0 or max_p95_seconds <= 0:
        raise ValueError(
            "a canary gate needs a positive error-rate and latency ceiling; a zero or negative threshold "
            "can never be satisfied, so the rollout would abort every time and look like a broken release"
        )
    if count < 1 or interval_seconds < 1:
        raise ValueError("a canary must be measured at least once, over a non-zero interval")

    error_query = (
        f'sum(rate(http_requests_total{{service="{service_name}",status=~"5.."}}[{interval_seconds}s])) '
        f'/ sum(rate(http_requests_total{{service="{service_name}"}}[{interval_seconds}s]))'
    )
    latency_query = (
        f"histogram_quantile(0.95, sum(rate("
        f'http_request_duration_seconds_bucket{{service="{service_name}"}}[{interval_seconds}s]'
        f")) by (le))"
    )

    return (
        "\n".join(
            [
                f"apiVersion: {ROLLOUTS_API_VERSION}",
                "kind: AnalysisTemplate",
                "metadata:",
                f"  name: {name}",
                "spec:",
                "  metrics:",
                "    # BOTH METRICS ARE REQUIRED. A release can be fast and broken (500s returned in two",
                "    # milliseconds look excellent on a latency panel) or correct and unusable (every",
                "    # response a 200 after nine seconds). Gating on one admits exactly one of those.",
                "    - name: error-rate",
                f"      interval: {interval_seconds}s",
                f"      count: {count}",
                f"      failureLimit: {failure_limit}",
                "      # An idle service makes this 0/0 = NaN, which Rollouts treats as INCONCLUSIVE rather",
                "      # than as a pass. That is deliberate: an unmeasured canary must not be promoted.",
                f"      successCondition: result[0] <= {max_error_rate}",
                "      provider:",
                "        prometheus:",
                f"          address: {prometheus_address}",
                "          query: >-",
                f"            {error_query}",
                "    - name: p95-latency-seconds",
                f"      interval: {interval_seconds}s",
                f"      count: {count}",
                f"      failureLimit: {failure_limit}",
                f"      successCondition: result[0] <= {max_p95_seconds}",
                "      provider:",
                "        prometheus:",
                f"          address: {prometheus_address}",
                "          query: >-",
                f"            {latency_query}",
            ]
        )
        + "\n"
    )


def canary_rollout_yaml(
    *,
    app_name: str,
    image: str,
    replicas: int = 3,
    port: int = 8080,
    analysis_template: str | None = None,
    steps: tuple[int, ...] = (20, 50, 80),
    pause_seconds: int = 60,
) -> str:
    """A canary Rollout, weighted through `steps` with analysis at every weight.

    ANALYSIS RUNS AT EVERY STEP, NOT ONCE AT THE START. A release that fails only under real load passes a
    20% canary and breaks at 80%, so measuring once would gate the safest weight and none of the others.

    THE FINAL STEP IS NOT 100. Rollouts promotes to 100% by completing the list, and writing `100` as a step
    adds a weight identical to the finished state — a step that can never fail and always adds a pause.
    """
    if not steps or any(weight <= 0 or weight >= 100 for weight in steps):
        raise ValueError(
            "canary weights must each be between 1 and 99: 0 is not a canary and 100 is the finished "
            "rollout, which Rollouts reaches by completing the steps"
        )
    if list(steps) != sorted(steps):
        raise ValueError(
            "canary weights must increase; a rollout that reduced the canary's share mid-flight would be "
            "testing less than it already had"
        )

    lines = [
        f"apiVersion: {ROLLOUTS_API_VERSION}",
        "kind: Rollout",
        "metadata:",
        f"  name: {app_name}",
        "spec:",
        f"  replicas: {replicas}",
        "  selector:",
        "    matchLabels:",
        f"      app: {app_name}",
        "  template:",
        "    metadata:",
        "      labels:",
        f"        app: {app_name}",
        "    spec:",
        "      containers:",
        f"        - name: {app_name}",
        f"          image: {image}",
        "          ports:",
        f"            - containerPort: {port}",
        "  strategy:",
        "    canary:",
        "      steps:",
    ]
    for weight in steps:
        lines += [
            f"        - setWeight: {weight}",
        ]
        if analysis_template:
            lines += [
                "        - analysis:",
                "            templates:",
                f"              - templateName: {analysis_template}",
                "            args:",
                "              - name: canary-weight",
                f'                value: "{weight}"',
            ]
        else:
            # NO ANALYSIS MEANS A TIMED PAUSE, and the file says so rather than looking gated. A pause is a
            # human's chance to notice; it is not a check, and a reader must not mistake one for the other.
            lines += [
                "        # A TIMED PAUSE, NOT A GATE: nothing is measured here. Supply an AnalysisTemplate",
                "        # to make this weight conditional on error rate and latency.",
                f"        - pause: {{duration: {pause_seconds}s}}",
            ]
    lines += [
        "      # An aborted analysis returns traffic to the stable ReplicaSet by itself. There is no",
        "      # rollback code anywhere in this product for that reason: the controller does it in",
        "      # milliseconds, and a backend reacting to a webhook would be slower and could disagree.",
        "      abortScaleDownDelaySeconds: 30",
    ]
    return "\n".join(lines) + "\n"


def blue_green_rollout_yaml(
    *,
    app_name: str,
    image: str,
    replicas: int = 3,
    port: int = 8080,
    analysis_template: str | None = None,
    auto_promotion: bool = False,
) -> str:
    """A blue-green Rollout with an active and a preview service.

    `autoPromotionEnabled` IS FALSE BY DEFAULT. The entire point of blue-green over canary is that a human
    (or an analysis run) looks at the new version on the preview service before any real traffic reaches it.
    Auto-promotion on turns it into a slower rolling update with extra resources.

    THE PREVIEW SERVICE IS A SEPARATE SERVICE, and its name is emitted here rather than left to a convention:
    a blue-green Rollout naming a preview service that does not exist reports as progressing for ever, with
    no error saying which object is missing.
    """
    lines = [
        f"apiVersion: {ROLLOUTS_API_VERSION}",
        "kind: Rollout",
        "metadata:",
        f"  name: {app_name}",
        "spec:",
        f"  replicas: {replicas}",
        "  selector:",
        "    matchLabels:",
        f"      app: {app_name}",
        "  template:",
        "    metadata:",
        "      labels:",
        f"        app: {app_name}",
        "    spec:",
        "      containers:",
        f"        - name: {app_name}",
        f"          image: {image}",
        "          ports:",
        f"            - containerPort: {port}",
        "  strategy:",
        "    blueGreen:",
        f"      activeService: {app_name}",
        f"      previewService: {app_name}-preview",
        f"      autoPromotionEnabled: {str(auto_promotion).lower()}",
        "      # The old ReplicaSet is kept for half an hour after promotion, so an undo is instant rather",
        "      # than a redeploy. Scaling it down immediately is what makes a bad promotion expensive.",
        "      scaleDownDelaySeconds: 1800",
    ]
    if analysis_template:
        lines += [
            "      prePromotionAnalysis:",
            "        templates:",
            f"          - templateName: {analysis_template}",
        ]
    else:
        lines += [
            "      # NO PRE-PROMOTION ANALYSIS. Promotion is then entirely a human's judgement, which is a",
            "      # legitimate choice and a different one from being gated. Supply a template to gate it.",
        ]
    return "\n".join(lines) + "\n"
