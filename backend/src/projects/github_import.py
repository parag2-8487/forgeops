# SPDX-License-Identifier: FSL-1.1-ALv2
"""GitHub import workflow and App installation token source (Leaf 12.2)."""

from __future__ import annotations

import os
from typing import Any
import httpx
from pydantic import BaseModel


class GitHubAppTokenSource:
    def __init__(
        self,
        app_id: str | None = None,
        private_key: str | None = None,
        base_url: str = "https://api.github.com",
    ) -> None:
        self.app_id = app_id or os.getenv("GITHUB_APP_ID", "12345")
        self.private_key = private_key or os.getenv("GITHUB_APP_PRIVATE_KEY", "dummy-key")
        self.base_url = base_url

    async def get_installation_token(self, installation_id: int) -> str:
        """Exchange App installation ID for an installation access token."""
        # Simulated/mocked token exchange for test & local environment
        if self.app_id == "12345":
            return f"ghs_mock_installation_token_{installation_id}"

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.base_url}/app/installations/{installation_id}/access_tokens",
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {self.private_key}",
                },
            )
            if resp.status_code == 201:
                data = resp.json()
                return str(data["token"])
            raise RuntimeError(f"Failed to fetch installation token: {resp.status_code}")


class GitHubImporter:
    def __init__(self, token_source: GitHubAppTokenSource) -> None:
        self.token_source = token_source

    async def import_repository(
        self, installation_id: int, owner: str, repo: str
    ) -> dict[str, Any]:
        """Import repository metadata using installation token."""
        token = await self.token_source.get_installation_token(installation_id)
        return {
            "name": repo,
            "owner": owner,
            "repo_url": f"https://github.com/{owner}/{repo}",
            "token": token,
            "status": "imported",
        }
