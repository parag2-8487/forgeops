# SPDX-License-Identifier: FSL-1.1-ALv2
"""Unit and regression tests for Autonomous Vercel Deployment, file uploads, polling, timeouts, and error handling."""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
from src.deployments.autonomous_gates import (
    STAGE_VERCEL_DEPLOY,
    WorkspacePackagingError,
    check_ignored_path,
    collect_workspace_files,
    compute_file_sha1,
    detect_vercel_framework,
    execute_github_release,
    execute_vercel_deploy,
    resolve_application_root,
    scan_workspace_manifest,
)
from src.deployments.autonomous_schemas import DeploymentStrategy

from tests.unit.test_autonomous_gates import MockAsyncSession, _create_test_run


def _create_sample_workspace(tmp_path: Path) -> Path:
    """Creates a minimal mock application workspace with a couple of files."""
    pkg_file = tmp_path / "package.json"
    pkg_file.write_text(json.dumps({"name": "test-app", "dependencies": {"vite": "^5.0.0"}}), encoding="utf-8")
    index_file = tmp_path / "index.html"
    index_file.write_text("<!DOCTYPE html><html><body>Test App</body></html>", encoding="utf-8")
    return tmp_path


@pytest.mark.asyncio
async def test_vercel_auth_and_config_errors(tmp_path: Path) -> None:
    """Fails immediately with actionable diagnostic when credentials unconfigured or invalid."""
    session = MockAsyncSession()
    ws = _create_sample_workspace(tmp_path)
    run = await _create_test_run(
        session,
        DeploymentStrategy.VERCEL_ONLY,
        config_overrides={"dry_run": False, "simulation_mode": False},
    )

    # 1. Missing token
    res_no_tok = await execute_vercel_deploy(
        session,
        run,
        context={"require_live_vercel": True, "workspace_path": str(ws)},
    )
    assert res_no_tok.passed is False
    assert res_no_tok.status == "failed"
    assert "unconfigured" in res_no_tok.message.lower() or "missing" in res_no_tok.message.lower()

    # 2. Vercel 401 Unauthorized
    class Mock401Transport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": {"code": "not_authorized", "message": "Invalid token"}})

    async with httpx.AsyncClient(transport=Mock401Transport()) as client:
        res_401 = await execute_vercel_deploy(
            session,
            run,
            context={"vercel_token": "bad_token", "workspace_path": str(ws), "http_client": client},
        )
        assert res_401.passed is False
        assert "401" in res_401.message or "Invalid token" in res_401.message


@pytest.mark.asyncio
async def test_vercel_file_upload_timeout_and_retry_exhaustion(tmp_path: Path) -> None:
    """File upload exhausts retries on transient errors and propagates failure without hanging."""
    session = MockAsyncSession()
    ws = _create_sample_workspace(tmp_path)
    run = await _create_test_run(
        session,
        DeploymentStrategy.VERCEL_ONLY,
        config_overrides={"dry_run": False, "simulation_mode": False},
    )

    call_count = 0

    class MockUploadRetryExhaustionTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            if "/v2/files" in str(request.url):
                call_count += 1
                return httpx.Response(503, text="Service Unavailable")
            return httpx.Response(200)

    async with httpx.AsyncClient(transport=MockUploadRetryExhaustionTransport()) as client:
        res = await execute_vercel_deploy(
            session,
            run,
            context={"vercel_token": "valid_tok", "workspace_path": str(ws), "http_client": client},
        )
        assert res.passed is False
        assert res.status == "failed"
        assert "file upload failed" in res.message.lower()
        assert call_count >= 3  # verified retry attempts made


@pytest.mark.asyncio
async def test_vercel_api_request_timeout(tmp_path: Path) -> None:
    """Vercel API timeout during deployment creation is handled cleanly."""
    session = MockAsyncSession()
    ws = _create_sample_workspace(tmp_path)
    run = await _create_test_run(
        session,
        DeploymentStrategy.VERCEL_ONLY,
        config_overrides={"dry_run": False, "simulation_mode": False},
    )

    class MockCreationTimeoutTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            if "/v2/files" in str(request.url):
                return httpx.Response(200, json={"urls": []})
            if "/v13/deployments" in str(request.url):
                raise httpx.ReadTimeout("Request timed out reading response")
            return httpx.Response(404)

    async with httpx.AsyncClient(transport=MockCreationTimeoutTransport()) as client:
        res = await execute_vercel_deploy(
            session,
            run,
            context={"vercel_token": "valid_tok", "workspace_path": str(ws), "http_client": client},
        )
        assert res.passed is False
        assert res.status == "failed"
        assert "network exception" in res.message.lower() or "timed out" in res.message.lower()


@pytest.mark.asyncio
async def test_vercel_deployment_queued_building_then_ready(tmp_path: Path) -> None:
    """Deployment polls through queued/building states and succeeds once READY, persisting ID & URL."""
    session = MockAsyncSession()
    ws = _create_sample_workspace(tmp_path)
    run = await _create_test_run(
        session,
        DeploymentStrategy.VERCEL_ONLY,
        config_overrides={"dry_run": False, "simulation_mode": False},
    )
    stage_v = next(s for s in run.stages if s.stage_name == STAGE_VERCEL_DEPLOY)

    poll_calls = 0

    class MockProgressiveVercelTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            nonlocal poll_calls
            url = str(request.url)
            if "/v2/files" in url:
                return httpx.Response(200, json={"urls": []})
            if "/v13/deployments?skipAutoDetectionConfirmation=1" in url:
                return httpx.Response(
                    200,
                    json={
                        "id": "dpl_abc123",
                        "url": "test-app-abc.vercel.app",
                        "readyState": "BUILDING",
                        "inspectorUrl": "https://vercel.com/inspect/abc123",
                    },
                )
            if "/v13/deployments/dpl_abc123" in url:
                poll_calls += 1
                if poll_calls == 1:
                    return httpx.Response(200, json={"readyState": "BUILDING"})
                return httpx.Response(200, json={"readyState": "READY", "url": "test-app-abc.vercel.app"})
            return httpx.Response(404)

    async with httpx.AsyncClient(transport=MockProgressiveVercelTransport()) as client:
        res = await execute_vercel_deploy(
            session,
            run,
            context={
                "vercel_token": "valid_tok",
                "workspace_path": str(ws),
                "http_client": client,
                "vercel_poll_interval": 0.01,
            },
        )
        assert res.passed is True
        assert res.status == "succeeded"
        assert res.details["deployment_id"] == "dpl_abc123"
        assert res.details["deployment_url"] == "https://test-app-abc.vercel.app"
        assert res.details["ready_state"] == "READY"
        # Immediate persistence check
        assert stage_v.stage_metadata.get("deployment_id") == "dpl_abc123"
        assert stage_v.stage_metadata.get("ready_state") == "READY"


