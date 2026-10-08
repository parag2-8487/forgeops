"""Authoritative ProjectBlueprint schema and contracts for ForgeOps.

This module serves as the single source of truth across repository analysis,
artifact generation, pre-execution validation, runtime execution, and AI self-healing.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional


class WorkloadType(str, Enum):
    """Classification of the application workload."""
    WEB_SERVICE = "web_service"
    TCP_SERVICE = "tcp_service"
    BACKGROUND_WORKER = "background_worker"
    BATCH_JOB = "batch_job"
    STATIC_SPA = "static_spa"


class ProtocolType(str, Enum):
    """Network protocol for endpoint exposure and health checks."""
    HTTP = "http"
    TCP = "tcp"
    NONE = "none"


@dataclass
class NetworkContract:
    """Network exposure and health verification configuration."""
    listen_port: Optional[int] = None
    protocol: ProtocolType = ProtocolType.HTTP
    health_check_path: Optional[str] = "/health"
    exposed_endpoints: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "listen_port": self.listen_port,
            "protocol": self.protocol.value if isinstance(self.protocol, ProtocolType) else self.protocol,
            "health_check_path": self.health_check_path,
            "exposed_endpoints": list(self.exposed_endpoints),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> NetworkContract:
        protocol = data.get("protocol", ProtocolType.HTTP)
        if isinstance(protocol, str):
            protocol = ProtocolType(protocol)
        return cls(
            listen_port=data.get("listen_port"),
            protocol=protocol,
            health_check_path=data.get("health_check_path", "/health"),
            exposed_endpoints=data.get("exposed_endpoints", []),
        )


@dataclass
class BuildConfig:
    """Build lifecycle and directory configuration."""
    source_dir: str = "."  # Relative path from repository root (e.g. "." or "apps/web")
    build_command: Optional[str] = None  # e.g. "npm run build", "cargo build --release"
    artifact_output_dir: Optional[str] = None  # Discovered output directory, e.g. ".next", "dist"
    install_command: Optional[str] = None  # e.g. "pnpm install --frozen-lockfile"
    cache_dirs: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source_dir": self.source_dir,
            "build_command": self.build_command,
            "artifact_output_dir": self.artifact_output_dir,
            "install_command": self.install_command,
            "cache_dirs": list(self.cache_dirs),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> BuildConfig:
        return cls(
            source_dir=data.get("source_dir", "."),
            build_command=data.get("build_command"),
            artifact_output_dir=data.get("artifact_output_dir"),
            install_command=data.get("install_command"),
            cache_dirs=data.get("cache_dirs", []),
        )


@dataclass
class RuntimeContract:
    """Runtime language, framework, dependencies and execution configuration."""
    language: str  # "nodejs", "python", "golang", "rust", "java", etc.
    runtime_version: str  # e.g. "20", "3.11", "1.22"
    framework: Optional[str] = None  # "nextjs", "fastapi", "react", "express", etc.
    package_manager: str = "npm"  # "pnpm", "npm", "yarn", "poetry", "cargo", "go", etc.
    start_command: str = ""  # e.g. "npm run start"
    environment_variables: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "language": self.language,
            "runtime_version": self.runtime_version,
            "framework": self.framework,
            "package_manager": self.package_manager,
            "start_command": self.start_command,
            "environment_variables": dict(self.environment_variables),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> RuntimeContract:
        return cls(
            language=data.get("language", ""),
            runtime_version=data.get("runtime_version", ""),
            framework=data.get("framework"),
            package_manager=data.get("package_manager", "npm"),
            start_command=data.get("start_command", ""),
            environment_variables=data.get("environment_variables", {}),
        )


@dataclass
class AmbiguityResolution:
    """Tracking deterministic ambiguity detection and operator clarification."""
    is_ambiguous: bool = False
    resolution_strategy: Optional[str] = None
    confidence_score: float = 1.0
    detected_candidates: List[str] = field(default_factory=list)
    unresolved_reason: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "is_ambiguous": self.is_ambiguous,
            "resolution_strategy": self.resolution_strategy,
            "confidence_score": self.confidence_score,
            "detected_candidates": list(self.detected_candidates),
            "unresolved_reason": self.unresolved_reason,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> AmbiguityResolution:
        return cls(
            is_ambiguous=data.get("is_ambiguous", False),
            resolution_strategy=data.get("resolution_strategy"),
            confidence_score=float(data.get("confidence_score", 1.0)),
            detected_candidates=data.get("detected_candidates", []),
            unresolved_reason=data.get("unresolved_reason"),
        )


@dataclass
class ProjectBlueprint:
    """Authoritative project representation for automated deployment."""
    blueprint_id: str
    repository_root: str
    is_monorepo: bool = False
    workload_type: WorkloadType = WorkloadType.WEB_SERVICE
    build_config: BuildConfig = field(default_factory=BuildConfig)
    runtime: RuntimeContract = field(default_factory=lambda: RuntimeContract(language="generic", runtime_version="latest"))
    network: NetworkContract = field(default_factory=NetworkContract)
    ambiguity: AmbiguityResolution = field(default_factory=AmbiguityResolution)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "blueprint_id": self.blueprint_id,
            "repository_root": self.repository_root,
            "is_monorepo": self.is_monorepo,
            "workload_type": self.workload_type.value if isinstance(self.workload_type, WorkloadType) else self.workload_type,
            "build_config": self.build_config.to_dict(),
            "runtime": self.runtime.to_dict(),
            "network": self.network.to_dict(),
            "ambiguity": self.ambiguity.to_dict(),
        }

    def to_json(self, indent: Optional[int] = None) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> ProjectBlueprint:
        workload = data.get("workload_type", WorkloadType.WEB_SERVICE)
        if isinstance(workload, str):
            workload = WorkloadType(workload)

        return cls(
            blueprint_id=data.get("blueprint_id", ""),
            repository_root=data.get("repository_root", ""),
            is_monorepo=data.get("is_monorepo", False),
            workload_type=workload,
            build_config=BuildConfig.from_dict(data.get("build_config", {})),
            runtime=RuntimeContract.from_dict(data.get("runtime", {})),
            network=NetworkContract.from_dict(data.get("network", {})),
            ambiguity=AmbiguityResolution.from_dict(data.get("ambiguity", {})),
        )

    @classmethod
    def from_json(cls, json_str: str) -> ProjectBlueprint:
        return cls.from_dict(json.loads(json_str))
