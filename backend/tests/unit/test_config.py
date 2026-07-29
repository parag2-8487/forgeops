# SPDX-License-Identifier: FSL-1.1-ALv2
"""Tests for src/core/config.py — strict project config vs ambient env tolerance.

Property P-15 focused examples: unknown project keys fail together while arbitrary
PATH, HOME, CI, shell, and editor variables are ignored.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest
from src.core.config import (
    PROJECT_CONFIG_KEYS,
    get_settings,
    load_project_dotenv,
)


class TestProjectConfigKeys:
    """Verify the PROJECT_CONFIG_KEYS inventory."""

    def test_inventory_has_74_keys(self):
        """The .env.example has 74 declared keys (design.md §13.1)."""
        assert len(PROJECT_CONFIG_KEYS) == 74, (
            f"Expected 74 keys in PROJECT_CONFIG_KEYS, got {len(PROJECT_CONFIG_KEYS)}"
        )

    def test_all_keys_are_uppercase(self):
        """All keys in the inventory should be uppercase."""
        for key in PROJECT_CONFIG_KEYS:
            assert key == key.upper(), f"Key {key!r} is not uppercase"


class TestLoadProjectDotenv:
    """Tests for load_project_dotenv."""

    def test_unknown_project_keys_raise_one_error(self, tmp_path: Path):
        """Unknown keys from project sources are errors accumulated into ONE report."""
        env_file = tmp_path / ".env.example"
        env_file.write_text(
            "APP_ENV=development\n"
            "DATABASE_URL=postgresql+asyncpg://x:y@localhost/db\n"
            "REDIS_URL=redis://localhost:6379/0\n"
            "UNKNOWN_KEY_A=foo\n"
            "UNKNOWN_KEY_B=bar\n"
        )
        with pytest.raises(ValueError, match="Unknown project configuration keys"):
            load_project_dotenv((".env.example",), base_dir=tmp_path)

    def test_unknown_keys_accumulated_together(self, tmp_path: Path):
        """All unknown keys appear in a single error message."""
        env_file = tmp_path / ".env.example"
        env_file.write_text(
            "APP_ENV=development\n"
            "REDIS_URL=redis://localhost:6379/0\n"
            "DATABASE_URL=postgresql+asyncpg://x:y@localhost/db\n"
            "BAD_KEY_ONE=1\n"
            "BAD_KEY_TWO=2\n"
            "BAD_KEY_THREE=3\n"
        )
        with pytest.raises(ValueError) as exc_info:
            load_project_dotenv((".env.example",), base_dir=tmp_path)
        msg = str(exc_info.value)
        assert "BAD_KEY_ONE" in msg
        assert "BAD_KEY_TWO" in msg
        assert "BAD_KEY_THREE" in msg

    def test_valid_keys_pass(self, tmp_path: Path):
        """All PROJECT_CONFIG_KEYS are accepted without error."""
        env_file = tmp_path / ".env.example"
        lines = [f"{key}=test_value" for key in sorted(PROJECT_CONFIG_KEYS)]
        env_file.write_text("\n".join(lines))
        result = load_project_dotenv((".env.example",), base_dir=tmp_path)
        assert len(result) == len(PROJECT_CONFIG_KEYS)

    def test_optional_override_merges(self, tmp_path: Path):
        """Second file (.env) overrides values from baseline."""
        base = tmp_path / ".env.example"
        base.write_text("APP_ENV=development\nDATABASE_URL=postgresql+asyncpg://x:y@h/d\nREDIS_URL=redis://r:6379/0\n")
        override = tmp_path / ".env"
        override.write_text("APP_ENV=production\n")
        result = load_project_dotenv((".env.example", ".env"), base_dir=tmp_path)
        assert result["APP_ENV"] == "production"

    def test_missing_optional_not_error(self, tmp_path: Path):
        """Missing optional override file is not an error."""
        base = tmp_path / ".env.example"
        base.write_text("APP_ENV=development\nDATABASE_URL=postgresql+asyncpg://x:y@h/d\nREDIS_URL=redis://r:6379/0\n")
        # No .env file exists
        result = load_project_dotenv((".env.example", ".env"), base_dir=tmp_path)
        assert result["APP_ENV"] == "development"

    def test_missing_required_baseline_raises(self, tmp_path: Path):
        """Missing required baseline file raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            load_project_dotenv((".env.example",), base_dir=tmp_path)


class TestSettingsAmbientEnv:
    """P-15: arbitrary ambient OS env vars must be IGNORED and never cause failure."""

    def test_path_home_ci_ignored(self):
        """PATH, HOME, CI, EDITOR, SHELL etc. must not cause Settings construction to fail."""
        # Provide minimum required fields via env
        env = {
            "DATABASE_URL": "postgresql+asyncpg://user:pass@localhost:5432/db",
            "REDIS_URL": "redis://localhost:6379/0",
            # These are ambient OS vars that must be tolerated:
            "PATH": "/usr/bin:/bin",
            "HOME": "/home/user",
            "CI": "true",
            "EDITOR": "vim",
            "SHELL": "/bin/bash",
            "TERM": "xterm-256color",
            "USER": "testuser",
            "LANG": "en_US.UTF-8",
            "COLORTERM": "truecolor",
            "VSCODE_PID": "12345",
        }
        with patch.dict(os.environ, env, clear=True):
            settings = get_settings()
            assert settings.app_env == "development"

    def test_settings_from_explicit_mapping(self):
        """get_settings with explicit mapping filters to Settings fields only."""
        explicit = {
            "DATABASE_URL": "postgresql+asyncpg://user:pass@localhost:5432/db",
            "REDIS_URL": "redis://localhost:6379/0",
            "APP_ENV": "test",
            "LOG_LEVEL": "DEBUG",
        }
        settings = get_settings(explicit=explicit)
        assert settings.app_env == "test"
        assert settings.log_level == "DEBUG"


class TestSettingsProductionIssuer:
    """MCP_OIDC_ISSUERS required non-empty when APP_ENV=production."""

    def test_production_requires_issuers(self):
        """Empty MCP_OIDC_ISSUERS in production must fail.

        Asserting the specific ValidationError matters: a blind `Exception` would
        also pass if the call failed for an unrelated reason (a typo in a key, a
        missing DSN), so the test would keep passing even if the production-issuer
        guard were deleted.
        """
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="MCP_OIDC_ISSUERS"):
            get_settings(
                explicit={
                    "DATABASE_URL": "postgresql+asyncpg://user:pass@localhost:5432/db",
                    "REDIS_URL": "redis://localhost:6379/0",
                    "APP_ENV": "production",
                    "MCP_OIDC_ISSUERS": "",
                }
            )

    def test_development_allows_empty_issuers(self):
        """Empty MCP_OIDC_ISSUERS in development is allowed."""
        settings = get_settings(
            explicit={
                "DATABASE_URL": "postgresql+asyncpg://user:pass@localhost:5432/db",
                "REDIS_URL": "redis://localhost:6379/0",
                "APP_ENV": "development",
                "MCP_OIDC_ISSUERS": "",
            }
        )
        assert settings.mcp_oidc_issuers == ""

    def test_production_with_issuers_succeeds(self):
        """Non-empty MCP_OIDC_ISSUERS in production succeeds."""
        settings = get_settings(
            explicit={
                "DATABASE_URL": "postgresql+asyncpg://user:pass@localhost:5432/db",
                "REDIS_URL": "redis://localhost:6379/0",
                "APP_ENV": "production",
                "MCP_OIDC_ISSUERS": "https://auth.example.com/",
            }
        )
        assert settings.mcp_oidc_issuers == "https://auth.example.com/"
