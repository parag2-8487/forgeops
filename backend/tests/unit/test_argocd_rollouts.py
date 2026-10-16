# SPDX-License-Identifier: FSL-1.1-ALv2
"""ArgoCD and Argo Rollouts manifest generation. Phase 2 §2.7 and §2.7a.

EVERY ASSERTION HERE PARSES THE YAML. Matching substrings would pass on a manifest that is well-formed
nonsense — an `automated:` block nested one level too deep reads correctly and is ignored by the controller,
which is the quietest possible failure for a GitOps manifest. Parsing is what distinguishes "the text
contains prune: false" from "ArgoCD will not prune".
"""

from __future__ import annotations

import pytest
import yaml
from src.argocd.renderers import (
    ARGOCD_NAMESPACE,
    RESOURCES_FINALIZER,
    argocd_app_of_apps_yaml,
    argocd_application_yaml,
    argocd_applicationset_yaml,
)
from src.argocd.rollout_renderers import (
    analysis_template_yaml,
    blue_green_rollout_yaml,
    canary_rollout_yaml,
)


def _parse(text: str) -> dict:
    parsed = yaml.safe_load(text)
    assert isinstance(parsed, dict), "the renderer produced something that is not a single YAML document"
    return parsed


class TestTheApplicationSafetyDefaults:
    def test_an_application_is_not_automated_and_therefore_cannot_prune(self) -> None:
        """The three defaults that matter, asserted on the parsed document.

        `prune: true` lets ArgoCD delete anything no longer in Git — a misplaced kustomization becomes a
        deleted StatefulSet. `selfHeal: true` reverts an operator's manual change mid-incident with no
        explanation on the dashboard they are watching. Automation at all bypasses the approval chokepoint.
        """
        document = _parse(
            argocd_application_yaml(
                app_name="api",
                repo_url="https://github.com/acme/api",
                target_revision="v1.2.3",
                path="k8s/production",
                destination_namespace="api-production",
            )
        )
        sync_policy = document["spec"]["syncPolicy"]
        # NO `automated` KEY AT ALL, not `automated: {prune: false}`. An absent block is the setting; an
        # empty one is a block a later edit fills in.
        assert "automated" not in sync_policy, (
            "a generated Application arrived with an automated sync policy, which applies Git to the "
            "cluster with no human in the loop"
        )

    def test_automation_is_expressible_and_still_defaults_prune_and_self_heal_off(self) -> None:
        """Enabling automation must not silently enable pruning with it."""
        document = _parse(
            argocd_application_yaml(
                app_name="api",
                repo_url="https://github.com/acme/api",
                target_revision="main",
                path="k8s",
                destination_namespace="api",
                automated=True,
            )
        )
        automated = document["spec"]["syncPolicy"]["automated"]
        assert automated["prune"] is False
        assert automated["selfHeal"] is False

    def test_deleting_an_application_deletes_what_it_deployed(self) -> None:
        """The finalizer IS on by default, and the asymmetry with `prune` is the point.

        Deleting an Application is an explicit act with an obvious intent. Without the finalizer it orphans
        every resource it deployed — objects nothing manages and nothing lists, which is worse than either
        pruning or leaving them managed.
        """
        document = _parse(
            argocd_application_yaml(
                app_name="api",
                repo_url="https://github.com/acme/api",
                target_revision="main",
                path="k8s",
                destination_namespace="api",
            )
        )
        assert RESOURCES_FINALIZER in document["metadata"]["finalizers"]

    def test_an_application_lands_in_the_namespace_the_controller_watches(self) -> None:
        """An Application created elsewhere is silently ignored — a failure with no error anywhere."""
        document = _parse(
            argocd_application_yaml(
                app_name="api",
                repo_url="https://github.com/acme/api",
                target_revision="main",
                path="k8s",
                destination_namespace="api",
            )
        )
        assert document["metadata"]["namespace"] == ARGOCD_NAMESPACE

    def test_the_revision_is_whatever_the_caller_named(self) -> None:
        """`targetRevision: HEAD` makes "what is running" unanswerable, so it is never a default."""
        document = _parse(
            argocd_application_yaml(
                app_name="api",
                repo_url="https://github.com/acme/api",
                target_revision="v1.2.3",
                path="k8s",
                destination_namespace="api",
            )
        )
        assert document["spec"]["source"]["targetRevision"] == "v1.2.3"


class TestTheAppOfAppsRoot:
    def test_the_root_is_never_automated(self) -> None:
        """The most consequential default in the file.

        An automated root with `prune` deletes APPLICATIONS, and each deletion cascades through the resources
        finalizer to everything that Application deployed. One wrong directory listing takes down a fleet.
        """
        document = _parse(
            argocd_app_of_apps_yaml(
                root_name="platform",
                repo_url="https://github.com/acme/platform",
                target_revision="main",
                applications_path="argocd/applications",
            )
        )
        assert "automated" not in document["spec"]["syncPolicy"]
        assert document["spec"]["source"]["directory"]["recurse"] is True