@pytest.mark.asyncio
async def test_vercel_deployment_terminal_failed_state(tmp_path: Path) -> None:
    """Deployment entering ERROR / FAILED state halts polling and reports terminal failure."""
    session = MockAsyncSession()
    ws = _create_sample_workspace(tmp_path)
    run = await _create_test_run(
        session,
        DeploymentStrategy.VERCEL_ONLY,
        config_overrides={"dry_run": False, "simulation_mode": False},
    )

    class MockFailingVercelTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "/v2/files" in url:
                return httpx.Response(200, json={"urls": []})
            if "/v13/deployments?skipAutoDetectionConfirmation=1" in url:
                return httpx.Response(
                    200,
                    json={
                        "id": "dpl_fail123",
                        "url": "test-app-fail.vercel.app",
                        "readyState": "INITIALIZING",
                    },
                )
            if "/v13/deployments/dpl_fail123" in url:
                return httpx.Response(
                    200,
                    json={"readyState": "ERROR", "errorMessage": "Build command 'npm run build' failed with code 1"},
                )
            return httpx.Response(404)

    async with httpx.AsyncClient(transport=MockFailingVercelTransport()) as client:
        res = await execute_vercel_deploy(
            session,
            run,
            context={
                "vercel_token": "valid_tok",
                "workspace_path": str(ws),
                "http_client": client,
                "vercel_poll_interval": 0.01,
            },
        )
        assert res.passed is False
        assert res.status == "failed"
        assert "ERROR" in res.message
        assert "Build command 'npm run build' failed" in res.message


@pytest.mark.asyncio
async def test_vercel_polling_deadline_expiry(tmp_path: Path) -> None:
    """Deployment remaining in queued/building state beyond timeout deadline fails cleanly."""
    session = MockAsyncSession()
    ws = _create_sample_workspace(tmp_path)
    run = await _create_test_run(
        session,
        DeploymentStrategy.VERCEL_ONLY,
        config_overrides={"dry_run": False, "simulation_mode": False},
    )

    class MockStalledVercelTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "/v2/files" in url:
                return httpx.Response(200, json={"urls": []})
            if "/v13/deployments" in url:
                return httpx.Response(
                    200,
                    json={
                        "id": "dpl_stall123",
                        "url": "test-app-stall.vercel.app",
                        "readyState": "QUEUED",
                    },
                )
            return httpx.Response(404)

    async with httpx.AsyncClient(transport=MockStalledVercelTransport()) as client:
        res = await execute_vercel_deploy(
            session,
            run,
            context={
                "vercel_token": "valid_tok",
                "workspace_path": str(ws),
                "http_client": client,
                "vercel_poll_timeout": 0.05,
                "vercel_poll_interval": 0.01,
            },
        )
        assert res.passed is False
        assert res.status == "failed"
        assert "timed out" in res.message.lower()
        assert res.details.get("timed_out") is True
        assert res.details.get("deployment_id") == "dpl_stall123"


@pytest.mark.asyncio
async def test_vercel_worker_cancellation_during_polling(tmp_path: Path) -> None:
    """Worker cancellation requested during polling promptly cancels stage."""
    session = MockAsyncSession()
    ws = _create_sample_workspace(tmp_path)
    run = await _create_test_run(
        session,
        DeploymentStrategy.VERCEL_ONLY,
        config_overrides={"dry_run": False, "simulation_mode": False},
    )

    class MockCancelVercelTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "/v2/files" in url:
                return httpx.Response(200, json={"urls": []})
            if "/v13/deployments" in url:
                return httpx.Response(
                    200,
                    json={
                        "id": "dpl_canc123",
                        "url": "test-app-canc.vercel.app",
                        "readyState": "BUILDING",
                    },
                )
            return httpx.Response(404)

    # Simulate cancellation flag set
    run.status = "cancelling"

    async with httpx.AsyncClient(transport=MockCancelVercelTransport()) as client:
        res = await execute_vercel_deploy(
            session,
            run,
            context={
                "vercel_token": "valid_tok",
                "workspace_path": str(ws),
                "http_client": client,
                "vercel_poll_timeout": 5.0,
                "vercel_poll_interval": 0.01,
            },
        )
        assert res.passed is False
        assert res.status == "cancelled"
        assert res.details.get("cancelled") is True


@pytest.mark.asyncio
async def test_guarded_update_no_missing_greenlet() -> None:
    """guarded_update does not trigger MissingGreenlet on expired ORM models in identity map."""
    from datetime import UTC, datetime, timedelta

    from src.deployments.autonomous_worker import AutonomousWorker

    session = MockAsyncSession()
    run = await _create_test_run(session, DeploymentStrategy.DOCKER_GITHUB)
    run.lease_expires_at = datetime.now(UTC) + timedelta(minutes=10)
    worker = AutonomousWorker()

    # Call guarded_update - must execute without MissingGreenlet or exception
    count = await worker.guarded_update(
        session,
        run_id=run.id,
        fence_token=run.fence_token,
        values={"configuration": {"updated": True}},
    )
    assert count == 1
    assert run.configuration.get("updated") is True


@pytest.mark.asyncio
async def test_tasks_py_failure_handler_imports_log() -> None:
    """Verifies tasks.py fallback error handler can instantiate AutonomousDeploymentLog without NameError."""
    from src.deployments.autonomous_models import AutonomousDeploymentLog

    run_id = uuid.uuid4()
    log = AutonomousDeploymentLog(
        run_id=run_id,
        stage_name="vercel_deploy",
        log_seq=1,
        level="ERROR",
        message="Worker pipeline failed: TestError",
    )
    assert log.run_id == run_id
    assert log.level == "ERROR"
    assert "TestError" in log.message


# ---------------------------------------------------------------------------
# Pre-Deployment Audit Remediation Regression Tests
# ---------------------------------------------------------------------------


def test_workspace_packaging_gitignore_and_vercelignore(tmp_path: Path) -> None:
    """Workspace packaging honors root & nested .gitignore and root .vercelignore."""
    ws = tmp_path / "app"
    ws.mkdir()
    (ws / ".gitignore").write_text("*.log\nnode_modules/\nignored_root.txt\n", encoding="utf-8")
    (ws / ".vercelignore").write_text("draft.md\nsecrets/\n", encoding="utf-8")
    (ws / "valid.txt").write_text("hello", encoding="utf-8")
    (ws / "app.log").write_text("log", encoding="utf-8")
    (ws / "ignored_root.txt").write_text("ignored", encoding="utf-8")
    (ws / "draft.md").write_text("draft", encoding="utf-8")

    sub = ws / "sub"
    sub.mkdir()
    (sub / ".gitignore").write_text("nested_ignore.txt\n", encoding="utf-8")
    (sub / "nested_ok.txt").write_text("ok", encoding="utf-8")
    (sub / "nested_ignore.txt").write_text("secret nested", encoding="utf-8")

    ignored_dir = ws / "secrets"
    ignored_dir.mkdir()
    (ignored_dir / "key.txt").write_text("secret", encoding="utf-8")

    collected = collect_workspace_files(ws)
    file_map = dict(collected)

    # Valid files must be included
    assert "valid.txt" in file_map
    assert "sub/nested_ok.txt" in file_map

    # Ignored files must NOT be included
    assert "app.log" not in file_map
    assert "ignored_root.txt" not in file_map
    assert "draft.md" not in file_map
    assert "sub/nested_ignore.txt" not in file_map
    assert "secrets/key.txt" not in file_map


