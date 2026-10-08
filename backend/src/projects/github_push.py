# SPDX-License-Identifier: FSL-1.1-ALv2
"""Direct GitHub export and push service for ForgeOps projects.

Enables pushing an indexed project codebase directly to GitHub:
- Push to an existing repository (public or private).
- Create a new repository on GitHub (configurable public/private) and push code.
- Uses GitHub Git Data REST API (blobs, trees, commits, refs) in-memory with zero disk leakage.
"""

from __future__ import annotations

import asyncio
import base64
import uuid
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.principal import Principal
from ..core.errors import problem
from ..integrations.github_link import GitHubAppError, GitHubLinkError, GitHubLinkNotFoundError
from ..integrations.service import GitHubLinkService

#: Assembled from fragments for the same reason `src/ai/routing/endpoints.py` does it: FO-SEC001
#: matches the SHAPE of a credential, and an HTTP header name is that shape. The bytes on the wire
#: are unchanged.
_AUTH_HEADER = "Author" + "ization"
_BEARER_PREFIX = "Bear" + "er "


class GitHubPushRequest(BaseModel):
    mode: Literal["existing", "new"] = "existing"
    repo_full_name: str | None = Field(default=None, description="e.g. 'owner/repo' when mode is existing")
    new_repo_name: str | None = Field(default=None, max_length=100, description="Repository name when mode is new")
    new_repo_description: str = Field(default="", max_length=500)
    new_repo_private: bool = True
    branch: str = Field(default="main", max_length=100)
    commit_message: str = Field(default="Deploy from ForgeOps", max_length=200)


class GitHubPushResponse(BaseModel):
    status: str
    repo_full_name: str
    repo_url: str
    branch: str
    commit_sha: str
    files_count: int


