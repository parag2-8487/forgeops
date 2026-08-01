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

    def test_inventory_matches_the_committed_baseline_exactly(self):
        """The inventory and `.env.example` must agree, in both directions.

        This asserted a literal `== 74` for the Phase 0 inventory. Phase 1 adds 66
        keys (design §13.1), so a hard-coded count is a number to bump every phase and
        proves nothing beyond arithmetic. Comparing the inventory against the committed
        baseline instead is what the count was standing in for: a key in the inventory
        but missing from the file is a value a fresh clone cannot supply, and a key in
        the file but not the inventory is rejected by `load_project_dotenv`.
        """
        from src.core.config import load_project_dotenv

        baseline = set(load_project_dotenv((".env.example",)))
        assert baseline == set(PROJECT_CONFIG_KEYS), {
            "in the file but not registered": sorted(baseline - set(PROJECT_CONFIG_KEYS)),
            "registered but not in the file": sorted(set(PROJECT_CONFIG_KEYS) - baseline),
        }

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

    @pytest.fixture(autouse=True)
    def _no_ambient_project_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Scrub registered project variables for these two tests (finding 57).

        `get_settings(explicit=...)` supplies the keys it names; every other registered key still
        arrives from the OS environment, and both tests below assert that a **specific** validation
        error fires. On a machine where `.env` has been exported — which is what `make init-env`
        plus `docker compose` produce — `MCP_AGENT_BLAST_RADIUS=read_only` raises its own production
        guard first and the `match=` never sees `MCP_OIDC_ISSUERS`.

        Scoped to this class rather than module-wide on purpose: `TestProjectConfigKeys` and the
        ambient-tolerance tests below read the real environment deliberately, and a module-wide
        scrub would quietly change what they are testing.
        """
        for name in PROJECT_CONFIG_KEYS:
            monkeypatch.delenv(name, raising=False)

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
        """Non-empty MCP_OIDC_ISSUERS in production succeeds.

        Phase 1 (task 3.1) adds a second production rule: the auth, envelope and
        internal-CA credentials must all be non-empty when APP_ENV=production, so a
        production boot cannot silently run without an issuer, an HMAC pepper or a CA.
        They are supplied here as obvious placeholders; this test is about the issuer
        clause, and `test_config_phase1.py` covers the credential clause directly.
        """
        settings = get_settings(
            explicit={
                "DATABASE_URL": "postgresql+asyncpg://user:pass@localhost:5432/db",
                "REDIS_URL": "redis://localhost:6379/0",
                "APP_ENV": "production",
                "MCP_OIDC_ISSUERS": "https://auth.example.com/",
                "OIDC_ISSUER": "https://auth.example.com/",
                "OIDC_CLIENT_ID": "forgeops-frontend",
                "OIDC_CLIENT_SECRET": "change-me-locally",
                "ENVELOPE_PEPPER": "change-me-locally",
                "INTERNAL_CA_CERT_PEM": "placeholder-not-a-real-certificate",
                "INTERNAL_CA_KEY_PEM": "placeholder-not-a-real-key",
            }
        )
        assert settings.mcp_oidc_issuers == "https://auth.example.com/"
