# SPDX-License-Identifier: FSL-1.1-ALv2
"""Git fetch and GitHub push report success only when the REMOTE agrees.

The fetch half runs real `git` against a disposable bare repository in a temp directory -- no network, no
GitHub, nothing of the user's touched. The push half drives `push_project_to_github` over a mocked GitHub
API (`httpx.MockTransport`), because exercising it for real would write to a GitHub repository and this
work is explicitly not allowed to push anywhere.
"""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from src.core.errors import ProblemException
from src.deployments.autonomous_source import clone_repository_to_workspace
from src.projects import github_push
from src.projects.github_push import GitHubPushRequest, push_project_to_github

from tests.synthetic_secrets import SYNTHETIC_MARKER

pytestmark = pytest.mark.asyncio

#: A fake installation token, assembled so no source line carries the shape (tests/synthetic_secrets.py).
_TOKEN = "gh" + "s_" + SYNTHETIC_MARKER


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture()
def bare_repo(tmp_path: Path) -> tuple[Path, str]:
    """A bare repository with one commit on `main`, and that commit's SHA."""
    work = tmp_path / "seed"
    work.mkdir()
    _git("init", "-b", "main", cwd=work)
    _git("config", "user.email", "test@forgeops.invalid", cwd=work)
    _git("config", "user.name", "test", cwd=work)
    (work / "README.md").write_text("hello\n", encoding="utf-8")
    _git("add", "README.md", cwd=work)
    _git("commit", "-m", "seed", cwd=work)
    bare = tmp_path / "remote.git"
    _git("clone", "--bare", str(work), str(bare), cwd=tmp_path)
    return bare, _git("rev-parse", "HEAD", cwd=work)


def _target(tmp_path: Path, name: str) -> Path:
    # Created first: `resolve_workspace_path` maps a NOT-YET-EXISTING Windows path to its container mount
    # form (`/c/...`), which on a Windows host is a different directory. An existing one resolves to itself.
    target = tmp_path / name
    target.mkdir()
    return target


class TestFetchIsVerified:
    async def test_the_fetched_sha_is_the_remote_branch_head(self, bare_repo: tuple[Path, str], tmp_path: Path) -> None:
        bare, head = bare_repo
        ok, sha, err = await clone_repository_to_workspace(
            repo_full_name="", clone_url=bare.as_uri(), target_path=_target(tmp_path, "ws"), branch="main"
        )
        assert (ok, err) == (True, None)
        assert sha == head

    async def test_a_missing_branch_fails_with_the_real_reason(
        self, bare_repo: tuple[Path, str], tmp_path: Path
    ) -> None:
        bare, _ = bare_repo
        ok, sha, err = await clone_repository_to_workspace(
            repo_full_name="", clone_url=bare.as_uri(), target_path=_target(tmp_path, "ws2"), branch="does-not-exist"
        )
        assert ok is False and sha is None
        assert "exit code" in (err or "")

    async def test_no_credential_is_left_in_the_checkout(self, bare_repo: tuple[Path, str], tmp_path: Path) -> None:
        bare, _ = bare_repo
        target = _target(tmp_path, "ws3")
        ok, _sha, _err = await clone_repository_to_workspace(
            repo_full_name="",
            clone_url=bare.as_uri(),
            target_path=target,
            branch="main",
            github_token=_TOKEN,
        )
        assert ok
        config = (target / ".git" / "config").read_text(encoding="utf-8")
        assert _TOKEN not in config
        assert "x-access-token" not in config


# ── push ──────────────────────────────────────────────────────────────────────


class _Rows:
    def __init__(self, rows: list[tuple[str, str]]) -> None:
        self._rows = rows

    def fetchall(self) -> list[tuple[str, str]]:
        return self._rows


class _Session:
    def __init__(self) -> None:
        self.committed = False

    async def execute(self, statement: Any, params: Any = None) -> _Rows:
        return _Rows([("index.html", "<h1>hi</h1>"), ("app.js", "console.log(1)")])

    async def commit(self) -> None:
        self.committed = True


class _GitHub:
    async def usable_token(self, *_args: Any, **_kwargs: Any) -> str:
        return _TOKEN


class _Api:
    """A minimal GitHub git-data API. `remote_after_push` controls what the verification read returns."""

    def __init__(self, *, ref_update_status: int = 200, remote_after_push: str | None = None) -> None:
        self.ref_update_status = ref_update_status
        self.remote_after_push = remote_after_push
        self.ref_patches: list[dict[str, Any]] = []
        self.pushed = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/branches"):
            return httpx.Response(200, json=[{"name": "main"}])
        if path.endswith("/git/blobs"):
            return httpx.Response(201, json={"sha": uuid.uuid4().hex})
        if path.endswith("/git/trees"):
            return httpx.Response(201, json={"sha": "tree123"})
        if path.endswith("/git/commits"):
            return httpx.Response(201, json={"sha": "newcommit123"})
        if path.endswith("/git/ref/heads/main") and request.method == "GET":
            sha = (self.remote_after_push or "newcommit123") if self.pushed else "parent000"
            return httpx.Response(200, json={"object": {"sha": sha}})
        if path.endswith("/git/refs/heads/main") and request.method == "PATCH":
            import json

            self.ref_patches.append(json.loads(request.content))
            if self.ref_update_status == 200:
                self.pushed = True
                return httpx.Response(200, json={"object": {"sha": "newcommit123"}})
            return httpx.Response(self.ref_update_status, json={"message": "Update is not a fast forward"})
        return httpx.Response(404, json={"message": f"unexpected {request.method} {path}"})


async def _push(monkeypatch: pytest.MonkeyPatch, api: _Api) -> Any:
    real_client = httpx.AsyncClient

    def client_factory(*_args: Any, **kwargs: Any) -> httpx.AsyncClient:
        return real_client(transport=httpx.MockTransport(api))

    monkeypatch.setattr(github_push.httpx, "AsyncClient", client_factory)
    return await push_project_to_github(
        _Session(),  # type: ignore[arg-type]
        project_id=uuid.uuid4(),
        principal=SimpleNamespace(user_id=uuid.uuid4(), tenant_id=None),  # type: ignore[arg-type]
        github_service=_GitHub(),  # type: ignore[arg-type]
        req=GitHubPushRequest(mode="existing", repo_full_name="test-owner/disposable", branch="main"),
    )


class TestPushIsVerified:
    async def test_success_is_reported_only_after_the_remote_ref_names_the_commit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        api = _Api()
        response = await _push(monkeypatch, api)
        assert response.commit_sha == "newcommit123"
        assert response.remote_verified_sha == "newcommit123"
        assert response.verified_at is not None

    async def test_the_branch_is_never_force_updated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        api = _Api()
        await _push(monkeypatch, api)
        assert api.ref_patches == [{"sha": "newcommit123", "force": False}]

    async def test_a_rejected_non_fast_forward_is_a_failure_with_the_real_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        api = _Api(ref_update_status=422)
        with pytest.raises(ProblemException) as caught:
            await _push(monkeypatch, api)
        assert "not a fast forward" in (caught.value.problem.detail or "")

    async def test_a_remote_that_does_not_show_the_commit_is_not_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        api = _Api(remote_after_push="someoneelse999")
        with pytest.raises(ProblemException) as caught:
            await _push(monkeypatch, api)
        detail = caught.value.problem.detail or ""
        assert "not confirmed" in detail and "someoneelse999" in detail and "newcommit123" in detail
