# SPDX-License-Identifier: FSL-1.1-ALv2
"""Autonomous Deployment Orchestrator public schemas and strategy validation.

Provides:
- DeploymentStrategy: enum of supported deployment strategies.
- StageStatus: enum of all canonical stage execution statuses.
- AutonomousRunStatus: enum of all canonical run execution statuses.
- Sub-config request schemas: GitHubConfigRequest, VercelConfigRequest, DockerConfigRequest.
- CreateAutonomousRunRequest: request schema with cross-field strategy and env validation.
- StagePublicResponse: public API representation of an individual pipeline stage or gate.
- AutonomousRunPublicResponse: public API representation of a run (strictly excludes internal fields).
- LogEntryPublicResponse, PaginatedLogsResponse: line-level log pagination schemas.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class DeploymentStrategy(StrEnum):
    """Supported orchestration deployment strategies."""

    DOCKER_GITHUB_VERCEL = "docker_github_vercel"
    DOCKER_GITHUB = "docker_github"
    GITHUB_ONLY = "github_only"
    VERCEL_ONLY = "vercel_only"


class StageStatus(StrEnum):
    """Canonical stage and gate execution statuses."""

    PENDING = "pending"
    WAITING = "waiting"
    RUNNING = "running"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    ROLLED_BACK = "rolled_back"


class AutonomousRunStatus(StrEnum):
    """Authoritative autonomous deployment run lifecycle statuses."""

    PENDING = "pending"
    RUNNING = "running"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"


class GitHubConfigRequest(BaseModel):
    """Target GitHub repository configuration."""

    model_config = ConfigDict(extra="ignore")

    repository_mode: str = "existing"
    repository_name: str
    target_branch: str = "main"
    commit_message: str | None = "Automated deployment by ForgeOps"


class VercelConfigRequest(BaseModel):
    """Target Vercel cloud deployment configuration."""

    model_config = ConfigDict(extra="ignore")

    project_name: str
    production_deploy: bool = True
    environment_variables: dict[str, str] = Field(default_factory=dict)


RESERVED_DOCKER_ENV_NAMES: frozenset[str] = frozenset({"PORT", "NODE_ENV"})
RESERVED_DOCKER_ENV_PREFIX: str = "FORGEOPS_"


def _check_reserved_docker_env(name: str) -> None:
    norm = name.strip().upper()
    if norm in RESERVED_DOCKER_ENV_NAMES or norm.startswith(RESERVED_DOCKER_ENV_PREFIX):
        raise ValueError(f"Environment variable '{name}' is reserved by the platform and cannot be overridden")


class DockerConfigRequest(BaseModel):
    """Target Docker container configuration."""

    model_config = ConfigDict(extra="ignore")

    port_bindings: dict[str, int] = Field(default_factory=dict)
    environment_overrides: dict[str, str] = Field(default_factory=dict)
    container_name: str | None = None
    image_tag: str | None = None

    @field_validator("environment_overrides")
    @classmethod
    def validate_environment_overrides(cls, v: dict[str, str]) -> dict[str, str]:
        for key in v.keys():
            _check_reserved_docker_env(key)
        return v


class CreateAutonomousRunRequest(BaseModel):
    """Request payload to initiate or register an autonomous deployment run."""

    model_config = ConfigDict(extra="ignore")

    strategy: DeploymentStrategy
    github_config: GitHubConfigRequest | None = None
    vercel_config: VercelConfigRequest | None = None
    docker_config: DockerConfigRequest | None = None
    idempotency_key: str | None = None

    @model_validator(mode="after")
    def validate_strategy_constraints(self) -> CreateAutonomousRunRequest:
        strat = self.strategy
        if strat == DeploymentStrategy.DOCKER_GITHUB_VERCEL:
            if self.github_config is None:
                raise ValueError("Strategy 'docker_github_vercel' requires github_config")
            if self.vercel_config is None:
                raise ValueError("Strategy 'docker_github_vercel' requires vercel_config")
        elif strat == DeploymentStrategy.DOCKER_GITHUB:
            if self.github_config is None:
                raise ValueError("Strategy 'docker_github' requires github_config")
            if self.vercel_config is not None:
                raise ValueError("Strategy 'docker_github' forbids vercel_config")
        elif strat == DeploymentStrategy.GITHUB_ONLY:
            if self.github_config is None:
                raise ValueError("Strategy 'github_only' requires github_config")
            if self.vercel_config is not None:
                raise ValueError("Strategy 'github_only' forbids vercel_config")
            if self.docker_config is not None:
                raise ValueError("Strategy 'github_only' forbids docker_config")
        elif strat == DeploymentStrategy.VERCEL_ONLY:
            if self.vercel_config is None:
                raise ValueError("Strategy 'vercel_only' requires vercel_config")
            if self.github_config is not None:
                raise ValueError("Strategy 'vercel_only' forbids github_config")
            if self.docker_config is not None:
                raise ValueError("Strategy 'vercel_only' forbids docker_config")

        if self.docker_config and self.docker_config.environment_overrides:
            for key in self.docker_config.environment_overrides.keys():
                _check_reserved_docker_env(key)

        return self


class StagePublicResponse(BaseModel):
    """Public representation of an operational stage or canonical gate."""

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: uuid.UUID
    run_id: uuid.UUID
    stage_name: str
    gate_id: str | None = None
    position: int
    status: StageStatus
    progress_pct: int = 0
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error_message: str | None = None
    stage_metadata: dict[str, Any] = Field(default_factory=dict, alias="metadata")
    created_at: datetime | None = None

    @model_validator(mode="before")
    @classmethod
    def resolve_stage_metadata(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if "metadata" in data and "stage_metadata" not in data:
                data["stage_metadata"] = data["metadata"]
            elif "stage_metadata" in data and "metadata" not in data:
                data["metadata"] = data["stage_metadata"]
        return data

    @property
    def metadata(self) -> dict[str, Any]:
        return self.stage_metadata


class AutonomousRunPublicResponse(BaseModel):
    """Authoritative public run response. Strictly excludes internal fencing and lease fields."""

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: uuid.UUID
    project_id: uuid.UUID
    parent_run_id: uuid.UUID | None = None
    attempt_number: int = 1
    status: AutonomousRunStatus
    strategy: DeploymentStrategy
    configuration: dict[str, Any] = Field(default_factory=dict)
    progress_pct: int = 0
    current_stage: str | None = None
    error_summary: str | None = None
    primary_error: dict[str, Any] | None = None
    compensation_error: dict[str, Any] | None = None
    dispatch_status: str = "pending"
    dispatch_requested_at: datetime | None = None
    idempotency_key: str | None = None
    created_by: uuid.UUID
    created_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    stages: list[StagePublicResponse] = Field(default_factory=list)
    agent_connected: bool | None = None


class LogEntryPublicResponse(BaseModel):
    """Individual public log line entry."""

    model_config = ConfigDict(from_attributes=True)

    id: int | None = None
    run_id: uuid.UUID
    stage_name: str
    log_seq: int
    level: str = "INFO"
    message: str
    created_at: datetime | None = None


class PaginatedLogsResponse(BaseModel):
    """Cursor-paginated collection of execution logs."""

    model_config = ConfigDict(from_attributes=True)

    run_id: uuid.UUID
    logs: list[LogEntryPublicResponse] = Field(default_factory=list)
    has_more: bool = False
    next_log_seq: int | None = None
    total_lines: int | None = None
