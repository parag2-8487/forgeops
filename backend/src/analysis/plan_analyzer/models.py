# SPDX-License-Identifier: FSL-1.1-ALv2
"""Data models for the plan analysis pipeline."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    FATAL = "fatal"


@dataclass(frozen=True)
class Finding:
    stage: str
    severity: Severity
    code: str
    message: str
    resource: str | None = None


@dataclass
class StageContext:
    """Mutable context passed through stages for accumulation."""

    findings: list[Finding] = field(default_factory=list)
    fatal: bool = False


@dataclass
class PlanDocument:
    """A parsed OpenTofu/Terraform plan in JSON format."""

    raw: dict[str, Any]
    format_version: str = ""
    terraform_version: str = ""
    resource_changes: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_json(cls, data: str | bytes) -> PlanDocument:
        """Parse a plan JSON string into a PlanDocument.

        Raises ValueError on invalid JSON or missing required fields.
        """
        try:
            raw = json.loads(data)
        except (json.JSONDecodeError, TypeError) as e:
            raise ValueError(f"Invalid plan JSON: {e}") from e

        if not isinstance(raw, dict):
            raise ValueError("Plan JSON must be an object")

        return cls(
            raw=raw,
            format_version=raw.get("format_version", ""),
            terraform_version=raw.get("terraform_version", ""),
            resource_changes=raw.get("resource_changes", []),
        )


class Stage(Protocol):
    """A pipeline stage that inspects a plan document."""

    name: str

    async def run(self, doc: PlanDocument, ctx: StageContext) -> list[Finding]: ...
