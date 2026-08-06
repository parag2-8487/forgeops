# SPDX-License-Identifier: FSL-1.1-ALv2
"""DryRun validation stage for generation pipeline (Leaf 13.7)."""

from __future__ import annotations

from typing import Any
from pydantic import BaseModel


class DryRunResult(BaseModel):
    valid: bool
    stage: str = "dry_run"
    errors: list[str]


class DryRunStage:
    """Validates artifact syntax and structure in dry-run mode prior to admission."""

    def validate_dockerfile(self, content: str) -> DryRunResult:
        errors: list[str] = []
        if "FROM " not in content:
            errors.append("Missing FROM directive in Dockerfile")

        return DryRunResult(valid=len(errors) == 0, errors=errors)

    def validate_k8s_manifest(self, content: str) -> DryRunResult:
        errors: list[str] = []
        if "apiVersion:" not in content or "kind:" not in content:
            errors.append("Missing mandatory Kubernetes resource fields (apiVersion, kind)")

        return DryRunResult(valid=len(errors) == 0, errors=errors)