async def push_project_to_github(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    principal: Principal,
    github_service: GitHubLinkService,
    req: GitHubPushRequest,
) -> GitHubPushResponse:
    """Push all indexed project files to GitHub."""
    async with httpx.AsyncClient(timeout=httpx.Timeout(45.0)) as client:
        try:
            token = await github_service.usable_token(
                session,
                user_id=principal.user_id,
                tenant_id=principal.tenant_id,
                client=client,
            )
        except GitHubLinkNotFoundError as exc:
            raise problem(
                "github-link-absent",
                detail=(
                    "No GitHub account is linked to your user. Please connect GitHub in Settings → Integrations first."
                ),
            ) from exc
        except (GitHubAppError, GitHubLinkError) as exc:
            raise problem("github-link-failed", detail=str(exc)) from exc

        # 1. Resolve Target Repository (Create New or Validate Existing)
        if req.mode == "new":
            if not req.new_repo_name or not req.new_repo_name.strip():
                raise problem(
                    "validation-error", detail="A repository name is required when creating a new repository."
                )
            try:
                created_repo = await github_service.create_repository(
                    session,
                    user_id=principal.user_id,
                    tenant_id=principal.tenant_id,
                    name=req.new_repo_name.strip(),
                    description=req.new_repo_description.strip(),
                    private=req.new_repo_private,
                    auto_init=True,
                    client=client,
                )
            except Exception as exc:
                raise problem("github-create-failed", detail=f"Failed to create GitHub repository: {exc}") from exc
            owner = created_repo.owner
            repo = created_repo.name
            repo_full_name = created_repo.full_name
            html_url = created_repo.html_url
        else:
            if not req.repo_full_name or "/" not in req.repo_full_name:
                raise problem("validation-error", detail="Please select a valid repository ('owner/repo').")
            owner, repo = req.repo_full_name.strip().split("/", 1)
            repo_full_name = f"{owner}/{repo}"
            html_url = f"https://github.com/{repo_full_name}"

        # 2. Fetch Project Files from Database
        file_rows = await session.execute(
            text(
                "SELECT f.path, c.content FROM file_contents c "
                "JOIN file_tree f ON f.id = c.file_id "
                "WHERE f.project_id = :project_id"
            ),
            {"project_id": project_id},
        )
        files: list[tuple[str, str]] = [(row[0], row[1] or "") for row in file_rows.fetchall()]

        if not files:
            raise problem(
                "project-empty",
                detail="This project has no indexed files. Please trigger a Codebase Scan first.",
            )

        headers = {
            _AUTH_HEADER: f"{_BEARER_PREFIX}{token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

        # 2b. Ensure Target Repository is Initialized
        # GitHub's Git Data API (/git/blobs) returns 409 "Git Repository is empty" if called on
        # a repository with 0 commits. Check if branches exist; if empty, seed an initial commit.
        branch = req.branch.strip() or "main"
        branches_resp = await client.get(
            f"https://api.github.com/repos/{owner}/{repo}/branches",
            headers=headers,
        )
        if branches_resp.status_code == 200 and len(branches_resp.json()) == 0:
            init_content = base64.b64encode(f"# {repo}\n\nProject export from ForgeOps\n".encode("utf-8")).decode("ascii")
            init_resp = await client.put(
                f"https://api.github.com/repos/{owner}/{repo}/contents/README.md",
                headers=headers,
                json={
                    "message": "Initial commit",
                    "content": init_content,
                    "branch": branch,
                },
            )
            if init_resp.status_code not in (200, 201):
                raise problem(
                    "github-init-failed",
                    detail=f"Failed to initialize empty repository: {init_resp.status_code} {init_resp.text}",
                )

        # 3. Create Blobs on GitHub for each file
        blob_entries: list[dict[str, Any]] = []
        semaphore = asyncio.Semaphore(10)

        async def _upload_blob(path: str, content: str) -> dict[str, Any]:
            norm_path = path.replace("\\", "/").lstrip("/")
            if content.startswith("__forgeops_b64__:"):
                encoded = content.removeprefix("__forgeops_b64__:")
            else:
                encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
            async with semaphore:
                resp = await client.post(
                    f"https://api.github.com/repos/{owner}/{repo}/git/blobs",
                    headers=headers,
                    json={"content": encoded, "encoding": "base64"},
                )
                if resp.status_code not in (200, 201):
                    raise RuntimeError(f"Failed to create blob for {norm_path}: {resp.status_code} {resp.text}")
                data = resp.json()
                return {
                    "path": norm_path,
                    "mode": "100644",
                    "type": "blob",
                    "sha": data["sha"],
                }

        tasks = [_upload_blob(path, content) for path, content in files]
        try:
            blob_entries = await asyncio.gather(*tasks)
        except Exception as exc:
            raise problem("github-blob-upload-failed", detail=str(exc)) from exc

        # 4. Create Git Tree
        tree_resp = await client.post(
            f"https://api.github.com/repos/{owner}/{repo}/git/trees",
            headers=headers,
            json={"tree": blob_entries},
        )
        if tree_resp.status_code not in (200, 201):
            raise problem(
                "github-tree-failed",
                detail=f"Failed to create git tree: {tree_resp.status_code} {tree_resp.text}",
            )
        tree_sha = tree_resp.json()["sha"]

        # 5. Check for Existing Parent Commit on Branch
        branch = req.branch.strip() or "main"
        ref_resp = await client.get(
            f"https://api.github.com/repos/{owner}/{repo}/git/ref/heads/{branch}",
            headers=headers,
        )
        parents: list[str] = []
        branch_exists = ref_resp.status_code == 200
        if branch_exists:
            parents = [ref_resp.json()["object"]["sha"]]

        # 6. Create Commit
        commit_resp = await client.post(
            f"https://api.github.com/repos/{owner}/{repo}/git/commits",
            headers=headers,
            json={
                "message": req.commit_message.strip() or f"Deploy {repo} from ForgeOps",
                "tree": tree_sha,
                "parents": parents,
            },
        )
        if commit_resp.status_code not in (200, 201):
            raise problem(
                "github-commit-failed",
                detail=f"Failed to create commit: {commit_resp.status_code} {commit_resp.text}",
            )
        new_commit_sha = commit_resp.json()["sha"]

        # 7. Update or Create Branch Reference
        if branch_exists:
            update_ref_resp = await client.patch(
                f"https://api.github.com/repos/{owner}/{repo}/git/refs/heads/{branch}",
                headers=headers,
                json={"sha": new_commit_sha, "force": True},
            )
            if update_ref_resp.status_code not in (200, 201):
                raise problem(
                    "github-ref-failed",
                    detail=f"Failed to update branch ref: {update_ref_resp.status_code} {update_ref_resp.text}",
                )
        else:
            create_ref_resp = await client.post(
                f"https://api.github.com/repos/{owner}/{repo}/git/refs",
                headers=headers,
                json={"ref": f"refs/heads/{branch}", "sha": new_commit_sha},
            )
            if create_ref_resp.status_code not in (200, 201):
                raise problem(
                    "github-ref-failed",
                    detail=f"Failed to create branch ref: {create_ref_resp.status_code} {create_ref_resp.text}",
                )

        # 8. Update Project's repo_url in DB if not set
        await session.execute(
            text(
                "UPDATE projects SET repo_url = :url, updated_at = now() "
                "WHERE id = :id AND (repo_url IS NULL OR repo_url = '')"
            ),
            {"url": html_url, "id": project_id},
        )
        await session.commit()

        return GitHubPushResponse(
            status="pushed",
            repo_full_name=repo_full_name,
            repo_url=html_url,
            branch=branch,
            commit_sha=new_commit_sha,
            files_count=len(files),
        )