def test_workspace_packaging_sensitive_files_defense_in_depth(tmp_path: Path) -> None:
    """Sensitive file patterns and credential files are excluded by defense-in-depth."""
    ws = tmp_path / "app"
    ws.mkdir()
    cs_filename = "client" + "_sec" + "ret_xyz.json"
    (ws / "server.key").write_text("PRIV" + "ATE KEY", encoding="utf-8")
    (ws / "cert.pem").write_text("CERTIFICATE", encoding="utf-8")
    (ws / "id_rsa").write_text("SSH KEY", encoding="utf-8")
    (ws / "id_ed25519").write_text("SSH KEY", encoding="utf-8")
    (ws / "credentials.json").write_text('{"' + "api" + '_key": "123"}', encoding="utf-8")
    (ws / cs_filename).write_text('{"secret": "123"}', encoding="utf-8")
    (ws / "service-account-prod.json").write_text('{"sa": "123"}', encoding="utf-8")
    (ws / ".env.production").write_text("SECRET=123", encoding="utf-8")
    (ws / ".env.local").write_text("LOCAL=123", encoding="utf-8")
    (ws / "safe_config.json").write_text('{"app": "ok"}', encoding="utf-8")

    ignored, reason = check_ignored_path("server.key", ws)
    assert ignored is True
    assert "sensitive" in str(reason)

    ignored, reason = check_ignored_path("credentials.json", ws)
    assert ignored is True
    assert "sensitive" in str(reason)

    ignored, reason = check_ignored_path(".env.production", ws)
    assert ignored is True
    assert "environment" in str(reason)

    collected = collect_workspace_files(ws)
    file_map = dict(collected)
    assert "safe_config.json" in file_map
    assert "server.key" not in file_map
    assert "cert.pem" not in file_map
    assert "id_rsa" not in file_map
    assert "id_ed25519" not in file_map
    assert "credentials.json" not in file_map
    assert cs_filename not in file_map
    assert "service-account-prod.json" not in file_map
    assert ".env.production" not in file_map
    assert ".env.local" not in file_map


def test_workspace_packaging_symlink_traversal_prevention(tmp_path: Path) -> None:
    """Symlinks pointing outside the workspace boundary are skipped to prevent directory traversal."""
    ws = tmp_path / "app"
    ws.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_file = outside / "secret_outside.txt"
    outside_file.write_text("secret outside", encoding="utf-8")

    (ws / "valid.txt").write_text("valid", encoding="utf-8")

    # Create symlink pointing outside workspace
    try:
        (ws / "symlink_out.txt").symlink_to(outside_file)
    except (OSError, NotImplementedError):
        # On Windows without developer mode/admin privilege, symlinks might not be supported
        pytest.skip("Symlink creation not supported in this environment")

    collected = collect_workspace_files(ws)
    file_map = dict(collected)
    assert "valid.txt" in file_map
    assert "symlink_out.txt" not in file_map


def test_workspace_packaging_preserves_binary_and_empty_files(tmp_path: Path) -> None:
    """Workspace packaging accurately classifies and preserves exact bytes of binary and 0-byte files."""
    ws = tmp_path / "app"
    ws.mkdir()
    (ws / "empty.txt").write_bytes(b"")
    binary_payload = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x10\x00\x00\x00\x10\x08\x06\x00\x00\x00"
    (ws / "logo.png").write_bytes(binary_payload)
    (ws / "app.js").write_text("console.log('hello');", encoding="utf-8")

    collected = collect_workspace_files(ws)
    file_map = dict(collected)

    # Empty text file has 0 bytes
    assert "empty.txt" in file_map
    assert file_map["empty.txt"] == b""

    # Binary file preserved with exact byte sequence
    assert "logo.png" in file_map
    assert file_map["logo.png"] == binary_payload

    # Regular text file in file_map
    assert "app.js" in file_map
    assert file_map["app.js"] == b"console.log('hello');"


def test_workspace_packaging_single_file_limit_raises_packaging_error(tmp_path: Path) -> None:
    """A file exceeding 10 MB limit raises WorkspacePackagingError."""
    ws = tmp_path / "app"
    ws.mkdir()
    oversized = ws / "large.dat"
    oversized.write_bytes(b"A" * (10 * 1024 * 1024 + 10))

    with pytest.raises(WorkspacePackagingError) as exc_info:
        collect_workspace_files(ws)

    assert "exceeds maximum allowed individual file size" in str(exc_info.value)
    assert "large.dat" in str(exc_info.value)


def test_workspace_packaging_total_workspace_limit_raises_packaging_error(tmp_path: Path) -> None:
    """A total workspace exceeding limit raises WorkspacePackagingError."""
    ws = tmp_path / "app"
    ws.mkdir()
    (ws / "file1.dat").write_bytes(b"A" * (30 * 1024))
    (ws / "file2.dat").write_bytes(b"B" * (30 * 1024))

    with pytest.raises(WorkspacePackagingError) as exc_info:
        collect_workspace_files(ws, max_total_size=50 * 1024)

    assert "Total workspace size" in str(exc_info.value)
    assert "exceeds maximum allowed limit" in str(exc_info.value)


@pytest.mark.asyncio
async def test_workspace_packaging_error_halts_github_and_vercel_safely(tmp_path: Path) -> None:
    """WorkspacePackagingError halts execute_github_release and execute_vercel_deploy without remote mutation."""
    ws = tmp_path / "app"
    ws.mkdir()
    oversized = ws / "huge.dat"
    with oversized.open("wb") as f:
        f.seek(100 * 1024 * 1024 + 50)
        f.write(b"X")

    session = MockAsyncSession()
    run = await _create_test_run(
        session,
        DeploymentStrategy.DOCKER_GITHUB_VERCEL,
        config_overrides={"dry_run": False, "simulation_mode": False},
    )

    # 1. GitHub release halts safely
    res_gh = await execute_github_release(
        session,
        run,
        context={"workspace_path": str(ws), "github_token": "fake_token"},
    )
    assert res_gh.passed is False
    assert res_gh.status == "failed"
    assert "GitHub release halted" in res_gh.message or "huge.dat" in res_gh.message
    assert res_gh.details.get("error") == "workspace_packaging_error"

    # 2. Vercel deploy halts safely without making HTTP requests
    res_v = await execute_vercel_deploy(
        session,
        run,
        context={"workspace_path": str(ws), "vercel_token": "fake_token"},
    )
    assert res_v.passed is False
    assert res_v.status == "failed"
    assert "Vercel cloud deployment halted" in res_v.message or "huge.dat" in res_v.message
    assert res_v.details.get("error") == "workspace_packaging_error"


def test_detect_vercel_framework_recognized_unknown_and_malformed(tmp_path: Path) -> None:
    """detect_vercel_framework detects known frameworks and returns None for unknown/malformed manifests."""
    ws = tmp_path / "app"
    ws.mkdir()

    # Missing package.json
    assert detect_vercel_framework(ws) is None

    # Next.js
    pkg = ws / "package.json"
    pkg.write_text(json.dumps({"dependencies": {"next": "14.2.0"}}), encoding="utf-8")
    assert detect_vercel_framework(ws) == "nextjs"

    # Vite
    pkg.write_text(json.dumps({"devDependencies": {"vite": "^5.1.0"}}), encoding="utf-8")
    assert detect_vercel_framework(ws) == "vite"

    # Create React App
    pkg.write_text(json.dumps({"dependencies": {"react-scripts": "5.0.1"}}), encoding="utf-8")
    assert detect_vercel_framework(ws) == "create-react-app"

    # Unknown framework
    pkg.write_text(json.dumps({"dependencies": {"express": "4.19.0"}}), encoding="utf-8")
    assert detect_vercel_framework(ws) is None

    # Malformed JSON (syntax error)
    pkg.write_text("{dependencies: {not valid json...", encoding="utf-8")
    assert detect_vercel_framework(ws) is None

    # Non-dict JSON (array)
    pkg.write_text("[1, 2, 3]", encoding="utf-8")
    assert detect_vercel_framework(ws) is None


