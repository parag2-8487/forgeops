# SPDX-License-Identifier: FSL-1.1-ALv2
"""The generated artifacts must describe the REAL project (design.md §11.5, §12.6).

WHY THESE ARE ASSERTED

`_render` used to name every project's Kubernetes resources `forgeops-app` and choose the runtime by
searching the operator's PROMPT for the substring "node". Both are hardcoded answers standing in for
reading the project: two projects generate colliding resource names, and the same project generates
different infrastructure depending on how somebody phrased a sentence.

The name is also the part that fails LOUDLY but late. Kubernetes refuses a label with capitals,
underscores or a leading digit, so an unsanitised project name reaches the validation pipeline and
fails the run there — three layers from the cause.
"""

from __future__ import annotations

import pytest
from src.generation.service import (
    GenerationService,
    _deployment_yaml,
    _ingress_yaml,
    _kubernetes_name,
    _service_yaml,
)

pytestmark = [pytest.mark.mandatory]


class TestTheKubernetesNameIsAValidLabel:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("checkout-api", "checkout-api"),
            # Capitals and underscores are both refused by Kubernetes.
            ("Checkout_API", "checkout-api"),
            ("  Payments Service  ", "payments-service"),
            # Runs of separators collapse rather than leaving an empty segment.
            ("a///b", "a-b"),
            # A label must START with a letter, so a numeric name is prefixed rather than discarded —
            # keeping it identifiable instead of silently becoming the generic default.
            ("2024-migration", "app-2024-migration"),
            # Nothing usable: the caller keeps its documented default.
            ("", ""),
            ("---", ""),
        ],
    )
    def test_it_produces_an_rfc_1123_label_or_nothing(self, raw: str, expected: str) -> None:
        assert _kubernetes_name(raw) == expected

    def test_it_never_exceeds_the_63_character_limit(self) -> None:
        """A label longer than 63 characters is rejected by the API server."""
        assert len(_kubernetes_name("x" * 200)) == 63
        assert len(_kubernetes_name("9" * 200)) <= 63


class TestTheManifestsReferToEachOther:
    """Three files that ignore each other would be worse than one."""

    def test_the_service_selects_the_deployment_and_the_ingress_names_the_service(self) -> None:
        name, port = "checkout-api", 3000
        deployment = _deployment_yaml(name, port)
        service = _service_yaml(name, port)
        ingress = _ingress_yaml(name, port)

        assert f"app: {name}" in deployment and f"app: {name}" in service
        assert f"name: {name}" in ingress
        # The Service publishes 80 and targets the container's port; the Ingress addresses 80.
        assert "port: 80" in service and f"targetPort: {port}" in service
        assert "number: 80" in ingress
        assert f"containerPort: {port}" in deployment

    def test_the_service_is_clusterip(self) -> None:
        """A type that provisions cloud infrastructure is a decision with a bill attached."""
        assert "type: ClusterIP" in _service_yaml("checkout-api", 8000)
        assert "LoadBalancer" not in _service_yaml("checkout-api", 8000)

    def test_the_ingress_host_is_reserved_for_local_resolution(self) -> None:
        """`.local` cannot belong to somebody else, unlike a plausible-looking domain."""
        assert "host: checkout-api.local" in _ingress_yaml("checkout-api", 8000)


class TestTheRenderReadsTheProject:
    def _render(self, prompt: str, project: dict | None):
        return GenerationService()._render(prompt, project)

    def test_the_project_name_becomes_the_resource_name(self) -> None:
        files = self._render("a service", {"name": "Checkout_API", "settings": {}})
        by_path = {f.path: f.content for f in files}
        assert "name: checkout-api" in by_path["k8s/deployment.yaml"]
        assert "image: checkout-api:latest" in by_path["k8s/deployment.yaml"]

    def test_a_recorded_runtime_beats_the_prompt(self) -> None:
        """A prompt is a request; `settings` is a fact somebody recorded about the project."""
        files = self._render("a python service", {"name": "api", "settings": {"runtime": "node"}})
        dockerfile = next(f.content for f in files if f.path == "Dockerfile")
        assert "node:20-alpine" in dockerfile
        assert "npm ci --omit=dev" in dockerfile

    def test_a_recorded_port_base_image_and_start_command_are_honoured(self) -> None:
        files = self._render(
            "a service",
            {
                "name": "api",
                "settings": {
                    "runtime": "python",
                    "port": 9001,
                    "base_image": "python:3.13-slim",
                    "start_command": ["gunicorn", "app:main"],
                },
            },
        )
        by_path = {f.path: f.content for f in files}
        assert "FROM python:3.13-slim" in by_path["Dockerfile"]
        assert "EXPOSE 9001" in by_path["Dockerfile"]
        assert '"gunicorn", "app:main"' in by_path["Dockerfile"]
        assert "targetPort: 9001" in by_path["k8s/service.yaml"]

    def test_a_start_command_string_is_split(self) -> None:
        files = self._render("a service", {"name": "api", "settings": {"start_command": "python -m app"}})
        assert '"python", "-m", "app"' in next(f.content for f in files if f.path == "Dockerfile")

    @pytest.mark.parametrize("bad_port", ["not-a-number", 0, 70000, None])
    def test_an_unusable_port_falls_back_rather_than_failing_the_run(self, bad_port: object) -> None:
        """A malformed setting must not fail generation: the runtime default is the honest answer."""
        files = self._render("a python service", {"name": "api", "settings": {"port": bad_port}})
        assert "EXPOSE 8000" in next(f.content for f in files if f.path == "Dockerfile")

    def test_no_project_still_renders_the_documented_default(self) -> None:
        """The service must stay usable without project facts; `_render` documents the fallback."""
        files = self._render("an express node api", None)
        by_path = {f.path: f.content for f in files}
        assert "node:20-alpine" in by_path["Dockerfile"]
        assert "name: forgeops-app" in by_path["k8s/deployment.yaml"]

    def test_a_malformed_settings_value_is_ignored(self) -> None:
        """`settings` is operator-supplied JSON, so it may not be a mapping at all."""
        files = self._render("a python service", {"name": "api", "settings": ["not", "a", "mapping"]})
        assert "name: api" in next(f.content for f in files if f.path == "k8s/deployment.yaml")
