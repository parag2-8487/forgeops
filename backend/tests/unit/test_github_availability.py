# SPDX-License-Identifier: FSL-1.1-ALv2
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from src.auth.principal import Principal, UserRole
from src.core.errors import ProblemException
from src.integrations.routes import RepositoryAvailabilityResponse, check_github_repository_availability

pytestmark = [pytest.mark.asyncio]


def _make_principal(login: str = "parag2-8487") -> Principal:
    return Principal.for_user(
        user_id=uuid.uuid4(),
        subject=login,
        email=f"{login}@example.com",
        role=UserRole.DEVELOPER,
        tenant_id=uuid.uuid4(),
    )


def _make_link_record(login: str = "parag2-8487", scopes: str = "repo,read:user"):
    record = MagicMock()
    record.github_login = login
    record.scopes = scopes
    return record


async def test_availability_success_when_remote_returns_404_with_repo_scope():
    principal = _make_principal("testuser")
    mock_service = MagicMock()
    mock_service.read = AsyncMock(return_value=_make_link_record("testuser", scopes="repo,read:user"))
    mock_service.usable_token = AsyncMock(return_value="mock_token_abc123")

    mock_request = MagicMock()
    mock_request.app.state.github_link_service = mock_service
    mock_session = AsyncMock()

    with patch("httpx.AsyncClient.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.headers = {"x-oauth-scopes": "repo,read:user"}
        mock_get.return_value = mock_resp

        res = await check_github_repository_availability(
            name="new-cool-repo",
            owner=None,
            request=mock_request,
            principal=principal,
            session=mock_session,
        )

        assert isinstance(res, RepositoryAvailabilityResponse)
        assert res.available is True
        assert res.status == "available"
        assert res.name == "new-cool-repo"
        assert res.owner == "testuser"
        assert res.full_name == "testuser/new-cool-repo"
        assert "available" in res.reason


async def test_availability_unknown_when_remote_returns_404_without_repo_scope():
    principal = _make_principal("testuser")
    mock_service = MagicMock()
    mock_service.read = AsyncMock(return_value=_make_link_record("testuser", scopes="read:user"))
    mock_service.usable_token = AsyncMock(return_value="mock_token_abc123")

    mock_request = MagicMock()
    mock_request.app.state.github_link_service = mock_service
    mock_session = AsyncMock()

    with patch("httpx.AsyncClient.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.headers = {"x-oauth-scopes": "read:user"}
        mock_get.return_value = mock_resp

        res = await check_github_repository_availability(
            name="new-cool-repo",
            owner=None,
            request=mock_request,
            principal=principal,
            session=mock_session,
        )

        assert isinstance(res, RepositoryAvailabilityResponse)
        assert res.available is False
        assert res.status == "unknown"
        assert "could not confirm" in res.message
        assert "repo" in res.message


async def test_availability_conflict_when_remote_returns_200():
    principal = _make_principal("testuser")
    mock_service = MagicMock()
    mock_service.read = AsyncMock(return_value=_make_link_record("testuser"))
    mock_service.usable_token = AsyncMock(return_value="mock_token_abc123")

    mock_request = MagicMock()
    mock_request.app.state.github_link_service = mock_service
    mock_session = AsyncMock()

    with patch("httpx.AsyncClient.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {}
        mock_get.return_value = mock_resp

        res = await check_github_repository_availability(
            name="existing-repo",
            owner=None,
            request=mock_request,
            principal=principal,
            session=mock_session,
        )

        assert res.available is False
        assert res.status == "taken"
        assert res.full_name == "testuser/existing-repo"
        assert "already exists" in res.reason


async def test_availability_rejects_invalid_names_without_github_call():
    principal = _make_principal("testuser")
    mock_service = MagicMock()
    mock_service.read = AsyncMock(return_value=_make_link_record("testuser"))
    mock_request = MagicMock()
    mock_request.app.state.github_link_service = mock_service
    mock_session = AsyncMock()

    for invalid in ["invalid space", "https://github.com/a/b", ".bad", "-bad", "bad.", "bad$char", "repo.git"]:
        res = await check_github_repository_availability(
            name=invalid,
            owner=None,
            request=mock_request,
            principal=principal,
            session=mock_session,
        )
        assert res.available is False
        assert res.status == "invalid"


async def test_availability_rejects_owner_mismatch():
    principal = _make_principal("testuser")
    mock_service = MagicMock()
    mock_service.read = AsyncMock(return_value=_make_link_record("testuser"))
    mock_request = MagicMock()
    mock_request.app.state.github_link_service = mock_service
    mock_session = AsyncMock()

    res = await check_github_repository_availability(
        name="otherowner/repo",
        owner=None,
        request=mock_request,
        principal=principal,
        session=mock_session,
    )
    assert res.available is False
    assert res.status == "invalid"
    assert "does not match connected GitHub account" in res.reason


async def test_availability_normalizes_matching_owner():
    principal = _make_principal("testuser")
    mock_service = MagicMock()
    mock_service.read = AsyncMock(return_value=_make_link_record("testuser", scopes="repo"))
    mock_service.usable_token = AsyncMock(return_value="mock_token_abc123")
    mock_request = MagicMock()
    mock_request.app.state.github_link_service = mock_service
    mock_session = AsyncMock()

    with patch("httpx.AsyncClient.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.headers = {"x-oauth-scopes": "repo"}
        mock_get.return_value = mock_resp

        res = await check_github_repository_availability(
            name="testuser/repo",
            owner=None,
            request=mock_request,
            principal=principal,
            session=mock_session,
        )
        assert res.available is True
        assert res.name == "repo"
        assert res.owner == "testuser"
        assert res.full_name == "testuser/repo"


async def test_availability_fails_when_unlinked():
    principal = _make_principal("testuser")
    mock_service = MagicMock()
    mock_service.read = AsyncMock(return_value=None)
    mock_request = MagicMock()
    mock_request.app.state.github_link_service = mock_service
    mock_session = AsyncMock()

    with pytest.raises(ProblemException) as exc_info:
        await check_github_repository_availability(
            name="repo",
            owner=None,
            request=mock_request,
            principal=principal,
            session=mock_session,
        )
    assert exc_info.value.problem.status == 409
    assert "github-link-absent" in exc_info.value.problem.type


async def test_availability_handles_rate_limits():
    principal = _make_principal("testuser")
    mock_service = MagicMock()
    mock_service.read = AsyncMock(return_value=_make_link_record("testuser"))
    mock_service.usable_token = AsyncMock(return_value="mock_token_abc123")
    mock_request = MagicMock()
    mock_request.app.state.github_link_service = mock_service
    mock_session = AsyncMock()

    with patch("httpx.AsyncClient.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 403
        mock_resp.text = "API rate limit exceeded"
        mock_resp.headers = {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1700000000"}
        mock_get.return_value = mock_resp

        res = await check_github_repository_availability(
            name="repo",
            owner=None,
            request=mock_request,
            principal=principal,
            session=mock_session,
        )
        assert res.available is False
        assert res.status == "error"
        assert "rate limit exceeded" in res.message.lower()


async def test_availability_handles_network_timeout():
    principal = _make_principal("testuser")
    mock_service = MagicMock()
    mock_service.read = AsyncMock(return_value=_make_link_record("testuser"))
    mock_service.usable_token = AsyncMock(return_value="mock_token_abc123")
    mock_request = MagicMock()
    mock_request.app.state.github_link_service = mock_service
    mock_session = AsyncMock()

    with patch("httpx.AsyncClient.get", side_effect=httpx.ConnectTimeout("Connection timed out")):
        res = await check_github_repository_availability(
            name="repo",
            owner=None,
            request=mock_request,
            principal=principal,
            session=mock_session,
        )
        assert res.available is False
        assert res.status == "error"
        assert "Could not connect" in res.message