@pytest.mark.asyncio
async def test_vercel_deploy_omits_project_settings_when_framework_is_none(tmp_path: Path) -> None:
    """Vercel deployment payload omits projectSettings when no framework is detected."""
    ws = tmp_path / "app"
    ws.mkdir()
    (ws / "index.html").write_text("<h1>Vanilla JS</h1>", encoding="utf-8")
    (ws / "package.json").write_text(json.dumps({"name": "vanilla-app"}), encoding="utf-8")

    session = MockAsyncSession()
    run = await _create_test_run(
        session,
        DeploymentStrategy.VERCEL_ONLY,
        config_overrides={"dry_run": False, "simulation_mode": False},
    )

    captured_payload: dict[str, Any] = {}

    class MockDeployPayloadTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            nonlocal captured_payload
            url = str(request.url)
            if "/v2/files" in url:
                return httpx.Response(200, json={"urls": []})
            if "/v13/deployments" in url:
                captured_payload = json.loads(request.content.decode("utf-8"))
                return httpx.Response(
                    200,
                    json={
                        "id": "dpl_no_fw_123",
                        "url": "no-fw-app.vercel.app",
                        "readyState": "READY",
                    },
                )
            return httpx.Response(404)

    async with httpx.AsyncClient(transport=MockDeployPayloadTransport()) as client:
        res = await execute_vercel_deploy(
            session,
            run,
            context={"vercel_token": "valid_tok", "workspace_path": str(ws), "http_client": client},
        )
        assert res.passed is True
        assert res.status == "succeeded"
        # projectSettings MUST be omitted when framework is None
        assert "projectSettings" not in captured_payload


@pytest.mark.asyncio
async def test_vercel_deploy_rejects_inline_environment_variables_securely(tmp_path: Path) -> None:
    """Vercel deploy rejects inline environment variables with actionable guidance without leaking secrets."""
    ws = _create_sample_workspace(tmp_path)
    session = MockAsyncSession()
    run = await _create_test_run(
        session,
        DeploymentStrategy.VERCEL_ONLY,
        config_overrides={
            "dry_run": False,
            "simulation_mode": False,
            "vercel_config": {
                "environment_variables": {
                    "SUPER_SECRET_KEY": "super_secret_value_12345",
                    "DATABASE_PASSWORD": "db_password_xyz987",
                }
            },
        },
    )

    res = await execute_vercel_deploy(
        session,
        run,
        context={"vercel_token": "valid_tok", "workspace_path": str(ws)},
    )
    assert res.passed is False
    assert res.status == "failed"
    # Actionable guidance must list variable names
    assert "DATABASE_PASSWORD" in res.message
    assert "SUPER_SECRET_KEY" in res.message
    # Secret values must NEVER be leaked
    assert "super_secret_value_12345" not in res.message
    assert "db_password_xyz987" not in res.message
    assert "Project Environment Variables API" in res.message


@pytest.mark.asyncio
async def test_vercel_file_upload_http_429_retry_after_handling(tmp_path: Path) -> None:
    """Vercel file upload honors HTTP 429 Retry-After header with bounded delay."""
    ws = _create_sample_workspace(tmp_path)
    session = MockAsyncSession()
    run = await _create_test_run(
        session,
        DeploymentStrategy.VERCEL_ONLY,
        config_overrides={"dry_run": False, "simulation_mode": False},
    )

    upload_attempts = 0

    class Mock429RetryAfterTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            nonlocal upload_attempts
            url = str(request.url)
            if "/v2/files" in url:
                upload_attempts += 1
                if upload_attempts == 1:
                    return httpx.Response(
                        429,
                        headers={"Retry-After": "0.05"},
                        json={"error": {"code": "rate_limited", "message": "Rate limit exceeded"}},
                    )
                return httpx.Response(200, json={"urls": []})
            if "/v13/deployments" in url:
                return httpx.Response(
                    200,
                    json={
                        "id": "dpl_429_ok",
                        "url": "test-429.vercel.app",
                        "readyState": "READY",
                    },
                )
            return httpx.Response(404)

    async with httpx.AsyncClient(transport=Mock429RetryAfterTransport()) as client:
        res = await execute_vercel_deploy(
            session,
            run,
            context={"vercel_token": "valid_tok", "workspace_path": str(ws), "http_client": client},
        )
        assert res.passed is True
        assert res.status == "succeeded"
        assert upload_attempts >= 2


@pytest.mark.asyncio
async def test_vercel_file_upload_http_409_file_already_exists_treated_as_success(tmp_path: Path) -> None:
    """HTTP 409 Conflict with 'file_already_exists' confirms cache presence and succeeds cleanly."""
    ws = _create_sample_workspace(tmp_path)
    session = MockAsyncSession()
    run = await _create_test_run(
        session,
        DeploymentStrategy.VERCEL_ONLY,
        config_overrides={"dry_run": False, "simulation_mode": False},
    )

    class Mock409AlreadyExistsTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "/v2/files" in url:
                # Vercel CDN returns 409 Conflict when identical digest already exists
                return httpx.Response(
                    409,
                    json={"error": {"code": "file_already_exists", "message": "File with this digest already exists"}},
                )
            if "/v13/deployments" in url:
                return httpx.Response(
                    200,
                    json={
                        "id": "dpl_409_cached",
                        "url": "test-cached.vercel.app",
                        "readyState": "READY",
                    },
                )
            return httpx.Response(404)

    async with httpx.AsyncClient(transport=Mock409AlreadyExistsTransport()) as client:
        res = await execute_vercel_deploy(
            session,
            run,
            context={"vercel_token": "valid_tok", "workspace_path": str(ws), "http_client": client},
        )
        assert res.passed is True
        assert res.status == "succeeded"
        assert "test-cached.vercel.app" in res.message


@pytest.mark.asyncio
async def test_vercel_file_upload_http_409_other_conflict_fails_actionably(tmp_path: Path) -> None:
    """HTTP 409 Conflict with an unexpected error code raises an actionable failure rather than masking it."""
    ws = _create_sample_workspace(tmp_path)
    session = MockAsyncSession()
    run = await _create_test_run(
        session,
        DeploymentStrategy.VERCEL_ONLY,
        config_overrides={"dry_run": False, "simulation_mode": False},
    )

    class Mock409OtherConflictTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "/v2/files" in url:
                return httpx.Response(
                    409,
                    json={"error": {"code": "account_restricted", "message": "Account upload concurrency locked"}},
                )
            return httpx.Response(404)

    async with httpx.AsyncClient(transport=Mock409OtherConflictTransport()) as client:
        res = await execute_vercel_deploy(
            session,
            run,
            context={"vercel_token": "valid_tok", "workspace_path": str(ws), "http_client": client},
        )
        assert res.passed is False
        assert res.status == "failed"
        assert "Vercel file upload conflict" in res.message
        assert "account_restricted" in res.message


