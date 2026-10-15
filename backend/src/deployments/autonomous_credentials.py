# SPDX-License-Identifier: FSL-1.1-ALv2
"""Secure worker credential resolution and propagation for autonomous deployments.

Implements:
1. resolve_deployment_credentials:
   - Queries stored credentials strictly scoped to the user who created the deployment (`run.created_by`).
   - Unseals GitHub personal/OAuth tokens from `github_account_links`.
   - Unseals Vercel access tokens from `provider_credentials`.
   - Cryptographically binds token unsealing to `user_id` as AES-256-GCM AAD.
   - Enforces credential presence, expiration validation, and authorization boundaries.
   - Never exposes tokens to logs, ARQ payloads, or API responses.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.config import get_settings
from src.integrations.github_link import derive_link_key, unseal_token

if TYPE_CHECKING:
    from .autonomous_models import AutonomousDeployment

logger = logging.getLogger(__name__)


def _active_targets_for_strategy(strategy: str) -> set[str]:
    """Returns the set of active deployment targets for a given strategy."""
    strat = strategy.lower()
    if strat in ("docker_github_vercel",):
        return {"docker", "github", "vercel"}
    if strat in ("docker_github",):
        return {"docker", "github"}
    if strat in ("github_only",):
        return {"github"}
    if strat in ("vercel_only",):
        return {"vercel"}
    return set()


async def resolve_deployment_credentials(
    session: AsyncSession,
    run: AutonomousDeployment,
) -> dict[str, Any]:
    """Securely resolves and unseals GitHub and Vercel credentials for run.created_by.

    Returns a dictionary of resolved credential contexts for in-memory evaluator use.
    Guarantees:
    - Queries ONLY credentials belonging to run.created_by.
    - Uses AES-256-GCM authenticated decryption with user_id as additional authenticated data.
    - Scoped strictly to the active targets required by run.strategy.
    - Missing or expired credentials are explicitly flagged in credential_status.
    """
    pepper = None
    try:
        settings = get_settings()
        raw_pepper = settings.envelope_pepper
        pepper = raw_pepper.get_secret_value() if hasattr(raw_pepper, "get_secret_value") else raw_pepper
    except Exception:
        pass
    if not pepper:
        pepper = os.environ.get("ENVELOPE_PEPPER", "")
    if not pepper:
        from pathlib import Path

        for env_candidate in [
            Path.cwd() / ".env",
            Path(__file__).resolve().parents[3] / ".env",
            Path(__file__).resolve().parents[4] / ".env",
        ]:
            if env_candidate.is_file():
                try:
                    for line in env_candidate.read_text(encoding="utf-8").splitlines():
                        if line.strip().startswith("ENVELOPE_PEPPER="):
                            pepper = line.split("=", 1)[1].strip().strip('"').strip("'")
                            break
                except Exception:
                    pass
                if pepper:
                    break

    if not pepper:
        logger.warning("ENVELOPE_PEPPER not configured; cannot unseal deployment credentials.")
        return {
            "github_token": None,
            "vercel_token": None,
            "credential_status": {
                "github": "unconfigured_pepper",
                "vercel": "unconfigured_pepper",
            },
        }

    link_key = derive_link_key(pepper)
    user_id = run.created_by
    active_targets = _active_targets_for_strategy(str(run.strategy))

    resolved: dict[str, Any] = {
        "github_token": None,
        "github_login": None,
        "vercel_token": None,
        "credential_status": {},
    }

    # 1. Resolve GitHub credentials if required
    if "github" in active_targets:
        try:
            gh_res = await session.execute(
                text(
                    "SELECT access_token_sealed, access_token_expires_at, refresh_token_sealed, "
                    "refresh_token_expires_at, scopes, github_login, github_user_id "
                    "FROM github_account_links WHERE user_id = :user_id"
                ),
                {"user_id": user_id},
            )
            gh_row = gh_res.mappings().first()
            if gh_row is None or not gh_row["access_token_sealed"]:
                resolved["credential_status"]["github"] = "missing_credentials"
            else:
                raw_sealed = gh_row["access_token_sealed"]
                sealed_bytes = bytes(raw_sealed) if not isinstance(raw_sealed, bytes) else raw_sealed
                token = unseal_token(sealed_bytes, user_id=user_id, key=link_key)

                expires_at = gh_row["access_token_expires_at"]
                if expires_at is not None:
                    if expires_at.tzinfo is None:
                        expires_at = expires_at.replace(tzinfo=UTC)
                    if expires_at <= datetime.now(UTC):
                        resolved["credential_status"]["github"] = "expired_token"
                        token = None

                if token:
                    resolved["github_token"] = token
                    resolved["github_login"] = gh_row["github_login"]
                    resolved["credential_status"]["github"] = "ready"
        except Exception as exc:
            logger.warning("Failed to unseal GitHub credentials for user %s: %s", user_id, exc)
            resolved["credential_status"]["github"] = f"unseal_error: {type(exc).__name__}"

    # 2. Resolve Vercel credentials if required
    if "vercel" in active_targets:
        try:
            v_res = await session.execute(
                text("SELECT encrypted_value FROM provider_credentials WHERE key_ref = :ref"),
                {"ref": f"vercel:{user_id}"},
            )
            v_row = v_res.mappings().first()
            if v_row is None or not v_row["encrypted_value"]:
                resolved["credential_status"]["vercel"] = "missing_credentials"
            else:
                raw_sealed = v_row["encrypted_value"]
                sealed_bytes = bytes(raw_sealed) if not isinstance(raw_sealed, bytes) else raw_sealed
                v_token = unseal_token(sealed_bytes, user_id=user_id, key=link_key)
                if v_token and v_token.strip():
                    resolved["vercel_token"] = v_token.strip()
                    resolved["credential_status"]["vercel"] = "ready"
                else:
                    resolved["credential_status"]["vercel"] = "empty_token"
        except Exception as exc:
            logger.warning("Failed to unseal Vercel credentials for user %s: %s", user_id, exc)
            resolved["credential_status"]["vercel"] = f"unseal_error: {type(exc).__name__}"

    return resolved
