# SPDX-License-Identifier: FSL-1.1-ALv2
import pytest
from src.projects.github_import import GitHubAppTokenSource, GitHubImporter

pytestmark = [pytest.mark.asyncio, pytest.mark.mandatory]


async def test_installation_token_source():
    source = GitHubAppTokenSource()
    token = await source.get_installation_token(99)
    assert token.startswith("ghs_mock_installation_token_")
    assert "99" in token


async def test_github_importer():
    source = GitHubAppTokenSource()
    importer = GitHubImporter(source)
    res = await importer.import_repository(42, "parag8487", "ForgeOps")
    assert res["name"] == "ForgeOps"
    assert res["owner"] == "parag8487"
    assert res["status"] == "imported"
    assert "ghs_mock_installation_token_42" in res["token"]