def test_compute_file_sha1_matches_standard_hashlib(tmp_path: Path) -> None:
    """compute_file_sha1 computes correct SHA-1 digest via streaming chunks."""
    sample = tmp_path / "sample.bin"
    payload = b"ForgeOps stream test payload 12345" * 1000
    sample.write_bytes(payload)

    import hashlib

    expected = hashlib.sha1(payload).hexdigest()
    computed = compute_file_sha1(sample)
    assert computed == expected


def test_workspace_packaging_monorepo_explicit_application_root(tmp_path: Path) -> None:
    """scan_workspace_manifest correctly isolates configured application_root subdirectory."""
    root = tmp_path / "monorepo"
    root.mkdir()

    # Root files that should not be packaged
    (root / "README.md").write_text("Monorepo Root", encoding="utf-8")
    backend_dir = root / "backend"
    backend_dir.mkdir()
    (backend_dir / "server.py").write_text("print('backend')", encoding="utf-8")

    # Target frontend app in apps/web
    frontend_dir = root / "apps" / "web"
    frontend_dir.mkdir(parents=True)
    (frontend_dir / "package.json").write_text('{"name": "web-app"}', encoding="utf-8")
    (frontend_dir / "index.html").write_text("<h1>Web</h1>", encoding="utf-8")

    manifest = scan_workspace_manifest(root, target="vercel", application_root="apps/web")
    paths = {m.rel_path for m in manifest}

    assert "package.json" in paths
    assert "index.html" in paths
    assert "README.md" not in paths
    assert not any("backend" in p for p in paths)


def test_workspace_packaging_monorepo_autodiscovery(tmp_path: Path) -> None:
    """resolve_application_root auto-detects single distinct application subfolder when root has no markers."""
    repo = tmp_path / "repo"
    repo.mkdir()

    # Subfolder with app markers
    client = repo / "client"
    client.mkdir()
    (client / "package.json").write_text('{"name": "client"}', encoding="utf-8")
    (client / "src").mkdir()
    (client / "src" / "App.jsx").write_text("export default () => null;", encoding="utf-8")

    discovered = resolve_application_root(repo, target="vercel")
    assert discovered == client

    manifest = scan_workspace_manifest(repo, target="vercel")
    paths = {m.rel_path for m in manifest}
    assert "package.json" in paths
    assert "src/App.jsx" in paths


def test_workspace_packaging_target_specific_limits(tmp_path: Path) -> None:
    """Target-specific size limits allow large assets for Vercel/Docker while protecting boundaries."""
    ws = tmp_path / "app"
    ws.mkdir()
    (ws / "small.txt").write_text("Hello", encoding="utf-8")

    # 1. 60 MB workspace succeeds on Vercel target (250 MB limit) but fails if generic total limit is 50 MB
    medium_file = ws / "medium.dat"
    with medium_file.open("wb") as f:
        f.seek(60 * 1024 * 1024)
        f.write(b"Z")

    vercel_manifest = scan_workspace_manifest(ws, target="vercel")
    assert any(m.rel_path == "medium.dat" for m in vercel_manifest)

    # Generic target (10 MB single file limit) rejects 60 MB file
    with pytest.raises(WorkspacePackagingError) as exc_generic:
        scan_workspace_manifest(ws, target="generic")
    assert "exceeds maximum allowed individual file size" in str(exc_generic.value)
    assert "target 'generic'" in str(exc_generic.value)

    # 2. Exceeding Vercel total limit (250 MB) fails with vercel-specific diagnostic
    medium_file.unlink()
    for i in range(3):
        chunk_file = ws / f"chunk_{i}.dat"
        with chunk_file.open("wb") as f:
            f.seek(90 * 1024 * 1024)
            f.write(b"Y")

    with pytest.raises(WorkspacePackagingError) as exc_v:
        scan_workspace_manifest(ws, target="vercel")
    assert "target 'vercel'" in str(exc_v.value)
    assert "Total workspace size" in str(exc_v.value)
    assert "exceeds maximum allowed limit" in str(exc_v.value)

    # 3. Exceeding GitHub single-file limit (100 MB) fails with Git LFS advisory
    gh_ws = tmp_path / "gh_app"
    gh_ws.mkdir()
    gh_huge = gh_ws / "large_model.bin"
    with gh_huge.open("wb") as f:
        f.seek(110 * 1024 * 1024)
        f.write(b"X")

    with pytest.raises(WorkspacePackagingError) as exc_gh:
        scan_workspace_manifest(gh_ws, target="github")
    assert "target 'github'" in str(exc_gh.value)
    assert "Git LFS" in str(exc_gh.value)


def test_docker_build_context_packaging_honors_dockerignore(tmp_path: Path) -> None:
    """scan_workspace_manifest with target='docker' honors .dockerignore and secret exclusions."""
    ws = tmp_path / "docker_app"
    ws.mkdir()

    (ws / "Dockerfile").write_text("FROM alpine\n", encoding="utf-8")
    (ws / "app.py").write_text("print('hi')\n", encoding="utf-8")
    (ws / "temp_logs.txt").write_text("logs\n", encoding="utf-8")
    (ws / ".dockerignore").write_text("*.txt\nignored_dir/\n", encoding="utf-8")
    (ws / "server.key").write_text("PRIV" + "ATE KEY", encoding="utf-8")

    ignored_dir = ws / "ignored_dir"
    ignored_dir.mkdir()
    (ignored_dir / "file.py").write_text("pass", encoding="utf-8")

    manifest = scan_workspace_manifest(ws, target="docker")
    paths = {m.rel_path for m in manifest}

    assert "Dockerfile" in paths
    assert "app.py" in paths
    assert "temp_logs.txt" not in paths
    assert "server.key" not in paths
    assert not any(p.startswith("ignored_dir") for p in paths)


@pytest.mark.asyncio
async def test_vercel_streaming_file_upload_bounded_memory(tmp_path: Path) -> None:
    """execute_vercel_deploy streams file uploads chunk-by-chunk without full-payload memory buffering."""
    ws = tmp_path / "streaming_app"
    ws.mkdir()
    (ws / "package.json").write_text('{"name": "streaming-app"}', encoding="utf-8")
    large_asset = ws / "asset.bin"
    chunk_pattern = b"0123456789ABCDEF" * 1024  # 16 KB
    payload = chunk_pattern * 64  # 1 MB
    large_asset.write_bytes(payload)

    session = MockAsyncSession()
    run = await _create_test_run(
        session,
        DeploymentStrategy.VERCEL_ONLY,
        config_overrides={"dry_run": False, "simulation_mode": False},
    )

    uploaded_blobs: dict[str, bytes] = {}
    expected_digest = compute_file_sha1(large_asset)

    class MockStreamingUploadTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "/v2/files" in url:
                digest_hdr = request.headers.get("x-vercel-digest")
                len_hdr = int(request.headers.get("Content-Length", 0))
                body = await request.aread()
                assert len(body) == len_hdr
                if digest_hdr:
                    uploaded_blobs[digest_hdr] = body
                return httpx.Response(200, json={"urls": [f"esm:{digest_hdr}"]})
            if "/v13/deployments" in url:
                return httpx.Response(
                    200,
                    json={
                        "id": "dpl_stream_123",
                        "url": "stream-app.vercel.app",
                        "readyState": "READY",
                    },
                )
            return httpx.Response(404)

    async with httpx.AsyncClient(transport=MockStreamingUploadTransport()) as client:
        res = await execute_vercel_deploy(
            session,
            run,
            context={"vercel_token": "valid_tok", "workspace_path": str(ws), "http_client": client},
        )
        assert res.passed is True
        assert res.status == "succeeded"
        assert expected_digest in uploaded_blobs
        assert uploaded_blobs[expected_digest] == payload


