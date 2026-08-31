# SPDX-License-Identifier: FSL-1.1-ALv2
"""FR-42: the secret scan's result reaches an operator.

WHY THIS FILE EXISTS. The agent's scanner has run on every file of every index since Phase 0 — that is
what produces the redacted bodies `file_contents` stores — and `file_contents.redaction_count` has recorded
the per-file result since revision `0003`. **Nothing read it.** The only readiness check on the subject
asked whether a scanner was *configured* (`.gitleaks.toml` present), which is a different question from
what the scan found.

So "secret scanning of the codebase for hardcoded secrets" happened on every scan and left no trace an
operator could reach. These tests drive the real index route with a report carrying redaction counts, then
assert the finding is both scored and readable.

The synthetic values are assembled from fragments by `backend/tests/synthetic_secrets.py`'s convention, for
the reason `check-added-shapes` gives: shape is the violation, not sensitivity.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import AsyncClient

# client and project_id come from `readiness_app_support.py` through `conftest.py`. Imported by
# NAME they would shadow every signature that takes them as a parameter, which is the `F811` the conftest
# note about the chokepoint fixtures describes.


def _file(path: str, content: str, language: str = "yaml", redactions: int = 0) -> dict[str, Any]:
    return {
        "path": path,
        "content_hash": f"{abs(hash(path)):064x}"[:64],
        "size_bytes": len(content),
        "last_modified": datetime(2026, 9, 1, 12, 0, tzinfo=UTC).isoformat(),
        "language": language,
        "detection_tier": 2,
        "content": content,
        "redaction_count": redactions,
        "symbols_supported": False,
        "chunks": [],
    }


def _report(files: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "generated_at": datetime(2026, 9, 1, 12, 0, tzinfo=UTC).isoformat(),
        "partial": False,
        "inventory": {
            "languages": sorted({f["language"] for f in files}),
            "manifests": [],
            "config_files": [],
            "entry_points": [],
            "file_count": len(files),
            "total_size_bytes": sum(f["size_bytes"] for f in files),
        },
        "files": files,
        "dependencies": [],
        "inventory_hash": "d" * 64,
        "redaction_count": sum(f["redaction_count"] for f in files),
    }


#: The redaction marker the agent leaves behind. Assembled, because a line carrying the shape of what it
#: replaced would defeat the purpose.
MARKER = "[REDACTED:" + "aws-access-key-id" + ":f4c1]"


@pytest.mark.asyncio
async def test_a_clean_project_reports_clean(client: AsyncClient, project_id: str) -> None:
    await client.post(
        f"/api/v1/analysis/codebase/{project_id}/index",
        json=_report([_file("config/settings.yaml", "debug: false\n")]),
    )
    response = await client.get(f"/api/v1/analysis/codebase/{project_id}/secrets")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["indexed"] is True
    assert body["clean"] is True
    assert body["files_with_findings"] == 0
    assert body["total_findings"] == 0
    assert body["findings"] == []


@pytest.mark.asyncio
async def test_a_finding_is_reported_with_its_path_and_count(client: AsyncClient, project_id: str) -> None:
    await client.post(
        f"/api/v1/analysis/codebase/{project_id}/index",
        json=_report(
            [
                _file("deploy/secrets.yaml", f"token: {MARKER}\n", redactions=1),
                _file("config/prod.yaml", f"a: {MARKER}\nb: {MARKER}\nc: {MARKER}\n", redactions=3),
                _file("README.md", "# docs\n", "unknown"),
            ]
        ),
    )
    response = await client.get(f"/api/v1/analysis/codebase/{project_id}/secrets")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["clean"] is False
    assert body["files_with_findings"] == 2
    assert body["total_findings"] == 4
    # Worst first: an operator triaging wants the file with three findings, not the alphabetically first.
    assert [f["file_path"] for f in body["findings"]] == ["config/prod.yaml", "deploy/secrets.yaml"]
    assert body["findings"][0]["redaction_count"] == 3


@pytest.mark.asyncio
async def test_no_value_is_ever_returned(client: AsyncClient, project_id: str) -> None:
    """The value did not survive the redaction, so there is nothing to return even if it were acceptable."""
    await client.post(
        f"/api/v1/analysis/codebase/{project_id}/index",
        json=_report([_file("deploy/secrets.yaml", f"token: {MARKER}\n", redactions=1)]),
    )
    response = await client.get(f"/api/v1/analysis/codebase/{project_id}/secrets")
    body = response.json()
    assert "value" not in body["findings"][0]
    assert set(body["findings"][0]) == {"file_path", "redaction_count"}
    # Not even the marker travels: the response carries paths and counts only.
    assert "REDACTED" not in response.text


@pytest.mark.asyncio
async def test_an_unindexed_project_is_not_reported_clean(client: AsyncClient, project_id: str) -> None:
    """An unindexed project has no findings AND no assurance; `clean: true` would be the fail-open reading."""
    response = await client.get(f"/api/v1/analysis/codebase/{project_id}/secrets")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["indexed"] is False
    assert body["clean"] is False
    assert body["total_findings"] == 0


@pytest.mark.asyncio
async def test_the_readiness_score_reflects_the_finding(client: AsyncClient, project_id: str) -> None:
    """The other half of FR-42: a finding must cost the project points, not merely be listed."""
    await client.post(
        f"/api/v1/analysis/codebase/{project_id}/index",
        json=_report([_file("deploy/secrets.yaml", f"token: {MARKER}\n", redactions=2)]),
    )
    readiness = await client.get(f"/api/v1/projects/{project_id}/readiness")
    assert readiness.status_code == 200, readiness.text
    checks = {c["id"]: c for c in readiness.json()["checks"]}
    assert "no_secrets_found_by_scan" in checks, "FR-42's scan-clean check is absent from the score"
    assert checks["no_secrets_found_by_scan"]["passed"] is False
    assert checks["no_secrets_found_by_scan"]["points"] == 0
    # The offending path is named, which is the single most useful thing a failed check can report.
    assert checks["no_secrets_found_by_scan"]["evidence"] == "deploy/secrets.yaml"


@pytest.mark.asyncio
async def test_a_clean_index_passes_the_scan_check(client: AsyncClient, project_id: str) -> None:
    await client.post(
        f"/api/v1/analysis/codebase/{project_id}/index",
        json=_report([_file("config/settings.yaml", "debug: false\n")]),
    )
    readiness = await client.get(f"/api/v1/projects/{project_id}/readiness")
    checks = {c["id"]: c for c in readiness.json()["checks"]}
    assert checks["no_secrets_found_by_scan"]["passed"] is True


@pytest.mark.asyncio
async def test_a_project_the_caller_cannot_see_gets_the_non_disclosing_403(
    client: AsyncClient,
) -> None:
    """Q-20: the body must not distinguish an unknown id from one the caller may not see."""
    unknown = uuid.uuid4()
    response = await client.get(f"/api/v1/analysis/codebase/{unknown}/secrets")
    assert response.status_code == 403, response.text
    assert response.json()["detail"] == "You do not have permission to perform this action."
