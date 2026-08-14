# SPDX-License-Identifier: FSL-1.1-ALv2
"""Structured artifact schemas for AI generation pipeline (Leaf 13.3)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class DockerfileArtifact(BaseModel):
    kind: Literal["dockerfile"] = "dockerfile"
    base_image: str = Field(default="python:3.11-slim")
    workdir: str = Field(default="/app")
    copy_files: list[str] = Field(default_factory=lambda: ["."])
    commands: list[str] = Field(default_factory=list)
    expose_port: int | None = Field(default=8000)
    cmd: list[str] = Field(default_factory=lambda: ["python", "main.py"])


class KubernetesManifestArtifact(BaseModel):
    kind: Literal["k8s"] = "k8s"
    name: str
    replicas: int = Field(default=1)
    image: str
    container_port: int = Field(default=8000)


class GeneratedArtifactEnvelope(BaseModel):
    artifact_id: str
    kind: str
    content: DockerfileArtifact | KubernetesManifestArtifact