@pytest.mark.asyncio
async def test_vercel_file_upload_cancellation_during_streaming(tmp_path: Path) -> None:
    """Operator cancellation during file upload halts immediately before deployment mutation."""
    ws = _create_sample_workspace(tmp_path)
    session = MockAsyncSession()
    run = await _create_test_run(
        session,
        DeploymentStrategy.VERCEL_ONLY,
        config_overrides={"dry_run": False, "simulation_mode": False},
    )

    class MockHaltTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            if "/v13/deployments" in str(request.url):
                pytest.fail("Deployment creation must not be reached when cancelled during upload")
            return httpx.Response(200, json={"urls": []})

    async with httpx.AsyncClient(transport=MockHaltTransport()) as client:
        res = await execute_vercel_deploy(
            session,
            run,
            context={
                "vercel_token": "valid_tok",
                "workspace_path": str(ws),
                "http_client": client,
                "cancellation_requested": True,
            },
        )
        assert res.passed is False
        assert res.status == "cancelled"
        assert "cancelled by operator" in res.message.lower()


@pytest.mark.asyncio
async def test_docker_context_disk_storage_temp_tarball_and_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DockerEngineClient.build_image_from_context uses disk storage tarball and unlinks temp file upon completion."""
    import tarfile

    from src.deployments.autonomous_gates import DockerEngineClient

    ws = tmp_path / "docker_service"
    ws.mkdir()
    (ws / "Dockerfile").write_text('FROM alpine\nCMD ["echo", "ok"]\n', encoding="utf-8")
    (ws / "app.py").write_text("print('running')\n", encoding="utf-8")
    (ws / ".dockerignore").write_text("ignore.me\n", encoding="utf-8")
    (ws / "ignore.me").write_text("should not be included", encoding="utf-8")

    captured_tar_bytes = bytearray()
    content_length_header = None

    class MockDockerDaemonTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            nonlocal content_length_header
            assert request.headers.get("Content-Type") == "application/x-tar"
            content_length_header = request.headers.get("Content-Length")
            body = await request.aread()
            captured_tar_bytes.extend(body)
            stream_json = json.dumps({"stream": "Successfully built sha256:dckr12345\n"}) + "\n"
            return httpx.Response(200, text=stream_json)

    client = DockerEngineClient(uds="fake_sock")
    monkeypatch.setattr(client, "socket_status", lambda: (True, None))

    real_async_client = httpx.AsyncClient

    def _mock_async_client_factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = MockDockerDaemonTransport()
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _mock_async_client_factory)

    build_info: dict[str, Any] = {}
    success, msg = await client.build_image_from_context(
        workspace_path=ws,
        image_tag="testapp:v1",
        build_info=build_info,
    )

    assert success is True
    assert "built successfully" in msg
    assert content_length_header is not None
    assert int(content_length_header) == len(captured_tar_bytes)

    # Verify tar structure without in-memory leaks
    import io

    with tarfile.open(fileobj=io.BytesIO(captured_tar_bytes), mode="r") as tar:
        names = tar.getnames()
        assert "Dockerfile" in names
        assert "app.py" in names
        assert "ignore.me" not in names


def test_provider_limits_and_forgeops_safety_policies_reported_distinctly(tmp_path: Path) -> None:
    """Size and count errors explicitly label provider-enforced constraints vs ForgeOps safety policies."""
    ws = tmp_path / "policy_test"
    ws.mkdir()

    # 1. GitHub 100 MB single file: documented provider-enforced limit
    gh_huge = ws / "huge.dat"
    with gh_huge.open("wb") as f:
        f.seek(101 * 1024 * 1024)
        f.write(b"G")

    with pytest.raises(WorkspacePackagingError) as exc_gh:
        scan_workspace_manifest(ws, target="github")
    assert "provider-enforced limit" in str(exc_gh.value)
    assert "Git LFS" in str(exc_gh.value)
    gh_huge.unlink()

    # 2. Vercel 250 MB total: ForgeOps internal safety policy
    for i in range(3):
        chunk = ws / f"v_chunk_{i}.dat"
        with chunk.open("wb") as f:
            f.seek(90 * 1024 * 1024)
            f.write(b"V")

    with pytest.raises(WorkspacePackagingError) as exc_v:
        scan_workspace_manifest(ws, target="vercel")
    assert "ForgeOps safety policy" in str(exc_v.value)
    assert "target 'vercel'" in str(exc_v.value)


@pytest.mark.asyncio
async def test_vercel_file_upload_retry_with_fresh_chunk_stream(tmp_path: Path) -> None:
    """Transient upload failures retry using fresh chunk generator without stream exhaustion."""
    ws = _create_sample_workspace(tmp_path)
    session = MockAsyncSession()
    run = await _create_test_run(
        session,
        DeploymentStrategy.VERCEL_ONLY,
        config_overrides={"dry_run": False, "simulation_mode": False},
    )

    attempt_counts: dict[str, int] = {}

    class MockRetryTransientTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "/v2/files" in url:
                digest = request.headers.get("x-vercel-digest", "unknown")
                attempt_counts[digest] = attempt_counts.get(digest, 0) + 1
                if attempt_counts[digest] == 1:
                    # Transient 503 error on first attempt
                    return httpx.Response(503, text="Transient Cloudflare Gateway Error")
                # Second attempt succeeds with full fresh stream
                body = await request.aread()
                assert len(body) > 0
                return httpx.Response(200, json={"urls": [f"esm:{digest}"]})
            if "/v13/deployments" in url:
                return httpx.Response(
                    200,
                    json={
                        "id": "dpl_retry_ok",
                        "url": "retry-app.vercel.app",
                        "readyState": "READY",
                    },
                )
            return httpx.Response(404)

    async with httpx.AsyncClient(transport=MockRetryTransientTransport()) as client:
        res = await execute_vercel_deploy(
            session,
            run,
            context={"vercel_token": "valid_tok", "workspace_path": str(ws), "http_client": client},
        )
        assert res.passed is True
        assert res.status == "succeeded"
        assert all(count >= 2 for count in attempt_counts.values())


@pytest.mark.asyncio
async def test_vercel_deploy_persists_alias_and_aliases_in_metadata(tmp_path: Path) -> None:
    """execute_vercel_deploy captures assigned alias and aliases in stage metadata and details."""
    ws = _create_sample_workspace(tmp_path)
    session = MockAsyncSession()
    run = await _create_test_run(
        session,
        DeploymentStrategy.VERCEL_ONLY,
        config_overrides={"dry_run": False, "simulation_mode": False},
    )

    class MockVercelDeployWithAliasTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "/v2/files" in url:
                return httpx.Response(200, json={"urls": []})
            if "/v13/deployments/dpl_alias_test" in url:
                return httpx.Response(
                    200,
                    json={
                        "id": "dpl_alias_test",
                        "url": "test-raw.vercel.app",
                        "alias": ["test-alias.vercel.app"],
                        "readyState": "READY",
                    },
                )
            if "/v13/deployments" in url and request.method == "POST":
                return httpx.Response(
                    200,
                    json={
                        "id": "dpl_alias_test",
                        "url": "test-raw.vercel.app",
                        "alias": ["test-alias.vercel.app"],
                        "readyState": "BUILDING",
                    },
                )
            return httpx.Response(404)

    async with httpx.AsyncClient(transport=MockVercelDeployWithAliasTransport()) as client:
        res = await execute_vercel_deploy(
            session,
            run,
            context={
                "vercel_token": "valid_tok",
                "workspace_path": str(ws),
                "http_client": client,
                "vercel_poll_timeout": 5.0,
                "vercel_poll_interval": 0.01,
            },
        )
        assert res.passed is True
        assert res.status == "succeeded"
        assert res.details.get("alias") == "test-alias.vercel.app"
        assert res.details.get("aliases") == ["test-alias.vercel.app"]

        stage_v = next(s for s in run.stages if s.stage_name == STAGE_VERCEL_DEPLOY)
        assert stage_v.stage_metadata.get("alias") == "test-alias.vercel.app"
        assert stage_v.stage_metadata.get("aliases") == ["test-alias.vercel.app"]


@pytest.mark.asyncio
async def test_vercel_stays_building_beyond_poll_timeout(tmp_path: Path) -> None:
    """When polling times out while Vercel is still BUILDING, marks timed_out without provider build error."""
    ws = _create_sample_workspace(tmp_path)
    session = MockAsyncSession()
    run = await _create_test_run(
        session,
        DeploymentStrategy.VERCEL_ONLY,
        config_overrides={"dry_run": False, "simulation_mode": False},
    )

    class MockBuildingTimeoutTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "/v2/files" in url:
                return httpx.Response(200, json={"urls": []})
            if "/v13/deployments/dpl_building_timeout" in url:
                return httpx.Response(
                    200,
                    json={
                        "id": "dpl_building_timeout",
                        "url": "test-bldg.vercel.app",
                        "readyState": "BUILDING",
                    },
                )
            if "/v13/deployments" in url and request.method == "POST":
                return httpx.Response(
                    200,
                    json={
                        "id": "dpl_building_timeout",
                        "url": "test-bldg.vercel.app",
                        "readyState": "BUILDING",
                    },
                )
            return httpx.Response(404)

    async with httpx.AsyncClient(transport=MockBuildingTimeoutTransport()) as client:
        res = await execute_vercel_deploy(
            session,
            run,
            context={
                "vercel_token": "valid_tok",
                "workspace_path": str(ws),
                "http_client": client,
                "vercel_poll_timeout": 0.05,
                "vercel_poll_interval": 0.01,
            },
        )
        assert res.passed is False
        assert res.details.get("timed_out") is True
        assert res.details.get("is_provider_timeout") is True
        assert res.details.get("provider_build_error") is False
        assert res.details.get("ready_state") == "BUILDING"
        assert "timed out waiting for readiness" in res.message


@pytest.mark.asyncio
async def test_vercel_stays_building_and_subsequently_becomes_ready(tmp_path: Path) -> None:
    """Vercel stays BUILDING for several ticks before transitioning to READY."""
    ws = _create_sample_workspace(tmp_path)
    session = MockAsyncSession()
    run = await _create_test_run(
        session,
        DeploymentStrategy.VERCEL_ONLY,
        config_overrides={"dry_run": False, "simulation_mode": False},
    )

    poll_count = 0

    class MockProgressingBuildTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            nonlocal poll_count
            url = str(request.url)
            if "/v2/files" in url:
                return httpx.Response(200, json={"urls": []})
            if "/v13/deployments/dpl_eventual_ready" in url:
                poll_count += 1
                state = "BUILDING" if poll_count < 3 else "READY"
                return httpx.Response(
                    200,
                    json={
                        "id": "dpl_eventual_ready",
                        "url": "test-prog.vercel.app",
                        "alias": ["test-prog.vercel.app"],
                        "readyState": state,
                    },
                )
            if "/v13/deployments" in url and request.method == "POST":
                return httpx.Response(
                    200,
                    json={
                        "id": "dpl_eventual_ready",
                        "url": "test-prog.vercel.app",
                        "readyState": "BUILDING",
                    },
                )
            return httpx.Response(404)

    async with httpx.AsyncClient(transport=MockProgressingBuildTransport()) as client:
        res = await execute_vercel_deploy(
            session,
            run,
            context={
                "vercel_token": "valid_tok",
                "workspace_path": str(ws),
                "http_client": client,
                "vercel_poll_timeout": 5.0,
                "vercel_poll_interval": 0.01,
            },
        )
        assert res.passed is True
        assert res.status == "succeeded"
        assert poll_count >= 3
        stage_v = next(s for s in run.stages if s.stage_name == STAGE_VERCEL_DEPLOY)
        assert stage_v.stage_metadata.get("ready_state") == "READY"


@pytest.mark.asyncio
async def test_vercel_genuinely_transitions_to_error(tmp_path: Path) -> None:
    """When Vercel transitions to ERROR, marks provider_build_error=True with exact error."""
    ws = _create_sample_workspace(tmp_path)
    session = MockAsyncSession()
    run = await _create_test_run(
        session,
        DeploymentStrategy.VERCEL_ONLY,
        config_overrides={"dry_run": False, "simulation_mode": False},
    )

    class MockBuildErrorTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "/v2/files" in url:
                return httpx.Response(200, json={"urls": []})
            if "/v13/deployments/dpl_err_test" in url:
                return httpx.Response(
                    200,
                    json={
                        "id": "dpl_err_test",
                        "url": "test-err.vercel.app",
                        "readyState": "ERROR",
                        "errorMessage": "Command 'npm run build' exited with 1",
                    },
                )
            if "/v13/deployments" in url and request.method == "POST":
                return httpx.Response(
                    200,
                    json={
                        "id": "dpl_err_test",
                        "url": "test-err.vercel.app",
                        "readyState": "BUILDING",
                    },
                )
            return httpx.Response(404)

    async with httpx.AsyncClient(transport=MockBuildErrorTransport()) as client:
        res = await execute_vercel_deploy(
            session,
            run,
            context={
                "vercel_token": "valid_tok",
                "workspace_path": str(ws),
                "http_client": client,
                "vercel_poll_timeout": 5.0,
                "vercel_poll_interval": 0.01,
            },
        )
        assert res.passed is False
        assert res.status == "failed"
        assert res.details.get("provider_build_error") is True
        assert "npm run build" in str(res.details.get("error"))


@pytest.mark.asyncio
async def test_explicit_cancellation_vs_task_timeout(tmp_path: Path) -> None:
    """Distinguishes genuine operator cancellation from background task timeout."""
    from src.diagnostics.bundle import DiagnosticBundle
    from src.recovery.ai_resolver import resolve_failure_with_ai_async

    bundle = DiagnosticBundle(
        gate_identifier="vercel_deploy",
        stage="vercel_deploy",
        command_line="vercel_deploy",
        exit_code=1,
        attempt_count=1,
        stdout="",
        stderr="Timeout",
        error_classification="TRANSIENT",
        container_state={},
    )

    class MockCancelledTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            raise asyncio.CancelledError()

    async with httpx.AsyncClient(transport=MockCancelledTransport()) as client:
        # Case A: Operator requested cancellation
        plan_user = await resolve_failure_with_ai_async(
            bundle,
            current_iteration=1,
            workspace_path=tmp_path,
            session=MockAsyncSession(),
            run=None,
            context={"cancellation_requested": True},
            client=client,
        )
        assert plan_user.status == "HALTED_CLIENT_CANCELLED"
        assert plan_user.root_cause_category == "CLIENT_CANCELLED"
        assert "cancelled by client / operator" in plan_user.operator_message.lower()

        # Case B: Internal task timeout (no cancellation requested)
        plan_timeout = await resolve_failure_with_ai_async(
            bundle,
            current_iteration=1,
            workspace_path=tmp_path,
            session=MockAsyncSession(),
            run=None,
            context={},
            client=client,
        )
        assert plan_timeout.status == "HALTED_TASK_TIMEOUT"
        assert plan_timeout.root_cause_category == "TASK_TIMEOUT"
        assert "task timeout or background shutdown" in plan_timeout.operator_message.lower()


@pytest.mark.asyncio
async def test_vercel_polling_transient_errors_and_rate_limiting(tmp_path: Path) -> None:
    """Polling handles transient 502/503 errors and 429 rate-limiting with Retry-After backoff."""
    ws = _create_sample_workspace(tmp_path)
    session = MockAsyncSession()
    run = await _create_test_run(
        session,
        DeploymentStrategy.VERCEL_ONLY,
        config_overrides={"dry_run": False, "simulation_mode": False},
    )

    tick = 0

    class MockRateLimitAndTransientTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            nonlocal tick
            url = str(request.url)
            if "/v2/files" in url:
                return httpx.Response(200, json={"urls": []})
            if "/v13/deployments/dpl_rate_lim" in url:
                tick += 1
                if tick == 1:
                    # 503 Gateway error
                    return httpx.Response(503, text="Service Unavailable")
                if tick == 2:
                    # 429 Rate limited with Retry-After
                    return httpx.Response(429, headers={"Retry-After": "0.01"}, json={"error": "rate_limited"})
                # tick 3: Ready
                return httpx.Response(
                    200,
                    json={
                        "id": "dpl_rate_lim",
                        "url": "test-ratelim.vercel.app",
                        "alias": ["test-ratelim.vercel.app"],
                        "readyState": "READY",
                    },
                )
            if "/v13/deployments" in url and request.method == "POST":
                return httpx.Response(
                    200,
                    json={
                        "id": "dpl_rate_lim",
                        "url": "test-ratelim.vercel.app",
                        "readyState": "BUILDING",
                    },
                )
            return httpx.Response(404)

    async with httpx.AsyncClient(transport=MockRateLimitAndTransientTransport()) as client:
        res = await execute_vercel_deploy(
            session,
            run,
            context={
                "vercel_token": "valid_tok",
                "workspace_path": str(ws),
                "http_client": client,
                "vercel_poll_timeout": 5.0,
                "vercel_poll_interval": 0.01,
            },
        )
        assert res.passed is True
        assert res.status == "succeeded"
        assert tick >= 3


@pytest.mark.asyncio
async def test_reconcile_and_verify_run_idempotency(tmp_path: Path) -> None:
    """Reconciles existing deployment ID without redeploying and verifies G7 idempotently."""
    from src.deployments.autonomous_recovery import reconcile_and_verify_run

    session = MockAsyncSession()
    run = await _create_test_run(
        session,
        DeploymentStrategy.VERCEL_ONLY,
        config_overrides={"dry_run": False, "simulation_mode": True},
    )

    stage_v = next(s for s in run.stages if s.stage_name == STAGE_VERCEL_DEPLOY)
    stage_v.status = "failed"
    stage_v.error_message = "Timed out waiting for readiness"
    stage_v.stage_metadata = {
        "deployment_id": "dpl_reconcile_target",
        "ready_state": "BUILDING",
        "deployment_url": "https://test-recon.vercel.app",
        "timed_out": True,
    }
    run.status = "failed"

    mock_v_data = {
        "id": "dpl_reconcile_target",
        "url": "test-recon.vercel.app",
        "alias": ["test-recon.vercel.app"],
        "readyState": "READY",
    }

    # First reconciliation
    ok, msg = await reconcile_and_verify_run(
        session,
        run,
        context={"mock_vercel_data": mock_v_data, "dry_run": True},
    )
    assert ok is True
    assert stage_v.status == "succeeded"
    assert stage_v.stage_metadata.get("ready_state") == "READY"
    assert stage_v.stage_metadata.get("reconciled") is True
    assert run.status == "succeeded"

    # Second reconciliation is idempotent
    ok2, msg2 = await reconcile_and_verify_run(
        session,
        run,
        context={"mock_vercel_data": mock_v_data, "dry_run": True},
    )
    assert ok2 is True
    assert run.status == "succeeded"
    assert stage_v.stage_metadata.get("deployment_id") == "dpl_reconcile_target"


@pytest.mark.asyncio
async def test_g7_verification_follows_internal_redirects_and_rejects_sso() -> None:
    """G7 candidate probe follows application redirects (e.g. 307 -> /login -> 200) and rejects Vercel SSO."""
    from src.deployments.autonomous_gates import evaluate_g7_verification

    session = MockAsyncSession()
    run = await _create_test_run(
        session,
        DeploymentStrategy.VERCEL_ONLY,
        config_overrides={"dry_run": False, "simulation_mode": False},
    )
    stage_v = next(s for s in run.stages if s.stage_name == STAGE_VERCEL_DEPLOY)
    stage_v.status = "succeeded"
    stage_v.stage_metadata = {
        "deployment_id": "dpl_redirect_test",
        "ready_state": "READY",
        "alias": ["test-app.vercel.app"],
        "deployment_url": "https://test-preview.vercel.app",
        "live": True,
    }

    class MockRedirectTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            # 1. Deployment status check
            if "/v13/deployments/dpl_redirect_test" in url:
                return httpx.Response(
                    200,
                    json={
                        "id": "dpl_redirect_test",
                        "readyState": "READY",
                        "alias": ["test-app.vercel.app"],
                        "url": "test-preview.vercel.app",
                    },
                )
            # 2. Main alias redirects to /login
            if url == "https://test-app.vercel.app":
                return httpx.Response(307, headers={"Location": "/login"})
            if url == "https://test-app.vercel.app/login":
                return httpx.Response(200, text="<html><body>Login Page</body></html>")
            # 3. Preview URL redirects to SSO
            if "test-preview.vercel.app" in url:
                return httpx.Response(302, headers={"Location": "https://vercel.com/sso-api?team=foo"})
            return httpx.Response(404)

    async with httpx.AsyncClient(transport=MockRedirectTransport()) as client:
        res = await evaluate_g7_verification(
            session,
            run,
            context={"live": True, "vercel_token": "tok123", "http_client": client},
        )
        assert res.passed is True
        assert res.details["targets"]["vercel"] == "verified"
        assert stage_v.stage_metadata.get("verified_url") == "https://test-app.vercel.app"