class TestTheApplicationSet:
    def test_it_generates_one_application_per_named_environment(self) -> None:
        document = _parse(
            argocd_applicationset_yaml(
                set_name="api",
                repo_url="https://github.com/acme/api",
                target_revision="main",
                environments=("dev", "staging", "production"),
            )
        )
        elements = document["spec"]["generators"][0]["list"]["elements"]
        assert [element["environment"] for element in elements] == ["dev", "staging", "production"]

    def test_removing_an_environment_does_not_delete_its_resources(self) -> None:
        """`preserveResourcesOnDeletion` is true.

        Removing a line from a list is a small edit with, by default, an enormous consequence — and recovery
        from an accidental deletion is far harder than cleanup after an intentional one.
        """
        document = _parse(
            argocd_applicationset_yaml(
                set_name="api",
                repo_url="https://github.com/acme/api",
                target_revision="main",
                environments=("dev",),
            )
        )
        assert document["spec"]["syncPolicy"]["preserveResourcesOnDeletion"] is True

    def test_an_empty_environment_list_is_refused(self) -> None:
        """A file that looks like a deployment and generates nothing."""
        with pytest.raises(ValueError, match="no environments"):
            argocd_applicationset_yaml(
                set_name="api",
                repo_url="https://github.com/acme/api",
                target_revision="main",
                environments=(),
            )


class TestTheCanaryIsGatedOnBothSignals:
    def test_an_analysis_template_always_carries_error_rate_and_latency(self) -> None:
        """THE ASSERTION THIS SUBSECTION EXISTS FOR.

        A release can be fast and broken — 500s returned in two milliseconds look excellent on a latency
        panel — or correct and unusable, every response a 200 after nine seconds. Gating on one signal admits
        exactly one of those failures, and a half-gated canary is more dangerous than an ungated one because
        it is believed.
        """
        document = _parse(analysis_template_yaml(name="api-gate", service_name="api"))
        metrics = {metric["name"] for metric in document["spec"]["metrics"]}
        assert metrics == {"error-rate", "p95-latency-seconds"}, f"the gate measures {metrics}"

    def test_both_metrics_carry_a_success_condition_and_a_failure_limit(self) -> None:
        """A metric with no success condition is collected and never evaluated."""
        document = _parse(analysis_template_yaml(name="api-gate", service_name="api"))
        for metric in document["spec"]["metrics"]:
            assert metric["successCondition"], f"{metric['name']} has no success condition"
            assert metric["failureLimit"] >= 1
            assert metric["provider"]["prometheus"]["query"].strip()

    def test_a_non_positive_threshold_is_refused(self) -> None:
        """A threshold that can never be satisfied aborts every rollout and looks like a broken release."""
        with pytest.raises(ValueError, match="positive"):
            analysis_template_yaml(name="api-gate", service_name="api", max_error_rate=0)
        with pytest.raises(ValueError, match="positive"):
            analysis_template_yaml(name="api-gate", service_name="api", max_p95_seconds=-1)

    def test_analysis_runs_at_every_canary_weight(self) -> None:
        """A release that fails only under load passes a 20% canary and breaks at 80%."""
        document = _parse(
            canary_rollout_yaml(
                app_name="api",
                image="registry/api:v2",
                analysis_template="api-gate",
                steps=(20, 50, 80),
            )
        )
        steps = document["spec"]["strategy"]["canary"]["steps"]
        weights = [step["setWeight"] for step in steps if "setWeight" in step]
        analyses = [step for step in steps if "analysis" in step]
        assert weights == [20, 50, 80]
        assert len(analyses) == len(weights), (
            f"{len(analyses)} analysis steps for {len(weights)} weights; an unmeasured weight is a weight "
            "that promotes itself"
        )

    def test_a_canary_without_a_template_pauses_and_does_not_pretend_to_be_gated(self) -> None:
        """A pause is a human's chance to notice. It is not a check, and the manifest must not imply one."""
        rendered = canary_rollout_yaml(app_name="api", image="registry/api:v2", steps=(20,))
        document = _parse(rendered)
        steps = document["spec"]["strategy"]["canary"]["steps"]
        assert any("pause" in step for step in steps)
        assert not any("analysis" in step for step in steps)
        assert "NOT A GATE" in rendered

    def test_invalid_weights_are_refused(self) -> None:
        with pytest.raises(ValueError, match="between 1 and 99"):
            canary_rollout_yaml(app_name="api", image="i", steps=(0, 50))
        with pytest.raises(ValueError, match="between 1 and 99"):
            canary_rollout_yaml(app_name="api", image="i", steps=(20, 100))
        with pytest.raises(ValueError, match="must increase"):
            canary_rollout_yaml(app_name="api", image="i", steps=(50, 20))


class TestBlueGreen:
    def test_promotion_is_manual_by_default(self) -> None:
        """Auto-promotion on turns blue-green into a slower rolling update with extra resources."""
        document = _parse(blue_green_rollout_yaml(app_name="api", image="registry/api:v2"))
        strategy = document["spec"]["strategy"]["blueGreen"]
        assert strategy["autoPromotionEnabled"] is False
        assert strategy["previewService"] == "api-preview"
        assert strategy["activeService"] == "api"

    def test_the_old_replicaset_is_kept_so_an_undo_is_instant(self) -> None:
        document = _parse(blue_green_rollout_yaml(app_name="api", image="registry/api:v2"))
        assert document["spec"]["strategy"]["blueGreen"]["scaleDownDelaySeconds"] >= 600

    def test_a_template_makes_promotion_conditional(self) -> None:
        document = _parse(
            blue_green_rollout_yaml(app_name="api", image="registry/api:v2", analysis_template="api-gate")
        )
        analysis = document["spec"]["strategy"]["blueGreen"]["prePromotionAnalysis"]
        assert analysis["templates"][0]["templateName"] == "api-gate"
