# SPDX-License-Identifier: FSL-1.1-ALv2
"""Artifact renderers turning structured Pydantic models into clean text (Leaf 13.3)."""

from __future__ import annotations

from .schemas import DockerfileArtifact, KubernetesManifestArtifact


def render_dockerfile(artifact: DockerfileArtifact) -> str:
    lines = [f"FROM {artifact.base_image}", f"WORKDIR {artifact.workdir}"]
    for cp in artifact.copy_files:
        lines.append(f"COPY {cp} {artifact.workdir}")
    for cmd in artifact.commands:
        lines.append(f"RUN {cmd}")
    if artifact.expose_port:
        lines.append(f"EXPOSE {artifact.expose_port}")
    cmd_str = ", ".join(f'"{c}"' for c in artifact.cmd)
    lines.append(f"CMD [{cmd_str}]")
    return "\n".join(lines) + "\n"


def render_k8s_manifest(artifact: KubernetesManifestArtifact) -> str:
    return f"""apiVersion: apps/v1
kind: Deployment
metadata:
  name: {artifact.name}
spec:
  replicas: {artifact.replicas}
  selector:
    matchLabels:
      app: {artifact.name}
  template:
    metadata:
      labels:
        app: {artifact.name}
    spec:
      containers:
      - name: {artifact.name}
        image: {artifact.image}
        ports:
        - containerPort: {artifact.container_port}
"""
