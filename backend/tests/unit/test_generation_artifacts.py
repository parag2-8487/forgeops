# SPDX-License-Identifier: FSL-1.1-ALv2
import pytest
from src.generation.renderers import render_dockerfile, render_k8s_manifest
from src.generation.schemas import DockerfileArtifact, KubernetesManifestArtifact

pytestmark = [pytest.mark.mandatory]


def test_render_dockerfile():
    art = DockerfileArtifact(
        base_image="python:3.11-slim",
        commands=["pip install -r requirements.txt"],
        expose_port=8000,
    )
    text = render_dockerfile(art)
    assert "FROM python:3.11-slim" in text
    assert "RUN pip install -r requirements.txt" in text
    assert "EXPOSE 8000" in text


def test_render_k8s_manifest():
    art = KubernetesManifestArtifact(name="web-app", replicas=3, image="web:v1", container_port=8080)
    text = render_k8s_manifest(art)
    assert "name: web-app" in text
    assert "replicas: 3" in text
    assert "containerPort: 8080" in text
