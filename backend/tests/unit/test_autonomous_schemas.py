# SPDX-License-Identifier: FSL-1.1-ALv2
"""Unit tests for Autonomous Deployment Orchestrator public schemas and secret redaction.

Tests:
1. Strategy validation rules across all 4 strategies (required/forbidden configs).
2. Reserved environment variables rejection in Docker configuration.
3. Schema serialization and strict exclusion of internal fields (fence_token, etc.).
4. Secret redaction with synthetic token fragments.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from src.core.logging import redact_all_secrets, redact_secrets
from src.deployments.autonomous_schemas import (
    AutonomousRunPublicResponse,
    CreateAutonomousRunRequest,
    DeploymentStrategy,
    DockerConfigRequest,
    GitHubConfigRequest,
    LogEntryPublicResponse,
    PaginatedLogsResponse,
    StagePublicResponse,
    StageStatus,
    VercelConfigRequest,
)


def _valid_gh_config() -> GitHubConfigRequest:
    return GitHubConfigRequest(
        repository_mode="existing",
        repository_name="owner/repo",
        target_branch="main",
        commit_message="Automated deployment by ForgeOps",
    )


def _valid_vercel_config() -> VercelConfigRequest:
    return VercelConfigRequest(
        project_name="my-frontend-app",
        production_deploy=True,
    )


def _valid_docker_config() -> DockerConfigRequest:
    return DockerConfigRequest(
        port_bindings={"8080": 8080},
        environment_overrides={"APP_MODE": "production", "FEATURE_FLAG": "enabled"},
    )


# ---------------------------------------------------------------------------
# 1. Strategy Validation Tests
# ---------------------------------------------------------------------------


class TestStrategyValidation:
    """Validates cross-field strategy constraints for all four deployment strategies."""

    def test_docker_github_vercel_success_with_docker(self) -> None:
        req = CreateAutonomousRunRequest(
            strategy=DeploymentStrategy.DOCKER_GITHUB_VERCEL,
            github_config=_valid_gh_config(),
            vercel_config=_valid_vercel_config(),
            docker_config=_valid_docker_config(),
            idempotency_key="idemp-12345",
        )
        assert req.strategy == DeploymentStrategy.DOCKER_GITHUB_VERCEL
        assert req.github_config is not None
        assert req.vercel_config is not None
        assert req.docker_config is not None

    def test_docker_github_vercel_success_without_docker(self) -> None:
        req = CreateAutonomousRunRequest(
            strategy=DeploymentStrategy.DOCKER_GITHUB_VERCEL,
            github_config=_valid_gh_config(),
            vercel_config=_valid_vercel_config(),
        )
        assert req.docker_config is None

    def test_docker_github_vercel_missing_github_fails(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            CreateAutonomousRunRequest(
                strategy=DeploymentStrategy.DOCKER_GITHUB_VERCEL,
                vercel_config=_valid_vercel_config(),
            )
        assert "requires github_config" in str(exc_info.value)

    def test_docker_github_vercel_missing_vercel_fails(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            CreateAutonomousRunRequest(
                strategy=DeploymentStrategy.DOCKER_GITHUB_VERCEL,
                github_config=_valid_gh_config(),
            )
        assert "requires vercel_config" in str(exc_info.value)

    def test_docker_github_success(self) -> None:
        req = CreateAutonomousRunRequest(
            strategy=DeploymentStrategy.DOCKER_GITHUB,
            github_config=_valid_gh_config(),
            docker_config=_valid_docker_config(),
        )
        assert req.strategy == DeploymentStrategy.DOCKER_GITHUB
        assert req.vercel_config is None

    def test_docker_github_missing_github_fails(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            CreateAutonomousRunRequest(
                strategy=DeploymentStrategy.DOCKER_GITHUB,
                docker_config=_valid_docker_config(),
            )
        assert "requires github_config" in str(exc_info.value)

    def test_docker_github_forbidden_vercel_fails(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            CreateAutonomousRunRequest(
                strategy=DeploymentStrategy.DOCKER_GITHUB,
                github_config=_valid_gh_config(),
                vercel_config=_valid_vercel_config(),
            )
        assert "forbids vercel_config" in str(exc_info.value)

    def test_github_only_success(self) -> None:
        req = CreateAutonomousRunRequest(
            strategy=DeploymentStrategy.GITHUB_ONLY,
            github_config=_valid_gh_config(),
        )
        assert req.strategy == DeploymentStrategy.GITHUB_ONLY
        assert req.vercel_config is None
        assert req.docker_config is None

    def test_github_only_missing_github_fails(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            CreateAutonomousRunRequest(
                strategy=DeploymentStrategy.GITHUB_ONLY,
            )
        assert "requires github_config" in str(exc_info.value)

    def test_github_only_forbidden_vercel_fails(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            CreateAutonomousRunRequest(
                strategy=DeploymentStrategy.GITHUB_ONLY,
                github_config=_valid_gh_config(),
                vercel_config=_valid_vercel_config(),
            )
        assert "forbids vercel_config" in str(exc_info.value)

    def test_github_only_forbidden_docker_fails(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            CreateAutonomousRunRequest(
                strategy=DeploymentStrategy.GITHUB_ONLY,
                github_config=_valid_gh_config(),
                docker_config=_valid_docker_config(),
            )
        assert "forbids docker_config" in str(exc_info.value)

    def test_vercel_only_success(self) -> None:
        req = CreateAutonomousRunRequest(
            strategy=DeploymentStrategy.VERCEL_ONLY,
            vercel_config=_valid_vercel_config(),
        )
        assert req.strategy == DeploymentStrategy.VERCEL_ONLY
        assert req.github_config is None
        assert req.docker_config is None

    def test_vercel_only_missing_vercel_fails(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            CreateAutonomousRunRequest(
                strategy=DeploymentStrategy.VERCEL_ONLY,
            )
        assert "requires vercel_config" in str(exc_info.value)

    def test_vercel_only_forbidden_github_fails(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            CreateAutonomousRunRequest(
                strategy=DeploymentStrategy.VERCEL_ONLY,
                vercel_config=_valid_vercel_config(),
                github_config=_valid_gh_config(),
            )
        assert "forbids github_config" in str(exc_info.value)

    def test_vercel_only_forbidden_docker_fails(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            CreateAutonomousRunRequest(
                strategy=DeploymentStrategy.VERCEL_ONLY,
                vercel_config=_valid_vercel_config(),
                docker_config=_valid_docker_config(),
            )
        assert "forbids docker_config" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 2. Reserved Environment Variables Rejection Tests
# ---------------------------------------------------------------------------


class TestReservedEnvVars:
    """Verifies that platform-reserved environment variables cannot be overridden."""

    @pytest.mark.parametrize(
        "reserved_key",
        [
            "PORT",
            "port",
            "Port",
            "NODE_ENV",
            "node_env",
            "Node_Env",
            "FORGEOPS_LEASE_EPOCH",
            "FORGEOPS_INTERNAL_AGENT_TOKEN",
            "forgeops_worker_id",
            "FORGEOPS_CUSTOM_SETTING",
        ],
    )
    def test_docker_config_rejects_reserved_vars_directly(self, reserved_key: str) -> None:
        with pytest.raises(ValidationError) as exc_info:
            DockerConfigRequest(
                environment_overrides={reserved_key: "override_val"},
            )
        assert "reserved by the platform" in str(exc_info.value)

    @pytest.mark.parametrize("reserved_key", ["PORT", "NODE_ENV", "FORGEOPS_WORKER"])
    def test_create_run_request_rejects_reserved_vars_in_docker_config(self, reserved_key: str) -> None:
        with pytest.raises(ValidationError) as exc_info:
            CreateAutonomousRunRequest(
                strategy=DeploymentStrategy.DOCKER_GITHUB,
                github_config=_valid_gh_config(),
                docker_config=DockerConfigRequest(environment_overrides={reserved_key: "danger_val"}),
            )
        assert "reserved by the platform" in str(exc_info.value)

    def test_allowed_env_vars_accepted(self) -> None:
        cfg = DockerConfigRequest(
            environment_overrides={
                "SERVICE_PORT_INTERNAL": "9000",
                "NODE_OPTIONS": "--max-old-space-size=4096",
                "DATABASE_DSN": "custom_val",
                "FOO_BAR": "baz",
            }
        )
        assert len(cfg.environment_overrides) == 4


# ---------------------------------------------------------------------------
# 3. Schema Serialization & Internal Field Exclusion Tests
# ---------------------------------------------------------------------------


class TestSchemaSerializationAndExclusion:
    """Verifies schema serializations and strict exclusion of internal fencing fields."""

    def test_stage_status_canonical_states(self) -> None:
        expected_states = {
            "pending",
            "waiting",
            "running",
            "cancelling",
            "cancelled",
            "succeeded",
            "failed",
            "skipped",
            "rolled_back",
        }
        actual_states = {s.value for s in StageStatus}
        assert actual_states == expected_states

    def test_stage_public_response_serialization(self) -> None:
        stage_id = uuid.uuid4()
        run_id = uuid.uuid4()
        now = datetime.now(UTC)

        stage = StagePublicResponse(
            id=stage_id,
            run_id=run_id,
            stage_name="G1_blueprint_gate",
            gate_id="G1",
            position=1,
            status=StageStatus.SUCCEEDED,
            progress_pct=100,
            started_at=now,
            completed_at=now,
            metadata={"blueprint_type": "node_docker"},
        )

        dumped = stage.model_dump()
        assert dumped["id"] == stage_id
        assert dumped["stage_name"] == "G1_blueprint_gate"
        assert dumped["gate_id"] == "G1"
        assert dumped["status"] == "succeeded"
        assert stage.metadata == {"blueprint_type": "node_docker"}

    def test_autonomous_run_strictly_excludes_internal_fields(self) -> None:
        # Verify internal fields are strictly absent from model definition
        model_fields = AutonomousRunPublicResponse.model_fields
        assert "fence_token" not in model_fields
        assert "worker_id" not in model_fields
        assert "lease_expires_at" not in model_fields
        assert "payload_hash" not in model_fields

        run_id = uuid.uuid4()
        project_id = uuid.uuid4()
        creator_id = uuid.uuid4()
        now = datetime.now(UTC)

        # Build payload that simulates an ORM instance with internal fields present
        raw_db_row = {
            "id": run_id,
            "project_id": project_id,
            "parent_run_id": None,
            "attempt_number": 1,
            "status": "pending",
            "strategy": "docker_github_vercel",
            "configuration": {"target": "prod"},
            "progress_pct": 0,
            "current_stage": "G1_blueprint_gate",
            "error_summary": None,
            "primary_error": None,
            "compensation_error": None,
            "dispatch_status": "pending",
            "dispatch_requested_at": now,
            "idempotency_key": "unique-idem-key",
            "created_by": creator_id,
            "created_at": now,
            "started_at": None,
            "completed_at": None,
            "stages": [],
            "agent_connected": True,
            # Internal fields that MUST NOT leak
            "fence_token": 42,
            "worker_id": "worker-fenced-node-9",
            "lease_expires_at": now,
            "payload_hash": "a1b2c3d4e5f60718293a4b5c6d7e8f90",
            "log_sequence_counter": 12,
            "outbox_sequence_counter": 5,
        }

        response = AutonomousRunPublicResponse.model_validate(raw_db_row)

        # Attribute access checks
        assert not hasattr(response, "fence_token")
        assert not hasattr(response, "worker_id")
        assert not hasattr(response, "lease_expires_at")
        assert not hasattr(response, "payload_hash")

        dumped = response.model_dump()
        assert "fence_token" not in dumped
        assert "worker_id" not in dumped
        assert "lease_expires_at" not in dumped
        assert "payload_hash" not in dumped

        json_str = response.model_dump_json()
        assert "fence_token" not in json_str
        assert "worker_id" not in json_str
        assert "lease_expires_at" not in json_str
        assert "payload_hash" not in json_str

    def test_log_entry_and_paginated_response(self) -> None:
        run_id = uuid.uuid4()
        entry = LogEntryPublicResponse(
            id=101,
            run_id=run_id,
            stage_name="G4_build_gate",
            log_seq=1,
            level="INFO",
            message="Step 1: Docker build initiated",
        )
        assert entry.log_seq == 1
        assert entry.level == "INFO"

        paginated = PaginatedLogsResponse(
            run_id=run_id,
            logs=[entry],
            has_more=False,
            next_log_seq=2,
            total_lines=1,
        )
        assert len(paginated.logs) == 1
        assert paginated.next_log_seq == 2


# ---------------------------------------------------------------------------
# 4. Secret Redaction Tests (Synthetic Token Fragments)
# ---------------------------------------------------------------------------


class TestSecretRedaction:
    """Verifies that secret redaction scrubs tokens without triggering scanners."""

    def test_redact_github_pat_classic(self) -> None:
        # Assembled from fragments: gh + p_
        frag_prefix = "gh" + "p_"
        synthetic_token = frag_prefix + "testtoken1234567890abcdef1234"
        raw_msg = f"Cloning repository using token: {synthetic_token} from remote"

        scrubbed = redact_secrets(raw_msg)
        assert "testtoken" not in scrubbed
        assert "[REDACTED]" in scrubbed
        assert frag_prefix not in scrubbed

    def test_redact_github_pat_fine_grained(self) -> None:
        # Assembled from fragments: git + hub_ + pat_
        frag_prefix = "git" + "hub_" + "pat_"
        synthetic_token = frag_prefix + "99abcdef88112233445566778899"
        raw_msg = f"Auth token: {synthetic_token}"

        scrubbed = redact_secrets(raw_msg)
        assert "99abcdef" not in scrubbed
        assert "[REDACTED]" in scrubbed

    def test_redact_aws_akid(self) -> None:
        # Assembled from fragments: A + K + I + A
        frag_prefix = "A" + "K" + "I" + "A"
        synthetic_id = frag_prefix + "0123456789ABCDEF"
        raw_msg = f"Configured credentials id={synthetic_id}"

        scrubbed = redact_secrets(raw_msg)
        assert "0123456789ABCDEF" not in scrubbed
        assert "[REDACTED]" in scrubbed

    def test_redact_aws_temporary_akid(self) -> None:
        # Assembled from fragments: A + S + I + A
        frag_prefix = "A" + "S" + "I" + "A"
        synthetic_id = frag_prefix + "0123456789ABCDEF"
        raw_msg = f"Session credentials id={synthetic_id}"

        scrubbed = redact_secrets(raw_msg)
        assert "0123456789ABCDEF" not in scrubbed
        assert "[REDACTED]" in scrubbed

    def test_redact_google_token(self) -> None:
        # Assembled from fragments: A + I + z + a
        frag_prefix = "A" + "I" + "z" + "a"
        synthetic_key = frag_prefix + "SyA1234567890123456789012345678901"
        raw_msg = f"Cloud request with credential {synthetic_key}"

        scrubbed = redact_secrets(raw_msg)
        assert "SyA1234567890" not in scrubbed
        assert "[REDACTED]" in scrubbed

    def test_redact_slack_bot_token(self) -> None:
        # Assembled from fragments: x + o + x + b + -
        frag_prefix = "x" + "o" + "x" + "b" + "-"
        synthetic_token = frag_prefix + "123456789012-abcdef"
        raw_msg = f"Webhook notification token={synthetic_token}"

        scrubbed = redact_secrets(raw_msg)
        assert "123456789012" not in scrubbed
        assert "[REDACTED]" in scrubbed

    def test_redact_vercel_token(self) -> None:
        # Assembled from fragments: v + e + r + c + e + l + _
        frag_prefix = "v" + "e" + "r" + "c" + "e" + "l" + "_"
        synthetic_token = frag_prefix + "abcdef1234567890abcdef"
        raw_msg = f"Vercel deployment token: {synthetic_token}"

        scrubbed = redact_secrets(raw_msg)
        assert "abcdef1234567890" not in scrubbed
        assert "[REDACTED]" in scrubbed

    def test_redact_all_secrets_alias(self) -> None:
        frag_prefix = "gh" + "p_"
        synthetic_token = frag_prefix + "testtoken1234567890abcdef1234"
        raw_msg = f"Token: {synthetic_token}"

        assert redact_all_secrets(raw_msg) == redact_secrets(raw_msg)

    def test_clean_text_untouched(self) -> None:
        clean_text = "Building container image forgeops-app:latest on port 8080."
        assert redact_secrets(clean_text) == clean_text
