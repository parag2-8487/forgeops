"""Diagnostic bundle data model and serialization in Python."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Optional

from backend.src.blueprint.models import ProjectBlueprint


@dataclass
class DiagnosticBundle:
    """Aggregates all contextual and forensic telemetry upon gate failure."""
    gate_identifier: str
    stage: str
    blueprint: Optional[ProjectBlueprint] = None
    deployment_manifest: Dict[str, str] = field(default_factory=dict)
    command_line: str = ""
    exit_code: int = 1
    attempt_count: int = 1
    stdout: str = ""
    stderr: str = ""
    error_classification: str = "DETERMINISTIC"
    container_state: Dict[str, Any] = field(default_factory=dict)
    container_logs: str = ""
    tree_snippet: str = ""

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        if self.blueprint:
            data["blueprint"] = self.blueprint.to_dict()
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> DiagnosticBundle:
        bp_data = data.get("blueprint")
        bp = ProjectBlueprint.from_dict(bp_data) if bp_data else None
        return cls(
            gate_identifier=data.get("gate_identifier", ""),
            stage=data.get("stage", ""),
            blueprint=bp,
            deployment_manifest=data.get("deployment_manifest", {}),
            command_line=data.get("command_line", ""),
            exit_code=data.get("exit_code", 1),
            attempt_count=data.get("attempt_count", 1),
            stdout=data.get("stdout", ""),
            stderr=data.get("stderr", ""),
            error_classification=data.get("error_classification", "DETERMINISTIC"),
            container_state=data.get("container_state", {}),
            container_logs=data.get("container_logs", ""),
            tree_snippet=data.get("tree_snippet", ""),
        )
