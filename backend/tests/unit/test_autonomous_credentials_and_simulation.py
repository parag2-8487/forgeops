# SPDX-License-Identifier: FSL-1.1-ALv2
"""Regression tests for credentials, authorization boundaries, simulation/live, and G7 sign-off."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from src.deployments.autonomous_credentials import resolve_deployment_credentials
from src.deployments.autonomous_gates import (
    evaluate_g7_verification,
    execute_github_release,
    execute_vercel_deploy,
)
from src.deployments.autonomous_models import (
    AutonomousDeployment,
    AutonomousDeploymentStage,
)
from src.deployments.autonomous_schemas import DeploymentStrategy
from src.deployments.autonomous_service import STAGE_G5_APPLY
from src.integrations.github_link import derive_link_key, seal_token

pytestmark = pytest.mark.asyncio

_TEST_PEPPER = "test-secret-pepper-for-forgeops-autonomous-unit-tests"


def _create_mock_run(
    strategy: DeploymentStrategy = DeploymentStrategy.DOCKER_GITHUB_VERCEL,
    user_id: uuid.UUID | None = None,
    simulation_mode: bool = False,
    dry_run: bool = False,
) -> AutonomousDeployment:
    uid = user_id or uuid.uuid4()
    rid = uuid.uuid4()
    cfg: dict[str, Any] = {
        "strategy": strategy.value,
        "github_config": {
            "repository_name": "owner/test-repo",
            "target_branch": "main",
            "publishing_mode": "direct_push",
        },
        "vercel_config": {
            "project_name": "test-vercel-proj",
            "production_deploy": True,
        },
        "docker_config": {
            "image_tag": "portfolio:0.1.0",
            "port_bindings": {"3000": 13080},
        },
    }
    if simulation_mode:
        cfg["simulation_mode"] = True
    if dry_run:
        cfg["dry_run"] = True

    run = AutonomousDeployment(
        id=rid,
        project_id=uuid.uuid4(),
        created_by=uid,
        strategy=strategy.value,
        configuration=cfg,
        status="pending",
        payload_hash="test-hash",
    )
    return run


class TestCredentialResolutionAndAuthorizationBoundaries:
    """Verifies credential retrieval, cryptographic unsealing, and authorization boundaries."""

    async def test_resolve_credentials_success_for_authorized_user(self) -> None:
        user_id = uuid.uuid4()
        run = _create_mock_run(user_id=user_id)

        key = derive_link_key(_TEST_PEPPER)
        test_gh_token = "gh" + "p_valid_token_12345"
        sealed_gh = seal_token(test_gh_token, user_id=user_id, key=key)
        sealed_vercel = seal_token("vercel_valid_token_67890", user_id=user_id, key=key)

        session = AsyncMock()

        # Mock query responses
        gh_mapping = MagicMock()
        gh_mapping.mappings.return_value.first.return_value = {
            "access_token_sealed": sealed_gh,
            "access_token_expires_at": datetime.now(UTC) + timedelta(hours=1),
            "refresh_token_sealed": None,
            "scopes": "repo",
            "github_login": "testuser",
            "github_user_id": 12345,
        }

        v_mapping = MagicMock()
        v_mapping.mappings.return_value.first.return_value = {
            "encrypted_value": sealed_vercel,
        }

        session.execute.side_effect = [gh_mapping, v_mapping]

        with patch("src.deployments.autonomous_credentials.get_settings") as mock_settings:
            mock_settings.return_value.envelope_pepper = _TEST_PEPPER
            creds = await resolve_deployment_credentials(session, run)

        assert creds["github_token"] == test_gh_token
        assert creds["github_login"] == "testuser"
        assert creds["vercel_token"] == "vercel_valid_token_67890"
        assert creds["credential_status"]["github"] == "ready"
        assert creds["credential_status"]["vercel"] == "ready"

    async def test_authorization_boundary_cross_user_isolation(self) -> None:
        """Verifies that an encrypted token belonging to user A cannot be decrypted by user B."""
        user_a = uuid.uuid4()
        user_b = uuid.uuid4()
        run_user_b = _create_mock_run(user_id=user_b)

        key = derive_link_key(_TEST_PEPPER)
        # Token sealed with user_a as AAD
        sealed_for_user_a = seal_token("secret_token_a", user_id=user_a, key=key)

        session = AsyncMock()
        gh_mapping = MagicMock()
        gh_mapping.mappings.return_value.first.return_value = {
            "access_token_sealed": sealed_for_user_a,
            "access_token_expires_at": None,
            "refresh_token_sealed": None,
            "github_login": "user_a",
        }
        session.execute.side_effect = [gh_mapping]

        with patch("src.deployments.autonomous_credentials.get_settings") as mock_settings:
            mock_settings.return_value.envelope_pepper = _TEST_PEPPER
            creds = await resolve_deployment_credentials(session, run_user_b)

        # Cross-user decryption MUST fail due to AES-GCM tag mismatch
        assert creds["github_token"] is None
        assert "unseal_error" in creds["credential_status"]["github"]

    async def test_missing_credentials_reported_explicitly(self) -> None:
        user_id = uuid.uuid4()
        run = _create_mock_run(user_id=user_id)

        session = AsyncMock()
        empty_mapping = MagicMock()
        empty_mapping.mappings.return_value.first.return_value = None
        session.execute.return_value = empty_mapping

        with patch("src.deployments.autonomous_credentials.get_settings") as mock_settings:
            mock_settings.return_value.envelope_pepper = _TEST_PEPPER
            creds = await resolve_deployment_credentials(session, run)

        assert creds["github_token"] is None
        assert creds["vercel_token"] is None
        assert creds["credential_status"]["github"] == "missing_credentials"
        assert creds["credential_status"]["vercel"] == "missing_credentials"

    async def test_expired_github_token_rejected(self) -> None:
        user_id = uuid.uuid4()
        run = _create_mock_run(user_id=user_id)

        key = derive_link_key(_TEST_PEPPER)
        sealed_gh = seal_token("expired_token", user_id=user_id, key=key)

        session = AsyncMock()
        gh_mapping = MagicMock()
        gh_mapping.mappings.return_value.first.return_value = {
            "access_token_sealed": sealed_gh,
            "access_token_expires_at": datetime.now(UTC) - timedelta(minutes=10),
            "refresh_token_sealed": None,
            "github_login": "testuser",
        }
        session.execute.side_effect = [gh_mapping, MagicMock(mappings=lambda: MagicMock(first=lambda: None))]

        with patch("src.deployments.autonomous_credentials.get_settings") as mock_settings:
            mock_settings.return_value.envelope_pepper = _TEST_PEPPER
            creds = await resolve_deployment_credentials(session, run)

        assert creds["github_token"] is None
        assert creds["credential_status"]["github"] == "expired_token"


class TestSimulationVsLiveDistinction:
    """Verifies that live deployments require genuine credentials and never emit synthetic IDs."""

    async def test_live_github_release_fails_when_credentials_missing(self) -> None:
        session = AsyncMock()
        run = _create_mock_run(simulation_mode=False)

        res = await execute_github_release(session, run, context={"github_token": None})

        assert res.passed is False
        assert res.status == "failed"
        assert "Missing GitHub credentials" in res.message
        assert res.details.get("error") == "missing_credentials"
        assert res.details.get("live") is True
        assert res.details.get("simulated") is False

    async def test_live_vercel_deploy_fails_when_credentials_missing(self) -> None:
        session = AsyncMock()
        run = _create_mock_run(simulation_mode=False)

        res = await execute_vercel_deploy(session, run, context={"vercel_token": None})

        assert res.passed is False
        assert res.status == "failed"
        assert "Missing Vercel Access Token" in res.message
        assert res.details.get("error") == "missing_credentials"
        assert res.details.get("live") is True
        assert res.details.get("simulated") is False

    async def test_simulated_github_release_never_generates_synthetic_sha(self) -> None:
        session = AsyncMock()
        run = _create_mock_run(simulation_mode=True)

        res = await execute_github_release(session, run)

        assert res.passed is True
        assert res.status == "succeeded"
        assert "[SIMULATION]" in res.message
        assert res.details.get("simulated") is True
        assert res.details.get("live") is False
        # Synthetic SHA pattern (sha_...) MUST NOT be generated!
        assert res.details.get("commit_sha") is None

    async def test_simulated_vercel_deploy_never_generates_synthetic_id(self) -> None:
        session = AsyncMock()
        run = _create_mock_run(simulation_mode=True)

        res = await execute_vercel_deploy(session, run)

        assert res.passed is True
        assert res.status == "succeeded"
        assert "[SIMULATION]" in res.message
        assert res.details.get("simulated") is True
        assert res.details.get("live") is False
        # Synthetic deployment ID pattern (dpl_...) MUST NOT be generated!
        assert res.details.get("deployment_id") is None


class TestG7SignOffGuardrails:
    """Verifies G7 final sign-off requirements for live vs simulated runs."""

    async def test_g7_fails_live_run_if_required_target_was_simulated(self) -> None:
        session = AsyncMock()
        run = _create_mock_run(simulation_mode=False)

        # Stage GH was simulated
        st_gh = AutonomousDeploymentStage(
            id=uuid.uuid4(),
            run_id=run.id,
            stage_name="github_release",
            position=1,
            status="succeeded",
            stage_metadata={"live": False, "simulated": True, "commit_sha": None},
        )
        st_v = AutonomousDeploymentStage(
            id=uuid.uuid4(),
            run_id=run.id,
            stage_name="vercel_deploy",
            position=2,
            status="succeeded",
            stage_metadata={"live": True, "simulated": False, "deployment_id": "real-dpl-123"},
        )
        st_d = AutonomousDeploymentStage(
            id=uuid.uuid4(),
            run_id=run.id,
            stage_name="apply_startup",
            position=3,
            status="succeeded",
            stage_metadata={"live": True, "simulated": False, "container_id": "real-cid-123"},
        )
        run.stages = [st_gh, st_v, st_d]

        g7 = await evaluate_g7_verification(session, run)

        # G7 must refuse to pass live deployment when GitHub was simulated!
        assert g7.overall_passed is False
        assert g7.target_results["github"] == "failed"
        assert g7.details.get("live") is True

    async def test_g7_passes_when_all_targets_genuinely_verified_live(self) -> None:
        session = AsyncMock()
        run = _create_mock_run(simulation_mode=False)

        st_gh = AutonomousDeploymentStage(
            id=uuid.uuid4(),
            run_id=run.id,
            stage_name="github_release",
            position=1,
            status="succeeded",
            stage_metadata={"live": True, "simulated": False, "commit_sha": "0123456789abcdef0123456789abcdef01234567"},
        )
        st_v = AutonomousDeploymentStage(
            id=uuid.uuid4(),
            run_id=run.id,
            stage_name="vercel_deploy",
            position=2,
            status="succeeded",
            stage_metadata={"live": True, "simulated": False, "deployment_id": "dpl_live_deployment_id"},
        )
        st_d = AutonomousDeploymentStage(
            id=uuid.uuid4(),
            run_id=run.id,
            stage_name=STAGE_G5_APPLY,
            position=3,
            status="succeeded",
            stage_metadata={"live": True, "simulated": False, "container_id": "container_live_123"},
        )
        run.stages = [st_gh, st_v, st_d]

        # Context overrides remote network calls for pure logic unit test
        g7 = await evaluate_g7_verification(
            session,
            run,
            context={"target_results_override": {"github": "verified", "vercel": "verified", "docker": "verified"}},
        )

        assert g7.overall_passed is True
        assert g7.target_results["github"] == "verified"
        assert g7.target_results["vercel"] == "verified"
        assert g7.target_results["docker"] == "verified"
        assert g7.details.get("live") is True

    async def test_g7_marks_simulated_in_explicit_simulation_mode(self) -> None:
        session = AsyncMock()
        run = _create_mock_run(simulation_mode=True)

        g7 = await evaluate_g7_verification(session, run)

        assert g7.overall_passed is True
        assert "[SIMULATION]" in g7.message
        assert g7.details.get("simulated") is True
        assert g7.details.get("live") is False
        assert g7.details.get("execution_mode") == "simulation"
